"""changelog 急迫度／摘要解析器的隔離測試：純文字進、dict 出，不碰網路。"""
import os, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
import server  # noqa: E402

SAMPLE = """Get:1 https://changelogs.ubuntu.com openssh 1:9.6p1-3ubuntu13.20 Changelog [388 kB]
openssh (1:9.6p1-3ubuntu13.20) noble-security; urgency=medium

  * SECURITY UPDATE: sftp directory traversal
    - debian/patches/CVE-2026-106552.patch: upstream: sftp: be stricter
    - CVE-2026-106552
  * SECURITY UPDATE: incorrect GSSAPIAuthentication auth persistence
    - CVE-2026-106553
    - CVE-2026-106555

 -- Someone <x@ubuntu.com>  Mon, 13 Oct 2026 10:00:00 +0000

openssh (1:9.6p1-3ubuntu13.19) noble; urgency=high

  * Fix a thing that spans
    two lines of text
  * CVE-2026-106552 mentioned again

 -- Someone <x@ubuntu.com>  Mon, 01 Sep 2026 10:00:00 +0000

openssh (1:9.6p1-3ubuntu13.18) noble; urgency=critical

  * old entry
"""


class ParseTests(unittest.TestCase):
    def test_newer_than_installed_only(self):
        m = server.parse_changelog_meta(SAMPLE, "1:9.6p1-3ubuntu13.18")
        self.assertEqual(m["entries"], 2)
        self.assertEqual(m["urgency"], "high")          # 13.18 的 critical 不算，它是已安裝版
        self.assertEqual(m["summary"], "SECURITY UPDATE: sftp directory traversal")
        self.assertEqual(m["cves"], 3)                  # 106552／106553／106555，重複的只算一次

    def test_installed_unknown_counts_everything(self):
        m = server.parse_changelog_meta(SAMPLE, "9.9")
        self.assertEqual(m["entries"], 3)
        self.assertEqual(m["urgency"], "critical")

    def test_continuation_line_joins_bullet(self):
        m = server.parse_changelog_meta(SAMPLE, "1:9.6p1-3ubuntu13.19")
        self.assertEqual(m["entries"], 1)
        m2 = server.parse_changelog_meta(SAMPLE.split("openssh (1:9.6p1-3ubuntu13.20)")[0] + SAMPLE.split(" -- Someone <x@ubuntu.com>  Mon, 13 Oct 2026 10:00:00 +0000\n\n")[1], "1:9.6p1-3ubuntu13.18")
        self.assertEqual(m2["summary"], "Fix a thing that spans two lines of text")

    def test_not_a_changelog(self):
        self.assertIsNone(server.parse_changelog_meta("E: Changelog unavailable", ""))
        self.assertIsNone(server.parse_changelog_meta("", ""))

    def test_no_urgency_header(self):
        m = server.parse_changelog_meta("foo (1.0) stable\n\n  * hi\n", "")
        self.assertIsNone(m["urgency"]); self.assertEqual(m["summary"], "hi")


if __name__ == "__main__":
    unittest.main(verbosity=1)
