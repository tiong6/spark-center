"""列印堆疊判斷與 snap 更新預檢的隔離測試：純判斷＋假 /proc 目錄，不碰 snapd／systemctl。
起因（2026-10-23）：預檢把 cups snap 自己的 cupsd 服務當成使用者程式，擋掉了 snap 更新。"""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
from backend.hardware import print_stack  # noqa: E402
import server  # noqa: E402


class PrintStackTests(unittest.TestCase):
    def test_proxy_coexistence_is_normal(self):
        st = print_stack(True, True, True, snap_proxy=True, snap_active=True, snap_clients=["chromium"])
        self.assertEqual(st["kind"], "proxy"); self.assertFalse(st["warn"])

    def test_two_independent_servers_warn(self):
        st = print_stack(True, True, True, snap_proxy=False, snap_active=True, snap_clients=[])
        self.assertEqual(st["kind"], "dual"); self.assertTrue(st["warn"])

    def test_snap_installed_but_stopped_is_not_dual(self):
        st = print_stack(True, True, True, snap_proxy=False, snap_active=False, snap_clients=[])
        self.assertEqual(st["kind"], "proxy"); self.assertFalse(st["warn"])

    def test_apt_only_warns_when_not_running(self):
        self.assertFalse(print_stack(True, True, False, False, False, [])["warn"])
        self.assertTrue(print_stack(True, False, False, False, False, [])["warn"])
        self.assertEqual(print_stack(False, False, True, False, True, [])["kind"], "snap_only")
        self.assertEqual(print_stack(False, False, False, False, False, [])["kind"], "none")


def fake_proc(entries):
    """entries: {pid: (exe, cgroup, cmdline)}；exe 是 symlink 目標，None 表示讀不到。"""
    root = tempfile.mkdtemp()
    for pid, (exe, cg, cmd) in entries.items():
        d = os.path.join(root, str(pid)); os.makedirs(d)
        if exe:
            os.symlink(exe, os.path.join(d, "exe"))
        open(os.path.join(d, "cgroup"), "w").write(cg)
        open(os.path.join(d, "cmdline"), "wb").write(cmd.replace(" ", "\0").encode() + b"\0")
    open(os.path.join(root, "self"), "w").close()   # 非數字項目要被跳過
    return root


class SnapBusyTests(unittest.TestCase):
    def test_services_are_flagged_and_apps_are_not(self):
        proc = fake_proc({
            2669: ("/snap/cups/1264/bin/sh", "0::/system.slice/snap.cups.cupsd.service\n", "/bin/sh /snap/cups/1264/scripts/run-cupsd"),
            3786: ("/snap/cups/1264/sbin/cupsd", "0::/system.slice/snap.cups.cupsd.service\n", "cupsd -f"),
            4000: ("/snap/cups/1264/bin/lpstat", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/snap.cups.lpstat-abc.scope\n", "lpstat -p"),
            4001: (None, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/snap.cups.accept-def.scope\n", "accept x"),   # exe 讀不到，靠 cgroup
            5000: ("/usr/bin/bash", "0::/user.slice/x.scope\n", "bash"),
        })
        hits = {h["pid"]: h for h in server.snap_running_apps("cups", proc=proc)}
        self.assertEqual(sorted(hits), [2669, 3786, 4000, 4001])
        self.assertTrue(hits[2669]["service"]); self.assertTrue(hits[3786]["service"])
        self.assertFalse(hits[4000]["service"]); self.assertFalse(hits[4001]["service"])
        self.assertEqual([h["pid"] for h in hits.values() if not h["service"]], [4000, 4001])

    def test_similar_snap_name_does_not_match(self):
        proc = fake_proc({7: ("/snap/cups-filters/1/bin/x", "0::/system.slice/snap.cups-filters.x.service\n", "x")})
        self.assertEqual(server.snap_running_apps("cups", proc=proc), [])


if __name__ == "__main__":
    unittest.main()
