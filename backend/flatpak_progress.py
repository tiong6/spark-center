"""flatpak CLI 輸出 → 進度。純函式，server 只負責把每一行餵進來。

前提（flatpak 1.14.6 實測）：不加 --noninteractive、C 語系、stdout 接 pipe 時，CLI 先印一張操作表
（每列一項：序號、ref、分支、操作、來源、下載大小上限 "< 16.4 MB"），之後每項印
"Installing 2/6…" 與 "Installing 2/6… ███▏ 61%" 這種進度行，每行即時送出。
只有一項時沒有 "2/6"。下載大小是上限（本機已有的物件不必再抓），所以總量一律說「最多」。
"""
import re

ROW = re.compile(r"^\s*(\d+)\.\t[^\t]*\t([^\t]+)\t([^\t]*)\t([^\t]+)\t[^\t]*(?:\t(.*))?$")
SIZE = re.compile(r"([\d.]+)\s*(bytes|kB|MB|GB|TB)")
PROGRESS = re.compile(r"^(?:Installing|Updating|Uninstalling)(?: (\d+)/(\d+))?…(?:.*?(\d+)%)?")
UNITS = {"bytes": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def parse_size(text):
    """'< 16.4\\xa0MB' → (16400000, '16.4 MB')；沒有大小（例如移除）→ (0, None)。"""
    m = SIZE.search((text or "").replace("\xa0", " "))
    if not m:
        return 0, None
    return int(float(m.group(1)) * UNITS[m.group(2)]), f"{m.group(1)} {m.group(2)}"


def format_size(n):
    """和 GLib g_format_size 同一套十進位單位，數字對得上 flatpak 自己印的。"""
    for unit, f in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            return f"{n / f:.1f} {unit}"
    return f"{n} bytes"


class Tracker:
    def __init__(self):
        self.ops = []        # [(ref, branch, bytes, size_text)]，照操作表順序
        self.current = None  # 第幾項（1 起算）
        self.percent = 0

    def feed(self, line):
        """回 'row'（操作表的一列）、'progress'（帶百分比的進度行，不必進記錄）、'item'（換下一項）或 None。"""
        m = ROW.match(line)
        if m and self.current is None:
            size, text = parse_size(m.group(5))
            self.ops.append((m.group(2), m.group(3), size, text))
            return "row"
        m = PROGRESS.match(line)
        if not m:
            return None
        index = int(m.group(1)) if m.group(1) else 1
        if index != self.current:
            self.current, self.percent = index, 0
        if m.group(3) is None:
            return "item"
        self.percent = min(100, int(m.group(3)))
        return "progress"

    def snapshot(self):
        """{'total','count','index','ref','size','progress'}；還沒開始 → None。progress 依各項下載上限加權。"""
        if not self.ops:
            return None
        total = sum(op[2] for op in self.ops)
        out = {"total": format_size(total) if total else None, "count": len(self.ops),
               "index": None, "ref": None, "size": None, "progress": None}
        if self.current is None or self.current > len(self.ops):
            return out
        ref, branch, size, size_text = self.ops[self.current - 1]
        done_before = self.ops[:self.current - 1]
        if total:
            done = sum(op[2] for op in done_before) + size * self.percent / 100
            progress = done * 100 / total
        else:   # 每項都沒有大小（例如全是移除）：改按項數平均
            progress = (len(done_before) + self.percent / 100) * 100 / len(self.ops)
        out.update(index=self.current, ref=f"{ref} {branch}".strip(), size=size_text, progress=int(progress))
        return out
