# Spark Center

DGX Dashboard 的 Update 按鈕會一次升級全部套件（含 Chrome、ChatGPT 等第三方），而且寫死更新完必重開機。
這個小工具做的是同一件事的可控版本：

- 列出 apt 可升級套件，按來源分組，自己勾要裝哪些
- 安裝前用 python-apt 模擬，把「實際會動到的套件」（含相依性帶進來的）先列出來確認
- 安裝走 aptdaemon D-Bus（和 Dashboard 同一個後端），密碼由 polkit 桌面視窗處理
- 只讀 `/var/run/reboot-required` 決定要不要提示重開，程式本身絕不重開機
- 只綁 127.0.0.1:11001，不要改成對外

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
