"""Ubuntu 的版本升級提示：/etc/update-manager/release-upgrades 的 Prompt=。

判斷照 Ubuntu 自己的讀法（UpdateManager/Core/MetaRelease.py 的 MetaReleaseCore.__init__ 與 parse()），
並且用同一個 configparser 解析，不自己猜格式：DGX OS 沒有官方的就地升級路徑，讀錯就會漏報會跳出來的升級視窗。
只用標準函式庫；不 import server／apt／apt_pkg。distro_info 有就用，沒有就退回 os-release。
公開函式都不丟例外（compose 例外：結果驗證不過時丟 ValueError，呼叫端回報、不寫檔）。
"""
import configparser
import email.utils
import io
import os
import platform
import stat
import time
from datetime import datetime

try:
    import distro_info
except ImportError:   # 不是每台都有；沒有就退回 os-release 判斷 LTS
    distro_info = None

# 路徑都在呼叫時才讀，測試可以直接換掉模組屬性
CONF = "/etc/update-manager/release-upgrades"
META_CONF = "/etc/update-manager/meta-release"
CACHE_DIR = "/var/lib/update-manager"
DEFAULT_URI = "https://changelogs.ubuntu.com/meta-release"
DEFAULT_URI_LTS = "https://changelogs.ubuntu.com/meta-release-lts"
FEED_MAX = 1 << 20        # 清單快取在使用者可寫的 ~/.cache：超過就不讀
VERSION_MAX = 64
CODENAME_MAX = 32

# 結構判斷直接借 configparser 自己的規則式，compose 才會和 Ubuntu 讀到的行對得上
_SECT = configparser.ConfigParser.SECTCRE
_OPT = configparser.ConfigParser.OPTCRE
_NONSPACE = configparser.ConfigParser.NONSPACECRE


def _err(e):
    return " ".join(str(e).split())[:200] or type(e).__name__


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _mtime(path):
    try:
        return _iso(os.stat(path).st_mtime)
    except (OSError, OverflowError, ValueError):
        return None


def effective(text):
    """(mode, raw, default_used, error)：Ubuntu 讀這份內容會得到什麼。text None＝檔案不存在。
    mode：never／lts／normal，或 malformed（Ubuntu 讀這份會出錯、不跳視窗：configparser 的格式錯誤讓 do-release-upgrade 卡住、
    軟體更新一直等；值裡的 %、不是 UTF-8 則讓它們直接當掉）。"""
    if text is None:
        return "normal", None, True, None   # 沒有檔案：Ubuntu 用預設的 meta-release 清單，任何新版都提示
    raw = None
    try:
        p = configparser.ConfigParser()
        p.read_file(io.StringIO(text, newline=None), source="release-upgrades")   # newline=None：和 open() 一樣認 \r\n 與 \r
        if not p.has_option("DEFAULT", "Prompt"):
            return "normal", None, True, None   # 只看 [DEFAULT]；別的段落裡的 Prompt Ubuntu 看不到
        raw = p.get("DEFAULT", "Prompt", raw=True)
        value = p.get("DEFAULT", "Prompt")      # 值裡的 % 在這裡丟例外；Ubuntu 沒接，檢查程式整個當掉
    except Exception as e:
        return "malformed", raw, False, _err(e)
    v = value.lower()
    if v in ("never", "no"):
        return "never", value, False, None
    if v == "lts":
        return "lts", value, False, None
    return "normal", value, False, None   # 其他任何值（空字串、false、同一行後面的註解）Ubuntu 都當 normal


def _load(path):
    """('missing'|'unreadable'|'undecodable'|'ok', 內容, 錯誤)。configparser 的 read() 開不了檔會安靜跳過，
    所以「讀不到」要和「不存在」分開回報：對 Ubuntu 兩者一樣，對使用者不一樣。
    只有 FileNotFoundError 算不存在：os.path.exists 在上層目錄進不去時也回 False，照它判斷，改寫時會把讀不到的檔整份蓋掉。
    只讀一般檔、開檔不阻塞：FIFO 會讓 /api/updates 永遠卡住，指到 /dev/zero 的連結會讀到記憶體用完。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing", None, None
    except OSError as e:
        return "unreadable", None, _err(e)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "unreadable", None, "not a regular file"
        with open(fd, "rb", closefd=False) as f:
            data = f.read()
    except OSError as e:
        return "unreadable", None, _err(e)
    finally:
        os.close(fd)
    try:
        return "ok", data.decode("utf-8"), None
    except UnicodeDecodeError as e:   # Ubuntu 的 configparser 在這裡丟的不是 configparser.Error，檢查程式直接當掉
        return "undecodable", None, _err(e)


def read_conf():
    """(text, error)：(None, None)＝檔案不存在；(None, 錯誤)＝讀不到或不是 UTF-8，不能安全改寫。"""
    _state, text, err = _load(CONF)
    return text, err


def status():
    """給 /api/updates 與常見問題的「本機」框：生效值、會不會跳視窗、Ubuntu 上次查到要提示哪一版。"""
    path = CONF
    out = {"file": path, "exists": False, "readable": False, "mtime": None, "raw": None, "mode": None,
           "default_used": False, "popup": False, "offered": None, "error": None}
    try:
        state, text, err = _load(path)
        out["exists"] = state != "missing"
        out["readable"] = state in ("ok", "undecodable")
        if out["exists"]:
            out["mtime"] = _mtime(path)
        if state == "ok":
            mode, raw, default_used, err = effective(text)
        elif state == "undecodable":
            mode, raw, default_used = "malformed", None, False
        else:   # 不存在或讀不到：Ubuntu 用同一個使用者的身分讀，同樣當作沒有設定檔
            mode, raw, default_used = "normal", None, True
        out.update(mode=mode, raw=raw, default_used=default_used, error=err, popup=mode in ("lts", "normal"))
        if out["popup"]:
            out["offered"] = offered(mode)
    except Exception as e:   # 不讓這裡拖垮 /api/updates；mode 留 None，前端顯示「—」而不是猜一個
        out.update(mode=None, popup=False, offered=None, error=_err(e))
    return out


def _codename():
    fake = os.environ.get("META_RELEASE_FAKE_CODENAME")   # Ubuntu 的 get_dist() 也認這個除錯用變數
    if fake:
        return fake
    return platform.freedesktop_os_release().get("VERSION_CODENAME", "unknown distribution")


def _is_lts(codename):
    if distro_info is None:   # Ubuntu 的 MetaRelease.py 少了 distro_info 根本跑不起來；這裡只求不壞
        osr = platform.freedesktop_os_release()
        return osr.get("VERSION_CODENAME") == codename and "LTS" in osr.get("VERSION", "")
    return distro_info.UbuntuDistroInfo().is_lts(codename)


def _feed_uri(mode, codename):
    """MetaReleaseCore 下載的清單網址。/etc/update-manager/meta-release 可以改網址（快取檔名跟著變）；
    Prompt=lts 而且正在跑的是 LTS 才換成 LTS 清單。這個檔讀不了 Ubuntu 會整個放棄檢查，這裡就讓例外往上丟。"""
    uri, uri_lts = DEFAULT_URI, DEFAULT_URI_LTS
    if os.path.exists(META_CONF):
        p = configparser.ConfigParser()
        p.read(META_CONF, encoding="utf-8")
        if p.has_section("METARELEASE"):
            if p.has_option("METARELEASE", "URI"):
                uri = p.get("METARELEASE", "URI")
            if p.has_option("METARELEASE", "URI_LTS"):
                uri_lts = p.get("METARELEASE", "URI_LTS")
    if mode == "lts" and _is_lts(codename):
        uri = uri_lts
    return uri


def _deb822(text):
    """apt TagFile 的子集：空行分段、「Key: value」、空白開頭的是上一欄的續行。欄位名不分大小寫。
    只有空白的行 apt 不當分段（前後兩段會併成一段），這裡跳過；檔案第一行之後、還沒有任何一段就遇到空行，
    apt 會切出一段空的，parse() 因缺欄位整份作廢。看不懂的行回 None：apt 會整份解析失敗。
    其他和 apt 不同的寫法（例如一段開頭就縮排）這裡一律回 None：寧可不說會提示哪一版，也不說錯。"""
    out, cur, last = [], {}, None
    for i, line in enumerate(text.split("\n")):
        line = line.rstrip("\r")
        if not line:
            if cur:
                out.append(cur)
            elif i and not out:
                return None
            cur, last = {}, None
            continue
        if not line.strip():
            continue
        if line[0] in " \t":
            if last is None:
                return None
            cur[last] += "\n" + line.strip()
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return None
        last = key.strip().lower()
        cur[last] = value.strip()
    if cur:
        out.append(cur)
    return out


def _pick(text, codename, path, mtime):
    """照 MetaReleaseCore.parse()：每段都要有 Dist／Version／Supported／Date，缺一個整份作廢；
    清單裡找不到正在跑的版本就不提示；否則依檔案順序，第一個日期比目前新、而且 Supported 不是 0 的就是要提示的版本。"""
    stanzas = _deb822(text)
    if stanzas is None:
        return None
    dists, current = [], None
    for s in stanzas:
        if any(k not in s for k in ("dist", "version", "supported", "date")):
            return None
        parsed = email.utils.parsedate(s["date"])
        if parsed is None:
            return None
        parsed = list(parsed)
        parsed[8] = 0   # 和 Ubuntu 一樣假設沒有日光節約
        d = (s["dist"], s["version"], time.mktime(tuple(parsed)), int(s["supported"]))
        dists.append(d)
        if d[0] == codename:
            current = d
    if current is None:
        return None
    for name, version, date, supported in dists:
        if date > current[2] and supported:
            return {"version": version[:VERSION_MAX], "codename": name[:CODENAME_MAX], "as_of": _iso(mtime), "source": path}
    return None


def offered(mode):
    """Ubuntu 上次下載的清單裡會提示哪一版：{version, codename, as_of, source}；算不出來就 None。
    快取位置照 _buildMetaReleaseFile：/var/lib/update-manager/ 與 $XDG_CACHE_HOME（預設 ~/.cache）/update-manager-core/，
    取最新、讀得到的那份。清單是使用者可寫的檔，內容當不可信文字（截長度，前端 esc）。
    Prompt=never 之後沒人會再更新它，所以一定附上檔案時間當「截至」。"""
    try:
        if mode not in ("lts", "normal"):
            return None
        codename = _codename()
        base = os.path.basename(_feed_uri(mode, codename))
        if not base:
            return None
        user_dir = os.path.join(os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "update-manager-core")
        found = []
        for path in (os.path.join(CACHE_DIR, base), os.path.join(user_dir, base)):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:   # 空檔 Ubuntu 自己也會刪掉，不算數
                found.append((st.st_mtime, path))
        for mtime, path in sorted(found, reverse=True):
            try:
                with open(path, "rb") as f:
                    data = f.read(FEED_MAX + 1)
            except OSError:
                continue
            if len(data) > FEED_MAX:
                return None
            # 不是 UTF-8 就不說是哪一版：壞位元組落在 Ubuntu 會讀的欄位（Version 等）它整份作廢，落在 Description 之類它照樣提示，往少說
            return _pick(data.decode("utf-8"), codename, path, mtime)
        return None
    except Exception:
        return None


def _eol(line):
    for e in ("\r\n", "\n", "\r"):
        if line.endswith(e):
            return e
    return ""


def _roles(lines):
    """每一行在 configparser 眼中是什麼（照 RawConfigParser._read 的判斷順序）：(角色, 所在段落, 選項名)。
    角色：comment／blank／section／option／cont（續行，選項名是它併進去的那個選項）／bad（configparser 會記錯誤）。"""
    out, sect, opt, indent = [], None, None, 0
    for line in lines:
        if line.strip().startswith(("#", ";")):
            out.append(("comment", sect, None))
            continue
        value = line.strip()
        if not value:
            out.append(("blank", sect, None))
            continue
        m = _NONSPACE.search(line)
        cur = m.start() if m else 0
        if sect is not None and opt and cur > indent:
            out.append(("cont", sect, opt))
            continue
        indent = cur
        m = _SECT.match(value)
        if m:
            sect, opt = m.group("header"), None
            out.append(("section", sect, None))
        elif sect is None:
            out.append(("bad", None, None))
        else:
            m = _OPT.match(value)
            if m:
                opt = m.group("option").rstrip().lower()
                out.append(("option", sect, opt))
            else:
                out.append(("bad", sect, None))   # configparser 記下錯誤繼續讀，選項名不變
    return out


def _others(text):
    """除了 [DEFAULT] 的 Prompt 以外的所有設定（段落、選項、原始值）；讀不了回 None。用來確認改寫沒動到別的東西。
    DEFAULT 當一般段落看、不做內插、重複不算錯，原本就有問題的檔也比得起來。"""
    p = configparser.RawConfigParser(strict=False, default_section="\x00")
    try:
        p.read_file(io.StringIO(text or "", newline=None))
    except configparser.Error:
        return None
    return {(s, k, v) for s in p.sections() for k, v in p.items(s, raw=True) if (s, k) != ("DEFAULT", "prompt")}


def compose(text, mode):
    """把 Prompt 改成 never 或 lts 的新內容；其他行逐字保留（含換行字元）。text None＝檔案不存在。
    [DEFAULT] 裡第一個 Prompt 行換掉（保留縮排與換行字元），之後的 Prompt 行和這些行的續行刪掉；
    沒有 Prompt 就插在 [DEFAULT] 標題下一行，連標題都沒有就在最後補一段。標題之前就有內容的檔（只寫了 Prompt=never）
    當成 DEFAULT、補上標題。最後用 effective() 驗證 Ubuntu 會讀成 mode、別的設定都沒變，否則丟 ValueError、不給結果。"""
    if mode not in ("never", "lts"):
        raise ValueError("mode must be never or lts")
    line_new = "Prompt=" + mode
    if text is None:
        result = "[DEFAULT]\n" + line_new + "\n"
    else:
        lines = io.StringIO(text, newline="").readlines()
        eol = next((e for e in map(_eol, lines) if e), "\n")
        roles = _roles(lines)
        if any(r == ("bad", None, None) for r in roles):
            lines = ["[DEFAULT]" + eol] + lines
            roles = _roles(lines)
        out, done, drop = [], False, False
        for line, (role, sect, opt) in zip(lines, roles):
            if role == "option":
                drop = sect == "DEFAULT" and opt == "prompt"
                if drop:
                    if not done:
                        lead = line[:len(line) - len(line.lstrip())]   # 縮排決定後面的行算不算續行，照留
                        out.append(lead + line_new + _eol(line))
                        done = True
                    continue
            elif role == "cont" and drop:
                continue
            elif role == "section":
                drop = False
            out.append(line)
        if not done:   # 沒有任何 Prompt 行被換掉：out 和 lines 一樣，roles 的索引照用
            hdr = next((i for i, r in enumerate(roles) if r[:2] == ("section", "DEFAULT")), None)
            if hdr is not None:
                if not _eol(out[hdr]):
                    out[hdr] += eol
                out.insert(hdr + 1, line_new + eol)
            else:
                tail = "".join(out)
                out = [tail + (eol if tail else "") + "[DEFAULT]" + eol + line_new + eol]
        result = "".join(out)
    got = effective(result)[0]
    if got != mode:
        raise ValueError(f"composed text reads as {got}, not {mode}")
    before = _others(text)
    if before is not None and _others(result) != before:
        raise ValueError("composed text changes other settings")
    return result
