# HANDOFF — 現在的接力棒

> 唯一接力棒；先看 git status 與 git log，這份是快照（2026-09-25 中秋收工，main = origin/main，工作樹乾淨）。

## 現況

- **機器**：ASUS Ascent GX10，DGX OS 7.6.0。Node 24.21.0（NodeSource node_24.x，今天從 22 升上來；22.23.3 的備份與狀態在 root 擁有的 /var/lib/spark-center/node-source/）。npm 11.19.0。OpenClaw 2026.9.6，gateway 服務已重啟跑新版。apt、fwupd 目前都沒有待裝更新。
- **已裝的選用 root 項目**：/usr/local/libexec/spark-center/node_source.py（與 repo 一致）與 /usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy；sudoers 兩條（dmidecode、nvme smart-log）。
- **服務**：spark-center user service 正常，非唯讀模式。
- **論壇**：Projects 區的介紹文 384218（有一位網友說會試用）；今天在支援區發了 384257「Three DGX Dashboard update problems」，等 NVIDIA 回覆。有人回了先貼給模型看再回。

## 今天做完的（38 個 commit，CHANGELOG 2026-09-25 有總覽）

1. 降回上一版：更新前依模擬結果保留舊版 .deb（含相依帶入）、核對索引 sha256；降回走 pkexec apt-get --allow-downgrades --no-remove，先模擬、會移除就拒絕；面板對照目前版本、去重、折疊。真機跑過 curl 更新→整組降回→裝回。
2. npm 全域套件面板：列出／更新／降回；描述、作者、倉庫、授權；只裝 engines 相容版（npm 自帶 semver 核對，不退回 @latest）；執行中程序偵測（含 bin 符號連結）、跑舊版檔案標紅、systemd 使用者服務一鍵重啟。
3. Node 大版本升級（astra6 實作、多輪 review 修正）：準備→模擬確認安裝→恢復；root 擁有 helper + polkit policy；完成判定在 helper 的 effective_state 一處；apt 清單在流程中鎖住 nodejs。
4. 遠端：POST Host 檢查接受任意本機埠，NVIDIA Sync Custom（localhost:隨機埠）實測可用。README 有 Remote access。
5. 唯讀模式 SPARK_CENTER_READONLY=1、uninstall.sh、README「它會碰你系統的什麼」。
6. DGX Dashboard「Update Available」老問題查清：root 後台快照，`sudo systemctl restart dgx-dashboard-admin.service` 立即解。

## 沒驗到的

- Node helper 自己的「確認安裝」步驟（那次是從 apt 清單裝的）與「恢復原版本」。要驗：面板按「檢查影響並恢復原 Node 版本」→ 回 22 → 再升一次。
- 降版中途失敗的 dpkg 修復提示（只有隔離測試）。

## 固定成本與教訓

- 改了 tools/node_source.py 就要重跑 README 的 `sudo install … node_source.py`，否則面板顯示「版本不符」停用。
- 重啟服務前先看 `/api/job` 是 idle（曾在使用者更新中重啟，頁面卡住）。
- 測「應被拒絕」的請求用隔離測試，不要打會啟動工作的真端點（曾因此啟動真的 apt 安裝、跳密碼視窗）。
- `git add` 列檔名，不用 `-A`（曾把 data/rollback 的 .deb commit 進公開 repo；已移出追蹤並 .gitignore，歷史保留）。
- MSG 表裡 astra6 加的鍵是 4 空格縮排，替換前先看實際縮排。

## 下一步候選（都不急）

- 實跑 Node「恢復原版本」再升一次，把最後兩段補驗。
- CSS 8 個重複選擇器順手併掉（check.sh 只警告）。
- 論壇回覆後視情況更新 README 的 screenshots（用英文 UI、隱藏 Wi-Fi 卡）。
- CI 等第一個外部 PR 再說（使用者決定）。
- 沒有要做的：pip／Docker 映像更新（理由在 README）。
