#!/usr/bin/env python3
"""Loopback admission proxy. No model loads while Strata owns shared memory.

Run `serve` on 11436; real Ollama stays on 11434. `strata -- COMMAND` holds
the exclusive lease for Strata's lifetime. Ollama requests take shared leases.
Direct callers of 11434 are outside this guard; never advertise a global lock.
"""
import fcntl
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DATA = Path(__file__).resolve().parents[1] / 'data'
LOCK = DATA / 'llm-residency.lock'
EVENT = DATA / 'llm-guard-event.json'
PENDING = DATA / 'llm-guard-pending'
UPSTREAM = ('127.0.0.1', 11434)
STRATA = ('127.0.0.1', 8080)
_event_lock = threading.Lock()
_last_notice = 0
MESSAGES = {
    'strata_resident': ('Strata 正在駐留或啟動；已拒絕 Ollama 載入。請先停止 Strata，再重試。',
                        'Strata is resident or starting. Stop Strata before retrying Ollama.'),
    'strata_unknown': ('無法確認 Strata 是否佔用記憶體；暫停 Ollama 載入，請檢查 Strata 服務。',
                       'Strata residency is unknown. Check Strata before loading Ollama.'),
    'ollama_resident': ('Ollama 尚有模型駐留或請求執行中；請先卸載，再啟動 Strata。',
                        'Ollama has resident models or active requests. Unload them before starting Strata.'),
}


def read_json(address, path):
    c = http.client.HTTPConnection(*address, timeout=2)
    try:
        c.request('GET', path)
        r = c.getresponse()
        if r.status != 200:
            return None
        return json.loads(r.read())
    finally:
        c.close()


def strata_process():
    # Covers starting/unresponsive instances, including non-systemd launches.
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = p.read_bytes().split(b'\0')
            exe = Path(os.fsdecode(args[0])).name if args else ''
            if exe.startswith('strata-') or exe == 'strata':
                return True
            if exe.startswith('python') and len(args) > 1 and args[1].endswith(b'/Strata/serve/server.py'):
                return True
            if exe.startswith('python') and b'--engine' in args and b'strata' in args:
                return True
        except OSError:
            pass
    return False


def blocked_reason():
    try:
        h = read_json(STRATA, '/health')
    except ConnectionRefusedError:
        return 'strata_unknown' if strata_process() else None
    except (OSError, ValueError, http.client.HTTPException):
        return 'strata_unknown'
    if not isinstance(h, dict) or h.get('service') != 'strata':
        return 'strata_unknown'
    # A serving Strata may auto-load on its next request. Reserve until stopped.
    return 'strata_resident'


def lease(exclusive=False):
    DATA.mkdir(exist_ok=True)
    f = LOCK.open('a')
    try:
        fcntl.flock(f, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def record_block(reason, model):
    global _last_notice
    with _event_lock:
        DATA.mkdir(exist_ok=True)
        event = {'at': time.time(), 'reason': reason, 'model': str(model or '')[:200]}
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=DATA, prefix='llm-guard-', delete=False) as f:
                json.dump(event, f, ensure_ascii=False)
            Path(f.name).replace(EVENT)
        except OSError:
            pass  # Notification/log failure must not bypass admission refusal.
        if time.monotonic() - _last_notice > 60:
            _last_notice = time.monotonic()
            try:
                subprocess.run(['notify-send', '-a', 'Spark Center', '-u', 'normal',
                                '模型載入已暫停 / Model load blocked', MESSAGES[reason][0]],
                               timeout=3, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                pass


def is_unload(path, body):
    return (path == '/api/generate' and body.get('keep_alive') in (0, '0', '0s')
            and not any(body.get(k) for k in ('prompt', 'messages', 'input', 'images', 'suffix', 'context')))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # Never log prompts or authorization headers.

    def reply(self, code, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        path = urlsplit(self.path).path
        # Local service-to-service endpoint, no browser cross-origin requests.
        host = self.headers.get('Host', '').split(':')[0]
        if host not in ('127.0.0.1', 'localhost') or self.headers.get('Origin'):
            return self.reply(403, {'error': 'Local service clients only'})
        if self.command == 'GET' and path == '/guard/status':
            reason = blocked_reason()
            f = lease()
            if f is None:
                reason = 'strata_resident'
            else:
                f.close()
            try:
                event = json.loads(EVENT.read_text())
            except (OSError, ValueError):
                event = None
            return self.reply(200, {'ok': True, 'blocked': bool(reason), 'reason': reason, 'last_block': event})
        allowed = {'GET': {'/api/tags', '/api/ps', '/api/version', '/v1/models', '/'},
                   'POST': {'/api/show', '/api/generate', '/api/chat', '/api/embed', '/api/embeddings',
                            '/v1/chat/completions', '/v1/completions', '/v1/embeddings', '/v1/responses'}}
        if path not in allowed.get(self.command, set()):
            return self.reply(404, {'error': 'Unsupported proxy route'})
        raw, body, hold = b'', {}, None
        if self.command == 'POST':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    raise ValueError()
                raw = self.rfile.read(size)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, OSError):
                return self.reply(400, {'error': 'Expected a JSON object with Content-Length, at most 32 MiB'})
            if path != '/api/show' and not is_unload(path, body):
                hold = lease()
                reason = 'strata_resident' if hold is None else blocked_reason()
                if reason:
                    if hold:
                        hold.close()
                    record_block(reason, body.get('model'))
                    return self.reply(409, {'error': MESSAGES[reason][0] + ' ' + MESSAGES[reason][1],
                                            'code': reason})
        marker = None
        if hold:
            PENDING.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=PENDING, delete=False) as f:
                json.dump({'model': body.get('model'), 'at': time.time()}, f)
            marker = Path(f.name)
        c = http.client.HTTPConnection(*UPSTREAM, timeout=1800)
        sent = False
        try:
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in ('content-type', 'authorization', 'accept')}
            c.request(self.command, self.path, body=raw or None, headers=headers)
            r = c.getresponse()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() in ('content-type', 'content-length'):
                    self.send_header(k, v)
            # Decode upstream chunk framing; stream each available chunk immediately.
            self.send_header('Connection', 'close')
            self.end_headers()
            sent = True
            self.close_connection = True
            disconnected = False
            while chunk := r.read1(65536):
                if not disconnected:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        # Finish draining upstream while retaining the lease.
                        disconnected = True
            # read1() can return EOF without raising on a truncated fixed-length
            # body. Do not treat that as proof the upstream request completed.
            if r.length is not None and r.length > 0:
                raise http.client.IncompleteRead(b'', r.length)
            if marker:
                marker.unlink()
        except (OSError, http.client.HTTPException):
            if not sent:
                self.reply(502, {'error': 'Ollama upstream unavailable'})
        finally:
            c.close()
            if hold:
                hold.close()


def run_strata(command):
    hold = lease(exclusive=True)
    reason = 'ollama_resident' if hold is None else None
    if not reason:
        if PENDING.exists() and any(PENDING.iterdir()):
            reason = 'ollama_resident'  # Interrupted proxy: state requires operator inspection.
    if not reason:
        try:
            ps = read_json(UPSTREAM, '/api/ps')
            if not isinstance(ps, dict) or not isinstance(ps.get('models'), list) or ps['models']:
                reason = 'ollama_resident'
        except (OSError, ValueError, http.client.HTTPException):
            reason = 'ollama_resident'  # Unknown Ollama state also fails closed.
    if reason:
        record_block(reason, 'Strata')
        print(MESSAGES[reason][0], file=sys.stderr)
        if hold:
            hold.close()
        return 75
    try:
        child = subprocess.Popen(command)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda signum, frame: child.send_signal(signum))
        return child.wait()
    finally:
        hold.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        ThreadingHTTPServer(('127.0.0.1', 11436), Handler).serve_forever()
    elif sys.argv[1:3] == ['strata', '--'] and len(sys.argv) > 3:
        sys.exit(run_strata(sys.argv[3:]))
    else:
        sys.exit('Usage: ollama_guard.py serve | strata -- COMMAND ...')
