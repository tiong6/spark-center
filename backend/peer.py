"""本機 HTTP 請求是哪個程序發的：用連線的來源埠反查 /proc/net/tcp 的 socket inode，再找持有它的程序與父程序鏈。

為什麼要有這個：2026-11-21 一批 14 個套件的安裝在使用者說「沒選要更新」時開始了，服務 log 卻不記成功的請求，
查不出是 App 視窗、別的瀏覽器分頁、還是某個 agent 的 curl／python 打的 API。只看得到同一使用者的程序
（別的使用者的 /proc/<pid>/fd 讀不到）→ 找不到就回空串列，呼叫端照實寫「查不到」，不猜。
"""
import os
import re
import socket
import time

SCAN_MAX_LINKS = 50000     # /proc/*/fd 最多讀這麼多個連結，或 SCAN_MAX_SECONDS，先到先停：查不到就回「查不到」，不讓一次查詢拖住服務
SCAN_MAX_SECONDS = 0.3
_INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "dash", "perl", "ruby"}
_SCRIPT_EXT = (".py", ".js", ".mjs", ".cjs", ".sh", ".pl", ".rb")
_CHROME = {"chrome", "google-chrome", "chromium", "chromium-browser", "brave", "msedge"}
_KEEP_FLAGS = ("--user-data-dir=", "--app-id=", "--type=")


def _hex4(ip, port):
    """/proc/net/tcp 的 IPv4 寫法：位址是 32 位元整數的主機位元組序（x86／arm64 小端）十六進位，埠是大端。"""
    return socket.inet_aton(ip)[::-1].hex().upper() + f":{port:04X}"


def _hex6_mapped(ip, port):
    """同一條 IPv4 連線、客戶端用 IPv6 socket（::ffff:127.0.0.1）時，它出現在 /proc/net/tcp6，位址每 4 位元組一組小端。"""
    raw = socket.inet_pton(socket.AF_INET6, "::ffff:" + ip)
    return "".join(raw[i:i + 4][::-1].hex().upper() for i in range(0, 16, 4)) + f":{port:04X}"


def _client_rows(client, server, proc):
    """客戶端那一側的 socket：local＝client (ip, port)、remote＝server (ip, port)，完整四元組比對。
    只比埠號不夠：別的帳號可以從 127.0.0.2 用同一個來源埠連進來（2026-11-21 Codex 第三輪重現）。inode 0（TIME_WAIT）不算。"""
    rows = []
    for name, enc in (("tcp", _hex4), ("tcp6", _hex6_mapped)):
        want_l, want_r = enc(*client), enc(*server)
        try:
            with open(os.path.join(proc, "net", name)) as f:
                next(f)
                for line in f:
                    cols = line.split()
                    if len(cols) > 9 and cols[1] == want_l and cols[2] == want_r and cols[9] != "0":
                        rows.append(cols)
        except (OSError, StopIteration, ValueError):
            continue
    return rows


def _socket_inode(client, server, proc):
    rows = _client_rows(client, server, proc)
    return rows[0][9] if len(rows) == 1 else None


def socket_uid(client, server, proc="/proc"):
    """發出這條連線的帳號 UID。client／server 是 (ip, port)：伺服器端拿 self.client_address 與 connection.getsockname()。
    /proc/net/tcp 的 uid 欄對所有帳號都看得到，不用讀別人的 /proc/<pid>/fd。
    查不到或比對到不只一列都回 None（呼叫端要當成「不是自己」拒絕，fail closed）。
    限制：這是事後讀 /proc 的快照，不是 Unix socket 那種核心給的對端憑證；同帳號的程序替別人轉送的請求也會被當成自己。"""
    rows = _client_rows(client, server, proc)
    try:
        return int(rows[0][7]) if len(rows) == 1 else None
    except ValueError:
        return None


def _owner(inode, proc):
    target = f"socket:[{inode}]"
    # 盡力而為的預算：每個 PID、每個 fd 都扣、都看時間（讀不到的目錄也算）；scandir 是逐筆列舉，不會先把十萬個 fd 讀成清單
    deadline, budget = time.monotonic() + SCAN_MAX_SECONDS, SCAN_MAX_LINKS
    with os.scandir(proc) as pids:
        for pe in pids:
            budget -= 1
            if budget <= 0 or time.monotonic() > deadline:
                return None
            if not pe.name.isdigit():
                continue
            try:
                with os.scandir(os.path.join(proc, pe.name, "fd")) as fds:
                    for fe in fds:
                        budget -= 1
                        if budget <= 0 or time.monotonic() > deadline:
                            return None
                        try:
                            if os.readlink(fe.path) == target:
                                return int(pe.name)
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def _info(pid, proc):
    try:
        with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
            args = f.read().split(b"\0")
        with open(os.path.join(proc, str(pid), "stat")) as f:
            stat = f.read()
    except OSError:
        return None
    comm = stat[stat.index("(") + 1:stat.rindex(")")]
    ppid = int(stat[stat.rindex(")") + 2:].split()[1])
    return {"pid": pid, "comm": comm, "ppid": ppid, "args": [a.decode(errors="replace") for a in args if a]}


def chain(client, server, proc="/proc", depth=5):
    """回 [{pid, comm, ppid, args}, …]，第一個是持有連線的程序，往上最多 depth 層（到 pid 1 為止）。查不到回 []。
    client／server 是 (ip, port)。"""
    inode = _socket_inode(client, server, proc)
    pid = _owner(inode, proc) if inode and inode != "0" else None
    out = []
    while pid and pid > 1 and len(out) < depth:
        i = _info(pid, proc)
        if not i:
            break
        out.append(i)
        pid = i["ppid"]
    return out


def clean(s, width=120):
    """寫進 journal／工作卡的不可信字串：控制字元（C0、DEL、C1 如 U+0085，以及 U+2028／2029 行分隔，換行可偽造一行假紀錄）轉義，再截長度。"""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}", str(s))[:width]


def _label(p):
    """程序的辨識字串。不收任意參數：指令列常帶 token／密碼（curl -H 'Authorization: Bearer …'、-d 的內容），
    寫進紀錄就外洩。只留：程序名、Chrome 用來分辨 profile 的幾個旗標、直譯器跑的腳本檔名（-c／-e 的行內程式只寫「行內程式」）。"""
    args = p["args"][1:]
    keep = [a for a in args if a.startswith(_KEEP_FLAGS)] if p["comm"] in _CHROME else []
    if p["comm"] in _INTERPRETERS or os.path.basename(p["args"][0] if p["args"] else "") in _INTERPRETERS:
        # 只在「緊接直譯器的第一個參數就是腳本檔」時記檔名；有任何選項（-c、-lc、-W x、-m mod…）就無法可靠判斷哪個是腳本，一律不記
        if args and not args[0].startswith("-") and args[0].endswith(_SCRIPT_EXT):
            keep.append(os.path.basename(args[0]))
        elif args:
            keep.append("<args>")
    return f"{p['pid']} {p['comm']}" + (" " + " ".join(keep) if keep else "")


def describe(procs, width=120):
    """一行人看得懂的：「1234 chrome --user-data-dir=… ← 1200 bash ← …」。"""
    return " ← ".join(clean(_label(p), width) for p in procs)
