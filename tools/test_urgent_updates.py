"""backend/urgent_updates.py 的隔離測試：挑選門檻、只通知一次、狀態檔讀寫。跑法：python3 tools/test_urgent_updates.py"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import urgent_updates as uu  # noqa: E402


def it(name, cand="2"):
    return {"name": name, "candidate": cand}


class CollectTests(unittest.TestCase):
    def test_threshold_matches_red_tag(self):
        items = [it("a"), it("b"), it("c"), it("d"), it("e"), it("f")]
        meta = {"a": {"ok": True, "urgency": "high", "summary": "s", "cves": 1},
                "b": {"ok": True, "urgency": "medium"}, "c": {"ok": True, "urgency": "critical"},
                "d": None, "e": {"ok": False, "urgency": "high"}, "f": {"ok": True, "urgency": "emergency"}}
        got = uu.collect(items, meta)
        self.assertEqual([u["name"] for u in got], ["a", "c", "f"])   # medium 不算；沒抓到／抓失敗不猜
        self.assertEqual(got[0], {"name": "a", "candidate": "2", "urgency": "high", "summary": "s", "cves": 1})

    def test_notify_once_per_version(self):
        u1 = uu.collect([it("hplip", "3.1")], {"hplip": {"ok": True, "urgency": "high"}})
        self.assertEqual(len(uu.unnotified(u1, set())), 1)
        done = {uu.key(u) for u in u1}
        self.assertEqual(uu.unnotified(u1, done), [])
        u2 = uu.collect([it("hplip", "3.2")], {"hplip": {"ok": True, "urgency": "high"}})
        self.assertEqual(len(uu.unnotified(u2, done)), 1)   # 新的候選版本再通知

    def test_state_roundtrip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "n.json")
            self.assertEqual(uu.load(p), set())
            uu.save(p, {"a 1", "b 2"})
            self.assertEqual(uu.load(p), {"a 1", "b 2"})
            with open(p, "w") as f:
                f.write("{not json")
            self.assertEqual(uu.load(p), set())
            with open(p, "w") as f:
                f.write('{"a": 1}')
            self.assertEqual(uu.load(p), set())

    def test_names_text(self):
        us = [{"name": n} for n in "abcde"]
        self.assertEqual(uu.names_text(us, sep=", "), ("a, b, c, d", True))
        self.assertEqual(uu.names_text(us[:2]), ("a、b", False))


if __name__ == "__main__":
    unittest.main()
