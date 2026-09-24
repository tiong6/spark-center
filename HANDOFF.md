# HANDOFF — 現在的接力棒

> 唯一接力棒；下一位先看 `git status` 與 `git log`，不要把這份快照當成即時狀態。

2026-09-24：依 `WORKORDER-i18n.md` 實作中英雙語，**待 Claude 審收，未 commit／push**。開始時工作樹乾淨，基準 HEAD 為 `8b4a751c1be34e52ef947e0c5080e25e4c730b95`。本次未啟動子代理。

## 審收前先知道的限制

工單「只刪 script 後中文 grep 必須為 0」無法與其餘要求同時照字面成立。實測七頁中六頁有 10 行含中文，磁碟頁 11 行：原有 CSS 中文註解、要求新增的「中」切換鈕，以及磁碟真實檔名「全部郵件」「重要郵件」。這些均保留；沒有隱藏或改寫檔名來通過檢查。

移除 script／style／語言切換鈕後，英文頁面文字與 title／placeholder 的中文命中：六頁為空；磁碟只有上述兩個檔名。八個指定英文 API 中七個無中文，`/api/disk` 只有兩個真實 `big_files[].path`。`/api/hardware/rear` 的使用者校準備註保留原文。aptdaemon 系統 locale 文字依工單豁免，不保證英文瀏覽器收到的每一筆外部工作紀錄都無中文。

中文 DOM 比對採用改動前保存的原檔副本（與基準 HEAD 一致）加相同的 API 回應，不使用 `git stash` 切換正在服務的檔案。七頁正規化後 DOM 字串完全相同；僅移除新增語言切換鈕、`data-i18n*` 屬性和頂欄必要文字包裝。監控停用測試頁的週期計時器，等 Wi-Fi 補充資料後重畫一次，以排除請求先後順序的差異。另有兩種語言各七頁直接連真實服務的驗證，沒有用固定資料取代真實服務驗證。

## 實作

- `index.html`：`STR` 每種語言 547 個 key；`t()`、靜態 `data-i18n*`、頂欄 `.switch`、`navigator.language` 預設、localStorage 記憶、重載切換與 `html.lang`；每個 API 請求由 `api()` 加上 `lang`。日期／數字格式也指定 `LANG`，避免英文模式仍出現中文上午／下午。英文較長的表頭與 Wi-Fi 欄名有僅限英文的寬度規則。
- `server.py`：`MSG` 每種語言 211 個 key、`msg()`；Handler 解析 query／Accept-Language，存 `self.lang`。內部快取、背景工作與已存快照保留原來的中文表示，HTTP 回應副本才依字串表和句型翻譯，因此兩種語言不會互相污染，也不需要遷移 `data/`。既有模型歷史的註記亦在回應時翻譯。
- 英文應用程式清單使用 `.desktop` 原始 `Name`，不使用 `Name[zh_TW]`。套件／模型／產品名、路徑、指令、技術識別名稱與單位保留；未命中字串表的外部資料不臆造翻譯。
- 不新增 i18n 套件、不拆前端檔案、不改 `CHANGELOG.md` 舊條目，不執行資料遷移。

## 已實際執行的驗證

`python3 -m py_compile server.py`、抽出 script 的 `node --check`、`git diff --check` 均 exit 0；已執行 `systemctl --user restart spark-center` 並確認 HTTP 可回應。

真實 Chrome／Playwright，1400×1800，每頁等待 12 秒：

```text
zh-TW: monitor llm updates apps disk hardware history — errors=0，scrollWidth=1400
 en:   monitor llm updates apps disk hardware history — errors=0，scrollWidth=1400
monitor: 兩種語言各 8 張卡片
中文固定資料 DOM: 七頁 IDENTICAL
```

八個指定 GET API 兩種語言均 HTTP 200，磁碟已等到 12 個分類的掃描明細出現才驗證。錯誤 POST 的真實輸出：

```text
en POST /api/simulate: HTTP 400, {"ok": false, "error": "No packages selected"}
en POST /api/install: HTTP 400, {"ok": false, "error": "Packages not found: spark-i18n-nonexistent-package-8b4a751c"}
zh-TW POST /api/simulate: HTTP 400, {"ok": false, "error": "沒有選取任何套件"}
zh-TW POST /api/install: HTTP 400, {"ok": false, "error": "找不到套件：spark-i18n-nonexistent-package-8b4a751c"}
PASS: 10 interleaved Accept-Language requests; no cache language contamination
```

另驗證：四組瀏覽器語言／已存偏好組合、實際點「中」會重載並保存 `zh-TW`、所有字串 key／佔位符、原有頂層函式都保留、清理指令／GPU 判定常數／核心配對輸出與原版一致。真實 Chromium snap changelog 的 meta、表頭、註記均為英文。211 個後端句型逐一驗證，另測工作進度、併發量測註記、複數 GPU 降速旗標等組合句。

術語表拆成 30 個詞／片語，均在渲染結果找到。真機目前沒有可升級套件，firmware 折疊、推測、重開機提示與商店查詢失敗使用明確標記的**瀏覽器內測試資料**覆蓋；未安裝套件、載入模型、執行量測或清理磁碟。

完整原始輸出、原版副本、API 基準、14 頁 DOM／PNG 與測試腳本在 `/tmp/spark-i18n-baseline/`，主要結果在 `after/`：

- `browser-validation.txt`、`api-validation.txt`、`glossary-validation.txt`
- `paired-dom.txt`、`zh-diff-*.txt`（七份差異檔皆空）
- `en-*.png`、`zh-TW-*.png`、`fixture-branches.txt`、`fixture-apps.txt`
- `dynamic-time.txt`：中文瀏覽器＋英文介面下，已載入模型的到期時間與監控 hover 時間測試

這些檔案是本機暫存驗證產物，未加入 repo；審收仍請獨立重跑工單。

## 看到但未順手修的既有問題

- 硬體頁同一段說明同時寫「快取 1 小時」與「靜態資料快取 60 秒」；實際 `HW_TTL` 是 3600。
- 監控說明仍寫「離開硬體分頁就停止取樣」，但 `showTab()` 依是否在 monitor 頁來控制即時取樣。兩種語言均保留原文語意，本次不改邏輯或順手改原文。

本次曾引入 `renderMon()` 時間戳 `t` 與翻譯函式同名的錯誤，實際畫面停在 Loading 時抓到；已把該區時間戳改名 `sampleTime`，重跑後卡片與錯誤檢查通過。不要只靠語法檢查審收。

## 仍擱置的其他工作

模型倉庫去重、調整 Ollama NUM_PARALLEL、改 Open WebUI 連線、刪除模型／Docker 卷與硬體採購不屬本工單，未執行。原先「英文版先不做」已被本次使用者指定工單取代。
