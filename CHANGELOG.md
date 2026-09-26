# 變更紀錄 / Changelog

版本以日期標示。格式：修正 / 變更 / 新增。

---

## 2026-09-26

*Monitor: network cards follow the live link state and show the link speed (e.g. "Wired · 10 Gb/s"), so a cable plugged in after start-up shows up within seconds instead of after the hourly hardware re-scan.*

### 新增

- **區網裝置**（硬體分頁新面板）：本機所在網段裡看得到的裝置。被動查、不掃描：ARP／NDP 鄰居表（最近講過話的，有 MAC 與 IEEE OUI 廠牌）＋ mDNS 瀏覽（會自報名字的，標「只在 mDNS 看到」、沒有 MAC）；名稱先問路由器 DNS 的 DHCP 主機名，再用 mDNS 反查；隨機化 MAC 標「私有位址」不猜廠牌。面板明講只含本機網段（VLAN），其他 VLAN 從這裡看不到、完整清單在路由器。快取 30 秒，重新掃描約 1 秒。真機：13 台（路由器、QNAP、iPhone、iPad、Apple TV、Dyson、Matter 裝置）。踩到 avahi 輸出夾非 UTF-8 位元組讓 text 解碼整段失敗，改位元組讀入寬鬆解碼。

### 修正

- **插上網路線後監控頁沒有有線網路卡。** 介面清單來自硬體快取（1 小時），快取裡該介面還是 DOWN 就不畫。現在每 2 秒的即時資料一併帶連線狀態與 /sys 的連線速度；卡片顯示與否看即時狀態，快取說 DOWN 但即時 UP 時重讀一次硬體資料補 IP。卡片標題加連線速度（10 Gb/s、1 Gb/s、Mb/s）。Wi-Fi 關掉時卡片自動消失。真機：接上 10GbE 後標題「有線網路 · 10 Gb/s」。

## 2026-09-25

*Roll back an update from the same tab (real .deb kept before each install, sha256-checked, apt-simulated before restoring); npm global CLI tools (Claude Code, Gemini CLI, OpenClaw…) with author/repository shown, Node-engine compatibility checked, running processes detected and restartable; guided Node major-version upgrade through a root-owned helper with a polkit policy; SSH/NVIDIA Sync port forwarding; six rounds of external review fixes. Tested end to end on this GX10: curl update → roll back → update again; Node 22 → 24 with backup kept; OpenClaw gateway restarted after its update.*

**今天的主題**（細節在下方各條）：
1. **降回上一版**：更新前依模擬結果保留所有會被換掉的舊版 .deb（含相依帶入），核對 apt 索引 sha256；降回走 `pkexec apt-get install --allow-downgrades --no-remove`，先模擬再確認，會移除別的軟體就拒絕。真機跑過「更新 curl → 整組降回 → 驗證 → 裝回」。
2. **npm 全域套件**：列出、更新、降回；顯示描述、作者、倉庫、授權與可信度說明；只裝與目前 Node 相容的版本（核對 engines，不退回 @latest）；偵測正在執行的程序，更新後給一鍵重啟，跑舊版檔案的程序常駐提醒。
3. **Node 大版本升級**：依官方 LTS 表提示；準備（備份舊版、切 NodeSource 倉庫）→ 模擬確認安裝 → 可恢復；由管理員另裝的 root 擁有 helper 執行，配 polkit policy，密碼視窗寫明是 Spark Center。真機 22 → 24。
4. **遠端**：POST 的 Host 檢查接受任意本機埠號，SSH -L 與 NVIDIA Sync 的 Custom 連線可用。
5. **外部 review 六輪**：全部重現後修，見「修正」。


### 新增

- **唯讀模式**：`SPARK_CENTER_READONLY=1` 時所有 POST 回 403、動作按鈕全部隱藏、頂欄顯示「唯讀模式」；看的功能全部照常。給第一次裝的人先跑一段時間確認它只讀，再打開更新功能。第二個實例實測：五個修改型端點全 403、GET 正常、頁面藏掉按鈕。
- **uninstall.sh 與「它會碰你系統的什麼」**：README 列出 install.sh 寫入的每個路徑與選用的 root 項目；uninstall.sh 一鍵移除使用者層級的東西，並印出選用 root 項目的移除指令。

- **NodeSource 主版本升級**：npm 面板可依官方 LTS 排程準備升級，先保存並核對舊版 SHA-256，再切換既有來源；另一次模擬與確認才安裝。支援取消準備與恢復來源／原 Node，回復前列出可能不相容的 npm 工具。只保留最近一次主版本備份，不處理 nvm、snap、自訂或多份來源。隔離流程測試與實際唯讀 API／介面驗證；尚未實測提權切換、安裝及恢復。

- **npm 套件執行中偵測與更新後重啟**：npm install -g 直接覆蓋目錄，正在跑的程序之後才載入的模組會讀到新版，新舊混用會出錯。面板掃 /proc 找正在執行該套件檔案的程序，標「執行中 N」（滑過看 PID 與 systemd 服務）；更新確認先講「更新後需要重啟」；更新完在工作卡片列出每個程序，屬於 systemd 使用者服務的給一鍵重啟（不需密碼；後端只接受即時掃描到的服務名），不是服務的只提醒。真機：OpenClaw gateway（openclaw-gateway.service）被正確偵測。
- **npm 全域套件面板**（更新分頁）。Claude Code、Gemini CLI、OpenClaw 這類 CLI 工具是 `npm -g` 裝的，apt／snap／flatpak 都看不到。面板用 `npm ls -g` 列全部、`npm outdated -g` 標新版（查不到新版時顯示「—」並掛紅字，不顯示 0），單顆或整批更新；裝在使用者的 prefix，不需要密碼。更新前把「目前版 → 新版」記進降回索引（不留檔案，registry 保留所有版本），降回面板可重裝指定版號。真機驗證：@playwright/cli 0.1.5 → 0.1.21 → 降回 0.1.5。pip 與 Docker 映像刻意不做：系統 pip 套件歸 apt 管（PEP 668），venv 版本是專案鎖的，Docker pull 新映像不等於容器已更新。
- **POST 的 Host 檢查放寬到任意本機埠號**，SSH 轉埠與 NVIDIA Sync 的 Custom 連線能用（實測 Sync 配的是 localhost:36027）。

- **降回上一版**（更新分頁新面板）。從這裡更新前，會先把每個要被換掉的舊版 .deb 留一份到 data/rollback/（保留最近 5 次，每檔上限 150 MB），來源依序是本機 apt 快取、來源伺服器的 pool、Launchpad（Ubuntu 官方套件永久保留）。ESM 需授權、第三方倉庫不留舊版、檔案太大的，誠實標「未保留」與原因。降回用 pkexec 跑 `apt-get install --allow-downgrades` 裝保留的 .deb（跳密碼視窗）；降回後該更新會再次出現在清單，不勾就不會再裝上。起因：Ubuntu 來源只發布最新版，「清 apt 快取」又會清掉本機舊 .deb，出事時沒有退路。

### 修正

- **Node 升級的 polkit policy**：helper 改成可直接執行（shebang `python3 -I`），pkexec 直接跑它而不是 `pkexec python3 …`，配上 `tools/spark-center-node-source.policy`（action `io.github.tiong6.spark-center.node-source`，exec.path 指向安裝後的 helper，auth_admin 每次都要密碼）。密碼視窗因此寫「Spark Center 要切換 NodeSource 倉庫並安裝或恢復 Node.js」，不再是「要以 root 執行 python3」。README 安裝步驟多一行。
- **Node 主版本升級 review 修正**：確認摘要與再次模擬比對只使用 Inst／Conf／Remv 交易行，排除非 root apt 模擬的 NOTE，避免提權後永遠被判定計畫改變。提權入口改為管理員另行安裝的 root-owned helper，拒絕可寫目錄、符號連結與不符版本；未安裝時停用並明示。準備視窗補上官方表的預期完整版本（以切換後 apt 索引為準），安裝成功改為灰字紀錄。真 apt 唯讀模擬與隔離／介面測試通過；尚未實測提權升降。

- **POST 的 Host 檢查改成「本機位址、埠號不限」。** SSH 轉埠或 NVIDIA Sync 的 Custom 連線在遠端那台常用別的本機埠號，瀏覽器送來的 Host 是那個埠，原本會被 403。DNS rebinding 靠的是非本機主機名，放寬埠號不影響這道防線；curl 驗過 localhost:任意埠放行、evil.example 與 localhost.evil.com 仍擋、跨站 Origin 與 text/plain 仍擋。README 加「Remote access」一節。

- **降回上一版：第三輪外部 review 的 4 個問題。**（1）原本走 aptdaemon 的 `install_file`，它最後跑 `DebPackage.check()`，預設拒絕比已安裝舊的版本（force=True 也一樣），所以按了根本降不回去；改成 pkexec 跑 `apt-get install -y --allow-downgrades <保留的 .deb>`，相依由 apt 解。（2）從來源伺服器（HTTP）或 Launchpad 下載的檔案原本只核對 Package/Version 欄位；現在核對 apt 索引裡該版本的 sha256，不符不採用；索引已無此版時只接受 Launchpad 的 HTTPS，並在面板標明「無法再核對雜湊」。（3）舊版從索引消失後，`installed.origins` 是空的，原本因此永遠不會試 Launchpad，而那正是需要備援的時候；改看該套件任一版本的來源。（4）原本只備份勾選的套件，漏掉相依帶動一起換掉的（例如只勾 curl 會一起升 libcurl4t64）；現在先模擬升級，依實際變更備份，面板標「相依帶入」，並多一顆「整組降回」，一次把整組交給 apt。真機實測（2026-09-25）：更新 curl → 整組降回 → curl 與 libcurl4t64 回到 10.13，dpkg 乾淨、無壞相依、curl 可用、libcurl4t64 的自動安裝標記保留；再裝回最新版。
- **降回上一版：第四輪 review 的 2 個問題。**（1）降回原本直接 `-y` 執行，沒有先展示相依變更；舊函式庫可能讓 apt 選擇移除依賴新版的應用程式，確認視窗卻沒說。現在降回和更新一樣走「apt-get -s 模擬 → 展示實際變更 → 確認」，模擬結果有移除就拒絕並列出是誰，真正執行時再加 `--no-remove` 雙重保險。（2）更新走 aptdaemon 時設定檔衝突一律保留現有版本，改用 apt-get 後沒有對應選項；服務的 stdin 是 /dev/null，dpkg 問不到人會中途失敗、留下未設定的套件。現在加 `--force-confdef --force-confold`，沿用同一政策。 降版失敗時另跑 `dpkg --audit`（不需 root）：有套件未完成設定就在錯誤訊息裡指引 `sudo dpkg --configure -a`，狀態完整也明講；修復本身需要再一次密碼，不自動做。

- **併發量測全部失敗仍記成成功**（外部 review 第二輪）。全敗改回 error 不寫歷史；部分失敗標「N/M 個請求失敗」、前端顯示失敗數、平均值防除零。
- **快速切換分頁時舊的監控請求關掉新分頁的輪詢**。startMon 在等待硬體資料回來後檢查監控分頁是否仍顯示。
- **模型「保持載入」選項被 10 秒一次的重繪重設**。選擇按模型記住並在重繪後還原。
- **修改型 API 沒有驗證請求來源**。只綁 127.0.0.1 擋不住瀏覽器裡任何網頁對本機發的跨站 POST（text/plain 的簡單請求不做 preflight，直接送到），清垃圾桶、docker prune、刪 Ollama 模型這幾個不需要密碼的動作會被外站網頁觸發。現在三道關：Host 必須是本機加本埠；有 Origin 就必須是自己；Content-Type 必須是 application/json（強迫瀏覽器 preflight，本服務不回應 OPTIONS，跨站就死在瀏覽器裡）。不符回 403 並說明原因。
- **fwupd 查詢失敗被當成「已是最新」**。`fwupdmgr` 逾時或 LVFS 連不上時回傳 None，原本被轉成空清單，畫面顯示所有裝置 Up to date。現在查不到的數字顯示「—」、每個裝置標「不明（查詢失敗）」、上方掛紅字說明，且失敗結果不快取。
- **清空垃圾桶漏掉隱藏檔且永遠回報成功**。bash 的 `*` 不含點開頭的檔案，後面的 echo 又蓋掉 rm 的退出碼。改用 `find -mindepth 1 -delete`，用 `&&` 串接，刪除失敗就退出碼非 0、不印「已清空」。

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

### 變更（結構）

- **前端拆檔**：index.html 只留骨架，CSS 到 static/css/app.css，JS 依分頁拆成 static/js/ 11 支（i18n、core、updates、apps、disk、history、hw-common、llm、hardware、monitor、main），`<script src>` 依原順序載入、共用全域作用域，由 server.py 直接提供，沒有建置步驟。純搬移：拆出的檔案串接回去與原文逐字相同；拆前拆後七頁中英 DOM 正規化比對相同、11 支 API 回應相同。
- **tools/check.sh**：改動前跑的靜態檢查，含每支 JS 的語法、CSS 同一選擇器重複定義（目前 8 處既有的只警告）、HTML 重複 id、中英字串表 key 與佔位符對齊。

### 變更（介面對齊 macOS／Windows 的同類畫面）

- **磁碟分頁頂端改成分段條**（macOS 儲存空間那種）：模型／Docker／應用資料／快取／日誌／垃圾桶各一段一色，附圖例；「其他」是即時已用量減掉掃到的總和，不是猜的；分類加總比即時用量多時照實標出。三個清單（分類、家目錄第一層、大檔案）的數字下方加相對長度橫條。清理鈕加 inline SVG 圖示。
- **應用程式分頁加圖示、安裝大小、安裝日期**（Finder／Windows 設定的應用程式列表）。圖示依 freedesktop 規則在圖示主題目錄建索引，snap 沒有 .desktop 圖示的轉發 snapd 的 /v2/icons；找不到就畫首字母方塊。大小與日期：apt 用 installed_size 與 dpkg 檔案清單的 mtime，snap 用 snapd API，flatpak 用 list 的 size 欄與部署目錄 mtime；拿不到顯示「—」。識別碼移到名稱下方小字。
- **硬體分頁頂端加「關於這台機器」**（macOS「關於這台 Mac」）：機器圖、型號、晶片、統一記憶體、儲存、DGX OS、序號、BIOS 六格大字，細節表格不動。
- **監控分頁卡片高度對齊**：副標單行省略、滑過看全文；Wi-Fi 四行狀態橫跨整張卡兩欄排；取樣說明收進 ⓘ。

### 新增

- **更新分頁：「重新整理後新出現」標籤**。按「重新整理來源」之前清單裡沒有的項目會標出來，滑過說明是索引更新後才發布的，不是上次沒裝到。起因：裝完一個更新、按重新整理，ESM 剛好推了新的安全更新，看起來像「可用更新不會消失」。
- **工作卡成功後 15 秒自動收起**，按鈕倒數顯示；視窗在背景或滑鼠停在卡片上就暫停，更新跑完時人不在座位也不會錯過結果；失敗的不自動收。
- **頂欄加 DGX Dashboard 連結**（用 dgx-dashboard 套件自己的圖示，另開視窗；埠號照 NVIDIA 啟動腳本的邏輯讀 ports.env，沒裝就不顯示）。Spark OS 韌體 OTA 仍要在那邊做。
- **監控卡片可拖曳排序、可隱藏、可還原版面**，記在瀏覽器；拖曳期間暫停重繪。網路卡標題改「Wi-Fi 網路」／「有線網路」，核心介面名（wlP9s9）退到副標。
- **頂欄機型改讀 DMI**（廠商＋型號，ASUSTeK 縮成 ASUS），換到 Dell／HP 的 GB10 會顯示對應機型；原本寫死。
- **介面中英雙語**。前端字串表 547 個 key、後端訊息表 211 個 key；預設跟瀏覽器語言走（zh 開頭顯示中文，其餘英文），頂欄「中／EN」切換並記住。限定語（推測、依 modinfo 判定、無法排除）逐句保留，不因翻譯變軟；套件名、模型名、路徑、指令、使用者自己輸入的校準備註不翻。審收方式：英文模式七頁 DOM 殘留中文為 0、十支 API 英文回應僅剩真實路徑、中文模式與改動前 DOM 逐頁比對相同。
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
