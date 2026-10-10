# HANDOFF — 現在的接力棒

> 2026-10-11 模型頁修正。先看 git status / git log；本檔只記錄這次驗證的狀態，不沿用舊快照的版本推測。

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
