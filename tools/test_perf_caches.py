"""2026-11-21 效能優化那輪的快取：Codex 審出 8 個失效時機／世代的缺陷，這裡每一個都用真的函式重現一次。
不碰 apt 交易、不碰網路；apps 的 snap／flatpak 與 apt 盤點用假函式。跑法：python3 tools/test_perf_caches.py"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


def _app(source, id_, cand=None):
    return {"source": source, "id": id_, "name": id_, "name_zh": None, "candidate": cand, "checked_updates": cand is not None}


class AppsCacheTests(unittest.TestCase):
    def setUp(self):
        server._APPS_CACHE.update(ts=0, data=None, gen=0, building=False)
        self.store_gate = threading.Event()          # 背景商店查詢等這個才回
        self.store_gate.set()
        patches = [
            mock.patch.object(server, "apt_read", lambda: mock.MagicMock(__enter__=lambda s: {}, __exit__=lambda *a: False)),
            mock.patch.object(server, "apps_apt", lambda cache: [_app("apt", "a")]),
            mock.patch.object(server, "apps_snap", self._snap),
            mock.patch.object(server, "apps_flatpak", lambda check_updates: []),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        self.snap_result = [_app("snap", "s", "2.0")]

    def _snap(self, check_updates):
        if check_updates:
            self.store_gate.wait(5)
            if isinstance(self.snap_result, Exception):
                raise self.snap_result
            return self.snap_result
        return [_app("snap", "s")]

    def _settle(self):
        for _ in range(50):
            with server._APPS_CACHE["lock"]:
                d = server._APPS_CACHE["data"]
            if d and not d["checking"]:
                return d
            time.sleep(0.02)
        return d

    def test_inventory_first_then_store_merges(self):
        d = server.list_apps()
        self.assertTrue(d["checking"]); self.assertTrue(all(a["checking"] for a in d["items"] if a["source"] == "snap"))
        d = self._settle()
        self.assertFalse(d["checking"]); self.assertEqual([a["candidate"] for a in d["items"] if a["source"] == "snap"], ["2.0"])

    def test_store_failure_clears_checking_and_marks_source(self):
        self.snap_result = None
        server.list_apps(); d = self._settle()
        self.assertFalse(d["checking"]); self.assertFalse(d["sources"]["snap"]["ok"])
        snap = [a for a in d["items"] if a["source"] == "snap"][0]
        self.assertFalse(snap["checking"]); self.assertIs(snap["checked_updates"], False)

    def test_invalidation_during_store_check_discards_old_result(self):
        self.store_gate.clear()
        server.list_apps()                       # 盤點已寫回，商店查詢卡在 gate
        server.apps_cache_invalidate()           # 期間套件交易結束
        self.store_gate.set(); time.sleep(0.1)
        with server._APPS_CACHE["lock"]:
            self.assertEqual(server._APPS_CACHE["ts"], 0)   # 舊商店結果沒有把失效蓋掉，下一次請求會重建
            self.assertTrue(server._APPS_CACHE["data"]["checking"])

    def test_invalidation_during_build_is_not_lost(self):
        """盤點進行中世代變了：盤點結果不寫回，ts 維持 0。"""
        real_apps_apt = server.apps_apt

        def slow_apt(cache):
            server.apps_cache_invalidate(); return real_apps_apt(cache)
        with mock.patch.object(server, "apps_apt", slow_apt):
            server.list_apps()
        with server._APPS_CACHE["lock"]:
            self.assertEqual(server._APPS_CACHE["ts"], 0)
            self.assertIsNone(server._APPS_CACHE["data"])

    def test_force_during_build_returns_old_and_rebuilds_next(self):
        self.store_gate.set(); server.list_apps(); self._settle()
        with server._APPS_CACHE["lock"]:
            server._APPS_CACHE["building"] = True
        d = server.list_apps(force=True)
        self.assertIsNotNone(d)
        with server._APPS_CACHE["lock"]:
            self.assertEqual(server._APPS_CACHE["ts"], 0); server._APPS_CACHE["building"] = False


class RoundTwoTests(unittest.TestCase):
    """Codex 第二輪重審抓到的 4 個。"""

    def test_removed_apt_config_key_disappears(self):
        import apt_pkg
        apt_pkg.config.set("Spark-Test::Never-Include", "true")
        self.assertTrue(apt_pkg.config.exists("Spark-Test::Never-Include"))
        server._apt_reload_config()
        self.assertFalse(apt_pkg.config.exists("Spark-Test::Never-Include"))
        self.assertTrue(apt_pkg.config.exists("Dir::State"))          # 真正的設定重讀回來了

    def test_config_reload_waits_for_apt_readers(self):
        """別的執行緒拿著 apt_read 時重讀設定：重讀要等它放鎖，讀者看到的設定一直是完整的。"""
        import apt_pkg
        seen = {}; release = threading.Event(); inside = threading.Event()

        def reader():
            with server.apt_read():
                inside.set(); release.wait(5)
                seen["dir_state_while_locked"] = apt_pkg.config.exists("Dir::State")
        with mock.patch.object(server.apt, "Cache", lambda: {}):
            with server._APT["lock"]:
                server._APT.update(cache=None, key=None)
            th = threading.Thread(target=reader); th.start(); inside.wait(5)
            t0 = time.monotonic(); rel = threading.Thread(target=server._apt_reload_config); rel.start()
            time.sleep(0.15); self.assertTrue(rel.is_alive())      # 重讀在等鎖
            release.set(); th.join(); rel.join()
            self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        self.assertTrue(seen["dir_state_while_locked"]); self.assertTrue(apt_pkg.config.exists("Dir::State"))
        with server._APT["lock"]:
            server._APT.update(cache=None, key=None, mods=None)

    def test_module_change_resets_inner_firmware_cache(self):
        server._MODFW_CACHE.update(ts=time.time(), names={"old.bin"})
        with server._APT["lock"]:
            server._APT.update(cache=None, key=None, mods=None)
        with mock.patch.object(server, "_loaded_modules_sig", lambda: 1), mock.patch.object(server.apt, "Cache", lambda: {}):
            with server.apt_read():
                pass
            self.assertEqual(server._MODFW_CACHE["ts"], 0)          # 第一次：簽章從 None 變 1，內層快取作廢
            server._MODFW_CACHE["ts"] = time.time()
            with server.apt_read():
                pass
            self.assertNotEqual(server._MODFW_CACHE["ts"], 0)      # 簽章沒變：不動
        with server._APT["lock"]:
            server._APT.update(cache=None, key=None, mods=None)

    def test_force_during_build_replies_checking(self):
        server._APPS_CACHE.update(ts=time.time(), data={"items": [], "sources": {}, "checking": False, "generated": "x"}, gen=0, building=True)
        try:
            self.assertTrue(server.list_apps(force=True)["checking"])
        finally:
            server._APPS_CACHE.update(building=False, data=None, ts=0)

    def test_post_side_status_is_fresh(self):
        with mock.patch.object(server, "_setup_status", lambda: {"ok": True, "items": [{"enabled": True}]}):
            server._STATUS.update(setup={"ok": True, "items": [{"enabled": False}]}, setup_ts=time.time())   # 動作進行中被 GET 寫進的舊狀態
            self.assertTrue(server.setup_status()["items"][0]["enabled"] is False)   # 一般讀還是快取
            self.assertTrue(server.setup_status_fresh()["items"][0]["enabled"])     # POST 回應走 fresh


class PrivateCacheLockTests(unittest.TestCase):
    """simulate 與 rollback_prepare 自開的 apt.Cache：建立與使用都要在 _APT 鎖裡（Codex 第四輪：鎖外的 required_download 撞上設定重讀會 E:Can't find a source）。"""

    def _cache_requiring_lock(self):
        test = self

        class Pkg:
            def __init__(self, name): self.name = name; self.installed = None; self.candidate = None; self.is_upgradable = False; self.phasing_applied = False; self.marked_delete = False
        class Cache(dict):
            required_download = required_space = broken_count = 0
            def __init__(self): test.assertTrue(server._APT["lock"]._is_owned(), "apt.Cache() built outside the _APT lock"); super().__init__()
            def actiongroup(self): return mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: False)
            def get_changes(self): test.assertTrue(server._APT["lock"]._is_owned(), "get_changes outside the lock"); return []
            def __iter__(self): return iter([])
        return Cache

    def test_simulate_holds_lock(self):
        with mock.patch.object(server.apt, "Cache", self._cache_requiring_lock()), mock.patch.object(server, "list_updates", lambda: []), \
                mock.patch.object(server, "kernel_pairing_warning", lambda *a: None):
            r = server.simulate([])
        self.assertTrue(r["ok"]); self.assertFalse(server._APT["lock"]._is_owned())

    def test_rollback_prepare_reads_apt_under_lock_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(server.apt, "Cache", self._cache_requiring_lock()), \
                mock.patch.object(server, "ROLLBACK_DIR", d), mock.patch.object(server, "_rollback_changes", lambda c, n: [(x, True) for x in n]), \
                mock.patch.object(server, "_rollback_index_add", lambda rec: None):
            rec = server.rollback_prepare(["nothing-installed"])
        self.assertEqual(rec["packages"][0]["reason"], server.msg("rollback_not_installed", server.LANG_DEFAULT))
        self.assertFalse(server._APT["lock"]._is_owned())


class StatusCacheTests(unittest.TestCase):
    def test_in_flight_result_not_resurrected_after_clear(self):
        calls = []

        def slow_status():
            calls.append(1); server.status_cache_clear(); return {"ok": True, "items": [{"enabled": False}]}
        with mock.patch.object(server, "_setup_status", slow_status):
            server.status_cache_clear()
            server.setup_status()
            self.assertIsNone(server._STATUS["setup"])   # 算的途中被清過：不寫回
            server.setup_status(); self.assertEqual(len(calls), 2)


class AptKeyTests(unittest.TestCase):
    def test_conf_key_sees_in_place_edit_inside_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pin = os.path.join(d, "99-pin")
            with open(pin, "w") as f:
                f.write("a")
            with mock.patch.object(server, "_APT_CONF_DIRS", (d,)), mock.patch.object(server, "_APT_CONF_FILES", ()):
                k1 = server._apt_conf_key()
                os.utime(pin, (1, 1))           # 原地改、目錄 mtime 不動
                self.assertNotEqual(server._apt_conf_key(), k1)

    def test_held_returns_plain_dicts(self):
        pkg = mock.MagicMock(); pkg.name = "x"; pkg.is_upgradable = True; pkg.phasing_applied = True; pkg.candidate.version = "2"
        cm = mock.MagicMock(__enter__=lambda s: [pkg], __exit__=lambda *a: False)
        with mock.patch.object(server, "apt_read", lambda: cm), mock.patch.object(server, "phased_info", lambda p: {"percent": 10}):
            self.assertEqual(server.apt_upgradable_held(), [{"name": "x", "candidate": "2", "percent": 10}])


if __name__ == "__main__":
    unittest.main()
