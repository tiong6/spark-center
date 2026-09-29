#!/bin/sh
# 前後端靜態檢查：語法、CSS 選擇器重複定義、HTML 重複 id、中英字串表對齊。改完先跑這個再重啟服務。
set -e
cd "$(dirname "$0")/.."
python3 -m py_compile server.py tools/node_source.py && echo "py_compile OK"
python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('tools/spark-center-node-source.policy')" && echo "polkit policy XML OK"
for f in static/js/*.js; do node --check "$f" || { echo "node --check FAILED: $f"; exit 1; }; done; echo "node --check OK ($(ls static/js/*.js | wc -l) files)"   # 迴圈裡的失敗要自己擋，set -e 對 && 左邊的迴圈不生效
python3 tools/lint_frontend.py
