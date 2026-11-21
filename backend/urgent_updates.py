"""急迫更新的主動提示：從更新清單與 changelog 急迫度挑出 high 以上的，決定哪些還沒發過桌面通知。純邏輯，不碰 apt、網路、執行緒。

為什麼要有這個：急迫度原本只在打開更新分頁時才抓，使用者沒開就不知道。背景每輪算一次，
同一個「套件＋候選版本」只通知一次（記在 data/urgent-notified.json），頂欄徽章則每次照目前清單畫。
門檻與更新分頁的紅色急迫度標籤相同（updates.js clMetaHtml 的 'reboot' 樣式）；急迫度是 Debian 維護者在
changelog 標頭寫的判斷，不是這台機器的風險評估，抓不到 changelog 的套件（第三方倉庫）不算，不猜。
"""
import json
import os

LEVELS = ("high", "critical", "emergency")


def collect(items, meta):
    """items：list_updates() 的項目；meta：updates_meta() 回的 {name: 快取項或 None}。
    回急迫度在 LEVELS 內的項目，依名稱排序：{name, candidate, urgency, summary, cves}。"""
    out = []
    for it in items:
        m = meta.get(it["name"]) or {}
        if m.get("ok") and m.get("urgency") in LEVELS:
            out.append({"name": it["name"], "candidate": it["candidate"], "urgency": m["urgency"],
                        "summary": m.get("summary") or "", "cves": m.get("cves") or 0})
    out.sort(key=lambda x: x["name"])
    return out


def key(u):
    return f"{u['name']} {u['candidate']}"


def unnotified(urgent, notified):
    return [u for u in urgent if key(u) not in notified]


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return set()
    return set(v) if isinstance(v, list) else set()


def save(path, keys):
    """只留目前還在清單上的鍵：裝掉的就忘掉，檔案不會一直長。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, ensure_ascii=False)
    os.replace(tmp, path)


def names_text(urgent, limit=4, sep="、"):
    """通知內文用：前幾個名字，其餘以「等」帶過由呼叫端的訊息處理（這裡只回名字串與是否截斷）。"""
    names = [u["name"] for u in urgent]
    return sep.join(names[:limit]), len(names) > limit
