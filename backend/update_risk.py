"""更新清單上的「上線幾天」與「NVIDIA 論壇有沒有討論這一版」。

純邏輯在這裡，網路抓取器可注入（測試不碰網路）；快取與背景執行緒仍在 server.py 的 changelog-meta。
拿不到的事實一律回 None／ok=False 並帶原因，不猜。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

FORUM_BASE = "https://forums.developer.nvidia.com"
FORUM_TOPIC_LIMIT = 5
FORUM_REFRESH = 6 * 3600       # 論壇結果 6 小時內不重抓
RETRY_AFTER = 3600             # 上線日或論壇抓失敗，1 小時後再試
ANNOUNCEMENT_RE = re.compile(r"^\d{3}(\.\d+)* release feedback & discussion$", re.I)   # NVIDIA 員工開的官方發布串（置頂），不是使用者回報
CUTOFF_SLACK = timedelta(days=1)   # 廠商 pool 檔案的時間戳可能比使用者第一次看到討論晚，往前多算一天
USER_AGENT = "spark-center"

_UPSTREAM_RE = re.compile(r"^(?:\d+:)?(\d+(?:\.\d+)+)")
_KERNEL_VER_RE = re.compile(r"^(?:\d+:)?(\d+\.\d+\.\d+-\d+)")


def _kernel_term(name, version):
    m = _KERNEL_VER_RE.match(version)
    return m.group(1) if m else None


def _upstream_term(name, version):
    m = _UPSTREAM_RE.match(version)
    return m.group(1) if m else None


def _dgx_term(name, version):
    up = _upstream_term(name, version)
    return f"{name} {up}" if up else None


# 依序比對，第一個命中的建構器決定搜尋詞；都不中＝不是 Spark 專屬套件，不查論壇。
FORUM_TERMS = [
    (re.compile(r"^(linux-image-nvidia|linux-nvidia|linux-headers-nvidia|linux-image-\d[\d.]*-nvidia)"), _kernel_term),
    (re.compile(r"^linux-(modules|tools|headers|image)-.*nvidia"), _kernel_term),
    (re.compile(r"^(dgx-|nvidia-dgx|nvidia-spark-)"), _dgx_term),   # nvidia-spark-*：initcall-bl／grub-pci／fstab-config 這類 Spark 專屬設定包
    (re.compile(r"^(nvidia-driver|nvidia-kernel|nvidia-dkms|nvidia-utils|libnvidia-|nvidia-.*-open|"
                r"xserver-xorg-video-nvidia|nvidia-firmware|nvidia-compute|nvidia-settings)"), _upstream_term),
]


def forum_term(name, candidate):
    for rx, build in FORUM_TERMS:
        if rx.match(name):
            return build(name, candidate)
    return None


def forum_url(term):
    return f"{FORUM_BASE}/search?q={urllib.parse.quote(term, safe='')}"


def parse_last_modified(value):
    """HTTP Last-Modified → UTC aware datetime；缺或壞 → None。"""
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def days_since(published_iso, now=None):
    """已上線整天數（向下取整）；時鐘比上線日早時當 0。"""
    d = _parse_iso(published_iso)
    if d is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - d).days)


def filter_topics(topics, published_iso):
    """留下上線日前一天以後建立的主題，新到舊。published 未知 → 全留。
    回 (討論, 公告)：標題是「620 release feedback & discussion」這種官方發布串的歸公告，不算成「有人在討論這版」；
    搜尋詞是版本字串，公告一定命中，不分開就每一版驅動都會亮紅。"""
    cut = _parse_iso(published_iso)
    cut = cut - CUTOFF_SLACK if cut else None
    out, ann = [], []
    for t in topics:
        created = _parse_iso(t.get("created_at"))
        if created is None:
            continue
        if cut is None or created >= cut:
            row = {"id": t.get("id"), "title": t.get("title", ""), "created": _iso(created),
                   "posts": t.get("posts_count", 0), "category_id": t.get("category_id")}
            (ann if ANNOUNCEMENT_RE.match(row["title"].strip()) else out).append(row)
    out.sort(key=lambda x: x["created"], reverse=True)
    ann.sort(key=lambda x: x["created"], reverse=True)
    return out, ann


def topic_url(topic_id):
    return f"{FORUM_BASE}/t/{int(topic_id)}"


def http_head(url):
    """回 (HTTP 狀態碼, Last-Modified 或 None)；連不上就丟例外。跟隨轉址。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        return e.code, None


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _err_note(e):
    s = str(e)
    if isinstance(e, TimeoutError) or "timed out" in s:
        return "timeout"
    return (s or type(e).__name__)[:120]


def _fetch_published(uri, head):
    if not uri:
        return None, "no uri"
    try:
        status, lm = head(uri)
    except Exception as e:
        return None, _err_note(e)
    if status != 200:
        return None, f"HTTP {status}"
    d = parse_last_modified(lm)
    return (_iso(d), None) if d else (None, "no Last-Modified")


def _fetch_forum(term, published, get_json, now):
    out = {"term": term, "url": forum_url(term), "ts": now}
    try:
        data = get_json(f"{FORUM_BASE}/search.json?q={urllib.parse.quote(term, safe='')}")
        if not isinstance(data, dict):
            raise ValueError("unexpected response")
        topics, ann = filter_topics(data.get("topics") or [], published)
    except Exception as e:
        return {**out, "ok": False, "note": _err_note(e), "total": 0, "topics": [], "announcement": None}
    announcement = {**ann[0], "url": topic_url(ann[0]["id"])} if ann and ann[0].get("id") is not None else None
    return {**out, "ok": True, "note": None, "total": len(topics), "topics": topics[:FORUM_TOPIC_LIMIT], "announcement": announcement}


def risk_is_stale(prev, name, candidate, now=None):
    """prev 缺或過期 → True。判斷規則和 fetch_risk 的重抓規則是同一份。"""
    now = now if now is not None else time.time()
    if not prev:
        return True
    age = now - prev.get("ts", 0)
    if prev.get("published") is None and age > RETRY_AFTER:
        return True
    if forum_term(name, candidate) is None:
        return False
    f = prev.get("forum")
    if not f:
        return True
    return now - f.get("ts", 0) > (FORUM_REFRESH if f.get("ok") else RETRY_AFTER)


def fetch_risk(name, candidate, uri, prev_risk=None, http_head=http_head, http_get_json=http_get_json, now=None):
    now = now if now is not None else time.time()
    prev = prev_risk or {}
    published, note = prev.get("published"), None       # 上線日取到後不會變，不重抓
    if published is None:
        published, note = _fetch_published(uri, http_head)
    term = forum_term(name, candidate)
    forum = None
    if term:
        old = prev.get("forum")
        keep = (old and old.get("term") == term and prev.get("published") == published
                and now - old.get("ts", 0) <= (FORUM_REFRESH if old.get("ok") else RETRY_AFTER))
        forum = old if keep else _fetch_forum(term, published, http_get_json, now)
    return {"ts": now, "published": published, "published_note": note, "forum": forum}
