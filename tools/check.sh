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
_ps=$(mktemp); python3 tools/test_print_stack.py > "$_ps" 2>&1 && echo "print stack / snap busy tests OK" || { cat "$_ps"; exit 1; }; rm -f "$_ps"   # 純判斷＋假 /proc，不碰 snapd
_cl=$(mktemp); python3 tools/test_changelog_meta.py > "$_cl" 2>&1 && echo "changelog meta tests OK" || { cat "$_cl"; exit 1; }; rm -f "$_cl"   # 純解析，不碰網路
_ur=$(mktemp); python3 tools/test_update_risk.py > "$_ur" 2>&1 && echo "update risk tests OK" || { cat "$_ur"; exit 1; }; rm -f "$_ur"   # 假抓取器，不碰網路
_ar=$(mktemp); python3 tools/test_auto_refresh.py > "$_ar" 2>&1 && echo "auto refresh tests OK" || { cat "$_ar"; exit 1; }; rm -f "$_ar"   # 純判斷＋暫存設定檔，不碰 apt／loginctl
_ro=$(mktemp); python3 tools/test_refresh_outcome.py > "$_ro" 2>&1 && echo "refresh outcome tests OK" || { cat "$_ro"; exit 1; }; rm -f "$_ro"   # 純判斷，不碰 apt／journal
_fp=$(mktemp); python3 tools/test_flatpak_progress.py > "$_fp" 2>&1 && echo "flatpak progress tests OK" || { cat "$_fp"; exit 1; }; rm -f "$_fp"   # 真實 CLI 輸出當測資，不碰 flatpak
_uu=$(mktemp); python3 tools/test_urgent_updates.py > "$_uu" 2>&1 && echo "urgent updates tests OK" || { cat "$_uu"; exit 1; }; rm -f "$_uu"   # 純判斷＋暫存檔，不碰 apt／notify-send
_rs=$(mktemp); python3 tools/test_rollback_skip.py > "$_rs" 2>&1 && echo "rollback skip tests OK" || { cat "$_rs"; exit 1; }; rm -f "$_rs"   # 假 apt／假下載，暫存目錄
_pe=$(mktemp); python3 tools/test_peer.py > "$_pe" 2>&1 && echo "peer lookup tests OK" || { cat "$_pe"; exit 1; }; rm -f "$_pe"   # 真的本機 socket，查自己
_aw=$(mktemp); python3 tools/test_ai_watch.py > "$_aw" 2>&1 && echo "ai board tests OK" || { cat "$_aw"; exit 1; }; rm -f "$_aw"   # 真實資料剪的 fixtures，不碰網路
_as=$(mktemp); timeout 120 python3 tools/test_ai_security.py > "$_as" 2>&1 && echo "security regression tests OK" || { cat "$_as"; exit 1; }; rm -f "$_as"   # 本機滴流伺服器、失敗刷新、結束與切換時序、UID
_pc=$(mktemp); timeout 120 python3 tools/test_perf_caches.py > "$_pc" 2>&1 && echo "perf cache tests OK" || { cat "$_pc"; exit 1; }; rm -f "$_pc"   # 假盤點函式，重現快取失效／世代的 8 個案例
_rp=$(mktemp); python3 tools/test_release_prompt.py > "$_rp" 2>&1 && echo "release prompt tests OK" || { cat "$_rp"; exit 1; }; rm -f "$_rp"   # 暫存檔＋假 pkexec，不碰 /etc；有 update-manager-core 就和 Ubuntu 的 MetaRelease 交叉比對

python3 tools/test_backend_modules.py
_dc=$(mktemp); python3 tools/test_disk_check.py > "$_dc" 2>&1 && echo "disk check tests OK" || { cat "$_dc"; exit 1; }; rm -f "$_dc"   # 隔離：mock sudo／tune2fs。看結束碼不看最後一行：ResourceWarning 在 GC 時才印、會隨機排在 OK 之後
python3 tools/test_stability.py
python3 tools/test_model_compat.py
python3 tools/test_llm_models.py
python3 tools/test_llm_switch.py
node tools/test_llm_ui.js

node tools/test_monitor_ui.js
node tools/test_updates_ui.js
