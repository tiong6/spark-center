#!/usr/bin/env python3
"""Spark Center — 可勾選項目的 apt 更新頁，只綁 127.0.0.1。

資料來源與誠實原則：
- 清單：python-apt 讀本機 apt cache（和 `apt list --upgradable` 同一份）。
- 影響範圍：安裝前一律用 python-apt 在記憶體模擬，把「實際會動到的套件」列給使用者。
- 安裝：走 aptdaemon 的 D-Bus 介面（和 DGX Dashboard 同一個後端），授權由 polkit 桌面視窗處理。
- 重開機：只讀 /var/run/reboot-required 與 fwupd 的等重開狀態，有才提示；程式本身絕不自動重開，
  只有使用者按「重新開機」鈕（/api/reboot）才執行，且沒有東西等重開就拒絕。
"""
import concurrent.futures
import glob
import hashlib
import http.client
import shutil
import tempfile
import html
import shlex
import socket
import stat
from pathlib import Path
import gzip
import html as html_mod
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import apt
import apt_pkg
import aptdaemon.client
import aptdaemon.enums as aenums
from gi.repository import GLib

from backend import hardware, disk, stability
from tools import model_capabilities
from backend.messages import LANG_DEFAULT, MSG, msg
from backend.system import HERE, READONLY, NO_SWEEP, _ENV_C, _run, _http_json, process_label

HOST = "127.0.0.1"
PORT = int(os.environ.get("SPARK_UPDATER_PORT", "11001"))



REBOOT_FLAG = "/var/run/reboot-required"
REBOOT_PKGS = "/var/run/reboot-required.pkgs"
APT_HISTORY = "/var/log/apt/history.log"
APT_LISTS = "/var/lib/apt/lists"


# 快取、背景工作及既有快照保留中文原值，只在 HTTP 回應副本翻譯，避免兩個語言互相污染。
# 規則只涵蓋本工具字串表的完整句型；不改套件、路徑、外部指令輸出或使用者校準備註。
_MSG_EXACT = {value: key for key, value in MSG[LANG_DEFAULT].items() if "{p" not in value}
_MSG_PATTERNS = []
for _key, _template in MSG[LANG_DEFAULT].items():
    if "{p" in _template:
        _parts = re.split(r"(\{p\d+\})", _template)
        _pattern = "".join("(?P<" + part[1:-1] + ">.*?)" if re.fullmatch(r"\{p\d+\}", part) else re.escape(part) for part in _parts)
        _MSG_PATTERNS.append((_key, re.compile(_pattern, re.S)))


def _message_text(value, lang):
    if lang == LANG_DEFAULT or not re.search("[一-鿿]", value):
        return value
    if value in _MSG_EXACT:
        return msg(_MSG_EXACT[value], lang)
    for key, pattern in _MSG_PATTERNS:
        found = pattern.fullmatch(value)
        if found:
            return msg(key, lang, **{k: _message_text(v, lang) for k, v in found.groupdict().items()})
    # 工作記錄的時間戳與組合說明，逐個翻譯已知片段，保留其餘原始資料。
    stamp = re.match(r"^(\d{2}:\d{2}:\d{2} )(.*)$", value, re.S)
    if stamp:
        return stamp[1] + _message_text(stamp[2], lang)
    for source, key in sorted(_MSG_EXACT.items(), key=lambda item: len(item[0]), reverse=True):
        if len(source) > 5 and value.startswith(source):
            return msg(key, lang) + _message_text(value[len(source):], lang)
        if len(source) > 5 and value.endswith(source):
            return _message_text(value[:-len(source)], lang) + msg(key, lang)
    for key, pattern in _MSG_PATTERNS:
        # 有固定句尾才可在組合文字中找邊界，避免吞掉後面的外部資料。
        if not re.search(r"\{p\d+\}$", MSG[LANG_DEFAULT][key]):
            value = pattern.sub(lambda found: msg(key, lang, **{
                k: _message_text(v, lang) for k, v in found.groupdict().items()}), value)
    for separator in (" · ", "、"):
        if separator in value:
            parts = value.split(separator)
            translated = [_message_text(part, lang) for part in parts]
            if translated != parts:
                return (", " if separator == "、" else separator).join(translated)
    return value


def _localized_response(value, lang, field=None):
    if lang == LANG_DEFAULT or field in {
        "calib", "path", "mountpoint", "mountpoints", "ssid", "bssid", "commandline",
        "command", "packages", "model", "id", "serial", "serial_number",
    }:
        return value
    if isinstance(value, dict):
        result = {k: _localized_response(v, lang, k) for k, v in value.items()}
        if "name_zh" in result:
            result["name_zh"] = ""  # 英文介面使用 .desktop 的原始 Name，不翻譯產品名稱。
        return result
    if isinstance(value, list):
        return [_localized_response(item, lang, field) for item in value]
    if isinstance(value, str):
        text = _message_text(value, lang)
        if field in ("group", "origin"):
            text = text.replace("（", " (").replace("）", ")")   # 來源名稱的全形括號在英文介面改半形
        return text
    return value


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


# 來源主機 → 人看得懂的名稱。廠商在 Release 檔的 Origin 欄位常填得看不懂（VS Code 填 "code stable"、
# NodeSource 填 ". nodistro"、OpenAI 乾脆不填），這張表只做「翻譯」，原始 Origin 仍隨 site 一起顯示。
SITE_NAMES = {
    "packages.microsoft.com": "Microsoft（VS Code）",
    "persistent.oaistatic.com": "OpenAI（ChatGPT／Codex）",
    "deb.nodesource.com": "NodeSource（Node.js）",
    "dl.google.com": "Google（Chrome）",
    "us-central1-apt.pkg.dev": "Google（Antigravity）",
    "brave-browser-apt-release.s3.brave.com": "Brave",
    "repository.spotify.com": "Spotify",
    "pkgs.tailscale.com": "Tailscale",
    "packagecloud.io": "GitHub（git-lfs）",
    "esm.ubuntu.com": "Ubuntu Pro（ESM）",
    "snapshot.ppa.launchpadcontent.net": "Canonical PPA",
    "nvidia.github.io": "NVIDIA（container toolkit）",
    "workbench.download.nvidia.com": "NVIDIA（AI Workbench）",
    "developer.download.nvidia.com": "NVIDIA（CUDA／HPC SDK）",
    "repo.download.nvidia.com": "NVIDIA（Spark OS）",
}


def _group_name(orig):
    """把來源整理成人看得懂的分組名。先查主機對照表，沒有就用 Origin 欄位，再沒有就用網址。"""
    site = orig["site"]
    if site in SITE_NAMES:
        return SITE_NAMES[site]
    name = orig["origin"] or orig["label"] or site or msg('unknown_source', LANG_DEFAULT)
    if site.endswith("nvidia.com"):
        # NVIDIA 有多個 repo（spark / baseos / cuda），保留路徑辨識
        return f"NVIDIA ({site})"
    return name


# Spark OS 本體／NVIDIA 平台套件：Dashboard 的「韌體＋重開機」流程對這些才有意義
SPARK_CORE_RE = re.compile(r"^(dgx-|nvidia-dgx|nvidia-driver|nvidia-kernel|nvidia-fabricmanager|"
                           r"linux-image-nvidia|linux-headers-nvidia|linux-nvidia|linux-image-\d[\d.]*-nvidia)")
# 核心與 NVIDIA 簽章模組是兩個獨立 meta 套件，彼此沒有相依：只升其中一個會開出沒有 GPU 驅動的系統
KERNEL_RE = re.compile(r"^(linux-image-nvidia|linux-nvidia|linux-headers-nvidia|linux-image-\d[\d.]*-nvidia)")
NVMOD_RE = re.compile(r"^linux-modules-nvidia-")


def kernel_pairing_warning(selected, upgradable):
    """勾了核心沒勾 NVIDIA 模組（或反過來）就警告。這是可勾選設計唯一比 Dashboard 危險的地方。"""
    sel_k = [n for n in selected if KERNEL_RE.match(n)]
    sel_m = [n for n in selected if NVMOD_RE.match(n)]
    av_k = [n for n in upgradable if KERNEL_RE.match(n)]
    av_m = [n for n in upgradable if NVMOD_RE.match(n)]
    if sel_k and av_m and not sel_m:
        return {"kind": "missing_modules", "missing": sorted(av_m),
                "message": msg('missing_modules', LANG_DEFAULT)}
    if sel_m and av_k and not sel_k:
        return {"kind": "missing_kernel", "missing": sorted(av_k),
                "message": msg('missing_kernel', LANG_DEFAULT)}
    return None


_DASH_CACHE = {"ts": 0, "data": None}


DASH_ADMIN_UNIT = "dgx-dashboard-admin.service"


def dashboard_recheck(log=lambda s: None):
    """裝完更新後叫 DGX Dashboard 重查。它的「有更新」來自 root 後台的快照，更新完常常還亮著（真機兩次）。
    重啟 dgx-dashboard-admin.service 需要 root：先用 pkcheck 問 polkit 有沒有被放行（不會跳視窗），
    有（README 那條選用規則）就 systemctl --no-ask-password restart；沒有就只留一句提示，不卡工作、不跳密碼。"""
    if READONLY:
        return False
    # 為什麼用 sudo -n 而不是 polkit：systemctl 走 polkit 時 --no-ask-password 只擋終端機提示，桌面的 polkit 視窗照樣跳
    # （真機踩到：測試時跳了密碼視窗）；pkcheck 帶 detail 又只准 root 問。sudo -n 沒放行就安靜失敗，絕不跳視窗。
    # 只在 Dashboard 後台回「ready」時重啟：它正在跑 OTA（狀態 pending 之類）時重啟會打斷 NVIDIA 自己的更新。
    st = _run(["busctl", "--system", "call", "com.nvidia.dgx.dashboard.admin1", "/com/nvidia/dgx/dashboard/admin",
               "com.nvidia.dgx.dashboard.admin1", "GetOTAAvailabilitySnapshot"], timeout=10) or ""
    if '"ready"' not in st:
        log(msg("dash_recheck_busy", LANG_DEFAULT, state=st.strip()[:60] or "?")); return False
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", DASH_ADMIN_UNIT], capture_output=True, text=True, timeout=60, env=_ENV_C)
    except Exception as e:
        log(msg("dash_recheck_failed", LANG_DEFAULT, err=str(e)[:120])); return False
    if r.returncode == 0:
        log(msg("dash_recheck_done", LANG_DEFAULT)); return True
    err = (r.stderr or r.stdout).strip()
    if "password" in err.lower() or "sudo" in err.lower():
        log(msg("dash_recheck_hint", LANG_DEFAULT)); return False
    log(msg("dash_recheck_failed", LANG_DEFAULT, err=err[:120])); return False


# ---------- Ubuntu 分階段更新（phased updates） ----------
# Ubuntu 的 -updates 用 Phased-Update-Percentage 逐步放量；每台機器依 machine-id 雜湊決定有沒有被納入。
# apt upgrade／aptdaemon 的整機升級（DGX Dashboard）／python-apt 的 cache.upgrade() 都會把沒納入的「保留」，
# 但 is_upgradable 與 mark_upgrade() 不看它——所以這裡的清單要標出來、安裝前要另外確認，否則會無聲越過 Ubuntu 的放量。
# 判定直接用 python-apt 的 pkg.phasing_applied（和 apt upgrade 同一個演算法），不自己算雜湊。
PHASED_CONF = "/etc/apt/apt.conf.d/99spark-center-phased"
PHASED_KEYS = {"always": "APT::Get::Always-Include-Phased-Updates", "never": "APT::Get::Never-Include-Phased-Updates"}
PHASED_RE = re.compile(r"(Always|Never)-Include-Phased-Updates", re.I)


def phased_info(pkg):
    """{"percent": 放量百分比或 None, "held": 這台機器被擋住}；兩者都沒有就 None（不是分階段套件）。"""
    pct = None
    try:
        v = pkg.candidate.record.get("Phased-Update-Percentage") if pkg.candidate else None
        pct = int(v) if v is not None else None
    except (ValueError, TypeError, KeyError, AttributeError):
        pct = None
    held = bool(getattr(pkg, "phasing_applied", False))
    if pct is None and not held:
        return None
    return {"percent": pct, "held": held}


def phased_policy_status():
    """全機策略：生效值用 apt-config 讀（不管誰設的），再找是哪些檔設的。mode: default／always／never；讀不到就 None。"""
    out = _run(["apt-config", "dump", "--format", "%f=%v%n", PHASED_KEYS["always"], PHASED_KEYS["never"]], timeout=10)
    mode = None
    if out is not None:
        mode = "default"
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if vals.get(PHASED_KEYS["always"], "").lower() in ("true", "1", "yes"):
            mode = "always"
        elif vals.get(PHASED_KEYS["never"], "").lower() in ("true", "1", "yes"):
            mode = "never"
    sources = []
    for path in ["/etc/apt/apt.conf"] + sorted(glob.glob("/etc/apt/apt.conf.d/*")):
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                if PHASED_RE.search(line) and not line.lstrip().startswith(("//", "#")):
                    sources.append(path); break
        except OSError:
            continue
    return {"mode": mode, "sources": sources, "file": PHASED_CONF, "ours": os.path.exists(PHASED_CONF),
            "other": [x for x in sources if x != PHASED_CONF]}


def phased_policy_set(mode):
    """寫／刪 /etc/apt/apt.conf.d/99spark-center-phased（pkexec 跳一次密碼）。改完重讀本程序的 apt 設定，
    否則 phasing_applied 還是舊判定；aptdaemon 與 Dashboard 的後台是獨立程序，要到它們下次啟動才讀到。"""
    if READONLY:
        return {"ok": False, "error": msg("readonly_mode", LANG_DEFAULT)}
    if mode not in ("default", "always", "never"):
        return {"ok": False, "error": msg("phased_bad_mode", LANG_DEFAULT)}
    if mode == "default":
        ok, err = _pkexec(["/usr/bin/rm", "-f", PHASED_CONF])
    else:
        d = tempfile.mkdtemp(prefix="spark-center-phased-")
        src = os.path.join(d, os.path.basename(PHASED_CONF))
        with open(src, "w", encoding="utf-8") as f:
            f.write(f"// Spark Center: Ubuntu phased updates policy = {mode}\n{PHASED_KEYS[mode]} \"true\";\n")
        os.chmod(src, 0o644)
        ok, err = _pkexec(["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0644", src, PHASED_CONF])
        shutil.rmtree(d, ignore_errors=True)
    if ok:
        apt_pkg.init_config()
        _DASH_CACHE["ts"] = 0
    st = phased_policy_status()
    return {"ok": ok, "error": err, "policy": st}


def dashboard_equivalent():
    """DGX Dashboard 那顆 Update 實際涵蓋什麼。依據：apt 歷史裡它的 aptdaemon 角色是 role-upgrade-system，
    且歷次都有安裝甚至移除套件——aptdaemon 的 safe_mode=True 會跳過這類升級，所以它用的是完整升級。
    之後再跑韌體並強制重開機（前端打 /update_reboot）。
    dist-upgrade 模擬要 1.5 秒以上，而更新分頁每次開頁都會打一次，所以快取起來；套件裝完會一併失效。"""
    if _DASH_CACHE["data"] and time.time() - _DASH_CACHE["ts"] < 300:
        return _DASH_CACHE["data"]
    out = _run(["apt-get", "-s", "dist-upgrade"], timeout=180) or ""
    ups, news = [], []
    for m in re.finditer(r"^Inst (\S+)(?: \[([^\]]*)\])?", out, re.M):
        (ups if m.group(2) else news).append(m.group(1))
    remv = re.findall(r"^Remv (\S+)", out, re.M)
    fw = None
    try:
        fw = fwupd_status().get("updates")
    except Exception:
        pass
    held = []
    try:
        held = sorted(p.name for p in apt.Cache() if p.is_upgradable and getattr(p, "phasing_applied", False))
    except Exception:
        held = None   # 讀不到就 None，前端顯示「—」
    data = {"upgrade": len(ups), "install": len(news), "remove": len(remv), "firmware": fw,
            "packages": sorted(set(ups + news)), "removes": sorted(set(remv)), "phased_held": held,
            "note": msg('dashboard_note', LANG_DEFAULT)}
    _DASH_CACHE.update(ts=time.time(), data=data)
    return data


_MODFW_CACHE = {"ts": 0, "names": set()}


def _loaded_module_firmware():
    """已載入核心模組用 MODULE_FIRMWARE 宣告會請求的 firmware 檔名（modinfo -F firmware）。
    這是「本機驅動會不會用到某包 firmware」唯一能查證的依據；沒宣告但動態請求的驅動查不到，
    所以前端把結論標成「依 modinfo」而不是斷言沒用。"""
    if time.time() - _MODFW_CACHE["ts"] < 600:
        return _MODFW_CACHE["names"]
    names = set()
    try:
        mods = [l.split()[0] for l in open("/proc/modules")]
    except OSError:
        mods = []
    for m in mods:
        out = _run(["modinfo", "-F", "firmware", m], timeout=5) or ""
        names.update(l.strip() for l in out.splitlines() if l.strip())
    _MODFW_CACHE.update(ts=time.time(), names=names)
    return names


def _firmware_usage(pkg, cache):
    """linux-firmware-* 子套件：被誰拖進來、本機已載入的模組有幾個檔案會用到。不是子套件回 None。"""
    if not pkg.name.startswith("linux-firmware-") or not pkg.installed:
        return None
    meta = cache["linux-firmware"] if "linux-firmware" in cache else None
    pulled_by = ""
    if meta and meta.installed and any(d.name == pkg.name for dep in meta.installed.dependencies for d in dep):
        pulled_by = "linux-firmware"
    wanted = _loaded_module_firmware()
    files = used = 0
    for f in pkg.installed_files:
        if not f.startswith("/lib/firmware/") or not os.path.isfile(f):
            continue
        files += 1
        rel = f[len("/lib/firmware/"):]
        for suf in (".zst", ".xz"):
            if rel.endswith(suf):
                rel = rel[:-len(suf)]
        if rel in wanted:
            used += 1
    return {"auto": pkg.is_auto_installed, "pulled_by": pulled_by, "files": files, "used": used,
            "passive": pkg.is_auto_installed and used == 0 and files > 0}


# ---------- 降回上一版：更新前把被換掉的舊版 .deb 留一份 ----------
# 為什麼要自己留：Ubuntu 的來源只發布最新版，「清 apt 快取」又會把本機的舊 .deb 清掉；出事時找不到退路。
# 來源順序：本機 apt 快取（免下載）→ 來源伺服器的 pool（Ubuntu 鏡像短期內、Microsoft 等長期保留）→ Launchpad（Ubuntu 官方套件永久保留）。
# ESM 的 pool 要授權、第三方多半不留，這些誠實標「未保留」與原因。保留最近 5 次更新，每個檔案上限 150 MB。
ROLLBACK_DIR = os.path.join(HERE, "data", "rollback")
ROLLBACK_KEEP = 5
ROLLBACK_MAX_AGE_DAYS = 30   # 降回只在更新後幾天內有意義：超過這個天數的紀錄連檔案一起清（次數與天數先到先刪）
ROLLBACK_MAX_MB = 150
_ROLLBACK_LOCK = threading.Lock()


def _rollback_job_bytes(job):
    """這筆工作留下來的 .deb 總大小（npm 不留檔，算 0）。"""
    total = 0
    for p in job.get("packages", []):
        if p.get("deb"):
            try:
                total += os.path.getsize(os.path.join(ROLLBACK_DIR, p["deb"]))
            except OSError:
                pass
    return total


def rollback_prune(max_age_days=None, everything=False):
    """刪掉超過 max_age_days 的紀錄（everything=True 全刪）。回 {"removed": n, "bytes": 釋放的位元組}。索引鎖內做，和新增互斥。"""
    cutoff = None if max_age_days is None else datetime.now() - timedelta(days=max_age_days)
    removed, freed = 0, 0
    with _ROLLBACK_LOCK:
        keep = []
        for j in _rollback_index_load():
            try:
                started = datetime.fromisoformat(j.get("started") or "")
            except ValueError:
                started = None
            old = everything or (cutoff is not None and (started is None or started < cutoff))
            if old:
                freed += _rollback_job_bytes(j); removed += 1
                shutil.rmtree(os.path.join(ROLLBACK_DIR, j["id"]), ignore_errors=True)
            else:
                keep.append(j)
        if removed:
            _rollback_index_save(keep)
    return {"removed": removed, "bytes": freed}


def _rollback_index_load():
    try:
        with open(os.path.join(ROLLBACK_DIR, "index.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _rollback_index_save(jobs):
    os.makedirs(ROLLBACK_DIR, exist_ok=True)
    tmp = os.path.join(ROLLBACK_DIR, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(ROLLBACK_DIR, "index.json"))


def _deb_matches(path, name, version):
    out = _run(["dpkg-deb", "-f", path, "Package", "Version"], timeout=30) or ""
    got = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return got.get("Package") == name and got.get("Version") == version


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rollback_changes(cache, names):
    """模擬這次升級，回實際會被換掉／移除的已安裝套件（含相依帶入的），勾選的排前面。
    只備份勾選清單會漏掉相依帶動的更新：舊主程式要求舊函式庫時，只留主程式沒有完整退路。"""
    for name in names:
        pkg = cache.get(name)
        if pkg and pkg.installed and pkg.candidate and pkg.candidate != pkg.installed:
            try:
                pkg.mark_upgrade()
            except Exception:
                pass
    out = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name); out.append((name, True))
    for pkg in cache.get_changes():
        if pkg.is_installed and pkg.name not in seen and (pkg.marked_upgrade or pkg.marked_downgrade or pkg.marked_delete):
            seen.add(pkg.name); out.append((pkg.name, False))
    return out


def _fetch_file(url, dest, max_bytes, progress=lambda got, total: None):
    """下載到 dest；先看 Content-Length 擋太大的；回 (ok, reason)。
    progress(got, total)：每收到一塊就叫一次（total 讀不到是 0）——Launchpad 抓 30 MB 的舊版在慢連線要一分多鐘，
    沒有進度的話工作卡看起來像當掉（2026-10-17 真機踩到）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "spark-center"})
    with urllib.request.urlopen(req, timeout=60) as r:
        n = int(r.headers.get("Content-Length") or 0)
        if n > max_bytes:
            return False, ("too_big", n)
        got = 0
        progress(0, n)
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 << 10)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    return False, ("too_big", got)
                f.write(chunk)
                progress(got, n)
    return True, None


def rollback_prepare(names, log=lambda s: None, status=lambda s: None, step=lambda i, n, name: None, progress=lambda text: None):
    """在升級前呼叫。回這次的紀錄（也已寫進 index）。任何一個套件失敗都不影響升級本身。
    step(i, n, name)：正在處理第 i 個（從 0 起）／共 n 個，給進度卡用；134 個套件逐一去 Launchpad 抓，沒步數會像當掉。
    progress(text)：下載中的一行人話（「snapd · Launchpad 12.3 / 32.7 MB」），給工作卡的「目前」欄；每半秒最多一次。"""
    cache = apt.Cache()
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    job_dir = os.path.join(ROLLBACK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    rec = {"id": job_id, "started": datetime.now().isoformat(timespec="seconds"), "packages": []}
    max_bytes = ROLLBACK_MAX_MB * 1024 * 1024
    try:
        targets = _rollback_changes(cache, names)
    except Exception as e:
        log("rollback simulate failed: " + str(e)); targets = [(n, True) for n in names]
    for idx, (name, selected) in enumerate(targets):
        step(idx, len(targets), name)
        pkg = cache.get(name)
        entry = {"name": name, "selected": selected, "old": None, "new": None, "arch": None, "deb": None, "source": None, "verified": None, "reason": None}
        if pkg and getattr(pkg, "phasing_applied", False):
            entry["phased"] = (phased_info(pkg) or {}).get("percent")   # 當時 Ubuntu 的放量百分比；這台機器沒被納入卻裝了＝提前安裝
        rec["packages"].append(entry)
        if not pkg or not pkg.installed:
            entry["reason"] = msg("rollback_not_installed", LANG_DEFAULT); continue
        inst = pkg.installed
        entry.update(old=inst.version, new=None if pkg.marked_delete else (pkg.candidate.version if pkg.candidate else None), arch=inst.architecture)
        status(msg("rollback_preparing", LANG_DEFAULT, name=name, old=inst.version))
        fname = f"{name}_{inst.version.replace(':', '%3a')}_{inst.architecture}.deb"
        dest = os.path.join(job_dir, fname)
        # 可信雜湊：apt 索引裡這個版本的 sha256（索引由簽章過的 Release 檔背書）。舊版常已從索引消失 → None。
        try:
            expected = inst.sha256 or None
        except Exception:
            expected = None
        # 1. 本機 apt 快取：apt 下載時已核對過雜湊（root 才寫得進去）；索引還有的話再對一次。
        cached = os.path.join("/var/cache/apt/archives", fname)
        if os.path.isfile(cached) and _deb_matches(cached, name, inst.version) and (not expected or _sha256_of(cached) == expected):
            shutil.copyfile(cached, dest); entry.update(deb=os.path.relpath(dest, ROLLBACK_DIR), source="cache", verified="sha256" if expected else "apt")
            log(msg("rollback_kept", LANG_DEFAULT, name=name, old=inst.version, source=msg("rollback_src_cache", LANG_DEFAULT))); continue
        # 2. 來源伺服器的 pool（多半是 HTTP，只在索引還有雜湊可核對時採用）；
        # 3. Launchpad（HTTPS；只要這個套件任一版本來自 Ubuntu 官方來源就試——舊版從索引消失後
        #    installed.origins 會是空的，只看舊版的來源會永遠不試，而那正是需要備援的時候）。
        cands = []
        try:
            if inst.uri and expected:
                cands.append(("repo", inst.uri))
        except Exception:
            pass
        sites = {(_origin_of(v).get("site") or "") for v in pkg.versions}
        if any(st.endswith("ubuntu.com") and "esm.ubuntu.com" not in st for st in sites):
            noepoch = inst.version.split(":", 1)[-1]
            cands.append(("launchpad", f"https://launchpad.net/ubuntu/+archive/primary/+files/{name}_{noepoch}_{inst.architecture}.deb"))
        reason = msg("rollback_no_source", LANG_DEFAULT)
        if not cands:
            log(msg("rollback_no_cache_no_source", LANG_DEFAULT, name=name, old=inst.version))
        for source, url in cands:
            src_name = msg(f"rollback_src_{source}", LANG_DEFAULT)
            log(msg("rollback_fetching", LANG_DEFAULT, name=name, old=inst.version, source=src_name, url=url))
            t0 = time.time(); last = [0.0]; got_total = [0, 0]
            def _prog(got, total, _src=src_name):
                got_total[0], got_total[1] = got, total
                if time.time() - last[0] < 0.5 and got and got != total:
                    return
                last[0] = time.time()
                progress(msg("rollback_progress", LANG_DEFAULT, name=name, source=_src, got=f"{got / 1e6:.1f}", total=f"{total / 1e6:.1f}" if total else "?"))
            try:
                ok, why = _fetch_file(url, dest, max_bytes, _prog)
            except Exception as e:
                reason = msg("rollback_fetch_failed", LANG_DEFAULT, err=str(e)[:80])
                log(msg("rollback_fetch_failed_src", LANG_DEFAULT, source=src_name, err=str(e)[:80])); continue
            if not ok:
                reason = msg("rollback_too_big", LANG_DEFAULT, mb=round(why[1] / 1048576), max=ROLLBACK_MAX_MB); break
            if not _deb_matches(dest, name, inst.version):
                reason = msg("rollback_bad_deb", LANG_DEFAULT); continue
            if expected:
                if _sha256_of(dest) != expected:
                    reason = msg("rollback_hash_mismatch", LANG_DEFAULT); continue
                verified = "sha256"
            elif url.startswith("https://launchpad.net/"):
                verified = "tls"
            else:
                reason = msg("rollback_no_trusted_hash", LANG_DEFAULT); continue
            entry.update(deb=os.path.relpath(dest, ROLLBACK_DIR), source=source, verified=verified)
            log(msg("rollback_kept_dl", LANG_DEFAULT, name=name, old=inst.version, source=src_name, mb=f"{got_total[0] / 1e6:.1f}", sec=f"{time.time() - t0:.0f}")); break
        if not entry["deb"]:
            try:
                os.remove(dest)
            except OSError:
                pass
            entry["reason"] = reason
            log(msg("rollback_not_kept", LANG_DEFAULT, name=name, old=inst.version, reason=reason))
    _rollback_index_add(rec)
    return rec


def _rollback_index_add(rec):
    """把一筆新紀錄放進索引：同 id 或同一組（套件、舊版、新版）的舊紀錄連目錄一起刪（更新→降回→再更新會產生一模一樣的紀錄），
    超過 ROLLBACK_KEEP 的最舊的也刪。"""
    def _sig(j):
        return frozenset((p.get("name"), p.get("old"), p.get("new")) for p in j.get("packages", []) if p.get("deb") or p.get("kind") == "npm")
    with _ROLLBACK_LOCK:
        jobs = []
        for j in _rollback_index_load():
            if j.get("id") == rec["id"] or (_sig(j) and _sig(j) == _sig(rec)):
                shutil.rmtree(os.path.join(ROLLBACK_DIR, j["id"]), ignore_errors=True)
                continue
            jobs.append(j)
        jobs.insert(0, rec)
        for old_job in jobs[ROLLBACK_KEEP:]:
            shutil.rmtree(os.path.join(ROLLBACK_DIR, old_job["id"]), ignore_errors=True)
        _rollback_index_save(jobs[:ROLLBACK_KEEP])
    rollback_prune(ROLLBACK_MAX_AGE_DAYS)


def rollback_deb_paths(job_id, name=None):
    """該筆工作裡保留下來的 .deb（name 給了就只回那一個）。路徑限制在 ROLLBACK_DIR 底下。"""
    out = []
    for j in _rollback_index_load():
        if j.get("id") == job_id:
            for p in j.get("packages", []):
                if (name is None or p.get("name") == name) and p.get("deb"):
                    fp = os.path.realpath(os.path.join(ROLLBACK_DIR, p["deb"]))
                    if fp.startswith(os.path.realpath(ROLLBACK_DIR) + os.sep) and os.path.isfile(fp):
                        out.append((fp, p))
    return out


# apt-get 對本機 .deb 的降版指令。--allow-downgrades 明講意圖；--no-remove 擋掉「為了降函式庫而移除依賴新版的應用程式」；
# conffile 沿用更新時的政策：使用者改過的設定檔一律保留現有版本（服務的 stdin 是 /dev/null，dpkg 問不到人會直接失敗、留下未設定的套件）。
ROLLBACK_APT = ["/usr/bin/apt-get", "install", "-y", "--allow-downgrades", "--no-remove",
                "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


def rollback_simulate(paths):
    """apt-get -s 模擬降回，回傳實際會被動到的套件（不需 root、不動系統）。
    不加 --no-remove 是為了看清楚會移除誰；有移除就回 ok=False，因為降回不該順手拆掉別的應用程式。"""
    cmd = ["/usr/bin/apt-get", "-s", "-o", "Debug::NoLocking=1", "install", "-y", "--allow-downgrades"] + paths
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=dict(os.environ, LC_ALL="C", LANG="C"))
    except Exception as e:
        return {"ok": False, "error": msg("rollback_sim_failed", LANG_DEFAULT, err=str(e)[:120])}
    if r.returncode != 0:
        err = "\n".join(l for l in (r.stderr + r.stdout).splitlines() if l.startswith("E:")) or f"rc={r.returncode}"
        return {"ok": False, "error": msg("rollback_sim_failed", LANG_DEFAULT, err=err[:300])}
    changes = []
    for line in r.stdout.splitlines():
        m = re.match(r"(Inst|Remv) (\S+?)(?::\S+)? (?:\[([^\]]*)\] )?(?:\(([^\s)]+))?", line)
        if not m:
            continue
        kind, name, frm, to = m.groups()
        if kind == "Remv":
            action = "remove"
        elif not frm:
            action = "install"
        elif apt_pkg.version_compare(to or "", frm) < 0:
            action = "downgrade"
        else:
            action = "upgrade"
        changes.append({"name": name, "action": action, "from": frm or "", "to": to or "", "requested": False})
    kept = {os.path.basename(p).split("_", 1)[0] for p in paths}
    for c in changes:
        c["requested"] = c["name"] in kept
    changes.sort(key=lambda c: (not c["requested"], c["action"], c["name"]))
    removed = [c["name"] for c in changes if c["action"] == "remove"]
    if removed:
        return {"ok": False, "changes": changes, "error": msg("rollback_would_remove", LANG_DEFAULT, pkgs=", ".join(removed))}
    return {"ok": True, "changes": changes}


# ---------- npm 全域套件 ----------
# 為什麼要做：Claude Code、Gemini CLI、OpenClaw 這類工具是 npm -g 裝的，apt/snap/flatpak 都看不到，改版又快。
# 裝在使用者的 prefix（~/.npmrc 的 prefix），更新不需要 root。舊版 registry 永久保留，降回只要指定版號，不用留檔案。
# pip 與 Docker 刻意不做：系統 pip 套件是 apt 管的（PEP 668），venv 的版本是專案鎖的；Docker pull 新映像不等於容器已更新。
_NPM = {"ts": 0, "data": None, "lock": threading.Lock()}
NPM_TTL = 600
NPM_BIN = shutil.which("npm") or "/usr/bin/npm"


def npm_running(prefix, names):
    """哪些程序正在執行這些 npm 全域套件的檔案（掃 /proc/*/cmdline 找 <prefix>/lib/node_modules/<name>/）。
    為什麼要看：npm install -g 直接覆蓋目錄，正在跑的程序之後才載入的模組會讀到新版，新舊混用會出錯，更新完要重啟。
    同時從 cgroup 讀出它屬於哪個 systemd 使用者服務（app.slice/xxx.service），有的話可以一鍵重啟；沒有的只能提醒。"""
    out = {n: [] for n in names}
    if not prefix:
        return out
    base = os.path.join(prefix, "lib", "node_modules") + os.sep
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        hit = None
        for a in argv[:8]:
            a = a.decode("utf-8", "replace")
            # npm 的 bin 是符號連結（<prefix>/bin/openclaw → ../lib/node_modules/openclaw/…），cmdline 可能只留 bin 路徑，要解回真實路徑
            if "/" in a and not a.startswith(base):
                try:
                    a = os.path.realpath(a)
                except OSError:
                    pass
            if a.startswith(base):
                rest = a[len(base):]
                name = rest.split("/")[0]
                if name.startswith("@") and "/" in rest[len(name) + 1:] + "/":
                    name = name + "/" + rest[len(name) + 1:].split("/")[0]
                if name in out:
                    hit = name; break
        if not hit:
            continue
        unit = None
        try:
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
                cg = f.read().strip().split(":", 2)[-1]
            last = cg.rsplit("/", 1)[-1]
            if "/user@" in cg and last.endswith(".service"):
                unit = last
        except OSError:
            pass
        cmd = " ".join(x.decode("utf-8", "replace") for x in argv if x)
        # 程序啟動時間（/proc/<pid> 目錄的 ctime）早於套件目前的 package.json → 記憶體裡跑的是更新前的舊版
        stale = None
        try:
            started = os.stat(f"/proc/{pid}").st_ctime
            installed_at = os.stat(os.path.join(base, hit, "package.json")).st_mtime
            stale = started < installed_at
        except OSError:
            pass
        out[hit].append({"pid": int(pid), "unit": unit, "cmd": cmd[:160], "stale": stale})
    return out


def _semver_key(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 1 if m.group(4) is None else 0, m.group(4) or "")


def _semver_lt(a, b):
    ka, kb = _semver_key(a), _semver_key(b)
    return bool(ka and kb and ka < kb)


_NODE_REL = {"ts": 0, "data": None}


def node_release_info(installed):
    """Node 官方版本表：現行 LTS 是哪個大版本、已裝的大版本支援到何時。一天查一次；查不到回 error，欄位留 None（前端顯示「—」）。
    為什麼要查：大版本升級不會出現在 apt 清單（NodeSource 每個大版本是不同倉庫），使用者不會自己去看。"""
    if _NODE_REL["data"] and time.time() - _NODE_REL["ts"] < 86400:
        d = dict(_NODE_REL["data"])
    else:
        d = {"lts_major": None, "lts_version": None, "installed_end": None, "lts_end": None, "error": None, "fetched": None}
        try:
            with urllib.request.urlopen(urllib.request.Request("https://nodejs.org/dist/index.json", headers={"User-Agent": "spark-center"}), timeout=15) as r:
                idx = json.load(r)
            lts = next((x for x in idx if x.get("lts")), None)
            if lts:
                d["lts_version"] = lts["version"].lstrip("v"); d["lts_major"] = int(d["lts_version"].split(".")[0])
            with urllib.request.urlopen(urllib.request.Request("https://raw.githubusercontent.com/nodejs/Release/main/schedule.json", headers={"User-Agent": "spark-center"}), timeout=15) as r:
                d["_schedule"] = {k.lstrip("v"): v.get("end") for k, v in json.load(r).items()}
            d["fetched"] = datetime.now().isoformat(timespec="seconds")
            _NODE_REL.update(ts=time.time(), data=dict(d))   # 存副本：下面的 pop 不能動到快取，否則第二次查就沒有時程了
        except Exception as e:
            d["error"] = msg("node_release_failed", LANG_DEFAULT, err=str(e)[:80])
    sched = d.pop("_schedule", {}) or {}
    major = (installed or "").split(".")[0]
    d["installed_end"] = sched.get(major)
    d["lts_end"] = sched.get(str(d["lts_major"])) if d["lts_major"] else None
    return d


def _node_semver_satisfies(node_version, ranges):
    """用 npm 自帶的 semver 判斷 node_version 是否符合各套件的 engines.node 範圍。回 {name: True/False}；跑不了回 {}（呼叫端當「不明」）。"""
    if not ranges:
        return {}
    js = "const s=require(process.argv[1]);const r=JSON.parse(process.argv[3]);const o={};for(const k in r){try{o[k]=s.satisfies(process.argv[2],r[k])}catch(e){o[k]=null}}console.log(JSON.stringify(o))"
    for sem in ("/usr/lib/node_modules/npm/node_modules/semver", os.path.join(os.path.dirname(os.path.realpath(NPM_BIN)), "..", "lib", "node_modules", "npm", "node_modules", "semver")):
        if os.path.isdir(sem):
            out = _run(["node", "-e", js, sem, node_version, json.dumps(ranges)], timeout=20)
            if out:
                try:
                    return json.loads(out)
                except ValueError:
                    return {}
    return {}


NODE_HELPER_SOURCE = os.path.join(HERE, 'tools', 'node_source.py')
NODE_HELPER = '/usr/local/libexec/spark-center/node_source.py'


def node_helper_ready():
    try:
        p = Path(NODE_HELPER)
        for item in (p, *p.parents):
            st = item.lstat()
            if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                return False
            if item == p and (not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o100):   # 要能直接執行（pkexec 跑它本身）
                return False
        # 套件更新後必須由管理員重新安裝 helper，不偷偷升級提權程式。
        return p.read_bytes() == Path(NODE_HELPER_SOURCE).read_bytes()
    except OSError:
        return False


def node_error(detail):
    key, _, extra = str(detail).partition(':')
    return msg(key if key in MSG[LANG_DEFAULT] else 'node_command_failed', LANG_DEFAULT) + (': ' + extra if extra else '')


def node_read(*args):
    if os.path.realpath(shutil.which('node') or '') != '/usr/bin/node':
        return {'ok': False, 'error': msg('node_source_unsupported', LANG_DEFAULT)}
    if not node_helper_ready():
        # 把安裝指令連 repo 路徑一起給前端，使用者複製貼上就好，不必翻 README
        return {'ok': False, 'error': msg('node_helper_unavailable', LANG_DEFAULT), 'helper_missing': True,
                'install_cmds': [f'cd {shlex.quote(HERE)}',
                                 'sudo install -d -o root -g root -m 0755 /usr/local/libexec/spark-center',
                                 'sudo install -o root -g root -m 0755 tools/node_source.py /usr/local/libexec/spark-center/node_source.py',
                                 'sudo install -o root -g root -m 0644 tools/spark-center-node-source.policy /usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy']}
    r = subprocess.run(['/usr/bin/python3', '-I', NODE_HELPER, *args], capture_output=True,
                       text=True, timeout=90, env=_ENV_C)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return {'ok': False, 'error': node_error(r.stderr)}
    if not j.get('ok'):
        j['error'] = node_error(j.get('error', ''))
    return j


def node_status():
    j = node_read('status')
    # helper 沒裝時也要知道「有沒有升級可做」：Node 已是現行 LTS 就整塊不顯示，不該為了一個用不到的功能叫人裝 helper
    ver = j['installed'].split('-', 1)[0] if j.get('ok') else (_run(["node", "--version"], timeout=10) or "").strip().lstrip("v")
    info = node_release_info(ver) if ver else {}
    j['target'] = info.get('lts_major')
    j['expected_version'] = info.get('lts_version')
    j['release_error'] = info.get('error')
    if not j.get('ok') and ver:
        j['major'] = int(ver.split('.')[0]) if ver.split('.')[0].isdigit() else None
    # 完成與否由 helper 的 effective_state 判定（status 回來的 state 已是有效狀態），這裡不再自己改
    return j


def node_npm_impact(version):
    # 回退檢查只讀本機已安裝套件，不需要 registry 在線，也不把快取當成現值。
    r = subprocess.run([NPM_BIN, 'ls', '-g', '--depth=0', '--json'], capture_output=True,
                       text=True, timeout=60, env=_ENV_C)
    try:
        if r.returncode:
            raise ValueError()
        deps = json.loads(r.stdout).get('dependencies', {})
        prefix = (_run([NPM_BIN, 'prefix', '-g'], timeout=20) or '').strip()
        if not prefix:
            raise ValueError()
        ranges, unknown = {}, []
        for name in deps:
            try:
                with open(os.path.join(prefix, 'lib', 'node_modules', name, 'package.json')) as f:
                    meta = json.load(f)
                constraint = (meta.get('engines') or {}).get('node')
                if constraint:
                    ranges[name] = constraint
            except (OSError, ValueError, TypeError):
                unknown.append(name)
        results = _node_semver_satisfies(version.split('-', 1)[0], ranges)
        return [{'name': n, 'range': ranges.get(n), 'unknown': n in unknown or results.get(n) is None}
                for n in sorted(set(unknown) | {n for n in ranges if results.get(n) is not True})]
    except (ValueError, TypeError):
        return [{'name': 'npm', 'range': None, 'unknown': True}]


def node_plan(action, target):
    if action not in ('prepare', 'install', 'restore') or type(target) is not int:
        return {'ok': False, 'error': msg('node_bad_action', LANG_DEFAULT)}
    if action == 'prepare':
        current = node_status()
        if not current.get('ok'):
            return current
        if target != current.get('target'):
            return {'ok': False, 'error': msg('node_target_unavailable', LANG_DEFAULT)}
    j = node_read('preview', action, str(target))
    if j.get('ok'):
        j['impact'] = node_npm_impact(j['version']) if action == 'restore' and j.get('args') else []
        j['expected_version'] = current.get('expected_version') if action == 'prepare' else None
        j['confirmation'] = hashlib.sha256(json.dumps([j['token'], j['impact'], j['expected_version']], sort_keys=True).encode()).hexdigest()
    return j


def npm_status(force=False):
    """npm ls -g（全部）＋ npm outdated -g（有新版的）。outdated 有新版時結束碼是 1，不能用 _run 的 0 判定。"""
    with _NPM["lock"]:
        if not force and _NPM["data"] and time.time() - _NPM["ts"] < NPM_TTL:
            # 套件查詢可以快取 10 分鐘，程序狀態不行：重啟後 PID 就變了，要每次即時重掃
            cached = dict(_NPM["data"])
            try:
                running = npm_running(cached.get("prefix"), [e["name"] for e in cached.get("packages", [])])
                cached["packages"] = [dict(e, running=running.get(e["name"], [])) for e in cached["packages"]]
            except Exception:
                pass
            return cached
    if not os.path.isfile(NPM_BIN):
        return {"ok": True, "available": False, "note": msg('npm_missing', LANG_DEFAULT)}
    out = {"ok": True, "available": True, "packages": [], "outdated": None, "error": None,
           "generated": datetime.now().isoformat(timespec="seconds"), "prefix": None,
           "node": (_run(["node", "--version"], timeout=10) or "").strip().lstrip("v") or None,
           "npm": (_run([NPM_BIN, "--version"], timeout=20) or "").strip() or None}
    out["node_info"] = node_release_info(out["node"])
    try:
        r = subprocess.run([NPM_BIN, "ls", "-g", "--depth=0", "--json"], capture_output=True, text=True, timeout=60, env=_ENV_C)
        deps = json.loads(r.stdout or "{}").get("dependencies") or {}
        pk = {n: {"name": n, "current": v.get("version"), "latest": None, "outdated": False} for n, v in deps.items()}
        out["prefix"] = (_run([NPM_BIN, "prefix", "-g"], timeout=20) or "").strip() or None
        # 描述、作者、原始碼倉庫從套件自己的 package.json 讀（不上網）。這些是套件自己宣稱的，npm 不驗證作者身分；
        # 前端只照實顯示並註明來源，判斷交給使用者。
        for n, e in pk.items():
            e.update(description=None, author=None, repo=None, homepage=None, license=None)
            try:
                with open(os.path.join(out["prefix"] or "", "lib", "node_modules", n, "package.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                a = meta.get("author"); a = a if isinstance(a, str) else (a or {}).get("name")
                r = meta.get("repository"); r = r if isinstance(r, str) else (r or {}).get("url")
                if r:
                    r = re.sub(r"^git\+|\.git$", "", r); r = re.sub(r"^(git|ssh)://", "https://", r); r = re.sub(r"^git@github\.com:", "https://github.com/", r)
                    if not r.startswith("http"):
                        r = "https://github.com/" + r   # npm 簡寫 "owner/repo"
                lic = meta.get("license"); lic = lic if isinstance(lic, str) else (lic or {}).get("type")
                e.update(description=(meta.get("description") or "")[:140] or None, author=(re.sub(r"\s*[<(].*$", "", a) if a else None),
                         repo=r, homepage=meta.get("homepage"), license=lic, engines_node=(meta.get("engines") or {}).get("node"))
            except Exception:
                pass
    except Exception as e:
        out.update(error=msg('npm_query_failed', LANG_DEFAULT, err=str(e)[:120]))
        return out
    try:
        r = subprocess.run([NPM_BIN, "outdated", "-g", "--json"], capture_output=True, text=True, timeout=90, env=_ENV_C)
        od = json.loads(r.stdout or "{}") if r.returncode in (0, 1) else None
        if od is None:
            raise RuntimeError((r.stderr or "").strip()[:120] or f"rc={r.returncode}")
        # wanted = npm 依目前 Node 版本（engines）算出的可裝最新版；dist-tag 的 latest 可能要求更新的 Node。
        # 更新時要裝 wanted 而不是 @latest：@latest 會硬裝不相容版（只印 EBADENGINE 警告），真機踩到：
        # agent-browser 0.38.1 要 Node ≥24，機器是 22。已裝版比 wanted 新就不是「有新版」，另標「較新」。
        for n, v in od.items():
            e = pk.setdefault(n, {"name": n, "current": v.get("current"), "latest": None, "outdated": False})
            want = v.get("wanted") or v.get("latest")
            e.update(latest=want, dist_latest=v.get("latest"), outdated=bool(want) and _semver_lt(v.get("current"), want),
                     newer=bool(want) and _semver_lt(want, v.get("current")))
        # npm 的選版只是「偏好」相容 engines 的版本：整個套件都不相容時仍會回不相容版，install 也只印警告。
        # 這裡對每個目標版查 engines.node，用 npm 附帶的 semver 核對目前 Node；不相容就標 blocked、不給更新鈕。
        # 「裝回相容版」（newer）的目標也要核對，不然前端給了鈕、後端不認。
        # npm view 失敗時退出碼非 0、stdout 是 {"error":{...}}：要和「成功查到但沒宣告 engines」分開，不能當成沒限制。
        checks = {}
        for e in pk.values():
            if e["outdated"] or e.get("newer"):
                try:
                    v = subprocess.run([NPM_BIN, "view", f"{e['name']}@{e['latest']}", "engines", "--json"], capture_output=True, text=True, timeout=30, env=_ENV_C)
                    body = json.loads(v.stdout) if v.stdout.strip() else {}
                    if v.returncode != 0 or (isinstance(body, dict) and body.get("error")):
                        raise RuntimeError(((body.get("error") or {}).get("code") if isinstance(body, dict) else None) or (v.stderr or "").strip()[:60] or f"rc={v.returncode}")
                    checks[e["name"]] = (body or {}).get("node")
                except Exception as ex:
                    checks[e["name"]] = f"?{str(ex)[:40]}"
        if any(str(r).startswith("?") for r in checks.values()):
            out["engines_partial"] = True   # 有查詢失敗的：這次結果不進快取，下次開頁再試
        if checks and out["node"]:
            sat = _node_semver_satisfies(out["node"], {n: r for n, r in checks.items() if r and not r.startswith("?")})
            for n, r in checks.items():
                e = pk[n]
                e["target_engines"] = r
                if r and r.startswith("?"):
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_unknown', LANG_DEFAULT))
                elif r and sat.get(n) is False:
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_blocked', LANG_DEFAULT, ver=e["latest"], need=r, node=out["node"]))
                elif r and sat.get(n) is None:
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_unknown', LANG_DEFAULT))
        out["outdated"] = sum(1 for e in pk.values() if e["outdated"])
    except Exception as e:
        # 查不到新版不能變成「全部最新」：outdated 留 None，前端顯示「—」並掛紅字
        out.update(error=msg('npm_outdated_failed', LANG_DEFAULT, err=str(e)[:120]))
    try:
        running = npm_running(out["prefix"], list(pk))
        for n, e in pk.items():
            e["running"] = running.get(n, [])
    except Exception:
        for e in pk.values():
            e["running"] = []
    out["packages"] = sorted(pk.values(), key=lambda e: (not e["outdated"], e["name"]))
    with _NPM["lock"]:
        if not out["error"] and not out.get("engines_partial"):
            _NPM.update(ts=time.time(), data=out)
    return out


# 認識的程式：更新前用它自己的備份指令、更新後跑它自己的升級／修復指令。只列我在真機驗證過的。
# 為什麼要有：OpenClaw 9.7 一啟動就把資料庫升到新格式，舊版打不開；「降回」救不了，只有備份和它自己的修復指令救得了。
NPM_KNOWN = {
    "openclaw": {"bin": "openclaw", "backup": ["backup", "create", "--verify", "--json", "--output"], "repair": ["doctor", "--fix", "--non-interactive"],
                 # 升級主程式後官方外掛（codex/discord/line…）會停在舊版（gateway status --deep 報 version drift）；
                 # 不自動 --accept-capabilities：外掛要更多權限時讓它失敗、留給使用者看。失敗不擋服務啟動。
                 "plugins": ["plugins", "update", "--all"],
                 "migrates_data": True, "docs": "https://docs.openclaw.ai/reference/database-schemas", "data_dir": "~/.openclaw",
                 "units": ["openclaw-gateway.service"]},
}
NPM_BACKUP_DIR = os.path.expanduser("~/.local/share/spark-center/npm-backups")
NPM_BACKUP_KEEP = 2
_NPM_JOB_OPTS = {"backup": True}


def _newest_file_size(folder, since):
    """folder 裡修改時間 ≥ since 的最新檔案大小（bytes）；沒有就 None。給沒有進度輸出的長指令當進度用。"""
    # 要遞迴找：OpenClaw 2026.9.x 把壓縮暫存檔寫在隱藏子資料夾 .openclaw-backup-publish-<id>/archive.tar.gz.tmp，
    # 只看第一層會一直拿到 None，卡片停在 0% 幾分鐘、看起來像當掉（2026-10-14 真機）。
    try:
        cands = []
        for root, _dirs, files in os.walk(folder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.getmtime(fp) >= since - 1:
                        cands.append(fp)
                except OSError:
                    continue
        if not cands:
            return None
        return os.path.getsize(max(cands, key=os.path.getmtime))
    except OSError:
        return None


def _npm_known_bin(name):
    k = NPM_KNOWN.get(name)
    prefix = (npm_status().get("prefix") or "").strip()
    if not k or not prefix:
        return None
    p = os.path.join(prefix, "bin", k["bin"])
    return p if os.path.exists(p) else None


def npm_backups(name):
    d = os.path.join(NPM_BACKUP_DIR, name)
    try:
        return sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".tar.gz")), key=os.path.getmtime, reverse=True)
    except OSError:
        return []


def npm_plan(names, rollback=False, targets=None):
    """更新（或降回）前要給使用者看的：會停哪些服務、認不認識這個程式、能不能備份、會不會升資料格式。"""
    st = npm_status()
    by = {e["name"]: e for e in st.get("packages", [])}
    items = []
    for n in names:
        e = by.get(n, {}); k = NPM_KNOWN.get(n)
        units = sorted({r["unit"] for r in e.get("running", []) if r.get("unit")})
        loose = [r["pid"] for r in e.get("running", []) if not r.get("unit")]
        items.append({"name": n, "current": e.get("current"), "target": (targets or {}).get(n) or e.get("latest"), "units": units, "loose_pids": loose,
                      "known": bool(k), "can_backup": bool(k and _npm_known_bin(n)), "migrates_data": bool(k and k.get("migrates_data")),
                      "data_dir": (k or {}).get("data_dir"), "docs": (k or {}).get("docs"), "backups": npm_backups(n)[:1]})
    return {"ok": True, "items": items, "rollback": rollback}


def _npm_units_for(names):
    """這個套件的 systemd 使用者服務：正在跑的、認識的程式列出的、以及 ExecStart 指到這個套件的（含已 failed 的）。
    真機 2026-09-30：降回失敗後 gateway 是 failed、沒在跑，只看「正在跑的」會找不到，救回時就不會啟動它。"""
    st = npm_status()
    units = {r["unit"] for e in st.get("packages", []) if e["name"] in names for r in e.get("running", []) if r.get("unit")}
    for n in names:
        units.update((NPM_KNOWN.get(n) or {}).get("units") or [])
    prefix = (st.get("prefix") or "").strip()
    if prefix:
        listing = _run(["systemctl", "--user", "list-units", "--type=service", "--all", "--plain", "--no-legend"], timeout=15) or ""
        for line in listing.splitlines():
            u = line.split()[0] if line.split() else ""
            if not u.endswith(".service") or u in units:
                continue
            exe = _run(["systemctl", "--user", "show", u, "-p", "ExecStart", "--value"], timeout=10) or ""
            if any(f"/node_modules/{n}/" in exe or f"/bin/{(NPM_KNOWN.get(n) or {}).get('bin', n)} " in exe + " " for n in names):
                units.add(u)
    return sorted(units)


def _npm_explain(journal, name):
    """把服務起不來的日誌翻成一句人話，並決定給哪些按鈕。認不出來就是通用說法。"""
    k = NPM_KNOWN.get(name) or {}
    low = (journal or "").lower()
    if "schema version" in low or "cannot open your existing data" in low:
        return "npm_reason_schema_new" if "newer schema" in low or "this build supports" in low else "npm_reason_schema", k
    if "older than the config" in low or "config was written by version" in low or "written by a newer version" in low:
        return "npm_reason_schema_new", k   # 真機 2026-09-30：9.6 拒絕啟動，因為 config 是 9.7 寫的。救法同 schema：回新版
    if "cannot find module" in low or "err_module_not_found" in low:
        return "npm_reason_halfloaded", k
    if "eaddrinuse" in low or "address already in use" in low:
        return "npm_reason_port", k
    return "npm_reason_unknown", k


def _unit_active(unit):
    """is-active 不是 active 時 rc 非零，_run 會回 None；這裡要的是字面狀態，直接讀 stdout。"""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _notify(title, body):
    """桌面通知：出事時使用者多半不在看這個視窗。沒有桌面就算了。"""
    try:
        env = _session_env()
        if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["notify-send", "-a", "Spark Center", "-u", "critical", title, body], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def rollback_record_npm(names):
    """npm 更新前把「目前版 → 新版」記進降回索引（不留檔案：registry 保留所有版本，降回就是 npm install -g name@old）。"""
    st = npm_status()
    cur = {e["name"]: e for e in st.get("packages", [])}
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    rec = {"id": job_id, "started": datetime.now().isoformat(timespec="seconds"), "packages": []}
    for n in names:
        e = cur.get(n) or {}
        rec["packages"].append({"name": n, "kind": "npm", "selected": True, "old": e.get("current"), "new": e.get("latest"),
                                "arch": None, "deb": None, "source": "npm", "verified": "registry",
                                "reason": None if e.get("current") else msg("rollback_not_installed", LANG_DEFAULT)})
    _rollback_index_add(rec)
    return rec


def rollback_npm_entries(job_id, name=None):
    for j in _rollback_index_load():
        if j.get("id") == job_id:
            return [p for p in j.get("packages", []) if p.get("kind") == "npm" and p.get("old") and (name is None or p.get("name") == name)]
    return []


_NODE_FLOW = {"ts": 0, "phase": None}


def _node_flow_pending():
    """Node 主版本升級進行到一半（已換倉庫、還沒裝或裝失敗）時回 phase，否則 None。
    這時 apt 清單也看得到 nodejs 24：從清單裝會讓升級流程的狀態對不上（真的發生過），所以清單裡的 nodejs 鎖住，只留一條路。
    判定交給 helper 的 effective_state（和面板、下一次升級的放行是同一個邏輯），結果快取 20 秒。"""
    if time.time() - _NODE_FLOW["ts"] < 20:
        return _NODE_FLOW["phase"]
    ph = None
    try:
        with open('/var/lib/spark-center/node-source/state.json', encoding='utf-8') as f:
            raw = json.load(f).get('phase')
        if raw in ('preparing', 'prepared', 'installing', 'install_failed'):
            j = node_read('status')
            st = (j.get('state') or {}) if j.get('ok') else {'phase': raw}
            ph = st.get('phase') if st.get('phase') in ('preparing', 'prepared', 'installing', 'install_failed') else None
    except (OSError, ValueError):
        ph = None
    _NODE_FLOW.update(ts=time.time(), phase=ph)
    return ph


def list_updates():
    cache = apt.Cache()
    node_flow = _node_flow_pending()
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
            "origin_raw": orig["origin"] or orig["label"] or "",
            "archive": orig["archive"],
            "size": cand.size,
            "summary": cand.summary or "",
            "security": "security" in (orig["archive"] or "").lower(),
            "reboot_hint": bool(REBOOT_HINT_RE.match(pkg.name)),
            "spark_core": bool(SPARK_CORE_RE.match(pkg.name)),
            "firmware": _firmware_usage(pkg, cache),
            "node_flow": node_flow if pkg.name == "nodejs" and node_flow else None,
            "phased": phased_info(pkg),
        })
    items.sort(key=lambda x: (x["group"], x["name"]))
    return items


def simulate(names):
    """在記憶體裡標記升級，回傳實際會被動到的套件。不動系統。"""
    cache = apt.Cache()
    unknown = [n for n in names if n not in cache]
    if unknown:
        return {"ok": False, "error": msg('pkgs_missing', LANG_DEFAULT, p0=', '.join(unknown))}
    # 分階段判定要在標記前讀：mark_upgrade 不看它，但 phasing_applied 是政策層的屬性，標記後仍可讀；提前讀是為了保險
    held_before = {p.name: phased_info(p) for p in cache if p.is_upgradable and getattr(p, "phasing_applied", False)}
    try:
        with cache.actiongroup():
            for n in names:
                cache[n].mark_upgrade()
    except Exception as e:  # 相依性解不開
        return {"ok": False, "error": msg('deps_failed', LANG_DEFAULT, p0=e)}
    changes = []
    phased = []   # 會被這次交易裝進去、但 Ubuntu 還沒對這台機器放量的（含相依帶入的）
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
            "phased": held_before.get(p.name),
        })
        if p.name in held_before and action in ("upgrade", "install"):
            phased.append({"name": p.name, "to": p.candidate.version if p.candidate else "", "percent": held_before[p.name]["percent"]})
    changes.sort(key=lambda c: (not c["requested"], c["action"], c["name"]))
    upgradable = [p.name for p in apt.Cache() if p.is_upgradable]
    return {
        "ok": True,
        "changes": changes,
        "pairing": kernel_pairing_warning(names, upgradable),
        "phased": phased,
        "download_bytes": cache.required_download,
        "space_bytes": cache.required_space,
        "broken": cache.broken_count,
    }


# ---------- 登入時自動開啟（XDG autostart） ----------

DGX_DASHBOARD_ICON = "/usr/share/icons/hicolor/scalable/apps/nvidia-dgx-dashboard.png"   # dgx-dashboard 套件的圖示
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/spark-center.desktop")
DESKTOP_INSTALLED = os.path.expanduser("~/.local/share/applications/spark-center.desktop")


def autostart_status():
    """真相只有一個：autostart 目錄裡有沒有這個 .desktop 檔（且沒被 Hidden 關掉）。"""
    enabled = False
    if os.path.isfile(AUTOSTART_FILE):
        try:
            txt = open(AUTOSTART_FILE, encoding="utf-8").read()
            enabled = not re.search(r"^Hidden=true$", txt, re.M) and not re.search(r"^X-GNOME-Autostart-enabled=false$", txt, re.M)
        except OSError:
            pass
    return {"ok": True, "enabled": enabled, "path": AUTOSTART_FILE}


def autostart_set(enabled):
    if not enabled:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return autostart_status()
    # 以 install.sh 產生的桌面捷徑為準（Exec 已填好實際路徑）；沒有就用模板現填
    try:
        txt = open(DESKTOP_INSTALLED, encoding="utf-8").read()
    except OSError:
        txt = open(os.path.join(HERE, "app", "spark-center.desktop.in"), encoding="utf-8").read().replace("@ROOT@", HERE).replace("@PORT@", str(PORT))
    txt = re.sub(r"^(Hidden|X-GNOME-Autostart-enabled|X-GNOME-Autostart-Delay)=.*\n?", "", txt, flags=re.M).rstrip("\n")
    txt += "\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n"   # 等桌面就緒再開，視窗才會在正確位置
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    return autostart_status()


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
            "xfer": None,          # apt 下載明細：{"done","total","speed","eta","items","items_total"}，aptdaemon progress-details
            "phase": None,         # apt 更新的階段：prepare（留舊版）| wait（鎖、解相依）| download | install | finish；其他工作為 None
            "step": None,          # 目前階段的 [已完成, 總數]；拿不到就 None（前端顯示不定進度條，不假裝百分比）
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
                self.state["status_text"] = msg('running', LANG_DEFAULT)
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
                    hint = {126: msg('auth_cancelled', LANG_DEFAULT), 127: msg('command_missing', LANG_DEFAULT),
                            10: msg('snap_busy', LANG_DEFAULT)}.get(rc)
                    self.state["error"] = (hint or msg('exit_code', LANG_DEFAULT, p0=rc)) + msg('see_log', LANG_DEFAULT)
                    if self.state.get("kind") == "rollback" and rc not in (126, 127):
                        # 降版中途失敗可能留下 unpacked／half-configured 的套件。dpkg --audit 不需 root，查得到就明講該怎麼救；
                        # 修復本身要再一次密碼，這裡不自動做，交給使用者在終端機跑。
                        audit = (_run(["dpkg", "--audit"], timeout=30) or "").strip()
                        self.state["error"] += msg('rollback_left_broken' if audit else 'rollback_state_intact', LANG_DEFAULT)
                self.state["status_text"] = msg('done', LANG_DEFAULT) if rc == 0 else msg('failed', LANG_DEFAULT)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(e)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
            self._log("EXCEPTION " + str(e))
        finally:
            _APPS_CACHE["ts"] = 0  # 讓應用程式清單重新掃描
            _FW["ts"] = 0
            _DASH_CACHE["ts"] = 0
            _NPM["ts"] = 0
            _NODE_FLOW["ts"] = 0

    _npm_last_units = {}

    def _npm_transaction(self, names, cmd, backup, repair, units=None):
        """有服務在跑的套件，正確順序：停服務 → （認識的）備份 → 裝 → （認識的）修復／遷移 → 啟動 → 確認活著。
        真機事故：跳過這些步驟直接裝，OpenClaw 9.7 在跑到一半時自己重啟、又沒做資料遷移，停了 8 分鐘。"""
        units = _npm_units_for(names) if units is None else units
        for n in names:
            self._npm_last_units[n] = units
        with self.lock:
            self.state["steps"] = []
        self._npm_stop(units)
        for n in names:
            if backup and n in NPM_KNOWN:
                if not self._npm_backup(n):
                    self._log(msg("npm_backup_failed_abort", LANG_DEFAULT, name=n))
                    self._npm_start_and_check(units, n)
                    with self.lock:
                        self.state.update(status="error", error=msg("npm_backup_failed_abort", LANG_DEFAULT, name=n))
                    return
        self._step("install")
        self._run_npm(cmd, names)
        with self.lock:
            ok = self.state.get("status") == "done"
            if ok:   # 裝完不算完：後面還有修復／啟動／確認，前端看到 done 會停止輪詢
                self.state.update(status="running", finished=None)
        if not ok:
            # 安裝失敗：把服務用原本的檔案拉起來，但結果仍是失敗（不讓「啟動成功」蓋掉安裝錯誤）
            with self.lock:
                err = self.state.get("error")
            self._npm_start_and_check(units, names[0])
            with self.lock:
                self.state.update(status="error", error=err or msg("failed", LANG_DEFAULT))
            return
        if repair:
            for n in names:
                if n in NPM_KNOWN and not self._npm_repair(n):
                    return self._npm_fail_units(units, n)
            for n in names:
                if n in NPM_KNOWN:
                    self._npm_plugins(n)
        return self._npm_start_and_check(units, names[0])

    def _step(self, step, **kw):
        with self.lock:
            self.state.setdefault("steps", []).append({"step": step, **kw})

    def _npm_stop(self, units):
        for u in units:
            self._step("stop", unit=u); self._log(msg("npm_step_stop", LANG_DEFAULT, unit=u))
            subprocess.run(["systemctl", "--user", "stop", u], capture_output=True, timeout=120)

    def _npm_backup(self, name):   # 備份進度看檔案大小：_newest_file_size
        k = NPM_KNOWN[name]; b = _npm_known_bin(name)
        if not b:
            self._log(msg("npm_backup_skipped", LANG_DEFAULT, name=name)); return True
        d = os.path.join(NPM_BACKUP_DIR, name); os.makedirs(d, exist_ok=True)
        self._step("backup", name=name); self._log(msg("npm_step_backup", LANG_DEFAULT, name=name))
        with self.lock:
            self.state["status_text"] = msg("npm_step_backup", LANG_DEFAULT, name=name)
        # openclaw backup 沒有進度輸出，幾 GB 的壓縮＋校驗會讓卡片停在 0% 幾分鐘像當掉。
        # 做法：備份指令在另一條執行緒跑（保持 subprocess.run，隔離測試才 mock 得到），這裡每 2 秒看輸出目錄裡
        # 這次新建的檔案長到多大；大小停住就是在校驗（--verify 會整個讀回來），拿不到檔案就什麼都不說。
        box = {}
        def _run_backup():
            try:
                box["r"] = subprocess.run([b] + k["backup"] + [d], capture_output=True, text=True, timeout=1800, env=_session_env())
            except Exception as e:
                box["e"] = e
        th = threading.Thread(target=_run_backup, daemon=True); since = time.time(); th.start()
        last = None
        while th.is_alive():
            th.join(2)
            cur = _newest_file_size(d, since)
            if cur is None:
                continue
            key = "npm_backup_verifying" if last is not None and cur == last else "npm_backup_writing"
            with self.lock:
                self.state["details"] = msg(key, LANG_DEFAULT, gb=f"{cur / 1e9:.1f}")
            last = cur
        with self.lock:
            self.state["details"] = ""
        if "e" in box:
            self._log("backup: " + str(box["e"])[:200]); return False
        r = box["r"]
        path = None
        try:
            path = json.loads(r.stdout).get("archivePath")
        except ValueError:
            pass
        if r.returncode != 0 or not path or not os.path.isfile(path):
            self._log((r.stderr or r.stdout).strip()[-600:]); return False
        size = os.path.getsize(path)
        self._log(msg("npm_backup_done", LANG_DEFAULT, path=path, mb=round(size / 1048576)))
        with self.lock:
            self.state["backup"] = {"name": name, "path": path, "bytes": size}
        for old in npm_backups(name)[NPM_BACKUP_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return True

    def _npm_repair(self, name):
        k = NPM_KNOWN[name]; b = _npm_known_bin(name)
        if not b:
            return True
        self._step("repair", name=name); self._log(msg("npm_step_repair", LANG_DEFAULT, name=name, cmd=" ".join([k["bin"]] + k["repair"])))
        with self.lock:
            self.state["status_text"] = msg("npm_step_repair", LANG_DEFAULT, name=name, cmd=" ".join([k["bin"]] + k["repair"]))
        try:
            r = subprocess.run([b] + k["repair"], capture_output=True, text=True, timeout=900, env=_session_env())
        except Exception as e:
            self._log("repair: " + str(e)[:200]); return False
        for line in (r.stdout + "\n" + r.stderr).splitlines():
            t = re.sub(r"[│◇└├─]+", "", line).strip()
            if t and re.search(r"schema|migrat|upgrade|repair|fixed|error|fail|complete", t, re.I):
                self._log("  " + t[:200])
        return r.returncode == 0

    def _npm_plugins(self, name):
        """主程式升級後把它的外掛更新到同版本。失敗只記錄、不擋啟動（外掛漂移不會讓服務起不來）。"""
        k = NPM_KNOWN[name]; b = _npm_known_bin(name)
        if not b or not k.get("plugins"):
            return True
        cmd = " ".join([k["bin"]] + k["plugins"])
        self._step("plugins", name=name); self._log(msg("npm_step_plugins", LANG_DEFAULT, name=name, cmd=cmd))
        with self.lock:
            self.state["status_text"] = msg("npm_step_plugins", LANG_DEFAULT, name=name, cmd=cmd)
        try:
            r = subprocess.run([b] + k["plugins"], capture_output=True, text=True, timeout=900, env=_session_env())
        except Exception as e:
            self._log(msg("npm_plugins_failed", LANG_DEFAULT, name=name, cmd=cmd)); self._log("  " + str(e)[:200]); return False
        for line in (r.stdout + "\n" + r.stderr).splitlines():
            t = line.strip()
            if t and re.search(r"updated|up to date|skipping|error|fail|block|capabilit", t, re.I):
                self._log("  " + t[:200])
        if r.returncode != 0:
            self._log(msg("npm_plugins_failed", LANG_DEFAULT, name=name, cmd=cmd))
        return r.returncode == 0

    def _npm_start_and_check(self, units, name):
        """啟動服務，等它真的活著（活過 6 秒）。有 Restart= 的服務最多等 60 秒讓 systemd 重試：
        2026-10-14 真機 OpenClaw 前兩次啟動因舊程序殘留而退出、第 3 次才起來，舊邏輯看到一次 failed 就判敗（假失敗）。
        失敗：翻成人話、決定按鈕、桌面通知。"""
        for u in units:
            self._step("start", unit=u); self._log(msg("npm_step_start", LANG_DEFAULT, unit=u))
            subprocess.run(["systemctl", "--user", "reset-failed", u], capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "start", u], capture_output=True, timeout=120)
        dead = []
        for u in units:
            state = "unknown"; retrying = False
            restarts = (_run(["systemctl", "--user", "show", "-p", "Restart", "--value", u], timeout=10) or "").strip() not in ("", "no")
            for _ in range(30):   # 30 × 2 秒 = 最多 60 秒（用次數不用時鐘：測試會把 sleep 換成空操作）
                time.sleep(2)
                state = _unit_active(u)
                if state == "active":
                    time.sleep(6)   # 活著不算，要活過幾秒：OpenClaw 是啟動幾秒後才因 schema 退出
                    state = _unit_active(u)
                    if state == "active":
                        break
                    continue
                if state in ("failed", "inactive"):
                    if not restarts:
                        break
                    tail = _run(["journalctl", "--user", "-u", u, "-n", "12", "--no-pager", "-o", "cat"], timeout=15) or ""
                    if _npm_explain(tail, name)[0] in ("npm_reason_schema", "npm_reason_schema_new"):
                        break   # 資料格式不合是確定的，重試幾次都一樣，不用空等一分鐘
                    if not retrying:   # systemd 會自己再拉（Restart=），別急著判敗
                        retrying = True; self._log(msg("npm_unit_retrying", LANG_DEFAULT, unit=u))
            if state != "active":
                dead.append((u, state))
        _NPM["ts"] = 0
        if not dead:
            for u in units:
                self._log(msg("npm_unit_ok", LANG_DEFAULT, unit=u))
            with self.lock:
                self.state.update(status="done", exit=self.state.get("exit") or "rc=0", status_text=msg("done", LANG_DEFAULT),
                                  finished=datetime.now().isoformat(timespec="seconds"))
            return True
        self._npm_fail_units([u for u, _ in dead], name, {u: s for u, s in dead})
        return False

    def _npm_fail_units(self, units, name, states=None):
        tails = []
        for u in units:
            tail = (_run(["journalctl", "--user", "-u", u, "-n", "12", "--no-pager", "-o", "cat"], timeout=15) or "").strip()
            tails.append(tail)
            self._log(msg("npm_unit_failed", LANG_DEFAULT, unit=u, state=(states or {}).get(u, "failed")))
            for line in tail.splitlines()[-8:]:
                self._log("  " + line[:240])
        reason, k = _npm_explain("\n".join(tails), name)
        with self.lock:
            fwd = (self.state.get("forward") or {}).get(name)
            actions = []
            if reason == "npm_reason_schema_new" and fwd:
                actions.append({"id": "forward", "name": name, "version": fwd})
            if k.get("repair") and reason != "npm_reason_schema_new":
                actions.append({"id": "repair", "name": name})
            actions.append({"id": "restart", "units": units})
            b = self.state.get("backup") or (npm_backups(name)[:1] and {"path": npm_backups(name)[0]}) or None
            if b:
                actions.append({"id": "backup_info", "name": name, "path": b["path"], "docs": k.get("docs")})
            self.state.update(status="error", reason=reason, reason_name=name, actions=actions,
                              error=msg("npm_unit_failed_summary", LANG_DEFAULT, units=", ".join(units)),
                              finished=datetime.now().isoformat(timespec="seconds"))
        _notify(msg("npm_notify_title", LANG_DEFAULT, name=name), msg("npm_notify_body", LANG_DEFAULT))

    def _run_npm(self, cmd, packages):
        """npm 11 起預設不跑套件的安裝腳本，只印 "install scripts not yet covered by allowScripts" 並建議 --allow-scripts=<清單>。
        使用者明確選了要更新這些套件，等同 npm 10 以前的行為：照 npm 自己列出的清單允許一次、重跑同一個安裝（有快取，幾秒）。
        清單只取 npm 輸出裡的那一行，不自己擴大。"""
        self._run_subprocess(cmd, packages)
        with self.lock:
            ok = self.state.get("status") == "done"
            text = "\n".join(self.state.get("log") or [])
        m = re.search(r"--allow-scripts=([@A-Za-z0-9._/,-]+)", text) if ok and "install-scripts" in text else None
        if m:
            with self.lock:
                self.state.update(status="running", finished=None)   # 第一次裝完不算完，別讓前端在這裡停止輪詢
            self._log(msg("npm_allow_scripts", LANG_DEFAULT, pkgs=m.group(1)))
            self._run_subprocess(cmd[:3] + [f"--allow-scripts={m.group(1)}"] + cmd[3:], packages)

    def _run_ollama_pull(self, model):
        req = urllib.request.Request(OLLAMA + "/api/pull", data=json.dumps({"name": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
        self._log(f"ollama pull {model}")
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                last = ""
                for line in r:
                    try:
                        j = json.loads(line.decode())
                    except ValueError:
                        continue
                    st = j.get("status", "")
                    tot, done = j.get("total"), j.get("completed")
                    with self.lock:
                        self.state["status_text"] = st
                        self.state["progress"] = int(done / tot * 100) if tot and done is not None else None
                        self.state["details"] = f"{st} {GB_(done)}/{GB_(tot)}" if tot else st
                    if st != last:
                        self._log(st); last = st
                    if j.get("error"):
                        raise RuntimeError(j["error"])
            with self.lock:
                self.state.update(status="done", exit="ok", status_text=msg('done', LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            with self.lock:
                self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
            self._log("ERROR " + str(e))
        finally:
            disk.invalidate()

    def _snap_rc_hint(self, rc):
        last = " ".join(self.state.get("log", [])[-3:])
        if "has running apps" in last:
            return msg('snap_running', LANG_DEFAULT)
        return {10: msg('snap_busy', LANG_DEFAULT),
                126: msg('auth_cancelled', LANG_DEFAULT), 127: msg('command_missing', LANG_DEFAULT)}.get(rc, msg('exit_code', LANG_DEFAULT, p0=rc))

    def _run_snap(self, names):
        """snap 更新。三個在真機上踩到的坑：
        1) snap CLI 不會自己觸發 snapd 的 polkit，非 root 直接回 access denied → 用 pkexec。
        2) `snap refresh` 的進度是 \r 覆寫的單行，逐行讀會看起來卡住 → 改用 `snap tasks` 讀真進度。
        3) 工作狀態只存在記憶體，服務一重啟就失去追蹤，而 snapd 那邊還在跑 → 先找進行中的變更並接上。"""
        cid = snap_change_for(names)
        proc = None
        if cid:
            self._log(msg('snap_attach', LANG_DEFAULT, p0=cid))
        else:
            self._log(msg('snap_auth', LANG_DEFAULT))
            cmd = ["pkexec", "/usr/bin/snap", "refresh"] + names
            self._log("$ " + " ".join(cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, env=_ENV_C, bufsize=1)
            except OSError as e:
                with self.lock:
                    self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
                return
            def drain():
                for line in proc.stdout:
                    t = line.rstrip("\r\n").strip()
                    if t and not re.fullmatch(r"[\s\u2588\u2591\-\\|/]*", t):
                        self._log(t)
            threading.Thread(target=drain, daemon=True).start()
        try:
            while True:
                if cid is None and proc is not None:
                    cid = snap_change_for(names)
                if cid:
                    pr = snap_change_progress(cid)
                    with self.lock:
                        self.state["progress"] = pr["percent"]
                        self.state["status_text"] = pr["doing"] or pr["status"]
                        mb = lambda b: f"{b / 1e6:.1f} MB"   # snapd 自己用 10^6，跟著一致
                        size = (f" · {mb(pr['bytes_done'])} / {mb(pr['bytes_total'])}"
                                if pr.get("bytes_total") else "")
                        step = min(pr["done"] + 1, pr["total"]) if pr["total"] else pr["done"]
                        self.state["details"] = (msg('snap_step', LANG_DEFAULT, p0=step, p1=pr['total']) + size
                                                 + (msg('snap_step_percent', LANG_DEFAULT, p0=pr['task_percent']) if pr["task_percent"] is not None else "")
                                                 + (msg('snap_overall', LANG_DEFAULT, p0=pr['task_ratio']) if pr["task_ratio"] is not None else ""))
                    if pr["status"] in ("Done", "Error", "Undone", "Hold"):
                        ok = pr["status"] == "Done"
                        reason = (pr.get("err") or "").strip()
                        if not ok:
                            for l in pr.get("fail_log") or []:
                                self._log(l[:300])
                            blob = (reason + " " + " ".join(pr.get("fail_log") or [])).lower()
                            if any(k in blob for k in ("unexpected eof", "connection", "timeout", "temporary failure", "i/o timeout")):
                                reason = (reason or msg('download_interrupted', LANG_DEFAULT)) + msg('network_retry', LANG_DEFAULT)
                        with self.lock:
                            self.state.update(status="done" if ok else "error", exit=pr["status"],
                                              error=None if ok else (reason or msg('snap_ended', LANG_DEFAULT, p0=cid, p1=pr['status'])),
                                              status_text=msg('done', LANG_DEFAULT) if ok else msg('failed', LANG_DEFAULT),
                                              finished=datetime.now().isoformat(timespec="seconds"))
                        self._log(msg('snap_status', LANG_DEFAULT, p0=cid, p1=pr['status']))
                        return
                if proc is not None and proc.poll() is not None and cid is None:
                    rc = proc.returncode
                    with self.lock:
                        self.state.update(status="done" if rc == 0 else "error", exit=f"rc={rc}",
                                          error=None if rc == 0 else (self._snap_rc_hint(rc) + msg('see_log', LANG_DEFAULT)),
                                          status_text=msg('done', LANG_DEFAULT) if rc == 0 else msg('failed', LANG_DEFAULT),
                                          finished=datetime.now().isoformat(timespec="seconds"))
                    return
                time.sleep(2)
        except Exception as e:
            with self.lock:
                self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
        finally:
            _APPS_CACHE["ts"] = 0

    def _run(self, kind, packages):
        if kind == "ollama_pull":
            return self._run_ollama_pull(packages[0])
        if kind == "shell":
            # packages[0] 是指令名（見 DISK_ACTIONS 白名單），不接受任意指令
            cmd = DISK_ACTIONS.get(packages[0]) if packages else None
            if not cmd:
                with self.lock:
                    self.state.update(status="error", error=msg('unknown_action', LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            return self._run_subprocess(cmd, packages)
        if kind == "flatpak":
            return self._run_subprocess(["flatpak", "update", "-y", "--noninteractive"] + packages, packages)
        if kind == "snap":
            return self._run_snap(packages)
        if kind == "install":   # 升級前先把舊版留下來；失敗不影響升級
            try:
                with self.lock:
                    self.state.update(phase="prepare", step=None, progress=None,
                                      status_text=msg("rollback_preparing", LANG_DEFAULT, name=", ".join(packages), old=""))
                def _step(i, n, name):
                    with self.lock:
                        self.state.update(step=[i, n], details=name, progress=int(i / n * 100) if n else None)
                def _progress(text):
                    with self.lock:
                        self.state["details"] = text
                rollback_prepare(packages, self._log, lambda txt: self.state.__setitem__("status_text", txt), _step, _progress)
            except Exception as e:
                self._log("rollback prepare failed: " + str(e))
            with self.lock:   # 留舊版結束；接下來的階段由 aptdaemon 的狀態決定，進度歸零不沿用
                self.state.update(phase="wait", step=None, details="", progress=None)
        if kind == 'node_source':
            action, target, token = packages
            if not node_helper_ready():
                with self.lock:
                    self.state.update(status='error', error=msg('node_helper_unavailable', LANG_DEFAULT), finished=datetime.now().isoformat(timespec='seconds'))
                return
            # 直接執行 helper（shebang 是 python3 -I）而不是 pkexec python3：polkit 的 action 用 exec.path 對應程式路徑，
            # 這樣密碼視窗才會顯示 policy 檔裡「Spark Center 要切換 NodeSource 倉庫…」，而不是「要以 root 執行 python3」。
            self._run_subprocess(['pkexec', NODE_HELPER, action, target, token], packages)
            with self.lock:
                if self.state['status'] == 'error':
                    detail = next((s.split('NODE_ERROR:', 1)[1] for s in reversed(self.state['log']) if 'NODE_ERROR:' in s), None)
                    if detail:
                        self.state['error'] = node_error(detail)
            return
        if kind == "npm":
            try:
                rollback_record_npm(packages)
            except Exception as e:
                self._log("rollback record failed: " + str(e))
            st = npm_status()
            # outdated（升級）與 newer（裝回相容版）都是經過 engines 核對的明確版本；blocked 的不在裡面
            want = {e["name"]: e.get("latest") for e in st.get("packages", []) if (e.get("outdated") or e.get("newer")) and not e.get("blocked")}
            missing = [n for n in packages if not want.get(n)]
            if st.get("error") or missing:
                # 目標版本查不到就停：退回 @latest 會裝到沒確認過、可能不相容的版本
                with self.lock:
                    self.state.update(status="error", error=msg("npm_target_unknown", LANG_DEFAULT, pkgs=", ".join(missing or packages), err=st.get("error") or ""),
                                      finished=datetime.now().isoformat(timespec="seconds"))
                return
            # --engine-strict：npm 對 engines 不符預設只印警告照裝，這裡要它直接失敗
            return self._npm_transaction(packages, [NPM_BIN, "install", "-g", "--engine-strict"] + [f"{n}@{want[n]}" for n in packages],
                                         backup=_NPM_JOB_OPTS.get("backup", True), repair=True)
        if kind == "rollback_npm":
            ents = rollback_npm_entries(packages[0], packages[1] or None)
            if not ents:
                with self.lock:
                    self.state.update(status="error", error=msg("rollback_not_found", LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            names = [e["name"] for e in ents]
            with self.lock:
                self.state["forward"] = {e["name"]: e.get("new") for e in ents if e.get("new")}   # 降回失敗時「回到新版並修復」要用
            # 降回不跑 repair：舊版的修復指令不會把資料格式降回去
            return self._npm_transaction(names, [NPM_BIN, "install", "-g"] + [f"{e['name']}@{e['old']}" for e in ents], backup=_NPM_JOB_OPTS.get("backup", True), repair=False)
        if kind == "npm_repair":   # 「讓它自己修」：跑程式自己的修復指令，再啟動服務
            name = packages[0]
            units = _npm_units_for([name]) or self._npm_last_units.get(name, [])
            self._npm_stop(units)
            if not self._npm_repair(name):
                return self._npm_fail_units(units, name)
            return self._npm_start_and_check(units, name)
        if kind == "npm_forward":   # 降回失敗後：裝回新版、跑修復、啟動
            name, ver = packages[0], packages[1]
            units = _npm_units_for([name]) or self._npm_last_units.get(name, [])
            return self._npm_transaction([name], [NPM_BIN, "install", "-g"] + [f"{name}@{ver}"], backup=False, repair=True, units=units)
        if kind == "rollback":
            # 為什麼不用 aptdaemon 的 install_file：它最後跑 DebPackage.check()，預設拒絕比已安裝舊的版本
            # （"A later version is already installed"），force=True 也一樣。apt-get 對本機 .deb 會照給的版本裝，
            # 加 --allow-downgrades 明講意圖，相依由 apt 解；一次給整組檔案，舊主程式要求舊函式庫時才有完整退路。
            paths = [fp for fp, _ in rollback_deb_paths(packages[0], packages[1] or None)]
            if not paths:
                with self.lock:
                    self.state.update(status="error", error=msg("rollback_not_found", LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            self._log(msg("rollback_auth", LANG_DEFAULT))
            return self._run_subprocess(["pkexec"] + ROLLBACK_APT + paths, packages)
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

            PHASE_OF = {aenums.STATUS_DOWNLOADING: "download", aenums.STATUS_DOWNLOADING_REPO: "download",
                        aenums.STATUS_COMMITTING: "install", aenums.STATUS_SETTING_UP: "install", aenums.STATUS_CLEANING_UP: "install",
                        aenums.STATUS_FINISHED: "finish"}
            def on_status(t, status):
                text = aenums.get_status_string_from_enum(status)
                with self.lock:
                    self.state["status_text"] = text
                    if kind == "install":   # 只有 install 走三段式；refresh／remove 的卡片維持單行
                        self.state["phase"] = PHASE_OF.get(status, "wait")
                        if self.state["phase"] != "download":
                            self.state["step"] = None
                self._log(text)

            def on_details(t, d):
                with self.lock:
                    self.state["details"] = d

            def on_progress(t, p):
                # aptdaemon 用 101 代表「進度未知」，不要當成百分比顯示
                with self.lock:
                    self.state["progress"] = int(p) if p <= 100 else None

            def on_progress_details(t, items, items_total, done, total, speed, eta):
                # aptdaemon 的百分比不是照位元組線性走（下載大約只占前半），幾百 MB 只看百分比會像卡住；
                # 這裡把位元組與速率直接端出來。下載結束後 total 會歸 0，就清掉。
                with self.lock:
                    self.state["xfer"] = ({"done": int(done), "total": int(total), "speed": int(speed), "eta": int(eta),
                                           "items": int(items), "items_total": int(items_total)} if total > 0 else None)
                    if kind == "install" and self.state["phase"] == "download" and items_total > 0:
                        self.state["step"] = [int(items), int(items_total)]

            def on_error(t, code, details):
                msg = f"{aenums.get_error_string_from_enum(code)}: {details}"
                with self.lock:
                    self.state["error"] = msg
                self._log("ERROR " + msg)

            def on_conffile(t, old, new):
                # 設定檔衝突一律保留現有版本，不互動、不猜。
                self._log(msg('config_conflict', LANG_DEFAULT, p0=old))
                t.resolve_config_file_conflict(old, "keep")

            def on_finished(t, exit_state):
                with self.lock:
                    self.state["exit"] = exit_state
                    self.state["status"] = (
                        "done" if exit_state == aenums.EXIT_SUCCESS else "error"
                    )
                    if self.state["status"] == "error" and not self.state["error"]:
                        self.state["error"] = msg('transaction_exit', LANG_DEFAULT, p0=exit_state)
                    self.state["finished"] = datetime.now().isoformat(timespec="seconds")
                self._log(f"finished: {exit_state}")
                _APPS_CACHE["ts"] = 0
                _FW["ts"] = 0
                _DASH_CACHE["ts"] = 0
                _NPM["ts"] = 0   # nodejs 從這條路裝過，Node 版本會變
                _NODE_FLOW["ts"] = 0
                if exit_state == aenums.EXIT_SUCCESS and kind in ("install", "remove"):
                    threading.Thread(target=dashboard_recheck, args=(self._log,), daemon=True).start()   # 不在 GLib 回呼裡等 systemctl
                loop.quit()

            trans.connect("status-changed", on_status)
            trans.connect("status-details-changed", on_details)
            trans.connect("progress-changed", on_progress)
            trans.connect("progress-details-changed", on_progress_details)
            trans.connect("error", on_error)
            trans.connect("config-file-conflict", on_conffile)
            trans.connect("finished", on_finished)
            self._log(msg('job_start', LANG_DEFAULT, p0=kind, p1=' '.join(packages) if packages else ''))
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


# ---------- 應用程式圖示 ----------
# .desktop 的 Icon= 是主題名稱或絕對路徑。主題名稱依 freedesktop 規則在圖示主題目錄找；這裡只索引 apps 類與 pixmaps，
# 一次約 0.1 秒，快取 1 小時。前端拿到的是 /appicon/<token>，token 只對應索引裡的路徑，不能指定任意檔案。
ICON_THEMES = ["Yaru-nvidia", "Yaru", "hicolor", "Adwaita", "Humanity"]
ICON_ROOTS = [f"/usr/share/icons/{t}" for t in ICON_THEMES] + [
    os.path.expanduser("~/.local/share/icons/hicolor"), "/var/lib/flatpak/exports/share/icons/hicolor", "/usr/share/pixmaps"]
ICON_SIZE_PREF = {"64x64": 0, "48x48": 1, "128x128": 2, "scalable": 3, "256x256": 4, "96x96": 5, "32x32": 6, "512x512": 7, "24x24": 8, "16x16": 9}
_ICON_INDEX = {"ts": 0, "idx": {}}
_ICON_TOKENS = {}   # token → 檔案路徑（只放解析出來的）


def _icon_index():
    if time.time() - _ICON_INDEX["ts"] < 3600 and _ICON_INDEX["idx"]:
        return _ICON_INDEX["idx"]
    idx = {}
    for ri, root in enumerate(ICON_ROOTS):
        for dp, _, fns in os.walk(root):
            parts = dp[len(root):].split("/")
            size = next((x for x in parts if x in ICON_SIZE_PREF), None)
            if root != "/usr/share/pixmaps" and size is None:
                continue
            for f in fns:
                base, ext = os.path.splitext(f)
                if ext not in (".png", ".svg"):
                    continue
                key = (0 if "apps" in parts or root == "/usr/share/pixmaps" else 1, ri, ICON_SIZE_PREF.get(size, 5), 0 if ext == ".png" else 1)
                if base not in idx or key < idx[base][0]:
                    idx[base] = (key, os.path.join(dp, f))
    _ICON_INDEX.update(ts=time.time(), idx=idx)
    return idx


def icon_url(name):
    """Icon= 值 → /appicon/<token>；找不到回 None（前端畫預設方塊，不假裝有圖）。"""
    if not name:
        return None
    path = None
    if name.startswith("/"):
        if os.path.isfile(name) and os.path.splitext(name)[1] in (".png", ".svg"):
            path = name
    else:
        hit = _icon_index().get(name) or _icon_index().get(os.path.splitext(name)[0])
        path = hit[1] if hit else None
    if not path:
        return None
    token = hashlib.sha1(path.encode()).hexdigest()[:16]
    _ICON_TOKENS[token] = path
    return f"/appicon/{token}"


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
        try:   # dpkg 檔案清單的 mtime ＝ 最後一次安裝／升級時間；沒有就不填
            installed_at = datetime.fromtimestamp(os.path.getmtime(f"/var/lib/dpkg/info/{pkg}.list")).isoformat(timespec="minutes")
        except OSError:
            installed_at = None
        out.append({
            "source": "apt", "id": pkg,
            "name": entry["name"], "name_zh": entry["name_zh"], "comment": entry["comment"],
            "icon": entry["icon"], "icon_url": icon_url(entry["icon"]), "categories": entry["categories"],
            "size": ap.installed.installed_size or None, "installed_at": installed_at,
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
    meta = {}
    try:   # snapd 的 REST API（socket 0666，不需 root）有安裝大小與日期；拿不到就留空
        for sn in _snapd_get("/v2/snaps") or []:
            meta[sn["name"]] = sn
    except Exception:
        pass
    out = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        name, ver, rev, tracking, publisher = parts[:5]
        m = meta.get(name, {})
        inst_at = (m.get("install-date") or "")[:16] or None
        notes = parts[5] if len(parts) > 5 else ""
        if "base" in notes or name.startswith("core") or name in ("bare", "snapd"):
            continue
        if "disabled" in notes:
            continue
        e = names.get(name, {})
        out.append({
            "source": "snap", "id": name,
            "name": e.get("name") or name, "name_zh": e.get("name_zh", ""), "comment": e.get("comment", ""),
            "icon": e.get("icon", ""), "icon_url": icon_url(e.get("icon", "")) or (f"/snapicon/{name}" if m.get("icon") else None), "categories": e.get("categories", ""),
            "size": m.get("installed-size"), "installed_at": inst_at,
            "version": ver, "candidate": updates.get(name),
            "origin": f"snap · {publisher.rstrip('*')} · {tracking}",
            "changelog": "snap",
            "checked_updates": check_updates and u is not None,
        })
    return out


def apps_flatpak(check_updates):
    txt = _run(["flatpak", "list", "--app", "--columns=application,name,version,origin,size,installation"])
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
                    updates[parts[0]] = ver or (msg('new_commit', LANG_DEFAULT, p0=commit) if commit else msg('new_version_no_number', LANG_DEFAULT))
    out = []
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        app, name, ver, origin = parts[:4]
        size_txt = parts[4] if len(parts) > 4 else ""
        inst = parts[5] if len(parts) > 5 else "system"
        size = _parse_size(size_txt)
        base = "/var/lib/flatpak/app" if inst == "system" else os.path.expanduser("~/.local/share/flatpak/app")
        try:
            inst_at = datetime.fromtimestamp(os.path.getmtime(os.path.join(base, app, "current", "active"))).isoformat(timespec="minutes")
        except OSError:
            inst_at = None
        out.append({
            "source": "flatpak", "id": app,
            "name": name or app, "name_zh": "", "comment": "", "icon": app, "icon_url": icon_url(app), "categories": "",
            "size": size, "installed_at": inst_at,
            "version": ver, "candidate": updates.get(app),
            "origin": f"flatpak · {origin}",
            "remote": origin,
            "changelog": "flatpak",
            "checked_updates": checked,
        })
    return out


def _parse_size(txt):
    """flatpak 印的 '83.6 MB'／'1.2 GB'（10 進位）→ bytes；解析不了回 None。"""
    m = re.match(r"([\d.]+)\s*([kMG]?B)", txt or "")
    if not m:
        return None
    return int(float(m.group(1)) * {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}[m.group(2)])


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


# 第三方 repo 不提供 apt changelog 時，指向廠商自己的發行說明頁（比只說「沒有」有用）
VENDOR_RELEASE_NOTES = {
    "google-chrome-stable": "https://chromereleases.googleblog.com/search/label/Stable%20updates",
    "code": "https://code.visualstudio.com/updates",
    "brave-browser": "https://brave.com/latest/",
    "tailscale": "https://tailscale.com/changelog",
    "chatgpt": "https://help.openai.com/en/articles/10119604-work-with-apps-on-macos-and-windows",
    "docker-ce": "https://docs.docker.com/engine/release-notes/",
    "docker-ce-cli": "https://docs.docker.com/engine/release-notes/",
    "nodejs": "https://github.com/nodejs/node/releases",
    "gh": "https://github.com/cli/cli/releases",
}


def _apt_vendor_hint(pkg):
    """沒有 changelog 時給使用者一條去處：廠商發行說明頁，沒有的話退回套件自己宣告的官網與來源網域。"""
    url = VENDOR_RELEASE_NOTES.get(pkg)
    home = site = None
    try:
        cache = apt.Cache()
        if pkg in cache and cache[pkg].candidate:
            v = cache[pkg].candidate
            home = v.homepage or None
            site = v.origins[0].site if v.origins else None
    except Exception:
        pass
    if url:
        return msg('vendor_notes', LANG_DEFAULT, p0=url)
    if home:
        return msg('changelog_home', LANG_DEFAULT, p0=home)
    if site:
        return msg('changelog_no_home', LANG_DEFAULT, p0=site)
    return ""


# ---------- 更新清單上的急迫度與一行摘要（學 DGX Dashboard 0.39 的 release highlights，但用真資料） ----------
# 資料來源：Debian changelog 的標頭 `pkg (version) dist; urgency=low|medium|high|critical` 與第一條 `*`。
# 第三方倉庫（Chrome、VS Code、NodeSource）多半沒有 changelog → 照實回 ok=False，畫面顯示「—」，不猜。
# `apt-get changelog` 一個套件約 1–2 秒、走網路：結果存 data/changelog-meta.json（鍵＝套件＋候選版本），
# 缺的交給 4 條背景執行緒抓，/api/updates 先回已有的，前端再輪詢 /api/updates/meta 補齊。
CHANGELOG_META_FILE = os.path.join(HERE, "data", "changelog-meta.json")
CHANGELOG_META_FAIL_TTL = 6 * 3600     # 抓不到的 6 小時後再試（changelogs.ubuntu.com 新版偶爾晚幾小時才有）
URGENCY_RANK = {"low": 0, "medium": 1, "high": 2, "emergency": 3, "critical": 3}
_CLMETA = {"lock": threading.Lock(), "cache": None, "pending": set(), "pool": None}
_CL_HEAD_RE = re.compile(r"^(\S+) \(([^)]+)\)(?: [^;]*;\s*urgency=(\w+))?")


def parse_changelog_meta(text, installed_version=""):
    """從 changelog 全文取「比已安裝版新的那幾筆」的最高急迫度、最新一筆的第一條摘要、筆數、CVE 數。
    沒有任何標頭 → None（不是 changelog）。已安裝版本找不到 → 全部當新的（和 changelog_apt 一樣）。"""
    entries, cur = [], None
    for line in text.splitlines():
        if line.startswith(("Get:", "Hit:", "Fetched")):
            continue
        m = _CL_HEAD_RE.match(line)
        if m:
            if installed_version and m.group(2) == installed_version:
                break
            cur = {"version": m.group(2), "urgency": (m.group(3) or "").lower() or None, "bullets": [], "text": []}
            entries.append(cur)
            continue
        if cur is None:
            continue
        cur["text"].append(line)
        st = line.strip()
        if st.startswith("* "):
            cur["bullets"].append(st[2:].strip())
        elif cur["bullets"] and line.startswith("    ") and st and not st.startswith("- "):
            cur["bullets"][-1] += " " + st          # 續行（縮排四格、不是子項）
    if not entries:
        return None
    urg = max((e["urgency"] for e in entries if e["urgency"]), key=lambda u: URGENCY_RANK.get(u, -1), default=None)
    newer_text = "\n".join(l for e in entries for l in e["text"])
    return {"urgency": urg, "summary": (entries[0]["bullets"][0] if entries[0]["bullets"] else "")[:200],
            "entries": len(entries), "cves": len(set(re.findall(r"CVE-\d{4}-\d{4,}", newer_text)))}


def _clmeta_load():
    with _CLMETA["lock"]:
        if _CLMETA["cache"] is None:
            try:
                with open(CHANGELOG_META_FILE, encoding="utf-8") as f:
                    _CLMETA["cache"] = json.load(f)
            except (OSError, ValueError):
                _CLMETA["cache"] = {}
        return _CLMETA["cache"]


def _clmeta_save():
    with _CLMETA["lock"]:
        os.makedirs(os.path.dirname(CHANGELOG_META_FILE), exist_ok=True)
        tmp = CHANGELOG_META_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CLMETA["cache"], f, ensure_ascii=False)
        os.replace(tmp, CHANGELOG_META_FILE)


def changelog_meta_fetch(name, installed, candidate):
    """同步抓一個套件並寫快取。回快取項：{"ok", "urgency", "summary", "entries", "cves", "ts"}；ok=False 時帶 note。"""
    r = changelog_apt(name, installed)
    if r.get("ok") and r.get("text"):
        meta = parse_changelog_meta(r["text"], installed)
        item = {"ok": True, "ts": time.time(), **meta} if meta else {"ok": False, "ts": time.time(), "note": r.get("note", "")}
    elif r.get("ok"):   # 回 ok 但沒內容＝changelog 到已安裝版就截完（候選版沒有新條目）
        item = {"ok": True, "ts": time.time(), "urgency": None, "summary": "", "entries": 0, "cves": 0}
    else:
        item = {"ok": False, "ts": time.time(), "note": r.get("note", "")}
    cache = _clmeta_load()
    with _CLMETA["lock"]:
        cache[f"{name} {candidate}"] = item
    _clmeta_save()
    return item


def _clmeta_worker(name, installed, candidate):
    try:
        changelog_meta_fetch(name, installed, candidate)
    except Exception as e:
        cache = _clmeta_load()
        with _CLMETA["lock"]:
            cache[f"{name} {candidate}"] = {"ok": False, "ts": time.time(), "note": str(e)[:120]}
    finally:
        with _CLMETA["lock"]:
            _CLMETA["pending"].discard(f"{name} {candidate}")


def updates_meta(items):
    """items：list_updates() 的項目。回 {"meta": {name: 快取項或 None}, "pending": [還在抓的名字]}；
    沒快取（或抓失敗已過 TTL）的丟給背景池，這一次先回 None。不等網路。"""
    cache = _clmeta_load()
    out, pending = {}, []
    now = time.time()
    for it in items:
        key = f"{it['name']} {it['candidate']}"
        with _CLMETA["lock"]:
            hit = cache.get(key)
            stale = hit is not None and not hit.get("ok") and now - hit.get("ts", 0) > CHANGELOG_META_FAIL_TTL
            if hit is not None and not stale:
                out[it["name"]] = hit; continue
            out[it["name"]] = None
            if key in _CLMETA["pending"]:
                pending.append(it["name"]); continue
            _CLMETA["pending"].add(key); pending.append(it["name"])
            if _CLMETA["pool"] is None:
                _CLMETA["pool"] = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="clmeta")
            _CLMETA["pool"].submit(_clmeta_worker, it["name"], it["installed"], it["candidate"])
    return {"meta": out, "pending": pending}


def changelog_apt(pkg, installed_version=""):
    """用 `apt-get changelog`（不需 root）抓候選版本的 changelog，截到已安裝版本為止。
    第三方 repo 多半沒提供，照實回報。python-apt 的 get_changelog 在這台機器上抓不到，所以走子程序。"""
    try:
        r = subprocess.run(["apt-get", "-q", "changelog", pkg], capture_output=True, text=True, timeout=25, env=_ENV_C)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "note": msg('changelog_timeout', LANG_DEFAULT)}
    except OSError as e:
        return {"ok": False, "text": "", "note": msg('apt_get_failed', LANG_DEFAULT, p0=e)}
    if r.returncode != 0:
        hint = _apt_vendor_hint(pkg)
        return {"ok": False, "text": "", "note": msg('no_changelog', LANG_DEFAULT) + (hint or "")}
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
        hint = _apt_vendor_hint(pkg)
        return {"ok": False, "text": "", "note": msg('no_changelog', LANG_DEFAULT) + (hint or "")}
    text = "\n".join(kept).strip()
    if truncated and not text:
        return {"ok": True, "text": "", "note": msg('changelog_current', LANG_DEFAULT)}
    if len(text) > 20000:
        text = text[:20000] + msg('truncated', LANG_DEFAULT)
    note = msg('changelog_source', LANG_DEFAULT) + (msg('changelog_since', LANG_DEFAULT) if truncated else msg('changelog_all', LANG_DEFAULT))
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
                    return {"ok": False, "text": "", "note": msg('appstream_no_releases', LANG_DEFAULT)}
                lines = []
                for r in list(rels)[:8]:
                    v = r.get("version", "?")
                    d = r.get("date", "")[:10]
                    desc = " ".join(t.strip() for t in r.itertext() if t.strip())
                    lines.append(f"{v}  {d}\n  {desc or msg('no_description', LANG_DEFAULT)}")
                    if v == installed_version:
                        break
                return {"ok": True, "text": "\n\n".join(lines), "note": msg('appstream_source', LANG_DEFAULT, p0=os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))))}
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
                    "note": msg('remote_no_notes', LANG_DEFAULT, p0=remote)}
    return {"ok": False, "text": "", "note": msg('appstream_unavailable', LANG_DEFAULT)}


def changelog_snap(name, installed_version=""):
    """Snap 商店沒有逐版 changelog，但 `snap info` 給得出發行者、各頻道的版本／發布日期／版次／大小，
    以及目前安裝的版次。照實說明這不是 changelog。需要連商店，離線會失敗。"""
    out = _run(["snap", "info", name], timeout=30)
    if out is None:
        return {"ok": False, "text": "", "note": msg('snap_info_failed', LANG_DEFAULT, p0=name)}
    g = lambda k: (lambda m: m.group(1).strip() if m else None)(re.search(rf"^{re.escape(k)}:\s*(.+)$", out, re.M))
    summary, publisher = g("summary"), g("publisher")
    store_url, tracking, refresh = g("store-url"), g("tracking"), g("refresh-date")
    rows, in_ch = [], False
    for line in out.splitlines():
        if line.startswith("channels:"):
            in_ch = True
            continue
        if line.startswith("installed:") or in_ch:
            m = re.match(r"^\s*(\S+?):\s+(\S+)\s+(?:(\d{4}-\d{2}-\d{2})\s+)?\((\d+)\)\s+(\S+)", line)
            if m:
                rows.append({"channel": m.group(1), "version": m.group(2), "date": m.group(3) or "",
                             "rev": m.group(4), "size": m.group(5)})
                continue
            if in_ch and line.strip() and not line.startswith(" ") and not line.startswith("installed:"):
                in_ch = False
    if not rows:
        return {"ok": False, "text": "", "note": msg('snap_no_channels', LANG_DEFAULT)}
    inst = next((r for r in rows if r["channel"] == "installed"), None)
    # 回結構化表格，不用空格排版：等寬字型下 CJK 不保證是英數的兩倍寬，靠字元數對齊一定會歪
    table_rows = []
    for r in rows:
        table_rows.append({
            "channel": msg('installed', LANG_DEFAULT) if r["channel"] == "installed" else r["channel"],
            "version": r["version"], "date": r["date"] or "—", "rev": r["rev"], "size": r["size"],
            "installed": r["channel"] == "installed",
            "current": bool(inst and r["channel"] != "installed" and r["rev"] == inst["rev"]),
        })
    meta = " · ".join(x for x in [msg('publisher', LANG_DEFAULT, p0=publisher) if publisher else None,
                                  msg('tracking', LANG_DEFAULT, p0=tracking) if tracking else None,
                                  msg('last_update', LANG_DEFAULT, p0=refresh) if refresh else None] if x)
    return {"ok": True, "text": "", "summary": summary, "meta": meta, "store_url": store_url,
            "table": {"columns": [msg('channel', LANG_DEFAULT), msg('version', LANG_DEFAULT), msg('release_date', LANG_DEFAULT), msg('revision', LANG_DEFAULT), msg('size', LANG_DEFAULT)], "rows": table_rows},
            "note": msg('snap_changelog_note', LANG_DEFAULT)}


def get_changelog(source, ident, installed_version=""):
    key = (source, ident, installed_version)
    if key in _CHANGELOG_CACHE:
        return _CHANGELOG_CACHE[key]
    if source == "apt":
        res = changelog_apt(ident, installed_version)
    elif source == "flatpak":
        res = changelog_flatpak(ident, installed_version)
    elif source == "snap":
        res = changelog_snap(ident, installed_version)
    else:
        res = {"ok": False, "text": "", "note": msg('unknown_source', LANG_DEFAULT)}
    if res["ok"]:
        _CHANGELOG_CACHE[key] = res
    return res


# ---------- Spark Center 自己的更新 ----------
# 別人 git clone 裝的，不會自動跟上；這裡每 6 小時 git fetch 一次（只讀），有新版就在更新分頁列出 commit 標題，
# 一顆「更新並重啟」跑 git pull --ff-only 再重啟服務（不需 root）。本機改過追蹤中的檔案、或有工作在跑、或唯讀模式：不動。
_SELF = {"ts": 0, "data": None, "lock": threading.Lock()}
SELF_TTL = 6 * 3600
SELF_HELPER_FILES = ["tools/node_source.py", "tools/spark-center-node-source.policy"]


def _git(*args, timeout=30):
    r = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, timeout=timeout, env=_ENV_C)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def self_status(force=False):
    with _SELF["lock"]:
        if not force and _SELF["data"] and time.time() - _SELF["ts"] < SELF_TTL:
            return _SELF["data"]
    rc, _, _ = _git("rev-parse", "--is-inside-work-tree", timeout=10)
    if rc != 0:
        return {"ok": True, "git": False, "note": msg("self_not_git", LANG_DEFAULT)}
    out = {"ok": True, "git": True, "fetch_error": None, "checked": None}
    rc, _, err = _git("fetch", "--quiet", "origin", timeout=60)
    if rc != 0:
        out["fetch_error"] = msg("self_fetch_failed", LANG_DEFAULT, err=err[:120])
    else:
        out["checked"] = datetime.now().isoformat(timespec="seconds")
    _, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    rc, upstream, _ = _git("rev-parse", "--abbrev-ref", "@{u}", timeout=10)
    if rc != 0:
        upstream = "origin/main"
    _, head, _ = _git("log", "-1", "--format=%h%x09%ad%x09%s", "--date=short", "HEAD", timeout=10)
    _, remote, _ = _git("log", "-1", "--format=%h%x09%ad%x09%s", "--date=short", upstream, timeout=10)
    _, behind, _ = _git("rev-list", "--count", f"HEAD..{upstream}", timeout=10)
    _, ahead, _ = _git("rev-list", "--count", f"{upstream}..HEAD", timeout=10)
    _, log, _ = _git("log", "--format=%h%x09%ad%x09%s", "--date=short", "-n", "40", f"HEAD..{upstream}", timeout=10)
    _, dirty, _ = _git("status", "--porcelain", "--untracked-files=no", timeout=10)
    _, helper_diff, _ = _git("diff", "--name-only", "HEAD", upstream, "--", *SELF_HELPER_FILES, timeout=10)
    split = lambda line: dict(zip(("hash", "date", "subject"), line.split("\t", 2)))
    out.update(branch=branch, upstream=upstream, head=split(head) if head else None, remote=split(remote) if remote else None,
               behind=int(behind or 0), ahead=int(ahead or 0), commits=[split(l) for l in log.splitlines() if l],
               dirty=[l.split(None, 1)[-1] for l in dirty.splitlines() if l.strip()], helper_changed=bool(helper_diff.strip()),   # _git 已 strip，不能用固定位移取檔名
               can_update=(not READONLY) and int(behind or 0) > 0 and not dirty.strip() and int(ahead or 0) == 0)
    with _SELF["lock"]:
        if not out["fetch_error"]:
            _SELF.update(ts=time.time(), data=out)
    return out


def self_update():
    """git pull --ff-only 然後 1 秒後由 systemd 的臨時計時器重啟本服務（從自己的 cgroup 外面重啟，才不會被自己的停止殺掉）。"""
    st = self_status(force=True)
    if not st.get("git"):
        return {"ok": False, "error": st.get("note")}
    if st.get("dirty"):
        return {"ok": False, "error": msg("self_dirty", LANG_DEFAULT, files=", ".join(st["dirty"][:5]))}
    if st.get("ahead"):
        return {"ok": False, "error": msg("self_ahead", LANG_DEFAULT, n=st["ahead"])}
    if not st.get("behind"):
        return {"ok": False, "error": msg("self_up_to_date", LANG_DEFAULT)}
    rc, out, err = _git("pull", "--ff-only", "--quiet", timeout=120)
    if rc != 0:
        return {"ok": False, "error": msg("self_pull_failed", LANG_DEFAULT, err=(err or out)[:200])}
    _, head, _ = _git("log", "-1", "--format=%h %s", timeout=10)
    _SELF["ts"] = 0
    try:
        subprocess.Popen(["systemd-run", "--user", "--quiet", "--on-active=1", "--unit=spark-center-self-restart",
                          "systemctl", "--user", "restart", "spark-center"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restart = True
    except OSError:
        restart = False
    return {"ok": True, "head": head, "restart_scheduled": restart, "helper_changed": st.get("helper_changed")}


# ---------- 設定分頁：選用功能的狀態與指令 ----------
# 新手卡在「選用步驟散在 README 四處、裝完不知道還有什麼沒開」。這裡把每一項列成：給你什麼、要付出什麼、現在開了沒、
# 路徑已填好的指令。狀態用 sudo -n -l <指令> 問「准不准」，不真的執行。
GPU_CLOCK_MIN, GPU_CLOCK_MAX, GPU_CLOCK_STEP = 1000, 3000, 100
GPU_CLOCK_VALUES = list(range(GPU_CLOCK_MIN, GPU_CLOCK_MAX + 1, GPU_CLOCK_STEP))
GPU_CLOCK_FILE = os.path.join(HERE, "data", "gpu-clock.json")
_GPU_CLOCK = {"ts": 0, "enabled": False}

SUDOERS_CMDS = {
    "gpu_clock": ["/usr/bin/nvidia-smi", "-rgc"],
    "dmidecode": ["/usr/sbin/dmidecode"],
    "nvme": ["/usr/sbin/nvme", "smart-log", "/dev/nvme0n1", "--output-format=json"],
    "dashboard_recheck": ["/usr/bin/systemctl", "restart", "dgx-dashboard-admin.service"],
    "disk_selftest": hardware.SELFTEST_CMDS["id_ctrl"],   # 放行偵測看這組裡的第一條；放行時整組一起寫
    "disk_fsck": hardware.FSCK_CMDS["info"],
}


def gpu_clock_state(force=False):
    """時脈上限的狀態：sudoers 有沒有放行（60 秒快取，監控每 2 秒問一次不該每次跑 sudo）、目前存的上限。"""
    if force or time.time() - _GPU_CLOCK["ts"] > 60:
        _GPU_CLOCK.update(ts=time.time(), enabled=_sudo_allowed(SUDOERS_CMDS["gpu_clock"]))
    cap = None
    try:
        with open(GPU_CLOCK_FILE) as f:
            cap = int(json.load(f).get("cap_mhz") or 0) or None
    except (OSError, ValueError):
        pass
    return {"enabled": _GPU_CLOCK["enabled"], "cap_mhz": cap, "min": GPU_CLOCK_MIN, "max": GPU_CLOCK_MAX, "step": GPU_CLOCK_STEP}


def gpu_clock_set(mhz):
    """mhz=None 還原驅動預設。只接受 sudoers 列出的那幾個值；成功才記檔，登入時 Spark Center 會重套（驅動重開機就忘）。"""
    if READONLY:
        return {"ok": False, "error": msg("readonly_mode", LANG_DEFAULT)}
    if not gpu_clock_state(force=True)["enabled"]:
        return {"ok": False, "error": msg("gpu_clock_not_enabled", LANG_DEFAULT)}
    if mhz is not None and mhz not in GPU_CLOCK_VALUES:
        return {"ok": False, "error": msg("gpu_clock_bad_value", LANG_DEFAULT, lo=GPU_CLOCK_MIN, hi=GPU_CLOCK_MAX, step=GPU_CLOCK_STEP)}
    cmd = ["sudo", "-n", "/usr/bin/nvidia-smi"] + (["-rgc"] if mhz is None else ["-lgc", f"0,{mhz}"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return {"ok": False, "error": msg("gpu_clock_failed", LANG_DEFAULT, err=out[-300:])}
    try:
        if mhz is None:
            if os.path.exists(GPU_CLOCK_FILE):
                os.remove(GPU_CLOCK_FILE)
        else:
            os.makedirs(os.path.dirname(GPU_CLOCK_FILE), exist_ok=True)
            with open(GPU_CLOCK_FILE, "w") as f:
                json.dump({"cap_mhz": mhz, "set": datetime.now().isoformat(timespec="seconds")}, f)
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "output": out[-300:], **gpu_clock_state()}


def _gpu_clock_reapply():
    """服務啟動時（登入）：之前設過上限就重套一次。驅動的 -lgc 重開機會忘，這裡不用另外做 systemd 單元。"""
    try:
        st = gpu_clock_state(force=True)
        if st["cap_mhz"] and st["enabled"]:
            r = gpu_clock_set(st["cap_mhz"])
            print("gpu clock cap reapplied:", st["cap_mhz"], "MHz" if r.get("ok") else r.get("error"), flush=True)
    except Exception as e:
        print("gpu clock reapply skipped:", e, flush=True)


_SUDO_L = {"ts": 0, "text": ""}


def _sudo_allowed(cmd):
    """這條指令能不能免密碼跑。不能用 `sudo -n -l <cmd>` 的回傳值：使用者在 sudo 群組時它對任何指令都回 0
    （「允許、但要密碼」也算允許），開關會全部顯示開啟、實際跑卻卡在要密碼。要看 `sudo -n -l` 列表裡的 NOPASSWD 行。"""
    try:
        if time.time() - _SUDO_L["ts"] > 30:
            r = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10)
            _SUDO_L.update(ts=time.time(), text=r.stdout if r.returncode == 0 else "")
        want = " ".join(cmd)
        for line in _SUDO_L["text"].splitlines():
            line = line.strip()
            if "NOPASSWD:" not in line:
                continue
            if line.startswith("(ALL") and line.endswith(" ALL"):
                return True   # 整體免密碼
            allowed = [c.strip().replace("\\", "") for c in line.split("NOPASSWD:", 1)[1].split(", ")]
            if want in allowed:
                return True
        return False
    except Exception:
        return False


def _pwa_installed():
    """Chrome 把 Spark Center 安裝成 app 後會寫 chrome-<id>-Default.desktop（內含我們的 profile 路徑）。和啟動器同一個判斷。"""
    prof = os.path.expanduser("~/.local/share/spark-center/chrome-profile")
    appdir = os.path.expanduser("~/.local/share/applications")
    try:
        for fn in os.listdir(appdir):
            if fn.startswith("chrome-") and fn.endswith("-Default.desktop"):
                txt = open(os.path.join(appdir, fn), encoding="utf-8", errors="replace").read()
                name = "Spark Center" if PORT == 11001 else f"Spark Center ({PORT})"
                if f"--user-data-dir={prof}" in txt and re.search(rf"^Name={re.escape(name)}$", txt, re.M):
                    return fn[len("chrome-"):-len("-Default.desktop")]
    except OSError:
        pass
    return None


def setup_status():
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    q = shlex.quote
    items = []
    caps = model_capabilities.snapshot()
    items.append({"id": "model_guard", "enabled": caps['guard_available'], "sudo": False,
                  "commands": [f"sh {q(os.path.join(HERE, 'install.sh'))} --guard"]})
    items.append({"id": "memory_policy", "enabled": caps['memory_policy']['state'] == 'configured',
                  "sudo": False, "action": "instructions",
                  "commands": [f"python3 {q(os.path.join(HERE, 'tools', 'calibrate_memory.py'))}"]})
    items.append({"id": "earlyoom", "enabled": caps['earlyoom'] == 'active', "sudo": False,
                  "action": "instructions", "commands": [f"sudo sh {q(os.path.join(HERE, 'tools', 'install_earlyoom.sh'))}"]})
    items.append({"id": "gpu_clock", "enabled": _sudo_allowed(SUDOERS_CMDS["gpu_clock"]), "sudo": True, "applies": os.path.exists("/usr/bin/nvidia-smi"),
                  "commands": [f'printf "%s\\n" "{user} ALL=(root) NOPASSWD: {SETUP_SUDOERS["gpu_clock"][1]}" | sudo tee /etc/sudoers.d/spark-center-gpu-clock',
                               "sudo chmod 440 /etc/sudoers.d/spark-center-gpu-clock"]})
    items.append({"id": "dmidecode", "enabled": _sudo_allowed(SUDOERS_CMDS["dmidecode"]), "sudo": True,
                  "commands": [f'echo "{user} ALL=(root) NOPASSWD: /usr/sbin/dmidecode" | sudo tee /etc/sudoers.d/spark-center-dmidecode',
                               "sudo chmod 440 /etc/sudoers.d/spark-center-dmidecode"]})
    items.append({"id": "nvme", "enabled": _sudo_allowed(SUDOERS_CMDS["nvme"]), "sudo": True,
                  "commands": [f'echo "{user} ALL=(root) NOPASSWD: /usr/sbin/nvme smart-log /dev/nvme0n1 --output-format=json" | sudo tee /etc/sudoers.d/spark-center-nvme',
                               "sudo chmod 440 /etc/sudoers.d/spark-center-nvme"]})
    for sid, cmds, applies in (("disk_selftest", hardware.SELFTEST_CMDS, os.path.exists(hardware.NVME_CTRL)),
                               ("disk_fsck", hardware.FSCK_CMDS, hardware.fsck_applies())):
        fn, line = SETUP_SUDOERS[sid]
        items.append({"id": sid, "enabled": _sudo_allowed(SUDOERS_CMDS[sid]), "sudo": True, "applies": applies,
                      "commands": [f'printf "%s\\n" "{user} ALL=(root) NOPASSWD: {line}" | sudo tee /etc/sudoers.d/{fn}',
                                   f"sudo chmod 440 /etc/sudoers.d/{fn}"]})
    items.append({"id": "dashboard_recheck", "enabled": _sudo_allowed(SUDOERS_CMDS["dashboard_recheck"]), "sudo": True,
                  "applies": os.path.exists("/opt/nvidia/dgx-dashboard"),
                  "commands": [f'echo "{user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart dgx-dashboard-admin.service" | sudo tee /etc/sudoers.d/spark-center-dashboard',
                               "sudo chmod 440 /etc/sudoers.d/spark-center-dashboard"]})
    ns = node_status()
    # 只列「Node 是 NodeSource 的 apt 套件」的機器：nvm／snap 裝的 helper 也裝得起來但功能會說不適用，列了只會讓新手困惑
    try:
        nodesource = os.path.realpath(shutil.which("node") or "") == "/usr/bin/node" and "nodesource" in (apt.Cache()["nodejs"].installed.version if apt.Cache().get("nodejs") and apt.Cache()["nodejs"].installed else "")
    except Exception:
        nodesource = False
    items.append({"id": "node_helper", "enabled": bool(node_helper_ready() and os.path.isfile("/usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy")),
                  "sudo": True, "applies": nodesource, "recommended": bool(nodesource and ns.get("target") and ns.get("major") and ns["target"] > ns["major"]),
                  "detail": {"node": ns.get("installed") or "", "lts": ns.get("target")},
                  "commands": [f"cd {q(HERE)}",
                               "sudo install -d -o root -g root -m 0755 /usr/local/libexec/spark-center",
                               "sudo install -o root -g root -m 0755 tools/node_source.py /usr/local/libexec/spark-center/node_source.py",
                               "sudo install -o root -g root -m 0644 tools/spark-center-node-source.policy /usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy"]})
    browser = next((b for b in ("google-chrome", "google-chrome-stable", "chromium", "brave-browser") if shutil.which(b)), None)
    items.append({"id": "pwa", "enabled": bool(_pwa_installed()), "sudo": False, "applies": bool(browser), "action": "pwa_install",
                  "commands": [f"{q(os.path.join(HERE, 'app', 'spark-center-app'))} --install"]})
    items.append({"id": "autostart", "enabled": autostart_status().get("enabled", False), "sudo": False, "action": "autostart", "commands": []})
    # 從別台電腦連（NVIDIA Sync／ssh -L）：SSH 有沒有在聽、要填的值直接給
    ssh_listening = bool(re.search(r"^LISTEN\s+\S+\s+\S+\s+\S*:22\s", _run(["ss", "-ltnH"], timeout=5) or "", re.M))
    ssh_boot = any((_run(["systemctl", "is-enabled", u], timeout=5) or "").strip() == "enabled" for u in ("ssh.socket", "ssh.service"))
    host = (_run(["hostname"], timeout=5) or "").strip()
    ips = []
    try:
        for i in json.loads(_run(["ip", "-j", "-4", "addr"], timeout=5) or "[]"):
            if i.get("ifname") == "lo" or not os.path.exists(f"/sys/class/net/{i.get('ifname','')}/device"):
                continue
            ips += [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet"]
    except (ValueError, KeyError):
        pass
    ts = (_run(["tailscale", "ip", "-4"], timeout=5) or "").strip().splitlines()[:1] if shutil.which("tailscale") else []
    items.append({"id": "remote", "enabled": ssh_listening, "sudo": True, "applies": os.path.exists("/usr/sbin/sshd"),
                  "detail": {"host": host, "mdns": f"{host}.local" if host else None, "ips": ips, "tailscale": ts[0] if ts else None, "user": user, "port": PORT, "boot": ssh_boot},
                  "commands": [f"ssh -L {PORT}:127.0.0.1:{PORT} {user}@{host}.local"]})
    cal = hardware.usbc_map_load(); srcs = {v.get("source") for v in cal.values()}
    items.append({"id": "usbc", "enabled": bool(cal), "sudo": False, "action": "link", "commands": [],
                  "state": "default" if srcs == {"default"} else ("calibrated" if cal else "none"), "n": len(cal)})
    return {"ok": True, "items": items, "repo": HERE, "readonly": READONLY, "capabilities": caps, "generated": datetime.now().isoformat(timespec="seconds")}


# 開關：真的替使用者做，不只給指令。每一步都是 pkexec 跑一個系統指令（install／rm）處理一個明確的檔案，
# 跳一次密碼視窗；沒有常駐 root、沒有任意 shell。sudoers 內容先用 visudo -c 驗過才裝。
SETUP_SUDOERS = {
    # 逐一列出允許的值（sudoers 逗號要寫成 \,）；沒有萬用字元，所以 -pl、-r 之類其他參數都不放行
    "gpu_clock": ("spark-center-gpu-clock", ", ".join(["/usr/bin/nvidia-smi -rgc"] + [f"/usr/bin/nvidia-smi -lgc 0\\,{v}" for v in GPU_CLOCK_VALUES])),
    "dmidecode": ("spark-center-dmidecode", "/usr/sbin/dmidecode"),
    "nvme": ("spark-center-nvme", "/usr/sbin/nvme smart-log /dev/nvme0n1 --output-format=json"),
    "dashboard_recheck": ("spark-center-dashboard", "/usr/bin/systemctl restart dgx-dashboard-admin.service"),
    # 磁碟檢查：裝置路徑與參數都寫死在 backend/hardware.py 的指令表，沒有萬用字元
    "disk_selftest": ("spark-center-disk-selftest", ", ".join(" ".join(c) for c in hardware.SELFTEST_CMDS.values())),
    "disk_fsck": ("spark-center-disk-fsck", ", ".join(" ".join(c) for c in hardware.FSCK_CMDS.values())),
}
NODE_POLICY = "/usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy"


def _pkexec(args, timeout=300):
    """回 (ok, error_message)。126 = 使用者取消或密碼錯，127 = 找不到程式。"""
    env = _session_env()
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return False, msg("setup_no_display_toggle", LANG_DEFAULT)   # SSH 進來、沒有桌面：密碼視窗沒地方跳
    try:
        r = subprocess.run(["pkexec"] + args, capture_output=True, text=True, timeout=timeout, env=_session_env())
    except subprocess.TimeoutExpired:
        return False, msg("setup_timeout", LANG_DEFAULT)
    if r.returncode == 0:
        return True, None
    if r.returncode == 126:
        return False, msg("auth_cancelled", LANG_DEFAULT)
    return False, (r.stderr or r.stdout).strip()[:200] or f"rc={r.returncode}"


def setup_toggle(item, enable):
    if READONLY:
        return {"ok": False, "error": msg("readonly_mode", LANG_DEFAULT)}
    if item == "model_guard":
        try:
            model_capabilities.install_guard(enable)
            return setup_status()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc)}
    if item in SETUP_SUDOERS:
        fn, cmd = SETUP_SUDOERS[item]
        dest = f"/etc/sudoers.d/{fn}"
        if enable:
            user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
            d = tempfile.mkdtemp(prefix="spark-center-sudoers-")
            src = os.path.join(d, fn)
            with open(src, "w", encoding="utf-8") as f:
                f.write(f"{user} ALL=(root) NOPASSWD: {cmd}\n")
            os.chmod(src, 0o644)   # root 的 install 要讀得到；目錄本身是 0700 的話 root 也讀得到
            chk = subprocess.run(["visudo", "-c", "-q", "-f", src], capture_output=True, text=True)
            if chk.returncode != 0:
                shutil.rmtree(d, ignore_errors=True)
                return {"ok": False, "error": msg("setup_visudo_failed", LANG_DEFAULT, err=(chk.stderr or chk.stdout).strip()[:120])}
            ok, err = _pkexec(["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0440", src, dest])
            shutil.rmtree(d, ignore_errors=True)
        else:
            # 早期版本（Spark Updater 時期）的檔名是 spark-updater-*，一起刪，否則開關看起來關不掉
            ok, err = _pkexec(["/usr/bin/rm", "-f", dest, dest.replace("/spark-center-", "/spark-updater-")])
    elif item == "node_helper":
        if enable:
            ok, err = _pkexec(["/usr/bin/install", "-D", "-o", "root", "-g", "root", "-m", "0755", NODE_HELPER_SOURCE, NODE_HELPER])
            if ok:
                ok, err = _pkexec(["/usr/bin/install", "-D", "-o", "root", "-g", "root", "-m", "0644", os.path.join(HERE, "tools", "spark-center-node-source.policy"), NODE_POLICY])
        else:
            ok, err = _pkexec(["/usr/bin/rm", "-f", NODE_HELPER, NODE_POLICY])
    elif item == "remote":
        # Ubuntu 24.04 的 sshd 是 ssh.socket 觸發；enable --now 兩個都做最穩
        ok, err = _pkexec(["/usr/bin/systemctl", "enable" if enable else "disable", "--now", "ssh.socket", "ssh.service"] if enable else ["/usr/bin/systemctl", "disable", "--now", "ssh.socket", "ssh.service"])
    elif item == "autostart":
        r = autostart_set(bool(enable)); ok, err = r.get("ok", False), r.get("error")
    else:
        return {"ok": False, "error": msg("node_bad_action", LANG_DEFAULT)}
    _SUDO_L["ts"] = 0; _GPU_CLOCK["ts"] = 0   # 剛裝／刪 sudoers，快取立刻失效
    hardware.disk_check_reset()
    if not ok:
        return {"ok": False, "error": err, **setup_status()}
    return setup_status()


def _session_env():
    """使用者 systemd 管理員的環境（有 DISPLAY／XAUTHORITY），開圖形程式要用；服務本身的環境沒有這些。"""
    env = dict(os.environ)
    out = _run(["systemctl", "--user", "show-environment"], timeout=10) or ""
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0] in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            k, v = line.split("=", 1); env[k] = v
    return env


def pwa_install_launch():
    env = _session_env()
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return {"ok": False, "error": msg("setup_no_display", LANG_DEFAULT)}
    env["SPARK_CENTER_URL"] = f"http://127.0.0.1:{PORT}"   # 啟動器要開的是這個服務自己的埠（install.sh 可能選了別的埠）
    try:
        subprocess.Popen([os.path.join(HERE, "app", "spark-center-app"), "--install"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def live_with_alerts():
    return {**hardware.hardware_live(), "stability": stability.snapshot(), "gpu_alert": gpu_alert(),
            "gpu_hot": gpu_hot(), "clock_cap": gpu_clock_state()}


# ---------- 本機 LLM 探針（偷師 sparkDash；它沒做 Ollama，這裡補上）----------
import urllib.request
import urllib.error
from tools import model_inventory, llm_switch

LLM_TARGETS = [
    {"kind": "ollama", "url": model_capabilities.OLLAMA},
    {"kind": "lmstudio", "url": "http://127.0.0.1:1234"},
    {"kind": "llama.cpp", "url": model_capabilities.STRATA},
    {"kind": "vllm", "url": "http://127.0.0.1:8000"},
]


def llm_status():
    out = []
    for t in LLM_TARGETS:
        base = t["url"]
        if t["kind"] == "ollama":
            ver = _http_json(base + "/api/version")
            ps = _http_json(base + "/api/ps")
            tags = _http_json(base + "/api/tags")
            ps_ok = isinstance(ps, dict) and isinstance(ps.get("models"), list)
            tags_ok = isinstance(tags, dict) and isinstance(tags.get("models"), list)
            out.append({
                "kind": "Ollama", "url": base, "version": (ver or {}).get("version"),
                "state": "online" if ver is not None and ps_ok and tags_ok else "unavailable",
                "loaded_state": "known" if ps_ok else "unknown", "installed_state": "known" if tags_ok else "unknown",
                "loaded": [{"name": m.get("name"), "size": m.get("size"), "size_vram": m.get("size_vram"),
                            "context": m.get("context_length"), "expires_at": m.get("expires_at"),
                            "quant": ((m.get("details") or {}).get("quantization_level")),
                            "params": ((m.get("details") or {}).get("parameter_size"))} for m in ps["models"]] if ps_ok else None,
                "installed": [{"name": m.get("name"), "size": m.get("size"), "modified": (m.get("modified_at") or "")[:10],
                               "quant": ((m.get("details") or {}).get("quantization_level")), "params": ((m.get("details") or {}).get("parameter_size")),
                               "family": ((m.get("details") or {}).get("family")), "digest": m.get("digest")} for m in tags["models"]] if tags_ok else None,
                "bench": True,
                "note": msg('ollama_metrics', LANG_DEFAULT),
            })
        elif t["kind"] == "lmstudio":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "LM Studio", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": msg('lmstudio_metrics', LANG_DEFAULT)})
        elif t["kind"] == "llama.cpp":
            health = _http_json(base + "/health") or {}
            props = _http_json(base + "/props")
            if health.get("service") == "strata":
                props = props or {}
                name = health.get("model") or props.get("model_alias")
                models = [{"name": name, "context": health.get("max_context"),
                           "path": props.get("model_path"),
                           "capabilities": ["vision"] if health.get("images") else []}] if name else []
                version = (props.get("build_info") or "").removeprefix("Strata ") or None
                out.append({"kind": "Strata", "url": base, "version": version,
                            "state": "online", "loaded_state": "known" if isinstance(health.get("loaded"), bool) else "unknown",
                            "loaded": models if health.get("loaded") is True else ([] if health.get("loaded") is False else None),
                            "installed": models, "bench": False,
                            "note": msg('strata_source', LANG_DEFAULT)})
                continue
            if props is None:
                if any(s.get("type") == "strata" for s in model_inventory.snapshot().get("sources", [])):
                    out.append({"kind": "Strata", "url": base, "state": "unavailable",
                                "loaded": None, "installed": None, "loaded_state": "unknown", "bench": False})
                continue
            slots = _http_json(base + "/slots") or []
            out.append({"kind": "llama.cpp", "url": base, "version": (props.get("build_info") or None),
                        "loaded": [{"name": (props.get("default_generation_settings") or {}).get("model") or props.get("model_path")}],
                        "installed": [], "bench": False, "slots": slots if isinstance(slots, list) else [],
                        "note": msg('llamacpp_source', LANG_DEFAULT)})
        elif t["kind"] == "vllm":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "vLLM", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": msg('vllm_metrics', LANG_DEFAULT)})
    return out


OLLAMA = model_capabilities.OLLAMA
OLLAMA_GUARDED = model_capabilities.GUARD
_SHOW_CACHE = {}


def llm_guard():
    return model_capabilities.route(force=True)['guard']


def llm_endpoint():
    route = model_capabilities.route(force=True)
    if route['guard']['blocked']:
        # Unload still uses the configured endpoint; it remains allowed by guard.
        return OLLAMA_GUARDED
    return route['endpoint']


def llm_admission(model, completion=False):
    if completion:
        show = _http_json(OLLAMA + "/api/show", {"model": model})
        if not isinstance(show, dict) or "completion" not in (show.get("capabilities") or []):
            return msg('llm_no_completion', LANG_DEFAULT), 422
    g = llm_guard()
    if g["blocked"]:
        return msg('llm_' + g['reason'], LANG_DEFAULT), 409
    return None, 200


def llm_request(url, data=None, timeout=600):
    # Select the endpoint before a request; never retry a refused/failed request upstream.
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())
        except ValueError:
            detail = {}
        code = detail.get("code")
        raise RuntimeError(msg('llm_' + code, LANG_DEFAULT) if code in ('strata_resident', 'strata_unknown')
                           else str(detail.get('error') or exc)) from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(msg('llm_guard_unavailable', LANG_DEFAULT)) from exc


def ollama_show(name, digest=None):
    """/api/show 的精華：家族、參數量、量化、上下文長度、能力。快取（模型不變就不重查）。"""
    key = (name, digest)
    if key in _SHOW_CACHE and time.monotonic() - _SHOW_CACHE[key][0] < 60:
        return _SHOW_CACHE[key][1]
    j = _http_json(OLLAMA + "/api/show", {"name": name}, timeout=20) or {}
    det = j.get("details") or {}
    mi = j.get("model_info") or {}
    ctx = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
    res = {"family": det.get("family"), "params": det.get("parameter_size"), "quant": det.get("quantization_level"),
           "context": ctx, "capabilities": j.get("capabilities") or [], "arch": mi.get("general.architecture"),
           "param_count": mi.get("general.parameter_count")}
    if j:
        _SHOW_CACHE[key] = (time.monotonic(), res)
    return res


def ollama_config():
    out = _run(["systemctl", "show", "ollama", "-p", "Environment", "-p", "ActiveState", "-p", "MainPID"], timeout=5) or ""
    env = dict(re.findall(r"(OLLAMA_[A-Z_]+)=([^\s\"]+)", out))
    active = re.search(r"ActiveState=(\w+)", out)
    return {"active": active.group(1) if active else None, "env": env,
            "num_parallel": int(env.get("OLLAMA_NUM_PARALLEL", "0") or 0) or None,
            "max_loaded": int(env.get("OLLAMA_MAX_LOADED_MODELS", "0") or 0) or None,
            "models_dir": env.get("OLLAMA_MODELS", "/usr/share/ollama/.ollama/models"),
            "note": msg('ollama_config', LANG_DEFAULT)}


def llm_stores():
    return model_inventory.snapshot()


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
        _LLM_BENCH["state"] = {"status": "running", "model": model, "num_predict": num_predict, "phase": msg('warming', LANG_DEFAULT),
                               "started": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_llm_bench_run, args=(model, num_predict), daemon=True).start()
    return True


def _llm_bench_run_concurrent(model, n, num_predict):
    """n 個請求同時送，總 tok/s = 所有回應的 eval_count 合計 ÷ 牆鐘時間。OLLAMA_NUM_PARALLEL 小於 n 時會排隊，數字會反映排隊。"""
    base = llm_endpoint()
    prompts = [f"Write {i+1} short paragraphs about the history of the number {i+7}. " * 2 for i in range(n)]
    try:
        warm = llm_request(base + "/api/generate", {"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1, "temperature": 0}}, timeout=600)
        if warm is None:
            raise RuntimeError(msg('warm_failed', LANG_DEFAULT))
        with _LLM_BENCH["lock"]:
            _LLM_BENCH["state"]["phase"] = msg('bench_concurrent_phase', LANG_DEFAULT, p0=n, p1=num_predict)
        results = [None] * n
        errors = [None] * n
        def one(i):
            try:
                results[i] = llm_request(base + "/api/generate", {"model": model, "prompt": prompts[i], "stream": False,
                                                               "options": {"num_predict": num_predict, "temperature": 0}}, timeout=1800)
            except RuntimeError as exc:
                errors[i] = str(exc)
        t0 = time.time()
        ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        [t.start() for t in ths]; [t.join() for t in ths]
        wall = time.time() - t0
        ok = [r for r in results if r]
        if not ok:   # 全部失敗：這不是成績，是失敗；不寫歷史
            raise RuntimeError(next((e for e in errors if e), msg('bench_all_failed', LANG_DEFAULT, n=n)))
        toks = sum(r.get("eval_count") or 0 for r in ok)
        per = [round((r.get("eval_count") or 0) / (r.get("eval_duration") or 1) * 1e9, 1) for r in ok]
        cfg = ollama_config()
        res = {"status": "done", "model": model, "finished": datetime.now().isoformat(timespec="seconds"), "concurrency": n,
               "num_predict": num_predict, "decode_tps": round(toks / wall, 1) if wall else None, "decode_tokens": toks,
               "per_request_tps": per, "wall_s": round(wall, 1), "failed": n - len(ok), "prefill_tps": None,
               "num_parallel": cfg.get("num_parallel"),
               "note": (msg('bench_partial_failed', LANG_DEFAULT, failed=n - len(ok), n=n) + " " if len(ok) < n else "") + msg('bench_concurrent_note', LANG_DEFAULT, p0=n, p1=num_predict, p2=cfg.get('num_parallel')) + (msg('requests_queued', LANG_DEFAULT) if (cfg.get("num_parallel") or 1) < n else msg('requests_parallel', LANG_DEFAULT))}
        for m in (llm_request(base + "/api/ps") or {}).get("models", []):
            if m.get("name") == model:
                det = m.get("details") or {}
                res.update({"size": m.get("size"), "quant": det.get("quantization_level"), "params": det.get("parameter_size"), "context": m.get("context_length")})
        llm_hist_append(res)
    except Exception as e:
        res = {"status": "error", "model": model, "error": str(e), "finished": datetime.now().isoformat(timespec="seconds")}
    with _LLM_BENCH["lock"]:
        _LLM_BENCH["state"] = res


def llm_bench_start_concurrent(model, n, num_predict=128):
    with _LLM_BENCH["lock"]:
        if _LLM_BENCH["state"].get("status") == "running":
            return False
        _LLM_BENCH["state"] = {"status": "running", "model": model, "concurrency": n, "num_predict": num_predict, "phase": msg('warming', LANG_DEFAULT),
                               "started": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_llm_bench_run_concurrent, args=(model, n, num_predict), daemon=True).start()
    return True


def _llm_bench_run(model, num_predict=128):
    """Ollama decode/prefill 基準：先暖機 1 token（把載入時間隔開），再量 128 token。
    數字直接取自 Ollama 回應的 eval_count/eval_duration，不是估的。"""
    base = llm_endpoint()
    prompt = "Explain, in plain prose without lists, why the sky appears blue during the day and red at sunset. " * 4
    try:
        warm = llm_request(base + "/api/generate", {"model": model, "prompt": "hi", "stream": False,
                                                    "options": {"num_predict": 1, "temperature": 0}}, timeout=600)
        if warm is None:
            raise RuntimeError(msg('warm_request_failed', LANG_DEFAULT))
        with _LLM_BENCH["lock"]:
            _LLM_BENCH["state"]["phase"] = msg('bench_phase', LANG_DEFAULT, p0=num_predict)
        r = llm_request(base + "/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                                 "options": {"num_predict": num_predict, "temperature": 0}}, timeout=900)
        if r is None:
            raise RuntimeError(msg('bench_failed', LANG_DEFAULT))
        ec, ed = r.get("eval_count") or 0, r.get("eval_duration") or 0
        pc, pd = r.get("prompt_eval_count") or 0, r.get("prompt_eval_duration") or 0
        # 模型資訊（大小／量化／參數量）從 /api/ps 抓，寫進歷史方便比較
        info = {}
        for m in (llm_request(base + "/api/ps") or {}).get("models", []):
            if m.get("name") == model:
                det = m.get("details") or {}
                info = {"size": m.get("size"), "size_vram": m.get("size_vram"), "quant": det.get("quantization_level"),
                        "params": det.get("parameter_size"), "context": m.get("context_length")}
        res = {"status": "done", "model": model, "finished": datetime.now().isoformat(timespec="seconds"),
               "decode_tps": round(ec / ed * 1e9, 1) if ed else None, "decode_tokens": ec,
               "prefill_tps": round(pc / pd * 1e9, 1) if pd else None, "prompt_tokens": pc,
               "load_ms": round((warm.get("load_duration") or 0) / 1e6), "total_ms": round((r.get("total_duration") or 0) / 1e6),
               "num_predict": num_predict, **info,
               "note": msg('bench_note', LANG_DEFAULT, p0=num_predict)}
        llm_hist_append(res)
    except Exception as e:
        res = {"status": "error", "model": model, "error": str(e), "finished": datetime.now().isoformat(timespec="seconds")}
    with _LLM_BENCH["lock"]:
        _LLM_BENCH["state"] = res


# ---------- 磁碟：誰在吃空間（掃描在背景執行緒，快取 10 分鐘）----------

DISK_ACTIONS = {  # 白名單：只有這些指令能被 /api/disk/action 觸發
    "docker_prune": ["docker", "image", "prune", "-f"],          # 只清 dangling 映像，不動有 tag 的
    "docker_builder_prune": ["docker", "builder", "prune", "-f"],
    # find -mindepth 1 -delete 含點開頭的隱藏檔（bash 的 * 不含）；用 && 串接，刪除失敗就不會印「已清空」、退出碼非 0
    "trash_empty": ["bash", "-c", "T=~/.local/share/Trash; mkdir -p \"$T/files\" \"$T/info\" && find \"$T/files\" \"$T/info\" -mindepth 1 -delete && echo " + msg("trash_cleared", LANG_DEFAULT) + " && du -sh \"$T\""],
    "npm_cache_clean": ["bash", "-c", "npm cache clean --force 2>&1 | tail -2; du -sh ~/.npm"],
}
# ---------- GPU 低功耗卡死偵測（論壇第 3 大抱怨：PD 控制器韌體卡住 → SM 釘在 611 MHz、功耗十幾瓦）----------
# 判定：連續 30 秒（6 次取樣）「GPU 使用率 ≥ 20% 但 SM 時脈 ≤ 800 MHz」，或硬體降速／功率煞車旗標持續亮著。
# 閒置時時脈本來就低，所以一定要配合使用率，避免誤報。

_GPU_WATCH = {"lock": threading.Lock(), "samples": [], "alert": None, "last_notify": 0, "hot": None, "hot_last_notify": 0}
# 過熱警報：門檻是驅動回報的降頻點（NVML），拿不到就不警報（不猜一個數字）。
# 連續 60 秒（12 個 5 秒樣本）都在門檻以上才叫，避免跑模型時在門檻附近晃來晃去一直響；同一波每 10 分鐘再提醒；降到門檻下 3 度才解除。
GPU_HOT_SAMPLES = 12
GPU_HOT_RENOTIFY_S = 120          # 還在門檻上就每 2 分鐘再響一輪，直到降溫解除（一聲就過去不算警報）
GPU_HOT_CLEAR_MARGIN = 3
GPU_HOT_SOUND = "alarm-clock-elapsed"
GPU_HOT_SOUND_REPEAT = 4          # 每輪連響幾聲
GPU_HOT_SOUND_GAP_S = 1.5
GPU_STUCK_CLOCK_MHZ = 800
GPU_STUCK_MIN_UTIL = 20
GPU_STUCK_SAMPLES = 6


def gpu_stuck_evaluate(samples):
    """純函式，方便測試。samples: 最近的 [{sm_mhz, util_pct, power_w, event_reasons}]。回 alert dict 或 None。"""
    recent = [x for x in samples[-GPU_STUCK_SAMPLES:] if x.get("sm_mhz") is not None and x.get("util_pct") is not None]
    if len(recent) < GPU_STUCK_SAMPLES:
        return None
    low_clock = all(x["sm_mhz"] <= GPU_STUCK_CLOCK_MHZ and x["util_pct"] >= GPU_STUCK_MIN_UTIL for x in recent)
    hw_flags = all(set(x.get("event_reasons") or []) & hardware.GPU_STUCK_BAD_REASONS for x in recent)
    if not (low_clock or hw_flags):
        return None
    last = recent[-1]
    return {
        "kind": "low_clock" if low_clock else "hw_slowdown",
        "sm_mhz": last["sm_mhz"], "util_pct": last["util_pct"], "power_w": last.get("power_w"),
        "reasons": sorted(set().union(*[set(x.get("event_reasons") or []) for x in recent])),
        "message": (msg('gpu_stuck_load', LANG_DEFAULT, p0=last['util_pct'], p1=last['sm_mhz'], p2=last.get('power_w') or '—')
                    if low_clock else msg('gpu_stuck_flags', LANG_DEFAULT, p0='、'.join(sorted(set().union(*[set(x.get('event_reasons') or []) for x in recent]))))),
        "advice": msg('gpu_stuck_advice', LANG_DEFAULT),
    }


def gpu_hot_evaluate(samples, threshold, current=None):
    """純函式。回 (hot: bool, peak)。current 是上一輪的狀態：已在警報中就用「門檻－餘裕」判斷解除，不用門檻本身。"""
    if not threshold:
        return False, None
    recent = [x["temp_c"] for x in samples if x.get("temp_c") is not None][-GPU_HOT_SAMPLES:]   # 偶爾讀不到的樣本跳過，看最近 N 個有效讀數
    if current:
        last = recent[-1] if recent else None
        return (last is not None and last >= threshold - GPU_HOT_CLEAR_MARGIN), (max(recent) if recent else None)
    if len(recent) < GPU_HOT_SAMPLES:
        return False, None
    return all(v >= threshold for v in recent), max(recent)


def _play_sound(name, times=1, gap=0.0):
    """系統音效（freedesktop 主題自帶，不用裝東西），可連響幾聲；在背景執行緒播，不擋監控迴圈。沒有音效工具或沒有桌面就算了。"""
    def run():
        try:
            env = _session_env()
            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                return
            if shutil.which("canberra-gtk-play"):
                cmd = ["canberra-gtk-play", "-i", name]
            elif shutil.which("paplay") and os.path.exists(f"/usr/share/sounds/freedesktop/stereo/{name}.oga"):
                cmd = ["paplay", f"/usr/share/sounds/freedesktop/stereo/{name}.oga"]
            else:
                return
            for i in range(times):
                subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                if i < times - 1:
                    time.sleep(gap)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _gpu_watch():
    while True:
        try:
            g = hardware.gpu_live()
            if g:
                with _GPU_WATCH["lock"]:
                    _GPU_WATCH["samples"].append({"t": time.time(), **g})
                    _GPU_WATCH["samples"] = _GPU_WATCH["samples"][-24:]
                    alert = gpu_stuck_evaluate(_GPU_WATCH["samples"])
                    if alert and not _GPU_WATCH["alert"]:
                        alert["since"] = datetime.now().isoformat(timespec="seconds")
                    elif alert and _GPU_WATCH["alert"]:
                        alert["since"] = _GPU_WATCH["alert"]["since"]
                    _GPU_WATCH["alert"] = alert
                    notify = alert and time.time() - _GPU_WATCH["last_notify"] > 3600
                    if notify:
                        _GPU_WATCH["last_notify"] = time.time()
                    # 過熱
                    threshold = (hardware.gpu_static() or {}).get("throttle_temp_c")
                    hot, peak = gpu_hot_evaluate(_GPU_WATCH["samples"], threshold, _GPU_WATCH["hot"])
                    hot_notify = False
                    if hot:
                        if not _GPU_WATCH["hot"]:
                            _GPU_WATCH["hot"] = {"since": datetime.now().isoformat(timespec="seconds"), "threshold": threshold, "peak": peak}
                        else:
                            _GPU_WATCH["hot"]["peak"] = max(_GPU_WATCH["hot"]["peak"] or 0, peak or 0)
                        _GPU_WATCH["hot"]["temp_c"] = g.get("temp_c")
                        hot_notify = time.time() - _GPU_WATCH["hot_last_notify"] > GPU_HOT_RENOTIFY_S
                        if hot_notify:
                            _GPU_WATCH["hot_last_notify"] = time.time()
                    else:
                        _GPU_WATCH["hot"] = None
                if notify:
                    subprocess.run(["notify-send", "-u", "critical", "-a", "Spark Center", msg('gpu_stuck_title', LANG_DEFAULT),
                                    alert["message"] + msg('gpu_help', LANG_DEFAULT) % PORT], timeout=10)
                if hot_notify:
                    _notify(msg('gpu_hot_title', LANG_DEFAULT, p0=g.get("temp_c")),
                            msg('gpu_hot_body', LANG_DEFAULT, p0=threshold, p1=GPU_HOT_SAMPLES * 5) + msg('gpu_help', LANG_DEFAULT) % PORT)
                    _play_sound(GPU_HOT_SOUND, GPU_HOT_SOUND_REPEAT, GPU_HOT_SOUND_GAP_S)
        except Exception:
            pass
        time.sleep(5)


def gpu_alert():
    with _GPU_WATCH["lock"]:
        return _GPU_WATCH["alert"]


def gpu_hot():
    with _GPU_WATCH["lock"]:
        return _GPU_WATCH["hot"]


# ---------- 磁碟快滿通知（背景，每 10 分鐘；≥90% 每 6 小時提醒一次）----------

def _disk_watch():
    last = 0
    while True:
        try:
            st = os.statvfs("/")
            pct = (1 - st.f_bavail / st.f_blocks) * 100
            if pct >= 90 and time.time() - last > 6 * 3600:
                free_gb = st.f_bavail * st.f_frsize / 1e9
                subprocess.run(["notify-send", "-u", "critical", "-a", "Spark Center", msg('disk_full_title', LANG_DEFAULT),
                                msg('disk_full', LANG_DEFAULT, p0=format(pct, '.0f'), p1=format(free_gb, '.0f'), p2=PORT)], timeout=10)
                last = time.time()
        except Exception:
            pass
        time.sleep(600)


# ---------- 韌體（fwupd）：不信 Dashboard 的回報，直接問 fwupd ----------
# 論壇第 1 大抱怨的根因：Dashboard 說韌體更新成功，fwupdmgr 卻顯示「expected 0x507 got 0x500」。
# 這裡把「現在版本」和「LVFS 最新版」與「歷史結果」並排，對不上就標出來。

_FW = {"ts": 0, "data": None, "lock": threading.Lock()}
FW_TTL = 600
FW_STATE = {0: msg('fw_unknown', LANG_DEFAULT), 1: msg('fw_pending', LANG_DEFAULT), 2: msg('fw_success', LANG_DEFAULT), 3: msg('failed', LANG_DEFAULT), 4: msg('fw_reboot', LANG_DEFAULT), 5: msg('fw_failed_reboot', LANG_DEFAULT), 6: msg('transaction_failed', LANG_DEFAULT)}
DISK_ACTIONS.update({
    "fwupd_refresh": ["fwupdmgr", "refresh", "--force"],
    "fwupd_update": ["fwupdmgr", "update", "-y", "--no-reboot-check"],   # polkit 會跳密碼；裝完不自動重開
})


def _fw_json(args, timeout=90):
    out = _run(["fwupdmgr"] + args + ["--json"], timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def fwupd_status(force=False):
    with _FW["lock"]:
        if not force and _FW["data"] and time.time() - _FW["ts"] < FW_TTL:
            return _FW["data"]
    if not _run(["which", "fwupdmgr"], timeout=3):
        return {"available": False, "note": msg('fwupd_missing', LANG_DEFAULT)}
    # 三支查詢各自可能失敗（逾時、LVFS 連不上、fwupd 壞掉）。失敗不能變成空清單，否則畫面會說「已是最新」。
    q_devs, q_ups, q_hist = _fw_json(["get-devices"]), _fw_json(["get-updates"], timeout=120), _fw_json(["get-history"])
    failed = [n for n, q in (("get-devices", q_devs), ("get-updates", q_ups), ("get-history", q_hist)) if q is None]
    if q_devs is None:   # 連裝置清單都拿不到，什麼都不能說
        return {"available": True, "error": msg("fwupd_query_failed", LANG_DEFAULT, what=", ".join(failed)), "devices": [],
                "updates": None, "mismatches": None, "pending": None, "generated": datetime.now().isoformat(timespec="seconds")}
    devs = q_devs.get("Devices", [])
    ups = (q_ups or {}).get("Devices", [])
    hist = (q_hist or {}).get("Devices", [])
    latest = {}   # DeviceId → 可升級的最新版本
    for d in ups:
        rels = d.get("Releases") or []
        if rels:
            latest[d.get("DeviceId")] = rels[0].get("Version")
    hist_by = {}
    for h in hist:
        rel = (h.get("Releases") or [{}])[0]
        hist_by.setdefault(h.get("DeviceId"), []).append({
            "old": h.get("VersionOld") or h.get("Version"), "new": rel.get("Version"), "state": h.get("UpdateState"),
            "state_zh": FW_STATE.get(h.get("UpdateState"), str(h.get("UpdateState"))), "error": h.get("UpdateError"),
            "when": datetime.fromtimestamp(h["Modified"]).isoformat(timespec="minutes") if h.get("Modified") else None,
            "summary": rel.get("Summary"), "urgency": rel.get("Urgency")})
    def _html_text(h):
        h = re.sub(r"</li>\s*", "\n", h or ""); h = re.sub(r"</p>\s*", "\n", h); h = re.sub(r"<li>", "• ", h)
        return html.unescape(re.sub(r"\s*\n\s*", "\n", re.sub(r"<[^>]+>", "", h)).strip())   # LVFS 的說明是 HTML，&quot; 這類實體要還原
    def _notes(did, cur, new):
        """LVFS 上這個裝置每一版的說明（廠商有上傳才有；ASUS 的 EC／SoC／PD 韌體有）。給「已裝的這版」與「可升級的新版」。"""
        rel = (_fw_json(["get-releases", did], timeout=30) or {}).get("Releases") or []
        pick = lambda v: next(({"version": r.get("Version"), "summary": r.get("Summary"), "text": _html_text(r.get("Description")),
                                "urgency": r.get("Urgency"), "size": r.get("Size"),
                                # LVFS 的 Created 是廠商上傳的釋出日；沒有就 None，前端顯示「—」
                                "date": datetime.fromtimestamp(r["Created"]).strftime("%Y-%m-%d") if r.get("Created") else None}
                               for r in rel if v and r.get("Version") == v), None)
        # fwupd 的裝置名常是通用的「UEFI Device Firmware」；LVFS 的 release 名稱才分得出 SoC FW／USB-C PD FW
        return {"installed": pick(cur), "latest": pick(new) if new and new != cur else None,
                "lvfs_name": next((r.get("Name") for r in rel if r.get("Name")), None)}
    rows = []
    for d in devs:
        flags = d.get("Flags") or []
        if "updatable" not in flags and "updatable-hidden" not in flags:
            continue
        did = d.get("DeviceId")
        cur = d.get("Version")
        hs = hist_by.get(did, [])
        last = hs[0] if hs else None
        # 對不上：歷史說成功升到 X，但現在版本不是 X → 就是論壇那種「默默失敗」
        mismatch = bool(last and last["state"] == 2 and last["new"] and cur and last["new"] != cur)
        rows.append({
            "id": did, "name": d.get("Name"), "version": cur, "plugin": d.get("Plugin"), "summary": d.get("Summary"),
            "vendor": d.get("Vendor"), "latest": latest.get(did), "update_available": did in latest,
            "needs_reboot": "needs-reboot" in flags, "internal": "internal" in flags, "hidden": "updatable-hidden" in flags,
            "history": hs, "mismatch": mismatch,
            "pending": bool(last and last["state"] in (1, 4)),
            "notes": _notes(did, cur, latest.get(did)) if "updatable-hidden" not in flags else None,
        })
    rows.sort(key=lambda r: (not r["mismatch"], not r["update_available"], r["hidden"], r["name"] or ""))
    # get-updates 失敗：更新數未知（None），不是 0；get-history 失敗：對不上／等重開機未知
    bios = {k: (hardware.read_text_file(f"/sys/class/dmi/id/bios_{k}") or "").strip() for k in ("version", "date")}   # SoC 韌體的「0105」在 DMI，fwupd 只給十六進位
    data = {"available": True, "devices": rows, "bios": bios if bios["version"] else None,
            "updates": None if q_ups is None else sum(1 for r in rows if r["update_available"]),
            "mismatches": None if q_hist is None else sum(1 for r in rows if r["mismatch"]),
            "pending": None if q_hist is None else sum(1 for r in rows if r["pending"]),
            "error": msg("fwupd_query_failed", LANG_DEFAULT, what=", ".join(failed)) if failed else None,
            "generated": datetime.now().isoformat(timespec="seconds"),
            "fwupd_version": (lambda v: (re.search(r"runtime\s+org\.freedesktop\.fwupd\s+(\S+)", v) or [None, None])[1])(_run(["fwupdmgr", "--version"], timeout=5) or "")}
    with _FW["lock"]:
        _FW.update(ts=0 if failed else time.time(), data=data)   # 失敗的不快取，下次再查
    return data


SNAP_ACTIVE = ("Do", "Doing", "Undo", "Undoing", "Wait", "Abort")
SNAP_TASK_STATES = SNAP_ACTIVE + ("Done", "Error", "Hold", "Undone")


class _SnapdConn(http.client.HTTPConnection):
    """snapd 的 REST API 走 unix socket。/run/snapd.socket 是 0666，讀取不需要 root。"""

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect("/run/snapd.socket")
        self.sock = sock


def _snapd_get(path, timeout=10):
    try:
        c = _SnapdConn("localhost", timeout=timeout)
        c.request("GET", path, headers={"Host": "localhost", "Accept": "application/json"})
        body = json.loads(c.getresponse().read().decode())
        c.close()
        return body.get("result") if body.get("status-code", 500) < 400 else None
    except Exception:
        return None


def snap_running_apps(name, proc="/proc"):
    """哪些程序正在使用這個 snap。snapd 不會更新有「應用程式」在跑的 snap，先查出來才不會白要一次密碼。
    exe 路徑是主要依據，讀不到（別的使用者的程序）再看 cgroup 標記。
    snap 自己的 systemd 服務（cgroup 是 snap.<name>.<svc>.service，例：cups 的 cupsd）標成 service=True：
    snapd 更新時會自己停掉再啟動，不算阻擋；曾把 cups snap 的 cupsd 當成使用者程式而擋掉更新（2026-10-23）。"""
    hits = []
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        exe = ""
        try:
            exe = os.readlink(f"{proc}/{pid}/exe")
        except OSError:
            pass
        cg = ""
        try:
            with open(f"{proc}/{pid}/cgroup") as f:
                cg = f.read()
        except OSError:
            pass
        hit = exe.startswith(f"/snap/{name}/") or f"snap.{name}." in cg
        if hit:
            cmd = ""
            try:
                with open(f"{proc}/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                pass
            hits.append({"pid": int(pid), "exe": exe, "cmd": cmd[:140],
                         "service": bool(re.search(rf"/snap\.{re.escape(name)}\.[^/\s]+\.service", cg)),
                         "app": os.path.basename(exe or (cmd.split(" ")[0] if cmd else "?"))})
    return hits


def snap_change_for(names):
    """找 snapd 裡跟這些 snap 有關、還沒結束的變更 id。唯讀，不需要 root。"""
    r = _snapd_get("/v2/changes?select=in-progress")
    if isinstance(r, list):
        for ch in r:
            if ch.get("status") in SNAP_ACTIVE and any(f'"{n}"' in (ch.get("summary") or "") for n in names):
                return ch.get("id")
        return None
    out = _run(["snap", "changes"], timeout=20) or ""
    for line in out.splitlines():
        m = re.match(r"^(\d+)\s+(\S+)\s", line)
        if m and m.group(2) in SNAP_ACTIVE and any(f'"{n}"' in line for n in names):
            return m.group(1)
    return None


def snap_change_progress(cid):
    """讀真進度。優先用 snapd API：下載任務的 progress 是精確的位元組數，能顯示 MB。
    API 不可用時退回解析 snap tasks 的文字（只拿得到百分比）。"""
    r = _snapd_get(f"/v2/changes/{cid}")
    if isinstance(r, dict) and r.get("tasks"):
        tasks = r["tasks"]
        total = len(tasks)
        done = sum(1 for t in tasks if t.get("status") == "Done")
        cur = next((t for t in tasks if t.get("status") in ("Doing", "Undoing")), None)
        pct = bytes_done = bytes_total = None
        doing = cur.get("summary") if cur else None
        if cur:
            pr = cur.get("progress") or {}
            d, t_ = pr.get("done"), pr.get("total")
            if isinstance(d, int) and isinstance(t_, int) and t_ > 1:
                pct = d / t_ * 100
                if t_ > (1 << 20):          # 大於 1 MB 才當位元組看，其餘是任務計數
                    bytes_done, bytes_total = d, t_
        overall = round(done / total * 100) if total else None
        fail_log = next(([l for l in (t.get("log") or [])][-3:] for t in tasks if t.get("status") == "Error"), [])
        return {"status": r.get("status", "Doing"), "done": done, "total": total, "doing": doing,
                "task_percent": round(pct) if pct is not None else None, "task_ratio": overall,
                "bytes_done": bytes_done, "bytes_total": bytes_total, "err": r.get("err"), "fail_log": fail_log,
                "percent": round(pct) if pct is not None else overall}
    out = _run(["snap", "tasks", cid], timeout=20) or ""
    total = done = 0
    doing = None
    pct = None
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(.*)$", line)
        if not m or m.group(1) not in SNAP_TASK_STATES:
            continue
        st = m.group(1)
        summary = re.split(r"\s{2,}", m.group(2))[-1].strip()
        total += 1
        if st == "Done":
            done += 1
        elif st in ("Doing", "Undoing") and doing is None:
            doing = summary
            p = re.search(r"\((\d+(?:\.\d+)?)%\)", summary)
            if p:
                pct = float(p.group(1))
    head = _run(["snap", "changes"], timeout=20) or ""
    status = next((l.split()[1] for l in head.splitlines() if l.startswith(cid + " ")), "Doing")
    overall = round(done / total * 100) if total else None
    # 進度條優先用「目前這步自己的百分比」：下載佔掉幾乎全部時間，用任務數比例會卡在個位數再突然跳到尾聲
    return {"status": status, "done": done, "total": total, "doing": doing,
            "task_percent": round(pct) if pct is not None else None, "task_ratio": overall,
            "bytes_done": None, "bytes_total": None, "err": None, "fail_log": [],
            "percent": round(pct) if pct is not None else overall}


def GB_(b):
    return "—" if b is None else f"{b/1e9:.1f} GB"


# ---------- HTTP ----------

# ---------- 常見問題分頁：NVIDIA 官方 FAQ 的本機對照 ----------
# 題目內容在前端（static/js/faq.js＋字串表）。這裡只給兩樣東西：這台機器實際是什麼狀態（給每題的「本機」框），
# 以及原文有沒有改版——內容是抄進來的快照，NVIDIA 改版後頁面要說出來，不能繼續假裝是最新的。
FAQ_TOPIC = 347344   # https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344
_FAQ_SRC = {"ts": 0, "data": None}


def faq_local():
    """每題「本機」框要的事實；讀不到的欄位給 None，前端顯示「—」。"""
    m = hardware.memory_info()
    mem = None
    if m.get("MemTotal"):
        mem = {"total": m["MemTotal"], "available": m.get("MemAvailable"), "buff_cache": hardware.buffer_cache_bytes(m)}
    wifi = None
    iface = hardware.wifi_interface()
    if iface:
        ips = []
        try:
            out = subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=5).stdout
            ips = [a["local"] for i in json.loads(out or "[]") for a in i.get("addr_info", []) if a.get("local")]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        wifi = {"name": iface, "state": hardware.read_text_file(f"/sys/class/net/{iface}/operstate"), "ipv4": ips}
    # Ubuntu 的版本升級提示：Prompt=lts/normal 會在新 LTS 出來時跳「軟體更新」視窗要你升到 26.04；DGX OS 沒有官方升級路徑
    release = {"prompt": None, "flag": os.path.exists("/var/lib/ubuntu-release-upgrader/release-upgrade-available")}
    for line in (hardware.read_text_file("/etc/update-manager/release-upgrades") or "").splitlines():
        if line.strip().lower().startswith("prompt="):
            release["prompt"] = line.split("=", 1)[1].strip()
    phased = {"policy": phased_policy_status(), "held": None}
    try:
        phased["held"] = [{"name": p.name, "candidate": p.candidate.version if p.candidate else None, "percent": (phased_info(p) or {}).get("percent")}
                          for p in apt.Cache() if p.is_upgradable and getattr(p, "phasing_applied", False)]
    except Exception:
        pass
    return {"ok": True, "cx7": hardware.cx7_state(), "mem": mem, "wifi": wifi, "release": release, "phased": phased}


FAQ_ARCHIVE_DIR = os.path.join(HERE, "data", "faq")


def _faq_archive(post):
    """每個版本的原文留一份純文字在 data/faq/v<N>.txt（本機、不進 git：原文是 NVIDIA 的）。
    下一版出來時才有前一版可以 diff；2026-09-30 改到第 12 版時沒有第 11 版原文，只能整篇重讀。已存在就不重寫。"""
    v = post.get("version")
    if not v:
        return
    path = os.path.join(FAQ_ARCHIVE_DIR, f"v{v}.txt")
    if os.path.exists(path):
        return
    d = post.get("cooked") or ""
    d = re.sub(r'<a [^>]*class="anchor"[^>]*>.*?</a>', "", d, flags=re.S)
    d = re.sub(r"<li>", "\n- ", d)
    d = re.sub(r"</?(p|ul|ol|h[1-6]|pre|br)[^>]*>", "\n", d)
    d = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', lambda m: m.group(2) if m.group(1).startswith("#") else f"{m.group(2)} [{m.group(1)}]", d, flags=re.S)
    t = html_mod.unescape(re.sub(r"<[^>]+>", "", d))
    t = re.sub(r"[ \t]+", " ", t); t = re.sub(r"\n\s*\n+", "\n", t).strip() + "\n"
    os.makedirs(FAQ_ARCHIVE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {FAQ_TOPIC} version {v} updated {(post.get('updated_at') or '')[:10]} archived {datetime.now().isoformat(timespec='seconds')}\n{t}")


def faq_source():
    """原文的版本號與最後編輯日（Discourse 的 post version），成功的結果快取一天；失敗不快取、回 error，不假裝是最新。"""
    if _FAQ_SRC["data"] and time.time() - _FAQ_SRC["ts"] < 86400:
        return dict(_FAQ_SRC["data"])
    d = {"ok": True, "version": None, "updated": None, "checked": None, "error": None}
    try:
        req = urllib.request.Request(f"https://forums.developer.nvidia.com/t/{FAQ_TOPIC}.json", headers={"User-Agent": "spark-center"})
        with urllib.request.urlopen(req, timeout=10) as r:
            post = json.load(r)["post_stream"]["posts"][0]
        d.update(version=post.get("version"), updated=(post.get("updated_at") or "")[:10] or None,
                 checked=datetime.now().isoformat(timespec="seconds"))
        _faq_archive(post)
        _FAQ_SRC.update(ts=time.time(), data=dict(d))
    except Exception as e:
        d["error"] = str(e)[:120]
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkCenter/0.2"

    def log_message(self, fmt, *args):  # 安靜一點，只記錯誤
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _request_lang(self):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        requested = (query.get("lang") or [None])[0]
        if requested is None:
            requested = self.headers.get("Accept-Language", LANG_DEFAULT).split(",", 1)[0].split(";", 1)[0]
        return "zh-TW" if requested.lower().startswith("zh") else "en"

    def _json(self, obj, code=200):
        body = json.dumps(_localized_response(obj, self.lang), ensure_ascii=False).encode()
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
        self.lang = self._request_lang()
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/"):
            # 前端拆成 index.html＋static/（CSS 一檔、JS 依分頁），仍由這支服務直接提供，沒有建置步驟。
            # 只允許 static/ 底下的檔案；no-store 是因為本機服務更新後不該看到舊版。
            base = os.path.realpath(os.path.join(HERE, "static"))
            fp = os.path.realpath(os.path.join(HERE, path.lstrip("/")))
            ctype = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}.get(os.path.splitext(fp)[1])
            if not fp.startswith(base + os.sep) or not ctype or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/snapicon/"):
            name = path[len("/snapicon/"):]
            body, ctype = None, "image/png"
            if re.fullmatch(r"[a-z0-9-]+", name):
                try:
                    c = _SnapdConn("localhost", timeout=5)
                    c.request("GET", f"/v2/icons/{name}/icon", headers={"Host": "localhost"})
                    r = c.getresponse()
                    if r.status == 200:
                        body, ctype = r.read(), r.getheader("Content-Type") or ctype
                    c.close()
                except Exception:
                    body = None
            if not body:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/appicon/"):
            fp = _ICON_TOKENS.get(path[len("/appicon/"):])
            if not fp or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if fp.endswith(".svg") else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/manifest.webmanifest", "/icon-256.png", "/icon-128.png", "/icon.svg"):
            # Chrome app 模式的視窗／工作列圖示來自 web manifest
            if path == "/manifest.webmanifest":
                # id 固定為 "/"：Chrome 用它算 app id（啟動器要靠這個 id 開已安裝的 app）。display_override 的 window-controls-overlay
                # 讓 Chrome 只疊三顆視窗鈕在頂欄右上，系統標題列消失、頂欄可拖曳（CSS 的 app-region）；不支援的瀏覽器退回 standalone。
                # 非預設埠的實例（測試用、或 install.sh 換過埠）名稱帶埠號：Chrome 的捷徑檔只有 Name 能分辨是哪一份，
                # 啟動器靠 Name 找 app id，兩份都叫 Spark Center 會抓錯（真機踩到：開到已關掉的測試埠）。
                app_name = "Spark Center" if PORT == 11001 else f"Spark Center ({PORT})"
                body = json.dumps({"id": "/", "scope": "/", "name": app_name, "short_name": app_name, "start_url": "/",
                                   "display": "standalone", "display_override": ["window-controls-overlay", "standalone"],
                                   "background_color": "#111312", "theme_color": "#000000",
                                   "icons": [{"src": "/icon-256.png", "sizes": "256x256", "type": "image/png"},
                                             {"src": "/icon-128.png", "sizes": "128x128", "type": "image/png"},
                                             {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}).encode()
                ctype = "application/manifest+json"
            else:
                fn = {"/icon-256.png": "spark-center-256.png", "/icon-128.png": "spark-center-128.png", "/icon.svg": "spark-center.svg"}[path]
                with open(os.path.join(HERE, "app", fn), "rb") as f:
                    body = f.read()
                ctype = "image/svg+xml" if fn.endswith(".svg") else "image/png"
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif path == "/api/updates/meta":
            try:
                self._json({"ok": True, **updates_meta(list_updates())})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/updates":
            try:
                items = list_updates()
                self._json({
                    "ok": True,
                    "items": items,
                    **updates_meta(items),
                    "dashboard": dashboard_equivalent(),
                    "phased_policy": phased_policy_status(),
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
        elif path == "/api/machine":
            # 頂欄的機型：直接讀 DMI，不寫死。ASUSTeK COMPUTER INC. 這種長名縮成常見寫法，其餘照原文。
            def dmi(k):
                try:
                    return open(f"/sys/class/dmi/id/{k}", encoding="utf-8", errors="replace").read().strip()
                except OSError:
                    return ""
            vendor = dmi("sys_vendor")
            vendor = re.sub(r"^ASUSTeK COMPUTER INC\.$", "ASUS", vendor, flags=re.I)
            vendor = re.sub(r"^(Dell Inc\.|HP Inc\.|Hewlett-Packard|Gigabyte Technology Co\., Ltd\.|Acer|NVIDIA)$",
                            lambda m: {"dell inc.": "Dell", "hp inc.": "HP", "hewlett-packard": "HP", "gigabyte technology co., ltd.": "GIGABYTE"}.get(m.group(1).lower(), m.group(1)), vendor, flags=re.I)
            self._json({"readonly": READONLY, "ok": True, "vendor": vendor, "product": dmi("product_name"), "family": dmi("product_family")})
        elif path == "/api/dashboard":
            # DGX Dashboard 的網址：埠號照 /usr/bin/dgx-dashboard 的邏輯讀 ports.env，讀不到就 11000
            port = 11000
            try:
                for line in open("/opt/nvidia/dgx-dashboard-service/ports.env", encoding="utf-8"):
                    m = re.match(r"\s*(?:export\s+)?DGX_DASHBOARD_PORT\s*=\s*\"?(\d+)", line)
                    if m:
                        port = int(m.group(1))
            except OSError:
                pass
            self._json({"ok": True, "url": f"http://localhost:{port}/", "installed": os.path.exists("/usr/bin/dgx-dashboard"),
                        "icon": os.path.isfile(DGX_DASHBOARD_ICON)})
        elif path == "/dashboard-icon":
            if not os.path.isfile(DGX_DASHBOARD_ICON):
                self.send_response(404); self.end_headers(); return
            with open(DGX_DASHBOARD_ICON, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/node':
            try:
                self._json(node_status())
            except Exception as e:
                self._json({'ok': False, 'error': node_error(str(e))}, 500)
        elif path == "/api/npm":
            try:
                self._json(npm_status(force="force=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/rollback":
            # 附上目前安裝的版本：降回之後那筆紀錄還在，前端要靠這個判斷「已經是這版」而不再給按鈕
            jobs = _rollback_index_load()
            try:
                cache = apt.Cache()
                npm_cur = None
                for j in jobs:
                    for p in j.get("packages", []):
                        if p.get("kind") == "npm":
                            if npm_cur is None:
                                npm_cur = {e["name"]: e.get("current") for e in npm_status().get("packages", [])}
                            p["installed"] = npm_cur.get(p.get("name"))
                            continue
                        pkg = cache.get(p.get("name"))
                        p["installed"] = pkg.installed.version if pkg and pkg.installed else None
            except Exception:
                pass
            # 同一個套件後來又被更新過（更新的那筆在索引前面）：舊那筆的 .deb 降回去會跨兩版，不算「還能降回」
            seen = set()
            for j in jobs:   # 索引是新到舊
                j["bytes"] = _rollback_job_bytes(j)
                for p in j.get("packages", []):
                    p["superseded"] = p.get("name") in seen
                for p in j.get("packages", []):
                    if p.get("deb") or p.get("kind") == "npm":
                        seen.add(p.get("name"))
            self._json({"ok": True, "jobs": jobs, "keep": ROLLBACK_KEEP, "max_mb": ROLLBACK_MAX_MB, "max_age_days": ROLLBACK_MAX_AGE_DAYS,
                        "total_bytes": sum(j["bytes"] for j in jobs)})
        elif path == "/api/autostart":
            self._json(autostart_status())
        elif path == "/api/hardware":
            try:
                force = "force=1" in (self.path.split("?", 1) + [""])[1]
                d = hardware.hardware_static(force=force)
                self._json({"ok": True, **d, "cached_age_s": hardware.cached_age()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/printers":
            try:
                self._json({"ok": True, **hardware.printers(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/diskcheck":
            try:
                self._json({"ok": True, **hardware.disk_check(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/setup":
            try:
                self._json(setup_status())
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/self":
            try:
                self._json(self_status(force="force=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/lan":
            try:
                self._json(hardware.lan_devices(force="force=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/rear":
            try:
                self._json({"ok": True, **hardware.rear_panel(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/usbports":
            try:
                self._json({"ok": True, "ports": hardware.usb_ports(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/wifi/channels":
            try:
                self._json(hardware.wifi_channel_analysis(rescan="rescan=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/wifi":
            try:
                self._json({"ok": True, "live": hardware.wifi_live(), "aps": hardware.wifi_access_points(), "bt_connected": hardware.bluetooth_connected(), "events_24h": hardware.wifi_events_24h()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/live":
            try:
                self._json({"ok": True, **live_with_alerts()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/disk":
            try:
                self._json({"ok": True, **disk.disk_status(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/firmware":
            try:
                self._json({"ok": True, **fwupd_status(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/llm":
            try:
                with _LLM_BENCH["lock"]:
                    bench = dict(_LLM_BENCH["state"])
                servers = llm_status()
                guard = llm_guard()
                caps = model_capabilities.snapshot()
                control = caps["switch_control"]
                for sv in servers:
                    if (sv["kind"] == "Strata" and sv.get("state") == "unavailable"
                            and control["available"] and control["service_state"] in ("inactive", "failed")
                            and guard.get("ok") is True and guard.get("blocked") is False):
                        sv.update(state="offline", loaded=[], loaded_state="known")
                    if sv["kind"] == "Ollama":
                        for m in sv["installed"] or []:
                            m.update({k: v for k, v in ollama_show(m["name"], m.get("digest")).items() if k in ("context", "capabilities")})
                self._json({"ok": True, "servers": servers, "bench": bench, "history": llm_hist_load()[::-1][:40], "config": ollama_config(), "guard": guard, "inventory": llm_stores(), "switch": llm_switch.SWITCH.snapshot(), "switch_control": control, "capabilities": caps})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/llm/switch":
            self._json({"ok": True, "switch": llm_switch.SWITCH.snapshot()})
        elif path == "/api/llm/stores":
            try:
                self._json({"ok": True, **llm_stores()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/apps":
            force = "force=1" in (self.path.split("?", 1) + [""])[1]
            try:
                self._json({"ok": True, **list_apps(force=force)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/faq":
            self._json(faq_local())
        elif path == "/api/faq/source":
            self._json(faq_source())
        elif path == "/api/changelog":
            from urllib.parse import parse_qs
            q = parse_qs((self.path.split("?", 1) + [""])[1])
            source = (q.get("source") or [""])[0]
            ident = (q.get("id") or [""])[0]
            ver = (q.get("installed") or [""])[0]
            if not source or not ident:
                return self._json({"ok": False, "error": msg('missing_source_id', LANG_DEFAULT)}, 400)
            self._json(get_changelog(source, ident, ver))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _origin_problem(self):
        """修改型請求的來源驗證。只綁 127.0.0.1 擋不住瀏覽器裡任何網頁對本機發的跨站 POST（text/plain 的
        簡單請求不會 preflight，直接送到）。三道關：Host 必須是本機＋本埠；有 Origin 就必須是自己；
        Content-Type 必須是 application/json（強迫瀏覽器 preflight，而本服務不回應 OPTIONS，跨站就死在瀏覽器裡）。"""
        # 只認本機位址，埠號不限：SSH 轉埠（ssh -L、NVIDIA Sync 的 Custom）在遠端那台常用別的本機埠號，
        # 瀏覽器送來的 Host 就是那個埠。DNS rebinding 靠的是非本機的主機名，放寬埠號不影響這道防線。
        def _local(hostport):
            h = hostport.rsplit(":", 1)[0] if re.search(r":\d+$", hostport) else hostport
            return h in ("127.0.0.1", "localhost", "[::1]")
        host = (self.headers.get("Host") or "").strip().lower()
        if not _local(host):
            return f"Host={host or '(none)'}"
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and not (origin.startswith("http://") and _local(origin[len("http://"):])):
            return f"Origin={origin}"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return f"Content-Type={ctype or '(none)'}"
        return None

    def do_POST(self):
        self.lang = self._request_lang()
        path = self.path.split("?", 1)[0]
        if READONLY:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"ok": False, "error": msg("readonly_mode", self.lang)}, 403)
        why = self._origin_problem()
        if why:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"ok": False, "error": msg("bad_origin", self.lang, why=why)}, 403)
        data = self._body()
        if path == "/api/open":
            # 外部連結交給系統預設瀏覽器。PWA 視窗裡 target=_blank 會在 Spark Center 自己的 Chrome profile 開一個
            # 完整瀏覽器，久了它就長成第二個瀏覽器（2026-10-10 盤點：3,823 筆別站紀錄、同步進 17 個擴充功能）。
            # 只收 http/https；沒桌面就回 503 讓前端退回新分頁。
            url = str(data.get("url") or "")
            if len(url) > 2048 or not re.fullmatch(r"https?://[^\s\x00-\x1f\"'<>\\]+", url):
                return self._json({"ok": False, "error": "bad_url"}, 400)
            env = _session_env()
            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                return self._json({"ok": False, "error": "no_display"}, 503)
            try:
                subprocess.Popen(["xdg-open", url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._json({"ok": True})
        if path == "/api/simulate":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            return self._json(simulate(names))
        if path == "/api/install":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            sim = simulate(names)
            if not sim["ok"]:
                return self._json(sim, 400)
            # 鎖看模擬的實際變更（含相依帶入、nodejs:arm64 這種架構限定名），不只看使用者送來的名字
            if any(c["name"].split(":")[0] == "nodejs" for c in sim["changes"]) and _node_flow_pending():
                return self._json({"ok": False, "error": msg('node_flow_locked', LANG_DEFAULT)}, 409)
            if sim.get("phased") and data.get("allow_phased") != [x["name"] for x in sim["phased"]]:
                # 使用者確認的提前安裝集合要和現在模擬出來的一樣；清單開了幾天、放量變了，就回去重看
                return self._json({"ok": False, "error": msg('phased_confirm_required', LANG_DEFAULT, names=", ".join(x["name"] for x in sim["phased"]))}, 409)
            if not JOB.start("install", names):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path in ('/api/node/preview', '/api/node/action'):
            try:
                action, target = data.get('action'), data.get('target')
                plan = node_plan(action, target)
                if not plan.get('ok') or path.endswith('/preview'):
                    return self._json(plan, 200 if plan.get('ok') else 400)
                if data.get('confirmation') != plan['confirmation']:
                    return self._json({'ok': False, 'error': msg('node_plan_changed', LANG_DEFAULT)}, 409)
                if not JOB.start('node_source', [action, str(target), plan['token']]):
                    return self._json({'ok': False, 'error': msg('job_running', LANG_DEFAULT)}, 409)
                return self._json({'ok': True})
            except Exception as e:
                return self._json({'ok': False, 'error': node_error(str(e))}, 400)
        if path == "/api/setup/toggle":
            item, enable = str(data.get("item") or ""), bool(data.get("enable"))
            if item not in ("gpu_clock", "dmidecode", "nvme", "dashboard_recheck", "node_helper", "autostart", "remote", "model_guard", "disk_selftest", "disk_fsck"):
                return self._json({"ok": False, "error": msg("node_bad_action", LANG_DEFAULT)}, 400)
            r = setup_toggle(item, enable)
            return self._json(r, 200 if r.get("ok") else 400)
        if path == "/api/setup/phased":
            r = phased_policy_set(data.get("mode"))
            return self._json(r, 200 if r.get("ok") else 400)
        if path == "/api/setup/pwa-install":
            r = pwa_install_launch()
            return self._json(r, 200 if r.get("ok") else 500)
        if path == "/api/self/update":
            if JOB.state.get("status") == "running":
                return self._json({"ok": False, "error": msg("job_running", LANG_DEFAULT)}, 409)
            try:
                r = self_update()
                return self._json(r, 200 if r.get("ok") else 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/lan/scan":
            if NO_SWEEP:
                return self._json({"ok": False, "error": msg("sweep_disabled", self.lang)}, 403)
            try:
                return self._json(hardware.lan_sweep())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reboot":
            # 只有使用者在畫面上按「重新開機」才會到這裡；而且只在真的有東西等重開（fwupd 排入的 capsule
            # 或 /var/run/reboot-required）時才執行，避免誤觸。授權走 logind 的 polkit（使用者服務已是 allow_active）。
            fw = fwupd_status(force=True)
            if not (fw.get("pending") or os.path.exists(REBOOT_FLAG)):
                return self._json({"ok": False, "error": msg("reboot_nothing_pending", LANG_DEFAULT)}, 400)
            r = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=30, env=_ENV_C)
            if r.returncode != 0:
                return self._json({"ok": False, "error": msg("reboot_failed", LANG_DEFAULT, err=(r.stderr or r.stdout).strip()[:200])}, 500)
            return self._json({"ok": True})
        if path == "/api/npm/restart":
            unit = str(data.get("unit") or "")
            # 只准重啟「目前正在執行某個 npm 全域套件」的 systemd 使用者服務，名稱從即時掃描來，不接受任意單元
            st = npm_status(force=True)
            allowed = {r["unit"] for e in st.get("packages", []) for r in e.get("running", []) if r.get("unit")}
            if not re.fullmatch(r"[A-Za-z0-9@._-]+\.service", unit) or unit not in allowed:
                return self._json({"ok": False, "error": msg("npm_restart_bad_unit", LANG_DEFAULT, unit=unit)}, 400)
            r = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, text=True, timeout=90, env=_ENV_C)
            if r.returncode != 0:
                return self._json({"ok": False, "error": msg("npm_restart_failed", LANG_DEFAULT, unit=unit, err=(r.stderr or r.stdout).strip()[:200])}, 500)
            _NPM["ts"] = 0
            time.sleep(3)
            act = (_run(["systemctl", "--user", "is-active", unit], timeout=20) or "").strip()
            if act != "active":
                tail = (_run(["journalctl", "--user", "-u", unit, "-n", "8", "--no-pager", "-o", "cat"], timeout=15) or "").strip()
                return self._json({"ok": False, "unit": unit, "active": act, "error": msg("npm_unit_failed", LANG_DEFAULT, unit=unit, state=act) + "\n" + tail[-1500:]}, 500)
            return self._json({"ok": True, "unit": unit, "active": act})
        if path == "/api/gpu/clock":   # {mhz: 2200} 或 {reset: true}；拉桿按「確定」才打這裡
            if data.get("reset"):
                return self._json(gpu_clock_set(None))
            try:
                mhz = int(data.get("mhz"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": msg("gpu_clock_bad_value", LANG_DEFAULT, lo=GPU_CLOCK_MIN, hi=GPU_CLOCK_MAX, step=GPU_CLOCK_STEP)}, 400)
            r = gpu_clock_set(mhz)
            return self._json(r, 200 if r.get("ok") else 400)
        if path == "/api/npm/plan":
            names = [n for n in data.get("names", []) if isinstance(n, str) and re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", n)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            targets = {k: v for k, v in (data.get("targets") or {}).items() if isinstance(v, str) and k in names}   # 降回時目標是舊版，前端知道
            return self._json(npm_plan(names, rollback=bool(data.get("rollback")), targets=targets))
        if path == "/api/npm/action":
            act, name = str(data.get("action") or ""), str(data.get("name") or "")
            if not re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", name) or act not in ("repair", "forward"):
                return self._json({"ok": False, "error": msg('node_bad_action', LANG_DEFAULT)}, 400)
            if act == "repair" and name not in NPM_KNOWN:
                return self._json({"ok": False, "error": msg('node_bad_action', LANG_DEFAULT)}, 400)
            ver = str(data.get("version") or "")
            if act == "forward" and not re.fullmatch(r"[0-9A-Za-z.+-]+", ver):
                return self._json({"ok": False, "error": msg('node_bad_action', LANG_DEFAULT)}, 400)
            if not JOB.start("npm_repair" if act == "repair" else "npm_forward", [name] if act == "repair" else [name, ver]):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/npm/update":
            names = [n for n in data.get("names", []) if isinstance(n, str) and re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", n)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            _NPM_JOB_OPTS["backup"] = bool(data.get("backup", True))
            if not JOB.start("npm", names):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/rollback/prune":
            # 只有使用者按鈕按下才來；days 限定在 0..365，0 = 全清
            try:
                days = int(data.get("days", ROLLBACK_MAX_AGE_DAYS))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "bad days"}, 400)
            if not 0 <= days <= 365:
                return self._json({"ok": False, "error": "bad days"}, 400)
            r = rollback_prune(None if days == 0 else days, everything=(days == 0))
            return self._json({"ok": True, **r})
        if path == "/api/rollback":
            job_id, name = str(data.get("job") or ""), str(data.get("name") or "")
            if not re.fullmatch(r"\d{8}T\d{6}", job_id) or (name and not re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._+-]*", name)):
                return self._json({"ok": False, "error": msg("rollback_not_found", LANG_DEFAULT)}, 400)
            ents = rollback_npm_entries(job_id, name or None)
            if ents:   # npm：沒有相依模擬，registry 直接重裝指定版本；照樣先展示變更再確認
                sim = {"ok": True, "npm": True, "changes": [{"name": e["name"], "action": "downgrade", "from": e.get("new") or "", "to": e["old"], "requested": True} for e in ents]}
                if data.get("simulate"):
                    return self._json(sim)
                _NPM_JOB_OPTS["backup"] = bool(data.get("backup", True))
                if not JOB.start("rollback_npm", [job_id, name]):
                    return self._json({"ok": False, "error": msg("job_running", LANG_DEFAULT)}, 409)
                return self._json({"ok": True})
            paths = [fp for fp, _ in rollback_deb_paths(job_id, name or None)]
            if not paths:
                return self._json({"ok": False, "error": msg("rollback_not_found", LANG_DEFAULT)}, 404)
            sim = rollback_simulate(paths)   # 降回也走「模擬 → 展示實際變更 → 確認」，會移除別的套件就不做
            if data.get("simulate") or not sim["ok"]:
                return self._json(sim, 200 if sim["ok"] else 400)
            if not JOB.start("rollback", [job_id, name]):
                return self._json({"ok": False, "error": msg("job_running", LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/autostart":
            try:
                return self._json(autostart_set(bool(data.get("enabled"))))
            except OSError as e:
                return self._json({"ok": False, "error": msg('autostart_write_failed', LANG_DEFAULT, p0=e)}, 500)
        if path == "/api/hardware/selftest":
            # {"action": "short"|"extended"|"abort"}：控制器自己跑，立刻返回；進度在 /api/hardware/diskcheck
            if data.get("action") not in ("short", "extended", "abort"):
                return self._json({"ok": False, "error": msg("node_bad_action", LANG_DEFAULT)}, 400)
            r = hardware.selftest_start(data["action"])
            return self._json(r, 200 if r.get("ok") else 409)
        if path == "/api/hardware/fsck":
            # {"schedule": true|false}：只動 tune2fs 的最大掛載次數，不重開機
            r = hardware.fsck_schedule(bool(data.get("schedule")))
            return self._json(r, 200 if r.get("ok") else 409)
        if path == "/api/hardware/usbc-map":
            # {"slot": 0-3, "controller": "NVDA8000:0x" | null, "note": "..."}；或 {"reset": true}
            if data.get("reset"):
                hardware.usbc_map_save({}); return self._json({"ok": True, "calib": {}})
            slot, ctrl = data.get("slot"), data.get("controller")
            if slot not in (0, 1, 2, 3):
                return self._json({"ok": False, "error": msg('slot_invalid', LANG_DEFAULT)}, 400)
            if ctrl is not None and not re.fullmatch(r"NVDA800[01]:0\d", str(ctrl)):
                return self._json({"ok": False, "error": msg('controller_invalid', LANG_DEFAULT)}, 400)
            m = hardware.usbc_map_load()
            if ctrl is None and not data.get("note"):
                m.pop(str(slot), None)
            else:
                m[str(slot)] = {"controller": ctrl, "note": (data.get("note") or "")[:80], "source": (data.get("source") or "manual")[:20],
                                "at": datetime.now().isoformat(timespec="seconds")}
            hardware.usbc_map_save(m)
            return self._json({"ok": True, "calib": m})
        if path == "/api/llm/switch":
            with _LLM_BENCH["lock"]:
                if _LLM_BENCH["state"]["status"] == "running":
                    return self._json({"ok": False, "error": msg('bench_running', self.lang)}, 409)
            try:
                if not model_capabilities.snapshot(force=True)['switch_control']['available']:
                    raise llm_switch.SwitchError('service_unconfigured')
                llm_switch.SWITCH.start(data.get('target'), data.get('model'), data.get('keep_alive', '5m'))
                return self._json({"ok": True, "switch": llm_switch.SWITCH.snapshot()}, 202)
            except llm_switch.SwitchError as exc:
                return self._json({"ok": False, "error": msg('llm_switch_' + exc.code, self.lang)}, 409)
        if (path in ("/api/llm/load", "/api/llm/unload", "/api/llm/bench", "/api/llm/bench_concurrent")
                or (path == "/api/disk/action" and data.get('action') == 'ollama_delete')):
            if llm_switch.SWITCH.snapshot()['status'] == 'running':
                return self._json({"ok": False, "error": msg('llm_switch_busy', self.lang)}, 409)
        if path == "/api/llm/stores/refresh":
            started = model_inventory.refresh()
            return self._json({"ok": True, "started": started})
        if path in ("/api/llm/load", "/api/llm/bench", "/api/llm/bench_concurrent"):
            model = data.get("model")
            if not isinstance(model, str) or not model:
                return self._json({"ok": False, "error": msg('missing_model', LANG_DEFAULT)}, 400)
            error, status = llm_admission(model, completion=path != "/api/llm/load")
            if error:
                return self._json({"ok": False, "error": error}, status)
        if path in ("/api/llm/load", "/api/llm/unload"):
            model = data.get("model")
            if not model or not isinstance(model, str):
                return self._json({"ok": False, "error": msg('missing_model', LANG_DEFAULT)}, 400)
            ka = "0" if path.endswith("unload") else (data.get("keep_alive") or "5m")
            try:
                llm_request(llm_endpoint() + "/api/generate", {"model": model, "keep_alive": ka, "stream": False}, timeout=600)
                return self._json({"ok": True})
            except RuntimeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 409)
        if path == "/api/llm/pull":
            model = data.get("model")
            if not model or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._/:-]+", model):
                return self._json({"ok": False, "error": msg('model_invalid', LANG_DEFAULT)}, 400)
            ok = JOB.start("ollama_pull", [model])
            return self._json({"ok": ok} if ok else {"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 200 if ok else 409)
        if path == "/api/llm/bench_concurrent":
            model, n = data.get("model"), data.get("concurrency", 2)
            if not model or n not in (1, 2, 4, 8):
                return self._json({"ok": False, "error": msg('concurrency_invalid', LANG_DEFAULT)}, 400)
            npred = data.get("num_predict", 128)
            if npred not in (64, 128, 256, 512):
                return self._json({"ok": False, "error": msg('num_predict_invalid', LANG_DEFAULT)}, 400)
            ok = llm_bench_start_concurrent(model, n, npred)
            return self._json({"ok": ok} if ok else {"ok": False, "error": msg('bench_running', LANG_DEFAULT)}, 200 if ok else 409)
        if path == "/api/llm/bench":
            model = data.get("model")
            if not model or not isinstance(model, str):
                return self._json({"ok": False, "error": msg('missing_model', LANG_DEFAULT)}, 400)
            npred = data.get("num_predict", 128)
            if npred not in (64, 128, 256, 512):
                return self._json({"ok": False, "error": msg('num_predict_invalid', LANG_DEFAULT)}, 400)
            if not llm_bench_start(model, npred):
                return self._json({"ok": False, "error": msg('bench_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/disk/action":
            act = data.get("action")
            if act == "ollama_delete":
                name = data.get("name")
                if not name or not isinstance(name, str):
                    return self._json({"ok": False, "error": msg('missing_name', LANG_DEFAULT)}, 400)
                req = urllib.request.Request("http://127.0.0.1:11434/api/delete", data=json.dumps({"name": name}).encode(),
                                             headers={"Content-Type": "application/json"}, method="DELETE")
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        r.read()
                except urllib.error.HTTPError as e:
                    return self._json({"ok": False, "error": msg('ollama_http', LANG_DEFAULT, p0=e.code, p1=e.read().decode(errors='replace')[:200])}, 502)
                except (urllib.error.URLError, OSError) as e:
                    return self._json({"ok": False, "error": msg('ollama_connect', LANG_DEFAULT, p0=e)}, 502)
                disk.invalidate()
                return self._json({"ok": True})
            if act == "apt_clean":
                ok = JOB.start("aptclean")
                return self._json({"ok": ok} if ok else {"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 200 if ok else 409)
            if act == "apt_autoremove":
                pk = disk.apt_autoremovable()
                if not pk:
                    return self._json({"ok": False, "error": msg('no_removable', LANG_DEFAULT)}, 400)
                return self._json({"ok": JOB.start("remove", pk), "packages": pk})
            if act in DISK_ACTIONS:
                return self._json({"ok": JOB.start("shell", [act])})
            return self._json({"ok": False, "error": msg('unknown_action', LANG_DEFAULT)}, 400)
        if path == "/api/apps/update":
            source = data.get("source"); ids = [i for i in data.get("ids", []) if isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9._+-]+", i)]
            if source not in ("flatpak", "snap") or not ids:
                return self._json({"ok": False, "error": msg('source_ids_invalid', LANG_DEFAULT)}, 400)
            if source == "snap":
                busy = {n: [x for x in snap_running_apps(n) if not x["service"]] for n in ids}   # 服務由 snapd 自己停啟，不擋
                busy = {n: v for n, v in busy.items() if v}
                if busy:
                    detail = "；".join(
                        f"{n}（{'、'.join(sorted({x['app'] for x in v}))}，PID {', '.join(str(x['pid']) for x in v[:4])}）"
                        for n, v in busy.items())
                    return self._json({"ok": False, "running": busy,
                                       "error": msg('snap_apps_running', LANG_DEFAULT, p0=detail)}, 409)
            if not JOB.start(source, ids):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/refresh":
            if not JOB.start("refresh"):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    process_label('spark-center')
    stability.start()
    threading.Thread(target=model_capabilities.snapshot, daemon=True).start()
    hardware.load_snapshot()   # 有快照就先用，服務重啟後硬體分頁不用等
    threading.Thread(target=hardware.fsck_state, daemon=True).start()   # 上次排程的開機檢查做完了就恢復設定並記錄結果
    # 服務重啟時 snapd 可能還在跑上一輪更新，接回來才不會讓畫面顯示成「沒事發生」
    try:
        out = _run(["snap", "changes"], timeout=20) or ""
        for line in out.splitlines():
            m = re.match(r'^(\d+)\s+(\S+)\s+.*?"([^"]+)"', line)
            if m and m.group(2) in SNAP_ACTIVE:
                print(f"attach to in-progress snap change {m.group(1)} ({m.group(3)})", flush=True)
                JOB.start("snap", [m.group(3)])
                break
    except Exception as e:
        print("snap attach skipped:", e, flush=True)
    threading.Thread(target=_disk_watch, daemon=True).start()
    threading.Thread(target=_gpu_watch, daemon=True).start()
    threading.Thread(target=_gpu_clock_reapply, daemon=True).start()
    threading.Thread(target=lambda: rollback_prune(ROLLBACK_MAX_AGE_DAYS), daemon=True).start()   # 過期的降回紀錄開機就清
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spark Center on http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
