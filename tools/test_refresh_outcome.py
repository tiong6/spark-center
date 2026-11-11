"""server.refresh_fetch_outcome 的隔離測試：apt update 有來源抓不到時，算部分完成還是失敗。
測資是 2026-11-11 Launchpad 掛掉那次的真實來源表與錯誤行。跑法：python3 tools/test_refresh_outcome.py"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

SOURCES = [
    ("https://workbench.download.nvidia.com/stable/linux/debian/", "default"),
    ("https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/nvidia-desktop-edge/ubuntu/", "noble"),
    ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/", "/"),
    ("https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/vulkan-packages-nv-desktop/ubuntu/", "noble"),
    ("https://ppa.launchpadcontent.net/strukturag/libheif/ubuntu/", "noble"),
    ("http://ports.ubuntu.com/ubuntu-ports/", "noble"),
    ("http://ports.ubuntu.com/ubuntu-ports/", "noble-updates"),
]
LAUNCHPAD_DOWN = [
    "https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/nvidia-desktop-edge/ubuntu/dists/noble/InRelease  Could not wait for server fd - select (11: Resource temporarily unavailable) [IP: 185.125.189.37 443]",
    "https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/vulkan-packages-nv-desktop/ubuntu/dists/noble/InRelease  503  Service Unavailable [IP: 185.125.189.69 443]",
    "https://ppa.launchpadcontent.net/strukturag/libheif/ubuntu/dists/noble/InRelease  Could not wait for server fd - select (11: Resource temporarily unavailable) [IP: 185.125.189.187 443]",
]
CJK = re.compile("[一-鿿]")


class OutcomeTests(unittest.TestCase):
    def test_some_sources_down_is_partial_not_failure(self):
        status, error, warning = server.refresh_fetch_outcome(LAUNCHPAD_DOWN, SOURCES)
        self.assertEqual(status, "done")
        self.assertIsNone(error)
        self.assertTrue(warning.startswith("4/7 個來源已更新；3 個暫時抓不到"), warning)
        for line in LAUNCHPAD_DOWN:
            self.assertIn(line, warning)

    def test_every_source_down_is_failure(self):
        failed = [f"{u}dists/{d}/InRelease  Temporary failure resolving" if d != "/" else f"{u}InRelease  Temporary failure resolving" for u, d in SOURCES]
        status, error, warning = server.refresh_fetch_outcome(failed, SOURCES)
        self.assertEqual(status, "error")
        self.assertIsNone(warning)
        self.assertTrue(error.startswith("7 個來源全部抓不到"), error)

    def test_flat_repo_is_matched_without_dists(self):
        failed = ["https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/InRelease  503  Service Unavailable"]
        status, _, warning = server.refresh_fetch_outcome(failed, SOURCES)
        self.assertEqual(status, "done")
        self.assertTrue(warning.startswith("6/7 個來源已更新；1 個"), warning)

    def test_several_files_of_one_source_count_once(self):
        failed = [
            "http://ports.ubuntu.com/ubuntu-ports/dists/noble-updates/main/binary-arm64/Packages.xz  Hash Sum mismatch",
            "http://ports.ubuntu.com/ubuntu-ports/dists/noble-updates/universe/binary-arm64/Packages.xz  Hash Sum mismatch",
        ]
        _, _, warning = server.refresh_fetch_outcome(failed, SOURCES)
        self.assertTrue(warning.startswith("6/7 個來源已更新；1 個"), warning)

    def test_suite_prefix_does_not_swallow_sibling_suite(self):
        # noble 的前綴不能吃到 noble-updates：靠 dists/<dist>/ 結尾的斜線區分
        failed = ["http://ports.ubuntu.com/ubuntu-ports/dists/noble-updates/InRelease  503  Service Unavailable"]
        _, _, warning = server.refresh_fetch_outcome(failed, SOURCES)
        self.assertTrue(warning.startswith("6/7 個來源已更新；1 個"), warning)

    def test_unmatched_url_never_inflates_updated_count(self):
        failed = ["https://elsewhere.example/dists/x/InRelease  404  Not Found"] + LAUNCHPAD_DOWN
        _, _, warning = server.refresh_fetch_outcome(failed, SOURCES)
        self.assertTrue(warning.startswith("4/7 個來源已更新；4 個"), warning)

    def test_unknown_source_list_claims_nothing_about_others(self):
        status, error, warning = server.refresh_fetch_outcome(LAUNCHPAD_DOWN, None)
        self.assertEqual(status, "error")
        self.assertIsNone(warning)
        self.assertNotIn("已更新", error.split("：")[0].replace("有沒有更新", ""))

    def test_english_ui_gets_english_text(self):
        # 翻譯層只認 {pN} 佔位符；這三句以前用 {n}/{list}，英文介面一直顯示中文
        for failed, sources in ((LAUNCHPAD_DOWN, SOURCES), (LAUNCHPAD_DOWN, None), (LAUNCHPAD_DOWN[:1], SOURCES[1:2])):
            _, error, warning = server.refresh_fetch_outcome(failed, sources)
            en = server._message_text(error or warning, "en")
            self.assertIsNone(CJK.search(en), en)
            for line in failed:
                self.assertIn(line, en)


if __name__ == "__main__":
    unittest.main()
