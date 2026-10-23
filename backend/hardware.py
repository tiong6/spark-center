"""Hardware/network queries and their caches. Does not import the HTTP server."""
import concurrent.futures
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from .messages import LANG_DEFAULT, msg
from .system import HERE, READONLY, NO_SWEEP, _ENV_C, _run

# ---------- 硬體資訊（全部免 root；拿不到的欄位回 None，前端顯示「—」）----------

DMI_DIR = "/sys/devices/virtual/dmi/id"
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "board_vendor", "board_name",
              "bios_vendor", "bios_version", "bios_date")


def read_text_file(path, default=None):
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def memory_info():
    d = {}
    for line in (read_text_file("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = int(v.strip().split()[0]) * 1024 if v.strip() else 0
    return d


def _os_release():
    d = {}
    for line in (read_text_file("/etc/os-release") or "").splitlines():
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
    app_sm = _nvml_uint("nvmlDeviceGetApplicationsClock", 1)   # 驅動的預設工作點（GB10 是 2418），滿載時實際跑的就是這個，不是 max
    return {
        "app_clock_mhz": app_sm or None, "max_clock_mhz": max_sm or None,
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


def gpu_static():
    via = _gpu_static_nvml()
    if via:
        # PCI bus id 只有 nvidia-smi 好拿，補一次（靜態，快取 60 秒內只跑一次）
        out = _run(["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader"], timeout=10)
        via["bus"] = out.strip() if out else None
        return via
    return _gpu_static_smi()


def gpu_live():
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
        speed = read_text_file(f"/sys/class/net/{name}/speed")
        kind = "wifi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else\
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
        chip = read_text_file(os.path.join(h, "name"), "?")
        for t in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            v = read_text_file(t)
            if not v or not v.lstrip("-").isdigit():
                continue
            label = read_text_file(t.replace("_input", "_label"), "") or os.path.basename(t).replace("_input", "")
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
        vid, pid = (read_text_file(os.path.join(d, "idVendor")) or "").lower(), (read_text_file(os.path.join(d, "idProduct")) or "").lower()
        if not vid:
            continue
        ifaces = []
        for i in sorted(os.listdir(base)):
            if not i.startswith(n + ":"):
                continue
            ip = os.path.join(base, i)
            cls = (read_text_file(os.path.join(ip, "bInterfaceClass")) or "").lower()
            sub = (read_text_file(os.path.join(ip, "bInterfaceSubClass")) or "").lower()
            proto = (read_text_file(os.path.join(ip, "bInterfaceProtocol")) or "").lower()
            drv = os.path.basename(os.readlink(os.path.join(ip, "driver"))) if os.path.islink(os.path.join(ip, "driver")) else None
            ifaces.append({"id": i.split(":", 1)[1], "class": cls, "sub": sub, "proto": proto, "driver": drv,
                           "label": USB_CLASS.get(cls, (cls, "other"))[0]})
        # 分類：以介面類別為主，HID 再依 protocol 分鍵盤/滑鼠
        kind, kind_label = "other", msg('device', LANG_DEFAULT)
        classes = [x["class"] for x in ifaces]
        dev_class = (read_text_file(os.path.join(d, "bDeviceClass")) or "").lower()
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
        speed = read_text_file(os.path.join(d, "speed")) or ""
        if kind == "roothub":
            product = msg('usb_root_bus', LANG_DEFAULT, p0='3.x' if speed not in ('12', '480', '1.5') else '2.0', p1=read_text_file(os.path.join(d, 'busnum')))
        else:
            product = read_text_file(os.path.join(d, "product"))
        manufacturer = read_text_file(os.path.join(d, "manufacturer"))
        mp = read_text_file(os.path.join(d, "bMaxPower"))  # 例如 "500mA"：裝置在描述子宣告的最大耗電，不是量測
        max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
        devs[n] = {
            "path": n, "vid": vid, "pid": pid,
            "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
            "name": product or products.get((vid, pid)) or msg('unknown_device', LANG_DEFAULT, p0=vid, p1=pid),
            "manufacturer": manufacturer or vendors.get(vid),
            "name_from_ids": not product and (vid, pid) in products,
            "speed": speed, "speed_label": USB_SPEED.get(speed, f"{speed} Mb/s" if speed else "—"),
            "kind": kind, "kind_label": kind_label,
            "serial": read_text_file(os.path.join(d, "serial")),
            "ports": read_text_file(os.path.join(d, "maxchild")),
            "interfaces": ifaces,
            "busnum": read_text_file(os.path.join(d, "busnum")), "devnum": read_text_file(os.path.join(d, "devnum")),
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
        speed = read_text_file(os.path.join(d, "speed")) or ""
        c = ctrls.setdefault(ctrl, {"controller": ctrl, "usb2_bus": None, "usb3_bus": None, "location": None,
                                    "connect_type": None, "devices": [], "internal": False})
        key = "usb3_bus" if speed not in ("12", "480", "1.5") else "usb2_bus"
        c[key] = read_text_file(os.path.join(d, "busnum"))
        # 埠資訊（root hub 的 port1..）
        for pdir in glob.glob(os.path.join(d, f"{n.replace('usb', '')}-0:1.0", f"{n}-port*")):
            loc_dir = os.path.join(pdir, "physical_location")
            if os.path.isdir(loc_dir) and not c["location"]:
                c["location"] = {k: read_text_file(os.path.join(loc_dir, k)) for k in ("panel", "vertical_position", "horizontal_position", "dock", "lid")}
            ct = read_text_file(os.path.join(pdir, "connect_type"))
            if ct and ct != "unknown":
                c["connect_type"] = ct
            devlink = os.path.join(pdir, "device")
            if os.path.exists(devlink):
                dev = os.path.realpath(devlink)
                dn = os.path.basename(dev)
                # 只列直接插在這個孔上的裝置（下面的 hub 子裝置數另外算）
                kids = [k for k in os.listdir(base) if k.startswith(dn + ".") and ":" not in k]
                mp = read_text_file(os.path.join(dev, "bMaxPower"))
                max_ma = int(re.sub(r"\D", "", mp)) if mp and re.sub(r"\D", "", mp) else None
                c["devices"].append({
                    "path": dn, "name": read_text_file(os.path.join(dev, "product")) or f"{read_text_file(os.path.join(dev, 'idVendor'))}:{read_text_file(os.path.join(dev, 'idProduct'))}",
                    "max_ma": max_ma, "max_w": round(max_ma * 5 / 1000, 2) if max_ma else None,
                    "manufacturer": read_text_file(os.path.join(dev, "manufacturer")),
                    "speed": read_text_file(os.path.join(dev, "speed")),
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


# 同款機器主機板一樣、孔位對控制器的對應也一樣：把作者 GX10 上校準出來的結果當該機型的預設值，裝了就有，
# 不用每台再校準。只對 DMI 產品名相符的機型套用；別家 GB10 板子不同，仍要自己校準。使用者校準會蓋掉預設（存進檔案）。
USBC_MAP_DEFAULTS = {
    "GX10": {"0": {"controller": "NVDA8000:03"}, "1": {"controller": "NVDA8000:02"},
             "2": {"controller": "NVDA8000:01"}, "3": {"controller": "NVDA8000:00"}},
}


def usbc_map_default():
    try:
        with open("/sys/class/dmi/id/product_name", encoding="utf-8") as f:
            product = f.read().strip()
    except OSError:
        return {}
    d = USBC_MAP_DEFAULTS.get(product)
    if not d:
        return {}
    return {k: {**v, "source": "default", "note": msg("usbc_default_note", LANG_DEFAULT, product=product)} for k, v in d.items()}


def usbc_map_load():
    """實體孔 → 控制器 的對應（伺服器端持久化，所有瀏覽器共用）。沒有校準檔時用該機型的內建預設（有的話）。
    slot 0 固定是電源輸入孔（依 ASUS 規格與評測），核心無 UCSI/typec，供電狀態偵測不到，只能手動標記。"""
    try:
        with open(USBC_MAP_FILE) as f:
            d = json.load(f)
            if isinstance(d, dict) and d:
                return d
    except (OSError, ValueError):
        pass
    return usbc_map_default()


def usbc_map_save(d):
    os.makedirs(os.path.dirname(USBC_MAP_FILE), exist_ok=True)
    tmp = USBC_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USBC_MAP_FILE)


CX7_HOTPLUG_FLAG = "/etc/nvidia/cx7-hotplug-enabled"


def cx7_state():
    """ConnectX-7 的狀態，後面板、PCI 表與常見問題分頁共用這一份（以前後面板跑 lspci、FAQ 讀 /sys，兩套會分歧）。
    2026-01 版起 CX7 預設熱插拔省電（旗標檔在就開）：沒插 QSFP 線就不上電，PCI 上一個 0x15b3 功能都看不到——
    那是預期，不是壞。pci_functions 讀不到時是 None，前端顯示「—」。"""
    try:
        n = sum(1 for d in os.listdir("/sys/bus/pci/devices") if (read_text_file(f"/sys/bus/pci/devices/{d}/vendor") or "").lower() == "0x15b3")
    except OSError:
        n = None
    ifaces = []
    try:
        for i in os.listdir("/sys/class/net"):
            drv = f"/sys/class/net/{i}/device/driver"
            if os.path.exists(drv) and os.path.basename(os.path.realpath(drv)).startswith("mlx"):
                ifaces.append(i)
    except OSError:
        pass
    return {"hotplug": os.path.exists(CX7_HOTPLUG_FLAG), "flag": CX7_HOTPLUG_FLAG, "pci_functions": n,
            "ifaces": sorted(ifaces), "driver_loaded": os.path.isdir("/sys/module/mlx5_core")}


def buffer_cache_bytes(m):
    """free(1) 的 buff/cache：Buffers + Cached + SReclaimable。監控卡與常見問題分頁共用這個算法。"""
    return m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0)


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
        net[i] = {"driver": drv, "state": read_text_file(f"/sys/class/net/{i}/operstate"), "speed": read_text_file(f"/sys/class/net/{i}/speed"),
                  "carrier": read_text_file(f"/sys/class/net/{i}/carrier")}
    eth = next(((n, v) for n, v in net.items() if v["driver"].startswith("r81")), None)
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
        "connectx7": cx7_state(),
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
        rd = lambda k: (read_text_file(os.path.join(sysd, k)) or "").strip()
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
    return {"devices": devs, "bridges": len(bridges), "cx7_hotplug": os.path.exists(CX7_HOTPLUG_FLAG),
            "empty_ports": [{"slot": b["slot"], "max_gen": b["max_gen"], "max_width": b["max_width"]} for b in empty]}


# ---------- 印表機（CUPS 佇列 + 區網 IPP 探索 + USB + 印表機自己回報的狀態）----------
# 印表機會開關機、會缺紙缺碳粉，不能跟硬體靜態清單一起快取 1 小時（還存成快照）：曾因此印表機開機後頁面仍說「掃不到」。
# 另開 /api/hardware/printers、短快取。狀態用 IPP 直接問印表機本身；CUPS 佇列的 idle 只代表佇列沒工作，不代表印表機在線。

PRINTER_TTL = 15
_PRINTERS = {"ts": 0, "data": None, "lock": threading.Lock()}
IPP_STATE = {3: "idle", 4: "processing", 5: "stopped"}
_IPP_STATUS_TEST = """{
  NAME "printer status"
  OPERATION Get-Printer-Attributes
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri printer-uri $uri
  ATTR keyword requested-attributes printer-state,printer-state-reasons,printer-state-message,printer-is-accepting-jobs,queued-job-count,marker-names,marker-colors,marker-types,marker-levels,marker-low-levels,marker-high-levels,printer-make-and-model,printer-info,printer-more-info,printer-supply-info-uri
  STATUS successful-ok
}
"""


def _dnssd_name(uri):
    """ipps://HP%20Color%20LaserJet%20M155nw%20(0194A6)._ipps._tcp.local/ → HP Color LaserJet M155nw (0194A6)；不是 DNS-SD 位址回 None。
    同一台印表機會同時廣播 _ipp 與 _ipps，用服務名稱當身分才不會列兩次。"""
    from urllib.parse import unquote
    m = re.match(r"^ipps?://([^/]+?)\._ipps?\._tcp\.local\.?(?:/|$)", uri or "")
    return unquote(m.group(1)) if m else None


def _ipp_status(uri):
    """IPP Get-Printer-Attributes 直接問印表機：狀態、警示、耗材量。問不到回 {"error": 原因}，前端照實說明，不猜。"""
    if not uri or not re.match(r"^ipps?://", uri):
        return {"error": "not_ipp"}
    if not shutil.which("ipptool"):
        return {"error": "no_ipptool"}
    import plistlib
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".test") as f:
        f.write(_IPP_STATUS_TEST)
        f.flush()
        try:
            r = subprocess.run(["ipptool", "-T", "5", "-X", uri, f.name], capture_output=True, timeout=12, env=_ENV_C)
        except (OSError, subprocess.TimeoutExpired):
            return {"error": "no_response"}
    try:
        test = plistlib.loads(r.stdout)["Tests"][0]
    except Exception:
        return {"error": "no_response"}
    a = {}
    for g in test.get("ResponseAttributes") or []:
        a.update(g)
    if not test.get("Successful") or "printer-state" not in a:
        return {"error": "no_response"}
    lst = lambda v: v if isinstance(v, list) else ([] if v is None else [v])
    names, colors, types, levels, lows, highs = (lst(a.get("marker-" + k)) for k in ("names", "colors", "types", "levels", "low-levels", "high-levels"))
    at = lambda xs, i: xs[i] if i < len(xs) else None
    return {
        "state": IPP_STATE.get(a.get("printer-state")),
        "reasons": [x for x in lst(a.get("printer-state-reasons")) if x and x != "none"],
        "message": a.get("printer-state-message") or None,
        "accepting": a.get("printer-is-accepting-jobs"),
        "jobs": a.get("queued-job-count"),
        "model": a.get("printer-make-and-model"),
        "more_info": a.get("printer-more-info"),
        "supply_info": a.get("printer-supply-info-uri"),
        # IPP 規定 marker-levels 是百分比；-1 印表機不提供、-2 未知、-3 有剩但量不明（前端照這三種說，不當成 0）
        "markers": [{"name": n, "color": at(colors, i), "type": at(types, i), "level": at(levels, i), "low": at(lows, i), "high": at(highs, i)}
                    for i, n in enumerate(names)],
    }


def print_stack(apt_installed, apt_active, snap_installed, snap_proxy, snap_active, snap_clients):
    """apt 版與 snap 版 CUPS 的關係（純判斷，無副作用）。
    cups snap 偵測到系統 CUPS 時會進「代理模式」：不當獨立伺服器，用 cups-proxyd 把 snap 程式的列印轉給 localhost:631，
    並且不跑自己的 cups-browsed（/snap/cups/current/scripts/run-cupsd）。所以兩套並存多半是正常的，不是衝突。
    kind：apt_only／snap_only／proxy（正常並存）／dual（兩個獨立伺服器，異常）／none。"""
    if apt_installed and snap_installed:
        kind = "proxy" if snap_proxy or not snap_active else "dual"
    elif apt_installed:
        kind = "apt_only"
    elif snap_installed:
        kind = "snap_only"
    else:
        kind = "none"
    return {"kind": kind, "apt_installed": apt_installed, "apt_active": apt_active, "snap_installed": snap_installed,
            "snap_proxy": snap_proxy, "snap_active": snap_active, "snap_clients": snap_clients,
            "warn": kind == "dual" or (kind == "apt_only" and not apt_active) or (kind == "snap_only" and not snap_active)}


def _snap_cups_clients():
    """哪些 snap 接在 cups snap 的 cups slot 上（它們只能透過 cups snap 列印，拔掉 cups snap 會讓它們印不了）。"""
    out = _run(["snap", "connections", "cups"], timeout=10) or ""
    return sorted({m.group(1) for m in re.finditer(r"^cups\s+(\S+):cups\s+cups:cups", out, re.M)})


def _print_stack():
    snap_installed = os.path.isdir("/snap/cups/current")
    return print_stack(
        apt_installed=os.path.exists("/usr/sbin/cupsd"),
        apt_active=(_run(["systemctl", "is-active", "cups"], timeout=5) or "").strip() == "active",
        snap_installed=snap_installed,
        snap_proxy=os.path.exists("/var/snap/cups/current/var/run/proxy-mode"),   # run-cupsd 進代理模式時 touch 的標記，root:root 0644
        snap_active=snap_installed and (_run(["systemctl", "is-active", "snap.cups.cupsd.service"], timeout=5) or "").strip() == "active",
        snap_clients=_snap_cups_clients() if snap_installed else [])


def _printers():
    stack = _print_stack()
    res = {"cups_active": stack["apt_active"] or (stack["kind"] == "snap_only" and stack["snap_active"]),
           "stack": stack, "queues": [], "devices": [], "usb": [], "jobs": None}
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
        # cups-browsed 自動建的佇列是 implicitclass://佇列名，真正的印表機位址在佇列選項 device-uri
        q["auto"] = (q["uri"] or "").startswith("implicitclass://")
        dev = None
        if q["auto"]:
            try:
                opts = shlex.split(_run(["lpoptions", "-p", q["name"]], timeout=5) or "")
                dev = next((o.split("=", 1)[1] for o in opts if o.startswith("device-uri=")), None)
            except ValueError:
                pass
        q["device_uri"] = dev or q["uri"]
    jobs = _run(["lpstat", "-o"], timeout=10)
    if jobs is not None:
        res["jobs"] = len([l for l in jobs.splitlines() if l.strip()])
    # 實體印表機：佇列指向的＋區網 driverless（mDNS 上的 IPP Everywhere）找到的，依 DNS-SD 服務名稱合併
    devices = {}
    for q in res["queues"]:
        name = _dnssd_name(q["device_uri"])
        d = devices.setdefault(name or q["device_uri"] or q["name"], {"name": name or q["name"], "uri": q["device_uri"], "model": None, "queues": [], "on_network": False})
        d["queues"].append(q["name"])
    dl = _run(["driverless", "list"], timeout=20) or ""
    for line in dl.splitlines():
        # "driverless:ipps://…/" en "HP" "HP ColorLaserJet M155-M156, driverless, cups-filters 2.0.0" "MFG:…"
        m = re.match(r'^"([^"]+)"\s+\S+\s+"[^"]*"\s+"([^"]*)"', line)
        uri, model = (m.group(1), m.group(2).split(",")[0].strip() or None) if m else (line.split()[0] if "://" in line and not line.startswith("DEBUG") else None, None)
        if not uri:
            continue
        uri = uri[len("driverless:"):] if uri.startswith("driverless:") else uri
        name = _dnssd_name(uri)
        d = devices.setdefault(name or uri, {"name": name or uri, "uri": uri, "model": None, "queues": [], "on_network": False})
        d["on_network"] = True
        d["model"] = d["model"] or model
    res["devices"] = list(devices.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for d, st in zip(res["devices"], ex.map(lambda d: _ipp_status(d["uri"]), res["devices"])):
            d["status"] = st
    # USB 印表機：介面類別 07
    base = "/sys/bus/usb/devices"
    for i in os.listdir(base):
        if ":" in i and (read_text_file(os.path.join(base, i, "bInterfaceClass")) or "") == "07":
            dev = os.path.join(base, i.split(":")[0])
            res["usb"].append({"name": read_text_file(os.path.join(dev, "product")) or i, "manufacturer": read_text_file(os.path.join(dev, "manufacturer"))})
    return res


def printers(force=False):
    with _PRINTERS["lock"]:
        if not force and _PRINTERS["data"] and time.time() - _PRINTERS["ts"] < PRINTER_TTL:
            return _PRINTERS["data"]
        data = {**_printers(), "generated": datetime.now().isoformat(timespec="seconds")}
        _PRINTERS.update(ts=time.time(), data=data)
        return data


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
    vid = (read_text_file(base + "/device/vendor") or "").lower()
    did = (read_text_file(base + "/device/device") or "").lower()
    wwid = read_text_file("/sys/class/block/nvme0n1/wwid") or ""
    m = re.match(r"eui\.([0-9a-f]+)", wwid)
    oui = m.group(1)[-16:][:6] if m and len(m.group(1)) >= 16 else None   # 可能有前導 0 補到 32 位，取最後 16 位才是 EUI-64
    ctrl = _run(["lspci", "-nn", "-s", os.path.basename(os.path.realpath(base + "/device"))], timeout=5) or ""
    ctrl_name = re.sub(r"^.*?: ", "", ctrl.strip().splitlines()[0]) if ctrl.strip() else None
    info = {"model": (read_text_file(base + "/model") or "").strip(), "firmware": (read_text_file(base + "/firmware_rev") or "").strip(),
            "serial": (read_text_file(base + "/serial") or "").strip(), "state": read_text_file(base + "/state"),
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


def _boot_id():
    return (read_text_file("/proc/sys/kernel/random/boot_id") or "").strip() or None


def load_snapshot():
    """服務啟動時載回上次的硬體快照，省一次慢掃描。只在同一次開機內有效：BIOS、核心、韌體版本只會在重開機後變，
    重開後若服務在一小時內重啟，照舊載回就會把舊 BIOS 版本顯示到按「重新掃描」為止（2026-10-09 實際發生）。"""
    try:
        with open(HW_SNAPSHOT) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("generated"):
            if d.get("boot_id") != _boot_id():
                return   # 不同次開機：作廢，下次 /api/hardware 重新掃描
            d.pop("printers", None)   # 舊版快照含印表機；印表機已改走即時端點，過期的不再供應
            _HW_CACHE.update(data=d, ts=os.path.getmtime(HW_SNAPSHOT))
    except (OSError, ValueError):
        pass


def hardware_static(force=False):
    if not force and _HW_CACHE["data"] and time.time() - _HW_CACHE["ts"] < HW_TTL:
        return _HW_CACHE["data"]
    osr = _os_release()
    mem = memory_info()
    data = {
        "system": {
            **{k: read_text_file(os.path.join(DMI_DIR, k)) for k in DMI_FIELDS},
            "hostname": read_text_file("/etc/hostname"),
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
        "gpu": gpu_static(),
        "disks": _disks(),
        "network": _network(),
        "bluetooth": _bluetooth(),
        "usb": _list_cmd(["lsusb"]),
        "usb_tree": _usb_tree(),
        "pci": _list_cmd(["lspci"]),
        "pci_devices": _pci_devices(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "boot_id": _boot_id(),   # 快照只在這次開機內可信（見 load_snapshot）
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
    lines = (read_text_file("/proc/stat") or "").splitlines()
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
        v = read_text_file(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
        res.append(int(v) // 1000 if v and v.isdigit() else None)
    return res


def _cpu_topology():
    """每核的最高時脈與調速器，用來把核心分到叢集（X925/A725 靠最高時脈區分）。"""
    cores = []
    for i in range(os.cpu_count() or 0):
        base = f"/sys/devices/system/cpu/cpu{i}/cpufreq/"
        mx = read_text_file(base + "cpuinfo_max_freq"); mn = read_text_file(base + "cpuinfo_min_freq")
        cores.append({"id": i, "max_mhz": int(mx) // 1000 if mx and mx.isdigit() else None,
                      "min_mhz": int(mn) // 1000 if mn and mn.isdigit() else None,
                      "governor": read_text_file(base + "scaling_governor")})
    return cores


def _net_counters():
    """/proc/net/dev 的累計位元組數；前端拿兩次取樣算每秒流量。
    連線狀態與速度也在這裡即時讀（/sys 兩個檔，便宜）：介面清單本身在硬體快取裡一小時才更新，
    插上網路線後監控頁要立刻看到卡片、看到 10 Gb/s，不能等快取。"""
    res = {}
    for line in (read_text_file("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 9:
            name = name.strip()
            speed = read_text_file(f"/sys/class/net/{name}/speed")
            res[name] = {"rx": int(f[0]), "tx": int(f[8]), "state": (read_text_file(f"/sys/class/net/{name}/operstate") or "").strip(),
                         "speed_mbps": int(speed) if speed and speed.strip().lstrip("-").isdigit() and int(speed) > 0 else None}
    return res


# ---------- 區網裝置：本機所在網段裡最近有通訊的鄰居 ----------
# 看得到的範圍要老實講：只有本機介面所在的 VLAN／網段；其他 VLAN 從這裡看不到，完整清單在路由器。
# 方法是被動的：ARP／NDP 鄰居表（最近講過話的）＋ mDNS（會自報名字的）＋ 反查 DNS（路由器知道 DHCP 主機名）。不主動掃描。
_LAN = {"ts": 0, "data": None, "lock": threading.Lock()}
LAN_TTL = 30
_OUI = {"loaded": False, "map": {}}


def _oui_vendor(mac):
    """IEEE OUI 表（ieee-data 套件）查廠牌；沒有那個檔就回 None。隨機化的私有位址（第二個 hex 位的 bit1）查不出廠牌，另外標。"""
    if not _OUI["loaded"]:
        _OUI["loaded"] = True
        for path in ("/usr/share/ieee-data/oui.txt", "/var/lib/ieee-data/oui.txt"):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "(hex)" in line:
                            k, _, v = line.partition("(hex)")
                            _OUI["map"][k.strip().replace("-", ":").lower()] = v.strip()
                break
            except OSError:
                continue
    return _OUI["map"].get(mac[:8].lower())


def lan_devices(force=False):
    with _LAN["lock"]:
        if not force and _LAN["data"] and time.time() - _LAN["ts"] < LAN_TTL:
            return _LAN["data"]
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor
    subnets, my_ips = {}, set()
    try:
        for i in json.loads(_run(["ip", "-j", "addr"], timeout=10) or "[]"):
            name = i.get("ifname", "")
            if name == "lo" or not os.path.exists(f"/sys/class/net/{name}/device"):
                continue   # 只算實體介面：docker／bridge／tailscale 的鄰居不是家裡的裝置
            for a in i.get("addr_info", []):
                if a.get("family") == "inet":
                    subnets[name] = str(ipaddress.ip_network(f"{a['local']}/{a['prefixlen']}", strict=False)); my_ips.add(a["local"])
    except (ValueError, KeyError):
        pass
    if not subnets:
        return {"ok": True, "devices": [], "subnets": {}, "generated": datetime.now().isoformat(timespec="seconds")}
    devs = {}
    try:
        for n in json.loads(_run(["ip", "-j", "neigh", "show"], timeout=10) or "[]"):
            dev, mac, dst = n.get("dev"), (n.get("lladdr") or "").lower(), n.get("dst") or ""
            if dev not in subnets or not mac or any(s in ("FAILED", "INCOMPLETE") for s in n.get("state", [])):
                continue
            d = devs.setdefault((dev, mac), {"dev": dev, "mac": mac, "ipv4": [], "ipv6": 0, "states": set()})
            (d["ipv4"].append(dst) if ":" not in dst else d.__setitem__("ipv6", d["ipv6"] + 1))
            d["states"].update(n.get("state", []))
    except ValueError:
        pass
    # mDNS 一次瀏覽（4 秒）＋ 每個 IPv4 反查一次（各 2 秒），並行跑
    mdns = {}
    def browse():
        try:
            # 位元組讀入再寬鬆解碼：裝置自報的名字可能夾非 UTF-8 位元組，text=True 會整段解碼失敗、mDNS 結果全丟
            out = subprocess.run(["timeout", "4", "avahi-browse", "-atrp"], capture_output=True, timeout=6, env=_ENV_C).stdout.decode("utf-8", "replace")
        except Exception:
            return
        for line in out.splitlines():
            f = line.split(";")
            if len(f) >= 8 and f[0] == "=" and f[7]:
                mdns.setdefault(f[7], f[6].removesuffix(".local"))
    def rdns(ip):
        # 先問路由器的 DNS（DHCP 主機名），沒有再用 mDNS 反查（iPhone 這類只回 mDNS）
        for cmd in (["getent", "hosts", ip], ["avahi-resolve", "-a", ip]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=2, env=_ENV_C).stdout.split()
                if len(out) > 1 and out[1] != ip:
                    return ip, out[1].removesuffix(".local")
            except Exception:
                pass
        return ip, None
    names = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fb = ex.submit(browse)
        for ip, nm in ex.map(rdns, [ip for d in devs.values() for ip in d["ipv4"]]):
            names[ip] = nm
        fb.result()
    out = []
    for d in devs.values():
        ip4 = sorted(d["ipv4"], key=lambda s: [int(x) for x in s.split(".")])
        private = bool(int(d["mac"][:2], 16) & 0x02)
        out.append({"dev": d["dev"], "mac": d["mac"], "ip": ip4[0] if ip4 else None, "ipv4": ip4, "ipv6": d["ipv6"],
                    "name": next((names.get(ip) for ip in ip4 if names.get(ip)), None),
                    "mdns": next((mdns.get(ip) for ip in ip4 if mdns.get(ip)), None),
                    "vendor": None if private else _oui_vendor(d["mac"]), "private_mac": private,
                    "state": "reachable" if "REACHABLE" in d["states"] or "DELAY" in d["states"] or "PROBE" in d["states"] else "stale"})
    # 只在 mDNS 看得到、沒和本機講過話的裝置（Apple TV、智慧家電這類）：也是這個網段的真實裝置，列出來但標明來源、沒有 MAC
    seen4 = {ip for d in out for ip in d["ipv4"]}
    for ip, host in mdns.items():
        if ":" in ip or ip in seen4 or ip in my_ips:
            continue
        try:
            if not any(ipaddress.ip_address(ip) in ipaddress.ip_network(s) for s in subnets.values()):
                continue
        except ValueError:
            continue
        out.append({"dev": None, "mac": None, "ip": ip, "ipv4": [ip], "ipv6": 0, "name": None, "mdns": host, "vendor": None, "private_mac": False, "state": "mdns"})
    out.sort(key=lambda x: ([int(p) for p in x["ip"].split(".")] if x["ip"] else [999]))
    data = {"ok": True, "devices": out, "subnets": subnets, "generated": datetime.now().isoformat(timespec="seconds"), "sweep": not NO_SWEEP and not READONLY}
    with _LAN["lock"]:
        _LAN.update(ts=time.time(), data=data)
    return data


def lan_sweep():
    """主動掃一次：對本機所在網段每個位址 ping 一下（不需 root，ping 有 cap_net_raw）。
    裝置就算不回 ICMP，網路卡也會回 ARP，鄰居表就有 MAC 了。只掃實體介面的網段，最多 1024 個位址，64 個並行，實測 254 個位址約 7 秒。
    刻意做成要按的按鈕：對整個網段發封包在別人的網路上不禮貌；唯讀模式下是 POST 所以會被擋。"""
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor
    targets = []
    for name, cidr in lan_devices().get("subnets", {}).items():
        net = ipaddress.ip_network(cidr)
        if net.num_addresses > 1024:
            continue
        targets += [str(h) for h in net.hosts()]
    targets = sorted(set(targets), key=lambda ip: [int(x) for x in ip.split(".")])   # 有線＋無線同一網段時只掃一遍
    def ping(ip):
        try:
            subprocess.run(["ping", "-c", "1", "-W", "1", "-n", "-q", ip], capture_output=True, timeout=3)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(ping, targets))
    time.sleep(0.3)
    return {"ok": True, "pinged": len(targets), **lan_devices(force=True)}


def _root_usage():
    try:
        st = os.statvfs("/")
        return {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        return None


# ---------- Wi-Fi 品質（iw，免 root）----------

def wifi_interface():
    for i in os.listdir("/sys/class/net"):
        if os.path.isdir(f"/sys/class/net/{i}/wireless"):
            return i
    return None


def wifi_live():
    """iw link + station dump：訊號、速率、MCS、重試、beacon loss。約 10 ms。"""
    iface = wifi_interface()
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


def wifi_access_points():
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


def bluetooth_connected():
    out = _run(["bluetoothctl", "devices", "Connected"], timeout=5) or ""
    return [l.split(" ", 2)[2] for l in out.splitlines() if l.startswith("Device ") and len(l.split(" ", 2)) == 3]


def wifi_events_24h():
    """NetworkManager 24 小時內 Wi-Fi 斷線／重連次數（journal，免 root 可讀系統單元）。"""
    out = _run(["journalctl", "-u", "NetworkManager", "--since", "-24 h", "--no-pager", "-o", "short"], timeout=15) or ""
    iface = wifi_interface() or "wl"
    disc = len([l for l in out.splitlines() if iface in l and "activated -> deactivating" in l])
    act = len([l for l in out.splitlines() if iface in l and "Activation: successful" in l])
    return {"disconnects": disc, "activations": act}


def hardware_live():
    mem = memory_info()
    load = (read_text_file("/proc/loadavg") or "").split()[:3]
    up = read_text_file("/proc/uptime")
    return {
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"), "buff_cache": buffer_cache_bytes(mem) if mem else None,
                   "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")},
        "load": [float(x) for x in load] if len(load) == 3 else None,
        "uptime_s": float(up.split()[0]) if up else None,
        "gpu": gpu_live(),
        "sensors": _sensors(),
        "cpu": _cpu_jiffies(),
        "cpu_freq": _cpu_freqs(),
        "wifi": wifi_live(),
        "net": _net_counters(),
        "disk_root": _root_usage(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": time.time(),
    }



# ── 磁碟檢查：SSD 自我檢測、開機檔案系統檢查、被動健康彙整（chkdsk 在 Linux 上對應的三件事）────────────
# 不做壞軌掃描（badblocks）：SSD 的壞區由控制器自己重對應，從 LBA 掃不到，跑幾小時也不會有意義。
NVME_CTRL = "/dev/nvme0"
ROOT_DEV = "/dev/nvme0n1p2"
SELFTEST_CMDS = {   # 這些字串就是 sudoers 放行的指令，逐字對應；改這裡等於改放行範圍
    "id_ctrl": ["/usr/sbin/nvme", "id-ctrl", NVME_CTRL, "--output-format=json"],
    "log": ["/usr/sbin/nvme", "self-test-log", NVME_CTRL, "--output-format=json"],
    "short": ["/usr/sbin/nvme", "device-self-test", NVME_CTRL, "--self-test-code=1"],
    "extended": ["/usr/sbin/nvme", "device-self-test", NVME_CTRL, "--self-test-code=2"],
    "abort": ["/usr/sbin/nvme", "device-self-test", NVME_CTRL, "--self-test-code=15"],
}
FSCK_CMDS = {
    "info": ["/usr/sbin/tune2fs", "-l", ROOT_DEV],
    "schedule": ["/usr/sbin/tune2fs", "-c", "1", ROOT_DEV],      # 最大掛載次數 1：下次開機 initramfs 的 e2fsck -a 會做完整檢查
    "unschedule": ["/usr/sbin/tune2fs", "-c", "-1", ROOT_DEV],   # 檢查做完就恢復「不看掛載次數」，否則每次開機都檢查
}
FSCK_RESULT = os.path.join(HERE, "data", "fsck-result.json")
DISKCHECK_TTL = 15
_DISKCHECK = {"lock": threading.Lock(), "ts": 0, "data": None, "support": None}
DISK_EVENT_RE = re.compile(r"EXT4-fs error|EXT4-fs \(\w+\): .*(?:corrupt|bad block|checksum)|Buffer I/O error|blk_update_request: (?:I/O|critical)|"
                           r"nvme nvme\d+: .*(?:timeout|reset|abort|I/O error)|end_request: I/O error|print_req_error: I/O error", re.I)
SELFTEST_UNUSED = 0xF


def _sudo(cmd, timeout=15):
    """sudo -n 跑一條 sudoers 放行的固定指令。回 (stdout, note)：note 不是 None 代表拿不到，並說明是沒放行還是指令失敗。"""
    try:
        r = subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True, timeout=timeout, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, msg('nvme_exec_failed', LANG_DEFAULT, p0=e)
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        err = err[-1] if err else f"rc={r.returncode}"
        if "password" in err.lower() or "sudo" in err.lower():
            return None, msg('diskcheck_not_allowed', LANG_DEFAULT)
        return None, msg('diskcheck_cmd_failed', LANG_DEFAULT, p0=f"{os.path.basename(cmd[0])} {cmd[1]}", p1=err)
    return r.stdout, None


def _root_source():
    for line in (read_text_file("/proc/mounts") or "").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "/":
            return p[0], p[2]
    return None, None


def _ext4_errors(sysfs="/sys/fs/ext4"):
    """ext4 自己記在 superblock 的錯誤計數，核心經 sysfs 公開，不用 root。0 代表這個檔案系統自建立以來沒記錄過錯誤。"""
    src, fstype = _root_source()
    if fstype != "ext4" or not src:
        return {"available": False, "fstype": fstype, "source": src}
    d = os.path.join(sysfs, os.path.basename(os.path.realpath(src)))
    cnt = (read_text_file(os.path.join(d, "errors_count")) or "").strip()
    if not cnt.isdigit():
        return {"available": False, "fstype": fstype, "source": src}
    def iso(v):
        v = (v or "").strip()
        return datetime.fromtimestamp(int(v)).isoformat(timespec="seconds") if v.isdigit() and int(v) > 0 else None
    return {"available": True, "fstype": fstype, "source": src, "errors_count": int(cnt),
            "first_error": iso(read_text_file(os.path.join(d, "first_error_time"))),
            "last_error": iso(read_text_file(os.path.join(d, "last_error_time"))),
            "last_error_func": (read_text_file(os.path.join(d, "last_error_func")) or "").strip() or None}


def _count_disk_events(text):
    hits = [l for l in text.splitlines() if DISK_EVENT_RE.search(l)]
    return {"count": len(hits), "last": hits[-1][:200] if hits else None}


def _kernel_disk_events(days=30):
    """核心日誌裡的磁碟錯誤（ext4 錯誤、I/O 錯誤、NVMe 逾時／重設）。journalctl -k 要能讀核心日誌（adm 群組）；讀不到就說讀不到。"""
    try:
        r = subprocess.run(["journalctl", "-k", "--since", f"-{days}d", "--no-pager", "-q", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=20, env=_ENV_C)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "days": days}
    if r.returncode != 0:
        return {"available": False, "days": days}
    return {"available": True, "days": days, **_count_disk_events(r.stdout)}


def _trim_status():
    out = _run(["systemctl", "show", "fstrim.timer", "-p", "ActiveState", "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime"], timeout=5)
    if not out:
        return {"available": False}
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {"available": True, "active": kv.get("ActiveState") == "active",
            "last": kv.get("LastTriggerUSec") or None, "next": kv.get("NextElapseUSecRealtime") or None}


def _flatten(d, out=None):
    """nvme-cli 1.x 與 2.x 的 JSON 鍵名不同（"Current Device Self-Test Operation" vs 巢狀小寫），攤平成正規化鍵再找。"""
    out = {} if out is None else out
    for k, v in d.items():
        if isinstance(v, dict):
            _flatten(v, out)
        else:
            out[re.sub(r"[^a-z0-9]", "", str(k).lower())] = v
    return out


def _pick(flat, *needles, exclude=()):
    for k, v in flat.items():
        if all(n in k for n in needles) and not any(x in k for x in exclude):
            return v
    return None


def _parse_selftest_log(j):
    flat = _flatten({k: v for k, v in j.items() if not isinstance(v, list)})
    cur = _pick(flat, "operation")
    comp = _pick(flat, "completion")
    entries = next((v for v in j.values() if isinstance(v, list)), [])
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _flatten(e)
        res = _pick(f, "result", exclude=("data",))
        code = _pick(f, "code")
        if not isinstance(res, int) or res == SELFTEST_UNUSED:
            continue
        rows.append({"result": res, "code": code if isinstance(code, int) else None,
                     "segment": _pick(f, "segment"), "power_on_hours": _pick(f, "poweronhours"),
                     "valid": _pick(f, "validdiagnostic")})
    return {"current_code": cur if isinstance(cur, int) else None, "running": isinstance(cur, int) and cur != 0,
            "progress": comp if isinstance(comp, int) else None, "entries": rows[:5]}


def _selftest_support():
    with _DISKCHECK["lock"]:
        if _DISKCHECK["support"] is not None:
            return _DISKCHECK["support"]
    out, note = _sudo(SELFTEST_CMDS["id_ctrl"])
    if out is None:
        return {"available": False, "note": note}
    try:
        j = json.loads(out)
    except ValueError:
        return {"available": False, "note": msg('nvme_not_json', LANG_DEFAULT)}
    oacs, edstt = j.get("oacs"), j.get("edstt")
    sup = {"available": True, "supported": bool(isinstance(oacs, int) and oacs & 0x10),   # OACS bit 4 = Device Self-test
           "extended_minutes": edstt if isinstance(edstt, int) and edstt > 0 else None}
    with _DISKCHECK["lock"]:
        _DISKCHECK["support"] = sup   # 支援與否不會變，只快取成功的答案
    return sup


def _selftest_status():
    if not os.path.exists(NVME_CTRL):
        return {"available": False, "applies": False}
    sup = _selftest_support()
    if not sup.get("available"):
        return {"available": False, "applies": True, "note": sup.get("note")}
    if not sup["supported"]:
        return {"available": True, "applies": True, "supported": False}
    out, note = _sudo(SELFTEST_CMDS["log"])
    if out is None:
        return {"available": False, "applies": True, "supported": True, "note": note}
    try:
        j = json.loads(out)
    except ValueError:
        return {"available": False, "applies": True, "supported": True, "note": msg('nvme_not_json', LANG_DEFAULT)}
    return {"available": True, "applies": True, "supported": True, "extended_minutes": sup["extended_minutes"], **_parse_selftest_log(j)}


def selftest_start(kind):
    """短測／長測由控制器自己跑，指令立刻返回，進度看 self-test-log。回 {"ok": bool, "error": ...}。"""
    if kind not in ("short", "extended", "abort"):
        return {"ok": False, "error": "bad action"}
    sup = _selftest_support()
    if not sup.get("available"):
        return {"ok": False, "error": sup.get("note")}
    if not sup["supported"]:
        return {"ok": False, "error": msg('selftest_unsupported', LANG_DEFAULT)}
    _, note = _sudo(SELFTEST_CMDS[kind], timeout=30)
    if note:
        return {"ok": False, "error": note}
    disk_check_invalidate()
    return {"ok": True}


def _parse_tune2fs(text):
    kv = {}
    for l in text.splitlines():
        if ":" in l:
            k, v = l.split(":", 1)
            kv[k.strip()] = v.strip()
    def num(k):
        v = kv.get(k)
        return int(v) if v is not None and re.fullmatch(r"-?\d+", v) else None
    def date(k):
        v = kv.get(k)
        try:
            return datetime.strptime(v, "%a %b %d %H:%M:%S %Y") if v else None   # LANG=C："Sat Oct 11 08:00:00 2026"
        except ValueError:
            return None
    return {"state": kv.get("Filesystem state"), "mount_count": num("Mount count"), "max_mount_count": num("Maximum mount count"),
            "last_checked": date("Last checked"), "check_interval": kv.get("Check interval"),
            "errors_behavior": kv.get("Errors behavior"), "lifetime_writes": kv.get("Lifetime writes")}


def _fsck_result_load():
    try:
        with open(FSCK_RESULT) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _boot_time():
    return time.time() - float((read_text_file("/proc/uptime") or "0").split()[0])


def fsck_state(auto_restore=True):
    """根分割的 e2fsck 狀態（tune2fs -l 要 sudoers 放行）。我們排程的方式是把最大掛載次數設 1；
    看到「這次開機已檢查過」就自動恢復 -1 並把結果記下來，否則之後每次開機都會檢查。"""
    src, _ = _root_source()
    if src != ROOT_DEV:
        return {"available": False, "applies": False, "note": msg('fsck_root_mismatch', LANG_DEFAULT, p0=src or "—", p1=ROOT_DEV)}
    out, note = _sudo(FSCK_CMDS["info"])
    if out is None:
        return {"available": False, "applies": True, "note": note, "last_result": _fsck_result_load()}
    p = _parse_tune2fs(out)
    ran_this_boot = p["last_checked"] is not None and p["last_checked"].timestamp() >= _boot_time() - 600   # initramfs 在核心起來後幾秒跑，留時鐘誤差裕度
    scheduled = p["max_mount_count"] == 1
    result = _fsck_result_load()
    if scheduled and ran_this_boot and auto_restore:
        _, err = _sudo(FSCK_CMDS["unschedule"])
        if err is None:
            scheduled = False
            result = {"checked_at": p["last_checked"].isoformat(timespec="seconds"), "state": p["state"],
                      "recorded": datetime.now().isoformat(timespec="seconds")}
            try:
                os.makedirs(os.path.dirname(FSCK_RESULT), exist_ok=True)
                with open(FSCK_RESULT, "w") as f:
                    json.dump(result, f)
            except OSError:
                pass
    return {"available": True, "applies": True, "state": p["state"], "mount_count": p["mount_count"], "max_mount_count": p["max_mount_count"],
            "last_checked": p["last_checked"].isoformat(timespec="seconds") if p["last_checked"] else None,
            "scheduled": scheduled, "ran_this_boot": ran_this_boot, "last_result": result,
            "errors_behavior": p["errors_behavior"], "lifetime_writes": p["lifetime_writes"]}


def fsck_schedule(enable):
    st = fsck_state(auto_restore=False)
    if not st.get("available"):
        return {"ok": False, "error": st.get("note")}
    _, err = _sudo(FSCK_CMDS["schedule" if enable else "unschedule"])
    if err:
        return {"ok": False, "error": err}
    disk_check_invalidate()
    return {"ok": True}


def disk_check_invalidate():
    with _DISKCHECK["lock"]:
        _DISKCHECK["ts"] = 0


def disk_check_reset():
    """sudoers 剛裝／刪：支援與否的答案可能從「沒放行」變成有答案，連同資料一起作廢。"""
    with _DISKCHECK["lock"]:
        _DISKCHECK.update(ts=0, support=None)


def fsck_applies():
    return _root_source()[0] == ROOT_DEV


def disk_check(force=False):
    """磁碟檢查面板的資料：15 秒快取（檢測中前端會輪詢）。不進 hardware_static 的 1 小時快照——會變的狀態不塞靜態快取。"""
    with _DISKCHECK["lock"]:
        if not force and _DISKCHECK["data"] and time.time() - _DISKCHECK["ts"] < DISKCHECK_TTL:
            return _DISKCHECK["data"]
    data = {"ext4": _ext4_errors(), "kernel": _kernel_disk_events(), "trim": _trim_status(),
            "selftest": _selftest_status(), "fsck": fsck_state(), "generated": datetime.now().isoformat(timespec="seconds")}
    with _DISKCHECK["lock"]:
        _DISKCHECK.update(ts=time.time(), data=data)
    return data


def cached_age():
    """Age of the hardware snapshot used in the HTTP response."""
    return int(time.time() - _HW_CACHE["ts"])
