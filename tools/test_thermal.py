"""具名溫度區 ↔ 核心段的對應：純函式，不讀 /sys。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import hardware

GX10_MHZ = [2808] * 5 + [3900] * 5 + [2808] * 5 + [3900] * 5
GX10_PARTS = ["0xd87"] * 5 + ["0xd85"] * 5 + ["0xd87"] * 5 + ["0xd85"] * 5


class ZoneCoreTests(unittest.TestCase):
    def test_gx10_zones_map_to_the_measured_core_ranges(self):
        runs = hardware.core_runs(GX10_MHZ, GX10_PARTS)
        got = {n: (hardware.zone_cores(n, runs) or {}) for n in ("TS0E", "TS0P", "TS1E", "TS1P")}
        self.assertEqual({n: (r["first"], r["last"], r["part"]) for n, r in got.items()},
                         {"TS0E": (0, 4, "Cortex-A725"), "TS0P": (5, 9, "Cortex-X925"),
                          "TS1E": (10, 14, "Cortex-A725"), "TS1P": (15, 19, "Cortex-X925")})

    def test_zones_without_a_core_meaning_map_to_nothing(self):
        runs = hardware.core_runs(GX10_MHZ, GX10_PARTS)
        for name in ("TSOC", "TGPU", "TUNC", "TS2P", "TS0X", "", None):
            self.assertIsNone(hardware.zone_cores(name, runs), name)

    def test_uniform_cores_have_no_e_p_split_so_no_zone_claims_cores(self):
        runs = hardware.core_runs([3000] * 8, ["0xd85"] * 8)
        self.assertEqual([(r["first"], r["last"], r["kind"]) for r in runs], [(0, 7, None)])
        self.assertIsNone(hardware.zone_cores("TS0P", runs))

    def test_an_unreadable_core_frequency_disables_the_mapping_instead_of_guessing(self):
        runs = hardware.core_runs([2808, None, 3900, 3900], [])
        self.assertTrue(all(r["kind"] is None for r in runs))
        self.assertIsNone(hardware.zone_cores("TS0P", runs))

    def test_unknown_part_keeps_the_range_but_names_no_core_type(self):
        runs = hardware.core_runs([1000, 1000, 2000, 2000], ["0xfff"] * 4)
        self.assertEqual((hardware.zone_cores("TS0P", runs)["first"], hardware.zone_cores("TS0P", runs)["part"]), (2, None))


if __name__ == "__main__":
    unittest.main()
