# 2026-10-11 發布驗收

## 範圍

在同一台 GB10、Linux/systemd/apt 環境，以暫存 clone 測試；不是重新安裝作業系統。原有 13 個提交保持不動，只增加一個相容層提交。回退基準 `v2026.10.09` = `19dbff4`；新版標籤 `v2026.10.11`。

## 乾淨安裝

- 從本機候選版本 git clone，不攜帶任何 `data/`；開始前以 assertion 確認 data 不存在。使用獨立 HOME，直接以 `/usr/bin/python3 server.py` 啟動，埠 11005。
- 將 `SPARK_GUARD_URL` 和 `SPARK_STRATA_URL` 指向未使用的 loopback 埠，兩個 REQUIRE 旗標設 0。測試副本不啟動 guard 或 Strata；不停止生產防護來偽裝缺少能力。主機既有 earlyoom 仍存在，這不是全新 OS 的驗證。
- 實際 API：`guard.protected=false`、`blocked=false`、memory state=`not_configured`、switch available=false。模型頁可見未保護橫幅、一般載入按鈕，不提供切換按鈕。
- 真模型驗證前先確認生產 Strata 閒置，drain 後停止；由 11005 模型頁按「載入」載入本機已存在的 `qwen3.5:4b`。Ollama `/api/ps` 確認駐留，實際生成「相容性測試成功」。再從頁面卸載、確認 models=[]，恢復 Strata loaded=true。沒有下載模型。
- 最終候選版本再 fresh clone 到新暫存目錄，仍不帶 data；11 個 API 均 HTTP 200，九個分頁逐一開啟。應用程式清單實際顯示 92 筆（93 個含表頭的 DOM rows）。

## 舊版升級

- 暫存 clone reset 到 `v2026.10.09`，11006 埠啟動舊 server.py，先記錄 API 與頁面。
- 停止這個暫存程序，以 `git pull --ff-only origin main` 從本機候選來源升級、再啟動。不執行新版 install.sh，也沒有帶入本機政策／服務檔。
- 11 個 API 升級前後皆 HTTP 200；沒有移除既有頂層欄位。

| API | 新增欄位 |
|---|---|
| /api/llm | capabilities、guard、inventory、switch、switch_control |
| /api/hardware/live | stability |
| /api/setup | capabilities |
| /api/job、/api/hardware、/api/hardware/rear、/api/disk、/api/history、/api/faq、/api/apps、/api/updates | 無 |

- 應用程式的 92 個 `(source, id)` 集合相同。
- 另在 11007 保留原版頁面作結構基準。DOM 比對正規化 CPU 負載／GPU 即時狀態後比較標題結構；不是整份 DOM 的逐位元相等，也不比較即時感測數字。監控只多「記憶體保護與事件」；模型改為目前運行、模型檔案與儲存位置、量測與歷史；設定多三個選用項。更新、應用程式、磁碟、硬體、歷史、FAQ 的標題結構無變更。
- 使用過的測試視窗曾保留未完成請求並持續顯示掃描中；以新視窗和帶版本 query 的 URL 重新載入後看到完整清單。臨時前端診斷 HTML 已移除，測試 checkout 最後核對乾淨。未將診斷程式加入版本。

## 保護、安裝與回歸測試

- `tools/check.sh` 通過；新增 9 個相容性隔離測試，涵蓋缺政策放行、嚴格模式缺政策／guard 拒絕、guard 明確拒絕時不轉送、舊 Strata 無 drain、切換暫停期間能力保留、Unicode／空白路徑的 unit 生成、缺政策時 earlyoom 不執行 apt 或写設定。保留 9 個既有 CSS 重複選擇器警告。
- 設定分頁對應的真實 POST `/api/setup/toggle`、model_guard=true 已執行；模板產生的 user unit 生效，guard_available=true，本機兩個 strict 旗標仍為 true。未額外授予 sudoers。
- 生產 Spark Center 已以新版重啟。Strata 駐留時 guard 仍回 blocked=true / strata_resident，切換能力可用；本機校準值未移植到測試副本。
- earlyoom 的新版 apt 安裝流程未在這台重裝套件；缺政策拒絕與脚本語法已驗證。本機原先安裝的 earlyoom 持續 active。要驗證全新 Ubuntu apt 套件安裝仍需獨立 OS；不將 shell shim 測試說成真實 apt 安裝。
- Strata drain patch 在獨立上游 checkout 執行 `serve.test_admission` + `serve.test_server`，144 tests / 51.434 s / OK；已開 [上游 PR #1](https://github.com/shi3z/Strata-DGX-Spark/pull/1)，不是 Spark Center 的安裝前提。

## 尚未完成的觀察

尚未經過兩到三天的生產觀察，未做 GPU OOM／長 PDF／NAS 中斷的破壞性測試。論壇公告與邀請已知使用者回報，留到觀察後；本次不冒稱完成。先前 systemd generator 重載約 90 秒的錯誤亦未在此相容層修復。
