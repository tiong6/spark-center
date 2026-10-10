# 模型頁：運行、檔案、量測

模型頁分成三個區域，避免把歷史紀錄、模型檔案、記憶體駐留混成同一清單。

- **目前運行**：讀取服務的 API；API 失敗顯示狀態不明，不當作空清單。記憶體只顯示服務實際回報值，Strata 未提供時為 `—`。Context 是執行上限，不是目前已配置記憶體。
- **模型檔案與儲存位置**：讀取本機持久快照，包含已登錄的 SSD 與 NAS 來源。服務關閉不會移除檔案紀錄。按「重新檢查檔案」才存取來源；不會每十秒掃 NAS，也不會啟動 Docker 容器查容量。
- **量測與歷史**：只列出 Ollama 明確回報 `completion` 的模型；後端再查一次能力與准入條件。歷史預設收合，不表示歷史模型仍在 SSD 或記憶體。

檔案容量是邏輯大小，顯示 GiB（2³⁰ bytes），不是 RAM 用量或實際磁碟配置區塊。Ollama 相同權重的別名合併為一筆，同一來源共享 blob 不重複加總。檢查存在與大小不等同 SHA-256 完整性驗證；尚未核對、缺檔、來源無法存取皆分開顯示。快照不是整顆磁碟的自動發現器。

## 登錄來源

機器專屬設定放在 `data/model-sources.json`，快照為 `data/model-inventory.json`，均不進 Git。例如（替換成實際絕對路徑）：

```json
[
  {"id":"ollama-local","type":"ollama","storage":"SSD","path":"/usr/share/ollama/.ollama/models"},
  {"id":"strata","type":"strata","storage":"SSD","path":"/path/to/Strata/strata-iq3_xxs.json"},
  {"id":"gpt-oss-archive","type":"ollama_archive","storage":"NAS","path":"/mounted/nas/ollama-archive/gpt-oss-120b","manifest":"manifests/registry.ollama.ai/library/gpt-oss/120b"},
  {"id":"lmstudio","type":"gguf","storage":"SSD","path":"/home/USER/.lmstudio/models"}
]
```

Strata 從設定中取得 native、PLE、pack、MTP、vision 模型檔案。Ollama 從 manifest 取得 blob 清單並核對大小。NAS 備份指定可信的主要 manifest，避免把備份目錄裡遺留但沒有對應 blob 的其他模型算作完整備份。LM Studio 來源只查登錄目錄下 `publisher/model/*.gguf`，不掃其他格式或整個家目錄。

檢查使用單一子程序依序處理來源，每來源等候上限 10 秒。超時保留上次成功快照。若 hard NFS 讓子程序連 SIGKILL 都要等待，服務不會重複產生更多讀取程序；需先恢復 NAS，再重新檢查。頁面輪詢只讀 SSD 上的快照。

## Strata 與 Ollama 載入防護

`tools/ollama_guard.py serve` 只監聽 `127.0.0.1:11436`，轉送到原本的 `127.0.0.1:11434`。Spark Center 的載入與量測只走防護入口，防護服務失聯即停止載入，不退回原連接埠。卸載仍允許；`keep_alive=0` 但帶有 prompt 的生成請求不能繞過保護。

Strata 服務可連線、啟動中、引擎仍存活、或狀態無法確認時，生成／載入請求收到 HTTP 409。為避免 Strata 自動載入的競爭，即使 Strata 的 `loaded=false`，也要停止 Strata 服務才放行 Ollama。預設是**拒絕並通知**，沒有排隊或稍後自動重試。

被拒絕時，桌面通知最多每 60 秒一次；模型頁顯示最近拒絕的時間與模型。事件保存在 `data/llm-guard-event.json`。桌面通知不可用不影響拒絕。

以 systemd 使用者服務啟動防護（`ExecStart` 路徑需替換）：

```ini
[Unit]
Description=Ollama admission guard for Strata shared memory
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 "/absolute/path/to/spark-center/tools/ollama_guard.py" serve
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
```

存成 `~/.config/systemd/user/spark-ollama-guard.service`，再執行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now spark-ollama-guard.service
```

Strata 的 systemd `ExecStart` 要加上相同 script 的 wrapper，保留原始 Python、server 路徑、所有參數。例如 drop-in：

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 "/absolute/path/to/spark-center/tools/ollama_guard.py" strata -- "/path/to/Strata/.venv/bin/python" "/path/to/Strata/serve/server.py" --engine strata --config "/path/to/Strata/config.json" --port 8080
```

wrapper 先取得 exclusive flock，確認 Ollama `/api/ps` 為空才啟動 Strata；持鎖直到子程序結束。Ollama 生成請求持 shared flock 到上游串流 EOF，所以客戶端提早關閉視窗也不會提前讓 Strata 啟動。若代理異常終止或上游回應中斷，`data/llm-guard-pending/` 的記錄阻止 Strata 啟動，以免把 `/api/ps` 尚未出現的載入中模型當成空閒。

若出現未結束的 pending 記錄，先查程序與日誌。只有確認原本的 Ollama 服務已停止、runner 已退出且沒有直接呼叫者，才能移除該記錄，再啟動 Ollama、防護與所需模型；不要只因 `/api/ps` 暫時為空就清掉記錄。

**適用範圍**：Spark Center 與設定成 11436 的客戶端。直接呼叫 11434、其他 Ollama 實例（例如獨立 embeddings 的 11435）、其他模型服務，以及繞過 wrapper 手動啟動的 Strata，不是完整互斥範圍。這不是系統層面的記憶體配額，也不保證任何單一模型都放得下。

OpenClaw 的 Ollama provider 改用 `http://127.0.0.1:11436`；OpenAI 相容 provider 改用 `http://127.0.0.1:11436/v1`。保留原本憑證、模型與其他 provider。除了主設定，也要核對 agent 的 `models.json`：目前安裝的 OpenClaw 合併流程可能保留舊 baseUrl。修改前將含憑證的設定私下備份為權限 0600，勿加入本 repo。

## 按鍵切換 Strata 與 Ollama

在「目前運行」區按「切換至 Strata」；Strata 駐留時，在已安裝的 Ollama 模型旁按「切換並載入」，可沿用該列的駐留時間。進度與失敗原因顯示在「目前運行」區，每次模型頁輪詢更新。NAS 備份不提供載入按鍵。

切換由 `tools/llm_switch.py` 執行；`POST /api/llm/switch` 只接受 `target: strata|ollama`、已安裝的 Ollama `model` 和 `keep_alive: 5m|30m|2h|-1`。GET 同一路徑可讀進度。只控制固定的 `strata-server.service` 使用者服務，而且要求其 ExecStart 已配置本專案的防護 wrapper。沒有自動下載、變更開機啟動或繞過防護。

- Strata 至 Ollama：先確認所選模型已安裝；檢查 Strata 沒有生成／排隊請求，再停止服務。確認 service 已停止、Strata 引擎程序已退出且防護放行後，經 11436 載入並核對 `/api/ps`。
- Ollama 至 Strata：先取得 exclusive lease 並檢查 pending。逐一卸載目前駐留模型，確認 `/api/ps` 清空，再釋放 lease、啟動 Strata；wrapper 重新取得租約。只有 service active、`/health loaded:true`、Ollama 無駐留時才顯示完成。
- 忙碌、狀態不明或卸載失敗時停止切換；重複點擊不產生第二個切換。切換期間 Spark Center 的載入、卸載、刪除與量測暫停。失敗後不自動回載原模型。

Strata 的忙碌檢查與服務停止不是原子操作。請先結束對話，切換期間不要從其他視窗或排程送出新請求；剛進來的 Strata 請求仍可能被停止。Ollama 卸載與 wrapper 取鎖之間若外部請求搶先，wrapper 會拒絕啟動，頁面顯示失敗，需等請求完成後重試。直接呼叫原連接埠的限制仍與上述防護範圍相同。

重啟 Spark Center 前也要確認 `/api/llm/switch` 不是 `running`；切換進度存於伺服器記憶體。關閉或重新整理網頁不會取消後端切換。

## 驗證與本次部署

2026-10-11 在 ASUS Ascent GX10、DGX OS 7.6.0 測試：

- `tools/check.sh`：Python/JS 語法、HTML、中英字串、既有回歸測試，以及新增模型頁測試。
- 隔離 HTTP 服務與小檔案：拒絕載入、保留卸載、文字生成能力、別名去重、缺檔、NAS 未掛載、輪詢生命週期、串流與中斷、wrapper 執行小型子程序。
- 真實 API：Strata 駐留時，使用不存在的測試模型呼叫防護入口，Ollama 與 OpenAI 請求均回 409；Spark Center 載入回 409，embedding 量測回 422。未載入第二個大型模型。
- 真實頁面：三區分開、SSD Strata 與 NAS 備份可見、歷史預設收合、Strata 駐留時載入與量測停用。
- OpenClaw 主設定已熱更新；對應 agent 快取同時修正。沒有執行排程報告或發送訊息。
- 按鍵實測完成 Strata → Ollama `qwen3.5:4b` → Strata，兩個模型都實際回答「切換測試成功」。切至 Ollama 約 9.12 秒，回 Strata 約 66.65 秒，來自本次 API started/finished 差值，不是效能保證。最後 Strata `loaded:true`、Ollama `/api/ps models:[]`；wrapper 已經這次完整啟動驗證，並持有 exclusive lease。
- 模型切換測試涵蓋 16 項隔離案例，加上原有 18 項模型頁／防護測試與 JS 測試。繁體中文與英文進度、按鍵停用與單排排列已在真實頁面確認。完整 NAS 雜湊、長文件壓力測試、OpenClaw 實際排程端到端回覆，未在這次執行。

回復外部設定時，先停止有關模型工作，再依私下備份回復 OpenClaw baseUrl，移除 Strata 的 `llm-admission.conf` drop-in，執行 daemon-reload。Spark Center 新版仍依賴防護入口；若停用防護，須同步回復本次程式版本，不能讓新版偷偷直連 11434。
