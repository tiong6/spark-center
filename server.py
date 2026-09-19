#!/usr/bin/env python3
"""Spark Center — 可勾選項目的 apt 更新頁，只綁 127.0.0.1。

資料來源與誠實原則：
- 清單：python-apt 讀本機 apt cache（和 `apt list --upgradable` 同一份）。
- 影響範圍：安裝前一律用 python-apt 在記憶體模擬，把「實際會動到的套件」列給使用者。
- 安裝：走 aptdaemon 的 D-Bus 介面（和 DGX Dashboard 同一個後端），授權由 polkit 桌面視窗處理。
- 重開機：只讀 /var/run/reboot-required，有才提示，程式本身絕不重開。
"""
import concurrent.futures
import glob
import gzip
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import apt
import aptdaemon.client
import aptdaemon.enums as aenums
from gi.repository import GLib

HOST = "127.0.0.1"
PORT = int(os.environ.get("SPARK_UPDATER_PORT", "11001"))
HERE = os.path.dirname(os.path.abspath(__file__))
REBOOT_FLAG = "/var/run/reboot-required"
REBOOT_PKGS = "/var/run/reboot-required.pkgs"
APT_HISTORY = "/var/log/apt/history.log"
APT_LISTS = "/var/lib/apt/lists"

# 名稱樣式 → 「通常需要重開」的提示。這是啟發式，前端會標明「推測」。
REBOOT_HINT_RE = re.compile(
    r"^(linux-image|linux-modules|linux-headers|nvidia-driver|nvidia-kernel|"
    r"libnvidia-|nvidia-utils|libc6|dgx-|systemd$|dbus$|udev$)"
)


# ---------- apt 清單與模擬 ----------

def _origin_of(version):
    if not version or not version.origins:
        return {"origin": "", "site": "", "archive": "", "label": ""}
    o = version.origins[0]
    return {
        "origin": o.origin or "",
        "site": o.site or "",
        "archive": o.archive or "",
        "label": o.label or "",
    }


def _group_name(orig):
    """把來源整理成人看得懂的分組名。優先用 Origin 欄位，沒有就用網址。"""
    name = orig["origin"] or orig["label"] or orig["site"] or "未知來源"
    site = orig["site"]
    if site.endswith("nvidia.com"):
        # NVIDIA 有多個 repo（spark / baseos / cuda），保留路徑辨識
        return f"NVIDIA ({site})"
    return name


def list_updates():
    cache = apt.Cache()
    items = []
    for pkg in cache:
        if not pkg.is_upgradable:
            continue
        cand, inst = pkg.candidate, pkg.installed
        orig = _origin_of(cand)
        items.append({
            "name": pkg.name,
            "installed": inst.version if inst else "",
            "candidate": cand.version,
            "group": _group_name(orig),
            "site": orig["site"],
            "archive": orig["archive"],
            "size": cand.size,
            "summary": cand.summary or "",
            "security": "security" in (orig["archive"] or "").lower(),
            "reboot_hint": bool(REBOOT_HINT_RE.match(pkg.name)),
        })
    items.sort(key=lambda x: (x["group"], x["name"]))
    return items


def simulate(names):
    """在記憶體裡標記升級，回傳實際會被動到的套件。不動系統。"""
    cache = apt.Cache()
    unknown = [n for n in names if n not in cache]
    if unknown:
        return {"ok": False, "error": f"找不到套件：{', '.join(unknown)}"}
    try:
        with cache.actiongroup():
            for n in names:
                cache[n].mark_upgrade()
    except Exception as e:  # 相依性解不開
        return {"ok": False, "error": f"相依性無法解析：{e}"}
    changes = []
    for p in cache.get_changes():
        if p.marked_delete:
            action = "remove"
        elif p.marked_install:
            action = "install"
        elif p.marked_upgrade:
            action = "upgrade"
        elif p.marked_downgrade:
            action = "downgrade"
        else:
            action = "other"
        changes.append({
            "name": p.name,
            "action": action,
            "from": p.installed.version if p.installed else "",
            "to": p.candidate.version if p.candidate else "",
            "requested": p.name in names,
        })
    changes.sort(key=lambda c: (not c["requested"], c["action"], c["name"]))
    return {
        "ok": True,
        "changes": changes,
        "download_bytes": cache.required_download,
        "space_bytes": cache.required_space,
        "broken": cache.broken_count,
    }


def reboot_status():
    if not os.path.exists(REBOOT_FLAG):
        return {"required": False, "packages": []}
    pkgs = []
    try:
        with open(REBOOT_PKGS) as f:
            pkgs = [l.strip() for l in f if l.strip()]
    except OSError:
        pass
    return {"required": True, "packages": pkgs}


def last_refresh():
    """apt 索引最後更新時間。優先讀 apt 自己寫的 update-success-stamp（每次 apt update 成功都會碰），
    沒有就退回 lists 目錄最新檔案的 mtime（內容沒變時 apt 不會碰檔，可能偏舊）。拿不到回 None。"""
    stamp = "/var/lib/apt/periodic/update-success-stamp"
    if os.path.exists(stamp):
        return datetime.fromtimestamp(os.path.getmtime(stamp)).isoformat(timespec="seconds")
    try:
        mt = max(
            os.path.getmtime(os.path.join(APT_LISTS, f))
            for f in os.listdir(APT_LISTS)
            if not f.startswith(("lock", "partial", "auxfiles"))
        )
        return datetime.fromtimestamp(mt).isoformat(timespec="seconds")
    except (OSError, ValueError):
        return None


def apt_history(limit=8):
    """讀 apt 歷史最近幾筆，只回 Start-Date / Commandline / Upgrade 摘要。"""
    entries, cur = [], {}
    try:
        with open(APT_HISTORY, errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    if cur:
                        entries.append(cur)
                        cur = {}
                    continue
                k, _, v = line.partition(": ")
                cur[k] = v
        if cur:
            entries.append(cur)
    except OSError:
        return []
    out = []
    for e in entries[-limit:][::-1]:
        pk = []
        for key in ("Upgrade", "Install", "Remove", "Purge"):
            if key in e:
                names = re.findall(r"([^\s,()]+):[a-z0-9]+ \(", e[key])
                pk.append({"action": key.lower(), "count": len(names), "names": names})
        out.append({
            "start": e.get("Start-Date", ""),
            "commandline": e.get("Commandline", ""),
            "actions": pk,
        })
    return out


# ---------- aptdaemon 工作 ----------

class Job:
    """同一時間只允許一個 aptdaemon 交易。狀態給前端輪詢。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.state = {
            "kind": None,          # "install" | "refresh" | "flatpak" | "snap"
            "status": "idle",      # idle | running | done | error
            "packages": [],
            "progress": 0,
            "status_text": "",
            "details": "",
            "exit": None,
            "error": None,
            "log": [],
            "started": None,
            "finished": None,
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _log(self, msg):
        with self.lock:
            self.state["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
            self.state["log"] = self.state["log"][-200:]

    def start(self, kind, packages=None):
        with self.lock:
            if self.state["status"] == "running":
                return False
            self.reset()
            self.state.update({
                "kind": kind,
                "status": "running",
                "packages": packages or [],
                "started": datetime.now().isoformat(timespec="seconds"),
            })
        threading.Thread(target=self._run, args=(kind, packages or []), daemon=True).start()
        return True

    def _run_subprocess(self, cmd, packages):
        """flatpak / snap 更新：直接跑 CLI，逐行把輸出寫進 log。授權由 polkit 處理
        （flatpak app-update 對 active session 免密碼；snap refresh 會跳密碼視窗）。"""
        self._log("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_ENV_C, bufsize=1)
            with self.lock:
                self.state["progress"] = None
                self.state["status_text"] = "執行中"
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.strip():
                    self._log(line)
                    with self.lock:
                        self.state["details"] = line.strip()[:120]
            rc = proc.wait(timeout=1800)
            with self.lock:
                self.state["exit"] = f"rc={rc}"
                self.state["status"] = "done" if rc == 0 else "error"
                if rc != 0:
                    self.state["error"] = f"指令結束碼 {rc}，見詳細記錄"
                self.state["status_text"] = "已完成" if rc == 0 else "失敗"
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(e)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
            self._log("EXCEPTION " + str(e))
        finally:
            _APPS_CACHE["ts"] = 0  # 讓應用程式清單重新掃描

    def _run(self, kind, packages):
        if kind == "shell":
            # packages[0] 是指令名（見 DISK_ACTIONS 白名單），不接受任意指令
            cmd = DISK_ACTIONS.get(packages[0]) if packages else None
            if not cmd:
                with self.lock:
                    self.state.update(status="error", error="未知動作", finished=datetime.now().isoformat(timespec="seconds"))
                return
            return self._run_subprocess(cmd, packages)
        if kind == "flatpak":
            return self._run_subprocess(["flatpak", "update", "-y", "--noninteractive"] + packages, packages)
        if kind == "snap":
            return self._run_subprocess(["snap", "refresh"] + packages, packages)
        loop = GLib.MainLoop()
        try:
            client = aptdaemon.client.AptClient()
            if kind == "refresh":
                trans = client.update_cache()
            elif kind == "aptclean":
                trans = client.clean()                 # polkit org.debian.apt.clean：active session 免密碼
            elif kind == "remove":
                trans = client.remove_packages(packages)  # autoremove 用；會跳 polkit 密碼視窗
            else:
                trans = client.upgrade_packages(packages)

            def on_status(t, status):
                text = aenums.get_status_string_from_enum(status)
                with self.lock:
                    self.state["status_text"] = text
                self._log(text)

            def on_details(t, d):
                with self.lock:
                    self.state["details"] = d

            def on_progress(t, p):
                # aptdaemon 用 101 代表「進度未知」，不要當成百分比顯示
                with self.lock:
                    self.state["progress"] = int(p) if p <= 100 else None

            def on_error(t, code, details):
                msg = f"{aenums.get_error_string_from_enum(code)}: {details}"
                with self.lock:
                    self.state["error"] = msg
                self._log("ERROR " + msg)

            def on_conffile(t, old, new):
                # 設定檔衝突一律保留現有版本，不互動、不猜。
                self._log(f"設定檔衝突 {old}，保留現有版本")
                t.resolve_config_file_conflict(old, "keep")

            def on_finished(t, exit_state):
                with self.lock:
                    self.state["exit"] = exit_state
                    self.state["status"] = (
                        "done" if exit_state == aenums.EXIT_SUCCESS else "error"
                    )
                    if self.state["status"] == "error" and not self.state["error"]:
                        self.state["error"] = f"交易結束狀態：{exit_state}"
                    self.state["finished"] = datetime.now().isoformat(timespec="seconds")
                self._log(f"finished: {exit_state}")
                loop.quit()

            trans.connect("status-changed", on_status)
            trans.connect("status-details-changed", on_details)
            trans.connect("progress-changed", on_progress)
            trans.connect("error", on_error)
            trans.connect("config-file-conflict", on_conffile)
            trans.connect("finished", on_finished)
            self._log(f"開始 {kind}: {' '.join(packages) if packages else ''}")
            trans.run()
            loop.run()
        except Exception as e:  # D-Bus 拒絕、polkit 取消等
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(e)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
            self._log("EXCEPTION " + str(e))


JOB = Job()


# ---------- 應用程式清單（apt / snap / flatpak）----------

DESKTOP_DIRS_APT = ("/usr/share/applications",)
SNAP_DESKTOP_DIR = "/var/lib/snapd/desktop/applications"
FLATPAK_APPSTREAM_GLOB = (
    "/var/lib/flatpak/appstream/*/*/active/appstream.xml.gz",
    os.path.expanduser("~/.local/share/flatpak/appstream/*/*/active/appstream.xml.gz"),
)
_ENV_C = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")


def _run(cmd, timeout=20):
    """跑外部指令，失敗或逾時回 None（呼叫端要把「拿不到」當一級狀態處理）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV_C)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_desktop(path):
    """只讀 [Desktop Entry] 主段落。回 None 表示不是要顯示的應用程式。"""
    d = {"name": "", "name_zh": "", "comment": "", "icon": "", "categories": ""}
    nodisplay = hidden = False
    typ = "Application"
    try:
        with open(path, errors="replace") as f:
            in_main = False
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    if in_main:
                        break
                    in_main = line == "[Desktop Entry]"
                    continue
                if not in_main or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k == "Name":
                    d["name"] = v
                elif k in ("Name[zh_TW]", "Name[zh_Hant]"):
                    d["name_zh"] = v
                elif k == "Comment":
                    d["comment"] = v
                elif k == "Icon":
                    d["icon"] = v
                elif k == "Categories":
                    d["categories"] = v
                elif k == "NoDisplay":
                    nodisplay = v.lower() == "true"
                elif k == "Hidden":
                    hidden = v.lower() == "true"
                elif k == "Type":
                    typ = v
    except OSError:
        return None
    if nodisplay or hidden or typ != "Application" or not d["name"]:
        return None
    return d


def _apt_desktop_owner_map():
    """dpkg 檔案清單 → {套件名: [desktop 路徑]}。純本機檔案，約 50ms。"""
    m = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5].split(":")[0]
        try:
            with open(lst, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(DESKTOP_DIRS_APT) and line.endswith(".desktop"):
                        m.setdefault(pkg, []).append(line)
        except OSError:
            continue
    return m


def apps_apt(cache):
    out = []
    for pkg, paths in _apt_desktop_owner_map().items():
        if pkg not in cache or not cache[pkg].installed:
            continue
        entry = None
        for p in sorted(paths):
            entry = parse_desktop(p)
            if entry:
                break
        if not entry:
            continue
        ap = cache[pkg]
        out.append({
            "source": "apt", "id": pkg,
            "name": entry["name"], "name_zh": entry["name_zh"], "comment": entry["comment"],
            "icon": entry["icon"], "categories": entry["categories"],
            "version": ap.installed.version,
            "candidate": ap.candidate.version if ap.is_upgradable else None,
            "origin": _group_name(_origin_of(ap.candidate or ap.installed)),
            "changelog": "apt",
        })
    return out


def apps_snap(check_updates):
    txt = _run(["snap", "list"])
    if txt is None:
        return None
    names = {}
    for p in glob.glob(os.path.join(SNAP_DESKTOP_DIR, "*.desktop")):
        snap = os.path.basename(p).split("_", 1)[0]
        e = parse_desktop(p)
        if e and snap not in names:
            names[snap] = e
    updates = {}
    if check_updates:
        u = _run(["snap", "refresh", "--list"], timeout=30)
        if u is not None and not u.startswith("All snaps"):
            for line in u.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    updates[parts[0]] = parts[1]
    out = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        name, ver, rev, tracking, publisher = parts[:5]
        notes = parts[5] if len(parts) > 5 else ""
        if "base" in notes or name.startswith("core") or name in ("bare", "snapd"):
            continue
        if "disabled" in notes:
            continue
        e = names.get(name, {})
        out.append({
            "source": "snap", "id": name,
            "name": e.get("name") or name, "name_zh": e.get("name_zh", ""), "comment": e.get("comment", ""),
            "icon": e.get("icon", ""), "categories": e.get("categories", ""),
            "version": ver, "candidate": updates.get(name),
            "origin": f"snap · {publisher.rstrip('*')} · {tracking}",
            "changelog": "snap",
            "checked_updates": check_updates and u is not None,
        })
    return out


def apps_flatpak(check_updates):
    txt = _run(["flatpak", "list", "--app", "--columns=application,name,version,origin"])
    if txt is None:
        return None
    updates = {}
    checked = False
    if check_updates:
        u = _run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version,commit"], timeout=30)
        if u is not None:
            checked = True
            for line in u.splitlines():
                parts = line.split("\t")
                if parts and parts[0]:
                    ver = parts[1] if len(parts) > 1 and parts[1] else ""
                    commit = parts[2][:12] if len(parts) > 2 else ""
                    # flathub 常不帶版本號，只有 commit；照實標示，不編版本
                    updates[parts[0]] = ver or (f"新 commit {commit}" if commit else "有新版（版本未提供）")
    out = []
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        app, name, ver, origin = parts[:4]
        out.append({
            "source": "flatpak", "id": app,
            "name": name or app, "name_zh": "", "comment": "", "icon": "", "categories": "",
            "version": ver, "candidate": updates.get(app),
            "origin": f"flatpak · {origin}",
            "remote": origin,
            "changelog": "flatpak",
            "checked_updates": checked,
        })
    return out


_APPS_CACHE = {"ts": 0, "data": None, "lock": threading.Lock()}
APPS_TTL = 300


def list_apps(force=False):
    with _APPS_CACHE["lock"]:
        if not force and _APPS_CACHE["data"] and time.time() - _APPS_CACHE["ts"] < APPS_TTL:
            return _APPS_CACHE["data"]
        cache = apt.Cache()
        apt_apps = apps_apt(cache)
        snap_apps = apps_snap(check_updates=True)
        fp_apps = apps_flatpak(check_updates=True)
        items = apt_apps + (snap_apps or []) + (fp_apps or [])
        items.sort(key=lambda a: (a["name_zh"] or a["name"]).lower())
        data = {
            "items": items,
            "sources": {
                "apt": {"ok": True, "count": len(apt_apps)},
                "snap": {"ok": snap_apps is not None, "count": len(snap_apps or [])},
                "flatpak": {"ok": fp_apps is not None, "count": len(fp_apps or [])},
            },
            "generated": datetime.now().isoformat(timespec="seconds"),
        }
        _APPS_CACHE.update(ts=time.time(), data=data)
        return data


# ---------- 更新說明 ----------

_CHANGELOG_CACHE = {}


def changelog_apt(pkg, installed_version=""):
    """用 `apt-get changelog`（不需 root）抓候選版本的 changelog，截到已安裝版本為止。
    第三方 repo 多半沒提供，照實回報。python-apt 的 get_changelog 在這台機器上抓不到，所以走子程序。"""
    try:
        r = subprocess.run(["apt-get", "-q", "changelog", pkg], capture_output=True, text=True, timeout=25, env=_ENV_C)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "note": "下載 changelog 逾時（25 秒）"}
    except OSError as e:
        return {"ok": False, "text": "", "note": f"無法執行 apt-get：{e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return {"ok": False, "text": "", "note": "此來源未提供更新說明（多為第三方 repo）" + (f"：{err[-1]}" if err else "")}
    head_re = re.compile(r"^(\S+) \(([^)]+)\)")
    kept, seen_any, truncated = [], False, False
    for line in r.stdout.splitlines():
        if line.startswith(("Get:", "Fetched", "Hit:")):
            continue
        m = head_re.match(line)
        if m:
            seen_any = True
            if installed_version and m.group(2) == installed_version:
                truncated = True
                break
        kept.append(line)
    if not seen_any:
        return {"ok": False, "text": "", "note": "此來源未提供更新說明（多為第三方 repo）"}
    text = "\n".join(kept).strip()
    if truncated and not text:
        return {"ok": True, "text": "", "note": "已安裝版本就是最新條目，沒有更新的 changelog"}
    if len(text) > 20000:
        text = text[:20000] + "\n…（已截斷）"
    note = "來源：apt changelog" + ("，已安裝版本之後的條目" if truncated else "，全部條目（找不到已安裝版本的分界）")
    return {"ok": True, "text": text, "note": note}


def changelog_flatpak(app_id, installed_version):
    """從本機 appstream 目錄讀 <releases>，回最新幾筆到已安裝版本為止。"""
    for pattern in FLATPAK_APPSTREAM_GLOB:
        for path in glob.glob(pattern):
            try:
                with gzip.open(path) as f:
                    root = ET.parse(f).getroot()
            except (OSError, ET.ParseError):
                continue
            for comp in root.iter("component"):
                if (comp.findtext("id") or "").removesuffix(".desktop") != app_id:
                    continue
                rels = comp.find("releases")
                if rels is None:
                    return {"ok": False, "text": "", "note": "appstream 裡沒有 release 記錄"}
                lines = []
                for r in list(rels)[:8]:
                    v = r.get("version", "?")
                    d = r.get("date", "")[:10]
                    desc = " ".join(t.strip() for t in r.itertext() if t.strip())
                    lines.append(f"{v}  {d}\n  {desc or '（無說明）'}")
                    if v == installed_version:
                        break
                return {"ok": True, "text": "\n\n".join(lines), "note": f"來源：{os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))} appstream"}
    # 本機沒有 appstream（flatpak 未同步 appstream 時就是這樣）→ 退回 remote-info 的提交訊息
    remote = ""
    data = _APPS_CACHE.get("data") or {}
    for a in data.get("items", []):
        if a.get("source") == "flatpak" and a.get("id") == app_id:
            remote = a.get("remote", "")
    if remote:
        txt = _run(["flatpak", "remote-info", remote, app_id], timeout=25)
        if txt:
            info = {}
            for line in txt.splitlines():
                k, _, v = line.strip().partition(":")
                if k in ("Version", "Commit", "Subject", "Date"):
                    info[k] = v.strip()
            body = "\n".join(f"{k}: {info[k]}" for k in ("Version", "Date", "Subject", "Commit") if k in info)
            return {"ok": True, "text": body,
                    "note": f"{remote} 未提供 release notes（本機無 appstream 資料）；以下是遠端最新版的提交資訊，不是更新說明"}
    return {"ok": False, "text": "", "note": "找不到這個 app 的 appstream 資料，也無法查詢遠端"}


def get_changelog(source, ident, installed_version=""):
    key = (source, ident, installed_version)
    if key in _CHANGELOG_CACHE:
        return _CHANGELOG_CACHE[key]
    if source == "apt":
        res = changelog_apt(ident, installed_version)
    elif source == "flatpak":
        res = changelog_flatpak(ident, installed_version)
    elif source == "snap":
        res = {"ok": False, "text": "", "note": f"Snap 商店不提供更新說明。可到 https://snapcraft.io/{ident} 查看發行者頁面。"}
    else:
        res = {"ok": False, "text": "", "note": "未知來源"}
    if res["ok"]:
        _CHANGELOG_CACHE[key] = res
    return res


# ---------- 硬體資訊（全部免 root；拿不到的欄位回 None，前端顯示「—」）----------

DMI_DIR = "/sys/devices/virtual/dmi/id"
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "board_vendor", "board_name",
              "bios_vendor", "bios_version", "bios_date")


def _read(path, default=None):
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _meminfo():
    d = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = int(v.strip().split()[0]) * 1024 if v.strip() else 0
    return d


def _os_release():
    d = {}
    for line in (_read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        d[k] = v.strip('"')
    return d


def _dpkg_version(pkg):
    out = _run(["dpkg-query", "-W", "-f=${Version}", pkg], timeout=5)
    return out.strip() if out else None


def _lscpu():
    out = _run(["lscpu", "-J"], timeout=10)
    if not out:
        return None
    try:
        rows = json.loads(out)["lscpu"]
    except (ValueError, KeyError):
        return None
    flat = []
    def walk(items):
        for it in items:
            flat.append((it.get("field", "").rstrip(":"), it.get("data", "")))
            if it.get("children"):
                walk(it["children"])
    walk(rows)
    get = lambda k: next((v for f, v in flat if f == k), None)
    # big.LITTLE 會有多個 Model name，各自帶核心數與最高時脈
    clusters, cur = [], None
    for f, v in flat:
        if f == "Model name":
            cur = {"model": v, "cores": None, "max_mhz": None}
            clusters.append(cur)
        elif cur and f == "Core(s) per socket" and cur["cores"] is None:
            cur["cores"] = v
        elif cur and f == "CPU max MHz" and cur["max_mhz"] is None:
            cur["max_mhz"] = v
    return {
        "architecture": get("Architecture"),
        "cpus": get("CPU(s)"),
        "vendor": get("Vendor ID"),
        "clusters": clusters,
        "l2": get("L2 cache"), "l3": get("L3 cache"),
    }


# ---------- NVML（偷師 DGX-Spark-Dashboard：用驅動函式庫直接讀，不每次開 nvidia-smi 子程序）----------
# 用 ctypes 開系統自帶的 libnvidia-ml.so，不需要 pip 套件。init 一次留著（每次 init/shutdown 約 9 ms，讀取本身 0.001 ms）。
# GB10 統一記憶體：記憶體資訊與功耗上限回 NOT_SUPPORTED（rc 3），和 nvidia-smi 印 N/A 一致，照實回 None。
import ctypes

_NVML = {"lib": None, "handle": None, "ok": False, "tried": False, "lock": threading.Lock()}


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _nvml():
    with _NVML["lock"]:
        if _NVML["tried"]:
            return _NVML if _NVML["ok"] else None
        _NVML["tried"] = True
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                return None
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return None
            _NVML.update(lib=lib, handle=h, ok=True)
            return _NVML
        except OSError:
            return None


def _nvml_uint(fn, *args):
    """呼叫回 unsigned int 的 NVML 函式；NOT_SUPPORTED 或任何錯誤回 None。"""
    n = _nvml()
    if not n:
        return None
    v = ctypes.c_uint()
    return v.value if getattr(n["lib"], fn)(n["handle"], *args, ctypes.byref(v)) == 0 else None


def _nvml_str(fn, on_device=True):
    n = _nvml()
    if not n:
        return None
    b = ctypes.create_string_buffer(96)
    rc = getattr(n["lib"], fn)(n["handle"], b, 96) if on_device else getattr(n["lib"], fn)(b, 96)
    return b.value.decode(errors="replace") if rc == 0 else None


def _gpu_static_nvml():
    n = _nvml()
    if not n:
        return None
    cuda = ctypes.c_int()
    cuda_v = None
    if n["lib"].nvmlSystemGetCudaDriverVersion_v2(ctypes.byref(cuda)) == 0:
        cuda_v = f"{cuda.value // 1000}.{(cuda.value % 1000) // 10}"
    pl = _nvml_uint("nvmlDeviceGetPowerManagementLimit")
    # 溫度門檻：1=SLOWDOWN（降頻）、0=SHUTDOWN、3=GPU_MAX。這是驅動給的真值，不是推算。
    slowdown = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 1)
    shutdown = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 0)
    gpu_max = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 3)
    max_sm = _nvml_uint("nvmlDeviceGetMaxClockInfo", 1)
    return {
        "name": _nvml_str("nvmlDeviceGetName"), "driver": _nvml_str("nvmlSystemGetDriverVersion", on_device=False),
        "vbios": _nvml_str("nvmlDeviceGetVbiosVersion"), "bus": None,
        "max_sm_mhz": f"{max_sm} MHz" if max_sm else None, "memory_total": None, "cuda": cuda_v,
        "power_limit_w": pl / 1000 if pl else None,
        "throttle_temp_c": slowdown, "shutdown_temp_c": shutdown, "gpu_max_temp_c": gpu_max,
        "throttle_source": "NVML slowdown threshold" if slowdown else None,
        "unified_memory": True, "source": "NVML",
    }


def _gpu_live_nvml():
    n = _nvml()
    if not n:
        return None
    u = _NvmlUtil()
    util = u.gpu if n["lib"].nvmlDeviceGetUtilizationRates(n["handle"], ctypes.byref(u)) == 0 else None
    temp = _nvml_uint("nvmlDeviceGetTemperature", 0)
    power = _nvml_uint("nvmlDeviceGetPowerUsage")
    clk = _nvml_uint("nvmlDeviceGetClockInfo", 1)
    return {"temp_c": temp, "util_pct": util, "power_w": power / 1000 if power is not None else None,
            "sm_mhz": clk, "memory_used": None, "source": "NVML"}


def _gpu_static():
    via = _gpu_static_nvml()
    if via:
        # PCI bus id 只有 nvidia-smi 好拿，補一次（靜態，快取 60 秒內只跑一次）
        out = _run(["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader"], timeout=10)
        via["bus"] = out.strip() if out else None
        return via
    return _gpu_static_smi()


def _gpu_live():
    return _gpu_live_nvml() or _gpu_live_smi()


def _gpu_static_smi():
    out = _run(["nvidia-smi", "--query-gpu=name,driver_version,vbios_version,pci.bus_id,clocks.max.sm,memory.total,power.limit,temperature.gpu.tlimit,temperature.gpu",
                "--format=csv,noheader"], timeout=10)
    if not out:
        return None
    parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    cuda = None
    head = _run(["nvidia-smi"], timeout=10) or ""
    m = re.search(r"CUDA Version:\s*([\d.]+)", head)
    if m:
        cuda = m.group(1)
    mem_total = None if parts[5].startswith("[") else parts[5]
    def watts(x):
        m2 = re.match(r"([\d.]+)", x)
        return float(m2.group(1)) if m2 else None
    return {"name": parts[0], "driver": parts[1], "vbios": parts[2], "bus": parts[3],
            "max_sm_mhz": parts[4], "memory_total": mem_total, "cuda": cuda,
            "power_limit_w": watts(parts[6]) if len(parts) > 6 else None,
            # tlimit 是「距離某個溫度上限還有幾度」，加當下溫度只是估計；NVML 可用時以它的 slowdown 門檻為準
            "throttle_temp_c": (watts(parts[7]) + watts(parts[8])) if len(parts) > 8 and watts(parts[7]) is not None and watts(parts[8]) is not None else None,
            "throttle_source": "nvidia-smi tlimit 推算", "source": "nvidia-smi",
            "unified_memory": mem_total is None}


def _gpu_live_smi():
    out = _run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,power.draw,clocks.sm,memory.used",
                "--format=csv,noheader,nounits"], timeout=10)
    if not out:
        return None
    p = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    num = lambda x: None if x.startswith("[") or x == "" else float(x)
    return {"temp_c": num(p[0]), "util_pct": num(p[1]), "power_w": num(p[2]), "sm_mhz": num(p[3]), "memory_used": num(p[4]), "source": "nvidia-smi"}


def _disks():
    out = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,ROTA,TRAN,FSTYPE,MOUNTPOINTS"], timeout=10)
    if not out:
        return None
    try:
        devs = json.loads(out)["blockdevices"]
    except (ValueError, KeyError):
        return None
    disks = []
    for d in devs:
        if d.get("type") != "disk":
            continue
        parts = []
        for c in d.get("children") or []:
            mps = [m for m in (c.get("mountpoints") or []) if m]
            usage = None
            if mps:
                try:
                    st = os.statvfs(mps[0])
                    usage = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
                except OSError:
                    pass
            parts.append({"name": c["name"], "size": c.get("size"), "fstype": c.get("fstype"), "mountpoints": mps, "usage": usage})
        disks.append({"name": d["name"], "size": d.get("size"), "model": (d.get("model") or "").strip(),
                      "rotational": bool(d.get("rota")), "transport": d.get("tran"), "partitions": parts})
    return disks


def _network():
    out = _run(["ip", "-j", "addr"], timeout=10)
    if not out:
        return None
    try:
        ifs = json.loads(out)
    except ValueError:
        return None
    res = []
    for i in ifs:
        name = i.get("ifname", "")
        if name == "lo":
            continue
        speed = _read(f"/sys/class/net/{name}/speed")
        kind = "wifi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else \
               "virtual" if os.path.islink(f"/sys/class/net/{name}/device") is False and not os.path.exists(f"/sys/class/net/{name}/device") else "ethernet"
        res.append({
            "name": name, "state": i.get("operstate"), "mac": i.get("address"),
            "ipv4": [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet"],
            "ipv6": [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet6" and a.get("scope") == "global"],
            "speed_mbps": int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            "kind": kind, "mtu": i.get("mtu"),
        })
    return res


_SENS_CACHE = {"ts": 0, "data": []}


def _sensors():
    """hwmon 溫度。ACPI 溫度區每次讀約 48 ms，快取 5 秒；其他項目全部加起來不到 1 ms。"""
    if time.time() - _SENS_CACHE["ts"] < 5:
        return _SENS_CACHE["data"]
    res = _sensors_read()
    _SENS_CACHE.update(ts=time.time(), data=res)
    return res


def _sensors_read():
    res = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read(os.path.join(h, "name"), "?")
        for t in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            v = _read(t)
            if not v or not v.lstrip("-").isdigit():
                continue
            label = _read(t.replace("_input", "_label"), "") or os.path.basename(t).replace("_input", "")
            res.append({"chip": chip, "label": label, "temp_c": int(v) / 1000})
    return res


# ---------- USB 樹（/sys/bus/usb/devices，免 root；名稱缺的用 usb.ids 補）----------

USB_IDS = "/usr/share/misc/usb.ids"
_USB_IDS_CACHE = None
USB_CLASS = {
    "00": ("依介面", ""), "01": ("音訊", "audio"), "02": ("通訊", "comm"), "03": ("人機介面", "hid"),
    "05": ("實體", "other"), "06": ("影像", "image"), "07": ("印表機", "printer"), "08": ("大量儲存", "storage"),
    "09": ("集線器", "hub"), "0a": ("CDC 資料", "comm"), "0b": ("智慧卡", "other"), "0d": ("內容安全", "other"),
    "0e": ("視訊", "video"), "0f": ("個人健康", "other"), "10": ("音訊/視訊", "video"), "11": ("Billboard", "billboard"),
    "dc": ("診斷", "other"), "e0": ("無線", "wireless"), "ef": ("複合", "other"), "fe": ("應用特定", "other"), "ff": ("廠商自訂", "vendor"),
}
USB_SPEED = {"1.5": "USB 1.0 低速 1.5 Mb/s", "12": "USB 1.1 全速 12 Mb/s", "480": "USB 2.0 高速 480 Mb/s",
             "5000": "USB 3.0 5 Gb/s", "10000": "USB 3.1 10 Gb/s", "20000": "USB 3.2 20 Gb/s"}


def _usb_ids():
    global _USB_IDS_CACHE
    if _USB_IDS_CACHE is not None:
        return _USB_IDS_CACHE
    vendors, products, cur = {}, {}, None
    try:
        with open(USB_IDS, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                if line[0] != "\t" and line[0] != " ":
                    if line[0] in "0123456789abcdef" and len(line) > 6 and line[4] == " ":
                        cur = line[:4].lower(); vendors[cur] = line[6:].strip()
                    else:
                        cur = None
                elif line.startswith("\t") and not line.startswith("\t\t") and cur:
                    products[(cur, line[1:5].lower())] = line[7:].strip()
    except OSError:
        pass
    _USB_IDS_CACHE = (vendors, products)
    return _USB_IDS_CACHE


def _usb_tree():
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return None
    vendors, products = _usb_ids()
    devs = {}
    for n in os.listdir(base):
        if ":" in n:
            continue
        d = os.path.join(base, n)
        vid, pid = (_read(os.path.join(d, "idVendor")) or "").lower(), (_read(os.path.join(d, "idProduct")) or "").lower()
        if not vid:
            continue
        ifaces = []
        for i in sorted(os.listdir(base)):
            if not i.startswith(n + ":"):
                continue
            ip = os.path.join(base, i)
            cls = (_read(os.path.join(ip, "bInterfaceClass")) or "").lower()
            sub = (_read(os.path.join(ip, "bInterfaceSubClass")) or "").lower()
            proto = (_read(os.path.join(ip, "bInterfaceProtocol")) or "").lower()
            drv = os.path.basename(os.readlink(os.path.join(ip, "driver"))) if os.path.islink(os.path.join(ip, "driver")) else None
            ifaces.append({"id": i.split(":", 1)[1], "class": cls, "sub": sub, "proto": proto, "driver": drv,
                           "label": USB_CLASS.get(cls, (cls, "other"))[0]})
        # 分類：以介面類別為主，HID 再依 protocol 分鍵盤/滑鼠
        kind, kind_label = "other", "裝置"
        classes = [x["class"] for x in ifaces]
        dev_class = (_read(os.path.join(d, "bDeviceClass")) or "").lower()
        if n.startswith("usb"):
            kind, kind_label = "roothub", "USB 根集線器"
            product = None  # 下面會用 bus 編號取名，比 "xHCI Host Controller" 好認
        elif "09" in classes or dev_class == "09":
            kind, kind_label = "hub", "集線器"
        elif "03" in classes:
            protos = {x["proto"] for x in ifaces if x["class"] == "03"}
            kind = "hid"; kind_label = "鍵盤" if "01" in protos else "滑鼠" if "02" in protos else "人機介面裝置"
        elif "08" in classes: kind, kind_label = "storage", "大量儲存"
        elif "e0" in classes: kind, kind_label = "wireless", "藍牙" if any(x["driver"] == "btusb" for x in ifaces) else "無線"
        elif "01" in classes: kind, kind_label = "audio", "音訊"
        elif "0e" in classes: kind, kind_label = "video", "視訊/攝影機"
        elif "07" in classes: kind, kind_label = "printer", "印表機"
        elif "02" in classes or "0a" in classes: kind, kind_label = "comm", "通訊/網路"
        elif "06" in classes: kind, kind_label = "image", "影像"
        elif "11" in classes: kind, kind_label = "billboard", "USB-C Billboard"
        elif "ff" in classes: kind, kind_label = "vendor", "廠商自訂"
        speed = _read(os.path.join(d, "speed")) or ""
        if kind == "roothub":
            product = f"USB {'3.x' if speed not in ('12', '480', '1.5') else '2.0'} 根集線器 · Bus {_read(os.path.join(d, 'busnum'))}"
        else:
            product = _read(os.path.join(d, "product"))
        manufacturer = _read(os.path.join(d, "manufacturer"))
        mp = _read(os.path.join(d, "bMaxPower"))  # 例如 "500mA"：裝置在描述子宣告的最大耗電，不是量測
        max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
        devs[n] = {
            "path": n, "vid": vid, "pid": pid,
            "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
            "name": product or products.get((vid, pid)) or f"未知裝置 {vid}:{pid}",
            "manufacturer": manufacturer or vendors.get(vid),
            "name_from_ids": not product and (vid, pid) in products,
            "speed": speed, "speed_label": USB_SPEED.get(speed, f"{speed} Mb/s" if speed else "—"),
            "kind": kind, "kind_label": kind_label,
            "serial": _read(os.path.join(d, "serial")),
            "ports": _read(os.path.join(d, "maxchild")),
            "interfaces": ifaces,
            "busnum": _read(os.path.join(d, "busnum")), "devnum": _read(os.path.join(d, "devnum")),
            "children": [],
        }
    # 掛樹：usbN 是 bus N 的 root；"5-1.4.2" 的父是 "5-1.4"，"5-1" 的父是 "usb5"
    roots = []
    def parent_of(n):
        if n.startswith("usb"):
            return None
        if "." in n:
            return n.rsplit(".", 1)[0]
        return "usb" + n.split("-", 1)[0]
    def sortkey(n):
        return [int(x) if x.isdigit() else x for x in re.split(r"[-.]", n.replace("usb", ""))]
    for n in sorted(devs, key=sortkey):
        p = parent_of(n)
        if p and p in devs:
            devs[p]["children"].append(devs[n])
        else:
            roots.append(devs[n])
    empty = [r for r in roots if r["kind"] == "roothub" and not r["children"]]
    used = [r for r in roots if not (r["kind"] == "roothub" and not r["children"])]
    def count(node):
        return 1 + sum(count(c) for c in node["children"])
    return {"roots": used, "empty_roothubs": len(empty), "total": sum(count(r) for r in roots),
            "ids_file": os.path.exists(USB_IDS)}


def usb_ports():
    """每個 xHCI 控制器一個實體 USB-C 孔（USB2 與 USB3 各一個根集線器）。
    位置取自韌體 ACPI _PLD（/sys .../physical_location），和 Windows 裝置管理員畫圖用的是同一份資料。
    韌體對同一側的兩個孔沒有區分順序，所以前端提供「插入即亮」辨識。"""
    base = "/sys/bus/usb/devices"
    ctrls = {}
    for n in os.listdir(base):
        if not n.startswith("usb"):
            continue
        d = os.path.join(base, n)
        ctrl = os.path.basename(os.path.realpath(os.path.join(d, "..")))
        speed = _read(os.path.join(d, "speed")) or ""
        c = ctrls.setdefault(ctrl, {"controller": ctrl, "usb2_bus": None, "usb3_bus": None, "location": None,
                                    "connect_type": None, "devices": [], "internal": False})
        key = "usb3_bus" if speed not in ("12", "480", "1.5") else "usb2_bus"
        c[key] = _read(os.path.join(d, "busnum"))
        # 埠資訊（root hub 的 port1..）
        for pdir in glob.glob(os.path.join(d, f"{n.replace('usb', '')}-0:1.0", f"{n}-port*")):
            loc_dir = os.path.join(pdir, "physical_location")
            if os.path.isdir(loc_dir) and not c["location"]:
                c["location"] = {k: _read(os.path.join(loc_dir, k)) for k in ("panel", "vertical_position", "horizontal_position", "dock", "lid")}
            ct = _read(os.path.join(pdir, "connect_type"))
            if ct and ct != "unknown":
                c["connect_type"] = ct
            devlink = os.path.join(pdir, "device")
            if os.path.exists(devlink):
                dev = os.path.realpath(devlink)
                dn = os.path.basename(dev)
                # 只列直接插在這個孔上的裝置（下面的 hub 子裝置數另外算）
                kids = [k for k in os.listdir(base) if k.startswith(dn + ".") and ":" not in k]
                mp = _read(os.path.join(dev, "bMaxPower"))
                max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
                c["devices"].append({
                    "path": dn, "name": _read(os.path.join(dev, "product")) or f"{_read(os.path.join(dev, 'idVendor'))}:{_read(os.path.join(dev, 'idProduct'))}",
                    "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
                    "manufacturer": _read(os.path.join(dev, "manufacturer")),
                    "speed": _read(os.path.join(dev, "speed")),
                    "children": len(kids),
                    "bus": "usb3" if key == "usb3_bus" else "usb2",
                })
    out = []
    for c in ctrls.values():
        # 沒有 _PLD、不可熱插拔、卻接著東西 → 內部（例如藍牙模組）
        c["internal"] = c["location"] is None and bool(c["devices"])
        c["external"] = c["location"] is not None
        out.append(c)
    out.sort(key=lambda c: c["controller"])
    return out


def displays():
    """xrandr 的輸出狀態。NVIDIA 驅動把四個 USB-C 的 DP Alt Mode 輸出命名為 USB-C-0..3、HDMI 為 HDMI-0。
    需要 X session；拿不到回 None（例如沒登入桌面）。"""
    env = dict(_ENV_C)
    env.setdefault("DISPLAY", ":1")
    xa = f"/run/user/{os.getuid()}/gdm/Xauthority"
    if os.path.exists(xa):
        env.setdefault("XAUTHORITY", xa)
    try:
        r = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    cur = None
    for line in r.stdout.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)(.*)$", line)
        if m:
            cur = m.group(1)
            mode = re.search(r"(\d+x\d+)\+\d+\+\d+", m.group(3))
            size = re.search(r"(\d+)mm x (\d+)mm", m.group(3))
            out[cur] = {"connected": m.group(2) == "connected", "mode": mode.group(1) if mode else None,
                        "primary": "primary" in m.group(3), "size_mm": [int(size.group(1)), int(size.group(2))] if size else None,
                        "hz": None}
        elif cur and out[cur]["connected"] and out[cur]["mode"] and line.startswith("   ") and "*" in line:
            hz = re.search(r"([\d.]+)\*", line)
            if hz:
                out[cur]["hz"] = float(hz.group(1))
    return out


USBC_MAP_FILE = os.path.join(HERE, "data", "usbc-map.json")


def usbc_map_load():
    """實體孔 → 控制器 的對應（伺服器端持久化，所有瀏覽器共用）。
    slot 0 固定是電源輸入孔（依 ASUS 規格與評測），核心無 UCSI/typec，供電狀態偵測不到，只能手動標記。"""
    try:
        with open(USBC_MAP_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def usbc_map_save(d):
    os.makedirs(os.path.dirname(USBC_MAP_FILE), exist_ok=True)
    tmp = USBC_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USBC_MAP_FILE)


def rear_panel():
    """後面板各孔的即時狀態。版面順序（左→右）依 ServeTheHome 評測：USB-C ×4（最左為 PD 電源輸入）、HDMI、10GbE、QSFP（ConnectX-7）。"""
    ports = usb_ports()
    ctrl_by_id = {p["controller"]: p for p in ports}
    disp = displays() or {}
    net = {}
    for i in os.listdir("/sys/class/net"):
        dev = f"/sys/class/net/{i}/device"
        if not os.path.exists(dev):
            continue
        drv = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(f"{dev}/driver") else ""
        net[i] = {"driver": drv, "state": _read(f"/sys/class/net/{i}/operstate"), "speed": _read(f"/sys/class/net/{i}/speed"),
                  "carrier": _read(f"/sys/class/net/{i}/carrier")}
    eth = next(((n, v) for n, v in net.items() if v["driver"].startswith("r81")), None)
    mlx = [n for n, v in net.items() if v["driver"].startswith("mlx")]
    mlx_pci = bool(_run(["lspci", "-d", "15b3:"], timeout=5))
    usbc = []
    for k in range(4):
        ctrl = f"NVDA8000:0{k}"
        c = ctrl_by_id.get(ctrl)
        usbc.append({
            "slot": k, "dp_output": f"USB-C-{k}", "display": disp.get(f"USB-C-{k}"),
            "controller_guess": ctrl, "usb": c and {"devices": c["devices"], "location": c["location"]},
        })
    return {
        "usbc": usbc, "hdmi": disp.get("HDMI-0"), "calib": usbc_map_load(),
        "eth10g": eth and {"iface": eth[0], **eth[1]},
        "connectx7": {"pci_present": mlx_pci, "ifaces": mlx, "driver_loaded": os.path.isdir("/sys/module/mlx5_core")},
        "controllers": ports, "displays_available": bool(disp),
        "layout_source": "ServeTheHome 評測描述的後面板順序；USB-C 編號對應實體位置為推測，可用插入校準",
    }


def _list_cmd(cmd):
    out = _run(cmd, timeout=10)
    return out.strip().splitlines() if out else None


def _dmi():
    """sudo -n dmidecode：只有 sudoers 放行 dmidecode 免密碼時才拿得到；否則回 available=False，前端照實說明。"""
    try:
        # dmidecode 的 -t 不吃逗號分隔的關鍵字，只吃逗號分隔的數字：1 system、2 baseboard、3 chassis、16 memory array、17 memory device
        r = subprocess.run(["sudo", "-n", "/usr/sbin/dmidecode", "-t", "1,2,3,16,17"],
                           capture_output=True, text=True, timeout=10, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "note": f"dmidecode 執行失敗：{e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        err = err[-1] if err else f"rc={r.returncode}"
        if "password" in err.lower() or "sudo" in err.lower():
            return {"available": False,
                    "note": "dmidecode 需要 root。要顯示序號與記憶體模組，請在 sudoers 只放行 /usr/sbin/dmidecode 免密碼（見 README）"}
        return {"available": False, "note": f"dmidecode 失敗：{err}"}
    sections, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("Handle "):
            cur = {"_type": re.search(r"DMI type (\d+)", line).group(1), "_fields": {}}
            sections.append(cur)
        elif cur is not None and line.startswith("\t") and ":" in line and not line.startswith("\t\t"):
            k, _, v = line.strip().partition(":")
            cur["_fields"][k.strip()] = v.strip()
    def first(t):
        return next((x["_fields"] for x in sections if x["_type"] == t), {})
    sysf, board, chassis, arr = first("1"), first("2"), first("3"), first("16")
    mods = []
    for x in sections:
        if x["_type"] != "17":
            continue
        f = x["_fields"]
        if f.get("Size", "").lower() in ("no module installed", "not installed", ""):
            continue
        mods.append({k2: (None if f.get(k1) in (None, "None", "Unknown", "Not Specified", "Not Provided") else f.get(k1)) for k1, k2 in (
            ("Locator", "slot"), ("Size", "size"), ("Type", "type"), ("Form Factor", "form"),
            ("Speed", "speed"), ("Configured Memory Speed", "configured_speed"),
            ("Manufacturer", "manufacturer"), ("Part Number", "part"), ("Serial Number", "serial"))})
    clean = lambda v: None if v in (None, "", "None", "Unknown", "Not Specified", "Not Provided", "To Be Filled By O.E.M.", "Default string") else v
    return {
        "available": True,
        "system": {"serial": clean(sysf.get("Serial Number")), "uuid": clean(sysf.get("UUID")),
                   "sku": clean(sysf.get("SKU Number")), "family": clean(sysf.get("Family"))},
        "board": {"serial": clean(board.get("Serial Number")), "version": clean(board.get("Version"))},
        "chassis": {"type": clean(chassis.get("Type")), "serial": clean(chassis.get("Serial Number"))},
        "memory": {"max_capacity": clean(arr.get("Maximum Capacity")), "slots": clean(arr.get("Number Of Devices")),
                   "error_correction": clean(arr.get("Error Correction Type")), "modules": mods},
    }


def _bluetooth():
    """bluetoothctl（免 root）。控制器、已配對裝置、連線狀態、電量（裝置有回報才有）。"""
    if not _run(["which", "bluetoothctl"], timeout=3):
        return None
    show = _run(["bluetoothctl", "show"], timeout=5)
    if show is None:
        return {"available": False, "note": "bluetoothctl 無法連到 bluetoothd（服務未啟動或無藍牙硬體）"}
    ctl = {}
    for line in show.splitlines():
        line = line.strip()
        if line.startswith("Controller "):
            ctl["address"] = line.split()[1]
        for k in ("Name", "Alias", "Powered", "Discoverable", "Pairable"):
            if line.startswith(k + ":"):
                ctl[k.lower()] = line.split(":", 1)[1].strip()
    blocked = None
    rk = _run(["rfkill", "-J"], timeout=5)
    if rk:
        try:
            for d in json.loads(rk).get("rfkilldevices", []):
                if d.get("type") == "bluetooth":
                    blocked = (d.get("soft") == "blocked") or (d.get("hard") == "blocked")
        except ValueError:
            pass
    devices = []
    paired = _run(["bluetoothctl", "devices", "Paired"], timeout=5) or ""
    for line in paired.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[0] != "Device":
            continue
        mac, name = parts[1], parts[2]
        info = _run(["bluetoothctl", "info", mac], timeout=5) or ""
        f = {}
        for l in info.splitlines():
            l = l.strip()
            for k in ("Icon", "Connected", "Trusted", "Battery Percentage"):
                if l.startswith(k + ":"):
                    f[k] = l.split(":", 1)[1].strip()
        batt = None
        m = re.search(r"\((\d+)\)", f.get("Battery Percentage", ""))
        if m:
            batt = int(m.group(1))
        devices.append({"mac": mac, "name": name, "icon": f.get("Icon"), "connected": f.get("Connected") == "yes",
                        "trusted": f.get("Trusted") == "yes", "battery": batt})
    devices.sort(key=lambda d: (not d["connected"], d["name"].lower()))
    return {"available": True, "controller": ctl, "rfkill_blocked": blocked, "devices": devices}


def _nvme_health():
    """NVMe 健康：免 root 的型號／韌體／序號從 /sys 讀；SMART 要 sudoers 放行
    `nvme smart-log /dev/nvme0n1 --output-format=json`（參數寫死）。沒放行就回 available=False 並說明。"""
    base = "/sys/class/nvme/nvme0"
    if not os.path.isdir(base):
        return None
    # 製造商：PCI 廠商 ID（1987=Phison）＋ EUI-64 前三組 OUI（64-79-A7=Phison）。兩者都是硬體登記值，不是猜的。
    OUI = {"6479a7": "Phison", "0025b3": "Samsung", "002538": "Samsung", "8ce38e": "Kioxia/Toshiba", "e4d25c": "Kioxia",
           "000cca": "HGST/WD", "001b44": "SanDisk", "5cd2e4": "Intel", "000000": None}
    PCI_VENDOR = {"0x1987": "Phison", "0x144d": "Samsung", "0x1e0f": "Kioxia", "0x15b7": "SanDisk/WD", "0x8086": "Intel",
                  "0x1c5c": "SK hynix", "0x1cc1": "ADATA", "0x126f": "Silicon Motion", "0x1e49": "YMTC", "0x2646": "Kingston", "0x1344": "Micron"}
    vid = (_read(base + "/device/vendor") or "").lower()
    did = (_read(base + "/device/device") or "").lower()
    wwid = _read("/sys/class/block/nvme0n1/wwid") or ""
    m = re.match(r"eui\.([0-9a-f]+)", wwid)
    oui = m.group(1)[-16:][:6] if m and len(m.group(1)) >= 16 else None   # 可能有前導 0 補到 32 位，取最後 16 位才是 EUI-64
    ctrl = _run(["lspci", "-nn", "-s", os.path.basename(os.path.realpath(base + "/device"))], timeout=5) or ""
    ctrl_name = re.sub(r"^.*?: ", "", ctrl.strip().splitlines()[0]) if ctrl.strip() else None
    info = {"model": (_read(base + "/model") or "").strip(), "firmware": (_read(base + "/firmware_rev") or "").strip(),
            "serial": (_read(base + "/serial") or "").strip(), "state": _read(base + "/state"),
            "vendor": PCI_VENDOR.get(vid) or vid, "vendor_pci_id": f"{vid}:{did}", "controller": ctrl_name,
            "oui": oui, "oui_vendor": OUI.get(oui) if oui else None,
            "dramless": "DRAM-less" in (ctrl_name or ""), "hmb": os.path.exists(base + "/hmb"),
            "form_factor_note": "NAND 顆粒廠商從軟體查不到", "vendor_source": "PCI 廠商 ID 與 EUI-64 OUI（硬體登記值）"}
    try:
        r = subprocess.run(["sudo", "-n", "/usr/sbin/nvme", "smart-log", "/dev/nvme0n1", "--output-format=json"],
                           capture_output=True, text=True, timeout=10, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {**info, "available": False, "note": f"nvme 執行失敗：{e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        err = err[-1] if err else f"rc={r.returncode}"
        if "password" in err.lower() or "sudo" in err.lower():
            return {**info, "available": False, "note": "SMART 需要 root：請在 sudoers 放行 nvme smart-log（見 README），這裡只顯示免 root 的欄位"}
        return {**info, "available": False, "note": f"nvme smart-log 失敗：{err}"}
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return {**info, "available": False, "note": "nvme 輸出不是 JSON"}
    g = lambda k: j.get(k)
    # data_units 單位是 512 bytes × 1000
    du_bytes = lambda v: v * 512 * 1000 if isinstance(v, (int, float)) else None
    temp_k = g("temperature")
    crit = g("critical_warning") or 0
    warn_bits = []
    if isinstance(crit, int):
        for bit, label in ((0, "備用空間低於門檻"), (1, "溫度超出門檻"), (2, "媒體可靠度下降"), (3, "已轉唯讀"), (4, "揮發性備援記憶體失效")):
            if crit & (1 << bit):
                warn_bits.append(label)
    return {
        **info, "available": True,
        "critical_warning": crit, "warnings": warn_bits,
        "temp_c": round(temp_k - 273.15, 1) if isinstance(temp_k, (int, float)) and temp_k > 200 else temp_k,
        "percent_used": g("percent_used"), "avail_spare": g("avail_spare"), "spare_thresh": g("spare_thresh"),
        "data_written": du_bytes(g("data_units_written")), "data_read": du_bytes(g("data_units_read")),
        "host_writes": g("host_write_commands"), "host_reads": g("host_read_commands"),
        "power_on_hours": g("power_on_hours"), "power_cycles": g("power_cycles"),
        "unsafe_shutdowns": g("unsafe_shutdowns"), "media_errors": g("media_errors"), "num_err_log_entries": g("num_err_log_entries"),
        "warning_temp_time": g("warning_temp_time"), "critical_comp_time": g("critical_comp_time"),
        "source": "nvme smart-log（sudoers 僅放行此指令）",
    }


_HW_CACHE = {"ts": 0, "data": None}


def hardware_static():
    if _HW_CACHE["data"] and time.time() - _HW_CACHE["ts"] < 60:
        return _HW_CACHE["data"]
    osr = _os_release()
    mem = _meminfo()
    data = {
        "system": {
            **{k: _read(os.path.join(DMI_DIR, k)) for k in DMI_FIELDS},
            "hostname": _read("/etc/hostname"),
            "os": osr.get("PRETTY_NAME"),
            "kernel": (_run(["uname", "-r"], timeout=5) or "").strip() or None,
            "dgx_release": _dpkg_version("dgx-release"),
            "dgx_dashboard": _dpkg_version("dgx-dashboard"),
        },
        "dmi": _dmi(),
        "nvme": _nvme_health(),
        "cpu": _lscpu(),
        "cpu_cores": _cpu_topology(),
        "memory": {"total": mem.get("MemTotal"), "swap_total": mem.get("SwapTotal")},
        "gpu": _gpu_static(),
        "disks": _disks(),
        "network": _network(),
        "bluetooth": _bluetooth(),
        "usb": _list_cmd(["lsusb"]),
        "usb_tree": _usb_tree(),
        "pci": _list_cmd(["lspci"]),
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    _HW_CACHE.update(ts=time.time(), data=data)
    return data


def _cpu_jiffies():
    """/proc/stat 的總和與每核 jiffies；前端做差分算使用率。"""
    lines = (_read("/proc/stat") or "").splitlines()
    if not lines or not lines[0].startswith("cpu "):
        return None
    def parse(line):
        v = [int(x) for x in line.split()[1:]]
        return {"total": sum(v), "idle": v[3] + (v[4] if len(v) > 4 else 0)}
    cores = []
    for line in lines[1:]:
        if not re.match(r"cpu\d+ ", line):
            break
        cores.append(parse(line))
    out = parse(lines[0]); out["cores"] = cores
    return out


def _cpu_freqs():
    """每核目前時脈（kHz → MHz）。拿不到的核心回 None。"""
    res = []
    for i in range(os.cpu_count() or 0):
        v = _read(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
        res.append(int(v) // 1000 if v and v.isdigit() else None)
    return res


def _cpu_topology():
    """每核的最高時脈與調速器，用來把核心分到叢集（X925/A725 靠最高時脈區分）。"""
    cores = []
    for i in range(os.cpu_count() or 0):
        base = f"/sys/devices/system/cpu/cpu{i}/cpufreq/"
        mx = _read(base + "cpuinfo_max_freq"); mn = _read(base + "cpuinfo_min_freq")
        cores.append({"id": i, "max_mhz": int(mx) // 1000 if mx and mx.isdigit() else None,
                      "min_mhz": int(mn) // 1000 if mn and mn.isdigit() else None,
                      "governor": _read(base + "scaling_governor")})
    return cores


def _net_counters():
    """/proc/net/dev 的累計位元組數；前端拿兩次取樣算每秒流量。"""
    res = {}
    for line in (_read("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 9:
            res[name.strip()] = {"rx": int(f[0]), "tx": int(f[8])}
    return res


def _root_usage():
    try:
        st = os.statvfs("/")
        return {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        return None


# ---------- Wi-Fi 品質（iw，免 root）----------

def _wifi_iface():
    for i in os.listdir("/sys/class/net"):
        if os.path.isdir(f"/sys/class/net/{i}/wireless"):
            return i
    return None


def _wifi_live():
    """iw link + station dump：訊號、速率、MCS、重試、beacon loss。約 10 ms。"""
    iface = _wifi_iface()
    if not iface:
        return None
    link = _run(["iw", "dev", iface, "link"], timeout=5) or ""
    if "Not connected" in link or not link.strip():
        return {"iface": iface, "connected": False}
    info = _run(["iw", "dev", iface, "info"], timeout=5) or ""
    st = _run(["iw", "dev", iface, "station", "dump"], timeout=5) or ""
    g = lambda pat, src, cast=str: (lambda m: cast(m.group(1)) if m else None)(re.search(pat, src))
    freq = g(r"freq:\s*([\d.]+)", link, float)
    band = "6 GHz" if freq and freq >= 5925 else "5 GHz" if freq and freq >= 4900 else "2.4 GHz" if freq else None
    rate_re = lambda k: g(rf"{k} bitrate:\s*([\d.]+) MBit/s", st, float)
    mcs = lambda k: g(rf"{k} bitrate:.*?(HE|VHT|EHT|HT)-MCS (\d+)", st)
    def mcs_val(k):
        m = re.search(rf"{k} bitrate:.*?(HE|VHT|EHT|HT)-MCS (\d+)", st)
        return {"phy": m.group(1), "mcs": int(m.group(2))} if m else None
    return {
        "iface": iface, "connected": True,
        "ssid": g(r"SSID:\s*(.+)", link), "bssid": g(r"Connected to ([0-9a-f:]{17})", link),
        "freq_mhz": freq, "band": band, "channel": g(r"channel (\d+)", info, int), "width_mhz": g(r"width:\s*(\d+) MHz", info, int),
        "signal_dbm": g(r"signal:\s*(-?\d+)", link, int), "signal_avg_dbm": g(r"signal avg:\s*(-?\d+)", st, int),
        "tx_mbps": rate_re("tx"), "rx_mbps": rate_re("rx"), "tx_mcs": mcs_val("tx"), "rx_mcs": mcs_val("rx"),
        "tx_packets": g(r"tx packets:\s*(\d+)", st, int), "tx_retries": g(r"tx retries:\s*(\d+)", st, int),
        "tx_failed": g(r"tx failed:\s*(\d+)", st, int), "beacon_loss": g(r"beacon loss:\s*(\d+)", st, int),
        "rx_drop": g(r"rx drop misc:\s*(\d+)", st, int), "connected_s": g(r"connected time:\s*(\d+)", st, int),
        "txpower_dbm": g(r"txpower ([\d.]+) dBm", info, float),
    }


_WIFI_SCAN = {"ts": 0, "data": None}


def _wifi_aps():
    """nmcli 看得到的 AP（不主動重掃，用 NetworkManager 快取）；快取 60 秒。"""
    if time.time() - _WIFI_SCAN["ts"] < 60 and _WIFI_SCAN["data"] is not None:
        return _WIFI_SCAN["data"]
    out = _run(["nmcli", "-t", "-f", "SSID,BSSID,FREQ,CHAN,SIGNAL,RATE,SECURITY,IN-USE", "dev", "wifi", "list"], timeout=15)
    aps = []
    if out:
        for line in out.splitlines():
            # BSSID 裡的冒號被跳脫成 \:
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 8:
                continue
            ssid, bssid, freq, chan, sig, rate, sec, inuse = parts[:8]
            aps.append({"ssid": ssid, "bssid": bssid.replace("\\:", ":"), "freq_mhz": int(freq.split()[0]) if freq.split() else None,
                        "chan": int(chan) if chan.isdigit() else None, "signal_pct": int(sig) if sig.isdigit() else None,
                        "rate_mbps": int(rate.split()[0]) if rate.split() and rate.split()[0].isdigit() else None,
                        "security": sec, "in_use": inuse.strip() == "*"})
    aps.sort(key=lambda a: -(a["signal_pct"] or 0))
    _WIFI_SCAN.update(ts=time.time(), data=aps)
    return aps


def _bt_connected():
    out = _run(["bluetoothctl", "devices", "Connected"], timeout=5) or ""
    return [l.split(" ", 2)[2] for l in out.splitlines() if l.startswith("Device ") and len(l.split(" ", 2)) == 3]


def _wifi_events_24h():
    """NetworkManager 24 小時內 Wi-Fi 斷線／重連次數（journal，免 root 可讀系統單元）。"""
    out = _run(["journalctl", "-u", "NetworkManager", "--since", "-24 h", "--no-pager", "-o", "short"], timeout=15) or ""
    iface = _wifi_iface() or "wl"
    disc = len([l for l in out.splitlines() if iface in l and "activated -> deactivating" in l])
    act = len([l for l in out.splitlines() if iface in l and "Activation: successful" in l])
    return {"disconnects": disc, "activations": act}


def hardware_live():
    mem = _meminfo()
    load = (_read("/proc/loadavg") or "").split()[:3]
    up = _read("/proc/uptime")
    return {
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
                   "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")},
        "load": [float(x) for x in load] if len(load) == 3 else None,
        "uptime_s": float(up.split()[0]) if up else None,
        "gpu": _gpu_live(),
        "sensors": _sensors(),
        "cpu": _cpu_jiffies(),
        "cpu_freq": _cpu_freqs(),
        "wifi": _wifi_live(),
        "net": _net_counters(),
        "disk_root": _root_usage(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": time.time(),
    }


# ---------- 本機 LLM 探針（偷師 sparkDash；它沒做 Ollama，這裡補上）----------
import urllib.request
import urllib.error

LLM_TARGETS = [
    {"kind": "ollama", "url": "http://127.0.0.1:11434"},
    {"kind": "lmstudio", "url": "http://127.0.0.1:1234"},
    {"kind": "llama.cpp", "url": "http://127.0.0.1:8080"},
    {"kind": "vllm", "url": "http://127.0.0.1:8000"},
]


def _http_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def llm_status():
    out = []
    for t in LLM_TARGETS:
        base = t["url"]
        if t["kind"] == "ollama":
            ver = _http_json(base + "/api/version")
            if ver is None:
                continue
            ps = _http_json(base + "/api/ps") or {}
            tags = _http_json(base + "/api/tags") or {}
            out.append({
                "kind": "Ollama", "url": base, "version": ver.get("version"),
                "loaded": [{"name": m.get("name"), "size": m.get("size"), "size_vram": m.get("size_vram"),
                            "context": m.get("context_length"), "expires_at": m.get("expires_at"),
                            "quant": ((m.get("details") or {}).get("quantization_level")),
                            "params": ((m.get("details") or {}).get("parameter_size"))} for m in ps.get("models", [])],
                "installed": [{"name": m.get("name"), "size": m.get("size")} for m in tags.get("models", [])],
                "bench": True,
                "note": "Ollama 不提供 Prometheus 指標，tok/s 只能靠實際跑一段生成量測（下方按鈕）。",
            })
        elif t["kind"] == "lmstudio":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "LM Studio", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": "LM Studio 的 /v1/models 只列可用模型，不給效能指標。"})
        elif t["kind"] == "llama.cpp":
            props = _http_json(base + "/props")
            if props is None:
                continue
            slots = _http_json(base + "/slots") or []
            out.append({"kind": "llama.cpp", "url": base, "version": (props.get("build_info") or None),
                        "loaded": [{"name": (props.get("default_generation_settings") or {}).get("model") or props.get("model_path")}],
                        "installed": [], "bench": False, "slots": slots if isinstance(slots, list) else [],
                        "note": "來源 /props 與 /slots。"})
        elif t["kind"] == "vllm":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "vLLM", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": "vLLM 的 Prometheus /metrics 尚未解析，這裡只列模型。"})
    return out


_LLM_BENCH = {"lock": threading.Lock(), "state": {"status": "idle"}}
LLM_HIST_FILE = os.path.join(HERE, "data", "llm-bench.json")


def llm_hist_load():
    try:
        with open(LLM_HIST_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def llm_hist_append(rec):
    h = llm_hist_load(); h.append(rec); h = h[-200:]
    os.makedirs(os.path.dirname(LLM_HIST_FILE), exist_ok=True)
    tmp = LLM_HIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, ensure_ascii=False)
    os.replace(tmp, LLM_HIST_FILE)


def llm_bench_start(model, num_predict=128):
    with _LLM_BENCH["lock"]:
        if _LLM_BENCH["state"].get("status") == "running":
            return False
        _LLM_BENCH["state"] = {"status": "running", "model": model, "num_predict": num_predict, "phase": "載入／暖機",
                               "started": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_llm_bench_run, args=(model, num_predict), daemon=True).start()
    return True


def _llm_bench_run(model, num_predict=128):
    """Ollama decode/prefill 基準：先暖機 1 token（把載入時間隔開），再量 128 token。
    數字直接取自 Ollama 回應的 eval_count/eval_duration，不是估的。"""
    base = LLM_TARGETS[0]["url"]
    prompt = "Explain, in plain prose without lists, why the sky appears blue during the day and red at sunset. " * 4
    try:
        warm = _http_json(base + "/api/generate", {"model": model, "prompt": "hi", "stream": False,
                                                    "options": {"num_predict": 1, "temperature": 0}}, timeout=600)
        if warm is None:
            raise RuntimeError("暖機請求失敗（模型載入失敗或逾時）")
        with _LLM_BENCH["lock"]:
            _LLM_BENCH["state"]["phase"] = f"生成 {num_predict} token 中"
        r = _http_json(base + "/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                                 "options": {"num_predict": num_predict, "temperature": 0}}, timeout=900)
        if r is None:
            raise RuntimeError("量測請求失敗")
        ec, ed = r.get("eval_count") or 0, r.get("eval_duration") or 0
        pc, pd = r.get("prompt_eval_count") or 0, r.get("prompt_eval_duration") or 0
        # 模型資訊（大小／量化／參數量）從 /api/ps 抓，寫進歷史方便比較
        info = {}
        for m in (_http_json(base + "/api/ps") or {}).get("models", []):
            if m.get("name") == model:
                det = m.get("details") or {}
                info = {"size": m.get("size"), "size_vram": m.get("size_vram"), "quant": det.get("quantization_level"),
                        "params": det.get("parameter_size"), "context": m.get("context_length")}
        res = {"status": "done", "model": model, "finished": datetime.now().isoformat(timespec="seconds"),
               "decode_tps": round(ec / ed * 1e9, 1) if ed else None, "decode_tokens": ec,
               "prefill_tps": round(pc / pd * 1e9, 1) if pd else None, "prompt_tokens": pc,
               "load_ms": round((warm.get("load_duration") or 0) / 1e6), "total_ms": round((r.get("total_duration") or 0) / 1e6),
               "num_predict": num_predict, **info,
               "note": f"單一請求、temperature 0、{num_predict} token；prefill 若 prompt 被快取會偏高。"}
        llm_hist_append(res)
    except Exception as e:
        res = {"status": "error", "model": model, "error": str(e), "finished": datetime.now().isoformat(timespec="seconds")}
    with _LLM_BENCH["lock"]:
        _LLM_BENCH["state"] = res


# ---------- 磁碟：誰在吃空間（掃描在背景執行緒，快取 10 分鐘）----------

HOME = os.path.expanduser("~")
DISK_ACTIONS = {  # 白名單：只有這些指令能被 /api/disk/action 觸發
    "docker_prune": ["docker", "image", "prune", "-f"],          # 只清 dangling 映像，不動有 tag 的
    "docker_builder_prune": ["docker", "builder", "prune", "-f"],
    "trash_empty": ["bash", "-c", "rm -rf ~/.local/share/Trash/files/* ~/.local/share/Trash/info/* 2>/dev/null; echo 已清空垃圾桶"],
    "npm_cache_clean": ["bash", "-c", "npm cache clean --force 2>&1 | tail -2; du -sh ~/.npm"],
}
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


def _apt_autoremovable():
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
        cat("ollama", "Ollama 模型", _du("/usr/share/ollama"),
            "系統服務，存在 /usr/share/ollama；同一模型的不同 tag 共用 blob，清單加總會大於實際占用。可在此刪除個別模型", None, om)
        # LM Studio
        lm = os.path.join(HOME, ".lmstudio", "models")
        if os.path.isdir(lm):
            items = []
            for pub in _du_children(lm, 30):
                for mdl in _du_children(pub["path"], 30):
                    items.append({"name": f"{pub['name']}/{mdl['name']}", "bytes": mdl["bytes"], "path": mdl["path"]})
            items.sort(key=lambda x: -x["bytes"])
            cat("lmstudio", "LM Studio 模型", _du(lm), "~/.lmstudio/models；請在 LM Studio 內刪除，這裡只列出", None, items[:30])
        # Docker
        dk = _docker_df()
        if dk:
            def gb(x):
                m = re.match(r"([\d.]+)\s*([KMGT]?B)", str(x or ""))
                if not m: return 0
                return float(m.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
            total_dk = sum(gb(r.get("Size")) for r in dk["summary"])
            recl = sum(gb(r.get("Reclaimable", "").split(" ")[0]) for r in dk["summary"])
            items = [{"name": f"{r.get('Type')}：{r.get('TotalCount') or r.get('Total')} 個，可回收 {r.get('Reclaimable')}", "bytes": gb(r.get("Size"))} for r in dk["summary"]]
            items += [{"name": f"卷 {v['name']}", "bytes": gb(v.get("size")), "note": f"被 {v.get('links')} 個容器用"} for v in dk["volumes"]]
            cat("docker", "Docker", total_dk, f"映像可回收約 {recl/1e9:.1f} GB；卷不自動清（可能是資料）", "docker_prune", items)
        # snap 使用者資料（Steam 等）
        sn = os.path.join(HOME, "snap")
        if os.path.isdir(sn):
            cat("snap_home", "snap 應用資料（~/snap）", _du(sn), "Steam 遊戲、瀏覽器設定檔等；請在各應用內管理", None, _du_children(sn, 10))
        cat("flatpak", "flatpak（系統）", _du("/var/lib/flatpak"), "app＋runtime；未使用的 runtime 可用 flatpak uninstall --unused", None)
        cat("snapd", "snap（系統）", _du("/var/lib/snapd"), "snapd 預設保留舊版本 2 份", None)
        cat("apt_cache", "apt 套件快取", _du("/var/cache/apt"), "下載過的 .deb，清掉無害", "apt_clean")
        ar = _apt_autoremovable()
        cat("apt_autoremove", "apt 可自動移除的套件", None, f"{len(ar)} 個不再需要的相依套件（含舊核心）" if ar else "沒有", "apt_autoremove" if ar else None, [{"name": n, "bytes": 0} for n in ar])
        cat("journal", "系統日誌 journal", _du("/var/log/journal"), "需 root 才能 vacuum，這裡不動", None)
        cat("cache", "~/.cache", _du(os.path.join(HOME, ".cache")), "各應用快取", None, _du_children(os.path.join(HOME, ".cache"), 8))
        cat("npm", "~/.npm", _du(os.path.join(HOME, ".npm")), "npm 快取", "npm_cache_clean")
        tr = os.path.join(HOME, ".local", "share", "Trash")
        cat("trash", "垃圾桶", _du(tr), "", "trash_empty")
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


# ---------- 磁碟快滿通知（背景，每 10 分鐘；≥90% 每 6 小時提醒一次）----------

def _disk_watch():
    last = 0
    while True:
        try:
            st = os.statvfs("/")
            pct = (1 - st.f_bavail / st.f_blocks) * 100
            if pct >= 90 and time.time() - last > 6 * 3600:
                free_gb = st.f_bavail * st.f_frsize / 1e9
                subprocess.run(["notify-send", "-u", "critical", "-a", "Spark Center", "根分割區快滿了",
                                f"已用 {pct:.0f}%，剩 {free_gb:.0f} GB。開 http://localhost:{PORT}/#disk 看誰在吃空間。"], timeout=10)
                last = time.time()
        except Exception:
            pass
        time.sleep(600)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "SparkCenter/0.1"

    def log_message(self, fmt, *args):  # 安靜一點，只記錯誤
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/updates":
            try:
                self._json({
                    "ok": True,
                    "items": list_updates(),
                    "reboot": reboot_status(),
                    "last_refresh": last_refresh(),
                    "job_running": JOB.snapshot()["status"] == "running",
                })
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/job":
            self._json(JOB.snapshot())
        elif path == "/api/history":
            self._json({"ok": True, "entries": apt_history()})
        elif path == "/api/reboot":
            self._json(reboot_status())
        elif path == "/api/hardware":
            try:
                self._json({"ok": True, **hardware_static()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/rear":
            try:
                self._json({"ok": True, **rear_panel(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/usbports":
            try:
                self._json({"ok": True, "ports": usb_ports(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/wifi":
            try:
                self._json({"ok": True, "live": _wifi_live(), "aps": _wifi_aps(), "bt_connected": _bt_connected(), "events_24h": _wifi_events_24h()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/live":
            try:
                self._json({"ok": True, **hardware_live()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/disk":
            try:
                self._json({"ok": True, **disk_status(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/llm":
            try:
                with _LLM_BENCH["lock"]:
                    bench = dict(_LLM_BENCH["state"])
                self._json({"ok": True, "servers": llm_status(), "bench": bench, "history": llm_hist_load()[::-1][:30]})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/apps":
            force = "force=1" in (self.path.split("?", 1) + [""])[1]
            try:
                self._json({"ok": True, **list_apps(force=force)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/changelog":
            from urllib.parse import parse_qs
            q = parse_qs((self.path.split("?", 1) + [""])[1])
            source = (q.get("source") or [""])[0]
            ident = (q.get("id") or [""])[0]
            ver = (q.get("installed") or [""])[0]
            if not source or not ident:
                return self._json({"ok": False, "error": "缺 source 或 id"}, 400)
            self._json(get_changelog(source, ident, ver))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        data = self._body()
        if path == "/api/simulate":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": "沒有選取任何套件"}, 400)
            return self._json(simulate(names))
        if path == "/api/install":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": "沒有選取任何套件"}, 400)
            sim = simulate(names)
            if not sim["ok"]:
                return self._json(sim, 400)
            if not JOB.start("install", names):
                return self._json({"ok": False, "error": "已有工作在進行中"}, 409)
            return self._json({"ok": True})
        if path == "/api/hardware/usbc-map":
            # {"slot": 0-3, "controller": "NVDA8000:0x" | null, "note": "..."}；或 {"reset": true}
            if data.get("reset"):
                usbc_map_save({}); return self._json({"ok": True, "calib": {}})
            slot, ctrl = data.get("slot"), data.get("controller")
            if slot not in (0, 1, 2, 3):
                return self._json({"ok": False, "error": "slot 需為 0–3"}, 400)
            if ctrl is not None and not re.fullmatch(r"NVDA800[01]:0\d", str(ctrl)):
                return self._json({"ok": False, "error": "controller 格式不對"}, 400)
            m = usbc_map_load()
            if ctrl is None and not data.get("note"):
                m.pop(str(slot), None)
            else:
                m[str(slot)] = {"controller": ctrl, "note": (data.get("note") or "")[:80], "source": (data.get("source") or "manual")[:20],
                                "at": datetime.now().isoformat(timespec="seconds")}
            usbc_map_save(m)
            return self._json({"ok": True, "calib": m})
        if path == "/api/llm/bench":
            model = data.get("model")
            if not model or not isinstance(model, str):
                return self._json({"ok": False, "error": "缺 model"}, 400)
            npred = data.get("num_predict", 128)
            if npred not in (64, 128, 256, 512):
                return self._json({"ok": False, "error": "num_predict 需為 64/128/256/512"}, 400)
            if not llm_bench_start(model, npred):
                return self._json({"ok": False, "error": "已有量測在進行中"}, 409)
            return self._json({"ok": True})
        if path == "/api/disk/action":
            act = data.get("action")
            if act == "ollama_delete":
                name = data.get("name")
                if not name or not isinstance(name, str):
                    return self._json({"ok": False, "error": "缺 name"}, 400)
                req = urllib.request.Request("http://127.0.0.1:11434/api/delete", data=json.dumps({"name": name}).encode(),
                                             headers={"Content-Type": "application/json"}, method="DELETE")
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        r.read()
                except urllib.error.HTTPError as e:
                    return self._json({"ok": False, "error": f"Ollama 回 {e.code}：{e.read().decode(errors='replace')[:200]}"}, 502)
                except (urllib.error.URLError, OSError) as e:
                    return self._json({"ok": False, "error": f"連不到 Ollama：{e}"}, 502)
                _DISK["ts"] = 0
                return self._json({"ok": True})
            if act == "apt_clean":
                ok = JOB.start("aptclean")
                return self._json({"ok": ok} if ok else {"ok": False, "error": "已有工作在進行中"}, 200 if ok else 409)
            if act == "apt_autoremove":
                pk = _apt_autoremovable()
                if not pk:
                    return self._json({"ok": False, "error": "沒有可移除的套件"}, 400)
                return self._json({"ok": JOB.start("remove", pk), "packages": pk})
            if act in DISK_ACTIONS:
                return self._json({"ok": JOB.start("shell", [act])})
            return self._json({"ok": False, "error": "未知動作"}, 400)
        if path == "/api/apps/update":
            source = data.get("source"); ids = [i for i in data.get("ids", []) if isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9._+-]+", i)]
            if source not in ("flatpak", "snap") or not ids:
                return self._json({"ok": False, "error": "source 需為 flatpak/snap 且 ids 非空"}, 400)
            if not JOB.start(source, ids):
                return self._json({"ok": False, "error": "已有工作在進行中"}, 409)
            return self._json({"ok": True})
        if path == "/api/refresh":
            if not JOB.start("refresh"):
                return self._json({"ok": False, "error": "已有工作在進行中"}, 409)
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    threading.Thread(target=_disk_watch, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spark Center on http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
