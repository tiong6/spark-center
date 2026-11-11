"""backend/flatpak_progress.py 的隔離測試。測資是 flatpak 1.14.6 在 C 語系、stdout 接 pipe 時的真實輸出
（2026-11-11 在隔離的 FLATPAK_USER_DIR 錄下）。跑法：python3 tools/test_flatpak_progress.py"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import flatpak_progress as fp  # noqa: E402
from backend.messages import MSG  # noqa: E402

TWO_ITEMS = """Looking for matches…


 1.\t   \torg.freedesktop.Platform.VAAPI.nvidia\t26.08\ti\tflathub\t< 75.0\xa0kB
 2.\t   \torg.freedesktop.Platform.codecs-extra\t26.08-extra\ti\tflathub\t< 16.4\xa0MB


Installing 1/2…
Installing 1/2…                        0%  0 bytes/s
Installing 1/2… ████████████████████ 100%
Installing 2/2…
Installing 2/2…                        0%  0 bytes/s
Installing 2/2… █▊                     9%
Installing 2/2… ████████████▏         61%
Installing 2/2… ████████████████████ 100%
Installation complete.""".split("\n")

ONE_ITEM = """Looking for matches…
 1.\t   \torg.freedesktop.Platform.codecs-extra\t26.08-extra\ti\tflathub\t< 16.4\xa0MB
Installing…
Installing…                        0%  0 bytes/s
Installing… █▌                     8%
Installing… ███████████████▌      78%""".split("\n")


def run(lines):
    t = fp.Tracker()
    return t, [t.feed(line) for line in lines]


class TrackerTests(unittest.TestCase):
    def test_table_gives_total_before_anything_downloads(self):
        t, _ = run(TWO_ITEMS[:6])
        s = t.snapshot()
        self.assertEqual((s["count"], s["total"], s["progress"], s["index"]), (2, "16.5 MB", None, None))

    def test_progress_is_weighted_by_download_size(self):
        t, _ = run(TWO_ITEMS[:14])   # 到第 2 項 61%（第 13 行）
        s = t.snapshot()
        self.assertEqual((s["index"], s["ref"], s["size"]), (2, "org.freedesktop.Platform.codecs-extra 26.08-extra", "16.4 MB"))
        # (75 kB + 16.4 MB × 61%) / 16.475 MB ≈ 61.17%：小的第一項不能讓進度跳到 50%
        self.assertEqual(s["progress"], 61)

    def test_new_item_resets_item_percent(self):
        t, _ = run(TWO_ITEMS[:12])   # 第 1 項 100% 後，剛進第 2 項
        s = t.snapshot()
        self.assertEqual((s["index"], s["progress"]), (2, 0))   # 75 kB / 16.475 MB → 0%

    def test_percent_lines_are_marked_so_they_stay_out_of_the_log(self):
        _, kinds = run(TWO_ITEMS)
        self.assertEqual(kinds.count("row"), 2)
        self.assertEqual(kinds.count("item"), 2)
        self.assertEqual(kinds.count("progress"), 6)
        self.assertIsNone(kinds[-1])   # Installation complete.

    def test_single_item_has_no_index_in_text(self):
        t, _ = run(ONE_ITEM)
        s = t.snapshot()
        self.assertEqual((s["index"], s["count"], s["progress"]), (1, 1, 78))

    def test_update_with_removal_and_unknown_sizes(self):
        lines = [
            " 1.\t   \torg.gnome.Platform\t51\ti\tflathub\t< 412.6\xa0MB",
            " 2.\t   \tio.github.nokse22.high-tide\tstable\tu\tflathub\t< 3.1\xa0MB (partial)",
            " 3.\t   \torg.gnome.Platform\t50\tr\tflathub\t",
            "Updating 2/3…",
            "Updating 2/3… ██████████ 50%",
        ]
        t, _ = run(lines)
        s = t.snapshot()
        self.assertEqual((s["count"], s["total"], s["size"]), (3, "415.7 MB", "3.1 MB"))
        self.assertEqual(s["progress"], int((412.6e6 + 3.1e6 * 0.5) * 100 / 415.7e6))

    def test_no_sizes_at_all_falls_back_to_item_count(self):
        t, _ = run([" 1.\t   \ta.b.C\tstable\tr\tflathub\t", " 2.\t   \ta.b.D\tstable\tr\tflathub\t",
                    "Uninstalling 2/2…", "Uninstalling 2/2… ██ 50%"])
        s = t.snapshot()
        self.assertEqual((s["total"], s["progress"]), (None, 75))

    def test_unparseable_output_claims_nothing(self):
        t, kinds = run(["error: Nothing matches foo", "Nothing to do."])
        self.assertIsNone(t.snapshot())
        self.assertEqual(kinds, [None, None])

    def test_format_size_matches_glib_units(self):
        self.assertEqual(fp.format_size(1_402_300_000), "1.4 GB")
        self.assertEqual(fp.format_size(75_000), "75.0 kB")
        self.assertEqual(fp.format_size(512), "512 bytes")

    def test_total_message_exists_in_both_languages(self):
        for lang in ("zh-TW", "en"):
            self.assertEqual(sorted(re.findall(r"\{p\d\}", MSG[lang]["flatpak_total"])), ["{p0}", "{p1}"])


if __name__ == "__main__":
    unittest.main()
