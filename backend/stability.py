"""Local memory telemetry, bounded event history and conservative admission.

No prompt content, network shares or model files are read. Limits are policy,
not an estimate of CUDA allocation capacity. Calibration records its evidence.
"""
import json
import contextlib
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DB = DATA / 'stability.sqlite3'
POLICY = DATA / 'memory-policy.json'
_LIMIT = 5000
_SAMPLE_LIMIT = 5000
_lock = threading.Lock()
_latest = {'available': False}
_started = False


def memory_sample():
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    psi = {}
    for line in Path('/proc/pressure/memory').read_text().splitlines():
        name, *fields = line.split()
        psi[name] = {k: float(v) for k, v in (field.split('=') for field in fields)}
    vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    return {'at': time.time(), 'total': mem['MemTotal'], 'available_bytes': mem['MemAvailable'],
            'swap_used': mem['SwapTotal'] - mem['SwapFree'], 'swap_total': mem['SwapTotal'],
            'pswpin': int(vm['pswpin']), 'pswpout': int(vm['pswpout']),
            'page_size': os.sysconf('SC_PAGE_SIZE'), 'psi': psi,
            'baseline_bytes': sum(mem.get(k, 0) for k in ('AnonPages', 'Shmem', 'SUnreclaim', 'PageTables', 'KernelStack'))}


@contextlib.contextmanager
def connect():
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DB, timeout=2)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL, kind TEXT, detail TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    try:
        with c:
            c.execute('CREATE TABLE IF NOT EXISTS memory_samples (id TEXT PRIMARY KEY, at REAL, detail TEXT)')
            if not c.execute("SELECT 1 FROM meta WHERE key='separate_memory_samples_v1'").fetchone():
                c.execute("INSERT OR IGNORE INTO memory_samples SELECT id,at,detail FROM events WHERE kind='memory_sample'")
                c.execute("DELETE FROM events WHERE kind='memory_sample'")
                c.execute("INSERT OR IGNORE INTO meta VALUES ('separate_memory_samples_v1','1')")
            yield c
    finally:
        c.close()


def event(kind, detail, *, key=None, at=None):
    """Best-effort recording must never bypass an admission denial."""
    try:
        now = time.time() if at is None else at
        with connect() as c:
            encoded = json.dumps(detail, ensure_ascii=False)
            if len(encoded) > 4000:
                encoded = json.dumps({'summary': encoded[:3500]}, ensure_ascii=False)
            identity = key or f'{time.time_ns()}-{os.getpid()}'
            if kind == 'memory_sample':
                inserted = c.execute('INSERT OR IGNORE INTO memory_samples VALUES (?,?,?)',
                                     (identity, now, encoded)).rowcount
            else:
                inserted = c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                                     (identity, now, kind[:80], encoded)).rowcount
            if inserted and kind == 'nvrm_oom':
                counter_key = 'nvrm-' + detail['boot']
                row = c.execute('SELECT value FROM meta WHERE key=?', (counter_key,)).fetchone()
                stats = json.loads(row[0]) if row else {'lines': 0, 'groups': 0, 'last': 0}
                stats['lines'] += 1
                stats['groups'] += int(now - stats['last'] > 5)
                stats['last'] = now
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (counter_key, json.dumps(stats)))
            c.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY at DESC LIMIT -1 OFFSET ?)', (_LIMIT,))
            c.execute('DELETE FROM events WHERE at < ?', (time.time() - 7 * 86400,))
            c.execute('DELETE FROM memory_samples WHERE id IN (SELECT id FROM memory_samples ORDER BY at DESC LIMIT -1 OFFSET ?)', (_SAMPLE_LIMIT,))
            c.execute('DELETE FROM memory_samples WHERE at < ?', (time.time() - 7 * 86400,))
    except (OSError, sqlite3.Error):
        pass


def required(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def admission(sample=None):
    if not POLICY.exists() and not required('SPARK_REQUIRE_MEMORY_POLICY'):
        return {'ok': True, 'reason': None, 'state': 'not_configured', 'at': time.time()}
    try:
        policy = json.loads(POLICY.read_text())
        floor = policy['min_available_bytes']
        if not isinstance(floor, int) or floor <= 0:
            raise ValueError('invalid reserve')
        s = sample if sample is not None else memory_sample()
        if s['total'] != policy['mem_total_bytes']:
            raise ValueError('memory capacity changed; recalibrate')
        low = s['available_bytes'] < floor
        # PSI threshold is an explicit policy value, not a measured hardware limit.
        pressure = s['psi']['full']['avg10'] >= policy['max_full_psi_avg10']
        reason = 'memory_low' if low else 'memory_pressure' if pressure else None
        return {'ok': reason is None, 'reason': reason, 'available_bytes': s['available_bytes'],
                'reserve_bytes': floor, 'full_psi_avg10': s['psi']['full']['avg10'], 'at': s['at']}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ok': False, 'reason': 'memory_unknown', 'at': time.time()}


def kernel_events():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    with connect() as c:
        row = c.execute('SELECT value FROM meta WHERE key=?', ('cursor-'+boot,)).fetchone()
    cmd = ['journalctl', '-k', '-b', '--no-pager', '-o', 'json', '-n', '1000',
           '--grep', 'NV_ERR_NO_MEMORY|Out of memory: Killed process|oom-kill']
    if row:
        cmd += ['--after-cursor', row[0]]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=3)
    # journalctl --grep returns 1 when no records match the cursor window.
    if r.returncode and not (r.returncode == 1 and not r.stdout.strip() and not r.stderr.strip()):
        raise OSError('kernel journal unavailable')
    cursor = None
    records = sorted((json.loads(line) for line in r.stdout.splitlines()), key=lambda rec: int(rec['__MONOTONIC_TIMESTAMP']))
    for rec in records:
        message = rec.get('MESSAGE', '')
        cursor = rec['__CURSOR']
        when = int(rec['__REALTIME_TIMESTAMP']) / 1e6
        kind = 'nvrm_oom' if 'NV_ERR_NO_MEMORY' in message else 'kernel_oom'
        event(kind, {'message': message[:1500], 'boot': boot}, key=cursor, at=when)
    if cursor:
        with connect() as c:
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('cursor-'+boot, cursor))


def snapshot():
    with _lock:
        result = dict(_latest)
    result['admission'] = admission()
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        with connect() as c:
            rows = c.execute('SELECT at,kind,detail FROM events ORDER BY at DESC LIMIT 40').fetchall()
            row = c.execute('SELECT value FROM meta WHERE key=?', ('nvrm-'+boot,)).fetchone()
        stats = json.loads(row[0]) if row else {'lines': 0, 'groups': 0}
        result.update(events=[{'at': at, 'kind': kind, 'detail': json.loads(d)} for at, kind, d in rows],
                      nvrm_lines=stats['lines'], nvrm_groups=stats['groups'],
                      retention_days=7, max_events=_LIMIT)
    except (OSError, ValueError, sqlite3.Error):
        result.update(events=[], nvrm_lines=None, nvrm_groups=None, history_error=True)
    return result


def monitor():
    global _latest
    previous = None
    last_reason = object()
    count = 0
    services = {}
    model_state = None
    while True:
        try:
            s = memory_sample()
            dt = s['at'] - previous['at'] if previous else 0
            s['swap_in_bps'] = max(0, (s['pswpin'] - previous['pswpin']) * s['page_size'] / dt) if dt > 0 else None
            s['swap_out_bps'] = max(0, (s['pswpout'] - previous['pswpout']) * s['page_size'] / dt) if dt > 0 else None
            previous = s
            state = admission(s)
            if state['reason'] != last_reason:
                event('memory_state', state)
                last_reason = state['reason']
            if count % 6 == 0:
                event('memory_sample', {k: s[k] for k in ('available_bytes', 'swap_used', 'swap_in_bps', 'swap_out_bps', 'psi')})
            try:
                kernel_events()
                journal_error = None
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, sqlite3.Error) as exc:
                journal_error = type(exc).__name__
            if count % 6 == 0:
                for unit, user in [('earlyoom.service', False), ('strata-server.service', True), ('spark-ollama-guard.service', True)]:
                    try:
                        cmd = ['systemctl'] + (['--user'] if user else []) + ['show', unit, '-p', 'ActiveState', '-p', 'Result', '--value']
                        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                        value = r.stdout.strip() if r.returncode == 0 else 'unknown'
                    except (OSError, subprocess.TimeoutExpired):
                        value = 'unknown'
                    if services.get(unit) != value:
                        services[unit] = value
                        event('service_state', {'model': unit, 'state': value})
            try:
                with urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=1) as response:
                    models = sorted(str(m.get('name', '')) for m in json.load(response)['models'])
            except (OSError, ValueError, KeyError, TypeError):
                models = None
            if models != model_state:
                event('model_state', {'model': ', '.join(models)[:300] if models else '',
                                      'engine': 'Ollama', 'known': models is not None, 'loaded': models})
                model_state = models
            with _lock:
                _latest = {'available': True, 'sample': s, 'journal_error': journal_error, 'services': dict(services)}
            count += 1
        except (OSError, ValueError, KeyError) as exc:
            with _lock:
                _latest = {'available': False, 'error': type(exc).__name__}
        time.sleep(5)


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    event('monitor_start', {'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
    threading.Thread(target=monitor, daemon=True, name='stability-monitor').start()
