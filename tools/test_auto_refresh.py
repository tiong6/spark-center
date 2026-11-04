"""backend/auto_refresh.py 的隔離測試：判斷、設定檔讀寫、loginctl 解析。跑法：python3 tools/test_auto_refresh.py"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import auto_refresh as ar  # noqa: E402

H = 3600


class VerdictTests(unittest.TestCase):
    def v(self, **kw):
        base = dict(hours=6, last_refresh_ts=1000, now=1000 + 7 * H, job_running=False, session_active=True, readonly=False)
        base.update(kw)
        return ar.verdict(**base)

    def test_due_after_interval(self):
        self.assertEqual(self.v(), "due")

    def test_fresh_inside_interval(self):
        self.assertEqual(self.v(now=1000 + 5 * H), "fresh")
        self.assertEqual(self.v(now=1000 + 6 * H), "due")   # 剛好到期就跑

    def test_off(self):
        self.assertEqual(self.v(hours=0), "off")

    def test_readonly_wins_over_everything(self):
        self.assertEqual(self.v(readonly=True, hours=0), "readonly")

    def test_job_running_waits(self):
        self.assertEqual(self.v(job_running=True), "job_running")

    def test_no_session_skips_only_when_due(self):
        self.assertEqual(self.v(session_active=False), "no_active_session")
        self.assertEqual(self.v(session_active=False, now=1000 + H), "fresh")

    def test_unknown_last_refresh_counts_as_due(self):
        self.assertEqual(self.v(last_refresh_ts=None, now=0), "due")


class SettingsTests(unittest.TestCase):
    def test_default_when_missing_or_bad(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            self.assertEqual(ar.load(p), ar.DEFAULT_HOURS)
            Path(p).write_text("not json")
            self.assertEqual(ar.load(p), ar.DEFAULT_HOURS)
            Path(p).write_text('{"auto_refresh_hours": 7}')   # 不在選項裡的值不採用
            self.assertEqual(ar.load(p), ar.DEFAULT_HOURS)

    def test_save_keeps_other_keys_and_rejects_bad_hours(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "settings.json")
            ar.save(p, 12)
            self.assertEqual(ar.load(p), 12)
            Path(p).write_text('{"other": 1, "auto_refresh_hours": 1}')
            ar.save(p, 0)
            self.assertEqual(ar.load(p), 0)
            self.assertIn('"other": 1', Path(p).read_text())
            with self.assertRaises(ValueError):
                ar.save(p, 7)
            self.assertEqual(ar.load(p), 0)


class SessionTests(unittest.TestCase):
    OUT = ("1150 1000 tungyu6 -     -    closing no -\n"
           "   4 1000 tungyu6 seat0 tty2 active  no -\n"
           "   9 1001 other   seat0 tty3 active  no -\n")

    def test_active_for_uid(self):
        self.assertTrue(ar.session_active(self.OUT, 1000))
        self.assertTrue(ar.session_active(self.OUT, 1001))
        self.assertFalse(ar.session_active(self.OUT, 1002))

    def test_closing_or_empty_is_not_active(self):
        self.assertFalse(ar.session_active("1150 1000 tungyu6 - - closing no -\n", 1000))
        self.assertFalse(ar.session_active("", 1000))
        self.assertFalse(ar.session_active(None, 1000))


if __name__ == "__main__":
    unittest.main()
