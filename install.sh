#!/bin/sh
# Spark Center 安裝：生成 systemd user 單元與桌面捷徑（路徑依 repo 位置），啟用服務。不需要 root。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
# 埠：預設 11001；被別的程式占住就往後找第一個空的（最多到 11020），寫進服務單元與桌面捷徑，兩邊一致。
PORT="${SPARK_UPDATER_PORT:-11001}"
if command -v ss >/dev/null 2>&1; then
  p="$PORT"; while [ "$p" -le 11020 ] && ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$p\$"; do p=$((p+1)); done
  [ "$p" != "$PORT" ] && echo "Port $PORT is in use; using $p instead. / 埠 $PORT 被占用，改用 $p。"
  PORT="$p"
fi
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@PORT@|$PORT|g" "$ROOT/spark-center.service.in" > "$HOME/.config/systemd/user/spark-center.service"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@PORT@|$PORT|g" "$ROOT/app/spark-center.desktop.in" > "$HOME/.local/share/applications/spark-center.desktop"
for s in 256 128 64 48; do mkdir -p "$HOME/.local/share/icons/hicolor/${s}x${s}/apps"; cp "$ROOT/app/spark-center-$s.png" "$HOME/.local/share/icons/hicolor/${s}x${s}/apps/spark-center.png"; done
cp "$ROOT/app/spark-center.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/spark-center.svg"
[ -f "$HOME/.local/share/icons/hicolor/index.theme" ] || cp /usr/share/icons/hicolor/index.theme "$HOME/.local/share/icons/hicolor/index.theme" 2>/dev/null || true
gtk-update-icon-cache -q -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now spark-center
echo "Spark Center is running: http://127.0.0.1:$PORT  (also in the application menu)"
echo "Spark Center 已啟動：http://127.0.0.1:$PORT（應用程式選單也有）"
echo "Optional features (serial numbers, NVMe health, Node upgrades, title-bar-free window, open at login): see the Setup tab in the app."
echo "選用功能（序號、NVMe 健康、Node 升級、無標題列視窗、登入自動開啟）：在 app 的「設定」分頁。"
# 裝完直接開 app 視窗；沒有桌面（SSH 進來裝）就略過
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then (SPARK_CENTER_URL="http://127.0.0.1:$PORT" "$ROOT/app/spark-center-app" >/dev/null 2>&1 &); fi
