"""磁碟檢查的隔離測試：mock 掉 sudo／journalctl／sysfs，不碰真 SSD、不改 tune2fs。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import hardware as hw


def fake_run(table):
    """subprocess.run 的替身：依指令（去掉 sudo -n）查表，表裡沒有就當 sudo 拒絕。"""
    def run(cmd, **kw):
        key = tuple(cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd)
        for k, (rc, out, err) in table.items():
            if key[:len(k)] == k:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required")
    return run


class SelfTestLogParsing(unittest.TestCase):
    def test_nvme_cli_1x_keys(self):
        j = {"Current Device Self-Test Operation": 1, "Current Device Self-Test Completion": 37,
             "Self-test Result Data": [{"Self-test Result": 0, "Self-test Code": 1, "Segment number": 0, "Valid Diagnostic Information": 0, "Power on hours": 4000},
                                       {"Self-test Result": 15, "Self-test Code": 0}]}
        p = hw._parse_selftest_log(j)
        self.assertTrue(p["running"]); self.assertEqual(p["progress"], 37); self.assertEqual(p["current_code"], 1)
        self.assertEqual(p["entries"], [{"result": 0, "code": 1, "segment": 0, "power_on_hours": 4000, "valid": 0}])

    def test_nvme_cli_2x_nested_lowercase_keys(self):
        j = {"current_operation": 0, "completion": 0,
             "result": [{"device_self_test_status": {"self_test_result": 7, "self_test_code": 2}, "segment_number": 3, "power_on_hours": 4100}]}
        p = hw._parse_selftest_log(j)
        self.assertFalse(p["running"])
        self.assertEqual(p["entries"][0]["result"], 7); self.assertEqual(p["entries"][0]["code"], 2); self.assertEqual(p["entries"][0]["segment"], 3)

    def test_unused_entries_dropped_and_missing_fields_are_none(self):
        p = hw._parse_selftest_log({"x": [{"Self-test Result": 15}, {"Self-test Result": 1}]})
        self.assertEqual(len(p["entries"]), 1); self.assertIsNone(p["entries"][0]["code"]); self.assertIsNone(p["progress"])


class SelfTestFlow(unittest.TestCase):
    def setUp(self):
        hw._DISKCHECK["support"] = None

    def test_not_allowed_in_sudoers_reports_not_configured(self):
        with patch.object(hw.subprocess, "run", fake_run({})), patch.object(hw.os.path, "exists", return_value=True):
            s = hw._selftest_status()
        self.assertFalse(s["available"]); self.assertTrue(s["applies"]); self.assertIn("sudoers", s["note"])
        self.assertEqual(hw.selftest_start("short")["ok"], False)

    def test_unsupported_controller_never_starts(self):
        table = {("/usr/sbin/nvme", "id-ctrl"): (0, json.dumps({"oacs": 0x0f, "edstt": 0}), "")}
        with patch.object(hw.subprocess, "run", fake_run(table)) as run, patch.object(hw.os.path, "exists", return_value=True):
            s = hw._selftest_status()
            r = hw.selftest_start("extended")
        self.assertEqual(s["supported"], False); self.assertFalse(r["ok"])
        self.assertIsNone(hw._DISKCHECK["support"]["extended_minutes"])

    def test_supported_runs_exact_sudoers_command_and_invalidates_cache(self):
        calls = []
        table = {("/usr/sbin/nvme", "id-ctrl"): (0, json.dumps({"oacs": 0x1f, "edstt": 45}), ""),
                 ("/usr/sbin/nvme", "self-test-log"): (0, json.dumps({"Current Device Self-Test Operation": 0, "Current Device Self-Test Completion": 0, "Self-test Result Data": []}), ""),
                 ("/usr/sbin/nvme", "device-self-test"): (0, "Device self-test started\n", "")}
        base = fake_run(table)
        def run(cmd, **kw): calls.append(cmd); return base(cmd, **kw)
        with patch.object(hw.subprocess, "run", run), patch.object(hw.os.path, "exists", return_value=True):
            s = hw._selftest_status()
            hw._DISKCHECK.update(ts=time.time(), data={"stale": True})
            r = hw.selftest_start("short")
        self.assertTrue(s["supported"]); self.assertEqual(s["extended_minutes"], 45); self.assertFalse(s["running"])
        self.assertTrue(r["ok"]); self.assertEqual(hw._DISKCHECK["ts"], 0)
        self.assertIn(["sudo", "-n"] + hw.SELFTEST_CMDS["short"], calls)
        self.assertEqual(hw.selftest_start("badblocks")["ok"], False)


TUNE2FS = """Filesystem volume name:   <none>
Filesystem state:         clean
Errors behavior:          Remount read-only
Mount count:              {mc}
Maximum mount count:      {mmc}
Last checked:             {lc}
Check interval:           0 (<none>)
Lifetime writes:          14 TB
"""


class FsckFlow(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.result = os.path.join(self.td.name, "fsck-result.json")
        self.p1 = patch.object(hw, "FSCK_RESULT", self.result); self.p1.start()
        self.p2 = patch.object(hw, "_root_source", return_value=(hw.ROOT_DEV, "ext4")); self.p2.start()
        self.p3 = patch.object(hw, "_boot_time", return_value=time.mktime((2026, 10, 11, 8, 0, 0, 0, 0, -1))); self.p3.start()

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3): p.stop()
        self.td.cleanup()

    def test_root_not_the_fixed_device_does_not_apply(self):
        with patch.object(hw, "_root_source", return_value=("/dev/sda1", "ext4")):
            st = hw.fsck_state()
        self.assertFalse(st["available"]); self.assertFalse(st["applies"]); self.assertIn("/dev/sda1", st["note"])

    def test_scheduled_and_not_yet_checked_stays_scheduled(self):
        table = {("/usr/sbin/tune2fs", "-l"): (0, TUNE2FS.format(mc=1, mmc=1, lc="Sat Oct 10 07:00:00 2026"), "")}
        calls = []
        base = fake_run(table)
        def run(cmd, **kw): calls.append(tuple(cmd)); return base(cmd, **kw)
        with patch.object(hw.subprocess, "run", run):
            st = hw.fsck_state()
        self.assertTrue(st["scheduled"]); self.assertFalse(st["ran_this_boot"]); self.assertIsNone(st["last_result"])
        self.assertFalse(any("-c" in c for c in calls))

    def test_check_done_this_boot_restores_and_records_result(self):
        table = {("/usr/sbin/tune2fs", "-l"): (0, TUNE2FS.format(mc=1, mmc=1, lc="Sun Oct 11 08:00:30 2026"), ""),
                 ("/usr/sbin/tune2fs", "-c", "-1"): (0, "", "")}
        calls = []
        base = fake_run(table)
        def run(cmd, **kw): calls.append(cmd); return base(cmd, **kw)
        with patch.object(hw.subprocess, "run", run):
            st = hw.fsck_state()
        self.assertFalse(st["scheduled"]); self.assertTrue(st["ran_this_boot"])
        self.assertEqual(st["last_result"]["state"], "clean"); self.assertEqual(st["last_result"]["checked_at"], "2026-10-11T08:00:30")
        self.assertIn(["sudo", "-n"] + hw.FSCK_CMDS["unschedule"], calls)
        self.assertEqual(json.load(open(self.result))["state"], "clean")

    def test_restore_denied_keeps_scheduled_and_no_result_file(self):
        table = {("/usr/sbin/tune2fs", "-l"): (0, TUNE2FS.format(mc=1, mmc=1, lc="Sun Oct 11 08:00:30 2026"), "")}
        with patch.object(hw.subprocess, "run", fake_run(table)):
            st = hw.fsck_state()
        self.assertTrue(st["scheduled"]); self.assertFalse(os.path.exists(self.result))

    def test_schedule_uses_exact_command_and_denied_is_reported(self):
        table = {("/usr/sbin/tune2fs", "-l"): (0, TUNE2FS.format(mc=3, mmc=-1, lc="Sat Jan  4 10:00:00 2025"), ""),
                 ("/usr/sbin/tune2fs", "-c", "1"): (0, "", "")}
        calls = []
        base = fake_run(table)
        def run(cmd, **kw): calls.append(cmd); return base(cmd, **kw)
        with patch.object(hw.subprocess, "run", run):
            st = hw.fsck_state(); r = hw.fsck_schedule(True); r2 = hw.fsck_schedule(False)
        self.assertEqual(st["last_checked"], "2025-01-04T10:00:00"); self.assertFalse(st["scheduled"])
        self.assertTrue(r["ok"]); self.assertIn(["sudo", "-n"] + hw.FSCK_CMDS["schedule"], calls)
        self.assertFalse(r2["ok"]); self.assertIn("sudoers", r2["error"])

    def test_not_allowed_still_shows_previous_result(self):
        json.dump({"checked_at": "2026-10-01T00:00:00", "state": "clean"}, open(self.result, "w"))
        with patch.object(hw.subprocess, "run", fake_run({})):
            st = hw.fsck_state()
        self.assertFalse(st["available"]); self.assertEqual(st["last_result"]["state"], "clean")


class Passive(unittest.TestCase):
    def test_ext4_sysfs_counter(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "nvme0n1p2"; d.mkdir()
            (d / "errors_count").write_text("2\n"); (d / "first_error_time").write_text("1760000000\n"); (d / "last_error_time").write_text("0\n"); (d / "last_error_func").write_text("ext4_lookup\n")
            with patch.object(hw, "_root_source", return_value=("/dev/nvme0n1p2", "ext4")), patch.object(hw.os.path, "realpath", side_effect=lambda p: p):
                e = hw._ext4_errors(sysfs=td)
            self.assertEqual(e["errors_count"], 2); self.assertIsNotNone(e["first_error"]); self.assertIsNone(e["last_error"]); self.assertEqual(e["last_error_func"], "ext4_lookup")
            with patch.object(hw, "_root_source", return_value=("/dev/sda1", "xfs")):
                self.assertFalse(hw._ext4_errors(sysfs=td)["available"])

    def test_kernel_event_matching(self):
        text = "\n".join(["2026-10-10T22:32:05 k kernel: NVRM: Out of memory",            # GPU，不是磁碟
                         "2026-10-10T22:33:05 k kernel: EXT4-fs error (device nvme0n1p2): ext4_lookup:1234: inode #5: comm x: deleted inode referenced",
                         "2026-10-10T22:34:05 k kernel: nvme nvme0: I/O 12 QID 3 timeout, aborting",
                         "2026-10-10T22:35:05 k kernel: Buffer I/O error on dev nvme0n1p2, logical block 1, lost async page write",
                         "2026-10-10T22:36:05 k kernel: usb 1-1: device descriptor read/64, error -71"])   # USB 錯誤，不算
        r = hw._count_disk_events(text)
        self.assertEqual(r["count"], 3); self.assertIn("Buffer I/O error", r["last"])
        self.assertEqual(hw._count_disk_events("")["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
