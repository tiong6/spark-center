# HANDOFF — 現在的接力棒

> **這是唯一一根棒子。收工時【覆蓋】它，不要另開新檔、不要加日期。**
> 舊棒在 `git log --follow HANDOFF.md`。
> 【開工的人】下面的「下一步」和任何待辦，先 `git log`/看程式碼確認還沒做——
> 快照會過期，「棒子說待辦、其實早出貨」是這個玩法最常見的坑。

**上次收工：2026-09-19** ｜ 工作樹乾淨，main 與 origin/main 同步（11a4ec5）｜ py_compile／JS 語法 OK ｜ gpu_stuck_evaluate 假樣本測試 PASS ｜ 服務 `spark-center` active，7 個主要 API 全 200 ｜ 沒有自動化測試套件，驗證靠 API curl 與瀏覽器 DOM 查詢 ｜ 公開 repo https://github.com/tiong6/spark-center

## 下一步：UI 英文化（使用者說「先不做」，別主動開工；其他候選見下）

使用者明確說今天先不做英文版。真的要做時：抽字串表＋語言切換，估 2–3 小時，README 英文摘要已寫好。
若使用者沒指定，可提的候選（依價值）：
1. **模型倉庫去重的實際動作**：Open WebUI 改連主機 Ollama（OLLAMA_BASE_URL），收回 86 GB 的 open-webui-ollama 卷；LM Studio 與 Ollama 各有一份 gpt-oss-120b。目前工具只列出，不動。
2. **LLM 併發壓測要有意義**得先把 `OLLAMA_NUM_PARALLEL` 從 1 調高（需 root：`sudo systemctl edit ollama`）。
3. README 缺截圖（本 session 影像讀取全被 API 拒絕，只能由使用者自己截）。

## 這一場做了什麼（git log 是真相，這裡只給脈絡）

一天內從零做到開源，45+ commits。git log 讀不出來的「為什麼」：

| 範圍 | 為什麼 |
|---|---|
| 整個專案 | DGX Dashboard 的 Update 鈕 = 盲裝全部 apt 套件（含 Chrome/ChatGPT）且寫死更新後必重開機（前端打 `/update_reboot`）。使用者要的是可勾選、不重開。 |
| NVML 走 ctypes | 偷師 DGX-Spark-Dashboard；不用 pip；GB10 記憶體/功耗上限回 NOT_SUPPORTED 是正常。降頻門檻用 NVML slowdown（86 °C），之前 tlimit 推算的 96 °C 是錯的。 |
| Wi-Fi 卡＋藍牙共存提示 | 使用者滑鼠斷線根因：Wi-Fi 切 2.4 GHz 時與藍牙同晶片（MT7925）同頻段，下載時藍牙被擠掉。 |
| 後面板孔位校準 | 韌體 ACPI _PLD 只給左/右，同側兩孔無序；驅動 USB-C-k ↔ 控制器 0k 一致；實體順序（面對機背左→右）= 03(電源)、02(螢幕)、01、00，已用插入法確認並存 data/usbc-map.json。 |
| fwupd 韌體面板 | 論壇第 1 大抱怨：Dashboard 說韌體成功、fwupd 其實失敗（USB-C PD 控制器 0x507 vs 0x500）。本機三個韌體 8/4 都真的升上去了。 |
| GPU 卡死偵測 | 論壇第 3 大抱怨（PD 控制器韌體卡住 → SM 釘 611 MHz）；判定需配合使用率避免閒置誤報；解法冷放電。 |
| 硬體資料改 1h 快取＋快照 | 使用者：硬體不會變，別每次重讀。 |
| 歷史重寫 | 使用者要求對外不提舊名；已 filter-branch 全歷史＋強制推送，hash 全變。**對話與 commit 訊息別再寫舊名。** |

專案脈絡（記憶檔也有）：`~/.claude/projects/.../memory/` 的 spark-updater-project.md、gx10-hardware-facts.md、spark-center-github.md。

## ⏸ 使用者明確擱置（別自作主張開工）

- **UI 英文化**：「先不做英文版了，fable 今天用太多了」。
- **改 Ollama NUM_PARALLEL／Open WebUI 連線／刪重複模型／清 Docker 卷**：工具只列不動，動要使用者按或自己做。
- **sudoers 檔名**仍是 spark-updater-*（只是本機檔名，不在 repo），使用者沒要求改。
- **4 TB SSD 升級**：使用者在考慮 Corsair MP700 Micro 4TB（Amazon US 缺貨、Amazon JP 有貨但不直寄台灣），純硬體採購，工具無事可做。

## 這一場的教訓（只寫會重複發生的）

- **大段字串替換後，每個分頁都要點一遍。** LLM 分頁改版時用「LLM 區塊起點 → stopHw」當範圍，把夾在中間的硬體分頁函式一起刪了；語法檢查抓不到「函式不存在」，直到使用者點硬體分頁才炸。修法：替換前先列出範圍內所有頂層定義。
- **Python heredoc 裡不要混 shell 行**（`grep ... || ...` 寫進 python 腳本 → SyntaxError → 整段沒執行，但後面的 shell 步驟照跑，會誤以為改好了）。
- **pkill -f 的樣式會比對到自己的 shell**，用 `[1]` 這種字元類別避開。
- **.desktop 的 Exec 含空格路徑要加引號**：desktop-file-validate 說合法，GIO 卻載不進來。
- **本 session 影像讀取全被 API 拒**（對話裡有超過 2000px 的圖後所有圖都讀不到）；畫面驗證改用 tesseract OCR 與 DOM 查詢，能做但看不到顏色/排版，要跟使用者講清楚。
- 使用者的誠實偏好很具體：拿不到顯示「—」並說原因、推測要標「推測」、不喜歡條列式 UI 要面板/表格/儀表、壞消息先講。
