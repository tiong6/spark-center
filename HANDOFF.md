## 2026-10-11 公開相容層與發布

- 原有 13 個提交未拆分、重排或重寫；其上是一個相容層 commit。新版本 v2026.10.11，回退 v2026.10.09（19dbff4）。推送後以 git remote refs 核對，不沿用歷史未推送數字。
- 預設保護皆選用：無 guard 明示警告與事件、直連 Ollama；無記憶體政策回 not_configured 並放行。已配置但損壞的政策仍拒絕；guard 已回拒絕時不改入口重送。
- 本機 Spark Center、guard、Strata user service 的 stability.conf 已明確加入 SPARK_REQUIRE_GUARD=1 與 SPARK_REQUIRE_MEMORY_POLICY=1；本機 fail-closed 不變。以下先前「缺政策也拒絕」是這台的歷史部署情境，不是公開預設。
- 設定可安裝 user guard；earlyoom 改用 apt、缺政策先提示校準；drain 是能力探測，不假設上游存在。Strata 獨立 patch 已開 https://github.com/shi3z/Strata-DGX-Spark/pull/1，沒有推本機 14 個 Strata 提交。
- 完整 check、乾淨安裝、git pull --ff-only 升級與真小模型載入已測。11 個 API HTTP200，既有頂層欄位零移除；九頁結構比對差異符合預期。詳細證據、測試隔離方式與限制見 docs/RELEASE_VALIDATION_2026-10-11.md。
- 兩到三天觀察尚未完成，尚未發論壇或聯絡使用者。若需追蹤誤擋，查 stability.sqlite3 的 admission_refused、memory_state、protection_route；不要把拒絕次數直接當事故次數。

## 2026-10-11 穩定性防護版本

- 新增 backend/stability.py：本機 /proc 記憶體、swap 速率、PSI、NVRM 配置失敗與事件時間軸；SQLite 上限 5,000 筆／7 天。不讀 NAS、不記錄提示詞。journalctl 空視窗 exit 1 已與權限失敗分開處理。
- 20 次量測得到 baseline peak 15,647,383,552 bytes；1.25 倍政策保留量 19,559,229,440 bytes（18.22 GiB）。PSI full avg10 >= 10% 或記憶體低於保留量拒絕新請求；缺政策也拒絕。不是模型峰值／CUDA 容量保證。
- guard 對 Ollama 生成／載入使用獨占租約，避免兩個受管理請求同時載入；Strata 本機 8080 透過環境變數查同一 admission。11434 直連與其他獨立服務仍是界外入口。
- Strata repo 新增原子 drain，含 body/tokenization 前處理，補 busy 檢查與 stop 之間的空隙。切換 phase 與 owner 持久保存，重啟顯示 interrupted，明確重試才核對並恢復；不重播套件工作。owner 不出現在 API 或事件紀錄。
- systemd user drop-ins：管理服務及 guard 的 OOMScoreAdjust=100，Strata 維持 200；Strata 指向本機 11436 admission。設定與 runtime policy 在機器上，未納入版控；重建方式見 docs/STABILITY.md。
- tools/check.sh 通過：backend 7、stability 9、模型 20、切換 16，另有 npm/GPU/JS；9 個既有 CSS 警告保留。Strata 原有 139 個 server tests、新增 5 個 admission tests 通過，telemetry 原有 ResourceWarning 仍在。
- 真機 drain 503、resume 200；Strata → qwen3.5:4b 8.85 秒 → Strata 46.83 秒，兩端分別回答「切換測試成功」「穩定性驗證成功」。最終 draining=false、active_requests=0、Ollama models=[]，NVRM 仍為已觀測 25 條／5 群（不是 25 次事故）。
- 沒有刻意耗盡記憶體、長 PDF 壓力測試或 NAS 故障注入；隔離測試涵蓋拒絕、競態、狀態寫入失敗及中斷恢復。這次未改 fstab：CIFS soft 是預設，下載器已有單寫入鎖／150 MiB/s 限速／讀回校驗。
- earlyoom 1.7-2 arm64 於 08:47:58 完成安裝，實際 active/running/enabled；TERM 7.61%、KILL 3.81%、兩者 swap 門檻 100%。已核對套件 SHA-256，root 安裝過程遇到 systemd generator sandbox Protocol error，daemon-reload 每次約 90 秒。這是新觀測到的系統層問題，未宣稱修復。詳見 docs/STABILITY.md。

# HANDOFF — 現在的接力棒

> 2026-10-11 模型頁修正。先看 git status / git log；本檔只記錄這次驗證的狀態，不沿用舊快照的版本推測。

## 2026-10-11 模組公開介面複核

- server 不再使用 hardware/disk 的私有名稱：11 個硬體查詢與 1 個磁碟查詢改成公開函式，內部呼叫及測試同步改名，沒有保留舊名 alias。server 的彙整函式改名 `live_with_alerts`，backend 的 `hardware_live` 保留純查詢語意。
- 加入 AST 邊界檢查，禁止 server 直接存取 hardware/disk 底線名稱；AGENTS.md 同步規範。7 個 backend 測試及既有完整 check 通過，既有 CSS 9 個警告保留。
- 閒置重啟後 live 回應欄位與重啟前一致；hardware、wifi、FAQ、disk、LLM API 實際 HTTP 200。最後提交後再確認閒置重啟並核對來源，以避免檔案 mtime 晚於載入時間的歧義。
- 待後續國際化整理：hardware/disk 掃描資料與 label 仍使用 LANG_DEFAULT；英文 API 回應由 server._localized_response 翻譯副本（不是只有前端翻譯）。這次不改既有翻譯行為。
- 未推送數量請以 `git rev-list --count origin/main..HEAD` 即時查詢；下面的 9 個數字是拆分開始前的歷史狀態，不是目前總數。

## 2026-10-11 後端模組拆分（本次）

- `server.py` 由 6,196 行降到 3,998 行；硬體與網路查詢搬到 `backend/hardware.py`，磁碟掃描及快取搬到 `backend/disk.py`。硬體警報／時脈控制、套件交易及 HTTP 路由仍在 server；模型模組仍在 tools。沒有改模型或載入設定。
- 共用 `_run`／`_http_json`、路徑與環境設定放在 `backend/system.py`；MSG 放在 `backend/messages.py`，lint 讀取位置同步修改。各模組不匯入 server，資料路徑仍指向專案根目錄 data。清理後透過 disk.invalidate() 持鎖作廢快取。
- 新增 `AGENTS.md` 固定依賴方向與功能歸屬；CLAUDE.md 更新字串表位置。不要把硬體／磁碟功能堆回 server。
- `tools/check.sh` 通過：新增 6 個獨立匯入、路徑、開機快照、磁碟快取／失敗釋鎖／符號連結測試；既有 18 個模型及 16 個切換測試、npm、GPU、JS 檢查通過。9 個既有 CSS 警告保留。
- 真服務重啟前 job=idle、switch=done；重啟後 job=idle。11 個 API 實際 HTTP 200，硬體快照內容（扣除快取年齡）與重啟前相同；硬體／live／rear／disk 回應頂層欄位一致。
- 實際輸出：`Disk scanning: False categories: 12`、`Live GPU: True memory: True`。瀏覽器九個分頁逐一驗證標題與內容區，另驗證英文介面。沒有執行套件更新、磁碟刪除、模型切換或推論壓力測試。
- 本次只整理模組，沒有宣稱速度改善。server 仍保留更新交易與 API 路由，未做全面重寫。保留本機 commit，未推送原有的 9 個未發布提交。

## 已部署

- 新增 Strata／Ollama 順序切換按鍵：目前運行區「切換至 Strata」，Ollama 列在 Strata 駐留時提供「切換並載入」。後端 tools/llm_switch.py、/api/llm/switch；沿用既有 lease、pending 與 wrapper，不下載模型。busy、未知狀態、卸載失敗、重複切換均停止／拒絕；進度及錯誤雙語顯示。Strata 正常停止且 guard 核對無程序時顯示「已停止」。
- 2026-10-11 真實瀏覽器按鍵完成 Strata → qwen3.5:4b（9.12 秒）→ Strata（66.65 秒），數字為本次 started/finished 差值。兩端真實回覆均為「切換測試成功」；最終 Strata loaded=true、Ollama models=[]、pending_count=0，Strata 主程序已是 guard wrapper 並持 EX lease。沒有更改模型或 context 設定。
- 最新 tools/check.sh 通過：18 個既有模型測試＋16 個切換測試，以及 JS 輪詢／進度／錯誤測試。CSS 保留 9 個既有警告。真實中英文頁面確認切換中按鍵停用、中文最終三個操作列 sameRow=true/ fitsCell=true、pageOverflow=false。

- 拉取欄位原為相鄰 inline 控制項，gap 約 0、輸入框 38.47px／按鈕 27.48px；改為獨立 flex 容器、10px gap、36px 同高、固定單排，移除 input 的 inline 最小寬度以允許縮小。實際中英文模型頁 DOM 輸出：inputHeight=35.995、buttonHeight=35.995、gap=9.994、sameRow=true、pageOverflow=false。tools/check.sh 通過（18 tests in 6.565s；9 個既有 CSS 警告），服務 idle 時重啟，/api/llm ok=true；未觸發模型下載。

- 檔案資訊改以共用資料夾、相對檔名與縮寫的 Ollama 雜湊呈現，單一檔案可再展開完整路徑。Strata 的不同目錄同名檔保持可區別；保留輪詢前的展開狀態與清單捲動位置。僅改畫面，未更名或移動模型。

- 同日修正模型列控制項間距：保留 10px gap、32px 高度，操作項固定單排，表格依內容分配操作欄寬；極窄時只在表格內橫向捲動。真實中英文畫面三列均確認 sameRow=true、fitsCell=true，既有檢查通過。

- ASUS Ascent GX10，DGX OS OTA 7.6.0。Spark Center 使用者服務運行於 loopback 11001。
- 模型頁三區：目前運行、持久檔案清單、量測與收合歷史。查詢失敗不再當成空清單；量測前後端檢查 completion；只保留一個輪詢計時器並阻止請求重疊。
- `tools/model_inventory.py` 只查登錄來源。一般頁面輪詢不碰 NAS；手動檢查在隔離子程序依序執行，超時保留快照。
- 本機登錄來源有 Ollama、Strata 設定、GPT-OSS 120B NAS 備份、Qwen 122B NAS 備份、空的 LM Studio 目錄。目前共 6 筆模型資料。機器路徑與快照在忽略的 `data/model-sources.json` / `data/model-inventory.json`，勿 commit。
- `spark-ollama-guard.service` 使用者服務已啟動且 enabled：11436 代理 11434。Strata 駐留、啟動或狀態不明時拒絕生成／載入，卸載可通過；有桌面提醒及最近拒絕紀錄。
- OpenClaw 主設定與 main agent models.json 的 ollama/vllm baseUrl 改到 11436（保留 /v1）。私下備份在 `~/.local/state/spark-center/llm-guard-backup-20261011/`，權限 0600。config 驗證成功，gateway 日誌確認 hot reload，CLI 列模型後再次核對兩個檔案的 URL 沒回到原埠。
- Strata 的 user service 使用 `strata-server.service.d/llm-admission.conf`，透過 wrapper 取得獨占租約並要求 Ollama 無駐留模型；已於這次切換實測重啟並成功推論。
- 啟動、來源登錄、復原與適用範圍見 `docs/MODEL_PAGE.md`。

## 真實驗證輸出

```text
Ran 18 tests in 6.606s
OK
PASS: one timer, no overlapping requests, exit cleanup, late-response isolation, unknown status, text-only benchmarks and guard disable
/api/generate: HTTP 409 strata_resident
/v1/chat/completions: HTTP 409 strata_resident
/api/llm/load: HTTP 409
/api/llm/bench (embedding): HTTP 422
```

- `tools/check.sh` 全部通過；9 個既有 CSS 重複選擇器警告仍在，這次未改樣式表。
- 瀏覽器各分頁切換，以及中英文模型頁通過 DOM 檢查；6 筆檔案、NAS/SSD 分開、GiB 單位、歷史預設收合；只列兩個 completion 模型，Strata 駐留時載入與量測停用。
- 代理隔離測試包含串流中斷後 drain、截斷 Content-Length 保留 pending、Strata wrapper 執行小型 Python 子程序。沒有用真大模型重現 OOM。
- 測試用不存在的模型名稱已從最近拒絕顯示清除，不影響真實拒絕紀錄。

## 範圍與未驗證

- 防護不是全系統封鎖：直連原 11434、獨立 11435 embeddings、其他服務／繞過 wrapper 的 Strata 不受完整互斥控制。
- 沒有執行 OpenClaw 排程報告或傳送訊息、長文件壓力測試、NAS 模型 SHA-256。Strata 忙碌檢查與停止不是原子操作，切換期間勿從別的視窗／排程發新請求。Ollama 卸載與 wrapper 取鎖交接若被外部請求搶先，會安全失敗，需等其完成後重試；失敗不自動回載原模型。
- `data/llm-guard-pending/` 非空表示上次請求可能未結束；不要因 `/api/ps` 暫時空就刪掉。先確認 Ollama 服務與 runner 已停止，按文件復原。
- 本次開始時 main 已比 origin/main 多 3 個既有 commit。這次保留本機提交，未將既有未發布提交一併推送。

## 固定操作紀律

- 重啟 Spark Center 前看 `/api/job` 無執行中工作，且 `/api/llm/switch` 不是 running，避免遺失記憶體內工作／切換狀態。
- 測拒絕用隔離服務，真服務只用不存在的測試模型；不得用大型載入嘗試撞 OOM。
- git add 列檔名，勿加入 data 或含憑證的 OpenClaw 備份。
- 先前 Node helper 確認／恢復與 dpkg 中斷修復的未驗證項目仍未在這次處理；詳見前版 HANDOFF 的 Git 歷史。
