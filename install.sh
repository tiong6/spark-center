#!/bin/sh
# Spark Center 安裝：生成 systemd user 單元與桌面捷徑（路徑依 repo 位置），啟用服務。不需要 root。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
sed "s|@ROOT@|$ROOT|g" "$ROOT/spark-center.service.in" > "$HOME/.config/systemd/user/spark-center.service"
sed "s|@ROOT@|$ROOT|g" "$ROOT/app/spark-center.desktop.in" > "$HOME/.local/share/applications/spark-center.desktop"
for s in 256 128 64 48; do mkdir -p "$HOME/.local/share/icons/hicolor/${s}x${s}/apps"; cp "$ROOT/app/spark-center-$s.png" "$HOME/.local/share/icons/hicolor/${s}x${s}/apps/spark-center.png"; done
cp "$ROOT/app/spark-center.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/spark-center.svg"
[ -f "$HOME/.local/share/icons/hicolor/index.theme" ] || cp /usr/share/icons/hicolor/index.theme "$HOME/.local/share/icons/hicolor/index.theme" 2>/dev/null || true
gtk-update-icon-cache -q -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now spark-center
echo "Spark Center: http://127.0.0.1:11001  （應用程式選單也有「Spark Center」）"
echo "選用：序號／記憶體模組與 NVMe SMART 需要 sudoers 放行 dmidecode 與 nvme smart-log，見 README。"
