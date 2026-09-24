# 變更紀錄 / Changelog

版本以日期標示。格式：修正 / 變更 / 新增。

---

## 2026-09-24

*Updates tab: hides firmware sub-packages no loaded driver uses, gives apt sources readable names and colours.*

### 變更

- **本機用不到的 firmware 子套件折疊起來**。Ubuntu 24.04 的 `linux-firmware` 總包硬相依全部拆分出的子套件（amd-graphics、qualcomm、netronome……），所以沒有 AMD 顯卡的機器也會一直看到 `linux-firmware-amd-graphics` 的更新堆在清單裡。現在把「自動安裝、被 `linux-firmware` 相依拖入、且套件在 `/lib/firmware` 的檔案與所有已載入核心模組 `modinfo -F firmware` 宣告的清單交集為零」的子套件收進預設收合的區塊，仍可勾選升級。有交集的（如 Wi-Fi 用的 mediatek）留在主清單。`modinfo` 查不到驅動動態請求的檔案，畫面標成「依 modinfo 判定」而不是斷言無用。
- **apt 來源用人看得懂的名稱**。廠商在 Release 檔的 `Origin` 欄位常填得看不懂：VS Code 填「code stable」、NodeSource 填「. nodistro」、OpenAI 乾脆不填（只好退到主機名 persistent.oaistatic.com）。加一張主機名對照表翻譯成 Microsoft（VS Code）、OpenAI（ChatGPT／Codex）、NodeSource（Node.js）等；沒對到的照舊退回 Origin，不硬猜。群組列旁仍顯示主機名與原始 Origin，方便對照 sources.list。應用程式分頁的來源欄同一份函式。
- **來源群組加識別色**：Ubuntu 橘、NVIDIA 綠、Microsoft 藍為固定色，其他第三方來源依名稱從色盤穩定挑色。顏色填在群組列的勾選格，只做辨識，不代表重要程度。
- 群組底下的套件內縮一階，讓來源與套件的層級看得出來。

### 修正

- **折疊的 firmware 子套件仍點亮更新分頁的綠點、仍被「全選」勾進去**。折疊的用意是不占視線，卻還算成待辦，裝完其他更新後綠點還亮著。綠點、「可升級 N 個」、全選都改成只看主清單；計數寫成「可升級 0 個（另 1 個本機用不到的 firmware 已折疊）」。
- **apt 下載幾百 MB 時進度長時間停在 1%，看起來像卡住**。aptdaemon 的百分比不照位元組線性走（下載大約只占前半）。接上 `progress-details-changed` 訊號，工作列直接顯示已下載 MB／總 MB／速率／預估剩餘。

### 新增

- **登入時自動開啟**（頂欄勾選框，預設關閉）。勾起來就在 `~/.config/autostart` 放一份桌面捷徑（延遲 3 秒等桌面就緒），取消就刪掉；狀態以檔案是否存在為準，不另存設定。

## 2026-09-22

*Fixes snap updates (they never worked), adds real download progress with MB, and restores the Hardware tab.*

### 修正

- **snap 更新完全無法運作**。snap 這個命令列程式不會主動觸發 snapd 的 polkit 授權，以非 root 執行一律回 `access denied (try with sudo)`。改為透過 `pkexec` 取得 root，桌面會跳密碼視窗。
- **snap 更新看起來卡住不動**。`snap refresh` 的進度是用歸位字元覆寫同一行，逐行讀取永遠讀不到內容。改為向 snapd 的 REST API（`/run/snapd.socket`，權限 0666，不需要 root）查詢真進度，顯示步驟數、已下載／總計 MB、以及目前步驟的百分比。
- **服務重啟會失去對進行中更新的追蹤**。工作狀態只存在記憶體，但 snapd 那邊仍在執行。改為啟動時掃描進行中的變更並自動接回。
- **重複按更新會失敗（結束碼 10）**。偵測到 snapd 已有進行中的變更時直接接上監看，不再重新要求密碼。
- **無法更新正在執行的 snap**。snapd 會拒絕，但使用者要輸入密碼之後才看得到失敗。改為在請求授權前先用 `/proc` 檢查，直接回報該關閉哪個程式與 PID。
- **應用程式表格的按鈕溢出表格外**。動作欄寬度不足以容納兩顆按鈕，已加寬並改用 flex 對齊。
- **硬體分頁空白**。前一次改版誤刪了該分頁的渲染函式，已從改版前的提交還原。
- pkexec 取消或密碼錯誤時只顯示結束碼，改為顯示可讀訊息。
- **第三方 repo 沒有 changelog 時只說「沒有」**。改為指向廠商自己的發行說明頁（Chrome、VS Code、Brave、Tailscale、Docker、Node、ChatGPT 等），沒有對應項目時退回套件宣告的官網或來源網域。網址在畫面上可點。
- **展開後的清單會自己收合**。監控區每 2 秒整個重繪，`<details>` 元素被重建、展開狀態隨之消失（Wi-Fi 頻道分析的 AP 清單、模型量測歷史）。改為把展開狀態記在渲染之外。
- **snap 更新失敗時不說原因**，只顯示「變更 N 結束於 Error」。改為帶出 snapd 自己的錯誤訊息與失敗任務的日誌；判斷為網路中斷（`unexpected EOF`、連線逾時等）時另外提示可以直接重試。

### 變更

- **硬體資料改為快取 1 小時並存成快照**，服務重啟後不必重新掃描；換硬體時可按「重新讀取硬體」。硬體分頁切換時不再重新渲染。
- **snap 的「說明」**不再只顯示「商店不提供更新說明」，改為列出 `snap info` 的各頻道版本、發布日期、版次與大小，並標示目前安裝的版次。內容以 HTML 表格渲染，不依賴等寬字型的字元寬度對齊。
- **監控分頁移除磁碟用量卡**，該數值在數分鐘內不會變動，磁碟與硬體分頁已有。
- 監控卡片標題統一大小與字重；Wi-Fi 的 2.4 GHz 與藍牙共存警語收合成標題旁的提示圖示，點擊才展開。

### 新增

- **與 DGX Dashboard 的對照**（更新分頁）。算出 Dashboard 那顆 Update 實際涵蓋的範圍：升級／新安裝／移除的套件數、韌體數，以及它會強制重開機。依據是 apt 歷史中它的 aptdaemon 角色為 `role-upgrade-system`，且歷次都有安裝甚至移除套件——`safe_mode=True` 會跳過這類升級，所以它用的是完整升級（等同 `apt full-upgrade`）。
- **Spark OS 本體套件標記**。`dgx-*`、`linux-image-nvidia*`、`nvidia-driver*` 這類出現時標「建議用 Dashboard」，因為 Dashboard 的韌體加重開機流程對這類才有意義。
- **核心與 NVIDIA 簽章模組的配對檢查**。兩者是獨立的 meta 套件、彼此沒有相依，只勾其中一個模擬不會有任何警告，但重開機後會進到沒有簽章 GPU 驅動的系統。現在確認視窗會擋下並提供「一起勾選」。這是可勾選設計唯一比 Dashboard 危險的地方。
- **Wi-Fi 頻道分析**（監控分頁的 Wi-Fi 卡，點「頻道分析」展開）。列出看得到的 AP 與各自頻道，計算 2.4 GHz 的 1/6/11 與 5 GHz 常用頻道的干擾分數，標出目前頻道與建議頻道。2.4 GHz 的分數會把相鄰 ±4 格的鄰居加權算進去（頻道間隔 5 MHz、訊號寬 20 MHz，相鄰頻道一樣會互相干擾）。只在按下時掃描，主動重掃約 8 秒且會讓連線暫時變鈍，因此不自動執行。分析結果只做建議，改頻道要自己進路由器管理頁。
- 監控分頁的卡片尺寸切換（大／小）。
- 預設開啟監控分頁，並記住上次瀏覽的分頁。

---

## 2026-09-19

*Initial public release.*

第一個公開版本。取代 DGX Dashboard 只有一顆「Update」按鈕、會盲目升級全部 apt 套件並強制重新開機的行為。

分頁：更新（可勾選、先模擬相依性、不自動重開）、應用程式（apt/snap/flatpak）、歷史、監控（DGX 風格儀表）、模型（Ollama 管理與效能量測）、磁碟（空間分析與白名單清理）、硬體（後面板示意、DMI、NVMe SMART、USB 樹、藍牙、印表機、PCI）。

另含韌體面板，直接讀 fwupd 比對版本，可抓出「回報成功但實際未更新」的情形。
