# HANDOFF — 現在的接力棒

> 唯一接力棒；先看 git status 與 git log，這份是快照。

## 2026-09-25：NodeSource 主版本升級

更新分頁的 npm 面板加入 Node 主版本流程，前端 static/js/node-source.js，後端 server.py，提權 helper tools/node_source.py。

- GET /api/node 辨識標準單一 NodeSource deb822 來源、現有金鑰與服務使用的 /usr/bin/node，目標來自官方 Node LTS 表。
- POST /api/node/preview 唯讀；POST /api/node/action 重新核對確認摘要，再透過現有 Job/pkexec 執行。prepare、install、restore 分開確認。
- prepare 保存 apt 索引 SHA-256 驗證的原版 .deb 後切來源，刷新並確認可信目標；失敗嘗試恢復原來源，復原失敗有持久狀態可重試。
- install/restore 先顯示 apt 模擬；實際執行禁止移除套件、保留設定檔。恢復前檢查本機全域 npm engines，警告不相容／未知者。
- root 狀態與單份備份在 /var/lib/spark-center/node-source/，每檔上限 150 MiB。下次主版本升級取代前次備份，有明確告知；不還原 npm 工具或專案／服務。

## 已驗證

- python3 tools/test_node_source.py：15 tests，OK。使用臨時來源與備份、替代 apt/提權呼叫，沒有改系統。
- tools/check.sh：Python、12 支 JS、89 個 HTML id、中英 STR 631 與 MSG 252 keys 通過。8 個既有 CSS 重複選擇器仍只警告。
- 先確認 /api/job idle 再重啟 spark-center user service。
- 實際唯讀 API：Node 22.23.3-1nodesource1、目標 LTS 24；prepare 預覽正確；過期確認 409，非目標 LTS 400，job 保持 idle。
- Playwright：中英文準備預覽、七分頁顯示正常；注入回復資料時顯示 apt 模擬與不相容 npm 警告。注入測試不是實機回復驗證。

## 尚未驗證／機器狀態

**尚未實際跑 pkexec 切來源、安裝 Node、恢復 Node 的整條路。** 這次只實作與隔離／唯讀驗證，不能宣稱真機升降成功。機器仍是 Node v22.23.3，來源仍 node_22.x，未建立系統 Node 回復狀態。

## 原有待辦與慣例

英文監控／更新截圖已在 README；監控圖隱藏網路卡避免揭露 SSID/IP，舊圖仍存在 Git 歷史。論壇文章尚未發。
前端字串進 i18n.js 的中英 STR，後端顯示訊息進中英 MSG。套件名、路徑、指令與使用者資料不翻。
變更流程與 commit/push 慣例見 CLAUDE.md；此專案無建置步驟。
