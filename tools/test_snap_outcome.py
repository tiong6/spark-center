"""snap 更新的結局判定：變更是 Error，但只有收尾的 check-rerefresh（再問一次商店）失敗時，版本其實已換好。
測資是 2026-11-28 firefox 9180→9262 的真實變更 271（商店回 HTTP 408）。跑法：python3 tools/test_snap_outcome.py"""
import copy
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

CHANGE = json.loads((ROOT / "tools/fixtures/snap-change-271.json").read_text())


def _run_job(change):
    """用真的 Job._run_snap 跑到底；snapd 與 pkexec 都換掉，不碰真機。"""
    job = server.Job()
    job.state.update(kind="snap", status="running", packages=["firefox"])
    with mock.patch.object(server, "snap_change_for", return_value="271"), \
         mock.patch.object(server, "_snapd_get", return_value=change), \
         mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("must not spawn")), \
         mock.patch.object(server, "apps_cache_invalidate"), mock.patch.object(server, "status_cache_clear"):
        job._run_snap(["firefox"])
    return job.state


class PostCheckTests(unittest.TestCase):
    def test_real_change_271_is_done_with_warning(self):
        self.assertEqual(CHANGE["status"], "Error")
        st = _run_job(CHANGE)
        self.assertEqual(st["status"], "done")
        self.assertIsNone(st["error"])
        self.assertIn("firefox 已換成新版", st["warning"])
        self.assertIn("HTTP status code 408", st["warning"])
        self.assertNotIn("ERROR", st["warning"])          # 時間戳與 ERROR 前綴已剝掉
        self.assertTrue(any("408" in l for l in st["log"]))   # 原始錯誤行仍留在詳細記錄

    def test_download_failure_stays_error(self):
        ch = copy.deepcopy(CHANGE)
        for t in ch["tasks"]:
            if t["kind"] == "download-snap":
                t["status"], t["log"] = "Error", ["2026-11-28T06:27:45+08:00 ERROR unexpected EOF"]
            elif t["kind"] != "prerequisites":
                t["status"] = "Hold"
        st = _run_job(ch)
        self.assertEqual(st["status"], "error")
        self.assertIsNone(st["warning"])

    def test_undone_change_stays_error(self):
        ch = copy.deepcopy(CHANGE)   # link 失敗後 snapd 把前面還原：舊版還在，不能說換好了
        for t in ch["tasks"]:
            if t["kind"] == "link-snap":
                t["status"] = "Error"
            elif t["kind"] == "unlink-current-snap":
                t["status"] = "Undone"
        self.assertFalse(server.snap_post_check_only(ch["tasks"]))
        self.assertEqual(_run_job(ch)["status"], "error")

    def test_pure_cases(self):
        f = server.snap_post_check_only
        self.assertTrue(f(CHANGE["tasks"]))
        self.assertFalse(f([dict(t, status="Done") for t in CHANGE["tasks"]]))   # 全 Done 不是這個情況
        self.assertFalse(f([{"kind": "check-rerefresh", "status": "Error"}]))    # 沒有 link-snap＝沒換過版本
        self.assertFalse(f([]))

    def test_english_translation(self):
        st = _run_job(CHANGE)
        en = server._message_text(st["warning"], "en")
        self.assertTrue(en.startswith("firefox is now on the new revision"), en)
        self.assertIn("HTTP status code 408", en)
        self.assertIsNone(re.search("[一-鿿]", en), en)


if __name__ == "__main__":
    unittest.main()
