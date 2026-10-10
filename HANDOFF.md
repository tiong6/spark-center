# HANDOFF — 現在的接力棒

> 2026-10-11 模型頁修正。先看 git status / git log；本檔只記錄這次驗證的狀態，不沿用舊快照的版本推測。

## 已部署

- 檔案資訊改以共用資料夾、相對檔名與縮寫的 Ollama 雜湊呈現，單一檔案可再展開完整路徑。Strata 的不同目錄同名檔保持可區別；保留輪詢前的展開狀態與清單捲動位置。僅改畫面，未更名或移動模型。

- 同日修正模型列控制項間距：保留 10px gap、32px 高度，操作項固定單排，表格依內容分配操作欄寬；極窄時只在表格內橫向捲動。真實中英文畫面三列均確認 sameRow=true、fitsCell=true，既有檢查通過。

- ASUS Ascent GX10，DGX OS OTA 7.6.0。Spark Center 使用者服務運行於 loopback 11001。
- 模型頁三區：目前運行、持久檔案清單、量測與收合歷史。查詢失敗不再當成空清單；量測前後端檢查 completion；只保留一個輪詢計時器並阻止請求重疊。
- `tools/model_inventory.py` 只查登錄來源。一般頁面輪詢不碰 NAS；手動檢查在隔離子程序依序執行，超時保留快照。
- 本機登錄來源有 Ollama、Strata 設定、GPT-OSS 120B NAS 備份、Qwen 122B NAS 備份、空的 LM Studio 目錄。目前共 6 筆模型資料。機器路徑與快照在忽略的 `data/model-sources.json` / `data/model-inventory.json`，勿 commit。
- `spark-ollama-guard.service` 使用者服務已啟動且 enabled：11436 代理 11434。Strata 駐留、啟動或狀態不明時拒絕生成／載入，卸載可通過；有桌面提醒及最近拒絕紀錄。
- OpenClaw 主設定與 main agent models.json 的 ollama/vllm baseUrl 改到 11436（保留 /v1）。私下備份在 `~/.local/state/spark-center/llm-guard-backup-20261011/`，權限 0600。config 驗證成功，gateway 日誌確認 hot reload，CLI 列模型後再次核對兩個檔案的 URL 沒回到原埠。
- Strata 的 user service 新增 `strata-server.service.d/llm-admission.conf`，下次啟動透過 wrapper 取得獨占租約，並要求 Ollama 無駐留模型。現有 Strata 程序未重啟，仍由 HTTP／程序偵測擋下 Ollama。
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
- 沒有執行 OpenClaw 排程報告或傳送訊息；沒有重啟 Strata 做完整模型載入；沒有重算 NAS 模型 SHA-256。不要宣稱這些通過。
- `data/llm-guard-pending/` 非空表示上次請求可能未結束；不要因 `/api/ps` 暫時空就刪掉。先確認 Ollama 服務與 runner 已停止，按文件復原。
- 本次開始時 main 已比 origin/main 多 3 個既有 commit。這次保留本機提交，未將既有未發布提交一併推送。

## 固定操作紀律

- 重啟 Spark Center 前看 `/api/job` 必須 idle，避免遺失記憶體內工作狀態。
- 測拒絕用隔離服務，真服務只用不存在的測試模型；不得用大型載入嘗試撞 OOM。
- git add 列檔名，勿加入 data 或含憑證的 OpenClaw 備份。
- 先前 Node helper 確認／恢復與 dpkg 中斷修復的未驗證項目仍未在這次處理；詳見前版 HANDOFF 的 Git 歷史。
