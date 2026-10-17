# HANDOFF — 現在的接力棒

> **這是唯一一根棒子。收工時【覆蓋】它，不要另開新檔、不要加日期。**
> 舊棒在 `git log --follow HANDOFF.md`。
> 【開工的人】下面的「下一步」和任何待辦，先 `git log`／看程式碼確認還沒做——
> 快照會過期，「棒子說待辦、其實早出貨」是這個玩法最常見的坑。

**上次收工：2026-10-17 晚** ｜ 工作樹乾淨 ｜ `tools/check.sh` 全過 ｜ 本機 HEAD 84a67d4，origin/main ec57fad（差一個：工單 commit 未推，使用者說推才推）｜ 11001 服務跑的是 6ec17bf（其後三個 commit 只有 docs／css，css 直接從磁碟服務、已生效）

## 下一步：執行 `docs/WORKORDER-text-diet.md`（介面文字減法）

使用者的判斷：介面文字過多，要「圖形化減法」，但先討論了可行性再開工單。工單已拍板的內容：
- 三類文字三種處理：來源／判定說明收進共用 ⓘ；誠實標註留結論、理由進 ⓘ；設定頁不減字改兩欄。
- 更新分頁三處圖形化：流程步驟列、Dashboard 對照兩張卡、分階段策略分段按鈕。
- 數字目標與審收方法在工單裡；量測工具 `tools/measure_prose.mjs`（需 `HC=<空目錄>`、服務在 11001）。

從哪裡下手：先做共用 ⓘ 元件（core.js，沿用 app.css 第 222 行 `.montools .info` 抽成全域），再拿硬體分頁當樣板做一頁，給使用者看過再推到其他頁——這是討論時說好的順序。

## 這一場做了什麼（git log 是真相，這裡只給脈絡）

| commit | 脈絡 |
|---|---|
| f658ed1 | 起因：Dashboard 更新 19 裝 18，剩 snapd 被 Ubuntu 分階段放量（phased 40%）保留；查出 Spark Center 的 mark_upgrade 路徑會無聲越過放量。使用者 cross-brainstorm 後拍板 1A 2A 3A 4bc（可裝但明確越線／兩段式計數／全機策略開關／Dashboard 對照＋FAQ）。 |
| 6ec17bf | 使用者更新 snapd 時看到「詳細記錄中都沒有文字」：是留舊版階段從 Launchpad 抓 32 MB 花 80 秒沒進度。當晚降回再更新時真機驗過新進度顯示。 |
| cff8f6c | 使用者問「Spark Center 是不是不夠實用」→ 結論是小眾不是不實用，該做的是第一印象。README 從 3,800 字降到 1,140，功能表搬 docs/FEATURES.md，七張真機截圖。素材用「降回 snapd 再更新回來」取得（使用者同意）。 |
| ec57fad | GitHub About／topics 用 gh 設好；social preview 使用者已手動上傳。 |
| 84a67d4 | 文字減法工單（本棒的下一步）。 |

## ⏸ 使用者明確擱置（別自作主張開工）

- 第一印象清單的第 5（一次授權多個 sudoers）、6（唯讀模式做成 app 內開關）、7（首次開啟引導）、8（論壇帖改寫）、9（分階段 bug 單獨回報帖）：討論過、使用者只選了 1–4。沒說不做，但也沒排程。
- 全機分階段策略開關的 **pkexec 寫檔路徑未在真機按過**（要密碼視窗）。下次有分階段套件出現時切一次「搶先安裝」再切回來即可驗；UI 上 Dashboard 後台何時重讀 apt 設定標為「推測」。

## 這一場的教訓（只寫會重複發生的）

- python-apt 的 `is_upgradable`／`mark_upgrade()` 不看 Ubuntu 分階段；判定要用 `pkg.phasing_applied`。任何「列清單→逐個 mark」的新功能都會再踩。
- 截圖只用真資料：清單空的時候，用降回功能製造真實待更新狀態（需使用者同意、跳兩次密碼），不造假 DOM。
- headless Chrome 截圖前用 TreeWalker 把序號／UUID／主機名換掉（`scratchpad/shot.mjs` 的做法），不要事後修圖。
