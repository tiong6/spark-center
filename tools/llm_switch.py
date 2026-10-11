"""Explicit, sequential model switches using the existing residency guard."""
import json
import os
import fcntl
import tempfile
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request

from tools import ollama_guard as guard
from backend import stability

UNIT = 'strata-server.service'
GUARDED = ('127.0.0.1', 11436)


class SwitchError(RuntimeError):
    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail
        super().__init__(detail or code)


def service_info():
    p = subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'LoadState',
                        '-p', 'ActiveState', '-p', 'ExecStart'],
                       capture_output=True, text=True, timeout=5, check=True)
    info = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
    wrapper = str(Path(guard.__file__).resolve()) + ' strata -- '
    return {'available': info.get('LoadState') == 'loaded' and wrapper in info.get('ExecStart', ''),
            'service_state': info.get('ActiveState', 'unknown')}


def control_info():
    try:
        return service_info()
    except (OSError, subprocess.SubprocessError):
        return {'available': False, 'service_state': 'unknown'}


def service(action):
    p = subprocess.run(['systemctl', '--user', action, UNIT], capture_output=True,
                       text=True, timeout=120, check=False)
    if p.returncode:
        raise SwitchError('service_failed', p.stderr.strip()[-500:])


def read(address, path):
    try:
        return guard.read_json(address, path)
    except (OSError, ValueError, guard.http.client.HTTPException) as exc:
        raise SwitchError('state_unknown', str(exc)) from exc


def post(address, path, body):
    req = urllib.request.Request('http://%s:%s%s' % (*address, path),
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('error'):
            raise SwitchError('request_failed', str(result))
        return result
    except urllib.error.HTTPError as exc:
        raise SwitchError('request_failed', exc.read().decode(errors='replace')[:500]) from exc
    except (OSError, ValueError) as exc:
        raise SwitchError('request_failed', str(exc)) from exc


def models(path='/api/ps'):
    result = read(guard.UPSTREAM, path)
    if not isinstance(result, dict) or not isinstance(result.get('models'), list):
        raise SwitchError('state_unknown')
    return result['models']


def no_pending():
    if guard.PENDING.exists() and any(guard.PENDING.iterdir()):
        raise SwitchError('pending')


def wait_for(check, timeout, code):
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise SwitchError(code)
        time.sleep(.5)


def drain(action, token=None):
    req = urllib.request.Request('http://%s:%s/admin/drain' % guard.STRATA,
                                 data=json.dumps({'action': action, 'token': token}).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            result = json.load(response)
        if not isinstance(result, dict) or 'error' in result:
            raise ValueError('invalid drain reply')
        return result
    except (OSError, ValueError) as exc:
        raise SwitchError('drain_failed', str(exc)) from exc


class Switcher:
    def __init__(self, state_path=None):
        self.lock = threading.Lock()
        self.state_path = Path(state_path) if state_path else None
        self.hold = None
        self.owned_drain = None
        self.state = {'status': 'idle'}
        if self.state_path and self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text())
                if not isinstance(self.state, dict) or self.state.get('status') not in ('idle', 'running', 'done', 'error'):
                    raise ValueError('invalid state')
                if self.state.get('status') == 'running':
                    self.state.update(status='error', error='interrupted', finished=time.time(),
                                      detail='Previous phase: ' + str(self.state.get('phase')))
            except (OSError, ValueError):
                self.state = {'status': 'error', 'error': 'interrupted', 'detail': 'State file unreadable'}

    def persist(self):
        if self.state_path:
            self.state_path.parent.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.state_path.parent, delete=False) as f:
                json.dump(self.state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            Path(f.name).replace(self.state_path)

    def snapshot(self):
        with self.lock:
            return {**{k: v for k, v in self.state.items() if k != 'drain_token'},
                    'recoverable': bool(self.state.get('drain_token')) and self.state.get('status') == 'error'}

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            self.persist()
        stability.event('switch', {k: v for k, v in values.items() if k != 'drain_token'})

    def start(self, target, model=None, keep_alive='5m'):
        if target not in ('strata', 'ollama') or keep_alive not in ('5m', '30m', '2h', '-1'):
            raise SwitchError('invalid')
        if target == 'ollama' and (not isinstance(model, str) or not model):
            raise SwitchError('invalid')
        with self.lock:
            if self.state['status'] == 'running':
                raise SwitchError('busy')
            if self.state_path:
                self.state_path.parent.mkdir(exist_ok=True)
                self.hold = self.state_path.with_suffix('.lock').open('a')
                try:
                    fcntl.flock(self.hold, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.hold.close()
                    self.hold = None
                    raise SwitchError('busy')
            old_token = self.state.get('drain_token')
            self.state = {'drain_token': old_token, 'status': 'running', 'phase': 'checking', 'target': target,
                          'model': model, 'started': time.time(), 'error': None}
            try:
                self.persist()
            except OSError as exc:
                self.state.update(status='error', error='state_store_failed')
                if self.hold:
                    self.hold.close()
                    self.hold = None
                raise SwitchError('state_store_failed', str(exc)) from exc
        try:
            threading.Thread(target=self.run, args=(target, model, keep_alive), daemon=True).start()
        except RuntimeError as exc:
            if self.hold:
                self.hold.close()
                self.hold = None
            self.update(status='error', error='request_failed', detail=str(exc))
            raise SwitchError('request_failed', str(exc)) from exc

    def run(self, target, model, keep_alive):
        try:
            info = service_info()
            if not info['available']:
                raise SwitchError('service_unconfigured')
            g = read(GUARDED, '/guard/status')
            if not isinstance(g, dict) or g.get('ok') is not True:
                raise SwitchError('state_unknown')
            no_pending()
            old_token = self.state.get('drain_token')
            if old_token and info['service_state'] == 'active':
                if models():
                    raise SwitchError('ollama_busy')
                gate = read(guard.STRATA, '/admin/drain')
                if not isinstance(gate, dict) or 'draining' not in gate:
                    raise SwitchError('drain_failed')
                if gate['draining']:
                    drain('resume', old_token)
                self.update(drain_token=None)
            if target == 'ollama':
                # Validate before stopping Strata; a typo must never trigger a download.
                if model not in [m.get('name') for m in models('/api/tags')]:
                    raise SwitchError('not_installed')
                self.to_ollama(model, keep_alive, info)
            else:
                self.to_strata(info)
            self.update(status='done', phase='done', finished=time.time())
        except SwitchError as exc:
            self.update(status='error', error=exc.code, detail=exc.detail, finished=time.time())
        except Exception as exc:
            self.update(status='error', error='request_failed', detail=str(exc), finished=time.time())
        finally:
            try:
                if self.owned_drain:
                    try:
                        if service_info()['service_state'] == 'active':
                            drain('resume', self.owned_drain)
                        self.update(drain_token=None)
                    except SwitchError as exc:
                        self.update(status='error', error='drain_failed', detail=exc.detail)
            finally:
                self.owned_drain = None
                if self.hold:
                    self.hold.close()
                    self.hold = None

    def to_ollama(self, model, keep_alive, info):
        if info['service_state'] == 'active':
            h = read(guard.STRATA, '/health')
            s = read(guard.STRATA, '/status')
            if not isinstance(h, dict) or h.get('service') != 'strata' or not isinstance(s, dict):
                raise SwitchError('state_unknown')
            if s.get('busy') is not False or s.get('queued') != 0:
                raise SwitchError('strata_busy')
            result = drain('drain')
            if not result.get('token'):
                raise SwitchError('drain_failed')
            self.owned_drain = result['token']
            self.update(phase='draining_strata', drain_token=result['token'])
            if result.get('active_requests') != 0:
                raise SwitchError('strata_busy')
            self.update(phase='stopping_strata')
            service('stop')
        elif info['service_state'] not in ('inactive', 'failed'):
            raise SwitchError('state_unknown')
        self.update(phase='waiting_strata')
        def stopped():
            return (service_info()['service_state'] in ('inactive', 'failed')
                    and not guard.strata_process() and guard.blocked_reason() is None)
        wait_for(stopped, 30, 'strata_not_stopped')
        self.update(phase='loading_ollama')
        # This request holds the existing shared lease through the full response.
        post(GUARDED, '/api/generate', {'model': model, 'keep_alive': keep_alive, 'stream': False})
        self.update(phase='verifying')
        wait_for(lambda: any(m.get('name') == model for m in models()), 15, 'not_loaded')

    def to_strata(self, info):
        if info['service_state'] == 'active':
            h = read(guard.STRATA, '/health')
            if not isinstance(h, dict) or h.get('service') != 'strata' or models():
                raise SwitchError('state_unknown')
        elif info['service_state'] in ('inactive', 'failed'):
            if guard.blocked_reason() is not None or guard.strata_process():
                raise SwitchError('state_unknown')
            hold = guard.lease(exclusive=True)
            if hold is None:
                raise SwitchError('ollama_busy')
            try:
                no_pending()
                self.update(phase='unloading_ollama')
                for m in models():
                    if not m.get('name'):
                        raise SwitchError('state_unknown')
                    post(GUARDED, '/api/generate', {'model': m['name'], 'keep_alive': 0, 'stream': False})
                wait_for(lambda: not models(), 60, 'not_unloaded')
            finally:
                hold.close()
            # The wrapper reacquires the exclusive lease. If a new Ollama request
            # wins this handoff, the wrapper refuses; never bypass or retry it.
            self.update(phase='starting_strata')
            service('start')
        else:
            raise SwitchError('state_unknown')
        self.update(phase='loading_strata')
        def ready():
            info = service_info()
            if info['service_state'] in ('failed', 'inactive', 'deactivating'):
                raise SwitchError('service_failed')
            if info['service_state'] != 'active':
                return False
            try:
                h = guard.read_json(guard.STRATA, '/health')
            except (OSError, ValueError, guard.http.client.HTTPException):
                return False
            if not isinstance(h, dict) or h.get('service') != 'strata':
                return False
            if h.get('loaded') is False:
                post(guard.STRATA, '/load', {})
                return False
            return h.get('loaded') is True
        wait_for(ready, 600, 'not_loaded')
        if models():
            raise SwitchError('state_unknown')


SWITCH = Switcher(guard.DATA / 'llm-switch.json')
