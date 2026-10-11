"""Disk inventory and background scan state. Actions stay in the server job layer."""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from .messages import LANG_DEFAULT, msg
from .system import _run, _http_json

HOME = os.path.expanduser("~")
_DISK = {"lock": threading.Lock(), "data": None, "ts": 0, "scanning": False}
DISK_TTL = 600


def _du(path):
    try:
        r = subprocess.run(["du", "-sxb", path], capture_output=True, text=True, timeout=120)
        return int(r.stdout.split()[0]) if r.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _du_children(path, limit=12):
    """path 底下第一層各項大小，由大到小。"""
    out = []
    try:
        names = os.listdir(path)
    except OSError:
        return out
    for n in names:
        full = os.path.join(path, n)
        if os.path.islink(full):
            continue
        v = _du(full)
        if v:
            out.append({"name": n, "path": full, "bytes": v})
    out.sort(key=lambda x: -x["bytes"])
    return out[:limit]


def _big_files(root, min_bytes=1 << 30, limit=25):
    try:
        r = subprocess.run(["find", root, "-xdev", "-type", "f", "-size", f"+{min_bytes // 1024}k", "-printf", "%s\t%p\n"],
                           capture_output=True, text=True, timeout=180)
        rows = [l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l]
        rows = [{"bytes": int(a), "path": b} for a, b in rows]
        rows.sort(key=lambda x: -x["bytes"])
        return rows[:limit]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []


def _docker_df():
    out = _run(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
    if not out:
        return None
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    vols = []
    v = _run(["docker", "volume", "ls", "-q"], timeout=30) or ""
    dfv = _run(["docker", "system", "df", "-v", "--format", "{{json .}}"], timeout=60)
    if dfv:
        try:
            j = json.loads(dfv.splitlines()[0]) if dfv.strip().startswith("{") else None
            for vol in (j or {}).get("Volumes", []) or []:
                vols.append({"name": vol.get("Name"), "size": vol.get("Size"), "links": vol.get("Links")})
        except (ValueError, IndexError, AttributeError):
            pass
    if not vols and dfv:
        # 舊版 docker 沒有 json，退回文字表
        txt = _run(["docker", "system", "df", "-v"], timeout=60) or ""
        sec = txt.split("VOLUME NAME", 1)
        if len(sec) > 1:
            for l in sec[1].splitlines()[1:]:
                parts = l.split()
                if len(parts) >= 3 and parts[0] != "":
                    vols.append({"name": parts[0], "links": parts[1], "size": parts[2]})
                elif not l.strip():
                    break
    return {"summary": rows, "volumes": vols}


def _ollama_models():
    j = _http_json("http://127.0.0.1:11434/api/tags")
    if not j:
        return None
    ms = [{"name": m.get("name"), "bytes": m.get("size") or 0, "modified": (m.get("modified_at") or "")[:10]} for m in j.get("models", [])]
    ms.sort(key=lambda x: -x["bytes"])
    return ms


def apt_autoremovable():
    out = _run(["apt-get", "-s", "autoremove"], timeout=60) or ""
    return [l.split()[1] for l in out.splitlines() if l.startswith("Remv ")]


def disk_scan():
    with _DISK["lock"]:
        if _DISK["scanning"]:
            return
        _DISK["scanning"] = True
    try:
        st = os.statvfs("/")
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        cats = []
        def cat(key, label, b, note="", action=None, items=None):
            cats.append({"key": key, "label": label, "bytes": b or 0, "note": note, "action": action, "items": items or []})
        # Ollama（系統服務，模型在 /usr/share/ollama）
        om = _ollama_models()
        cat("ollama", msg('ollama_models', LANG_DEFAULT), _du("/usr/share/ollama"),
            msg('ollama_disk_note', LANG_DEFAULT), None, om)
        # LM Studio
        lm = os.path.join(HOME, ".lmstudio", "models")
        if os.path.isdir(lm):
            items = []
            for pub in _du_children(lm, 30):
                for mdl in _du_children(pub["path"], 30):
                    items.append({"name": f"{pub['name']}/{mdl['name']}", "bytes": mdl["bytes"], "path": mdl["path"]})
            items.sort(key=lambda x: -x["bytes"])
            cat("lmstudio", msg('lmstudio_models', LANG_DEFAULT), _du(lm), msg('lmstudio_disk_note', LANG_DEFAULT), None, items[:30])
        # Docker
        dk = _docker_df()
        if dk:
            def gb(x):
                m = re.match(r"([\d.]+)\s*([KMGT]?B)", str(x or ""))
                if not m: return 0
                return float(m.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
            total_dk = sum(gb(r.get("Size")) for r in dk["summary"])
            recl = sum(gb(r.get("Reclaimable", "").split(" ")[0]) for r in dk["summary"])
            items = [{"name": msg('docker_count', LANG_DEFAULT, p0=r.get('Type'), p1=r.get('TotalCount') or r.get('Total'), p2=r.get('Reclaimable')), "bytes": gb(r.get("Size"))} for r in dk["summary"]]
            items += [{"name": msg('volume', LANG_DEFAULT, p0=v['name']), "bytes": gb(v.get("size")), "note": msg('volume_links', LANG_DEFAULT, p0=v.get('links'))} for v in dk["volumes"]]
            cat("docker", "Docker", total_dk, msg('docker_reclaim', LANG_DEFAULT, p0=format(recl/1e9, '.1f')), "docker_prune", items)
        # snap 使用者資料（Steam 等）
        sn = os.path.join(HOME, "snap")
        if os.path.isdir(sn):
            cat("snap_home", msg('snap_data', LANG_DEFAULT), _du(sn), msg('snap_data_note', LANG_DEFAULT), None, _du_children(sn, 10))
        cat("flatpak", msg('flatpak_system', LANG_DEFAULT), _du("/var/lib/flatpak"), msg('flatpak_note', LANG_DEFAULT), None)
        cat("snapd", msg('snap_system', LANG_DEFAULT), _du("/var/lib/snapd"), msg('snap_retention', LANG_DEFAULT), None)
        # 只量 archives：apt clean 清的就是這裡；同層的 pkgcache.bin／srcpkgcache.bin 是套件索引，清不掉也不該清
        cat("apt_cache", msg('apt_cache', LANG_DEFAULT), _du("/var/cache/apt/archives"), msg('apt_cache_note', LANG_DEFAULT), "apt_clean")
        ar = apt_autoremovable()
        cat("apt_autoremove", msg('apt_autoremove', LANG_DEFAULT), None, msg('autoremove_count', LANG_DEFAULT, p0=len(ar)) if ar else msg('none', LANG_DEFAULT), "apt_autoremove" if ar else None, [{"name": n, "bytes": 0} for n in ar])
        cat("journal", msg('journal', LANG_DEFAULT), _du("/var/log/journal"), msg('journal_note', LANG_DEFAULT), None)
        cat("cache", "~/.cache", _du(os.path.join(HOME, ".cache")), msg('app_cache', LANG_DEFAULT), None, _du_children(os.path.join(HOME, ".cache"), 8))
        cat("npm", "~/.npm", _du(os.path.join(HOME, ".npm")), msg('npm_cache', LANG_DEFAULT), "npm_cache_clean")
        tr = os.path.join(HOME, ".local", "share", "Trash")
        cat("trash", msg('trash', LANG_DEFAULT), _du(tr), "", "trash_empty")
        home_top = _du_children(HOME, 15)
        big = _big_files(HOME)
        data = {"total": total, "free": free, "used": total - free, "categories": cats, "home_top": home_top, "big_files": big,
                "scanned": datetime.now().isoformat(timespec="seconds")}
        with _DISK["lock"]:
            _DISK.update(data=data, ts=time.time())
    finally:
        with _DISK["lock"]:
            _DISK["scanning"] = False


def disk_status(force=False):
    with _DISK["lock"]:
        stale = not _DISK["data"] or time.time() - _DISK["ts"] > DISK_TTL
        scanning = _DISK["scanning"]
        data = _DISK["data"]
    if (stale or force) and not scanning:
        threading.Thread(target=disk_scan, daemon=True).start()
        scanning = True
    st = os.statvfs("/")
    live = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    return {"scanning": scanning, "data": data, "live": live}



def invalidate():
    """Invalidate inventory after a completed cleanup; keep lock ownership here."""
    with _DISK["lock"]:
        _DISK["ts"] = 0
