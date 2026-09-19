# Spark Center

收工用 /handoff，棒子在根目錄 HANDOFF.md（唯一一根，覆蓋不另開）。
改動流程：`python3 -m py_compile server.py` 與 JS 語法檢查 → `systemctl --user restart spark-center` → curl API → 瀏覽器 DOM 驗證 → commit → push。
大段替換前先列出範圍內所有頂層定義；改完把每個分頁都點一遍。
對外文件與 commit 訊息不提本專案的舊名。
