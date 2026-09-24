# Spark Center

收工用 /handoff，棒子在根目錄 HANDOFF.md（唯一一根，覆蓋不另開）。
改動流程：`tools/check.sh`（py_compile、每支 JS 的 node --check、CSS 重複選擇器、重複 id、中英字串表對齊）→ 先 `curl -s 127.0.0.1:11001/api/job` 確認 status 是 idle（工作狀態只在記憶體，重啟會讓頁面停住）→ `systemctl --user restart spark-center` → curl API → 瀏覽器 DOM 驗證 → commit → push。
前端是 index.html（骨架）＋ static/css/app.css ＋ static/js/*.js（依分頁拆，`<script src>` 依序載入、共用全域作用域，順序不能亂）；沒有建置步驟。新字串同時進 static/js/i18n.js 的 STR 與 server.py 的 MSG 兩邊。
Commit 訊息：英文標題、中文內文。
大段替換前先列出範圍內所有頂層定義；改完把每個分頁都點一遍。
對外文件與 commit 訊息不提本專案的舊名。
