#!/bin/sh
# Spark Center 移除：拿掉 install.sh 放進家目錄的東西。不需要 root。
# 不刪 repo 目錄本身（含 data/ 的量測歷史與保留的舊版 .deb），要的話最後自己 rm -rf。
set -e
systemctl --user disable --now spark-center 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/spark-center.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -f "$HOME/.local/share/applications/spark-center.desktop" "$HOME/.config/autostart/spark-center.desktop"
for s in 256 128 64 48; do rm -f "$HOME/.local/share/icons/hicolor/${s}x${s}/apps/spark-center.png"; done
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/spark-center.svg"
gtk-update-icon-cache -q -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Spark Center user files removed. The repository directory (and its data/) is untouched."
echo
echo "Optional root-owned pieces, only if you installed them (each needs sudo):"
echo "  sudo rm -f /etc/sudoers.d/spark-center-dmidecode /etc/sudoers.d/spark-center-nvme"
echo "  sudo rm -rf /usr/local/libexec/spark-center /var/lib/spark-center"
echo "  sudo rm -f /usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy"
