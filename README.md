# Spark Center

DGX Dashboard 的 Update 按鈕會一次升級全部套件（含 Chrome、ChatGPT 等第三方），而且寫死更新完必重開機。
這個小工具做的是同一件事的可控版本：

- 列出 apt 可升級套件，按來源分組，自己勾要裝哪些
- 安裝前用 python-apt 模擬，把「實際會動到的套件」（含相依性帶進來的）先列出來確認
- 安裝走 aptdaemon D-Bus（和 Dashboard 同一個後端），密碼由 polkit 桌面視窗處理
- 只讀 `/var/run/reboot-required` 決定要不要提示重開，程式本身絕不重開機
- 只綁 127.0.0.1:11001，不要改成對外

另外兩個分頁：

- **應用程式**：列出所有有桌面啟動項（.desktop）的 app，合併 apt、snap、flatpak 三種來源，顯示版本、來源與是否有新版
  （snap/flatpak 會向商店查，查不到就標「未能查詢」，不假裝是最新）
- **歷史**：/var/log/apt/history.log 最近 8 筆，表格＋動作標籤
- **監控**：DGX Dashboard 風格的即時儀表＋折線（SVG 手繪無外部庫）：系統記憶體、CPU 使用率（/proc/stat 差分，可切「每核」看 20 顆各自的使用率與時脈）、
  GPU 使用率、GPU 溫度（刻度上限為 tlimit 推算的降頻點）、GPU 功耗（nvidia-smi 無上限就不畫儀表）、磁碟、每個實體介面的上下行流量（/proc/net/dev 差分）。
  每 2 秒取樣、保留 150 點、只存在頁面內；離開分頁即停止輪詢。每核溫度這台沒有感測器，不顯示。
- **硬體**：類似 Windows 系統資訊／裝置管理員的靜態清單：系統、CPU、記憶體模組、GPU、儲存、網路、藍牙（bluetoothctl）、感測器、
  USB 樹（/sys/bus/usb 依 hub 層級掛樹，名稱缺的以 usb.ids 補並標示）、PCI。進入分頁抓一次，不輪詢。機型/BIOS（/sys DMI）、CPU（lscpu）、記憶體、GPU（nvidia-smi，含溫度/使用率/功耗每 5 秒更新）、
  儲存（lsblk＋statvfs 用量）、網路（ip -j）、hwmon 溫度、USB、PCI。主要來源不提權。
  序號、UUID、記憶體模組明細來自 dmidecode，需要 root；工具用 `sudo -n` 呼叫，沒放行就在畫面上標明不顯示。
  要放行只開這一個唯讀指令（dmidecode 不會改任何東西）：

  ```
  echo "$USER ALL=(root) NOPASSWD: /usr/sbin/dmidecode" | sudo tee /etc/sudoers.d/spark-center-dmidecode
  sudo chmod 440 /etc/sudoers.d/spark-center-dmidecode
  sudo visudo -c
  ```

有新版時可按「說明」看更新內容。各來源能給的不一樣，畫面上會標明：
apt 走 `apt-get changelog`（Ubuntu 官方套件有，第三方 repo 多半沒有）；
flatpak 本機沒有 appstream 時只能給遠端 commit 的提交訊息，不是 release notes；snap 商店不提供。

它取代不了 Dashboard 的 Spark OS 韌體 OTA（那段是 NVIDIA 閉源流程）。
定位：日常軟體更新用這頁；清單裡出現 dgx-release / dgx-spark-ota-update-meta / linux-image-nvidia 這類 Spark OS 本體更新時，再用 Dashboard。

## 安裝

```
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/spark-center.service" ~/.config/systemd/user/spark-center.service
systemctl --user daemon-reload
systemctl --user enable --now spark-center
```

開 http://localhost:11001

## 相依

Ubuntu 24.04 內建：python3-apt、python3-aptdaemon、python3-dbus、python3-gi。沒有 pip 套件。

## 檔案

- `server.py` 後端（stdlib http.server + python-apt + aptdaemon.client）
- `index.html` 前端（單檔，無外部資源）
- `spark-center.service` systemd user unit
