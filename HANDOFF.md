# HANDOFF — 現在的接力棒

> **這是唯一一根棒子。收工時【覆蓋】它，不要另開新檔、不要加日期。**
> 舊棒在 `git log --follow HANDOFF.md`。
> 【開工的人】下面的「下一步」和任何待辦，先 `git log`／看程式碼確認還沒做——快照會過期。

**上次收工：2026-11-21 午後** ｜ 工作樹乾淨 ｜ `tools/check.sh` 全過 ｜ 本機 HEAD = 84f4343 的下一個（這根棒子的 commit）｜ **origin/main 還在 c4d3121，本週 10 個 commit 未推**（使用者要明確說「推」才推）｜ 11001 服務跑的是 84f4343，job idle

## 下一步

1. **推 GitHub**：等使用者點頭。推之前沒有別的必做事項；資安互審（Codex 五輪）與效能互審（Codex 五輪）都到 APPROVED／PASS。
2. 推完後使用者自己按一次 AI 動態分頁的「更新」把 VS Code 的 Claude Code 擴充升到 2.1.330（選單就會有 Fable 5.2）；「全部更新（14 個）」與單一「更新」鈕都**沒在真機按過**，第一次按就是上線。
3. README 缺 AI 動態分頁的英文截圖（其他分頁都有）；發論壇前補。

## 這一場做了什麼（git log 是真相，這裡只給脈絡）

| commit | 脈絡 |
|---|---|
| da5f4e0 | 急迫更新主動提示（桌面通知＋頂欄徽章）、工作卡「略過保留舊版」、頂欄 ⏻ 結束。起因：使用者問「有急迫度能不能主動提示」；Launchpad 26 KB/s 把一次更新拖 20 分鐘。 |
| 07419fa | 每筆 POST 記來源程序鏈。起因：一批 14 個套件的安裝在使用者說「沒按」的情況下開始，舊 log 查不出是誰。 |
| 4348f9c…6ecfbd4 | 新分頁「AI 動態」（原名 AI 布告欄）：新模型表（OpenRouter）、本機 AI 工具落後幾版（Claude Code VS Code／CLI、ChatGPT 擴充附的 Codex）、VS Code 擴充對照 Marketplace＋全部更新、當天有新模型亮點、只支援 VS Code 的說明。直接回答了「VS Code 選單為什麼沒有 Fable 5.2」：擴充 2.1.318，Fable 5.2 在 2.1.321。 |
| 6d6b8c5 | 資安：所有 POST 要求連線 UID＝服務 UID（/proc/net/tcp 四元組）、GET 檢查 Host、下載硬期限／32 MB／只 https／POST 不跟轉址、來源 log 不收 argv、兩條 ReDoS。 |
| 84f4343 | 效能：共用唯讀 apt.Cache、先顯示分頁再載清單、apps 商店查詢背景、hwmon 背景重讀；Codex 審出 14 個快取世代缺陷全修。 |

## ⏸ 擱置／已知限制（別自作主張開工）

- **其他本機帳號的 UID 檢查沒用真的第二個帳號試過**（要 sudo 密碼），只有替身 /proc 資料的測試。這台是單人機器。
- 下載總期限管不到 DNS 解析與 TLS 握手（標準庫限制），已寫在註解。
- Cursor／Windsurf／VSCodium 的擴充沒涵蓋（它們用 Open VSX）；做法已和使用者討論，使用者說先不用。
- apt 鎖裡還有慢工作（list_updates 的 modinfo、apps_apt 掃 dpkg 清單）：會讓同時進來的另一個 apt 讀取等，無死鎖、不在熱路徑。
- `_sensors_read()` 全部讀失敗會回 []，畫面像「沒有感測器」——既有行為，Codex 點出，未改。
- DGX Dashboard 強制重開機等 10 月擱置的項目仍擱置（見 `git log --follow HANDOFF.md` 上一根）。

## 這一場的教訓（只寫會重複發生的）

- **先量再改**：效能問題的根因（每請求建 apt.Cache、hwmon 快取到期重讀 NVMe）和兩邊事前的猜測都不同；Codex 的靜態推測「GPU 鎖裡的 nvidia-smi」用對照實驗一試就否決。量法：`curl -w time_total` 冷熱各打、`cProfile` 直接 import server 呼叫函式、CDP 包函式計時。
- **Codex 做正確性／併發審查很強、做效能推測要對照量測**；資安審查的「攻擊情境」措辭會被 OpenAI 內容過濾擋，換新 session＋中性措辭。
- **快取失效要用世代計數，不要用時間戳或「清一下」**：秒級 generated 會撞、POST 前清會被動作中的查詢寫回、盤點中的失效會被盤點結果蓋掉。進行中的計算在寫回前比對世代。
- `apt_pkg.init_config()` 不會移除已刪掉的鍵；重讀設定要先清。
- Linux 的 `shutdown(SHUT_RD)` 只叫醒當下卡住的 recv，之後到的資料照樣讀得到；要中斷下載得「shutdown＋每塊看旗標」兩道。
- 服務的 HTTP log 不記成功請求時，事後查不出是誰按的；修改型請求一律記來源。
