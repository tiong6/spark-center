"""「略過保留舊版」的隔離測試：假 apt 快取、假下載，暫存目錄，不碰網路／apt／真的降回索引。
跑法：python3 tools/test_rollback_skip.py"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


def _no_subprocess(*a, **k):
    raise AssertionError(f"unpatched subprocess call: {a}")


class FakePkg:
    def __init__(self, name):
        self.name = name
        self.installed = SimpleNamespace(version="1.0", architecture="arm64", sha256=None, uri=None)
        self.candidate = SimpleNamespace(version="2.0")
        self.marked_delete = False
        self.phasing_applied = False
        self.versions = [self.installed]


class SkipTests(unittest.TestCase):
    def run_prepare(self, names, fetch, skipped):
        cache = {n: FakePkg(n) for n in names}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(subprocess, "run", _no_subprocess), \
                mock.patch.object(server.apt, "Cache", lambda: cache), \
                mock.patch.object(server, "ROLLBACK_DIR", d), \
                mock.patch.object(server, "_rollback_changes", lambda c, n: [(x, True) for x in n]), \
                mock.patch.object(server, "_origin_of", lambda v: {"site": "ports.ubuntu.com"}), \
                mock.patch.object(server, "_fetch_file", fetch), \
                mock.patch.object(server, "_deb_matches", lambda *a: True), \
                mock.patch.object(server, "_rollback_index_add", lambda rec: None):
            rec = server.rollback_prepare(names, skipped=skipped)
            left = os.listdir(os.path.join(d, rec["id"]))
        return rec, left

    def test_skip_mid_download_aborts_and_skips_rest(self):
        flag = {"skip": False}
        calls = []

        def fetch(url, dest, max_bytes, progress):
            calls.append(url)
            with open(dest, "wb") as f:
                f.write(b"x")
            if "b_" in url:              # 第二個套件下載到一半，使用者按了略過
                progress(10, 100)
                flag["skip"] = True
                progress(20, 100)
                raise AssertionError("progress should have raised")
            progress(100, 100)
            return True, None

        rec, left = self.run_prepare(["a", "b", "c"], fetch, lambda: flag["skip"])
        by = {p["name"]: p for p in rec["packages"]}
        self.assertTrue(by["a"]["deb"])                      # 已保留的不受影響
        self.assertIsNone(by["b"]["deb"])
        self.assertEqual(by["b"]["reason"], server.msg("rollback_skipped", server.LANG_DEFAULT))
        self.assertEqual(by["c"]["reason"], server.msg("rollback_skipped", server.LANG_DEFAULT))
        self.assertEqual(len(calls), 2)                      # c 根本沒去抓
        self.assertEqual(left, ["a_1.0_arm64.deb"])          # 抓到一半的 b 檔案有清掉

    def test_no_skip_keeps_all(self):
        def fetch(url, dest, max_bytes, progress):
            open(dest, "wb").close(); progress(1, 1); return True, None
        rec, left = self.run_prepare(["a", "b"], fetch, lambda: False)
        self.assertTrue(all(p["deb"] for p in rec["packages"]))
        self.assertEqual(len(left), 2)


if __name__ == "__main__":
    unittest.main()
