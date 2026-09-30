"""GPU 過熱判定的隔離測試。跑法：python3 tools/test_gpu_hot.py"""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
import server  # noqa: E402

N = server.GPU_HOT_SAMPLES


def S(*temps):
    return [{"temp_c": v} for v in temps]


class T(unittest.TestCase):
    def test_no_threshold_never_alerts(self):
        self.assertEqual(server.gpu_hot_evaluate(S(*[99] * N), None), (False, None))

    def test_needs_full_minute_above(self):
        self.assertEqual(server.gpu_hot_evaluate(S(*[86] * (N - 1)), 86)[0], False)
        self.assertEqual(server.gpu_hot_evaluate(S(*[86] * N), 86), (True, 86))

    def test_one_dip_resets(self):
        self.assertEqual(server.gpu_hot_evaluate(S(*([88] * (N - 1) + [85])), 86)[0], False)
        self.assertEqual(server.gpu_hot_evaluate(S(*([85] + [88] * (N - 1))), 86)[0], False)

    def test_bouncing_around_threshold_does_not_alert(self):
        self.assertEqual(server.gpu_hot_evaluate(S(*([86, 85] * (N // 2))), 86)[0], False)

    def test_clears_only_below_margin(self):
        cur = {"since": "x"}
        self.assertTrue(server.gpu_hot_evaluate(S(*([88] * N + [84])), 86, cur)[0])   # 84 ≥ 86-3，還在警報
        self.assertFalse(server.gpu_hot_evaluate(S(*([88] * N + [82])), 86, cur)[0])  # 82 < 83，解除

    def test_missing_samples_ignored(self):
        self.assertEqual(server.gpu_hot_evaluate(S(*([87] * N + [None])), 86)[0], True)


if __name__ == "__main__":
    unittest.main()
