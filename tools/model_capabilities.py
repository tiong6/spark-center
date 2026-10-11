"""Optional model protection discovery; no service starts during probing."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

from backend import stability
from tools import llm_switch

ROOT = Path(__file__).resolve().parents[1]
OLLAMA = os.environ.get('SPARK_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
GUARD = os.environ.get('SPARK_GUARD_URL', 'http://127.0.0.1:11436').rstrip('/')
STRATA = os.environ.get('SPARK_STRATA_URL', 'http://127.0.0.1:8080').rstrip('/')
_lock = threading.RLock()
_cached = None
_when = 0
_last_route = None
_drain_seen = False


def probe(url):
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def route(force=False):
    """A policy decision before dispatch, never a retry after a failed request."""
    global _last_route
    g = probe(GUARD + '/guard/status')
    available = isinstance(g, dict) and g.get('ok') is True and isinstance(g.get('blocked'), bool)
    strict = stability.required('SPARK_REQUIRE_GUARD')
    if available:
        result = {'guard': {**g, 'protected': True}, 'endpoint': GUARD}
    elif strict:
        result = {'guard': {'ok': False, 'blocked': True, 'reason': 'guard_unavailable', 'protected': False}, 'endpoint': GUARD}
    else:
        result = {'guard': {'ok': True, 'blocked': False, 'reason': 'guard_not_configured', 'protected': False}, 'endpoint': OLLAMA}
    state = 'guard' if available else 'required_unavailable' if strict else 'direct_unprotected'
    with _lock:
        if state != _last_route:
            stability.event('protection_route', {'state': state})
            _last_route = state
    return result


def snapshot(force=False):
    global _cached, _when, _drain_seen
    with _lock:
        if not force and _cached is not None and time.monotonic() - _when < 5:
            return _cached
        r = route()
        control = llm_switch.control_info()
        d = probe(STRATA + '/admin/drain')
        if isinstance(d, dict) and isinstance(d.get('draining'), bool) and isinstance(d.get('active_requests'), int):
            _drain_seen = True
        elif control['service_state'] not in ('inactive', 'failed'):
            _drain_seen = False
        # Retain a successful probe while this controller intentionally stops Strata.
        # After controller restart, an unknown stopped engine needs a manual start/probe.
        drain = _drain_seen and control['available']
        admission = stability.admission()
        try:
            unit = subprocess.run(['systemctl', 'show', 'earlyoom.service', '-p', 'ActiveState', '--value'],
                                  text=True, capture_output=True, timeout=2)
            oom = unit.stdout.strip() if unit.returncode == 0 else 'unknown'
        except (OSError, subprocess.TimeoutExpired):
            oom = 'unknown'
        _cached = {'guard_available': r['guard']['protected'],
                   'guard_required': stability.required('SPARK_REQUIRE_GUARD'),
                   'memory_required': stability.required('SPARK_REQUIRE_MEMORY_POLICY'),
                   'memory_policy': {**admission, 'state': admission.get('state', 'unavailable' if admission.get('reason') == 'memory_unknown' else 'configured')},
                   'strata_service': control, 'drain': drain, 'earlyoom': oom,
                   'switch_control': {**control, 'available': bool(drain and r['guard']['protected'])}}
        _when = time.monotonic()
        return _cached


def install_guard(enable):
    """User service: no root or sudoers grants needed."""
    global _cached
    if not enable and stability.required('SPARK_REQUIRE_GUARD'):
        raise RuntimeError('Strict guard policy is enabled; change SPARK_REQUIRE_GUARD explicitly before disabling.')
    if enable:
        subprocess.run(['sh', str(ROOT / 'install.sh'), '--guard'], check=True, capture_output=True, text=True, timeout=30)
    else:
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'spark-ollama-guard.service'],
                       check=True, capture_output=True, text=True, timeout=20)
    _cached = None
