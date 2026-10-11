#!/bin/sh
# 前後端靜態檢查：語法、CSS 選擇器重複定義、HTML 重複 id、中英字串表對齊。改完先跑這個再重啟服務。
set -e
cd "$(dirname "$0")/.."
python3 -m py_compile server.py backend/*.py tools/node_source.py tools/model_inventory.py tools/ollama_guard.py tools/llm_switch.py && echo "py_compile OK"
python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('tools/spark-center-node-source.policy')" && echo "polkit policy XML OK"
for f in static/js/*.js; do node --check "$f" || { echo "node --check FAILED: $f"; exit 1; }; done; echo "node --check OK ($(ls static/js/*.js | wc -l) files)"   # 迴圈裡的失敗要自己擋，set -e 對 && 左邊的迴圈不生效
python3 tools/lint_frontend.py
python3 tools/test_npm_safe.py 2>&1 | tail -1 | grep -q "^OK" && echo "npm safe-update tests OK" || { python3 tools/test_npm_safe.py; exit 1; }   # 隔離測試，不碰真 npm／systemctl
python3 tools/test_gpu_hot.py 2>&1 | tail -1 | grep -q "^OK" && echo "GPU overheat tests OK" || { python3 tools/test_gpu_hot.py; exit 1; }

python3 tools/test_backend_modules.py
python3 tools/test_stability.py
python3 tools/test_model_compat.py
python3 tools/test_llm_models.py
python3 tools/test_llm_switch.py
node tools/test_llm_ui.js

node tools/test_monitor_ui.js
