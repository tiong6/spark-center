"""背景自動重新整理 apt 索引：設定的讀寫與「這一輪該不該跑」的判斷。純邏輯，不碰 apt、不碰執行緒。

為什麼要有這個：Ubuntu 的 apt-daily.timer 一天只更新索引一次（06:00／18:00 加最多 12 小時隨機延遲），
更新分頁只讀本機索引，清單最晚可能落後一天。這裡以使用者選的間隔補跑 apt update（走和「重新整理來源」同一條 aptdaemon 路）。
「上次更新」認 apt 自己的 update-success-stamp，所以手動按鈕、apt-daily 跑過都會重新計時，不會重複抓。
"""
import json
import os

CHOICES = (0, 1, 6, 12, 24)   # 小時；0 = 關閉。順序就是下拉選單的順序
DEFAULT_HOURS = 6             # 與 Spark Center 自身版本檢查、論壇風險重查同一個節拍
TICK_SECONDS = 60
RETRY_AFTER = 30 * 60   # 背景起的那一輪失敗（stamp 沒更新）時，隔這麼久才再試；否則來源掛掉會每分鐘打一次
KEY = "auto_refresh_hours"


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f).get(KEY, DEFAULT_HOURS)
    except (OSError, ValueError, AttributeError):
        return DEFAULT_HOURS
    return v if v in CHOICES else DEFAULT_HOURS


def save(path, hours):
    """只動自己的 key，同檔其他設定保留。hours 不在 CHOICES 就 ValueError，讓呼叫端回 400。"""
    if hours not in CHOICES:
        raise ValueError(f"hours must be one of {CHOICES}")
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        pass
    data[KEY] = hours
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def verdict(hours, last_refresh_ts, now, job_running, session_active, readonly, last_started=None):
    """回 "due" 或略過原因。原因字串原樣送到前端當 i18n key 的尾巴，改名要連 updates.auto_skip_* 一起改。
    last_refresh_ts 為 None（讀不到 stamp）視為從未更新 → 到期；不假裝知道。"""
    if readonly:
        return "readonly"
    if hours == 0:
        return "off"
    if job_running:
        return "job_running"
    if last_refresh_ts is not None and now - last_refresh_ts < hours * 3600:
        return "fresh"
    if last_started is not None and now - last_started < RETRY_AFTER:
        return "retry_later"   # 上一輪背景重新整理沒讓 stamp 更新（抓不到來源），先別連打
    if not session_active:
        return "no_active_session"   # polkit 的 update-cache 只對本機 active session 免密碼；沒有就會跳密碼框或直接失敗
    return "due"


def session_active(loginctl_output, uid):
    """loginctl list-sessions --no-legend 的輸出：SESSION UID USER SEAT TTY STATE IDLE SINCE。
    有一列是這個 uid 且 STATE 為 active 就算有人在桌面。解析不出來回 False（寧可少跑一輪，不跳密碼框）。"""
    for line in (loginctl_output or "").splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[1] == str(uid) and cols[5] == "active":
            return True
    return False
