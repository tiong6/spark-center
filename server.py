#!/usr/bin/env python3
"""Spark Center — 可勾選項目的 apt 更新頁，只綁 127.0.0.1。

資料來源與誠實原則：
- 清單：python-apt 讀本機 apt cache（和 `apt list --upgradable` 同一份）。
- 影響範圍：安裝前一律用 python-apt 在記憶體模擬，把「實際會動到的套件」列給使用者。
- 安裝：走 aptdaemon 的 D-Bus 介面（和 DGX Dashboard 同一個後端），授權由 polkit 桌面視窗處理。
- 重開機：只讀 /var/run/reboot-required，有才提示，程式本身絕不重開。
"""
import concurrent.futures
import glob
import hashlib
import http.client
import shutil
import shlex
import socket
import stat
from pathlib import Path
import gzip
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import apt
import apt_pkg
import aptdaemon.client
import aptdaemon.enums as aenums
from gi.repository import GLib

HOST = "127.0.0.1"
PORT = int(os.environ.get("SPARK_UPDATER_PORT", "11001"))
# 唯讀模式：所有會改東西的 POST 一律 403，畫面藏掉動作按鈕。給第一次裝的人先跑一天確認它只讀，再打開更新功能。
READONLY = os.environ.get("SPARK_CENTER_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
HERE = os.path.dirname(os.path.abspath(__file__))
REBOOT_FLAG = "/var/run/reboot-required"
REBOOT_PKGS = "/var/run/reboot-required.pkgs"
APT_HISTORY = "/var/log/apt/history.log"
APT_LISTS = "/var/lib/apt/lists"


LANG_DEFAULT = "zh-TW"
MSG = {
    "zh-TW": {
    "readonly_mode": "唯讀模式（SPARK_CENTER_READONLY=1）：這個服務不會改任何東西。要啟用更新等功能，拿掉這個環境變數後重啟服務。",
    "node_flow_locked": "nodejs 正由 Node 主版本升級流程處理，請到 npm 面板按「確認安裝」，或先「恢復原版本」。",
    "node_helper_unavailable": "Node 升級 helper 未安裝、版本不符或權限不安全；請依 README 的選用安裝步驟由管理員安裝。",
    "node_source_unsupported": "不適用：需要單一標準 NodeSource 來源、既有簽章金鑰與 /usr/bin/node；nvm、snap 或自訂來源請自行管理。",
    "node_command_failed": "Node 操作失敗，請查看指令輸出；不代表已恢復。",
    "node_backup_invalid": "無法保存或驗證原 Node 套件（需要可信 SHA-256，且不超過 150 MB）；停止操作。",
    "node_target_unavailable": "無法確認可信的目標 LTS／架構套件，停止操作。",
    "node_pending": "尚有 Node 升級紀錄，請先完成安裝或恢復原版本。",
    "node_no_pending": "目前沒有可執行此操作的 Node 升級紀錄，請重新整理。",
    "node_source_changed": "NodeSource 設定已被其他操作更改，停止覆寫；請重新檢查。",
    "node_bad_action": "無效的 Node 操作。",
    "node_root_required": "此 Node 操作需要桌面授權。",
    "node_plan_changed": "Node 狀態或模擬結果已改變，請重新檢查並確認。",
    "node_version_mismatch": "安裝後的 Node 版本與預期不同，請查看工作記錄。",

        "unknown_source": "未知來源",
        "missing_modules": "你勾了核心，但沒有勾對應的 NVIDIA 簽章模組。兩者沒有相依關係，模擬不會提醒你。只升核心的話，重開機後會進到一個沒有簽章 GPU 驅動的系統，退回 DKMS 自簽又會被 Secure Boot 擋掉。",
        "missing_kernel": "你勾了 NVIDIA 簽章模組，但沒有勾對應的核心。模組是對著特定核心版本編譯的，沒有那個核心就用不到。",
        "dashboard_note": "Dashboard 用 aptdaemon 的完整升級（可安裝／移除套件），裝完跑韌體並強制重開機，過程不顯示清單。",
        "running": "執行中",
        "command_missing": "找不到指令",
        "auth_cancelled": "授權被取消或密碼錯誤（pkexec）",
        "snap_busy": "snapd 已有進行中的變更，等它跑完再試",
        "see_log": "，見詳細記錄",
        "failed": "失敗",
        "done": "已完成",
        "snap_running": "該 snap 有程式正在執行，關掉它再更新",
        "snap_auth": "snap refresh 需要 root，透過 pkexec 取得授權（桌面會跳密碼視窗）",
        "download_interrupted": "下載中斷",
        "network_retry": "（看起來是網路中斷，再按一次即可重試）",
        "unknown_action": "未知動作",
        "new_version_no_number": "有新版（版本未提供）",
        "changelog_timeout": "下載 changelog 逾時（25 秒）",
        "no_changelog": "此來源未提供更新說明（多為第三方 repo）。",
        "changelog_current": "已安裝版本就是最新條目，沒有更新的 changelog",
        "appstream_no_releases": "appstream 裡沒有 release 記錄",
        "no_description": "（無說明）",
        "appstream_unavailable": "找不到這個 app 的 appstream 資料，也無法查詢遠端",
        "snap_no_channels": "snap info 沒有回報頻道資訊",
        "installed": "已安裝",
        "size": "大小",
        "version": "版本",
        "revision": "版次",
        "release_date": "發布日期",
        "channel": "頻道",
        "snap_changelog_note": "Snap 商店沒有逐版更新說明；以下是 snap info 的頻道與版本資訊，不是 changelog。",
        "gpu_idle": "GPU 閒置",
        "app_clocks": "應用程式時脈設定",
        "hw_slowdown": "硬體降速",
        "sw_power_cap": "軟體功率上限",
        "hw_power_brake": "硬體功率煞車",
        "hw_thermal": "硬體熱降速",
        "sw_thermal": "軟體熱降速",
        "display_clocks": "顯示時脈設定",
        "tlimit_estimate": "nvidia-smi tlimit 推算",
        "usb_hid": "人機介面",
        "usb_interface": "依介面",
        "usb_communication": "通訊",
        "usb_audio": "音訊",
        "usb_printer": "印表機",
        "usb_storage": "大量儲存",
        "usb_physical": "實體",
        "usb_image": "影像",
        "usb_cdc": "CDC 資料",
        "usb_security": "內容安全",
        "usb_card": "智慧卡",
        "usb_hub": "集線器",
        "usb_health": "個人健康",
        "usb_video": "視訊",
        "usb_av": "音訊/視訊",
        "usb_vendor": "廠商自訂",
        "usb_application": "應用特定",
        "usb_wireless": "無線",
        "usb_composite": "複合",
        "usb_diagnostic": "診斷",
        "usb_low": "USB 1.0 低速 1.5 Mb/s",
        "usb_full": "USB 1.1 全速 12 Mb/s",
        "usb_high": "USB 2.0 高速 480 Mb/s",
        "device": "裝置",
        "usb_root": "USB 根集線器",
        "hid_device": "人機介面裝置",
        "mouse": "滑鼠",
        "keyboard": "鍵盤",
        "bluetooth": "藍牙",
        "camera": "視訊/攝影機",
        "usb_network": "通訊/網路",
        "rear_layout": "ServeTheHome 評測描述的後面板順序；USB-C 編號對應實體位置為推測，可用插入校準",
        "phison": "Phison PS5027-E27T 控制器（系統碟）",
        "network_10g": "10GbE 網路",
        "realtek": "Realtek RTL8127 10 Gb 乙太網路",
        "mediatek": "MediaTek MT7925（AzureWave 模組）",
        "wifi_bt": "Wi-Fi 7 / 藍牙",
        "gb10_gpu": "NVIDIA GB10 Blackwell 顯示核心",
        "storage_controller": "儲存控制器",
        "crypto": "加密",
        "multimedia": "多媒體",
        "serial_bus": "序列匯流排",
        "bridge": "橋接",
        "network": "網路",
        "accelerator": "處理加速",
        "display": "顯示",
        "dmi_root": "dmidecode 需要 root。要顯示序號與記憶體模組，請在 sudoers 只放行 /usr/sbin/dmidecode 免密碼（見 README）",
        "bluetooth_unavailable": "bluetoothctl 無法連到 bluetoothd（服務未啟動或無藍牙硬體）",
        "nand_unavailable": "NAND 顆粒廠商從軟體查不到",
        "nvme_vendor_source": "PCI 廠商 ID 與 EUI-64 OUI（硬體登記值）",
        "smart_root": "SMART 需要 root：請在 sudoers 放行 nvme smart-log（見 README），這裡只顯示免 root 的欄位",
        "nvme_not_json": "nvme 輸出不是 JSON",
        "spare_low": "備用空間低於門檻",
        "media_degraded": "媒體可靠度下降",
        "read_only": "已轉唯讀",
        "volatile_failed": "揮發性備援記憶體失效",
        "temp_threshold": "溫度超出門檻",
        "nvme_source": "nvme smart-log（sudoers 僅放行此指令）",
        "wifi_scan_failed": "nmcli 掃描失敗",
        "wifi_24_note": "2.4 GHz 只有 1/6/11 互不重疊；相鄰頻道也會互相干擾，分數已把 ±4 格的鄰居加權算進去。",
        "wifi_5_note": "5 GHz 的 20 MHz 頻道互不重疊，只算同頻道；但若路由器用 40/80 MHz，實際會跨到相鄰頻道。",
        "wifi_snapshot": "掃描是一瞬間的快照，鄰居用量會隨時段變動，建議不同時間各掃一次再決定。",
        "wifi_signal": "訊號是 NetworkManager 給的百分比，不是 dBm，只適合互相比較。",
        "wifi_analysis_only": "本工具只做分析，改頻道要自己進路由器管理頁。",
        "ollama_metrics": "Ollama 不提供 Prometheus 指標，tok/s 只能靠實際跑一段生成量測（下方按鈕）。",
        "lmstudio_metrics": "LM Studio 的 /v1/models 只列可用模型，不給效能指標。",
        "llamacpp_source": "來源 /props 與 /slots。",
        "vllm_metrics": "vLLM 的 Prometheus /metrics 尚未解析，這裡只列模型。",
        "ollama_config": "改設定要 root：sudo systemctl edit ollama，在 [Service] 加 Environment=OLLAMA_NUM_PARALLEL=4 後 restart。本工具不提權，只顯示。",
        "webui_volume": "Open WebUI 卷",
        "duplicate_note": "重複判定用名稱正規化（去掉 GGUF、instruct 等字尾）比對，是啟發式；同名不代表同一量化版本，刪之前自己確認。",
        "warming": "載入／暖機",
        "warm_failed": "暖機失敗",
        "rollback_preparing": "保留舊版以便降回：{name} {old}",
        "rollback_kept": "已保留 {name} {old}（{source}）",
        "rollback_not_kept": "未保留 {name} {old}：{reason}",
        "rollback_src_cache": "apt 快取",
        "rollback_src_repo": "來源伺服器",
        "rollback_src_launchpad": "Launchpad",
        "rollback_no_source": "來源不保留舊版（ESM 需授權，或第三方倉庫已移除）",
        "rollback_too_big": "{mb} MB 超過保留上限 {max} MB",
        "rollback_fetch_failed": "下載失敗：{err}",
        "rollback_bad_deb": "下載到的檔案不是預期的套件版本",
        "rollback_not_installed": "套件未安裝，沒有舊版可留",
        "rollback_not_found": "找不到該筆保留檔",
        "rollback_hash_mismatch": "下載到的檔案與 apt 索引的 sha256 不符，不採用",
        "rollback_no_trusted_hash": "來源是 HTTP 而索引已無此版的雜湊可核對，不採用",
        "rollback_auth": "降回上一版需要 root，透過 pkexec 執行 apt-get（桌面會跳密碼視窗）",
        "rollback_sim_failed": "無法模擬降回：{err}",
        "npm_missing": "找不到 npm，沒有全域套件可查",
        "npm_restart_bad_unit": "{unit} 不是目前正在執行 npm 全域套件的 systemd 使用者服務，不重啟",
        "npm_restart_failed": "重啟 {unit} 失敗：{err}",
        "node_release_failed": "查不到 Node 官方版本表：{err}",
        "npm_target_unknown": "查不到 {pkgs} 的目標版本，不更新（不會退回 @latest 亂裝）。{err}",
        "npm_engine_blocked": "新版 {ver} 要求 Node {need}，你的是 {node}，不給更新",
        "npm_engine_unknown": "查不到新版的 Node 需求，先不給更新",
        "npm_query_failed": "npm ls -g 失敗：{err}",
        "npm_outdated_failed": "查不到新版（npm outdated 失敗，可能是連不上 registry）：{err}",
        "rollback_left_broken": "。dpkg 回報有套件未完成設定：請在終端機跑 sudo dpkg --configure -a 修復後，再重新整理更新清單",
        "rollback_state_intact": "。dpkg 回報套件狀態完整，沒有留下未完成設定的套件",
        "rollback_would_remove": "降回會連帶移除 {pkgs}（它們需要新版）。這裡不替你拆掉別的軟體，所以不做",
        "bench_all_failed": "{n} 個併發請求全部失敗，沒有可記的成績",
        "bench_partial_failed": "{failed}/{n} 個請求失敗，數字只算成功的那幾個，不是完整成績",
        "requests_queued": "小於併發數，後面的請求在排隊。",
        "requests_parallel": "足夠同時處理。",
        "warm_request_failed": "暖機請求失敗（模型載入失敗或逾時）",
        "bench_failed": "量測請求失敗",
        "ollama_models": "Ollama 模型",
        "ollama_disk_note": "系統服務，存在 /usr/share/ollama；同一模型的不同 tag 共用 blob，清單加總會大於實際占用。可在此刪除個別模型",
        "lmstudio_models": "LM Studio 模型",
        "lmstudio_disk_note": "~/.lmstudio/models；請在 LM Studio 內刪除，這裡只列出",
        "snap_data_note": "Steam 遊戲、瀏覽器設定檔等；請在各應用內管理",
        "snap_data": "snap 應用資料（~/snap）",
        "flatpak_note": "app＋runtime；未使用的 runtime 可用 flatpak uninstall --unused",
        "flatpak_system": "flatpak（系統）",
        "snap_retention": "snapd 預設保留舊版本 2 份",
        "snap_system": "snap（系統）",
        "apt_cache": "apt 套件快取",
        "apt_cache_note": "下載過的 .deb，清掉無害",
        "apt_autoremove": "apt 可自動移除的套件",
        "none": "沒有",
        "journal": "系統日誌 journal",
        "journal_note": "需 root 才能 vacuum，這裡不動",
        "app_cache": "各應用快取",
        "npm_cache": "npm 快取",
        "trash": "垃圾桶",
        "gpu_stuck_advice": "論壇多人確認的根因是電源供應器內 USB-C PD 控制器韌體卡住。解法：拔掉電源供應器與所有 USB-C 裝置，按住電源鍵 30 秒，再等 60 秒讓電容放電，然後接回開機。只重開機沒用，PD 控制器在變壓器裡，要斷電才會重置。",
        "gpu_stuck_title": "GPU 卡在低功耗狀態",
        "disk_full_title": "根分割區快滿了",
        "transaction_failed": "交易失敗",
        "fw_pending": "待處理（等重開機）",
        "fw_success": "成功",
        "fw_unknown": "未知",
        "fw_failed_reboot": "重開後失敗",
        "fw_reboot": "需要重開機",
        "fwupd_missing": "沒有 fwupdmgr",
        "fwupd_query_failed": "fwupdmgr 查詢失敗（{what}），無法判斷有沒有韌體更新；這不是「已是最新」",
        "bad_origin": "拒絕：請求不是來自本機的 Spark Center 頁面（{why}）",
        "missing_source_id": "缺 source 或 id",
        "no_pkgs": "沒有選取任何套件",
        "job_running": "已有工作在進行中",
        "slot_invalid": "slot 需為 0–3",
        "controller_invalid": "controller 格式不對",
        "missing_model": "缺 model",
        "ollama_no_response": "Ollama 沒回應（模型不存在或載入失敗）",
        "model_invalid": "模型名稱格式不對",
        "concurrency_invalid": "缺 model 或 concurrency 需為 1/2/4/8",
        "num_predict_invalid": "num_predict 需為 64/128/256/512",
        "bench_running": "已有量測在進行中",
        "missing_name": "缺 name",
        "no_removable": "沒有可移除的套件",
        "source_ids_invalid": "source 需為 flatpak/snap 且 ids 非空",
        "pkgs_missing": "找不到套件：{p0}",
        "deps_failed": "相依性無法解析：{p0}",
        "exit_code": "指令結束碼 {p0}",
        "snap_attach": "snapd 已有進行中的變更 {p0}，直接接上監看（不需要再輸入密碼）",
        "snap_step": "步驟 {p0}/{p1}",
        "snap_step_percent": " · 這步 {p0}%",
        "snap_overall": "（整體 {p0}%）",
        "snap_ended": "snapd 變更 {p0} 結束於 {p1}",
        "snap_status": "snapd 變更 {p0}: {p1}",
        "config_conflict": "設定檔衝突 {p0}，保留現有版本",
        "transaction_exit": "交易結束狀態：{p0}",
        "job_start": "開始 {p0}: {p1}",
        "new_commit": "新 commit {p0}",
        "vendor_notes": "改看廠商的發行說明：{p0}",
        "changelog_home": "這個來源沒有提供 changelog；套件宣告的官網是 {p0}",
        "changelog_no_home": "這個來源（{p0}）沒有提供 changelog，套件也沒宣告官網",
        "apt_get_failed": "無法執行 apt-get：{p0}",
        "appstream_source": "來源：{p0} appstream",
        "remote_no_notes": "{p0} 未提供 release notes（本機無 appstream 資料）；以下是遠端最新版的提交資訊，不是更新說明",
        "snap_info_failed": "snap info {p0} 失敗（離線、商店不可達或名稱不符）",
        "publisher": "發行者 {p0}",
        "tracking": "追蹤 {p0}",
        "last_update": "上次更新 {p0}",
        "usb_root_bus": "USB {p0} 根集線器 · Bus {p1}",
        "unknown_device": "未知裝置 {p0}:{p1}",
        "dmidecode_exec_failed": "dmidecode 執行失敗：{p0}",
        "dmidecode_failed": "dmidecode 失敗：{p0}",
        "nvme_exec_failed": "nvme 執行失敗：{p0}",
        "nvme_smart_failed": "nvme smart-log 失敗：{p0}",
        "bench_concurrent_phase": "{p0} 個請求同時生成 {p1} token",
        "bench_concurrent_note": "{p0} 併發、每請求 {p1} token；總 tok/s＝合計 token ÷ 牆鐘時間。OLLAMA_NUM_PARALLEL={p2}，",
        "bench_phase": "生成 {p0} token 中",
        "bench_note": "單一請求、temperature 0、{p0} token；prefill 若 prompt 被快取會偏高。",
        "docker_count": "{p0}：{p1} 個，可回收 {p2}",
        "volume": "卷 {p0}",
        "volume_links": "被 {p0} 個容器用",
        "docker_reclaim": "映像可回收約 {p0} GB；卷不自動清（可能是資料）",
        "autoremove_count": "{p0} 個不再需要的相依套件（含舊核心）",
        "gpu_stuck_load": "GPU 有負載（{p0}%）但 SM 時脈釘在 {p1} MHz、功耗 {p2} W，持續 30 秒以上。",
        "gpu_stuck_flags": "GPU 硬體降速旗標持續亮著：{p0}。",
        "disk_full": "已用 {p0}%，剩 {p1} GB。開 http://localhost:{p2}/#disk 看誰在吃空間。",
        "autostart_write_failed": "寫入 autostart 失敗：{p0}",
        "ollama_http": "Ollama 回 {p0}：{p1}",
        "ollama_connect": "連不到 Ollama：{p0}",
        "snap_apps_running": "snapd 不會更新正在執行的 snap：{p0}。請先關閉這些程式再試。",
        "truncated": "\n…（已截斷）",
        "changelog_source": "來源：apt changelog",
        "changelog_all": "，全部條目（找不到已安裝版本的分界）",
        "changelog_since": "，已安裝版本之後的條目",
        "gpu_help": " 開 http://localhost:%d/#monitor 看處理方式。",
        "trash_cleared": "已清空垃圾桶"
    },
    "en": {
    "readonly_mode": "Read-only mode (SPARK_CENTER_READONLY=1): this service changes nothing. To enable updates and other actions, remove that environment variable and restart the service.",
    "node_flow_locked": "nodejs is being handled by the Node major-upgrade flow; use 'Confirm install' on the npm panel, or restore the original version first.",
    "node_helper_unavailable": "Node upgrade helper is missing, outdated or has unsafe permissions. Ask an administrator to follow the optional helper installation steps in README.",
    "node_source_unsupported": "Not applicable: requires one standard NodeSource repository, its existing signing key and /usr/bin/node. Manage nvm, snap or custom repositories separately.",
    "node_command_failed": "Node operation failed; inspect command output. Restoration is not implied.",
    "node_backup_invalid": "Could not save or verify the original Node package (trusted SHA-256 and at most 150 MB required). Operation stopped.",
    "node_target_unavailable": "A trusted target LTS/architecture package could not be confirmed; operation stopped.",
    "node_pending": "A Node upgrade is already pending; finish installation or restore the original version first.",
    "node_no_pending": "No Node upgrade record allows this action; refresh the page.",
    "node_source_changed": "NodeSource configuration was changed by another operation; refusing to overwrite it. Recheck the configuration.",
    "node_bad_action": "Invalid Node operation.",
    "node_root_required": "This Node operation requires desktop authorization.",
    "node_plan_changed": "Node state or the simulation changed; review and confirm again.",
    "node_version_mismatch": "The installed Node version differs from the expected version; inspect the job log.",

        "unknown_source": "Unknown source",
        "missing_modules": "You selected the kernel without the matching NVIDIA signed modules. They have no dependency relationship, so simulation will not warn you. Upgrading only the kernel leaves the system without a signed GPU driver after reboot; falling back to self-signed DKMS modules will also be blocked by Secure Boot.",
        "missing_kernel": "You selected the NVIDIA signed modules without the matching kernel. These modules are built for a specific kernel version and cannot be used without it.",
        "dashboard_note": "Dashboard uses aptdaemon's full upgrade (which can install or remove packages), then updates firmware and forces a reboot, without showing the package list.",
        "running": "Running",
        "command_missing": "Command not found",
        "auth_cancelled": "Authorization was cancelled or the password was incorrect (pkexec)",
        "snap_busy": "snapd already has a change in progress; wait for it to finish and try again",
        "see_log": "; see the detailed log",
        "failed": "Failed",
        "done": "Completed",
        "snap_running": "This snap has running apps; close them before updating",
        "snap_auth": "snap refresh requires root; requesting authorization through pkexec (a password dialog will appear on the desktop)",
        "download_interrupted": "Download interrupted",
        "network_retry": " (the network connection appears to have been interrupted; click again to retry)",
        "unknown_action": "Unknown action",
        "new_version_no_number": "Update available (version not provided)",
        "changelog_timeout": "Changelog download timed out (25 seconds)",
        "no_changelog": "This source does not provide release notes (usually a third-party repository).",
        "changelog_current": "The installed version is the latest entry; there are no newer changelog entries",
        "appstream_no_releases": "No release records in appstream",
        "no_description": "(no description)",
        "appstream_unavailable": "Could not find appstream data for this app or query the remote",
        "snap_no_channels": "snap info returned no channel information",
        "installed": "Installed",
        "size": "Size",
        "version": "Version",
        "revision": "Revision",
        "release_date": "Release date",
        "channel": "Channel",
        "snap_changelog_note": "The Snap Store does not provide release notes for each version; the following is channel and version information from snap info, not a changelog.",
        "gpu_idle": "GPU idle",
        "app_clocks": "Application clock setting",
        "hw_slowdown": "Hardware slowdown",
        "sw_power_cap": "Software power cap",
        "hw_power_brake": "Hardware power brake",
        "hw_thermal": "Hardware thermal slowdown",
        "sw_thermal": "Software thermal slowdown",
        "display_clocks": "Display clock setting",
        "tlimit_estimate": "nvidia-smi tlimit (estimated)",
        "usb_hid": "Human interface",
        "usb_interface": "Per interface",
        "usb_communication": "Communications",
        "usb_audio": "Audio",
        "usb_printer": "Printer",
        "usb_storage": "Mass storage",
        "usb_physical": "Physical",
        "usb_image": "Imaging",
        "usb_cdc": "CDC data",
        "usb_security": "Content security",
        "usb_card": "Smart card",
        "usb_hub": "Hub",
        "usb_health": "Personal healthcare",
        "usb_video": "Video",
        "usb_av": "Audio/video",
        "usb_vendor": "Vendor-specific",
        "usb_application": "Application-specific",
        "usb_wireless": "Wireless",
        "usb_composite": "Composite",
        "usb_diagnostic": "Diagnostic",
        "usb_low": "USB 1.0 low speed 1.5 Mb/s",
        "usb_full": "USB 1.1 full speed 12 Mb/s",
        "usb_high": "USB 2.0 high speed 480 Mb/s",
        "device": "Device",
        "usb_root": "USB root hub",
        "hid_device": "Human interface device",
        "mouse": "Mouse",
        "keyboard": "Keyboard",
        "bluetooth": "Bluetooth",
        "camera": "Video/camera",
        "usb_network": "Communications/network",
        "rear_layout": "Rear-panel order described in the ServeTheHome review; USB-C numbers mapped to physical positions (estimated), which can be calibrated by plugging in a device",
        "phison": "Phison PS5027-E27T controller (system drive)",
        "network_10g": "10GbE network",
        "realtek": "Realtek RTL8127 10 Gb Ethernet",
        "mediatek": "MediaTek MT7925 (AzureWave module)",
        "wifi_bt": "Wi-Fi 7 / Bluetooth",
        "gb10_gpu": "NVIDIA GB10 Blackwell GPU",
        "storage_controller": "Storage controller",
        "crypto": "Encryption",
        "multimedia": "Multimedia",
        "serial_bus": "Serial bus",
        "bridge": "Bridge",
        "network": "Network",
        "accelerator": "Processing accelerator",
        "display": "Display",
        "dmi_root": "dmidecode requires root. To show serial numbers and memory modules, allow only /usr/sbin/dmidecode without a password in sudoers (see README)",
        "bluetooth_unavailable": "bluetoothctl could not connect to bluetoothd (the service is not running or there is no Bluetooth hardware)",
        "nand_unavailable": "The NAND chip manufacturer is unavailable through software",
        "nvme_vendor_source": "PCI vendor ID and EUI-64 OUI (hardware registration values)",
        "smart_root": "SMART requires root: allow nvme smart-log in sudoers (see README); only fields available without root are shown here",
        "nvme_not_json": "nvme output is not JSON",
        "spare_low": "Available spare is below the threshold",
        "media_degraded": "Media reliability degraded",
        "read_only": "Switched to read-only mode",
        "volatile_failed": "Volatile memory backup failed",
        "temp_threshold": "Temperature exceeds the threshold",
        "nvme_source": "nvme smart-log (only this command is allowed in sudoers)",
        "wifi_scan_failed": "nmcli scan failed",
        "wifi_24_note": "On 2.4 GHz, only channels 1/6/11 do not overlap. Adjacent channels also interfere; the score includes weighted neighbors within ±4 channels.",
        "wifi_5_note": "On 5 GHz, 20 MHz channels do not overlap, so only the same channel is counted; a router using 40/80 MHz will actually span adjacent channels.",
        "wifi_snapshot": "A scan is a snapshot at one instant. Neighboring network usage varies by time of day; scan at different times before deciding.",
        "wifi_signal": "Signal is the percentage reported by NetworkManager, not dBm; it is only suitable for relative comparisons.",
        "wifi_analysis_only": "This tool only analyzes channels; change the channel yourself in your router's management page.",
        "ollama_metrics": "Ollama does not provide Prometheus metrics; tok/s can only be measured by running actual generation (using the button below).",
        "lmstudio_metrics": "LM Studio's /v1/models only lists available models; it does not provide performance metrics.",
        "llamacpp_source": "Based on /props and /slots.",
        "vllm_metrics": "vLLM's Prometheus /metrics is not parsed yet; only models are listed here.",
        "ollama_config": "Changing settings requires root: run sudo systemctl edit ollama, add Environment=OLLAMA_NUM_PARALLEL=4 under [Service], then restart. This tool only displays settings and does not elevate privileges.",
        "webui_volume": "Open WebUI volume",
        "duplicate_note": "Duplicate detection compares normalized names (removing suffixes such as GGUF and instruct) and is heuristic; matching names do not imply the same quantization. Verify before deleting.",
        "warming": "Loading / warming up",
        "warm_failed": "Warm-up failed",
        "rollback_preparing": "Keeping the previous version for rollback: {name} {old}",
        "rollback_kept": "Kept {name} {old} ({source})",
        "rollback_not_kept": "Not kept {name} {old}: {reason}",
        "rollback_src_cache": "apt cache",
        "rollback_src_repo": "source repository",
        "rollback_src_launchpad": "Launchpad",
        "rollback_no_source": "the source does not keep old versions (ESM needs authentication, or the third-party repository removed it)",
        "rollback_too_big": "{mb} MB exceeds the {max} MB limit",
        "rollback_fetch_failed": "download failed: {err}",
        "rollback_bad_deb": "the downloaded file is not the expected package version",
        "rollback_not_installed": "package is not installed; nothing to keep",
        "rollback_not_found": "that kept file was not found",
        "rollback_hash_mismatch": "the downloaded file does not match the sha256 in the apt index; not used",
        "rollback_no_trusted_hash": "the source is plain HTTP and the index no longer has a hash for this version to check against; not used",
        "rollback_auth": "Rolling back requires root; running apt-get through pkexec (a password dialog will appear on the desktop)",
        "rollback_sim_failed": "Could not simulate the rollback: {err}",
        "npm_missing": "npm not found; no global packages to check",
        "npm_restart_bad_unit": "{unit} is not a systemd user service currently running an npm global package; not restarting",
        "npm_restart_failed": "Restarting {unit} failed: {err}",
        "node_release_failed": "Could not read the official Node release table: {err}",
        "npm_target_unknown": "Could not determine the target version for {pkgs}; not updating (no silent fallback to @latest). {err}",
        "npm_engine_blocked": "version {ver} requires Node {need}; yours is {node}, so no update is offered",
        "npm_engine_unknown": "the new version's Node requirement could not be read; no update offered for now",
        "npm_query_failed": "npm ls -g failed: {err}",
        "npm_outdated_failed": "Could not check for new versions (npm outdated failed, possibly no access to the registry): {err}",
        "rollback_left_broken": ". dpkg reports packages left unconfigured: run sudo dpkg --configure -a in a terminal to repair, then refresh the update list",
        "rollback_state_intact": ". dpkg reports the package state is intact; nothing was left unconfigured",
        "rollback_would_remove": "Rolling back would also remove {pkgs} (they need the newer version). This tool will not remove other software for you, so it stops here",
        "bench_all_failed": "All {n} concurrent requests failed; nothing to record",
        "bench_partial_failed": "{failed} of {n} requests failed; the numbers cover only the successful ones and are not a complete result",
        "requests_queued": "below the concurrency; later requests are queued.",
        "requests_parallel": "sufficient for simultaneous processing.",
        "warm_request_failed": "Warm-up request failed (model loading failed or timed out)",
        "bench_failed": "Benchmark request failed",
        "ollama_models": "Ollama models",
        "ollama_disk_note": "System service, stored in /usr/share/ollama; different tags of the same model share blobs, so the sum of the listed sizes exceeds actual disk usage. Individual models can be deleted here",
        "lmstudio_models": "LM Studio models",
        "lmstudio_disk_note": "~/.lmstudio/models; delete models in LM Studio. This tool only lists them",
        "snap_data_note": "Steam games, browser profiles, etc.; manage them in each app",
        "snap_data": "snap app data (~/snap)",
        "flatpak_note": "Apps and runtimes; unused runtimes can be removed with flatpak uninstall --unused",
        "flatpak_system": "flatpak (system)",
        "snap_retention": "snapd retains 2 old revisions by default",
        "snap_system": "snap (system)",
        "apt_cache": "apt package cache",
        "apt_cache_note": "Downloaded .deb files; safe to clear",
        "apt_autoremove": "apt packages available for automatic removal",
        "none": "None",
        "journal": "System journal",
        "journal_note": "Vacuuming requires root; this tool does not change it",
        "app_cache": "App caches",
        "npm_cache": "npm cache",
        "trash": "Trash",
        "gpu_stuck_advice": "Multiple forum users have confirmed that the root cause is stuck firmware in the power supply's USB-C PD controller. Disconnect the power supply and all USB-C devices, hold the power button for 30 seconds, then wait another 60 seconds for the capacitors to discharge before reconnecting and powering on. Rebooting alone does not help: the PD controller is inside the power adapter and requires a power disconnect to reset.",
        "gpu_stuck_title": "GPU stuck in a low-power state",
        "disk_full_title": "Root partition is nearly full",
        "transaction_failed": "Transaction failed",
        "fw_pending": "Pending (waiting for reboot)",
        "fw_success": "Success",
        "fw_unknown": "Unknown",
        "fw_failed_reboot": "Failed after reboot",
        "fw_reboot": "Reboot required",
        "fwupd_missing": "fwupdmgr is unavailable",
        "fwupd_query_failed": "fwupdmgr query failed ({what}); whether firmware updates exist is unknown. This is not \"up to date\"",
        "bad_origin": "Rejected: the request did not come from the local Spark Center page ({why})",
        "missing_source_id": "Missing source or id",
        "no_pkgs": "No packages selected",
        "job_running": "A job is already running",
        "slot_invalid": "slot must be 0–3",
        "controller_invalid": "Invalid controller format",
        "missing_model": "Missing model",
        "ollama_no_response": "Ollama did not respond (model does not exist or loading failed)",
        "model_invalid": "Invalid model name format",
        "concurrency_invalid": "Missing model or concurrency must be 1/2/4/8",
        "num_predict_invalid": "num_predict must be 64/128/256/512",
        "bench_running": "A benchmark is already running",
        "missing_name": "Missing name",
        "no_removable": "No packages available to remove",
        "source_ids_invalid": "source must be flatpak/snap and ids must not be empty",
        "pkgs_missing": "Packages not found: {p0}",
        "deps_failed": "Could not resolve dependencies: {p0}",
        "exit_code": "Command exit code {p0}",
        "snap_attach": "snapd change {p0} is already running; monitoring it directly (no password required again)",
        "snap_step": "Step {p0}/{p1}",
        "snap_step_percent": " · this step {p0}%",
        "snap_overall": " (overall {p0}%)",
        "snap_ended": "snapd change {p0} ended with {p1}",
        "snap_status": "snapd change {p0}: {p1}",
        "config_conflict": "Configuration file conflict for {p0}; keeping the current version",
        "transaction_exit": "Transaction exit status: {p0}",
        "job_start": "Starting {p0}: {p1}",
        "new_commit": "New commit {p0}",
        "vendor_notes": "See the vendor's release notes instead: {p0}",
        "changelog_home": "This source does not provide a changelog; the package's declared website is {p0}",
        "changelog_no_home": "This source ({p0}) does not provide a changelog, and the package does not declare a website",
        "apt_get_failed": "Could not run apt-get: {p0}",
        "appstream_source": "Source: {p0} appstream",
        "remote_no_notes": "{p0} does not provide release notes (no local appstream data); the following is commit information for the latest remote version, not release notes",
        "snap_info_failed": "snap info {p0} failed (offline, store unreachable, or name mismatch)",
        "publisher": "Publisher {p0}",
        "tracking": "Tracking {p0}",
        "last_update": "Last updated {p0}",
        "usb_root_bus": "USB {p0} root hub · Bus {p1}",
        "unknown_device": "Unknown device {p0}:{p1}",
        "dmidecode_exec_failed": "Could not run dmidecode: {p0}",
        "dmidecode_failed": "dmidecode failed: {p0}",
        "nvme_exec_failed": "Could not run nvme: {p0}",
        "nvme_smart_failed": "nvme smart-log failed: {p0}",
        "bench_concurrent_phase": "{p0} requests generating {p1} tokens concurrently",
        "bench_concurrent_note": "Concurrency {p0}, {p1} tokens per request; total tok/s = total tokens ÷ wall-clock time. OLLAMA_NUM_PARALLEL={p2}, ",
        "bench_phase": "Generating {p0} tokens",
        "bench_note": "Single request, temperature 0, {p0} tokens; prefill may be higher if the prompt is cached.",
        "docker_count": "{p0}: {p1} items, {p2} reclaimable",
        "volume": "Volume {p0}",
        "volume_links": "Used by {p0} containers",
        "docker_reclaim": "About {p0} GB reclaimable from images; volumes are not cleared automatically (they may contain data)",
        "autoremove_count": "{p0} dependencies no longer needed (including old kernels)",
        "gpu_stuck_load": "GPU is under load ({p0}%) but its SM clock is stuck at {p1} MHz, with power at {p2} W, for over 30 seconds.",
        "gpu_stuck_flags": "GPU hardware slowdown flags remain active: {p0}.",
        "disk_full": "{p0}% used, {p1} GB remaining. Open http://localhost:{p2}/#disk to see disk usage.",
        "autostart_write_failed": "Could not write autostart: {p0}",
        "ollama_http": "Ollama returned {p0}: {p1}",
        "ollama_connect": "Could not connect to Ollama: {p0}",
        "snap_apps_running": "snapd will not update running snaps: {p0}. Close these apps and try again.",
        "truncated": "\n… (truncated)",
        "changelog_source": "Source: apt changelog",
        "changelog_all": ", all entries (could not identify the installed-version boundary)",
        "changelog_since": ", entries after the installed version",
        "gpu_help": " Open http://localhost:%d/#monitor for instructions.",
        "trash_cleared": "Trash emptied"
    }
}


def msg(key, lang, **kw):
    return MSG.get(lang, MSG[LANG_DEFAULT]).get(key, MSG[LANG_DEFAULT].get(key, key)).format(**kw)


# 快取、背景工作及既有快照保留中文原值，只在 HTTP 回應副本翻譯，避免兩個語言互相污染。
# 規則只涵蓋本工具字串表的完整句型；不改套件、路徑、外部指令輸出或使用者校準備註。
_MSG_EXACT = {value: key for key, value in MSG[LANG_DEFAULT].items() if "{p" not in value}
_MSG_PATTERNS = []
for _key, _template in MSG[LANG_DEFAULT].items():
    if "{p" in _template:
        _parts = re.split(r"(\{p\d+\})", _template)
        _pattern = "".join("(?P<" + part[1:-1] + ">.*?)" if re.fullmatch(r"\{p\d+\}", part) else re.escape(part) for part in _parts)
        _MSG_PATTERNS.append((_key, re.compile(_pattern, re.S)))


def _message_text(value, lang):
    if lang == LANG_DEFAULT or not re.search("[一-鿿]", value):
        return value
    if value in _MSG_EXACT:
        return msg(_MSG_EXACT[value], lang)
    for key, pattern in _MSG_PATTERNS:
        found = pattern.fullmatch(value)
        if found:
            return msg(key, lang, **{k: _message_text(v, lang) for k, v in found.groupdict().items()})
    # 工作記錄的時間戳與組合說明，逐個翻譯已知片段，保留其餘原始資料。
    stamp = re.match(r"^(\d{2}:\d{2}:\d{2} )(.*)$", value, re.S)
    if stamp:
        return stamp[1] + _message_text(stamp[2], lang)
    for source, key in sorted(_MSG_EXACT.items(), key=lambda item: len(item[0]), reverse=True):
        if len(source) > 5 and value.startswith(source):
            return msg(key, lang) + _message_text(value[len(source):], lang)
        if len(source) > 5 and value.endswith(source):
            return _message_text(value[:-len(source)], lang) + msg(key, lang)
    for key, pattern in _MSG_PATTERNS:
        # 有固定句尾才可在組合文字中找邊界，避免吞掉後面的外部資料。
        if not re.search(r"\{p\d+\}$", MSG[LANG_DEFAULT][key]):
            value = pattern.sub(lambda found: msg(key, lang, **{
                k: _message_text(v, lang) for k, v in found.groupdict().items()}), value)
    for separator in (" · ", "、"):
        if separator in value:
            parts = value.split(separator)
            translated = [_message_text(part, lang) for part in parts]
            if translated != parts:
                return (", " if separator == "、" else separator).join(translated)
    return value


def _localized_response(value, lang, field=None):
    if lang == LANG_DEFAULT or field in {
        "calib", "path", "mountpoint", "mountpoints", "ssid", "bssid", "commandline",
        "command", "packages", "model", "id", "serial", "serial_number",
    }:
        return value
    if isinstance(value, dict):
        result = {k: _localized_response(v, lang, k) for k, v in value.items()}
        if "name_zh" in result:
            result["name_zh"] = ""  # 英文介面使用 .desktop 的原始 Name，不翻譯產品名稱。
        return result
    if isinstance(value, list):
        return [_localized_response(item, lang, field) for item in value]
    if isinstance(value, str):
        text = _message_text(value, lang)
        if field in ("group", "origin"):
            text = text.replace("（", " (").replace("）", ")")   # 來源名稱的全形括號在英文介面改半形
        return text
    return value


# 名稱樣式 → 「通常需要重開」的提示。這是啟發式，前端會標明「推測」。
REBOOT_HINT_RE = re.compile(
    r"^(linux-image|linux-modules|linux-headers|nvidia-driver|nvidia-kernel|"
    r"libnvidia-|nvidia-utils|libc6|dgx-|systemd$|dbus$|udev$)"
)


# ---------- apt 清單與模擬 ----------

def _origin_of(version):
    if not version or not version.origins:
        return {"origin": "", "site": "", "archive": "", "label": ""}
    o = version.origins[0]
    return {
        "origin": o.origin or "",
        "site": o.site or "",
        "archive": o.archive or "",
        "label": o.label or "",
    }


# 來源主機 → 人看得懂的名稱。廠商在 Release 檔的 Origin 欄位常填得看不懂（VS Code 填 "code stable"、
# NodeSource 填 ". nodistro"、OpenAI 乾脆不填），這張表只做「翻譯」，原始 Origin 仍隨 site 一起顯示。
SITE_NAMES = {
    "packages.microsoft.com": "Microsoft（VS Code）",
    "persistent.oaistatic.com": "OpenAI（ChatGPT／Codex）",
    "deb.nodesource.com": "NodeSource（Node.js）",
    "dl.google.com": "Google（Chrome）",
    "us-central1-apt.pkg.dev": "Google（Antigravity）",
    "brave-browser-apt-release.s3.brave.com": "Brave",
    "repository.spotify.com": "Spotify",
    "pkgs.tailscale.com": "Tailscale",
    "packagecloud.io": "GitHub（git-lfs）",
    "esm.ubuntu.com": "Ubuntu Pro（ESM）",
    "snapshot.ppa.launchpadcontent.net": "Canonical PPA",
    "nvidia.github.io": "NVIDIA（container toolkit）",
    "workbench.download.nvidia.com": "NVIDIA（AI Workbench）",
    "developer.download.nvidia.com": "NVIDIA（CUDA／HPC SDK）",
    "repo.download.nvidia.com": "NVIDIA（Spark OS）",
}


def _group_name(orig):
    """把來源整理成人看得懂的分組名。先查主機對照表，沒有就用 Origin 欄位，再沒有就用網址。"""
    site = orig["site"]
    if site in SITE_NAMES:
        return SITE_NAMES[site]
    name = orig["origin"] or orig["label"] or site or msg('unknown_source', LANG_DEFAULT)
    if site.endswith("nvidia.com"):
        # NVIDIA 有多個 repo（spark / baseos / cuda），保留路徑辨識
        return f"NVIDIA ({site})"
    return name


# Spark OS 本體／NVIDIA 平台套件：Dashboard 的「韌體＋重開機」流程對這些才有意義
SPARK_CORE_RE = re.compile(r"^(dgx-|nvidia-dgx|nvidia-driver|nvidia-kernel|nvidia-fabricmanager|"
                           r"linux-image-nvidia|linux-headers-nvidia|linux-nvidia|linux-image-\d[\d.]*-nvidia)")
# 核心與 NVIDIA 簽章模組是兩個獨立 meta 套件，彼此沒有相依：只升其中一個會開出沒有 GPU 驅動的系統
KERNEL_RE = re.compile(r"^(linux-image-nvidia|linux-nvidia|linux-headers-nvidia|linux-image-\d[\d.]*-nvidia)")
NVMOD_RE = re.compile(r"^linux-modules-nvidia-")


def kernel_pairing_warning(selected, upgradable):
    """勾了核心沒勾 NVIDIA 模組（或反過來）就警告。這是可勾選設計唯一比 Dashboard 危險的地方。"""
    sel_k = [n for n in selected if KERNEL_RE.match(n)]
    sel_m = [n for n in selected if NVMOD_RE.match(n)]
    av_k = [n for n in upgradable if KERNEL_RE.match(n)]
    av_m = [n for n in upgradable if NVMOD_RE.match(n)]
    if sel_k and av_m and not sel_m:
        return {"kind": "missing_modules", "missing": sorted(av_m),
                "message": msg('missing_modules', LANG_DEFAULT)}
    if sel_m and av_k and not sel_k:
        return {"kind": "missing_kernel", "missing": sorted(av_k),
                "message": msg('missing_kernel', LANG_DEFAULT)}
    return None


_DASH_CACHE = {"ts": 0, "data": None}


def dashboard_equivalent():
    """DGX Dashboard 那顆 Update 實際涵蓋什麼。依據：apt 歷史裡它的 aptdaemon 角色是 role-upgrade-system，
    且歷次都有安裝甚至移除套件——aptdaemon 的 safe_mode=True 會跳過這類升級，所以它用的是完整升級。
    之後再跑韌體並強制重開機（前端打 /update_reboot）。
    dist-upgrade 模擬要 1.5 秒以上，而更新分頁每次開頁都會打一次，所以快取起來；套件裝完會一併失效。"""
    if _DASH_CACHE["data"] and time.time() - _DASH_CACHE["ts"] < 300:
        return _DASH_CACHE["data"]
    out = _run(["apt-get", "-s", "dist-upgrade"], timeout=180) or ""
    ups, news = [], []
    for m in re.finditer(r"^Inst (\S+)(?: \[([^\]]*)\])?", out, re.M):
        (ups if m.group(2) else news).append(m.group(1))
    remv = re.findall(r"^Remv (\S+)", out, re.M)
    fw = None
    try:
        fw = fwupd_status().get("updates")
    except Exception:
        pass
    data = {"upgrade": len(ups), "install": len(news), "remove": len(remv), "firmware": fw,
            "packages": sorted(set(ups + news)), "removes": sorted(set(remv)),
            "note": msg('dashboard_note', LANG_DEFAULT)}
    _DASH_CACHE.update(ts=time.time(), data=data)
    return data


_MODFW_CACHE = {"ts": 0, "names": set()}


def _loaded_module_firmware():
    """已載入核心模組用 MODULE_FIRMWARE 宣告會請求的 firmware 檔名（modinfo -F firmware）。
    這是「本機驅動會不會用到某包 firmware」唯一能查證的依據；沒宣告但動態請求的驅動查不到，
    所以前端把結論標成「依 modinfo」而不是斷言沒用。"""
    if time.time() - _MODFW_CACHE["ts"] < 600:
        return _MODFW_CACHE["names"]
    names = set()
    try:
        mods = [l.split()[0] for l in open("/proc/modules")]
    except OSError:
        mods = []
    for m in mods:
        out = _run(["modinfo", "-F", "firmware", m], timeout=5) or ""
        names.update(l.strip() for l in out.splitlines() if l.strip())
    _MODFW_CACHE.update(ts=time.time(), names=names)
    return names


def _firmware_usage(pkg, cache):
    """linux-firmware-* 子套件：被誰拖進來、本機已載入的模組有幾個檔案會用到。不是子套件回 None。"""
    if not pkg.name.startswith("linux-firmware-") or not pkg.installed:
        return None
    meta = cache["linux-firmware"] if "linux-firmware" in cache else None
    pulled_by = ""
    if meta and meta.installed and any(d.name == pkg.name for dep in meta.installed.dependencies for d in dep):
        pulled_by = "linux-firmware"
    wanted = _loaded_module_firmware()
    files = used = 0
    for f in pkg.installed_files:
        if not f.startswith("/lib/firmware/") or not os.path.isfile(f):
            continue
        files += 1
        rel = f[len("/lib/firmware/"):]
        for suf in (".zst", ".xz"):
            if rel.endswith(suf):
                rel = rel[:-len(suf)]
        if rel in wanted:
            used += 1
    return {"auto": pkg.is_auto_installed, "pulled_by": pulled_by, "files": files, "used": used,
            "passive": pkg.is_auto_installed and used == 0 and files > 0}


# ---------- 降回上一版：更新前把被換掉的舊版 .deb 留一份 ----------
# 為什麼要自己留：Ubuntu 的來源只發布最新版，「清 apt 快取」又會把本機的舊 .deb 清掉；出事時找不到退路。
# 來源順序：本機 apt 快取（免下載）→ 來源伺服器的 pool（Ubuntu 鏡像短期內、Microsoft 等長期保留）→ Launchpad（Ubuntu 官方套件永久保留）。
# ESM 的 pool 要授權、第三方多半不留，這些誠實標「未保留」與原因。保留最近 5 次更新，每個檔案上限 150 MB。
ROLLBACK_DIR = os.path.join(HERE, "data", "rollback")
ROLLBACK_KEEP = 5
ROLLBACK_MAX_MB = 150
_ROLLBACK_LOCK = threading.Lock()


def _rollback_index_load():
    try:
        with open(os.path.join(ROLLBACK_DIR, "index.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _rollback_index_save(jobs):
    os.makedirs(ROLLBACK_DIR, exist_ok=True)
    tmp = os.path.join(ROLLBACK_DIR, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(ROLLBACK_DIR, "index.json"))


def _deb_matches(path, name, version):
    out = _run(["dpkg-deb", "-f", path, "Package", "Version"], timeout=30) or ""
    got = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return got.get("Package") == name and got.get("Version") == version


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rollback_changes(cache, names):
    """模擬這次升級，回實際會被換掉／移除的已安裝套件（含相依帶入的），勾選的排前面。
    只備份勾選清單會漏掉相依帶動的更新：舊主程式要求舊函式庫時，只留主程式沒有完整退路。"""
    for name in names:
        pkg = cache.get(name)
        if pkg and pkg.installed and pkg.candidate and pkg.candidate != pkg.installed:
            try:
                pkg.mark_upgrade()
            except Exception:
                pass
    out = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name); out.append((name, True))
    for pkg in cache.get_changes():
        if pkg.is_installed and pkg.name not in seen and (pkg.marked_upgrade or pkg.marked_downgrade or pkg.marked_delete):
            seen.add(pkg.name); out.append((pkg.name, False))
    return out


def _fetch_file(url, dest, max_bytes):
    """下載到 dest；先看 Content-Length 擋太大的；回 (ok, reason)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "spark-center"})
    with urllib.request.urlopen(req, timeout=60) as r:
        n = int(r.headers.get("Content-Length") or 0)
        if n > max_bytes:
            return False, ("too_big", n)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    return False, ("too_big", got)
                f.write(chunk)
    return True, None


def rollback_prepare(names, log=lambda s: None, status=lambda s: None):
    """在升級前呼叫。回這次的紀錄（也已寫進 index）。任何一個套件失敗都不影響升級本身。"""
    cache = apt.Cache()
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    job_dir = os.path.join(ROLLBACK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    rec = {"id": job_id, "started": datetime.now().isoformat(timespec="seconds"), "packages": []}
    max_bytes = ROLLBACK_MAX_MB * 1024 * 1024
    try:
        targets = _rollback_changes(cache, names)
    except Exception as e:
        log("rollback simulate failed: " + str(e)); targets = [(n, True) for n in names]
    for name, selected in targets:
        pkg = cache.get(name)
        entry = {"name": name, "selected": selected, "old": None, "new": None, "arch": None, "deb": None, "source": None, "verified": None, "reason": None}
        rec["packages"].append(entry)
        if not pkg or not pkg.installed:
            entry["reason"] = msg("rollback_not_installed", LANG_DEFAULT); continue
        inst = pkg.installed
        entry.update(old=inst.version, new=None if pkg.marked_delete else (pkg.candidate.version if pkg.candidate else None), arch=inst.architecture)
        status(msg("rollback_preparing", LANG_DEFAULT, name=name, old=inst.version))
        fname = f"{name}_{inst.version.replace(':', '%3a')}_{inst.architecture}.deb"
        dest = os.path.join(job_dir, fname)
        # 可信雜湊：apt 索引裡這個版本的 sha256（索引由簽章過的 Release 檔背書）。舊版常已從索引消失 → None。
        try:
            expected = inst.sha256 or None
        except Exception:
            expected = None
        # 1. 本機 apt 快取：apt 下載時已核對過雜湊（root 才寫得進去）；索引還有的話再對一次。
        cached = os.path.join("/var/cache/apt/archives", fname)
        if os.path.isfile(cached) and _deb_matches(cached, name, inst.version) and (not expected or _sha256_of(cached) == expected):
            shutil.copyfile(cached, dest); entry.update(deb=os.path.relpath(dest, ROLLBACK_DIR), source="cache", verified="sha256" if expected else "apt")
            log(msg("rollback_kept", LANG_DEFAULT, name=name, old=inst.version, source=msg("rollback_src_cache", LANG_DEFAULT))); continue
        # 2. 來源伺服器的 pool（多半是 HTTP，只在索引還有雜湊可核對時採用）；
        # 3. Launchpad（HTTPS；只要這個套件任一版本來自 Ubuntu 官方來源就試——舊版從索引消失後
        #    installed.origins 會是空的，只看舊版的來源會永遠不試，而那正是需要備援的時候）。
        cands = []
        try:
            if inst.uri and expected:
                cands.append(("repo", inst.uri))
        except Exception:
            pass
        sites = {(_origin_of(v).get("site") or "") for v in pkg.versions}
        if any(st.endswith("ubuntu.com") and "esm.ubuntu.com" not in st for st in sites):
            noepoch = inst.version.split(":", 1)[-1]
            cands.append(("launchpad", f"https://launchpad.net/ubuntu/+archive/primary/+files/{name}_{noepoch}_{inst.architecture}.deb"))
        reason = msg("rollback_no_source", LANG_DEFAULT)
        for source, url in cands:
            try:
                ok, why = _fetch_file(url, dest, max_bytes)
            except Exception as e:
                reason = msg("rollback_fetch_failed", LANG_DEFAULT, err=str(e)[:80]); continue
            if not ok:
                reason = msg("rollback_too_big", LANG_DEFAULT, mb=round(why[1] / 1048576), max=ROLLBACK_MAX_MB); break
            if not _deb_matches(dest, name, inst.version):
                reason = msg("rollback_bad_deb", LANG_DEFAULT); continue
            if expected:
                if _sha256_of(dest) != expected:
                    reason = msg("rollback_hash_mismatch", LANG_DEFAULT); continue
                verified = "sha256"
            elif url.startswith("https://launchpad.net/"):
                verified = "tls"
            else:
                reason = msg("rollback_no_trusted_hash", LANG_DEFAULT); continue
            entry.update(deb=os.path.relpath(dest, ROLLBACK_DIR), source=source, verified=verified)
            log(msg("rollback_kept", LANG_DEFAULT, name=name, old=inst.version, source=msg(f"rollback_src_{source}", LANG_DEFAULT))); break
        if not entry["deb"]:
            try:
                os.remove(dest)
            except OSError:
                pass
            entry["reason"] = reason
            log(msg("rollback_not_kept", LANG_DEFAULT, name=name, old=inst.version, reason=reason))
    _rollback_index_add(rec)
    return rec


def _rollback_index_add(rec):
    """把一筆新紀錄放進索引：同 id 或同一組（套件、舊版、新版）的舊紀錄連目錄一起刪（更新→降回→再更新會產生一模一樣的紀錄），
    超過 ROLLBACK_KEEP 的最舊的也刪。"""
    def _sig(j):
        return frozenset((p.get("name"), p.get("old"), p.get("new")) for p in j.get("packages", []) if p.get("deb") or p.get("kind") == "npm")
    with _ROLLBACK_LOCK:
        jobs = []
        for j in _rollback_index_load():
            if j.get("id") == rec["id"] or (_sig(j) and _sig(j) == _sig(rec)):
                shutil.rmtree(os.path.join(ROLLBACK_DIR, j["id"]), ignore_errors=True)
                continue
            jobs.append(j)
        jobs.insert(0, rec)
        for old_job in jobs[ROLLBACK_KEEP:]:
            shutil.rmtree(os.path.join(ROLLBACK_DIR, old_job["id"]), ignore_errors=True)
        _rollback_index_save(jobs[:ROLLBACK_KEEP])


def rollback_deb_paths(job_id, name=None):
    """該筆工作裡保留下來的 .deb（name 給了就只回那一個）。路徑限制在 ROLLBACK_DIR 底下。"""
    out = []
    for j in _rollback_index_load():
        if j.get("id") == job_id:
            for p in j.get("packages", []):
                if (name is None or p.get("name") == name) and p.get("deb"):
                    fp = os.path.realpath(os.path.join(ROLLBACK_DIR, p["deb"]))
                    if fp.startswith(os.path.realpath(ROLLBACK_DIR) + os.sep) and os.path.isfile(fp):
                        out.append((fp, p))
    return out


# apt-get 對本機 .deb 的降版指令。--allow-downgrades 明講意圖；--no-remove 擋掉「為了降函式庫而移除依賴新版的應用程式」；
# conffile 沿用更新時的政策：使用者改過的設定檔一律保留現有版本（服務的 stdin 是 /dev/null，dpkg 問不到人會直接失敗、留下未設定的套件）。
ROLLBACK_APT = ["/usr/bin/apt-get", "install", "-y", "--allow-downgrades", "--no-remove",
                "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


def rollback_simulate(paths):
    """apt-get -s 模擬降回，回傳實際會被動到的套件（不需 root、不動系統）。
    不加 --no-remove 是為了看清楚會移除誰；有移除就回 ok=False，因為降回不該順手拆掉別的應用程式。"""
    cmd = ["/usr/bin/apt-get", "-s", "-o", "Debug::NoLocking=1", "install", "-y", "--allow-downgrades"] + paths
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=dict(os.environ, LC_ALL="C", LANG="C"))
    except Exception as e:
        return {"ok": False, "error": msg("rollback_sim_failed", LANG_DEFAULT, err=str(e)[:120])}
    if r.returncode != 0:
        err = "\n".join(l for l in (r.stderr + r.stdout).splitlines() if l.startswith("E:")) or f"rc={r.returncode}"
        return {"ok": False, "error": msg("rollback_sim_failed", LANG_DEFAULT, err=err[:300])}
    changes = []
    for line in r.stdout.splitlines():
        m = re.match(r"(Inst|Remv) (\S+?)(?::\S+)? (?:\[([^\]]*)\] )?(?:\(([^\s)]+))?", line)
        if not m:
            continue
        kind, name, frm, to = m.groups()
        if kind == "Remv":
            action = "remove"
        elif not frm:
            action = "install"
        elif apt_pkg.version_compare(to or "", frm) < 0:
            action = "downgrade"
        else:
            action = "upgrade"
        changes.append({"name": name, "action": action, "from": frm or "", "to": to or "", "requested": False})
    kept = {os.path.basename(p).split("_", 1)[0] for p in paths}
    for c in changes:
        c["requested"] = c["name"] in kept
    changes.sort(key=lambda c: (not c["requested"], c["action"], c["name"]))
    removed = [c["name"] for c in changes if c["action"] == "remove"]
    if removed:
        return {"ok": False, "changes": changes, "error": msg("rollback_would_remove", LANG_DEFAULT, pkgs=", ".join(removed))}
    return {"ok": True, "changes": changes}


# ---------- npm 全域套件 ----------
# 為什麼要做：Claude Code、Gemini CLI、OpenClaw 這類工具是 npm -g 裝的，apt/snap/flatpak 都看不到，改版又快。
# 裝在使用者的 prefix（~/.npmrc 的 prefix），更新不需要 root。舊版 registry 永久保留，降回只要指定版號，不用留檔案。
# pip 與 Docker 刻意不做：系統 pip 套件是 apt 管的（PEP 668），venv 的版本是專案鎖的；Docker pull 新映像不等於容器已更新。
_NPM = {"ts": 0, "data": None, "lock": threading.Lock()}
NPM_TTL = 600
NPM_BIN = shutil.which("npm") or "/usr/bin/npm"


def npm_running(prefix, names):
    """哪些程序正在執行這些 npm 全域套件的檔案（掃 /proc/*/cmdline 找 <prefix>/lib/node_modules/<name>/）。
    為什麼要看：npm install -g 直接覆蓋目錄，正在跑的程序之後才載入的模組會讀到新版，新舊混用會出錯，更新完要重啟。
    同時從 cgroup 讀出它屬於哪個 systemd 使用者服務（app.slice/xxx.service），有的話可以一鍵重啟；沒有的只能提醒。"""
    out = {n: [] for n in names}
    if not prefix:
        return out
    base = os.path.join(prefix, "lib", "node_modules") + os.sep
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        hit = None
        for a in argv[:8]:
            a = a.decode("utf-8", "replace")
            # npm 的 bin 是符號連結（<prefix>/bin/openclaw → ../lib/node_modules/openclaw/…），cmdline 可能只留 bin 路徑，要解回真實路徑
            if "/" in a and not a.startswith(base):
                try:
                    a = os.path.realpath(a)
                except OSError:
                    pass
            if a.startswith(base):
                rest = a[len(base):]
                name = rest.split("/")[0]
                if name.startswith("@") and "/" in rest[len(name) + 1:] + "/":
                    name = name + "/" + rest[len(name) + 1:].split("/")[0]
                if name in out:
                    hit = name; break
        if not hit:
            continue
        unit = None
        try:
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
                cg = f.read().strip().split(":", 2)[-1]
            last = cg.rsplit("/", 1)[-1]
            if "/user@" in cg and last.endswith(".service"):
                unit = last
        except OSError:
            pass
        cmd = " ".join(x.decode("utf-8", "replace") for x in argv if x)
        # 程序啟動時間（/proc/<pid> 目錄的 ctime）早於套件目前的 package.json → 記憶體裡跑的是更新前的舊版
        stale = None
        try:
            started = os.stat(f"/proc/{pid}").st_ctime
            installed_at = os.stat(os.path.join(base, hit, "package.json")).st_mtime
            stale = started < installed_at
        except OSError:
            pass
        out[hit].append({"pid": int(pid), "unit": unit, "cmd": cmd[:160], "stale": stale})
    return out


def _semver_key(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 1 if m.group(4) is None else 0, m.group(4) or "")


def _semver_lt(a, b):
    ka, kb = _semver_key(a), _semver_key(b)
    return bool(ka and kb and ka < kb)


_NODE_REL = {"ts": 0, "data": None}


def node_release_info(installed):
    """Node 官方版本表：現行 LTS 是哪個大版本、已裝的大版本支援到何時。一天查一次；查不到回 error，欄位留 None（前端顯示「—」）。
    為什麼要查：大版本升級不會出現在 apt 清單（NodeSource 每個大版本是不同倉庫），使用者不會自己去看。"""
    if _NODE_REL["data"] and time.time() - _NODE_REL["ts"] < 86400:
        d = dict(_NODE_REL["data"])
    else:
        d = {"lts_major": None, "lts_version": None, "installed_end": None, "lts_end": None, "error": None, "fetched": None}
        try:
            with urllib.request.urlopen(urllib.request.Request("https://nodejs.org/dist/index.json", headers={"User-Agent": "spark-center"}), timeout=15) as r:
                idx = json.load(r)
            lts = next((x for x in idx if x.get("lts")), None)
            if lts:
                d["lts_version"] = lts["version"].lstrip("v"); d["lts_major"] = int(d["lts_version"].split(".")[0])
            with urllib.request.urlopen(urllib.request.Request("https://raw.githubusercontent.com/nodejs/Release/main/schedule.json", headers={"User-Agent": "spark-center"}), timeout=15) as r:
                d["_schedule"] = {k.lstrip("v"): v.get("end") for k, v in json.load(r).items()}
            d["fetched"] = datetime.now().isoformat(timespec="seconds")
            _NODE_REL.update(ts=time.time(), data=dict(d))   # 存副本：下面的 pop 不能動到快取，否則第二次查就沒有時程了
        except Exception as e:
            d["error"] = msg("node_release_failed", LANG_DEFAULT, err=str(e)[:80])
    sched = d.pop("_schedule", {}) or {}
    major = (installed or "").split(".")[0]
    d["installed_end"] = sched.get(major)
    d["lts_end"] = sched.get(str(d["lts_major"])) if d["lts_major"] else None
    return d


def _node_semver_satisfies(node_version, ranges):
    """用 npm 自帶的 semver 判斷 node_version 是否符合各套件的 engines.node 範圍。回 {name: True/False}；跑不了回 {}（呼叫端當「不明」）。"""
    if not ranges:
        return {}
    js = "const s=require(process.argv[1]);const r=JSON.parse(process.argv[3]);const o={};for(const k in r){try{o[k]=s.satisfies(process.argv[2],r[k])}catch(e){o[k]=null}}console.log(JSON.stringify(o))"
    for sem in ("/usr/lib/node_modules/npm/node_modules/semver", os.path.join(os.path.dirname(os.path.realpath(NPM_BIN)), "..", "lib", "node_modules", "npm", "node_modules", "semver")):
        if os.path.isdir(sem):
            out = _run(["node", "-e", js, sem, node_version, json.dumps(ranges)], timeout=20)
            if out:
                try:
                    return json.loads(out)
                except ValueError:
                    return {}
    return {}


NODE_HELPER_SOURCE = os.path.join(HERE, 'tools', 'node_source.py')
NODE_HELPER = '/usr/local/libexec/spark-center/node_source.py'


def node_helper_ready():
    try:
        p = Path(NODE_HELPER)
        for item in (p, *p.parents):
            st = item.lstat()
            if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                return False
            if item == p and (not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o100):   # 要能直接執行（pkexec 跑它本身）
                return False
        # 套件更新後必須由管理員重新安裝 helper，不偷偷升級提權程式。
        return p.read_bytes() == Path(NODE_HELPER_SOURCE).read_bytes()
    except OSError:
        return False


def node_error(detail):
    key, _, extra = str(detail).partition(':')
    return msg(key if key in MSG[LANG_DEFAULT] else 'node_command_failed', LANG_DEFAULT) + (': ' + extra if extra else '')


def node_read(*args):
    if os.path.realpath(shutil.which('node') or '') != '/usr/bin/node':
        return {'ok': False, 'error': msg('node_source_unsupported', LANG_DEFAULT)}
    if not node_helper_ready():
        # 把安裝指令連 repo 路徑一起給前端，使用者複製貼上就好，不必翻 README
        return {'ok': False, 'error': msg('node_helper_unavailable', LANG_DEFAULT), 'helper_missing': True,
                'install_cmds': [f'cd {shlex.quote(HERE)}',
                                 'sudo install -d -o root -g root -m 0755 /usr/local/libexec/spark-center',
                                 'sudo install -o root -g root -m 0755 tools/node_source.py /usr/local/libexec/spark-center/node_source.py',
                                 'sudo install -o root -g root -m 0644 tools/spark-center-node-source.policy /usr/share/polkit-1/actions/io.github.tiong6.spark-center.node-source.policy']}
    r = subprocess.run(['/usr/bin/python3', '-I', NODE_HELPER, *args], capture_output=True,
                       text=True, timeout=90, env=_ENV_C)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return {'ok': False, 'error': node_error(r.stderr)}
    if not j.get('ok'):
        j['error'] = node_error(j.get('error', ''))
    return j


def node_status():
    j = node_read('status')
    # helper 沒裝時也要知道「有沒有升級可做」：Node 已是現行 LTS 就整塊不顯示，不該為了一個用不到的功能叫人裝 helper
    ver = j['installed'].split('-', 1)[0] if j.get('ok') else (_run(["node", "--version"], timeout=10) or "").strip().lstrip("v")
    info = node_release_info(ver) if ver else {}
    j['target'] = info.get('lts_major')
    j['expected_version'] = info.get('lts_version')
    j['release_error'] = info.get('error')
    if not j.get('ok') and ver:
        j['major'] = int(ver.split('.')[0]) if ver.split('.')[0].isdigit() else None
    # 完成與否由 helper 的 effective_state 判定（status 回來的 state 已是有效狀態），這裡不再自己改
    return j


def node_npm_impact(version):
    # 回退檢查只讀本機已安裝套件，不需要 registry 在線，也不把快取當成現值。
    r = subprocess.run([NPM_BIN, 'ls', '-g', '--depth=0', '--json'], capture_output=True,
                       text=True, timeout=60, env=_ENV_C)
    try:
        if r.returncode:
            raise ValueError()
        deps = json.loads(r.stdout).get('dependencies', {})
        prefix = (_run([NPM_BIN, 'prefix', '-g'], timeout=20) or '').strip()
        if not prefix:
            raise ValueError()
        ranges, unknown = {}, []
        for name in deps:
            try:
                with open(os.path.join(prefix, 'lib', 'node_modules', name, 'package.json')) as f:
                    meta = json.load(f)
                constraint = (meta.get('engines') or {}).get('node')
                if constraint:
                    ranges[name] = constraint
            except (OSError, ValueError, TypeError):
                unknown.append(name)
        results = _node_semver_satisfies(version.split('-', 1)[0], ranges)
        return [{'name': n, 'range': ranges.get(n), 'unknown': n in unknown or results.get(n) is None}
                for n in sorted(set(unknown) | {n for n in ranges if results.get(n) is not True})]
    except (ValueError, TypeError):
        return [{'name': 'npm', 'range': None, 'unknown': True}]


def node_plan(action, target):
    if action not in ('prepare', 'install', 'restore') or type(target) is not int:
        return {'ok': False, 'error': msg('node_bad_action', LANG_DEFAULT)}
    if action == 'prepare':
        current = node_status()
        if not current.get('ok'):
            return current
        if target != current.get('target'):
            return {'ok': False, 'error': msg('node_target_unavailable', LANG_DEFAULT)}
    j = node_read('preview', action, str(target))
    if j.get('ok'):
        j['impact'] = node_npm_impact(j['version']) if action == 'restore' and j.get('args') else []
        j['expected_version'] = current.get('expected_version') if action == 'prepare' else None
        j['confirmation'] = hashlib.sha256(json.dumps([j['token'], j['impact'], j['expected_version']], sort_keys=True).encode()).hexdigest()
    return j


def npm_status(force=False):
    """npm ls -g（全部）＋ npm outdated -g（有新版的）。outdated 有新版時結束碼是 1，不能用 _run 的 0 判定。"""
    with _NPM["lock"]:
        if not force and _NPM["data"] and time.time() - _NPM["ts"] < NPM_TTL:
            # 套件查詢可以快取 10 分鐘，程序狀態不行：重啟後 PID 就變了，要每次即時重掃
            cached = dict(_NPM["data"])
            try:
                running = npm_running(cached.get("prefix"), [e["name"] for e in cached.get("packages", [])])
                cached["packages"] = [dict(e, running=running.get(e["name"], [])) for e in cached["packages"]]
            except Exception:
                pass
            return cached
    if not os.path.isfile(NPM_BIN):
        return {"ok": True, "available": False, "note": msg('npm_missing', LANG_DEFAULT)}
    out = {"ok": True, "available": True, "packages": [], "outdated": None, "error": None,
           "generated": datetime.now().isoformat(timespec="seconds"), "prefix": None,
           "node": (_run(["node", "--version"], timeout=10) or "").strip().lstrip("v") or None,
           "npm": (_run([NPM_BIN, "--version"], timeout=20) or "").strip() or None}
    out["node_info"] = node_release_info(out["node"])
    try:
        r = subprocess.run([NPM_BIN, "ls", "-g", "--depth=0", "--json"], capture_output=True, text=True, timeout=60, env=_ENV_C)
        deps = json.loads(r.stdout or "{}").get("dependencies") or {}
        pk = {n: {"name": n, "current": v.get("version"), "latest": None, "outdated": False} for n, v in deps.items()}
        out["prefix"] = (_run([NPM_BIN, "prefix", "-g"], timeout=20) or "").strip() or None
        # 描述、作者、原始碼倉庫從套件自己的 package.json 讀（不上網）。這些是套件自己宣稱的，npm 不驗證作者身分；
        # 前端只照實顯示並註明來源，判斷交給使用者。
        for n, e in pk.items():
            e.update(description=None, author=None, repo=None, homepage=None, license=None)
            try:
                with open(os.path.join(out["prefix"] or "", "lib", "node_modules", n, "package.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                a = meta.get("author"); a = a if isinstance(a, str) else (a or {}).get("name")
                r = meta.get("repository"); r = r if isinstance(r, str) else (r or {}).get("url")
                if r:
                    r = re.sub(r"^git\+|\.git$", "", r); r = re.sub(r"^(git|ssh)://", "https://", r); r = re.sub(r"^git@github\.com:", "https://github.com/", r)
                    if not r.startswith("http"):
                        r = "https://github.com/" + r   # npm 簡寫 "owner/repo"
                lic = meta.get("license"); lic = lic if isinstance(lic, str) else (lic or {}).get("type")
                e.update(description=(meta.get("description") or "")[:140] or None, author=(re.sub(r"\s*[<(].*$", "", a) if a else None),
                         repo=r, homepage=meta.get("homepage"), license=lic, engines_node=(meta.get("engines") or {}).get("node"))
            except Exception:
                pass
    except Exception as e:
        out.update(error=msg('npm_query_failed', LANG_DEFAULT, err=str(e)[:120]))
        return out
    try:
        r = subprocess.run([NPM_BIN, "outdated", "-g", "--json"], capture_output=True, text=True, timeout=90, env=_ENV_C)
        od = json.loads(r.stdout or "{}") if r.returncode in (0, 1) else None
        if od is None:
            raise RuntimeError((r.stderr or "").strip()[:120] or f"rc={r.returncode}")
        # wanted = npm 依目前 Node 版本（engines）算出的可裝最新版；dist-tag 的 latest 可能要求更新的 Node。
        # 更新時要裝 wanted 而不是 @latest：@latest 會硬裝不相容版（只印 EBADENGINE 警告），真機踩到：
        # agent-browser 0.38.1 要 Node ≥24，機器是 22。已裝版比 wanted 新就不是「有新版」，另標「較新」。
        for n, v in od.items():
            e = pk.setdefault(n, {"name": n, "current": v.get("current"), "latest": None, "outdated": False})
            want = v.get("wanted") or v.get("latest")
            e.update(latest=want, dist_latest=v.get("latest"), outdated=bool(want) and _semver_lt(v.get("current"), want),
                     newer=bool(want) and _semver_lt(want, v.get("current")))
        # npm 的選版只是「偏好」相容 engines 的版本：整個套件都不相容時仍會回不相容版，install 也只印警告。
        # 這裡對每個目標版查 engines.node，用 npm 附帶的 semver 核對目前 Node；不相容就標 blocked、不給更新鈕。
        # 「裝回相容版」（newer）的目標也要核對，不然前端給了鈕、後端不認。
        # npm view 失敗時退出碼非 0、stdout 是 {"error":{...}}：要和「成功查到但沒宣告 engines」分開，不能當成沒限制。
        checks = {}
        for e in pk.values():
            if e["outdated"] or e.get("newer"):
                try:
                    v = subprocess.run([NPM_BIN, "view", f"{e['name']}@{e['latest']}", "engines", "--json"], capture_output=True, text=True, timeout=30, env=_ENV_C)
                    body = json.loads(v.stdout) if v.stdout.strip() else {}
                    if v.returncode != 0 or (isinstance(body, dict) and body.get("error")):
                        raise RuntimeError(((body.get("error") or {}).get("code") if isinstance(body, dict) else None) or (v.stderr or "").strip()[:60] or f"rc={v.returncode}")
                    checks[e["name"]] = (body or {}).get("node")
                except Exception as ex:
                    checks[e["name"]] = f"?{str(ex)[:40]}"
        if any(str(r).startswith("?") for r in checks.values()):
            out["engines_partial"] = True   # 有查詢失敗的：這次結果不進快取，下次開頁再試
        if checks and out["node"]:
            sat = _node_semver_satisfies(out["node"], {n: r for n, r in checks.items() if r and not r.startswith("?")})
            for n, r in checks.items():
                e = pk[n]
                e["target_engines"] = r
                if r and r.startswith("?"):
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_unknown', LANG_DEFAULT))
                elif r and sat.get(n) is False:
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_blocked', LANG_DEFAULT, ver=e["latest"], need=r, node=out["node"]))
                elif r and sat.get(n) is None:
                    e.update(outdated=False, newer=False, blocked=True, blocked_reason=msg('npm_engine_unknown', LANG_DEFAULT))
        out["outdated"] = sum(1 for e in pk.values() if e["outdated"])
    except Exception as e:
        # 查不到新版不能變成「全部最新」：outdated 留 None，前端顯示「—」並掛紅字
        out.update(error=msg('npm_outdated_failed', LANG_DEFAULT, err=str(e)[:120]))
    try:
        running = npm_running(out["prefix"], list(pk))
        for n, e in pk.items():
            e["running"] = running.get(n, [])
    except Exception:
        for e in pk.values():
            e["running"] = []
    out["packages"] = sorted(pk.values(), key=lambda e: (not e["outdated"], e["name"]))
    with _NPM["lock"]:
        if not out["error"] and not out.get("engines_partial"):
            _NPM.update(ts=time.time(), data=out)
    return out


def rollback_record_npm(names):
    """npm 更新前把「目前版 → 新版」記進降回索引（不留檔案：registry 保留所有版本，降回就是 npm install -g name@old）。"""
    st = npm_status()
    cur = {e["name"]: e for e in st.get("packages", [])}
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    rec = {"id": job_id, "started": datetime.now().isoformat(timespec="seconds"), "packages": []}
    for n in names:
        e = cur.get(n) or {}
        rec["packages"].append({"name": n, "kind": "npm", "selected": True, "old": e.get("current"), "new": e.get("latest"),
                                "arch": None, "deb": None, "source": "npm", "verified": "registry",
                                "reason": None if e.get("current") else msg("rollback_not_installed", LANG_DEFAULT)})
    _rollback_index_add(rec)
    return rec


def rollback_npm_entries(job_id, name=None):
    for j in _rollback_index_load():
        if j.get("id") == job_id:
            return [p for p in j.get("packages", []) if p.get("kind") == "npm" and p.get("old") and (name is None or p.get("name") == name)]
    return []


_NODE_FLOW = {"ts": 0, "phase": None}


def _node_flow_pending():
    """Node 主版本升級進行到一半（已換倉庫、還沒裝或裝失敗）時回 phase，否則 None。
    這時 apt 清單也看得到 nodejs 24：從清單裝會讓升級流程的狀態對不上（真的發生過），所以清單裡的 nodejs 鎖住，只留一條路。
    判定交給 helper 的 effective_state（和面板、下一次升級的放行是同一個邏輯），結果快取 20 秒。"""
    if time.time() - _NODE_FLOW["ts"] < 20:
        return _NODE_FLOW["phase"]
    ph = None
    try:
        with open('/var/lib/spark-center/node-source/state.json', encoding='utf-8') as f:
            raw = json.load(f).get('phase')
        if raw in ('preparing', 'prepared', 'installing', 'install_failed'):
            j = node_read('status')
            st = (j.get('state') or {}) if j.get('ok') else {'phase': raw}
            ph = st.get('phase') if st.get('phase') in ('preparing', 'prepared', 'installing', 'install_failed') else None
    except (OSError, ValueError):
        ph = None
    _NODE_FLOW.update(ts=time.time(), phase=ph)
    return ph


def list_updates():
    cache = apt.Cache()
    node_flow = _node_flow_pending()
    items = []
    for pkg in cache:
        if not pkg.is_upgradable:
            continue
        cand, inst = pkg.candidate, pkg.installed
        orig = _origin_of(cand)
        items.append({
            "name": pkg.name,
            "installed": inst.version if inst else "",
            "candidate": cand.version,
            "group": _group_name(orig),
            "site": orig["site"],
            "origin_raw": orig["origin"] or orig["label"] or "",
            "archive": orig["archive"],
            "size": cand.size,
            "summary": cand.summary or "",
            "security": "security" in (orig["archive"] or "").lower(),
            "reboot_hint": bool(REBOOT_HINT_RE.match(pkg.name)),
            "spark_core": bool(SPARK_CORE_RE.match(pkg.name)),
            "firmware": _firmware_usage(pkg, cache),
            "node_flow": node_flow if pkg.name == "nodejs" and node_flow else None,
        })
    items.sort(key=lambda x: (x["group"], x["name"]))
    return items


def simulate(names):
    """在記憶體裡標記升級，回傳實際會被動到的套件。不動系統。"""
    cache = apt.Cache()
    unknown = [n for n in names if n not in cache]
    if unknown:
        return {"ok": False, "error": msg('pkgs_missing', LANG_DEFAULT, p0=', '.join(unknown))}
    try:
        with cache.actiongroup():
            for n in names:
                cache[n].mark_upgrade()
    except Exception as e:  # 相依性解不開
        return {"ok": False, "error": msg('deps_failed', LANG_DEFAULT, p0=e)}
    changes = []
    for p in cache.get_changes():
        if p.marked_delete:
            action = "remove"
        elif p.marked_install:
            action = "install"
        elif p.marked_upgrade:
            action = "upgrade"
        elif p.marked_downgrade:
            action = "downgrade"
        else:
            action = "other"
        changes.append({
            "name": p.name,
            "action": action,
            "from": p.installed.version if p.installed else "",
            "to": p.candidate.version if p.candidate else "",
            "requested": p.name in names,
        })
    changes.sort(key=lambda c: (not c["requested"], c["action"], c["name"]))
    upgradable = [p.name for p in apt.Cache() if p.is_upgradable]
    return {
        "ok": True,
        "changes": changes,
        "pairing": kernel_pairing_warning(names, upgradable),
        "download_bytes": cache.required_download,
        "space_bytes": cache.required_space,
        "broken": cache.broken_count,
    }


# ---------- 登入時自動開啟（XDG autostart） ----------

DGX_DASHBOARD_ICON = "/usr/share/icons/hicolor/scalable/apps/nvidia-dgx-dashboard.png"   # dgx-dashboard 套件的圖示
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/spark-center.desktop")
DESKTOP_INSTALLED = os.path.expanduser("~/.local/share/applications/spark-center.desktop")


def autostart_status():
    """真相只有一個：autostart 目錄裡有沒有這個 .desktop 檔（且沒被 Hidden 關掉）。"""
    enabled = False
    if os.path.isfile(AUTOSTART_FILE):
        try:
            txt = open(AUTOSTART_FILE, encoding="utf-8").read()
            enabled = not re.search(r"^Hidden=true$", txt, re.M) and not re.search(r"^X-GNOME-Autostart-enabled=false$", txt, re.M)
        except OSError:
            pass
    return {"ok": True, "enabled": enabled, "path": AUTOSTART_FILE}


def autostart_set(enabled):
    if not enabled:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return autostart_status()
    # 以 install.sh 產生的桌面捷徑為準（Exec 已填好實際路徑）；沒有就用模板現填
    try:
        txt = open(DESKTOP_INSTALLED, encoding="utf-8").read()
    except OSError:
        txt = open(os.path.join(HERE, "app", "spark-center.desktop.in"), encoding="utf-8").read().replace("@ROOT@", HERE)
    txt = re.sub(r"^(Hidden|X-GNOME-Autostart-enabled|X-GNOME-Autostart-Delay)=.*\n?", "", txt, flags=re.M).rstrip("\n")
    txt += "\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n"   # 等桌面就緒再開，視窗才會在正確位置
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    return autostart_status()


def reboot_status():
    if not os.path.exists(REBOOT_FLAG):
        return {"required": False, "packages": []}
    pkgs = []
    try:
        with open(REBOOT_PKGS) as f:
            pkgs = [l.strip() for l in f if l.strip()]
    except OSError:
        pass
    return {"required": True, "packages": pkgs}


def last_refresh():
    """apt 索引最後更新時間。優先讀 apt 自己寫的 update-success-stamp（每次 apt update 成功都會碰），
    沒有就退回 lists 目錄最新檔案的 mtime（內容沒變時 apt 不會碰檔，可能偏舊）。拿不到回 None。"""
    stamp = "/var/lib/apt/periodic/update-success-stamp"
    if os.path.exists(stamp):
        return datetime.fromtimestamp(os.path.getmtime(stamp)).isoformat(timespec="seconds")
    try:
        mt = max(
            os.path.getmtime(os.path.join(APT_LISTS, f))
            for f in os.listdir(APT_LISTS)
            if not f.startswith(("lock", "partial", "auxfiles"))
        )
        return datetime.fromtimestamp(mt).isoformat(timespec="seconds")
    except (OSError, ValueError):
        return None


def apt_history(limit=8):
    """讀 apt 歷史最近幾筆，只回 Start-Date / Commandline / Upgrade 摘要。"""
    entries, cur = [], {}
    try:
        with open(APT_HISTORY, errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    if cur:
                        entries.append(cur)
                        cur = {}
                    continue
                k, _, v = line.partition(": ")
                cur[k] = v
        if cur:
            entries.append(cur)
    except OSError:
        return []
    out = []
    for e in entries[-limit:][::-1]:
        pk = []
        for key in ("Upgrade", "Install", "Remove", "Purge"):
            if key in e:
                names = re.findall(r"([^\s,()]+):[a-z0-9]+ \(", e[key])
                pk.append({"action": key.lower(), "count": len(names), "names": names})
        out.append({
            "start": e.get("Start-Date", ""),
            "commandline": e.get("Commandline", ""),
            "actions": pk,
        })
    return out


# ---------- aptdaemon 工作 ----------

class Job:
    """同一時間只允許一個 aptdaemon 交易。狀態給前端輪詢。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.state = {
            "kind": None,          # "install" | "refresh" | "flatpak" | "snap"
            "status": "idle",      # idle | running | done | error
            "packages": [],
            "progress": 0,
            "status_text": "",
            "details": "",
            "xfer": None,          # apt 下載明細：{"done","total","speed","eta","items","items_total"}，aptdaemon progress-details
            "exit": None,
            "error": None,
            "log": [],
            "started": None,
            "finished": None,
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _log(self, msg):
        with self.lock:
            self.state["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
            self.state["log"] = self.state["log"][-200:]

    def start(self, kind, packages=None):
        with self.lock:
            if self.state["status"] == "running":
                return False
            self.reset()
            self.state.update({
                "kind": kind,
                "status": "running",
                "packages": packages or [],
                "started": datetime.now().isoformat(timespec="seconds"),
            })
        threading.Thread(target=self._run, args=(kind, packages or []), daemon=True).start()
        return True

    def _run_subprocess(self, cmd, packages):
        """flatpak / snap 更新：直接跑 CLI，逐行把輸出寫進 log。授權由 polkit 處理
        （flatpak app-update 對 active session 免密碼；snap refresh 會跳密碼視窗）。"""
        self._log("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_ENV_C, bufsize=1)
            with self.lock:
                self.state["progress"] = None
                self.state["status_text"] = msg('running', LANG_DEFAULT)
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.strip():
                    self._log(line)
                    with self.lock:
                        self.state["details"] = line.strip()[:120]
            rc = proc.wait(timeout=1800)
            with self.lock:
                self.state["exit"] = f"rc={rc}"
                self.state["status"] = "done" if rc == 0 else "error"
                if rc != 0:
                    hint = {126: msg('auth_cancelled', LANG_DEFAULT), 127: msg('command_missing', LANG_DEFAULT),
                            10: msg('snap_busy', LANG_DEFAULT)}.get(rc)
                    self.state["error"] = (hint or msg('exit_code', LANG_DEFAULT, p0=rc)) + msg('see_log', LANG_DEFAULT)
                    if self.state.get("kind") == "rollback" and rc not in (126, 127):
                        # 降版中途失敗可能留下 unpacked／half-configured 的套件。dpkg --audit 不需 root，查得到就明講該怎麼救；
                        # 修復本身要再一次密碼，這裡不自動做，交給使用者在終端機跑。
                        audit = (_run(["dpkg", "--audit"], timeout=30) or "").strip()
                        self.state["error"] += msg('rollback_left_broken' if audit else 'rollback_state_intact', LANG_DEFAULT)
                self.state["status_text"] = msg('done', LANG_DEFAULT) if rc == 0 else msg('failed', LANG_DEFAULT)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(e)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
            self._log("EXCEPTION " + str(e))
        finally:
            _APPS_CACHE["ts"] = 0  # 讓應用程式清單重新掃描
            _FW["ts"] = 0
            _DASH_CACHE["ts"] = 0
            _NPM["ts"] = 0
            _NODE_FLOW["ts"] = 0

    def _run_ollama_pull(self, model):
        req = urllib.request.Request(OLLAMA + "/api/pull", data=json.dumps({"name": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
        self._log(f"ollama pull {model}")
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                last = ""
                for line in r:
                    try:
                        j = json.loads(line.decode())
                    except ValueError:
                        continue
                    st = j.get("status", "")
                    tot, done = j.get("total"), j.get("completed")
                    with self.lock:
                        self.state["status_text"] = st
                        self.state["progress"] = int(done / tot * 100) if tot and done is not None else None
                        self.state["details"] = f"{st} {GB_(done)}/{GB_(tot)}" if tot else st
                    if st != last:
                        self._log(st); last = st
                    if j.get("error"):
                        raise RuntimeError(j["error"])
            with self.lock:
                self.state.update(status="done", exit="ok", status_text=msg('done', LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            with self.lock:
                self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
            self._log("ERROR " + str(e))
        finally:
            _DISK["ts"] = 0

    def _snap_rc_hint(self, rc):
        last = " ".join(self.state.get("log", [])[-3:])
        if "has running apps" in last:
            return msg('snap_running', LANG_DEFAULT)
        return {10: msg('snap_busy', LANG_DEFAULT),
                126: msg('auth_cancelled', LANG_DEFAULT), 127: msg('command_missing', LANG_DEFAULT)}.get(rc, msg('exit_code', LANG_DEFAULT, p0=rc))

    def _run_snap(self, names):
        """snap 更新。三個在真機上踩到的坑：
        1) snap CLI 不會自己觸發 snapd 的 polkit，非 root 直接回 access denied → 用 pkexec。
        2) `snap refresh` 的進度是 \r 覆寫的單行，逐行讀會看起來卡住 → 改用 `snap tasks` 讀真進度。
        3) 工作狀態只存在記憶體，服務一重啟就失去追蹤，而 snapd 那邊還在跑 → 先找進行中的變更並接上。"""
        cid = snap_change_for(names)
        proc = None
        if cid:
            self._log(msg('snap_attach', LANG_DEFAULT, p0=cid))
        else:
            self._log(msg('snap_auth', LANG_DEFAULT))
            cmd = ["pkexec", "/usr/bin/snap", "refresh"] + names
            self._log("$ " + " ".join(cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, env=_ENV_C, bufsize=1)
            except OSError as e:
                with self.lock:
                    self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
                return
            def drain():
                for line in proc.stdout:
                    t = line.rstrip("\r\n").strip()
                    if t and not re.fullmatch(r"[\s\u2588\u2591\-\\|/]*", t):
                        self._log(t)
            threading.Thread(target=drain, daemon=True).start()
        try:
            while True:
                if cid is None and proc is not None:
                    cid = snap_change_for(names)
                if cid:
                    pr = snap_change_progress(cid)
                    with self.lock:
                        self.state["progress"] = pr["percent"]
                        self.state["status_text"] = pr["doing"] or pr["status"]
                        mb = lambda b: f"{b / 1e6:.1f} MB"   # snapd 自己用 10^6，跟著一致
                        size = (f" · {mb(pr['bytes_done'])} / {mb(pr['bytes_total'])}"
                                if pr.get("bytes_total") else "")
                        step = min(pr["done"] + 1, pr["total"]) if pr["total"] else pr["done"]
                        self.state["details"] = (msg('snap_step', LANG_DEFAULT, p0=step, p1=pr['total']) + size
                                                 + (msg('snap_step_percent', LANG_DEFAULT, p0=pr['task_percent']) if pr["task_percent"] is not None else "")
                                                 + (msg('snap_overall', LANG_DEFAULT, p0=pr['task_ratio']) if pr["task_ratio"] is not None else ""))
                    if pr["status"] in ("Done", "Error", "Undone", "Hold"):
                        ok = pr["status"] == "Done"
                        reason = (pr.get("err") or "").strip()
                        if not ok:
                            for l in pr.get("fail_log") or []:
                                self._log(l[:300])
                            blob = (reason + " " + " ".join(pr.get("fail_log") or [])).lower()
                            if any(k in blob for k in ("unexpected eof", "connection", "timeout", "temporary failure", "i/o timeout")):
                                reason = (reason or msg('download_interrupted', LANG_DEFAULT)) + msg('network_retry', LANG_DEFAULT)
                        with self.lock:
                            self.state.update(status="done" if ok else "error", exit=pr["status"],
                                              error=None if ok else (reason or msg('snap_ended', LANG_DEFAULT, p0=cid, p1=pr['status'])),
                                              status_text=msg('done', LANG_DEFAULT) if ok else msg('failed', LANG_DEFAULT),
                                              finished=datetime.now().isoformat(timespec="seconds"))
                        self._log(msg('snap_status', LANG_DEFAULT, p0=cid, p1=pr['status']))
                        return
                if proc is not None and proc.poll() is not None and cid is None:
                    rc = proc.returncode
                    with self.lock:
                        self.state.update(status="done" if rc == 0 else "error", exit=f"rc={rc}",
                                          error=None if rc == 0 else (self._snap_rc_hint(rc) + msg('see_log', LANG_DEFAULT)),
                                          status_text=msg('done', LANG_DEFAULT) if rc == 0 else msg('failed', LANG_DEFAULT),
                                          finished=datetime.now().isoformat(timespec="seconds"))
                    return
                time.sleep(2)
        except Exception as e:
            with self.lock:
                self.state.update(status="error", error=str(e), finished=datetime.now().isoformat(timespec="seconds"))
        finally:
            _APPS_CACHE["ts"] = 0

    def _run(self, kind, packages):
        if kind == "ollama_pull":
            return self._run_ollama_pull(packages[0])
        if kind == "shell":
            # packages[0] 是指令名（見 DISK_ACTIONS 白名單），不接受任意指令
            cmd = DISK_ACTIONS.get(packages[0]) if packages else None
            if not cmd:
                with self.lock:
                    self.state.update(status="error", error=msg('unknown_action', LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            return self._run_subprocess(cmd, packages)
        if kind == "flatpak":
            return self._run_subprocess(["flatpak", "update", "-y", "--noninteractive"] + packages, packages)
        if kind == "snap":
            return self._run_snap(packages)
        if kind == "install":   # 升級前先把舊版留下來；失敗不影響升級
            try:
                with self.lock:
                    self.state["status_text"] = msg("rollback_preparing", LANG_DEFAULT, name=", ".join(packages), old="")
                rollback_prepare(packages, self._log, lambda txt: self.state.__setitem__("status_text", txt))
            except Exception as e:
                self._log("rollback prepare failed: " + str(e))
        if kind == 'node_source':
            action, target, token = packages
            if not node_helper_ready():
                with self.lock:
                    self.state.update(status='error', error=msg('node_helper_unavailable', LANG_DEFAULT), finished=datetime.now().isoformat(timespec='seconds'))
                return
            # 直接執行 helper（shebang 是 python3 -I）而不是 pkexec python3：polkit 的 action 用 exec.path 對應程式路徑，
            # 這樣密碼視窗才會顯示 policy 檔裡「Spark Center 要切換 NodeSource 倉庫…」，而不是「要以 root 執行 python3」。
            self._run_subprocess(['pkexec', NODE_HELPER, action, target, token], packages)
            with self.lock:
                if self.state['status'] == 'error':
                    detail = next((s.split('NODE_ERROR:', 1)[1] for s in reversed(self.state['log']) if 'NODE_ERROR:' in s), None)
                    if detail:
                        self.state['error'] = node_error(detail)
            return
        if kind == "npm":
            try:
                rollback_record_npm(packages)
            except Exception as e:
                self._log("rollback record failed: " + str(e))
            st = npm_status()
            # outdated（升級）與 newer（裝回相容版）都是經過 engines 核對的明確版本；blocked 的不在裡面
            want = {e["name"]: e.get("latest") for e in st.get("packages", []) if (e.get("outdated") or e.get("newer")) and not e.get("blocked")}
            missing = [n for n in packages if not want.get(n)]
            if st.get("error") or missing:
                # 目標版本查不到就停：退回 @latest 會裝到沒確認過、可能不相容的版本
                with self.lock:
                    self.state.update(status="error", error=msg("npm_target_unknown", LANG_DEFAULT, pkgs=", ".join(missing or packages), err=st.get("error") or ""),
                                      finished=datetime.now().isoformat(timespec="seconds"))
                return
            # --engine-strict：npm 對 engines 不符預設只印警告照裝，這裡要它直接失敗
            return self._run_subprocess([NPM_BIN, "install", "-g", "--engine-strict"] + [f"{n}@{want[n]}" for n in packages], packages)
        if kind == "rollback_npm":
            ents = rollback_npm_entries(packages[0], packages[1] or None)
            if not ents:
                with self.lock:
                    self.state.update(status="error", error=msg("rollback_not_found", LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            return self._run_subprocess([NPM_BIN, "install", "-g"] + [f"{e['name']}@{e['old']}" for e in ents], [e["name"] for e in ents])
        if kind == "rollback":
            # 為什麼不用 aptdaemon 的 install_file：它最後跑 DebPackage.check()，預設拒絕比已安裝舊的版本
            # （"A later version is already installed"），force=True 也一樣。apt-get 對本機 .deb 會照給的版本裝，
            # 加 --allow-downgrades 明講意圖，相依由 apt 解；一次給整組檔案，舊主程式要求舊函式庫時才有完整退路。
            paths = [fp for fp, _ in rollback_deb_paths(packages[0], packages[1] or None)]
            if not paths:
                with self.lock:
                    self.state.update(status="error", error=msg("rollback_not_found", LANG_DEFAULT), finished=datetime.now().isoformat(timespec="seconds"))
                return
            self._log(msg("rollback_auth", LANG_DEFAULT))
            return self._run_subprocess(["pkexec"] + ROLLBACK_APT + paths, packages)
        loop = GLib.MainLoop()
        try:
            client = aptdaemon.client.AptClient()
            if kind == "refresh":
                trans = client.update_cache()
            elif kind == "aptclean":
                trans = client.clean()                 # polkit org.debian.apt.clean：active session 免密碼
            elif kind == "remove":
                trans = client.remove_packages(packages)  # autoremove 用；會跳 polkit 密碼視窗
            else:
                trans = client.upgrade_packages(packages)

            def on_status(t, status):
                text = aenums.get_status_string_from_enum(status)
                with self.lock:
                    self.state["status_text"] = text
                self._log(text)

            def on_details(t, d):
                with self.lock:
                    self.state["details"] = d

            def on_progress(t, p):
                # aptdaemon 用 101 代表「進度未知」，不要當成百分比顯示
                with self.lock:
                    self.state["progress"] = int(p) if p <= 100 else None

            def on_progress_details(t, items, items_total, done, total, speed, eta):
                # aptdaemon 的百分比不是照位元組線性走（下載大約只占前半），幾百 MB 只看百分比會像卡住；
                # 這裡把位元組與速率直接端出來。下載結束後 total 會歸 0，就清掉。
                with self.lock:
                    self.state["xfer"] = ({"done": int(done), "total": int(total), "speed": int(speed), "eta": int(eta),
                                           "items": int(items), "items_total": int(items_total)} if total > 0 else None)

            def on_error(t, code, details):
                msg = f"{aenums.get_error_string_from_enum(code)}: {details}"
                with self.lock:
                    self.state["error"] = msg
                self._log("ERROR " + msg)

            def on_conffile(t, old, new):
                # 設定檔衝突一律保留現有版本，不互動、不猜。
                self._log(msg('config_conflict', LANG_DEFAULT, p0=old))
                t.resolve_config_file_conflict(old, "keep")

            def on_finished(t, exit_state):
                with self.lock:
                    self.state["exit"] = exit_state
                    self.state["status"] = (
                        "done" if exit_state == aenums.EXIT_SUCCESS else "error"
                    )
                    if self.state["status"] == "error" and not self.state["error"]:
                        self.state["error"] = msg('transaction_exit', LANG_DEFAULT, p0=exit_state)
                    self.state["finished"] = datetime.now().isoformat(timespec="seconds")
                self._log(f"finished: {exit_state}")
                _APPS_CACHE["ts"] = 0
                _FW["ts"] = 0
                _DASH_CACHE["ts"] = 0
                _NPM["ts"] = 0   # nodejs 從這條路裝過，Node 版本會變
                _NODE_FLOW["ts"] = 0
                loop.quit()

            trans.connect("status-changed", on_status)
            trans.connect("status-details-changed", on_details)
            trans.connect("progress-changed", on_progress)
            trans.connect("progress-details-changed", on_progress_details)
            trans.connect("error", on_error)
            trans.connect("config-file-conflict", on_conffile)
            trans.connect("finished", on_finished)
            self._log(msg('job_start', LANG_DEFAULT, p0=kind, p1=' '.join(packages) if packages else ''))
            trans.run()
            loop.run()
        except Exception as e:  # D-Bus 拒絕、polkit 取消等
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(e)
                self.state["finished"] = datetime.now().isoformat(timespec="seconds")
            self._log("EXCEPTION " + str(e))


JOB = Job()


# ---------- 應用程式清單（apt / snap / flatpak）----------

DESKTOP_DIRS_APT = ("/usr/share/applications",)
SNAP_DESKTOP_DIR = "/var/lib/snapd/desktop/applications"
FLATPAK_APPSTREAM_GLOB = (
    "/var/lib/flatpak/appstream/*/*/active/appstream.xml.gz",
    os.path.expanduser("~/.local/share/flatpak/appstream/*/*/active/appstream.xml.gz"),
)
_ENV_C = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")


def _run(cmd, timeout=20):
    """跑外部指令，失敗或逾時回 None（呼叫端要把「拿不到」當一級狀態處理）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV_C)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_desktop(path):
    """只讀 [Desktop Entry] 主段落。回 None 表示不是要顯示的應用程式。"""
    d = {"name": "", "name_zh": "", "comment": "", "icon": "", "categories": ""}
    nodisplay = hidden = False
    typ = "Application"
    try:
        with open(path, errors="replace") as f:
            in_main = False
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    if in_main:
                        break
                    in_main = line == "[Desktop Entry]"
                    continue
                if not in_main or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k == "Name":
                    d["name"] = v
                elif k in ("Name[zh_TW]", "Name[zh_Hant]"):
                    d["name_zh"] = v
                elif k == "Comment":
                    d["comment"] = v
                elif k == "Icon":
                    d["icon"] = v
                elif k == "Categories":
                    d["categories"] = v
                elif k == "NoDisplay":
                    nodisplay = v.lower() == "true"
                elif k == "Hidden":
                    hidden = v.lower() == "true"
                elif k == "Type":
                    typ = v
    except OSError:
        return None
    if nodisplay or hidden or typ != "Application" or not d["name"]:
        return None
    return d


# ---------- 應用程式圖示 ----------
# .desktop 的 Icon= 是主題名稱或絕對路徑。主題名稱依 freedesktop 規則在圖示主題目錄找；這裡只索引 apps 類與 pixmaps，
# 一次約 0.1 秒，快取 1 小時。前端拿到的是 /appicon/<token>，token 只對應索引裡的路徑，不能指定任意檔案。
ICON_THEMES = ["Yaru-nvidia", "Yaru", "hicolor", "Adwaita", "Humanity"]
ICON_ROOTS = [f"/usr/share/icons/{t}" for t in ICON_THEMES] + [
    os.path.expanduser("~/.local/share/icons/hicolor"), "/var/lib/flatpak/exports/share/icons/hicolor", "/usr/share/pixmaps"]
ICON_SIZE_PREF = {"64x64": 0, "48x48": 1, "128x128": 2, "scalable": 3, "256x256": 4, "96x96": 5, "32x32": 6, "512x512": 7, "24x24": 8, "16x16": 9}
_ICON_INDEX = {"ts": 0, "idx": {}}
_ICON_TOKENS = {}   # token → 檔案路徑（只放解析出來的）


def _icon_index():
    if time.time() - _ICON_INDEX["ts"] < 3600 and _ICON_INDEX["idx"]:
        return _ICON_INDEX["idx"]
    idx = {}
    for ri, root in enumerate(ICON_ROOTS):
        for dp, _, fns in os.walk(root):
            parts = dp[len(root):].split("/")
            size = next((x for x in parts if x in ICON_SIZE_PREF), None)
            if root != "/usr/share/pixmaps" and size is None:
                continue
            for f in fns:
                base, ext = os.path.splitext(f)
                if ext not in (".png", ".svg"):
                    continue
                key = (0 if "apps" in parts or root == "/usr/share/pixmaps" else 1, ri, ICON_SIZE_PREF.get(size, 5), 0 if ext == ".png" else 1)
                if base not in idx or key < idx[base][0]:
                    idx[base] = (key, os.path.join(dp, f))
    _ICON_INDEX.update(ts=time.time(), idx=idx)
    return idx


def icon_url(name):
    """Icon= 值 → /appicon/<token>；找不到回 None（前端畫預設方塊，不假裝有圖）。"""
    if not name:
        return None
    path = None
    if name.startswith("/"):
        if os.path.isfile(name) and os.path.splitext(name)[1] in (".png", ".svg"):
            path = name
    else:
        hit = _icon_index().get(name) or _icon_index().get(os.path.splitext(name)[0])
        path = hit[1] if hit else None
    if not path:
        return None
    token = hashlib.sha1(path.encode()).hexdigest()[:16]
    _ICON_TOKENS[token] = path
    return f"/appicon/{token}"


def _apt_desktop_owner_map():
    """dpkg 檔案清單 → {套件名: [desktop 路徑]}。純本機檔案，約 50ms。"""
    m = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5].split(":")[0]
        try:
            with open(lst, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(DESKTOP_DIRS_APT) and line.endswith(".desktop"):
                        m.setdefault(pkg, []).append(line)
        except OSError:
            continue
    return m


def apps_apt(cache):
    out = []
    for pkg, paths in _apt_desktop_owner_map().items():
        if pkg not in cache or not cache[pkg].installed:
            continue
        entry = None
        for p in sorted(paths):
            entry = parse_desktop(p)
            if entry:
                break
        if not entry:
            continue
        ap = cache[pkg]
        try:   # dpkg 檔案清單的 mtime ＝ 最後一次安裝／升級時間；沒有就不填
            installed_at = datetime.fromtimestamp(os.path.getmtime(f"/var/lib/dpkg/info/{pkg}.list")).isoformat(timespec="minutes")
        except OSError:
            installed_at = None
        out.append({
            "source": "apt", "id": pkg,
            "name": entry["name"], "name_zh": entry["name_zh"], "comment": entry["comment"],
            "icon": entry["icon"], "icon_url": icon_url(entry["icon"]), "categories": entry["categories"],
            "size": ap.installed.installed_size or None, "installed_at": installed_at,
            "version": ap.installed.version,
            "candidate": ap.candidate.version if ap.is_upgradable else None,
            "origin": _group_name(_origin_of(ap.candidate or ap.installed)),
            "changelog": "apt",
        })
    return out


def apps_snap(check_updates):
    txt = _run(["snap", "list"])
    if txt is None:
        return None
    names = {}
    for p in glob.glob(os.path.join(SNAP_DESKTOP_DIR, "*.desktop")):
        snap = os.path.basename(p).split("_", 1)[0]
        e = parse_desktop(p)
        if e and snap not in names:
            names[snap] = e
    updates = {}
    if check_updates:
        u = _run(["snap", "refresh", "--list"], timeout=30)
        if u is not None and not u.startswith("All snaps"):
            for line in u.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    updates[parts[0]] = parts[1]
    meta = {}
    try:   # snapd 的 REST API（socket 0666，不需 root）有安裝大小與日期；拿不到就留空
        for sn in _snapd_get("/v2/snaps") or []:
            meta[sn["name"]] = sn
    except Exception:
        pass
    out = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        name, ver, rev, tracking, publisher = parts[:5]
        m = meta.get(name, {})
        inst_at = (m.get("install-date") or "")[:16] or None
        notes = parts[5] if len(parts) > 5 else ""
        if "base" in notes or name.startswith("core") or name in ("bare", "snapd"):
            continue
        if "disabled" in notes:
            continue
        e = names.get(name, {})
        out.append({
            "source": "snap", "id": name,
            "name": e.get("name") or name, "name_zh": e.get("name_zh", ""), "comment": e.get("comment", ""),
            "icon": e.get("icon", ""), "icon_url": icon_url(e.get("icon", "")) or (f"/snapicon/{name}" if m.get("icon") else None), "categories": e.get("categories", ""),
            "size": m.get("installed-size"), "installed_at": inst_at,
            "version": ver, "candidate": updates.get(name),
            "origin": f"snap · {publisher.rstrip('*')} · {tracking}",
            "changelog": "snap",
            "checked_updates": check_updates and u is not None,
        })
    return out


def apps_flatpak(check_updates):
    txt = _run(["flatpak", "list", "--app", "--columns=application,name,version,origin,size,installation"])
    if txt is None:
        return None
    updates = {}
    checked = False
    if check_updates:
        u = _run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version,commit"], timeout=30)
        if u is not None:
            checked = True
            for line in u.splitlines():
                parts = line.split("\t")
                if parts and parts[0]:
                    ver = parts[1] if len(parts) > 1 and parts[1] else ""
                    commit = parts[2][:12] if len(parts) > 2 else ""
                    # flathub 常不帶版本號，只有 commit；照實標示，不編版本
                    updates[parts[0]] = ver or (msg('new_commit', LANG_DEFAULT, p0=commit) if commit else msg('new_version_no_number', LANG_DEFAULT))
    out = []
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        app, name, ver, origin = parts[:4]
        size_txt = parts[4] if len(parts) > 4 else ""
        inst = parts[5] if len(parts) > 5 else "system"
        size = _parse_size(size_txt)
        base = "/var/lib/flatpak/app" if inst == "system" else os.path.expanduser("~/.local/share/flatpak/app")
        try:
            inst_at = datetime.fromtimestamp(os.path.getmtime(os.path.join(base, app, "current", "active"))).isoformat(timespec="minutes")
        except OSError:
            inst_at = None
        out.append({
            "source": "flatpak", "id": app,
            "name": name or app, "name_zh": "", "comment": "", "icon": app, "icon_url": icon_url(app), "categories": "",
            "size": size, "installed_at": inst_at,
            "version": ver, "candidate": updates.get(app),
            "origin": f"flatpak · {origin}",
            "remote": origin,
            "changelog": "flatpak",
            "checked_updates": checked,
        })
    return out


def _parse_size(txt):
    """flatpak 印的 '83.6 MB'／'1.2 GB'（10 進位）→ bytes；解析不了回 None。"""
    m = re.match(r"([\d.]+)\s*([kMG]?B)", txt or "")
    if not m:
        return None
    return int(float(m.group(1)) * {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}[m.group(2)])


_APPS_CACHE = {"ts": 0, "data": None, "lock": threading.Lock()}
APPS_TTL = 300


def list_apps(force=False):
    with _APPS_CACHE["lock"]:
        if not force and _APPS_CACHE["data"] and time.time() - _APPS_CACHE["ts"] < APPS_TTL:
            return _APPS_CACHE["data"]
        cache = apt.Cache()
        apt_apps = apps_apt(cache)
        snap_apps = apps_snap(check_updates=True)
        fp_apps = apps_flatpak(check_updates=True)
        items = apt_apps + (snap_apps or []) + (fp_apps or [])
        items.sort(key=lambda a: (a["name_zh"] or a["name"]).lower())
        data = {
            "items": items,
            "sources": {
                "apt": {"ok": True, "count": len(apt_apps)},
                "snap": {"ok": snap_apps is not None, "count": len(snap_apps or [])},
                "flatpak": {"ok": fp_apps is not None, "count": len(fp_apps or [])},
            },
            "generated": datetime.now().isoformat(timespec="seconds"),
        }
        _APPS_CACHE.update(ts=time.time(), data=data)
        return data


# ---------- 更新說明 ----------

_CHANGELOG_CACHE = {}


# 第三方 repo 不提供 apt changelog 時，指向廠商自己的發行說明頁（比只說「沒有」有用）
VENDOR_RELEASE_NOTES = {
    "google-chrome-stable": "https://chromereleases.googleblog.com/search/label/Stable%20updates",
    "code": "https://code.visualstudio.com/updates",
    "brave-browser": "https://brave.com/latest/",
    "tailscale": "https://tailscale.com/changelog",
    "chatgpt": "https://help.openai.com/en/articles/10119604-work-with-apps-on-macos-and-windows",
    "docker-ce": "https://docs.docker.com/engine/release-notes/",
    "docker-ce-cli": "https://docs.docker.com/engine/release-notes/",
    "nodejs": "https://github.com/nodejs/node/releases",
    "gh": "https://github.com/cli/cli/releases",
}


def _apt_vendor_hint(pkg):
    """沒有 changelog 時給使用者一條去處：廠商發行說明頁，沒有的話退回套件自己宣告的官網與來源網域。"""
    url = VENDOR_RELEASE_NOTES.get(pkg)
    home = site = None
    try:
        cache = apt.Cache()
        if pkg in cache and cache[pkg].candidate:
            v = cache[pkg].candidate
            home = v.homepage or None
            site = v.origins[0].site if v.origins else None
    except Exception:
        pass
    if url:
        return msg('vendor_notes', LANG_DEFAULT, p0=url)
    if home:
        return msg('changelog_home', LANG_DEFAULT, p0=home)
    if site:
        return msg('changelog_no_home', LANG_DEFAULT, p0=site)
    return ""


def changelog_apt(pkg, installed_version=""):
    """用 `apt-get changelog`（不需 root）抓候選版本的 changelog，截到已安裝版本為止。
    第三方 repo 多半沒提供，照實回報。python-apt 的 get_changelog 在這台機器上抓不到，所以走子程序。"""
    try:
        r = subprocess.run(["apt-get", "-q", "changelog", pkg], capture_output=True, text=True, timeout=25, env=_ENV_C)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "note": msg('changelog_timeout', LANG_DEFAULT)}
    except OSError as e:
        return {"ok": False, "text": "", "note": msg('apt_get_failed', LANG_DEFAULT, p0=e)}
    if r.returncode != 0:
        hint = _apt_vendor_hint(pkg)
        return {"ok": False, "text": "", "note": msg('no_changelog', LANG_DEFAULT) + (hint or "")}
    head_re = re.compile(r"^(\S+) \(([^)]+)\)")
    kept, seen_any, truncated = [], False, False
    for line in r.stdout.splitlines():
        if line.startswith(("Get:", "Fetched", "Hit:")):
            continue
        m = head_re.match(line)
        if m:
            seen_any = True
            if installed_version and m.group(2) == installed_version:
                truncated = True
                break
        kept.append(line)
    if not seen_any:
        hint = _apt_vendor_hint(pkg)
        return {"ok": False, "text": "", "note": msg('no_changelog', LANG_DEFAULT) + (hint or "")}
    text = "\n".join(kept).strip()
    if truncated and not text:
        return {"ok": True, "text": "", "note": msg('changelog_current', LANG_DEFAULT)}
    if len(text) > 20000:
        text = text[:20000] + msg('truncated', LANG_DEFAULT)
    note = msg('changelog_source', LANG_DEFAULT) + (msg('changelog_since', LANG_DEFAULT) if truncated else msg('changelog_all', LANG_DEFAULT))
    return {"ok": True, "text": text, "note": note}


def changelog_flatpak(app_id, installed_version):
    """從本機 appstream 目錄讀 <releases>，回最新幾筆到已安裝版本為止。"""
    for pattern in FLATPAK_APPSTREAM_GLOB:
        for path in glob.glob(pattern):
            try:
                with gzip.open(path) as f:
                    root = ET.parse(f).getroot()
            except (OSError, ET.ParseError):
                continue
            for comp in root.iter("component"):
                if (comp.findtext("id") or "").removesuffix(".desktop") != app_id:
                    continue
                rels = comp.find("releases")
                if rels is None:
                    return {"ok": False, "text": "", "note": msg('appstream_no_releases', LANG_DEFAULT)}
                lines = []
                for r in list(rels)[:8]:
                    v = r.get("version", "?")
                    d = r.get("date", "")[:10]
                    desc = " ".join(t.strip() for t in r.itertext() if t.strip())
                    lines.append(f"{v}  {d}\n  {desc or msg('no_description', LANG_DEFAULT)}")
                    if v == installed_version:
                        break
                return {"ok": True, "text": "\n\n".join(lines), "note": msg('appstream_source', LANG_DEFAULT, p0=os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))))}
    # 本機沒有 appstream（flatpak 未同步 appstream 時就是這樣）→ 退回 remote-info 的提交訊息
    remote = ""
    data = _APPS_CACHE.get("data") or {}
    for a in data.get("items", []):
        if a.get("source") == "flatpak" and a.get("id") == app_id:
            remote = a.get("remote", "")
    if remote:
        txt = _run(["flatpak", "remote-info", remote, app_id], timeout=25)
        if txt:
            info = {}
            for line in txt.splitlines():
                k, _, v = line.strip().partition(":")
                if k in ("Version", "Commit", "Subject", "Date"):
                    info[k] = v.strip()
            body = "\n".join(f"{k}: {info[k]}" for k in ("Version", "Date", "Subject", "Commit") if k in info)
            return {"ok": True, "text": body,
                    "note": msg('remote_no_notes', LANG_DEFAULT, p0=remote)}
    return {"ok": False, "text": "", "note": msg('appstream_unavailable', LANG_DEFAULT)}


def changelog_snap(name, installed_version=""):
    """Snap 商店沒有逐版 changelog，但 `snap info` 給得出發行者、各頻道的版本／發布日期／版次／大小，
    以及目前安裝的版次。照實說明這不是 changelog。需要連商店，離線會失敗。"""
    out = _run(["snap", "info", name], timeout=30)
    if out is None:
        return {"ok": False, "text": "", "note": msg('snap_info_failed', LANG_DEFAULT, p0=name)}
    g = lambda k: (lambda m: m.group(1).strip() if m else None)(re.search(rf"^{re.escape(k)}:\s*(.+)$", out, re.M))
    summary, publisher = g("summary"), g("publisher")
    store_url, tracking, refresh = g("store-url"), g("tracking"), g("refresh-date")
    rows, in_ch = [], False
    for line in out.splitlines():
        if line.startswith("channels:"):
            in_ch = True
            continue
        if line.startswith("installed:") or in_ch:
            m = re.match(r"^\s*(\S+?):\s+(\S+)\s+(?:(\d{4}-\d{2}-\d{2})\s+)?\((\d+)\)\s+(\S+)", line)
            if m:
                rows.append({"channel": m.group(1), "version": m.group(2), "date": m.group(3) or "",
                             "rev": m.group(4), "size": m.group(5)})
                continue
            if in_ch and line.strip() and not line.startswith(" ") and not line.startswith("installed:"):
                in_ch = False
    if not rows:
        return {"ok": False, "text": "", "note": msg('snap_no_channels', LANG_DEFAULT)}
    inst = next((r for r in rows if r["channel"] == "installed"), None)
    # 回結構化表格，不用空格排版：等寬字型下 CJK 不保證是英數的兩倍寬，靠字元數對齊一定會歪
    table_rows = []
    for r in rows:
        table_rows.append({
            "channel": msg('installed', LANG_DEFAULT) if r["channel"] == "installed" else r["channel"],
            "version": r["version"], "date": r["date"] or "—", "rev": r["rev"], "size": r["size"],
            "installed": r["channel"] == "installed",
            "current": bool(inst and r["channel"] != "installed" and r["rev"] == inst["rev"]),
        })
    meta = " · ".join(x for x in [msg('publisher', LANG_DEFAULT, p0=publisher) if publisher else None,
                                  msg('tracking', LANG_DEFAULT, p0=tracking) if tracking else None,
                                  msg('last_update', LANG_DEFAULT, p0=refresh) if refresh else None] if x)
    return {"ok": True, "text": "", "summary": summary, "meta": meta, "store_url": store_url,
            "table": {"columns": [msg('channel', LANG_DEFAULT), msg('version', LANG_DEFAULT), msg('release_date', LANG_DEFAULT), msg('revision', LANG_DEFAULT), msg('size', LANG_DEFAULT)], "rows": table_rows},
            "note": msg('snap_changelog_note', LANG_DEFAULT)}


def get_changelog(source, ident, installed_version=""):
    key = (source, ident, installed_version)
    if key in _CHANGELOG_CACHE:
        return _CHANGELOG_CACHE[key]
    if source == "apt":
        res = changelog_apt(ident, installed_version)
    elif source == "flatpak":
        res = changelog_flatpak(ident, installed_version)
    elif source == "snap":
        res = changelog_snap(ident, installed_version)
    else:
        res = {"ok": False, "text": "", "note": msg('unknown_source', LANG_DEFAULT)}
    if res["ok"]:
        _CHANGELOG_CACHE[key] = res
    return res


# ---------- 硬體資訊（全部免 root；拿不到的欄位回 None，前端顯示「—」）----------

DMI_DIR = "/sys/devices/virtual/dmi/id"
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "board_vendor", "board_name",
              "bios_vendor", "bios_version", "bios_date")


def _read(path, default=None):
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _meminfo():
    d = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = int(v.strip().split()[0]) * 1024 if v.strip() else 0
    return d


def _os_release():
    d = {}
    for line in (_read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        d[k] = v.strip('"')
    return d


def _dpkg_version(pkg):
    out = _run(["dpkg-query", "-W", "-f=${Version}", pkg], timeout=5)
    return out.strip() if out else None


def _lscpu():
    out = _run(["lscpu", "-J"], timeout=10)
    if not out:
        return None
    try:
        rows = json.loads(out)["lscpu"]
    except (ValueError, KeyError):
        return None
    flat = []
    def walk(items):
        for it in items:
            flat.append((it.get("field", "").rstrip(":"), it.get("data", "")))
            if it.get("children"):
                walk(it["children"])
    walk(rows)
    get = lambda k: next((v for f, v in flat if f == k), None)
    # big.LITTLE 會有多個 Model name，各自帶核心數與最高時脈
    clusters, cur = [], None
    for f, v in flat:
        if f == "Model name":
            cur = {"model": v, "cores": None, "max_mhz": None}
            clusters.append(cur)
        elif cur and f == "Core(s) per socket" and cur["cores"] is None:
            cur["cores"] = v
        elif cur and f == "CPU max MHz" and cur["max_mhz"] is None:
            cur["max_mhz"] = v
    return {
        "architecture": get("Architecture"),
        "cpus": get("CPU(s)"),
        "vendor": get("Vendor ID"),
        "clusters": clusters,
        "l2": get("L2 cache"), "l3": get("L3 cache"),
    }


# ---------- NVML（偷師 DGX-Spark-Dashboard：用驅動函式庫直接讀，不每次開 nvidia-smi 子程序）----------
# 用 ctypes 開系統自帶的 libnvidia-ml.so，不需要 pip 套件。init 一次留著（每次 init/shutdown 約 9 ms，讀取本身 0.001 ms）。
# GB10 統一記憶體：記憶體資訊與功耗上限回 NOT_SUPPORTED（rc 3），和 nvidia-smi 印 N/A 一致，照實回 None。
import ctypes

_NVML = {"lib": None, "handle": None, "ok": False, "tried": False, "lock": threading.Lock()}


NVML_EVENT_REASONS = {0x1: msg('gpu_idle', LANG_DEFAULT), 0x2: msg('app_clocks', LANG_DEFAULT), 0x4: msg('sw_power_cap', LANG_DEFAULT), 0x8: msg('hw_slowdown', LANG_DEFAULT), 0x10: "Sync Boost",
                      0x20: msg('sw_thermal', LANG_DEFAULT), 0x40: msg('hw_thermal', LANG_DEFAULT), 0x80: msg('hw_power_brake', LANG_DEFAULT), 0x100: msg('display_clocks', LANG_DEFAULT)}
GPU_STUCK_BAD_REASONS = {msg('hw_slowdown', LANG_DEFAULT), msg('hw_power_brake', LANG_DEFAULT), msg('hw_thermal', LANG_DEFAULT)}


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _nvml():
    with _NVML["lock"]:
        if _NVML["tried"]:
            return _NVML if _NVML["ok"] else None
        _NVML["tried"] = True
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                return None
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return None
            _NVML.update(lib=lib, handle=h, ok=True)
            return _NVML
        except OSError:
            return None


def _nvml_uint(fn, *args):
    """呼叫回 unsigned int 的 NVML 函式；NOT_SUPPORTED 或任何錯誤回 None。"""
    n = _nvml()
    if not n:
        return None
    v = ctypes.c_uint()
    return v.value if getattr(n["lib"], fn)(n["handle"], *args, ctypes.byref(v)) == 0 else None


def _nvml_str(fn, on_device=True):
    n = _nvml()
    if not n:
        return None
    b = ctypes.create_string_buffer(96)
    rc = getattr(n["lib"], fn)(n["handle"], b, 96) if on_device else getattr(n["lib"], fn)(b, 96)
    return b.value.decode(errors="replace") if rc == 0 else None


def _gpu_static_nvml():
    n = _nvml()
    if not n:
        return None
    cuda = ctypes.c_int()
    cuda_v = None
    if n["lib"].nvmlSystemGetCudaDriverVersion_v2(ctypes.byref(cuda)) == 0:
        cuda_v = f"{cuda.value // 1000}.{(cuda.value % 1000) // 10}"
    pl = _nvml_uint("nvmlDeviceGetPowerManagementLimit")
    # 溫度門檻：1=SLOWDOWN（降頻）、0=SHUTDOWN、3=GPU_MAX。這是驅動給的真值，不是推算。
    slowdown = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 1)
    shutdown = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 0)
    gpu_max = _nvml_uint("nvmlDeviceGetTemperatureThreshold", 3)
    max_sm = _nvml_uint("nvmlDeviceGetMaxClockInfo", 1)
    return {
        "name": _nvml_str("nvmlDeviceGetName"), "driver": _nvml_str("nvmlSystemGetDriverVersion", on_device=False),
        "vbios": _nvml_str("nvmlDeviceGetVbiosVersion"), "bus": None,
        "max_sm_mhz": f"{max_sm} MHz" if max_sm else None, "memory_total": None, "cuda": cuda_v,
        "power_limit_w": pl / 1000 if pl else None,
        "throttle_temp_c": slowdown, "shutdown_temp_c": shutdown, "gpu_max_temp_c": gpu_max,
        "throttle_source": "NVML slowdown threshold" if slowdown else None,
        "unified_memory": True, "source": "NVML",
    }


def _gpu_live_nvml():
    n = _nvml()
    if not n:
        return None
    u = _NvmlUtil()
    util = u.gpu if n["lib"].nvmlDeviceGetUtilizationRates(n["handle"], ctypes.byref(u)) == 0 else None
    temp = _nvml_uint("nvmlDeviceGetTemperature", 0)
    power = _nvml_uint("nvmlDeviceGetPowerUsage")
    clk = _nvml_uint("nvmlDeviceGetClockInfo", 1)
    pstate = _nvml_uint("nvmlDeviceGetPerformanceState")
    mask = ctypes.c_ulonglong()
    reasons = None
    if n["lib"].nvmlDeviceGetCurrentClocksEventReasons(n["handle"], ctypes.byref(mask)) == 0:
        reasons = [name for bit, name in NVML_EVENT_REASONS.items() if mask.value & bit]
    return {"temp_c": temp, "util_pct": util, "power_w": power / 1000 if power is not None else None,
            "sm_mhz": clk, "memory_used": None, "source": "NVML",
            "pstate": f"P{pstate}" if pstate is not None else None, "event_reasons": reasons, "event_mask": mask.value if reasons is not None else None}


def _gpu_static():
    via = _gpu_static_nvml()
    if via:
        # PCI bus id 只有 nvidia-smi 好拿，補一次（靜態，快取 60 秒內只跑一次）
        out = _run(["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader"], timeout=10)
        via["bus"] = out.strip() if out else None
        return via
    return _gpu_static_smi()


def _gpu_live():
    return _gpu_live_nvml() or _gpu_live_smi()


def _gpu_static_smi():
    out = _run(["nvidia-smi", "--query-gpu=name,driver_version,vbios_version,pci.bus_id,clocks.max.sm,memory.total,power.limit,temperature.gpu.tlimit,temperature.gpu",
                "--format=csv,noheader"], timeout=10)
    if not out:
        return None
    parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    cuda = None
    head = _run(["nvidia-smi"], timeout=10) or ""
    m = re.search(r"CUDA Version:\s*([\d.]+)", head)
    if m:
        cuda = m.group(1)
    mem_total = None if parts[5].startswith("[") else parts[5]
    def watts(x):
        m2 = re.match(r"([\d.]+)", x)
        return float(m2.group(1)) if m2 else None
    return {"name": parts[0], "driver": parts[1], "vbios": parts[2], "bus": parts[3],
            "max_sm_mhz": parts[4], "memory_total": mem_total, "cuda": cuda,
            "power_limit_w": watts(parts[6]) if len(parts) > 6 else None,
            # tlimit 是「距離某個溫度上限還有幾度」，加當下溫度只是估計；NVML 可用時以它的 slowdown 門檻為準
            "throttle_temp_c": (watts(parts[7]) + watts(parts[8])) if len(parts) > 8 and watts(parts[7]) is not None and watts(parts[8]) is not None else None,
            "throttle_source": msg('tlimit_estimate', LANG_DEFAULT), "source": "nvidia-smi",
            "unified_memory": mem_total is None}


def _gpu_live_smi():
    out = _run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,power.draw,clocks.sm,memory.used",
                "--format=csv,noheader,nounits"], timeout=10)
    if not out:
        return None
    p = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    num = lambda x: None if x.startswith("[") or x == "" else float(x)
    return {"temp_c": num(p[0]), "util_pct": num(p[1]), "power_w": num(p[2]), "sm_mhz": num(p[3]), "memory_used": num(p[4]), "source": "nvidia-smi"}


def _disks():
    out = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,ROTA,TRAN,FSTYPE,MOUNTPOINTS"], timeout=10)
    if not out:
        return None
    try:
        devs = json.loads(out)["blockdevices"]
    except (ValueError, KeyError):
        return None
    disks = []
    for d in devs:
        if d.get("type") != "disk":
            continue
        parts = []
        for c in d.get("children") or []:
            mps = [m for m in (c.get("mountpoints") or []) if m]
            usage = None
            if mps:
                try:
                    st = os.statvfs(mps[0])
                    usage = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
                except OSError:
                    pass
            parts.append({"name": c["name"], "size": c.get("size"), "fstype": c.get("fstype"), "mountpoints": mps, "usage": usage})
        disks.append({"name": d["name"], "size": d.get("size"), "model": (d.get("model") or "").strip(),
                      "rotational": bool(d.get("rota")), "transport": d.get("tran"), "partitions": parts})
    return disks


def _network():
    out = _run(["ip", "-j", "addr"], timeout=10)
    if not out:
        return None
    try:
        ifs = json.loads(out)
    except ValueError:
        return None
    res = []
    for i in ifs:
        name = i.get("ifname", "")
        if name == "lo":
            continue
        speed = _read(f"/sys/class/net/{name}/speed")
        kind = "wifi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else \
               "virtual" if os.path.islink(f"/sys/class/net/{name}/device") is False and not os.path.exists(f"/sys/class/net/{name}/device") else "ethernet"
        res.append({
            "name": name, "state": i.get("operstate"), "mac": i.get("address"),
            "ipv4": [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet"],
            "ipv6": [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet6" and a.get("scope") == "global"],
            "speed_mbps": int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            "kind": kind, "mtu": i.get("mtu"),
        })
    return res


_SENS_CACHE = {"ts": 0, "data": []}


def _sensors():
    """hwmon 溫度。ACPI 溫度區每次讀約 48 ms，快取 5 秒；其他項目全部加起來不到 1 ms。"""
    if time.time() - _SENS_CACHE["ts"] < 5:
        return _SENS_CACHE["data"]
    res = _sensors_read()
    _SENS_CACHE.update(ts=time.time(), data=res)
    return res


def _sensors_read():
    res = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read(os.path.join(h, "name"), "?")
        for t in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            v = _read(t)
            if not v or not v.lstrip("-").isdigit():
                continue
            label = _read(t.replace("_input", "_label"), "") or os.path.basename(t).replace("_input", "")
            res.append({"chip": chip, "label": label, "temp_c": int(v) / 1000})
    return res


# ---------- USB 樹（/sys/bus/usb/devices，免 root；名稱缺的用 usb.ids 補）----------

USB_IDS = "/usr/share/misc/usb.ids"
_USB_IDS_CACHE = None
USB_CLASS = {
    "00": (msg('usb_interface', LANG_DEFAULT), ""), "01": (msg('usb_audio', LANG_DEFAULT), "audio"), "02": (msg('usb_communication', LANG_DEFAULT), "comm"), "03": (msg('usb_hid', LANG_DEFAULT), "hid"),
    "05": (msg('usb_physical', LANG_DEFAULT), "other"), "06": (msg('usb_image', LANG_DEFAULT), "image"), "07": (msg('usb_printer', LANG_DEFAULT), "printer"), "08": (msg('usb_storage', LANG_DEFAULT), "storage"),
    "09": (msg('usb_hub', LANG_DEFAULT), "hub"), "0a": (msg('usb_cdc', LANG_DEFAULT), "comm"), "0b": (msg('usb_card', LANG_DEFAULT), "other"), "0d": (msg('usb_security', LANG_DEFAULT), "other"),
    "0e": (msg('usb_video', LANG_DEFAULT), "video"), "0f": (msg('usb_health', LANG_DEFAULT), "other"), "10": (msg('usb_av', LANG_DEFAULT), "video"), "11": ("Billboard", "billboard"),
    "dc": (msg('usb_diagnostic', LANG_DEFAULT), "other"), "e0": (msg('usb_wireless', LANG_DEFAULT), "wireless"), "ef": (msg('usb_composite', LANG_DEFAULT), "other"), "fe": (msg('usb_application', LANG_DEFAULT), "other"), "ff": (msg('usb_vendor', LANG_DEFAULT), "vendor"),
}
USB_SPEED = {"1.5": msg('usb_low', LANG_DEFAULT), "12": msg('usb_full', LANG_DEFAULT), "480": msg('usb_high', LANG_DEFAULT),
             "5000": "USB 3.0 5 Gb/s", "10000": "USB 3.1 10 Gb/s", "20000": "USB 3.2 20 Gb/s"}


def _usb_ids():
    global _USB_IDS_CACHE
    if _USB_IDS_CACHE is not None:
        return _USB_IDS_CACHE
    vendors, products, cur = {}, {}, None
    try:
        with open(USB_IDS, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                if line[0] != "\t" and line[0] != " ":
                    if line[0] in "0123456789abcdef" and len(line) > 6 and line[4] == " ":
                        cur = line[:4].lower(); vendors[cur] = line[6:].strip()
                    else:
                        cur = None
                elif line.startswith("\t") and not line.startswith("\t\t") and cur:
                    products[(cur, line[1:5].lower())] = line[7:].strip()
    except OSError:
        pass
    _USB_IDS_CACHE = (vendors, products)
    return _USB_IDS_CACHE


def _usb_tree():
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return None
    vendors, products = _usb_ids()
    devs = {}
    for n in os.listdir(base):
        if ":" in n:
            continue
        d = os.path.join(base, n)
        vid, pid = (_read(os.path.join(d, "idVendor")) or "").lower(), (_read(os.path.join(d, "idProduct")) or "").lower()
        if not vid:
            continue
        ifaces = []
        for i in sorted(os.listdir(base)):
            if not i.startswith(n + ":"):
                continue
            ip = os.path.join(base, i)
            cls = (_read(os.path.join(ip, "bInterfaceClass")) or "").lower()
            sub = (_read(os.path.join(ip, "bInterfaceSubClass")) or "").lower()
            proto = (_read(os.path.join(ip, "bInterfaceProtocol")) or "").lower()
            drv = os.path.basename(os.readlink(os.path.join(ip, "driver"))) if os.path.islink(os.path.join(ip, "driver")) else None
            ifaces.append({"id": i.split(":", 1)[1], "class": cls, "sub": sub, "proto": proto, "driver": drv,
                           "label": USB_CLASS.get(cls, (cls, "other"))[0]})
        # 分類：以介面類別為主，HID 再依 protocol 分鍵盤/滑鼠
        kind, kind_label = "other", msg('device', LANG_DEFAULT)
        classes = [x["class"] for x in ifaces]
        dev_class = (_read(os.path.join(d, "bDeviceClass")) or "").lower()
        if n.startswith("usb"):
            kind, kind_label = "roothub", msg('usb_root', LANG_DEFAULT)
            product = None  # 下面會用 bus 編號取名，比 "xHCI Host Controller" 好認
        elif "09" in classes or dev_class == "09":
            kind, kind_label = "hub", msg('usb_hub', LANG_DEFAULT)
        elif "03" in classes:
            protos = {x["proto"] for x in ifaces if x["class"] == "03"}
            kind = "hid"; kind_label = msg('keyboard', LANG_DEFAULT) if "01" in protos else msg('mouse', LANG_DEFAULT) if "02" in protos else msg('hid_device', LANG_DEFAULT)
        elif "08" in classes: kind, kind_label = "storage", msg('usb_storage', LANG_DEFAULT)
        elif "e0" in classes: kind, kind_label = "wireless", msg('bluetooth', LANG_DEFAULT) if any(x["driver"] == "btusb" for x in ifaces) else msg('usb_wireless', LANG_DEFAULT)
        elif "01" in classes: kind, kind_label = "audio", msg('usb_audio', LANG_DEFAULT)
        elif "0e" in classes: kind, kind_label = "video", msg('camera', LANG_DEFAULT)
        elif "07" in classes: kind, kind_label = "printer", msg('usb_printer', LANG_DEFAULT)
        elif "02" in classes or "0a" in classes: kind, kind_label = "comm", msg('usb_network', LANG_DEFAULT)
        elif "06" in classes: kind, kind_label = "image", msg('usb_image', LANG_DEFAULT)
        elif "11" in classes: kind, kind_label = "billboard", "USB-C Billboard"
        elif "ff" in classes: kind, kind_label = "vendor", msg('usb_vendor', LANG_DEFAULT)
        speed = _read(os.path.join(d, "speed")) or ""
        if kind == "roothub":
            product = msg('usb_root_bus', LANG_DEFAULT, p0='3.x' if speed not in ('12', '480', '1.5') else '2.0', p1=_read(os.path.join(d, 'busnum')))
        else:
            product = _read(os.path.join(d, "product"))
        manufacturer = _read(os.path.join(d, "manufacturer"))
        mp = _read(os.path.join(d, "bMaxPower"))  # 例如 "500mA"：裝置在描述子宣告的最大耗電，不是量測
        max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
        devs[n] = {
            "path": n, "vid": vid, "pid": pid,
            "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
            "name": product or products.get((vid, pid)) or msg('unknown_device', LANG_DEFAULT, p0=vid, p1=pid),
            "manufacturer": manufacturer or vendors.get(vid),
            "name_from_ids": not product and (vid, pid) in products,
            "speed": speed, "speed_label": USB_SPEED.get(speed, f"{speed} Mb/s" if speed else "—"),
            "kind": kind, "kind_label": kind_label,
            "serial": _read(os.path.join(d, "serial")),
            "ports": _read(os.path.join(d, "maxchild")),
            "interfaces": ifaces,
            "busnum": _read(os.path.join(d, "busnum")), "devnum": _read(os.path.join(d, "devnum")),
            "children": [],
        }
    # 掛樹：usbN 是 bus N 的 root；"5-1.4.2" 的父是 "5-1.4"，"5-1" 的父是 "usb5"
    roots = []
    def parent_of(n):
        if n.startswith("usb"):
            return None
        if "." in n:
            return n.rsplit(".", 1)[0]
        return "usb" + n.split("-", 1)[0]
    def sortkey(n):
        return [int(x) if x.isdigit() else x for x in re.split(r"[-.]", n.replace("usb", ""))]
    for n in sorted(devs, key=sortkey):
        p = parent_of(n)
        if p and p in devs:
            devs[p]["children"].append(devs[n])
        else:
            roots.append(devs[n])
    empty = [r for r in roots if r["kind"] == "roothub" and not r["children"]]
    used = [r for r in roots if not (r["kind"] == "roothub" and not r["children"])]
    def count(node):
        return 1 + sum(count(c) for c in node["children"])
    return {"roots": used, "empty_roothubs": len(empty), "total": sum(count(r) for r in roots),
            "ids_file": os.path.exists(USB_IDS)}


def usb_ports():
    """每個 xHCI 控制器一個實體 USB-C 孔（USB2 與 USB3 各一個根集線器）。
    位置取自韌體 ACPI _PLD（/sys .../physical_location），和 Windows 裝置管理員畫圖用的是同一份資料。
    韌體對同一側的兩個孔沒有區分順序，所以前端提供「插入即亮」辨識。"""
    base = "/sys/bus/usb/devices"
    ctrls = {}
    for n in os.listdir(base):
        if not n.startswith("usb"):
            continue
        d = os.path.join(base, n)
        ctrl = os.path.basename(os.path.realpath(os.path.join(d, "..")))
        speed = _read(os.path.join(d, "speed")) or ""
        c = ctrls.setdefault(ctrl, {"controller": ctrl, "usb2_bus": None, "usb3_bus": None, "location": None,
                                    "connect_type": None, "devices": [], "internal": False})
        key = "usb3_bus" if speed not in ("12", "480", "1.5") else "usb2_bus"
        c[key] = _read(os.path.join(d, "busnum"))
        # 埠資訊（root hub 的 port1..）
        for pdir in glob.glob(os.path.join(d, f"{n.replace('usb', '')}-0:1.0", f"{n}-port*")):
            loc_dir = os.path.join(pdir, "physical_location")
            if os.path.isdir(loc_dir) and not c["location"]:
                c["location"] = {k: _read(os.path.join(loc_dir, k)) for k in ("panel", "vertical_position", "horizontal_position", "dock", "lid")}
            ct = _read(os.path.join(pdir, "connect_type"))
            if ct and ct != "unknown":
                c["connect_type"] = ct
            devlink = os.path.join(pdir, "device")
            if os.path.exists(devlink):
                dev = os.path.realpath(devlink)
                dn = os.path.basename(dev)
                # 只列直接插在這個孔上的裝置（下面的 hub 子裝置數另外算）
                kids = [k for k in os.listdir(base) if k.startswith(dn + ".") and ":" not in k]
                mp = _read(os.path.join(dev, "bMaxPower"))
                max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
                c["devices"].append({
                    "path": dn, "name": _read(os.path.join(dev, "product")) or f"{_read(os.path.join(dev, 'idVendor'))}:{_read(os.path.join(dev, 'idProduct'))}",
                    "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
                    "manufacturer": _read(os.path.join(dev, "manufacturer")),
                    "speed": _read(os.path.join(dev, "speed")),
                    "children": len(kids),
                    "bus": "usb3" if key == "usb3_bus" else "usb2",
                })
    out = []
    for c in ctrls.values():
        # 沒有 _PLD、不可熱插拔、卻接著東西 → 內部（例如藍牙模組）
        c["internal"] = c["location"] is None and bool(c["devices"])
        c["external"] = c["location"] is not None
        out.append(c)
    out.sort(key=lambda c: c["controller"])
    return out


def displays():
    """xrandr 的輸出狀態。NVIDIA 驅動把四個 USB-C 的 DP Alt Mode 輸出命名為 USB-C-0..3、HDMI 為 HDMI-0。
    需要 X session；拿不到回 None（例如沒登入桌面）。"""
    env = dict(_ENV_C)
    env.setdefault("DISPLAY", ":1")
    xa = f"/run/user/{os.getuid()}/gdm/Xauthority"
    if os.path.exists(xa):
        env.setdefault("XAUTHORITY", xa)
    try:
        r = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    cur = None
    for line in r.stdout.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)(.*)$", line)
        if m:
            cur = m.group(1)
            mode = re.search(r"(\d+x\d+)\+\d+\+\d+", m.group(3))
            size = re.search(r"(\d+)mm x (\d+)mm", m.group(3))
            out[cur] = {"connected": m.group(2) == "connected", "mode": mode.group(1) if mode else None,
                        "primary": "primary" in m.group(3), "size_mm": [int(size.group(1)), int(size.group(2))] if size else None,
                        "hz": None}
        elif cur and out[cur]["connected"] and out[cur]["mode"] and line.startswith("   ") and "*" in line:
            hz = re.search(r"([\d.]+)\*", line)
            if hz:
                out[cur]["hz"] = float(hz.group(1))
    return out


USBC_MAP_FILE = os.path.join(HERE, "data", "usbc-map.json")


def usbc_map_load():
    """實體孔 → 控制器 的對應（伺服器端持久化，所有瀏覽器共用）。
    slot 0 固定是電源輸入孔（依 ASUS 規格與評測），核心無 UCSI/typec，供電狀態偵測不到，只能手動標記。"""
    try:
        with open(USBC_MAP_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def usbc_map_save(d):
    os.makedirs(os.path.dirname(USBC_MAP_FILE), exist_ok=True)
    tmp = USBC_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USBC_MAP_FILE)


def rear_panel():
    """後面板各孔的即時狀態。版面順序（左→右）依 ServeTheHome 評測：USB-C ×4（最左為 PD 電源輸入）、HDMI、10GbE、QSFP（ConnectX-7）。"""
    ports = usb_ports()
    ctrl_by_id = {p["controller"]: p for p in ports}
    disp = displays() or {}
    net = {}
    for i in os.listdir("/sys/class/net"):
        dev = f"/sys/class/net/{i}/device"
        if not os.path.exists(dev):
            continue
        drv = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(f"{dev}/driver") else ""
        net[i] = {"driver": drv, "state": _read(f"/sys/class/net/{i}/operstate"), "speed": _read(f"/sys/class/net/{i}/speed"),
                  "carrier": _read(f"/sys/class/net/{i}/carrier")}
    eth = next(((n, v) for n, v in net.items() if v["driver"].startswith("r81")), None)
    mlx = [n for n, v in net.items() if v["driver"].startswith("mlx")]
    mlx_pci = bool(_run(["lspci", "-d", "15b3:"], timeout=5))
    usbc = []
    for k in range(4):
        ctrl = f"NVDA8000:0{k}"
        c = ctrl_by_id.get(ctrl)
        usbc.append({
            "slot": k, "dp_output": f"USB-C-{k}", "display": disp.get(f"USB-C-{k}"),
            "controller_guess": ctrl, "usb": c and {"devices": c["devices"], "location": c["location"]},
        })
    return {
        "usbc": usbc, "hdmi": disp.get("HDMI-0"), "calib": usbc_map_load(),
        "eth10g": eth and {"iface": eth[0], **eth[1]},
        "connectx7": {"pci_present": mlx_pci, "ifaces": mlx, "driver_loaded": os.path.isdir("/sys/module/mlx5_core")},
        "controllers": ports, "displays_available": bool(disp),
        "layout_source": msg('rear_layout', LANG_DEFAULT),
    }


# ---------- PCI 裝置（人看的版本：橋接器收掉、連結速度、驅動、友善名稱）----------

PCI_GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}
PCI_LANE_GBPS = {1: 0.25, 2: 0.5, 3: 0.985, 4: 1.969, 5: 3.938, 6: 7.563}  # 每通道 GB/s（扣編碼後）
PCI_FRIENDLY = {  # vendor:device → (類型, 友善名稱)
    "1987:5027": ("NVMe SSD", msg('phison', LANG_DEFAULT)),
    "10ec:8127": (msg('network_10g', LANG_DEFAULT), msg('realtek', LANG_DEFAULT)),
    "14c3:7925": (msg('wifi_bt', LANG_DEFAULT), msg('mediatek', LANG_DEFAULT)),
    "10de:2e12": ("GPU", msg('gb10_gpu', LANG_DEFAULT)),
}
PCI_CLASS_ZH = {"01": msg('storage_controller', LANG_DEFAULT), "02": msg('network', LANG_DEFAULT), "03": msg('display', LANG_DEFAULT), "04": msg('multimedia', LANG_DEFAULT), "06": msg('bridge', LANG_DEFAULT), "0c": msg('serial_bus', LANG_DEFAULT), "10": msg('crypto', LANG_DEFAULT), "12": msg('accelerator', LANG_DEFAULT)}


def _pci_devices():
    out = _run(["lspci", "-mm", "-nn"], timeout=10)
    if not out:
        return None
    devs, bridges = [], []
    for line in out.splitlines():
        # 格式：slot "class [xxxx]" "vendor [vvvv]" "device [dddd]" -rXX -pXX "subv" "subd"
        m = re.match(r'^(\S+) "([^"]*) \[([0-9a-f]{4})\]" "([^"]*) \[([0-9a-f]{4})\]" "([^"]*) \[([0-9a-f]{4})\]"(.*)$', line)
        if not m:
            continue
        slot, cls_name, cls, ven_name, vid, dev_name, did, rest = m.groups()
        if not re.match(r"^[0-9a-f]{4}:", slot):
            slot = "0000:" + slot
        sub = re.findall(r'"([^"]*)"', rest)
        sysd = f"/sys/bus/pci/devices/{slot}"
        rd = lambda k: (_read(os.path.join(sysd, k)) or "").strip()
        cur_s, cur_w, max_s, max_w = rd("current_link_speed"), rd("current_link_width"), rd("max_link_speed"), rd("max_link_width")
        gen = lambda v: PCI_GEN.get(re.sub(r" GT/s.*", "", v)) if v else None
        cur_gen, max_gen = gen(cur_s), gen(max_s)
        width = lambda v: int(v) if v.isdigit() else None
        cw, mw = width(cur_w), width(max_w)
        drv = os.path.basename(os.path.realpath(os.path.join(sysd, "driver"))) if os.path.islink(os.path.join(sysd, "driver")) else None
        key = f"{vid}:{did}"
        kind, friendly = PCI_FRIENDLY.get(key, (PCI_CLASS_ZH.get(cls[:2], cls_name), f"{ven_name} {dev_name}".strip()))
        rec = {"slot": slot, "class": cls, "class_name": cls_name, "vendor": ven_name, "vid": vid, "did": did, "device": dev_name,
               "subsystem": (sub[0] + " " + sub[1]).strip() if len(sub) >= 2 else None,
               "kind": kind, "friendly": friendly, "driver": drv,
               "cur_gen": cur_gen, "cur_width": cw, "max_gen": max_gen, "max_width": mw,
               "cur_gbps": round(PCI_LANE_GBPS.get(cur_gen, 0) * (cw or 0), 2) if cur_gen and cw else None,
               "max_gbps": round(PCI_LANE_GBPS.get(max_gen, 0) * (mw or 0), 2) if max_gen and mw else None}
        (bridges if cls.startswith("06") else devs).append(rec)
    # 空的根埠：橋接器底下沒有任何端點裝置（ConnectX-7 應該在這裡）
    used_domains = {d["slot"].split(":")[0] for d in devs}
    empty = [b for b in bridges if b["slot"].split(":")[0] not in used_domains]
    return {"devices": devs, "bridges": len(bridges),
            "empty_ports": [{"slot": b["slot"], "max_gen": b["max_gen"], "max_width": b["max_width"]} for b in empty]}


# ---------- 印表機（CUPS 佇列 + 區網 IPP 探索 + USB）----------

def _printers():
    res = {"cups_active": (_run(["systemctl", "is-active", "cups"], timeout=5) or "").strip() == "active", "queues": [], "discovered": [], "usb": []}
    lp = _run(["lpstat", "-p", "-d"], timeout=10) or ""
    for line in lp.splitlines():
        m = re.match(r"printer (\S+) is (\w+)", line)
        if m:
            res["queues"].append({"name": m.group(1), "state": m.group(2), "default": False})
    m = re.search(r"system default destination: (\S+)", lp)
    if m:
        for q in res["queues"]:
            q["default"] = q["name"] == m.group(1)
    lv = _run(["lpstat", "-v"], timeout=10) or ""
    uris = dict(re.findall(r"device for (\S+): (\S+)", lv))
    for q in res["queues"]:
        q["uri"] = uris.get(q["name"])
    jobs = _run(["lpstat", "-o"], timeout=10) or ""
    res["jobs"] = len([l for l in jobs.splitlines() if l.strip()])
    # 區網探索：driverless 列出 IPP Everywhere 印表機（mDNS），沒有就是空
    dl = _run(["driverless", "list"], timeout=20) or ""
    for line in dl.splitlines():
        if line.startswith("DEBUG"):
            continue
        m = re.match(r'"?(\S+)"?\s+\S+\s+"([^"]+)"', line)
        if m:
            res["discovered"].append({"uri": m.group(1), "name": m.group(2)})
        elif line.strip() and "://" in line:
            res["discovered"].append({"uri": line.split()[0], "name": line.strip()})
    # USB 印表機：介面類別 07
    base = "/sys/bus/usb/devices"
    for i in os.listdir(base):
        if ":" in i and (_read(os.path.join(base, i, "bInterfaceClass")) or "") == "07":
            dev = os.path.join(base, i.split(":")[0])
            res["usb"].append({"name": _read(os.path.join(dev, "product")) or i, "manufacturer": _read(os.path.join(dev, "manufacturer"))})
    return res


def _list_cmd(cmd):
    out = _run(cmd, timeout=10)
    return out.strip().splitlines() if out else None


def _dmi():
    """sudo -n dmidecode：只有 sudoers 放行 dmidecode 免密碼時才拿得到；否則回 available=False，前端照實說明。"""
    try:
        # dmidecode 的 -t 不吃逗號分隔的關鍵字，只吃逗號分隔的數字：1 system、2 baseboard、3 chassis、16 memory array、17 memory device
        r = subprocess.run(["sudo", "-n", "/usr/sbin/dmidecode", "-t", "1,2,3,16,17"],
                           capture_output=True, text=True, timeout=10, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "note": msg('dmidecode_exec_failed', LANG_DEFAULT, p0=e)}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        err = err[-1] if err else f"rc={r.returncode}"
        if "password" in err.lower() or "sudo" in err.lower():
            return {"available": False,
                    "note": msg('dmi_root', LANG_DEFAULT)}
        return {"available": False, "note": msg('dmidecode_failed', LANG_DEFAULT, p0=err)}
    sections, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("Handle "):
            cur = {"_type": re.search(r"DMI type (\d+)", line).group(1), "_fields": {}}
            sections.append(cur)
        elif cur is not None and line.startswith("\t") and ":" in line and not line.startswith("\t\t"):
            k, _, v = line.strip().partition(":")
            cur["_fields"][k.strip()] = v.strip()
    def first(t):
        return next((x["_fields"] for x in sections if x["_type"] == t), {})
    sysf, board, chassis, arr = first("1"), first("2"), first("3"), first("16")
    mods = []
    for x in sections:
        if x["_type"] != "17":
            continue
        f = x["_fields"]
        if f.get("Size", "").lower() in ("no module installed", "not installed", ""):
            continue
        mods.append({k2: (None if f.get(k1) in (None, "None", "Unknown", "Not Specified", "Not Provided") else f.get(k1)) for k1, k2 in (
            ("Locator", "slot"), ("Size", "size"), ("Type", "type"), ("Form Factor", "form"),
            ("Speed", "speed"), ("Configured Memory Speed", "configured_speed"),
            ("Manufacturer", "manufacturer"), ("Part Number", "part"), ("Serial Number", "serial"))})
    clean = lambda v: None if v in (None, "", "None", "Unknown", "Not Specified", "Not Provided", "To Be Filled By O.E.M.", "Default string") else v
    return {
        "available": True,
        "system": {"serial": clean(sysf.get("Serial Number")), "uuid": clean(sysf.get("UUID")),
                   "sku": clean(sysf.get("SKU Number")), "family": clean(sysf.get("Family"))},
        "board": {"serial": clean(board.get("Serial Number")), "version": clean(board.get("Version"))},
        "chassis": {"type": clean(chassis.get("Type")), "serial": clean(chassis.get("Serial Number"))},
        "memory": {"max_capacity": clean(arr.get("Maximum Capacity")), "slots": clean(arr.get("Number Of Devices")),
                   "error_correction": clean(arr.get("Error Correction Type")), "modules": mods},
    }


def _bluetooth():
    """bluetoothctl（免 root）。控制器、已配對裝置、連線狀態、電量（裝置有回報才有）。"""
    if not _run(["which", "bluetoothctl"], timeout=3):
        return None
    show = _run(["bluetoothctl", "show"], timeout=5)
    if show is None:
        return {"available": False, "note": msg('bluetooth_unavailable', LANG_DEFAULT)}
    ctl = {}
    for line in show.splitlines():
        line = line.strip()
        if line.startswith("Controller "):
            ctl["address"] = line.split()[1]
        for k in ("Name", "Alias", "Powered", "Discoverable", "Pairable"):
            if line.startswith(k + ":"):
                ctl[k.lower()] = line.split(":", 1)[1].strip()
    blocked = None
    rk = _run(["rfkill", "-J"], timeout=5)
    if rk:
        try:
            for d in json.loads(rk).get("rfkilldevices", []):
                if d.get("type") == "bluetooth":
                    blocked = (d.get("soft") == "blocked") or (d.get("hard") == "blocked")
        except ValueError:
            pass
    devices = []
    paired = _run(["bluetoothctl", "devices", "Paired"], timeout=5) or ""
    for line in paired.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[0] != "Device":
            continue
        mac, name = parts[1], parts[2]
        info = _run(["bluetoothctl", "info", mac], timeout=5) or ""
        f = {}
        for l in info.splitlines():
            l = l.strip()
            for k in ("Icon", "Connected", "Trusted", "Battery Percentage"):
                if l.startswith(k + ":"):
                    f[k] = l.split(":", 1)[1].strip()
        batt = None
        m = re.search(r"\((\d+)\)", f.get("Battery Percentage", ""))
        if m:
            batt = int(m.group(1))
        devices.append({"mac": mac, "name": name, "icon": f.get("Icon"), "connected": f.get("Connected") == "yes",
                        "trusted": f.get("Trusted") == "yes", "battery": batt})
    devices.sort(key=lambda d: (not d["connected"], d["name"].lower()))
    return {"available": True, "controller": ctl, "rfkill_blocked": blocked, "devices": devices}


def _nvme_health():
    """NVMe 健康：免 root 的型號／韌體／序號從 /sys 讀；SMART 要 sudoers 放行
    `nvme smart-log /dev/nvme0n1 --output-format=json`（參數寫死）。沒放行就回 available=False 並說明。"""
    base = "/sys/class/nvme/nvme0"
    if not os.path.isdir(base):
        return None
    # 製造商：PCI 廠商 ID（1987=Phison）＋ EUI-64 前三組 OUI（64-79-A7=Phison）。兩者都是硬體登記值，不是猜的。
    OUI = {"6479a7": "Phison", "0025b3": "Samsung", "002538": "Samsung", "8ce38e": "Kioxia/Toshiba", "e4d25c": "Kioxia",
           "000cca": "HGST/WD", "001b44": "SanDisk", "5cd2e4": "Intel", "000000": None}
    PCI_VENDOR = {"0x1987": "Phison", "0x144d": "Samsung", "0x1e0f": "Kioxia", "0x15b7": "SanDisk/WD", "0x8086": "Intel",
                  "0x1c5c": "SK hynix", "0x1cc1": "ADATA", "0x126f": "Silicon Motion", "0x1e49": "YMTC", "0x2646": "Kingston", "0x1344": "Micron"}
    vid = (_read(base + "/device/vendor") or "").lower()
    did = (_read(base + "/device/device") or "").lower()
    wwid = _read("/sys/class/block/nvme0n1/wwid") or ""
    m = re.match(r"eui\.([0-9a-f]+)", wwid)
    oui = m.group(1)[-16:][:6] if m and len(m.group(1)) >= 16 else None   # 可能有前導 0 補到 32 位，取最後 16 位才是 EUI-64
    ctrl = _run(["lspci", "-nn", "-s", os.path.basename(os.path.realpath(base + "/device"))], timeout=5) or ""
    ctrl_name = re.sub(r"^.*?: ", "", ctrl.strip().splitlines()[0]) if ctrl.strip() else None
    info = {"model": (_read(base + "/model") or "").strip(), "firmware": (_read(base + "/firmware_rev") or "").strip(),
            "serial": (_read(base + "/serial") or "").strip(), "state": _read(base + "/state"),
            "vendor": PCI_VENDOR.get(vid) or vid, "vendor_pci_id": f"{vid}:{did}", "controller": ctrl_name,
            "oui": oui, "oui_vendor": OUI.get(oui) if oui else None,
            "dramless": "DRAM-less" in (ctrl_name or ""), "hmb": os.path.exists(base + "/hmb"),
            "form_factor_note": msg('nand_unavailable', LANG_DEFAULT), "vendor_source": msg('nvme_vendor_source', LANG_DEFAULT)}
    try:
        r = subprocess.run(["sudo", "-n", "/usr/sbin/nvme", "smart-log", "/dev/nvme0n1", "--output-format=json"],
                           capture_output=True, text=True, timeout=10, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {**info, "available": False, "note": msg('nvme_exec_failed', LANG_DEFAULT, p0=e)}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        err = err[-1] if err else f"rc={r.returncode}"
        if "password" in err.lower() or "sudo" in err.lower():
            return {**info, "available": False, "note": msg('smart_root', LANG_DEFAULT)}
        return {**info, "available": False, "note": msg('nvme_smart_failed', LANG_DEFAULT, p0=err)}
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return {**info, "available": False, "note": msg('nvme_not_json', LANG_DEFAULT)}
    g = lambda k: j.get(k)
    # data_units 單位是 512 bytes × 1000
    du_bytes = lambda v: v * 512 * 1000 if isinstance(v, (int, float)) else None
    temp_k = g("temperature")
    crit = g("critical_warning") or 0
    warn_bits = []
    if isinstance(crit, int):
        for bit, label in ((0, msg('spare_low', LANG_DEFAULT)), (1, msg('temp_threshold', LANG_DEFAULT)), (2, msg('media_degraded', LANG_DEFAULT)), (3, msg('read_only', LANG_DEFAULT)), (4, msg('volatile_failed', LANG_DEFAULT))):
            if crit & (1 << bit):
                warn_bits.append(label)
    return {
        **info, "available": True,
        "critical_warning": crit, "warnings": warn_bits,
        "temp_c": round(temp_k - 273.15, 1) if isinstance(temp_k, (int, float)) and temp_k > 200 else temp_k,
        "percent_used": g("percent_used"), "avail_spare": g("avail_spare"), "spare_thresh": g("spare_thresh"),
        "data_written": du_bytes(g("data_units_written")), "data_read": du_bytes(g("data_units_read")),
        "host_writes": g("host_write_commands"), "host_reads": g("host_read_commands"),
        "power_on_hours": g("power_on_hours"), "power_cycles": g("power_cycles"),
        "unsafe_shutdowns": g("unsafe_shutdowns"), "media_errors": g("media_errors"), "num_err_log_entries": g("num_err_log_entries"),
        "warning_temp_time": g("warning_temp_time"), "critical_comp_time": g("critical_comp_time"),
        "source": msg('nvme_source', LANG_DEFAULT),
    }


_HW_CACHE = {"ts": 0, "data": None}
HW_TTL = 3600                                   # 硬體不會變，快取 1 小時；換硬體用「重新讀取」
HW_SNAPSHOT = os.path.join(HERE, "data", "hardware.json")


def _hw_snapshot_load():
    try:
        with open(HW_SNAPSHOT) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("generated"):
            _HW_CACHE.update(data=d, ts=os.path.getmtime(HW_SNAPSHOT))
    except (OSError, ValueError):
        pass


def hardware_static(force=False):
    if not force and _HW_CACHE["data"] and time.time() - _HW_CACHE["ts"] < HW_TTL:
        return _HW_CACHE["data"]
    osr = _os_release()
    mem = _meminfo()
    data = {
        "system": {
            **{k: _read(os.path.join(DMI_DIR, k)) for k in DMI_FIELDS},
            "hostname": _read("/etc/hostname"),
            "os": osr.get("PRETTY_NAME"),
            "kernel": (_run(["uname", "-r"], timeout=5) or "").strip() or None,
            "dgx_release": _dpkg_version("dgx-release"),
            "dgx_dashboard": _dpkg_version("dgx-dashboard"),
        },
        "dmi": _dmi(),
        "nvme": _nvme_health(),
        "cpu": _lscpu(),
        "cpu_cores": _cpu_topology(),
        "memory": {"total": mem.get("MemTotal"), "swap_total": mem.get("SwapTotal")},
        "gpu": _gpu_static(),
        "disks": _disks(),
        "network": _network(),
        "bluetooth": _bluetooth(),
        "usb": _list_cmd(["lsusb"]),
        "usb_tree": _usb_tree(),
        "pci": _list_cmd(["lspci"]),
        "pci_devices": _pci_devices(),
        "printers": _printers(),
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    _HW_CACHE.update(ts=time.time(), data=data)
    try:
        os.makedirs(os.path.dirname(HW_SNAPSHOT), exist_ok=True)
        tmp = HW_SNAPSHOT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, HW_SNAPSHOT)
    except OSError:
        pass
    return data


def _cpu_jiffies():
    """/proc/stat 的總和與每核 jiffies；前端做差分算使用率。"""
    lines = (_read("/proc/stat") or "").splitlines()
    if not lines or not lines[0].startswith("cpu "):
        return None
    def parse(line):
        v = [int(x) for x in line.split()[1:]]
        return {"total": sum(v), "idle": v[3] + (v[4] if len(v) > 4 else 0)}
    cores = []
    for line in lines[1:]:
        if not re.match(r"cpu\d+ ", line):
            break
        cores.append(parse(line))
    out = parse(lines[0]); out["cores"] = cores
    return out


def _cpu_freqs():
    """每核目前時脈（kHz → MHz）。拿不到的核心回 None。"""
    res = []
    for i in range(os.cpu_count() or 0):
        v = _read(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
        res.append(int(v) // 1000 if v and v.isdigit() else None)
    return res


def _cpu_topology():
    """每核的最高時脈與調速器，用來把核心分到叢集（X925/A725 靠最高時脈區分）。"""
    cores = []
    for i in range(os.cpu_count() or 0):
        base = f"/sys/devices/system/cpu/cpu{i}/cpufreq/"
        mx = _read(base + "cpuinfo_max_freq"); mn = _read(base + "cpuinfo_min_freq")
        cores.append({"id": i, "max_mhz": int(mx) // 1000 if mx and mx.isdigit() else None,
                      "min_mhz": int(mn) // 1000 if mn and mn.isdigit() else None,
                      "governor": _read(base + "scaling_governor")})
    return cores


def _net_counters():
    """/proc/net/dev 的累計位元組數；前端拿兩次取樣算每秒流量。
    連線狀態與速度也在這裡即時讀（/sys 兩個檔，便宜）：介面清單本身在硬體快取裡一小時才更新，
    插上網路線後監控頁要立刻看到卡片、看到 10 Gb/s，不能等快取。"""
    res = {}
    for line in (_read("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 9:
            name = name.strip()
            speed = _read(f"/sys/class/net/{name}/speed")
            res[name] = {"rx": int(f[0]), "tx": int(f[8]), "state": (_read(f"/sys/class/net/{name}/operstate") or "").strip(),
                         "speed_mbps": int(speed) if speed and speed.strip().lstrip("-").isdigit() and int(speed) > 0 else None}
    return res


def _root_usage():
    try:
        st = os.statvfs("/")
        return {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        return None


# ---------- Wi-Fi 品質（iw，免 root）----------

def _wifi_iface():
    for i in os.listdir("/sys/class/net"):
        if os.path.isdir(f"/sys/class/net/{i}/wireless"):
            return i
    return None


def _wifi_live():
    """iw link + station dump：訊號、速率、MCS、重試、beacon loss。約 10 ms。"""
    iface = _wifi_iface()
    if not iface:
        return None
    link = _run(["iw", "dev", iface, "link"], timeout=5) or ""
    if "Not connected" in link or not link.strip():
        return {"iface": iface, "connected": False}
    info = _run(["iw", "dev", iface, "info"], timeout=5) or ""
    st = _run(["iw", "dev", iface, "station", "dump"], timeout=5) or ""
    g = lambda pat, src, cast=str: (lambda m: cast(m.group(1)) if m else None)(re.search(pat, src))
    freq = g(r"freq:\s*([\d.]+)", link, float)
    band = "6 GHz" if freq and freq >= 5925 else "5 GHz" if freq and freq >= 4900 else "2.4 GHz" if freq else None
    rate_re = lambda k: g(rf"{k} bitrate:\s*([\d.]+) MBit/s", st, float)
    mcs = lambda k: g(rf"{k} bitrate:.*?(HE|VHT|EHT|HT)-MCS (\d+)", st)
    def mcs_val(k):
        m = re.search(rf"{k} bitrate:.*?(HE|VHT|EHT|HT)-MCS (\d+)", st)
        return {"phy": m.group(1), "mcs": int(m.group(2))} if m else None
    return {
        "iface": iface, "connected": True,
        "ssid": g(r"SSID:\s*(.+)", link), "bssid": g(r"Connected to ([0-9a-f:]{17})", link),
        "freq_mhz": freq, "band": band, "channel": g(r"channel (\d+)", info, int), "width_mhz": g(r"width:\s*(\d+) MHz", info, int),
        "signal_dbm": g(r"signal:\s*(-?\d+)", link, int), "signal_avg_dbm": g(r"signal avg:\s*(-?\d+)", st, int),
        "tx_mbps": rate_re("tx"), "rx_mbps": rate_re("rx"), "tx_mcs": mcs_val("tx"), "rx_mcs": mcs_val("rx"),
        "tx_packets": g(r"tx packets:\s*(\d+)", st, int), "tx_retries": g(r"tx retries:\s*(\d+)", st, int),
        "tx_failed": g(r"tx failed:\s*(\d+)", st, int), "beacon_loss": g(r"beacon loss:\s*(\d+)", st, int),
        "rx_drop": g(r"rx drop misc:\s*(\d+)", st, int), "connected_s": g(r"connected time:\s*(\d+)", st, int),
        "txpower_dbm": g(r"txpower ([\d.]+) dBm", info, float),
    }


def wifi_channel_analysis(rescan=False):
    """頻道分析。2.4 GHz 每個頻道間隔 5 MHz、訊號寬 20 MHz，所以相鄰 ±4 格都會重疊，
    只有 1/6/11 互不重疊；干擾分數 = 重疊範圍內鄰居訊號的加權總和（差越遠權重越低）。
    5 GHz 的 20 MHz 頻道互不重疊，只算同頻道。主動重掃約 8 秒且會讓連線變鈍，所以只在按鈕觸發時做。"""
    args = ["nmcli", "-t", "-f", "SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY,IN-USE", "dev", "wifi", "list"]
    if rescan:
        args.append("--rescan")
        args.append("yes")
    out = _run(args, timeout=40)
    if out is None:
        return {"ok": False, "error": msg('wifi_scan_failed', LANG_DEFAULT)}
    aps, mine = [], None
    for line in out.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 7:
            continue
        ssid, bssid, chan, freq, sig, sec, inuse = parts[:7]
        try:
            chan, freq, sig = int(chan), int(freq.split()[0]), int(sig)
        except (ValueError, IndexError):
            continue
        ap = {"ssid": ssid or None, "bssid": bssid.replace("\\:", ":"), "chan": chan, "freq": freq,
              "signal": sig, "security": sec, "in_use": inuse.strip() == "*",
              "band": "6 GHz" if freq >= 5925 else "5 GHz" if freq >= 4900 else "2.4 GHz"}
        aps.append(ap)
        if ap["in_use"]:
            mine = ap
    def score(band, cand, spacing):
        """cand 頻道的干擾分數。2.4 GHz 用 ±4 格線性衰減權重；5/6 GHz 只算同頻道。"""
        tot, srcs = 0, []
        for a in aps:
            if a["band"] != band or a["in_use"]:
                continue
            d = abs(a["chan"] - cand)
            w = max(0.0, 1 - d / 5) if spacing == "overlap" else (1.0 if d == 0 else 0.0)
            if w > 0:
                tot += a["signal"] * w
                srcs.append({"ssid": a["ssid"], "chan": a["chan"], "signal": a["signal"], "weight": round(w, 2)})
        srcs.sort(key=lambda x: -x["signal"] * x["weight"])
        return {"chan": cand, "score": round(tot), "sources": srcs[:6]}
    bands = {}
    if any(a["band"] == "2.4 GHz" for a in aps):
        cands = [score("2.4 GHz", c, "overlap") for c in (1, 6, 11)]
        bands["2.4 GHz"] = {"candidates": sorted(cands, key=lambda x: x["score"]),
                            "note": msg('wifi_24_note', LANG_DEFAULT)}
    fives = sorted({a["chan"] for a in aps if a["band"] == "5 GHz"})
    if fives:
        common = [36, 40, 44, 48, 149, 153, 157, 161]
        cands = [score("5 GHz", c, "same") for c in sorted(set(common) | set(fives))]
        bands["5 GHz"] = {"candidates": sorted(cands, key=lambda x: x["score"])[:8],
                          "note": msg('wifi_5_note', LANG_DEFAULT)}
    aps.sort(key=lambda a: (a["band"], a["chan"], -a["signal"]))
    return {"ok": True, "aps": aps, "bands": bands, "current": mine, "rescanned": bool(rescan),
            "scanned": datetime.now().isoformat(timespec="seconds"),
            "caveats": [msg('wifi_snapshot', LANG_DEFAULT),
                        msg('wifi_signal', LANG_DEFAULT),
                        msg('wifi_analysis_only', LANG_DEFAULT)]}


_WIFI_SCAN = {"ts": 0, "data": None}


def _wifi_aps():
    """nmcli 看得到的 AP（不主動重掃，用 NetworkManager 快取）；快取 60 秒。"""
    if time.time() - _WIFI_SCAN["ts"] < 60 and _WIFI_SCAN["data"] is not None:
        return _WIFI_SCAN["data"]
    out = _run(["nmcli", "-t", "-f", "SSID,BSSID,FREQ,CHAN,SIGNAL,RATE,SECURITY,IN-USE", "dev", "wifi", "list"], timeout=15)
    aps = []
    if out:
        for line in out.splitlines():
            # BSSID 裡的冒號被跳脫成 \:
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 8:
                continue
            ssid, bssid, freq, chan, sig, rate, sec, inuse = parts[:8]
            aps.append({"ssid": ssid, "bssid": bssid.replace("\\:", ":"), "freq_mhz": int(freq.split()[0]) if freq.split() else None,
                        "chan": int(chan) if chan.isdigit() else None, "signal_pct": int(sig) if sig.isdigit() else None,
                        "rate_mbps": int(rate.split()[0]) if rate.split() and rate.split()[0].isdigit() else None,
                        "security": sec, "in_use": inuse.strip() == "*"})
    aps.sort(key=lambda a: -(a["signal_pct"] or 0))
    _WIFI_SCAN.update(ts=time.time(), data=aps)
    return aps


def _bt_connected():
    out = _run(["bluetoothctl", "devices", "Connected"], timeout=5) or ""
    return [l.split(" ", 2)[2] for l in out.splitlines() if l.startswith("Device ") and len(l.split(" ", 2)) == 3]


def _wifi_events_24h():
    """NetworkManager 24 小時內 Wi-Fi 斷線／重連次數（journal，免 root 可讀系統單元）。"""
    out = _run(["journalctl", "-u", "NetworkManager", "--since", "-24 h", "--no-pager", "-o", "short"], timeout=15) or ""
    iface = _wifi_iface() or "wl"
    disc = len([l for l in out.splitlines() if iface in l and "activated -> deactivating" in l])
    act = len([l for l in out.splitlines() if iface in l and "Activation: successful" in l])
    return {"disconnects": disc, "activations": act}


def hardware_live():
    mem = _meminfo()
    load = (_read("/proc/loadavg") or "").split()[:3]
    up = _read("/proc/uptime")
    return {
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
                   "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")},
        "load": [float(x) for x in load] if len(load) == 3 else None,
        "uptime_s": float(up.split()[0]) if up else None,
        "gpu": _gpu_live(),
        "gpu_alert": gpu_alert(),
        "sensors": _sensors(),
        "cpu": _cpu_jiffies(),
        "cpu_freq": _cpu_freqs(),
        "wifi": _wifi_live(),
        "net": _net_counters(),
        "disk_root": _root_usage(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": time.time(),
    }


# ---------- 本機 LLM 探針（偷師 sparkDash；它沒做 Ollama，這裡補上）----------
import urllib.request
import urllib.error

LLM_TARGETS = [
    {"kind": "ollama", "url": "http://127.0.0.1:11434"},
    {"kind": "lmstudio", "url": "http://127.0.0.1:1234"},
    {"kind": "llama.cpp", "url": "http://127.0.0.1:8080"},
    {"kind": "vllm", "url": "http://127.0.0.1:8000"},
]


def _http_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def llm_status():
    out = []
    for t in LLM_TARGETS:
        base = t["url"]
        if t["kind"] == "ollama":
            ver = _http_json(base + "/api/version")
            if ver is None:
                continue
            ps = _http_json(base + "/api/ps") or {}
            tags = _http_json(base + "/api/tags") or {}
            out.append({
                "kind": "Ollama", "url": base, "version": ver.get("version"),
                "loaded": [{"name": m.get("name"), "size": m.get("size"), "size_vram": m.get("size_vram"),
                            "context": m.get("context_length"), "expires_at": m.get("expires_at"),
                            "quant": ((m.get("details") or {}).get("quantization_level")),
                            "params": ((m.get("details") or {}).get("parameter_size"))} for m in ps.get("models", [])],
                "installed": [{"name": m.get("name"), "size": m.get("size"), "modified": (m.get("modified_at") or "")[:10],
                               "quant": ((m.get("details") or {}).get("quantization_level")), "params": ((m.get("details") or {}).get("parameter_size")),
                               "family": ((m.get("details") or {}).get("family"))} for m in tags.get("models", [])],
                "bench": True,
                "note": msg('ollama_metrics', LANG_DEFAULT),
            })
        elif t["kind"] == "lmstudio":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "LM Studio", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": msg('lmstudio_metrics', LANG_DEFAULT)})
        elif t["kind"] == "llama.cpp":
            props = _http_json(base + "/props")
            if props is None:
                continue
            slots = _http_json(base + "/slots") or []
            out.append({"kind": "llama.cpp", "url": base, "version": (props.get("build_info") or None),
                        "loaded": [{"name": (props.get("default_generation_settings") or {}).get("model") or props.get("model_path")}],
                        "installed": [], "bench": False, "slots": slots if isinstance(slots, list) else [],
                        "note": msg('llamacpp_source', LANG_DEFAULT)})
        elif t["kind"] == "vllm":
            models = _http_json(base + "/v1/models")
            if models is None:
                continue
            out.append({"kind": "vLLM", "url": base, "version": None,
                        "loaded": [{"name": m.get("id")} for m in models.get("data", [])], "installed": [], "bench": False,
                        "note": msg('vllm_metrics', LANG_DEFAULT)})
    return out


OLLAMA = "http://127.0.0.1:11434"
_SHOW_CACHE = {}


def ollama_show(name):
    """/api/show 的精華：家族、參數量、量化、上下文長度、能力。快取（模型不變就不重查）。"""
    if name in _SHOW_CACHE:
        return _SHOW_CACHE[name]
    j = _http_json(OLLAMA + "/api/show", {"name": name}, timeout=20) or {}
    det = j.get("details") or {}
    mi = j.get("model_info") or {}
    ctx = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
    res = {"family": det.get("family"), "params": det.get("parameter_size"), "quant": det.get("quantization_level"),
           "context": ctx, "capabilities": j.get("capabilities") or [], "arch": mi.get("general.architecture"),
           "param_count": mi.get("general.parameter_count")}
    if j:
        _SHOW_CACHE[name] = res
    return res


def ollama_config():
    out = _run(["systemctl", "show", "ollama", "-p", "Environment", "-p", "ActiveState", "-p", "MainPID"], timeout=5) or ""
    env = dict(re.findall(r"(OLLAMA_[A-Z_]+)=([^\s\"]+)", out))
    active = re.search(r"ActiveState=(\w+)", out)
    return {"active": active.group(1) if active else None, "env": env,
            "num_parallel": int(env.get("OLLAMA_NUM_PARALLEL", "0") or 0) or None,
            "max_loaded": int(env.get("OLLAMA_MAX_LOADED_MODELS", "0") or 0) or None,
            "models_dir": env.get("OLLAMA_MODELS", "/usr/share/ollama/.ollama/models"),
            "note": msg('ollama_config', LANG_DEFAULT)}


def llm_stores():
    """三個模型倉庫並排，找重複：Ollama（主機）、LM Studio（~/.lmstudio/models）、Open WebUI 容器內的 Ollama（docker 卷）。"""
    def norm(n):
        toks = re.split(r"[-_:. ]+", n.lower().split("/")[-1])
        toks = [t for t in toks if t and not re.fullmatch(r"(\d+(\.\d+)?[bm]|a\d+b|q\d\w*|mxfp\d|f16|bf16|gguf|instruct|it|chat|latest|\d+k)", t)]
        return "".join(toks)
    ol = [{"name": m["name"], "bytes": m.get("size") or 0} for m in (_http_json(OLLAMA + "/api/tags") or {}).get("models", [])]
    lm = []
    lmdir = os.path.join(HOME, ".lmstudio", "models")
    if os.path.isdir(lmdir):
        for pub in os.listdir(lmdir):
            pd = os.path.join(lmdir, pub)
            if not os.path.isdir(pd):
                continue
            for mdl in os.listdir(pd):
                md = os.path.join(pd, mdl)
                if os.path.isdir(md):
                    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(md) for f in fs)
                    lm.append({"name": f"{pub}/{mdl}", "bytes": size})
    vol = {"available": False, "models": [], "bytes": None}
    if _run(["docker", "volume", "inspect", "open-webui-ollama"], timeout=10):
        out = _run(["docker", "run", "--rm", "-v", "open-webui-ollama:/v:ro", "busybox", "sh", "-c",
                    "ls /v/models/manifests/registry.ollama.ai/library 2>/dev/null; echo ---; du -sk /v/models 2>/dev/null | cut -f1"], timeout=90)
        if out is not None:
            names, _, kb = out.partition("---")
            vol = {"available": True, "models": [n for n in names.split() if n], "bytes": int(kb.strip()) * 1024 if kb.strip().isdigit() else None}
    groups = {}
    for src, items in (("Ollama", ol), ("LM Studio", lm), (msg('webui_volume', LANG_DEFAULT), [{"name": n, "bytes": None} for n in vol["models"]])):
        for it in items:
            groups.setdefault(norm(it["name"].split(":")[0]), []).append({"source": src, **it})
    dups = [{"key": k, "items": v} for k, v in groups.items() if len({x["source"] for x in v}) > 1]
    dups.sort(key=lambda d: -sum((x["bytes"] or 0) for x in d["items"]))
    return {"ollama": ol, "lmstudio": lm, "openwebui_volume": vol, "duplicates": dups,
            "note": msg('duplicate_note', LANG_DEFAULT)}


_LLM_BENCH = {"lock": threading.Lock(), "state": {"status": "idle"}}
LLM_HIST_FILE = os.path.join(HERE, "data", "llm-bench.json")


def llm_hist_load():
    try:
        with open(LLM_HIST_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def llm_hist_append(rec):
    h = llm_hist_load(); h.append(rec); h = h[-200:]
    os.makedirs(os.path.dirname(LLM_HIST_FILE), exist_ok=True)
    tmp = LLM_HIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, ensure_ascii=False)
    os.replace(tmp, LLM_HIST_FILE)


def llm_bench_start(model, num_predict=128):
    with _LLM_BENCH["lock"]:
        if _LLM_BENCH["state"].get("status") == "running":
            return False
        _LLM_BENCH["state"] = {"status": "running", "model": model, "num_predict": num_predict, "phase": msg('warming', LANG_DEFAULT),
                               "started": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_llm_bench_run, args=(model, num_predict), daemon=True).start()
    return True


def _llm_bench_run_concurrent(model, n, num_predict):
    """n 個請求同時送，總 tok/s = 所有回應的 eval_count 合計 ÷ 牆鐘時間。OLLAMA_NUM_PARALLEL 小於 n 時會排隊，數字會反映排隊。"""
    base = OLLAMA
    prompts = [f"Write {i+1} short paragraphs about the history of the number {i+7}. " * 2 for i in range(n)]
    try:
        warm = _http_json(base + "/api/generate", {"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1, "temperature": 0}}, timeout=600)
        if warm is None:
            raise RuntimeError(msg('warm_failed', LANG_DEFAULT))
        with _LLM_BENCH["lock"]:
            _LLM_BENCH["state"]["phase"] = msg('bench_concurrent_phase', LANG_DEFAULT, p0=n, p1=num_predict)
        results = [None] * n
        def one(i):
            results[i] = _http_json(base + "/api/generate", {"model": model, "prompt": prompts[i], "stream": False,
                                                            "options": {"num_predict": num_predict, "temperature": 0}}, timeout=1800)
        t0 = time.time()
        ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        [t.start() for t in ths]; [t.join() for t in ths]
        wall = time.time() - t0
        ok = [r for r in results if r]
        if not ok:   # 全部失敗：這不是成績，是失敗；不寫歷史
            raise RuntimeError(msg('bench_all_failed', LANG_DEFAULT, n=n))
        toks = sum(r.get("eval_count") or 0 for r in ok)
        per = [round((r.get("eval_count") or 0) / (r.get("eval_duration") or 1) * 1e9, 1) for r in ok]
        cfg = ollama_config()
        res = {"status": "done", "model": model, "finished": datetime.now().isoformat(timespec="seconds"), "concurrency": n,
               "num_predict": num_predict, "decode_tps": round(toks / wall, 1) if wall else None, "decode_tokens": toks,
               "per_request_tps": per, "wall_s": round(wall, 1), "failed": n - len(ok), "prefill_tps": None,
               "num_parallel": cfg.get("num_parallel"),
               "note": (msg('bench_partial_failed', LANG_DEFAULT, failed=n - len(ok), n=n) + " " if len(ok) < n else "") + msg('bench_concurrent_note', LANG_DEFAULT, p0=n, p1=num_predict, p2=cfg.get('num_parallel')) + (msg('requests_queued', LANG_DEFAULT) if (cfg.get("num_parallel") or 1) < n else msg('requests_parallel', LANG_DEFAULT))}
        for m in (_http_json(base + "/api/ps") or {}).get("models", []):
            if m.get("name") == model:
                det = m.get("details") or {}
                res.update({"size": m.get("size"), "quant": det.get("quantization_level"), "params": det.get("parameter_size"), "context": m.get("context_length")})
        llm_hist_append(res)
    except Exception as e:
        res = {"status": "error", "model": model, "error": str(e), "finished": datetime.now().isoformat(timespec="seconds")}
    with _LLM_BENCH["lock"]:
        _LLM_BENCH["state"] = res


def llm_bench_start_concurrent(model, n, num_predict=128):
    with _LLM_BENCH["lock"]:
        if _LLM_BENCH["state"].get("status") == "running":
            return False
        _LLM_BENCH["state"] = {"status": "running", "model": model, "concurrency": n, "num_predict": num_predict, "phase": msg('warming', LANG_DEFAULT),
                               "started": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_llm_bench_run_concurrent, args=(model, n, num_predict), daemon=True).start()
    return True


def _llm_bench_run(model, num_predict=128):
    """Ollama decode/prefill 基準：先暖機 1 token（把載入時間隔開），再量 128 token。
    數字直接取自 Ollama 回應的 eval_count/eval_duration，不是估的。"""
    base = LLM_TARGETS[0]["url"]
    prompt = "Explain, in plain prose without lists, why the sky appears blue during the day and red at sunset. " * 4
    try:
        warm = _http_json(base + "/api/generate", {"model": model, "prompt": "hi", "stream": False,
                                                    "options": {"num_predict": 1, "temperature": 0}}, timeout=600)
        if warm is None:
            raise RuntimeError(msg('warm_request_failed', LANG_DEFAULT))
        with _LLM_BENCH["lock"]:
            _LLM_BENCH["state"]["phase"] = msg('bench_phase', LANG_DEFAULT, p0=num_predict)
        r = _http_json(base + "/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                                 "options": {"num_predict": num_predict, "temperature": 0}}, timeout=900)
        if r is None:
            raise RuntimeError(msg('bench_failed', LANG_DEFAULT))
        ec, ed = r.get("eval_count") or 0, r.get("eval_duration") or 0
        pc, pd = r.get("prompt_eval_count") or 0, r.get("prompt_eval_duration") or 0
        # 模型資訊（大小／量化／參數量）從 /api/ps 抓，寫進歷史方便比較
        info = {}
        for m in (_http_json(base + "/api/ps") or {}).get("models", []):
            if m.get("name") == model:
                det = m.get("details") or {}
                info = {"size": m.get("size"), "size_vram": m.get("size_vram"), "quant": det.get("quantization_level"),
                        "params": det.get("parameter_size"), "context": m.get("context_length")}
        res = {"status": "done", "model": model, "finished": datetime.now().isoformat(timespec="seconds"),
               "decode_tps": round(ec / ed * 1e9, 1) if ed else None, "decode_tokens": ec,
               "prefill_tps": round(pc / pd * 1e9, 1) if pd else None, "prompt_tokens": pc,
               "load_ms": round((warm.get("load_duration") or 0) / 1e6), "total_ms": round((r.get("total_duration") or 0) / 1e6),
               "num_predict": num_predict, **info,
               "note": msg('bench_note', LANG_DEFAULT, p0=num_predict)}
        llm_hist_append(res)
    except Exception as e:
        res = {"status": "error", "model": model, "error": str(e), "finished": datetime.now().isoformat(timespec="seconds")}
    with _LLM_BENCH["lock"]:
        _LLM_BENCH["state"] = res


# ---------- 磁碟：誰在吃空間（掃描在背景執行緒，快取 10 分鐘）----------

HOME = os.path.expanduser("~")
DISK_ACTIONS = {  # 白名單：只有這些指令能被 /api/disk/action 觸發
    "docker_prune": ["docker", "image", "prune", "-f"],          # 只清 dangling 映像，不動有 tag 的
    "docker_builder_prune": ["docker", "builder", "prune", "-f"],
    # find -mindepth 1 -delete 含點開頭的隱藏檔（bash 的 * 不含）；用 && 串接，刪除失敗就不會印「已清空」、退出碼非 0
    "trash_empty": ["bash", "-c", "T=~/.local/share/Trash; mkdir -p \"$T/files\" \"$T/info\" && find \"$T/files\" \"$T/info\" -mindepth 1 -delete && echo " + msg("trash_cleared", LANG_DEFAULT) + " && du -sh \"$T\""],
    "npm_cache_clean": ["bash", "-c", "npm cache clean --force 2>&1 | tail -2; du -sh ~/.npm"],
}
_DISK = {"lock": threading.Lock(), "data": None, "ts": 0, "scanning": False}
DISK_TTL = 600


def _du(path):
    try:
        r = subprocess.run(["du", "-sxb", path], capture_output=True, text=True, timeout=120)
        return int(r.stdout.split()[0]) if r.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _du_children(path, limit=12):
    """path 底下第一層各項大小，由大到小。"""
    out = []
    try:
        names = os.listdir(path)
    except OSError:
        return out
    for n in names:
        full = os.path.join(path, n)
        if os.path.islink(full):
            continue
        v = _du(full)
        if v:
            out.append({"name": n, "path": full, "bytes": v})
    out.sort(key=lambda x: -x["bytes"])
    return out[:limit]


def _big_files(root, min_bytes=1 << 30, limit=25):
    try:
        r = subprocess.run(["find", root, "-xdev", "-type", "f", "-size", f"+{min_bytes // 1024}k", "-printf", "%s\t%p\n"],
                           capture_output=True, text=True, timeout=180)
        rows = [l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l]
        rows = [{"bytes": int(a), "path": b} for a, b in rows]
        rows.sort(key=lambda x: -x["bytes"])
        return rows[:limit]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []


def _docker_df():
    out = _run(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
    if not out:
        return None
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    vols = []
    v = _run(["docker", "volume", "ls", "-q"], timeout=30) or ""
    dfv = _run(["docker", "system", "df", "-v", "--format", "{{json .}}"], timeout=60)
    if dfv:
        try:
            j = json.loads(dfv.splitlines()[0]) if dfv.strip().startswith("{") else None
            for vol in (j or {}).get("Volumes", []) or []:
                vols.append({"name": vol.get("Name"), "size": vol.get("Size"), "links": vol.get("Links")})
        except (ValueError, IndexError, AttributeError):
            pass
    if not vols and dfv:
        # 舊版 docker 沒有 json，退回文字表
        txt = _run(["docker", "system", "df", "-v"], timeout=60) or ""
        sec = txt.split("VOLUME NAME", 1)
        if len(sec) > 1:
            for l in sec[1].splitlines()[1:]:
                parts = l.split()
                if len(parts) >= 3 and parts[0] != "":
                    vols.append({"name": parts[0], "links": parts[1], "size": parts[2]})
                elif not l.strip():
                    break
    return {"summary": rows, "volumes": vols}


def _ollama_models():
    j = _http_json("http://127.0.0.1:11434/api/tags")
    if not j:
        return None
    ms = [{"name": m.get("name"), "bytes": m.get("size") or 0, "modified": (m.get("modified_at") or "")[:10]} for m in j.get("models", [])]
    ms.sort(key=lambda x: -x["bytes"])
    return ms


def _apt_autoremovable():
    out = _run(["apt-get", "-s", "autoremove"], timeout=60) or ""
    return [l.split()[1] for l in out.splitlines() if l.startswith("Remv ")]


def disk_scan():
    with _DISK["lock"]:
        if _DISK["scanning"]:
            return
        _DISK["scanning"] = True
    try:
        st = os.statvfs("/")
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        cats = []
        def cat(key, label, b, note="", action=None, items=None):
            cats.append({"key": key, "label": label, "bytes": b or 0, "note": note, "action": action, "items": items or []})
        # Ollama（系統服務，模型在 /usr/share/ollama）
        om = _ollama_models()
        cat("ollama", msg('ollama_models', LANG_DEFAULT), _du("/usr/share/ollama"),
            msg('ollama_disk_note', LANG_DEFAULT), None, om)
        # LM Studio
        lm = os.path.join(HOME, ".lmstudio", "models")
        if os.path.isdir(lm):
            items = []
            for pub in _du_children(lm, 30):
                for mdl in _du_children(pub["path"], 30):
                    items.append({"name": f"{pub['name']}/{mdl['name']}", "bytes": mdl["bytes"], "path": mdl["path"]})
            items.sort(key=lambda x: -x["bytes"])
            cat("lmstudio", msg('lmstudio_models', LANG_DEFAULT), _du(lm), msg('lmstudio_disk_note', LANG_DEFAULT), None, items[:30])
        # Docker
        dk = _docker_df()
        if dk:
            def gb(x):
                m = re.match(r"([\d.]+)\s*([KMGT]?B)", str(x or ""))
                if not m: return 0
                return float(m.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
            total_dk = sum(gb(r.get("Size")) for r in dk["summary"])
            recl = sum(gb(r.get("Reclaimable", "").split(" ")[0]) for r in dk["summary"])
            items = [{"name": msg('docker_count', LANG_DEFAULT, p0=r.get('Type'), p1=r.get('TotalCount') or r.get('Total'), p2=r.get('Reclaimable')), "bytes": gb(r.get("Size"))} for r in dk["summary"]]
            items += [{"name": msg('volume', LANG_DEFAULT, p0=v['name']), "bytes": gb(v.get("size")), "note": msg('volume_links', LANG_DEFAULT, p0=v.get('links'))} for v in dk["volumes"]]
            cat("docker", "Docker", total_dk, msg('docker_reclaim', LANG_DEFAULT, p0=format(recl/1e9, '.1f')), "docker_prune", items)
        # snap 使用者資料（Steam 等）
        sn = os.path.join(HOME, "snap")
        if os.path.isdir(sn):
            cat("snap_home", msg('snap_data', LANG_DEFAULT), _du(sn), msg('snap_data_note', LANG_DEFAULT), None, _du_children(sn, 10))
        cat("flatpak", msg('flatpak_system', LANG_DEFAULT), _du("/var/lib/flatpak"), msg('flatpak_note', LANG_DEFAULT), None)
        cat("snapd", msg('snap_system', LANG_DEFAULT), _du("/var/lib/snapd"), msg('snap_retention', LANG_DEFAULT), None)
        # 只量 archives：apt clean 清的就是這裡；同層的 pkgcache.bin／srcpkgcache.bin 是套件索引，清不掉也不該清
        cat("apt_cache", msg('apt_cache', LANG_DEFAULT), _du("/var/cache/apt/archives"), msg('apt_cache_note', LANG_DEFAULT), "apt_clean")
        ar = _apt_autoremovable()
        cat("apt_autoremove", msg('apt_autoremove', LANG_DEFAULT), None, msg('autoremove_count', LANG_DEFAULT, p0=len(ar)) if ar else msg('none', LANG_DEFAULT), "apt_autoremove" if ar else None, [{"name": n, "bytes": 0} for n in ar])
        cat("journal", msg('journal', LANG_DEFAULT), _du("/var/log/journal"), msg('journal_note', LANG_DEFAULT), None)
        cat("cache", "~/.cache", _du(os.path.join(HOME, ".cache")), msg('app_cache', LANG_DEFAULT), None, _du_children(os.path.join(HOME, ".cache"), 8))
        cat("npm", "~/.npm", _du(os.path.join(HOME, ".npm")), msg('npm_cache', LANG_DEFAULT), "npm_cache_clean")
        tr = os.path.join(HOME, ".local", "share", "Trash")
        cat("trash", msg('trash', LANG_DEFAULT), _du(tr), "", "trash_empty")
        home_top = _du_children(HOME, 15)
        big = _big_files(HOME)
        data = {"total": total, "free": free, "used": total - free, "categories": cats, "home_top": home_top, "big_files": big,
                "scanned": datetime.now().isoformat(timespec="seconds")}
        with _DISK["lock"]:
            _DISK.update(data=data, ts=time.time())
    finally:
        with _DISK["lock"]:
            _DISK["scanning"] = False


def disk_status(force=False):
    with _DISK["lock"]:
        stale = not _DISK["data"] or time.time() - _DISK["ts"] > DISK_TTL
        scanning = _DISK["scanning"]
        data = _DISK["data"]
    if (stale or force) and not scanning:
        threading.Thread(target=disk_scan, daemon=True).start()
        scanning = True
    st = os.statvfs("/")
    live = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    return {"scanning": scanning, "data": data, "live": live}


# ---------- GPU 低功耗卡死偵測（論壇第 3 大抱怨：PD 控制器韌體卡住 → SM 釘在 611 MHz、功耗十幾瓦）----------
# 判定：連續 30 秒（6 次取樣）「GPU 使用率 ≥ 20% 但 SM 時脈 ≤ 800 MHz」，或硬體降速／功率煞車旗標持續亮著。
# 閒置時時脈本來就低，所以一定要配合使用率，避免誤報。

_GPU_WATCH = {"lock": threading.Lock(), "samples": [], "alert": None, "last_notify": 0}
GPU_STUCK_CLOCK_MHZ = 800
GPU_STUCK_MIN_UTIL = 20
GPU_STUCK_SAMPLES = 6


def gpu_stuck_evaluate(samples):
    """純函式，方便測試。samples: 最近的 [{sm_mhz, util_pct, power_w, event_reasons}]。回 alert dict 或 None。"""
    recent = [x for x in samples[-GPU_STUCK_SAMPLES:] if x.get("sm_mhz") is not None and x.get("util_pct") is not None]
    if len(recent) < GPU_STUCK_SAMPLES:
        return None
    low_clock = all(x["sm_mhz"] <= GPU_STUCK_CLOCK_MHZ and x["util_pct"] >= GPU_STUCK_MIN_UTIL for x in recent)
    hw_flags = all(set(x.get("event_reasons") or []) & GPU_STUCK_BAD_REASONS for x in recent)
    if not (low_clock or hw_flags):
        return None
    last = recent[-1]
    return {
        "kind": "low_clock" if low_clock else "hw_slowdown",
        "sm_mhz": last["sm_mhz"], "util_pct": last["util_pct"], "power_w": last.get("power_w"),
        "reasons": sorted(set().union(*[set(x.get("event_reasons") or []) for x in recent])),
        "message": (msg('gpu_stuck_load', LANG_DEFAULT, p0=last['util_pct'], p1=last['sm_mhz'], p2=last.get('power_w') or '—')
                    if low_clock else msg('gpu_stuck_flags', LANG_DEFAULT, p0='、'.join(sorted(set().union(*[set(x.get('event_reasons') or []) for x in recent]))))),
        "advice": msg('gpu_stuck_advice', LANG_DEFAULT),
    }


def _gpu_watch():
    while True:
        try:
            g = _gpu_live()
            if g:
                with _GPU_WATCH["lock"]:
                    _GPU_WATCH["samples"].append({"t": time.time(), **g})
                    _GPU_WATCH["samples"] = _GPU_WATCH["samples"][-24:]
                    alert = gpu_stuck_evaluate(_GPU_WATCH["samples"])
                    if alert and not _GPU_WATCH["alert"]:
                        alert["since"] = datetime.now().isoformat(timespec="seconds")
                    elif alert and _GPU_WATCH["alert"]:
                        alert["since"] = _GPU_WATCH["alert"]["since"]
                    _GPU_WATCH["alert"] = alert
                    notify = alert and time.time() - _GPU_WATCH["last_notify"] > 3600
                    if notify:
                        _GPU_WATCH["last_notify"] = time.time()
                if notify:
                    subprocess.run(["notify-send", "-u", "critical", "-a", "Spark Center", msg('gpu_stuck_title', LANG_DEFAULT),
                                    alert["message"] + msg('gpu_help', LANG_DEFAULT) % PORT], timeout=10)
        except Exception:
            pass
        time.sleep(5)


def gpu_alert():
    with _GPU_WATCH["lock"]:
        return _GPU_WATCH["alert"]


# ---------- 磁碟快滿通知（背景，每 10 分鐘；≥90% 每 6 小時提醒一次）----------

def _disk_watch():
    last = 0
    while True:
        try:
            st = os.statvfs("/")
            pct = (1 - st.f_bavail / st.f_blocks) * 100
            if pct >= 90 and time.time() - last > 6 * 3600:
                free_gb = st.f_bavail * st.f_frsize / 1e9
                subprocess.run(["notify-send", "-u", "critical", "-a", "Spark Center", msg('disk_full_title', LANG_DEFAULT),
                                msg('disk_full', LANG_DEFAULT, p0=format(pct, '.0f'), p1=format(free_gb, '.0f'), p2=PORT)], timeout=10)
                last = time.time()
        except Exception:
            pass
        time.sleep(600)


# ---------- 韌體（fwupd）：不信 Dashboard 的回報，直接問 fwupd ----------
# 論壇第 1 大抱怨的根因：Dashboard 說韌體更新成功，fwupdmgr 卻顯示「expected 0x507 got 0x500」。
# 這裡把「現在版本」和「LVFS 最新版」與「歷史結果」並排，對不上就標出來。

_FW = {"ts": 0, "data": None, "lock": threading.Lock()}
FW_TTL = 600
FW_STATE = {0: msg('fw_unknown', LANG_DEFAULT), 1: msg('fw_pending', LANG_DEFAULT), 2: msg('fw_success', LANG_DEFAULT), 3: msg('failed', LANG_DEFAULT), 4: msg('fw_reboot', LANG_DEFAULT), 5: msg('fw_failed_reboot', LANG_DEFAULT), 6: msg('transaction_failed', LANG_DEFAULT)}
DISK_ACTIONS.update({
    "fwupd_refresh": ["fwupdmgr", "refresh", "--force"],
    "fwupd_update": ["fwupdmgr", "update", "-y", "--no-reboot-check"],   # polkit 會跳密碼；裝完不自動重開
})


def _fw_json(args, timeout=90):
    out = _run(["fwupdmgr"] + args + ["--json"], timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def fwupd_status(force=False):
    with _FW["lock"]:
        if not force and _FW["data"] and time.time() - _FW["ts"] < FW_TTL:
            return _FW["data"]
    if not _run(["which", "fwupdmgr"], timeout=3):
        return {"available": False, "note": msg('fwupd_missing', LANG_DEFAULT)}
    # 三支查詢各自可能失敗（逾時、LVFS 連不上、fwupd 壞掉）。失敗不能變成空清單，否則畫面會說「已是最新」。
    q_devs, q_ups, q_hist = _fw_json(["get-devices"]), _fw_json(["get-updates"], timeout=120), _fw_json(["get-history"])
    failed = [n for n, q in (("get-devices", q_devs), ("get-updates", q_ups), ("get-history", q_hist)) if q is None]
    if q_devs is None:   # 連裝置清單都拿不到，什麼都不能說
        return {"available": True, "error": msg("fwupd_query_failed", LANG_DEFAULT, what=", ".join(failed)), "devices": [],
                "updates": None, "mismatches": None, "pending": None, "generated": datetime.now().isoformat(timespec="seconds")}
    devs = q_devs.get("Devices", [])
    ups = (q_ups or {}).get("Devices", [])
    hist = (q_hist or {}).get("Devices", [])
    latest = {}   # DeviceId → 可升級的最新版本
    for d in ups:
        rels = d.get("Releases") or []
        if rels:
            latest[d.get("DeviceId")] = rels[0].get("Version")
    hist_by = {}
    for h in hist:
        rel = (h.get("Releases") or [{}])[0]
        hist_by.setdefault(h.get("DeviceId"), []).append({
            "old": h.get("VersionOld") or h.get("Version"), "new": rel.get("Version"), "state": h.get("UpdateState"),
            "state_zh": FW_STATE.get(h.get("UpdateState"), str(h.get("UpdateState"))), "error": h.get("UpdateError"),
            "when": datetime.fromtimestamp(h["Modified"]).isoformat(timespec="minutes") if h.get("Modified") else None,
            "summary": rel.get("Summary"), "urgency": rel.get("Urgency")})
    rows = []
    for d in devs:
        flags = d.get("Flags") or []
        if "updatable" not in flags and "updatable-hidden" not in flags:
            continue
        did = d.get("DeviceId")
        cur = d.get("Version")
        hs = hist_by.get(did, [])
        last = hs[0] if hs else None
        # 對不上：歷史說成功升到 X，但現在版本不是 X → 就是論壇那種「默默失敗」
        mismatch = bool(last and last["state"] == 2 and last["new"] and cur and last["new"] != cur)
        rows.append({
            "id": did, "name": d.get("Name"), "version": cur, "plugin": d.get("Plugin"), "summary": d.get("Summary"),
            "vendor": d.get("Vendor"), "latest": latest.get(did), "update_available": did in latest,
            "needs_reboot": "needs-reboot" in flags, "internal": "internal" in flags, "hidden": "updatable-hidden" in flags,
            "history": hs, "mismatch": mismatch,
            "pending": bool(last and last["state"] in (1, 4)),
        })
    rows.sort(key=lambda r: (not r["mismatch"], not r["update_available"], r["hidden"], r["name"] or ""))
    # get-updates 失敗：更新數未知（None），不是 0；get-history 失敗：對不上／等重開機未知
    data = {"available": True, "devices": rows,
            "updates": None if q_ups is None else sum(1 for r in rows if r["update_available"]),
            "mismatches": None if q_hist is None else sum(1 for r in rows if r["mismatch"]),
            "pending": None if q_hist is None else sum(1 for r in rows if r["pending"]),
            "error": msg("fwupd_query_failed", LANG_DEFAULT, what=", ".join(failed)) if failed else None,
            "generated": datetime.now().isoformat(timespec="seconds"),
            "fwupd_version": (lambda v: (re.search(r"runtime\s+org\.freedesktop\.fwupd\s+(\S+)", v) or [None, None])[1])(_run(["fwupdmgr", "--version"], timeout=5) or "")}
    with _FW["lock"]:
        _FW.update(ts=0 if failed else time.time(), data=data)   # 失敗的不快取，下次再查
    return data


SNAP_ACTIVE = ("Do", "Doing", "Undo", "Undoing", "Wait", "Abort")
SNAP_TASK_STATES = SNAP_ACTIVE + ("Done", "Error", "Hold", "Undone")


class _SnapdConn(http.client.HTTPConnection):
    """snapd 的 REST API 走 unix socket。/run/snapd.socket 是 0666，讀取不需要 root。"""

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect("/run/snapd.socket")
        self.sock = sock


def _snapd_get(path, timeout=10):
    try:
        c = _SnapdConn("localhost", timeout=timeout)
        c.request("GET", path, headers={"Host": "localhost", "Accept": "application/json"})
        body = json.loads(c.getresponse().read().decode())
        c.close()
        return body.get("result") if body.get("status-code", 500) < 400 else None
    except Exception:
        return None


def snap_running_apps(name):
    """哪些程序正在使用這個 snap。snapd 不會更新有程式在跑的 snap，先查出來才不會白要一次密碼。
    exe 路徑是主要依據，讀不到（別的使用者的程序）再看 cgroup 標記。"""
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        exe = ""
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            pass
        hit = exe.startswith(f"/snap/{name}/")
        if not hit:
            try:
                with open(f"/proc/{pid}/cgroup") as f:
                    hit = f"snap.{name}." in f.read()
            except OSError:
                hit = False
        if hit:
            cmd = ""
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                pass
            hits.append({"pid": int(pid), "exe": exe, "cmd": cmd[:140],
                         "app": os.path.basename(exe or (cmd.split(" ")[0] if cmd else "?"))})
    return hits


def snap_change_for(names):
    """找 snapd 裡跟這些 snap 有關、還沒結束的變更 id。唯讀，不需要 root。"""
    r = _snapd_get("/v2/changes?select=in-progress")
    if isinstance(r, list):
        for ch in r:
            if ch.get("status") in SNAP_ACTIVE and any(f'"{n}"' in (ch.get("summary") or "") for n in names):
                return ch.get("id")
        return None
    out = _run(["snap", "changes"], timeout=20) or ""
    for line in out.splitlines():
        m = re.match(r"^(\d+)\s+(\S+)\s", line)
        if m and m.group(2) in SNAP_ACTIVE and any(f'"{n}"' in line for n in names):
            return m.group(1)
    return None


def snap_change_progress(cid):
    """讀真進度。優先用 snapd API：下載任務的 progress 是精確的位元組數，能顯示 MB。
    API 不可用時退回解析 snap tasks 的文字（只拿得到百分比）。"""
    r = _snapd_get(f"/v2/changes/{cid}")
    if isinstance(r, dict) and r.get("tasks"):
        tasks = r["tasks"]
        total = len(tasks)
        done = sum(1 for t in tasks if t.get("status") == "Done")
        cur = next((t for t in tasks if t.get("status") in ("Doing", "Undoing")), None)
        pct = bytes_done = bytes_total = None
        doing = cur.get("summary") if cur else None
        if cur:
            pr = cur.get("progress") or {}
            d, t_ = pr.get("done"), pr.get("total")
            if isinstance(d, int) and isinstance(t_, int) and t_ > 1:
                pct = d / t_ * 100
                if t_ > (1 << 20):          # 大於 1 MB 才當位元組看，其餘是任務計數
                    bytes_done, bytes_total = d, t_
        overall = round(done / total * 100) if total else None
        fail_log = next(([l for l in (t.get("log") or [])][-3:] for t in tasks if t.get("status") == "Error"), [])
        return {"status": r.get("status", "Doing"), "done": done, "total": total, "doing": doing,
                "task_percent": round(pct) if pct is not None else None, "task_ratio": overall,
                "bytes_done": bytes_done, "bytes_total": bytes_total, "err": r.get("err"), "fail_log": fail_log,
                "percent": round(pct) if pct is not None else overall}
    out = _run(["snap", "tasks", cid], timeout=20) or ""
    total = done = 0
    doing = None
    pct = None
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(.*)$", line)
        if not m or m.group(1) not in SNAP_TASK_STATES:
            continue
        st = m.group(1)
        summary = re.split(r"\s{2,}", m.group(2))[-1].strip()
        total += 1
        if st == "Done":
            done += 1
        elif st in ("Doing", "Undoing") and doing is None:
            doing = summary
            p = re.search(r"\((\d+(?:\.\d+)?)%\)", summary)
            if p:
                pct = float(p.group(1))
    head = _run(["snap", "changes"], timeout=20) or ""
    status = next((l.split()[1] for l in head.splitlines() if l.startswith(cid + " ")), "Doing")
    overall = round(done / total * 100) if total else None
    # 進度條優先用「目前這步自己的百分比」：下載佔掉幾乎全部時間，用任務數比例會卡在個位數再突然跳到尾聲
    return {"status": status, "done": done, "total": total, "doing": doing,
            "task_percent": round(pct) if pct is not None else None, "task_ratio": overall,
            "bytes_done": None, "bytes_total": None, "err": None, "fail_log": [],
            "percent": round(pct) if pct is not None else overall}


def GB_(b):
    return "—" if b is None else f"{b/1e9:.1f} GB"


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "SparkCenter/0.2"

    def log_message(self, fmt, *args):  # 安靜一點，只記錯誤
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _request_lang(self):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        requested = (query.get("lang") or [None])[0]
        if requested is None:
            requested = self.headers.get("Accept-Language", LANG_DEFAULT).split(",", 1)[0].split(";", 1)[0]
        return "zh-TW" if requested.lower().startswith("zh") else "en"

    def _json(self, obj, code=200):
        body = json.dumps(_localized_response(obj, self.lang), ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        self.lang = self._request_lang()
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/"):
            # 前端拆成 index.html＋static/（CSS 一檔、JS 依分頁），仍由這支服務直接提供，沒有建置步驟。
            # 只允許 static/ 底下的檔案；no-store 是因為本機服務更新後不該看到舊版。
            base = os.path.realpath(os.path.join(HERE, "static"))
            fp = os.path.realpath(os.path.join(HERE, path.lstrip("/")))
            ctype = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}.get(os.path.splitext(fp)[1])
            if not fp.startswith(base + os.sep) or not ctype or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/snapicon/"):
            name = path[len("/snapicon/"):]
            body, ctype = None, "image/png"
            if re.fullmatch(r"[a-z0-9-]+", name):
                try:
                    c = _SnapdConn("localhost", timeout=5)
                    c.request("GET", f"/v2/icons/{name}/icon", headers={"Host": "localhost"})
                    r = c.getresponse()
                    if r.status == 200:
                        body, ctype = r.read(), r.getheader("Content-Type") or ctype
                    c.close()
                except Exception:
                    body = None
            if not body:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/appicon/"):
            fp = _ICON_TOKENS.get(path[len("/appicon/"):])
            if not fp or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if fp.endswith(".svg") else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/manifest.webmanifest", "/icon-256.png", "/icon-128.png", "/icon.svg"):
            # Chrome app 模式的視窗／工作列圖示來自 web manifest
            if path == "/manifest.webmanifest":
                body = json.dumps({"name": "Spark Center", "short_name": "Spark Center", "start_url": "/", "display": "standalone",
                                   "background_color": "#111312", "theme_color": "#111312",
                                   "icons": [{"src": "/icon-256.png", "sizes": "256x256", "type": "image/png"},
                                             {"src": "/icon-128.png", "sizes": "128x128", "type": "image/png"},
                                             {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}).encode()
                ctype = "application/manifest+json"
            else:
                fn = {"/icon-256.png": "spark-center-256.png", "/icon-128.png": "spark-center-128.png", "/icon.svg": "spark-center.svg"}[path]
                with open(os.path.join(HERE, "app", fn), "rb") as f:
                    body = f.read()
                ctype = "image/svg+xml" if fn.endswith(".svg") else "image/png"
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif path == "/api/updates":
            try:
                self._json({
                    "ok": True,
                    "items": list_updates(),
                    "dashboard": dashboard_equivalent(),
                    "reboot": reboot_status(),
                    "last_refresh": last_refresh(),
                    "job_running": JOB.snapshot()["status"] == "running",
                })
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/job":
            self._json(JOB.snapshot())
        elif path == "/api/history":
            self._json({"ok": True, "entries": apt_history()})
        elif path == "/api/reboot":
            self._json(reboot_status())
        elif path == "/api/machine":
            # 頂欄的機型：直接讀 DMI，不寫死。ASUSTeK COMPUTER INC. 這種長名縮成常見寫法，其餘照原文。
            def dmi(k):
                try:
                    return open(f"/sys/class/dmi/id/{k}", encoding="utf-8", errors="replace").read().strip()
                except OSError:
                    return ""
            vendor = dmi("sys_vendor")
            vendor = re.sub(r"^ASUSTeK COMPUTER INC\.$", "ASUS", vendor, flags=re.I)
            vendor = re.sub(r"^(Dell Inc\.|HP Inc\.|Hewlett-Packard|Gigabyte Technology Co\., Ltd\.|Acer|NVIDIA)$",
                            lambda m: {"dell inc.": "Dell", "hp inc.": "HP", "hewlett-packard": "HP", "gigabyte technology co., ltd.": "GIGABYTE"}.get(m.group(1).lower(), m.group(1)), vendor, flags=re.I)
            self._json({"readonly": READONLY, "ok": True, "vendor": vendor, "product": dmi("product_name"), "family": dmi("product_family")})
        elif path == "/api/dashboard":
            # DGX Dashboard 的網址：埠號照 /usr/bin/dgx-dashboard 的邏輯讀 ports.env，讀不到就 11000
            port = 11000
            try:
                for line in open("/opt/nvidia/dgx-dashboard-service/ports.env", encoding="utf-8"):
                    m = re.match(r"\s*(?:export\s+)?DGX_DASHBOARD_PORT\s*=\s*\"?(\d+)", line)
                    if m:
                        port = int(m.group(1))
            except OSError:
                pass
            self._json({"ok": True, "url": f"http://localhost:{port}/", "installed": os.path.exists("/usr/bin/dgx-dashboard"),
                        "icon": os.path.isfile(DGX_DASHBOARD_ICON)})
        elif path == "/dashboard-icon":
            if not os.path.isfile(DGX_DASHBOARD_ICON):
                self.send_response(404); self.end_headers(); return
            with open(DGX_DASHBOARD_ICON, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/node':
            try:
                self._json(node_status())
            except Exception as e:
                self._json({'ok': False, 'error': node_error(str(e))}, 500)
        elif path == "/api/npm":
            try:
                self._json(npm_status(force="force=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/rollback":
            # 附上目前安裝的版本：降回之後那筆紀錄還在，前端要靠這個判斷「已經是這版」而不再給按鈕
            jobs = _rollback_index_load()
            try:
                cache = apt.Cache()
                npm_cur = None
                for j in jobs:
                    for p in j.get("packages", []):
                        if p.get("kind") == "npm":
                            if npm_cur is None:
                                npm_cur = {e["name"]: e.get("current") for e in npm_status().get("packages", [])}
                            p["installed"] = npm_cur.get(p.get("name"))
                            continue
                        pkg = cache.get(p.get("name"))
                        p["installed"] = pkg.installed.version if pkg and pkg.installed else None
            except Exception:
                pass
            self._json({"ok": True, "jobs": jobs, "keep": ROLLBACK_KEEP, "max_mb": ROLLBACK_MAX_MB})
        elif path == "/api/autostart":
            self._json(autostart_status())
        elif path == "/api/hardware":
            try:
                force = "force=1" in (self.path.split("?", 1) + [""])[1]
                d = hardware_static(force=force)
                self._json({"ok": True, **d, "cached_age_s": int(time.time() - _HW_CACHE["ts"])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/rear":
            try:
                self._json({"ok": True, **rear_panel(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/usbports":
            try:
                self._json({"ok": True, "ports": usb_ports(), "ts": time.time()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/wifi/channels":
            try:
                self._json(wifi_channel_analysis(rescan="rescan=1" in (self.path.split("?", 1) + [""])[1]))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/wifi":
            try:
                self._json({"ok": True, "live": _wifi_live(), "aps": _wifi_aps(), "bt_connected": _bt_connected(), "events_24h": _wifi_events_24h()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/hardware/live":
            try:
                self._json({"ok": True, **hardware_live()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/disk":
            try:
                self._json({"ok": True, **disk_status(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/firmware":
            try:
                self._json({"ok": True, **fwupd_status(force="force=1" in (self.path.split("?", 1) + [""])[1])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/llm":
            try:
                with _LLM_BENCH["lock"]:
                    bench = dict(_LLM_BENCH["state"])
                servers = llm_status()
                for sv in servers:
                    if sv["kind"] == "Ollama":
                        for m in sv["installed"]:
                            m.update({k: v for k, v in ollama_show(m["name"]).items() if k in ("context", "capabilities")})
                self._json({"ok": True, "servers": servers, "bench": bench, "history": llm_hist_load()[::-1][:40], "config": ollama_config()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/llm/stores":
            try:
                self._json({"ok": True, **llm_stores()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/apps":
            force = "force=1" in (self.path.split("?", 1) + [""])[1]
            try:
                self._json({"ok": True, **list_apps(force=force)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/changelog":
            from urllib.parse import parse_qs
            q = parse_qs((self.path.split("?", 1) + [""])[1])
            source = (q.get("source") or [""])[0]
            ident = (q.get("id") or [""])[0]
            ver = (q.get("installed") or [""])[0]
            if not source or not ident:
                return self._json({"ok": False, "error": msg('missing_source_id', LANG_DEFAULT)}, 400)
            self._json(get_changelog(source, ident, ver))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _origin_problem(self):
        """修改型請求的來源驗證。只綁 127.0.0.1 擋不住瀏覽器裡任何網頁對本機發的跨站 POST（text/plain 的
        簡單請求不會 preflight，直接送到）。三道關：Host 必須是本機＋本埠；有 Origin 就必須是自己；
        Content-Type 必須是 application/json（強迫瀏覽器 preflight，而本服務不回應 OPTIONS，跨站就死在瀏覽器裡）。"""
        # 只認本機位址，埠號不限：SSH 轉埠（ssh -L、NVIDIA Sync 的 Custom）在遠端那台常用別的本機埠號，
        # 瀏覽器送來的 Host 就是那個埠。DNS rebinding 靠的是非本機的主機名，放寬埠號不影響這道防線。
        def _local(hostport):
            h = hostport.rsplit(":", 1)[0] if re.search(r":\d+$", hostport) else hostport
            return h in ("127.0.0.1", "localhost", "[::1]")
        host = (self.headers.get("Host") or "").strip().lower()
        if not _local(host):
            return f"Host={host or '(none)'}"
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and not (origin.startswith("http://") and _local(origin[len("http://"):])):
            return f"Origin={origin}"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return f"Content-Type={ctype or '(none)'}"
        return None

    def do_POST(self):
        self.lang = self._request_lang()
        path = self.path.split("?", 1)[0]
        if READONLY:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"ok": False, "error": msg("readonly_mode", self.lang)}, 403)
        why = self._origin_problem()
        if why:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"ok": False, "error": msg("bad_origin", self.lang, why=why)}, 403)
        data = self._body()
        if path == "/api/simulate":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            return self._json(simulate(names))
        if path == "/api/install":
            names = [n for n in data.get("packages", []) if isinstance(n, str)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            sim = simulate(names)
            if not sim["ok"]:
                return self._json(sim, 400)
            # 鎖看模擬的實際變更（含相依帶入、nodejs:arm64 這種架構限定名），不只看使用者送來的名字
            if any(c["name"].split(":")[0] == "nodejs" for c in sim["changes"]) and _node_flow_pending():
                return self._json({"ok": False, "error": msg('node_flow_locked', LANG_DEFAULT)}, 409)
            if not JOB.start("install", names):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path in ('/api/node/preview', '/api/node/action'):
            try:
                action, target = data.get('action'), data.get('target')
                plan = node_plan(action, target)
                if not plan.get('ok') or path.endswith('/preview'):
                    return self._json(plan, 200 if plan.get('ok') else 400)
                if data.get('confirmation') != plan['confirmation']:
                    return self._json({'ok': False, 'error': msg('node_plan_changed', LANG_DEFAULT)}, 409)
                if not JOB.start('node_source', [action, str(target), plan['token']]):
                    return self._json({'ok': False, 'error': msg('job_running', LANG_DEFAULT)}, 409)
                return self._json({'ok': True})
            except Exception as e:
                return self._json({'ok': False, 'error': node_error(str(e))}, 400)
        if path == "/api/npm/restart":
            unit = str(data.get("unit") or "")
            # 只准重啟「目前正在執行某個 npm 全域套件」的 systemd 使用者服務，名稱從即時掃描來，不接受任意單元
            st = npm_status(force=True)
            allowed = {r["unit"] for e in st.get("packages", []) for r in e.get("running", []) if r.get("unit")}
            if not re.fullmatch(r"[A-Za-z0-9@._-]+\.service", unit) or unit not in allowed:
                return self._json({"ok": False, "error": msg("npm_restart_bad_unit", LANG_DEFAULT, unit=unit)}, 400)
            r = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, text=True, timeout=90, env=_ENV_C)
            if r.returncode != 0:
                return self._json({"ok": False, "error": msg("npm_restart_failed", LANG_DEFAULT, unit=unit, err=(r.stderr or r.stdout).strip()[:200])}, 500)
            _NPM["ts"] = 0
            act = (_run(["systemctl", "--user", "is-active", unit], timeout=20) or "").strip()
            return self._json({"ok": True, "unit": unit, "active": act})
        if path == "/api/npm/update":
            names = [n for n in data.get("names", []) if isinstance(n, str) and re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", n)]
            if not names:
                return self._json({"ok": False, "error": msg('no_pkgs', LANG_DEFAULT)}, 400)
            if not JOB.start("npm", names):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/rollback":
            job_id, name = str(data.get("job") or ""), str(data.get("name") or "")
            if not re.fullmatch(r"\d{8}T\d{6}", job_id) or (name and not re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._+-]*", name)):
                return self._json({"ok": False, "error": msg("rollback_not_found", LANG_DEFAULT)}, 400)
            ents = rollback_npm_entries(job_id, name or None)
            if ents:   # npm：沒有相依模擬，registry 直接重裝指定版本；照樣先展示變更再確認
                sim = {"ok": True, "changes": [{"name": e["name"], "action": "downgrade", "from": e.get("new") or "", "to": e["old"], "requested": True} for e in ents]}
                if data.get("simulate"):
                    return self._json(sim)
                if not JOB.start("rollback_npm", [job_id, name]):
                    return self._json({"ok": False, "error": msg("job_running", LANG_DEFAULT)}, 409)
                return self._json({"ok": True})
            paths = [fp for fp, _ in rollback_deb_paths(job_id, name or None)]
            if not paths:
                return self._json({"ok": False, "error": msg("rollback_not_found", LANG_DEFAULT)}, 404)
            sim = rollback_simulate(paths)   # 降回也走「模擬 → 展示實際變更 → 確認」，會移除別的套件就不做
            if data.get("simulate") or not sim["ok"]:
                return self._json(sim, 200 if sim["ok"] else 400)
            if not JOB.start("rollback", [job_id, name]):
                return self._json({"ok": False, "error": msg("job_running", LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/autostart":
            try:
                return self._json(autostart_set(bool(data.get("enabled"))))
            except OSError as e:
                return self._json({"ok": False, "error": msg('autostart_write_failed', LANG_DEFAULT, p0=e)}, 500)
        if path == "/api/hardware/usbc-map":
            # {"slot": 0-3, "controller": "NVDA8000:0x" | null, "note": "..."}；或 {"reset": true}
            if data.get("reset"):
                usbc_map_save({}); return self._json({"ok": True, "calib": {}})
            slot, ctrl = data.get("slot"), data.get("controller")
            if slot not in (0, 1, 2, 3):
                return self._json({"ok": False, "error": msg('slot_invalid', LANG_DEFAULT)}, 400)
            if ctrl is not None and not re.fullmatch(r"NVDA800[01]:0\d", str(ctrl)):
                return self._json({"ok": False, "error": msg('controller_invalid', LANG_DEFAULT)}, 400)
            m = usbc_map_load()
            if ctrl is None and not data.get("note"):
                m.pop(str(slot), None)
            else:
                m[str(slot)] = {"controller": ctrl, "note": (data.get("note") or "")[:80], "source": (data.get("source") or "manual")[:20],
                                "at": datetime.now().isoformat(timespec="seconds")}
            usbc_map_save(m)
            return self._json({"ok": True, "calib": m})
        if path in ("/api/llm/load", "/api/llm/unload"):
            model = data.get("model")
            if not model or not isinstance(model, str):
                return self._json({"ok": False, "error": msg('missing_model', LANG_DEFAULT)}, 400)
            ka = "0" if path.endswith("unload") else (data.get("keep_alive") or "5m")
            r = _http_json(OLLAMA + "/api/generate", {"model": model, "keep_alive": ka}, timeout=600)
            return self._json({"ok": r is not None, "error": None if r is not None else msg('ollama_no_response', LANG_DEFAULT)}, 200 if r is not None else 502)
        if path == "/api/llm/pull":
            model = data.get("model")
            if not model or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._/:-]+", model):
                return self._json({"ok": False, "error": msg('model_invalid', LANG_DEFAULT)}, 400)
            ok = JOB.start("ollama_pull", [model])
            return self._json({"ok": ok} if ok else {"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 200 if ok else 409)
        if path == "/api/llm/bench_concurrent":
            model, n = data.get("model"), data.get("concurrency", 2)
            if not model or n not in (1, 2, 4, 8):
                return self._json({"ok": False, "error": msg('concurrency_invalid', LANG_DEFAULT)}, 400)
            npred = data.get("num_predict", 128)
            if npred not in (64, 128, 256, 512):
                return self._json({"ok": False, "error": msg('num_predict_invalid', LANG_DEFAULT)}, 400)
            ok = llm_bench_start_concurrent(model, n, npred)
            return self._json({"ok": ok} if ok else {"ok": False, "error": msg('bench_running', LANG_DEFAULT)}, 200 if ok else 409)
        if path == "/api/llm/bench":
            model = data.get("model")
            if not model or not isinstance(model, str):
                return self._json({"ok": False, "error": msg('missing_model', LANG_DEFAULT)}, 400)
            npred = data.get("num_predict", 128)
            if npred not in (64, 128, 256, 512):
                return self._json({"ok": False, "error": msg('num_predict_invalid', LANG_DEFAULT)}, 400)
            if not llm_bench_start(model, npred):
                return self._json({"ok": False, "error": msg('bench_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/disk/action":
            act = data.get("action")
            if act == "ollama_delete":
                name = data.get("name")
                if not name or not isinstance(name, str):
                    return self._json({"ok": False, "error": msg('missing_name', LANG_DEFAULT)}, 400)
                req = urllib.request.Request("http://127.0.0.1:11434/api/delete", data=json.dumps({"name": name}).encode(),
                                             headers={"Content-Type": "application/json"}, method="DELETE")
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        r.read()
                except urllib.error.HTTPError as e:
                    return self._json({"ok": False, "error": msg('ollama_http', LANG_DEFAULT, p0=e.code, p1=e.read().decode(errors='replace')[:200])}, 502)
                except (urllib.error.URLError, OSError) as e:
                    return self._json({"ok": False, "error": msg('ollama_connect', LANG_DEFAULT, p0=e)}, 502)
                _DISK["ts"] = 0
                return self._json({"ok": True})
            if act == "apt_clean":
                ok = JOB.start("aptclean")
                return self._json({"ok": ok} if ok else {"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 200 if ok else 409)
            if act == "apt_autoremove":
                pk = _apt_autoremovable()
                if not pk:
                    return self._json({"ok": False, "error": msg('no_removable', LANG_DEFAULT)}, 400)
                return self._json({"ok": JOB.start("remove", pk), "packages": pk})
            if act in DISK_ACTIONS:
                return self._json({"ok": JOB.start("shell", [act])})
            return self._json({"ok": False, "error": msg('unknown_action', LANG_DEFAULT)}, 400)
        if path == "/api/apps/update":
            source = data.get("source"); ids = [i for i in data.get("ids", []) if isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9._+-]+", i)]
            if source not in ("flatpak", "snap") or not ids:
                return self._json({"ok": False, "error": msg('source_ids_invalid', LANG_DEFAULT)}, 400)
            if source == "snap":
                busy = {n: snap_running_apps(n) for n in ids}
                busy = {n: v for n, v in busy.items() if v}
                if busy:
                    detail = "；".join(
                        f"{n}（{'、'.join(sorted({x['app'] for x in v}))}，PID {', '.join(str(x['pid']) for x in v[:4])}）"
                        for n, v in busy.items())
                    return self._json({"ok": False, "running": busy,
                                       "error": msg('snap_apps_running', LANG_DEFAULT, p0=detail)}, 409)
            if not JOB.start(source, ids):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        if path == "/api/refresh":
            if not JOB.start("refresh"):
                return self._json({"ok": False, "error": msg('job_running', LANG_DEFAULT)}, 409)
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    _hw_snapshot_load()   # 有快照就先用，服務重啟後硬體分頁不用等
    # 服務重啟時 snapd 可能還在跑上一輪更新，接回來才不會讓畫面顯示成「沒事發生」
    try:
        out = _run(["snap", "changes"], timeout=20) or ""
        for line in out.splitlines():
            m = re.match(r'^(\d+)\s+(\S+)\s+.*?"([^"]+)"', line)
            if m and m.group(2) in SNAP_ACTIVE:
                print(f"attach to in-progress snap change {m.group(1)} ({m.group(3)})", flush=True)
                JOB.start("snap", [m.group(3)])
                break
    except Exception as e:
        print("snap attach skipped:", e, flush=True)
    threading.Thread(target=_disk_watch, daemon=True).start()
    threading.Thread(target=_gpu_watch, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spark Center on http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
