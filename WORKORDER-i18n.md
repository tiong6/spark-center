# 工單：介面英文化（zh-TW / en 雙語）

執行者：任一模型或 Codex。審收者：Claude（Fable），審收方式見文末，執行者的自我回報不作數。
專案規則見 `CLAUDE.md`（改動流程、不提舊名）。誠實原則：**限定語不能翻丟**，見「術語表」。

## 目標

1. 前端 `index.html` 所有使用者看得到的字串走字串表，支援 `zh-TW` 與 `en`。
2. 後端 `server.py` 回傳給畫面的字串（錯誤、狀態、註記、說明）也雙語。
3. 頂欄一個「中 / EN」切換；預設語言依 `navigator.language`：以 `zh` 開頭 → zh-TW，其餘 → en；使用者切過就記在 `localStorage['spark-center.lang']`。
4. 註解、commit 訊息、CHANGELOG 維持中文，不在範圍。

## 規模（2026-09-24 量的）

| 檔案 | 含中文的行 | 中文片段 | 備註 |
|---|---|---|---|
| index.html | ~300 | ~690 | 幾乎全是介面字串，嵌在 JS 樣板字串裡 |
| server.py | ~330 | ~420 | 一半以上是註解（不動）；可顯示的約 150–200 段：`_json({"ok": False, "error": …})` 36 處、`note`/`status_text`/`details` 56 處、其餘散在 dict 字面值 |

## 架構（照做，不要另想）

### 前端

- 檔案頂端（`<script>` 開頭）放一張字串表：
  ```js
  const STR = {
    'zh-TW': { 'tab.monitor': '監控', 'updates.meta': '可升級 {n} 個', … },
    'en':    { 'tab.monitor': 'Monitor', 'updates.meta': '{n} upgradable', … },
  };
  let LANG = (() => { try { return localStorage.getItem('spark-center.lang'); } catch (e) {} return null; })()
          || (navigator.language.toLowerCase().startsWith('zh') ? 'zh-TW' : 'en');
  const t = (key, vars) => { let s = (STR[LANG] && STR[LANG][key]) ?? (STR['zh-TW'][key] ?? key); if (vars) for (const k in vars) s = s.split('{' + k + '}').join(vars[k]); return s; };
  ```
- key 用 `區域.意義` 命名（`tab.*`、`updates.*`、`apps.*`、`disk.*`、`hw.*`、`mon.*`、`llm.*`、`history.*`、`job.*`、`common.*`），不要用中文當 key。
- 帶數字或名稱的句子用 `{n}`、`{name}` 佔位，不要用字串拼接（英文語序不同）。
- HTML 靜態文字（分頁名、表頭、按鈕、面板標題）加 `data-i18n="key"`，載入與切換時跑一次 `document.querySelectorAll('[data-i18n]')` 套上；有 `title`/`placeholder` 的用 `data-i18n-title`、`data-i18n-placeholder`。
- 切換語言：設 `LANG`、寫 localStorage、`location.reload()`。不要嘗試不重載就換字，會漏。
- `<html lang>` 跟著改。`TITLES`（分頁標題）改成從 `t()` 取。
- 頂欄切換放在「登入時自動開啟」左邊，樣式沿用 `.switch`。

### 後端

- `server.py` 加：
  ```python
  LANG_DEFAULT = "zh-TW"
  MSG = {
      "zh-TW": {"no_pkgs": "沒有選取任何套件", …},
      "en":    {"no_pkgs": "No packages selected", …},
  }
  def msg(key, lang, **kw): return MSG.get(lang, MSG[LANG_DEFAULT]).get(key, MSG[LANG_DEFAULT].get(key, key)).format(**kw)
  ```
- Handler 從 `?lang=` 或 `Accept-Language` 取語言（前端每個 `fetch` 加 `?lang=${LANG}`，`api()` 一處改即可），存到 `self.lang`，所有 `_json` 的錯誤訊息、`note`、`status_text`、`details` 都經 `msg()`。
- aptdaemon 的狀態文字（`get_status_string_from_enum`）本身跟系統 locale 走，不用翻；但我們自己拼的字（「開始 install: …」「解析相依性中」這種 `_log`）要翻。
- 硬體／磁碟／模型分頁的 `note` 字串很多是整句說明（例：「同一模型的不同 tag 共用 blob，清單加總會大於實際占用」），照句翻，不要縮。

## 術語表（全站一致，審收會逐一 grep）

| 中文 | 英文 | 備註 |
|---|---|---|
| 監控 / 模型 / 更新 / 應用程式 / 磁碟 / 硬體 / 歷史 | Monitor / Models / Updates / Apps / Disk / Hardware / History | 分頁名 |
| 可升級 | upgradable | |
| 本機用不到的 firmware 子套件 | firmware sub-packages this machine does not use | |
| 依 modinfo 判定，無法排除驅動動態請求 | judged from `modinfo`; drivers that request firmware at runtime cannot be ruled out | **限定語，不能省** |
| 推測 | estimated | 凡標「推測」的地方英文一律 `(estimated)` |
| 通常需重開（推測） | usually needs reboot (estimated) | |
| 本工具不會自動重開，時機由你決定 | This tool never reboots on its own; the timing is yours | |
| 建議用 Dashboard | use the stock Dashboard for this | |
| 拿不到 / 未能查詢 | unavailable / could not query | 顯示「—」的地方英文也顯示「—」 |
| 統一記憶體 | unified memory | |
| 降頻門檻 | slowdown threshold | NVML 用語 |
| 依據 | based on | 例：based on `/var/run/reboot-required` |
| 掃描於 / 快取 N 分鐘 | scanned at / cached N min | |
| 其他（系統、未掃描） | Other (system, not scanned) | |
| 登入時自動開啟 | Open at login | |
| 說明（按鈕） | Details | changelog 鈕 |
| 檢查影響並更新 (N) | Check impact and update (N) | |
| 重新整理來源 | Refresh sources | apt update |
| 清空垃圾桶 / 清 apt 快取 / 清 npm 快取 / 清 dangling 映像 | Empty trash / Clear apt cache / Clear npm cache / Remove dangling images | |

沒列到的：用系統設定畫面（GNOME Settings、macOS System Settings）的慣用英文，不要發明。

## 不准做的事

- 不改任何邏輯、不重構、不順手修 bug（看到就記在回報裡，不動）。
- 不把限定語（推測、依 modinfo、無法排除、不假裝）翻掉或翻軟。
- 不用外部 i18n 套件、不拆檔（單一 `index.html` 是設計原則）。
- 不動 `data/`、不動 `CHANGELOG.md` 舊條目。

## 完成定義（執行者自己先跑，審收者會重跑）

1. `python3 -m py_compile server.py` 通過；把 `<script>` 內容抽出來 `node --check` 通過。
2. `systemctl --user restart spark-center` 後，兩種語言各把七個分頁跑一遍：
   ```sh
   google-chrome --headless=new --disable-gpu --no-sandbox --user-data-dir=/tmp/hc \
     --window-size=1400,1800 --virtual-time-budget=12000 --dump-dom "http://127.0.0.1:11001/#<tab>" > <tab>.html
   ```
   英文模式下（先在 localStorage 設 `spark-center.lang=en`，或用 `--initScript`）七個 dump 裡 `grep -c '[一-鿿]'` 必須為 0（`<script>` 內的字串表除外，先把 script 砍掉再 grep）。
3. 英文模式下打這些 API 各一次，回應裡不得有中文：`/api/updates`、`/api/apps`、`/api/disk`、`/api/hardware`、`/api/llm`、`/api/history`、`/api/job`、`/api/firmware`；再故意打錯的 POST（`/api/simulate` 空清單、`/api/install` 不存在的套件）看錯誤訊息是英文。
4. 中文模式下畫面與改動前一致（用 git stash 前後各 dump 一次 DOM diff，只允許 `data-i18n` 屬性差異）。
5. 術語表每一條在英文 dump 或 API 回應裡 grep 得到，且沒有第二種譯法。
6. 回報：改了幾個 key、哪些字串刻意沒翻（例如指令名、套件名）、看到但沒動的 bug。

## 審收（Claude 做，執行者不用管）

- 重跑上面 1–5，不看回報只看結果。
- 抽 20 段含限定語的句子對照原文。
- 英文截圖七張，看版面有沒有因英文較長而爆版（表頭、按鈕、hero 六格）。
- 通過才 commit；不通過退回列出項目。
