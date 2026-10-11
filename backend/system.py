"""Shared process/HTTP helpers and application paths; no server dependency."""
import os
import json
import subprocess
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READONLY = os.environ.get("SPARK_CENTER_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
NO_SWEEP = os.environ.get("SPARK_CENTER_NO_SWEEP", "").strip().lower() in ("1", "true", "yes", "on")
_ENV_C = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")


def _run(cmd, timeout=20):
    """跑外部指令，失敗或逾時回 None（呼叫端要把「拿不到」當一級狀態處理）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV_C)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _http_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def process_label(name):
    """Linux comm lets the OOM daemon distinguish the manager from model engines."""
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(15, name.encode()[:15], 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'PR_SET_NAME failed')
