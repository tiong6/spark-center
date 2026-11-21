"""本機 HTTP 請求是哪個程序發的：用連線的來源埠反查 /proc/net/tcp 的 socket inode，再找持有它的程序與父程序鏈。

為什麼要有這個：2026-11-21 一批 14 個套件的安裝在使用者說「沒選要更新」時開始了，服務 log 卻不記成功的請求，
查不出是 App 視窗、別的瀏覽器分頁、還是某個 agent 的 curl／python 打的 API。只看得到同一使用者的程序
（別的使用者的 /proc/<pid>/fd 讀不到）→ 找不到就回空串列，呼叫端照實寫「查不到」，不猜。
"""
import os


def _socket_inode(client_port, server_port, proc):
    """客戶端那一側的 socket：local＝127.0.0.1:client_port、remote＝…:server_port。"""
    want_l, want_r = f"{client_port:04X}", f"{server_port:04X}"
    try:
        with open(os.path.join(proc, "net", "tcp")) as f:
            next(f)
            for line in f:
                cols = line.split()
                if len(cols) > 9 and cols[1].split(":")[1] == want_l and cols[2].split(":")[1] == want_r:
                    return cols[9]
    except (OSError, StopIteration, IndexError):
        pass
    return None


def _owner(inode, proc):
    target = f"socket:[{inode}]"
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, fd)) == target:
                        return int(pid)
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


def chain(client_port, server_port, proc="/proc", depth=5):
    """回 [{pid, comm, ppid, args}, …]，第一個是持有連線的程序，往上最多 depth 層（到 pid 1 為止）。查不到回 []。"""
    inode = _socket_inode(client_port, server_port, proc)
    pid = _owner(inode, proc) if inode and inode != "0" else None
    out = []
    while pid and pid > 1 and len(out) < depth:
        i = _info(pid, proc)
        if not i:
            break
        out.append(i)
        pid = i["ppid"]
    return out


def describe(procs, width=120):
    """一行人看得懂的：「1234 chrome --user-data-dir=… ← 1200 chrome ← …」。Chrome 的參數太長，只留辨識用的幾個。"""
    parts = []
    for p in procs:
        keep = [a for a in p["args"][1:] if a.startswith(("--user-data-dir", "--app-id", "--type=")) or not a.startswith("-")]
        s = f"{p['pid']} {p['comm']}" + (" " + " ".join(keep) if keep else "")
        parts.append(s[:width])
    return " ← ".join(parts)
