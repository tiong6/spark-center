"""更新風險資訊（上線天數＋NVIDIA 論壇討論）的隔離測試：抓取器全是假的，不碰網路。"""
import os, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
from backend import update_risk as ur  # noqa: E402
import server  # noqa: E402

LM = "Fri, 23 Oct 2026 23:17:27 GMT"
PUB = "2026-10-23T23:17:27Z"
NOW = 1_793_000_000.0
KERNEL = ("linux-image-7.0.0-1024-nvidia", "7.0.0-1024.25~24.04.1")


def topic(i, created, cat=721):
    return {"id": i, "title": f"t{i}", "created_at": created, "posts_count": 3, "category_id": cat}


FORUM = {"topics": [topic(387182, "2026-10-28T01:00:00.000Z"), topic(387456, "2026-10-30T09:00:00.000Z"),
                    topic(1, "2026-10-01T00:00:00.000Z"), topic(387397, "2026-10-30T02:00:00.000Z", cat=5)]}


def head_ok(url): return 200, LM
def json_ok(url): return FORUM


class Terms(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(ur.forum_term(*KERNEL), "7.0.0-1024")
        self.assertEqual(ur.forum_term("linux-modules-7.0.0-1024-nvidia", "7.0.0-1024.25~24.04.1"), "7.0.0-1024")
        self.assertEqual(ur.forum_term("nvidia-driver-580-open", "580.178.04-0ubuntu1"), "580.178.04")
        self.assertEqual(ur.forum_term("libnvidia-compute-580", "1:580.178.04-0ubuntu1"), "580.178.04")
        self.assertEqual(ur.forum_term("dgx-dashboard", "0.39.3-5"), "dgx-dashboard 0.39.3")
        self.assertEqual(ur.forum_term("nvidia-spark-initcall-bl", "1.0-2"), "nvidia-spark-initcall-bl 1.0")   # Spark 專屬設定包，1024 的官方 workaround 就是靠它發的
        self.assertIsNone(ur.forum_term("brave-browser", "1.98.63"))
        self.assertIsNone(ur.forum_term("openssh-server", "1:9.6p1-3"))
        self.assertIsNone(ur.forum_term("linux-image-nvidia", "garbage"))

    def test_every_server_kernel_name_has_a_term(self):
        for n in ("linux-image-nvidia", "linux-nvidia", "linux-headers-nvidia"):
            self.assertTrue(server.KERNEL_RE.match(n))
            self.assertEqual(ur.forum_term(n, "7.0.0-1024.25"), "7.0.0-1024", n)

    def test_url(self):
        self.assertEqual(ur.forum_url("dgx-dashboard 0.39.3"), "https://forums.developer.nvidia.com/search?q=dgx-dashboard%200.39.3")


class Pure(unittest.TestCase):
    def test_parse_last_modified(self):
        self.assertEqual(ur.parse_last_modified(LM), datetime(2026, 10, 23, 23, 17, 27, tzinfo=timezone.utc))
        self.assertIsNone(ur.parse_last_modified(None))
        self.assertIsNone(ur.parse_last_modified("not a date"))

    def test_filter_topics_cutoff_minus_one_day(self):
        got = ur.filter_topics(FORUM["topics"], PUB)
        self.assertEqual([t["id"] for t in got], [387456, 387397, 387182])      # 新到舊，10/01 那篇被擋
        edge = ur.filter_topics([topic(9, "2026-10-22T23:17:27Z"), topic(8, "2026-10-22T23:17:26Z")], PUB)
        self.assertEqual([t["id"] for t in edge], [9])
        self.assertEqual(len(ur.filter_topics(FORUM["topics"], None)), 4)

    def test_days_since(self):
        now = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(ur.days_since(PUB, now), 7)
        self.assertEqual(ur.days_since("2026-10-30T23:59:00Z", now), 0)
        self.assertEqual(ur.days_since("2026-11-05T00:00:00Z", now), 0)
        self.assertIsNone(ur.days_since(None, now))


class Fetch(unittest.TestCase):
    def go(self, head=head_ok, js=json_ok, prev=None, now=NOW, name=KERNEL):
        return ur.fetch_risk(name[0], name[1], "http://x/pkg.deb", prev, head, js, now)

    def test_ok(self):
        r = self.go()
        self.assertEqual(r["published"], PUB); self.assertIsNone(r["published_note"])
        f = r["forum"]
        self.assertTrue(f["ok"]); self.assertEqual(f["total"], 3); self.assertEqual(f["term"], "7.0.0-1024")
        self.assertEqual(f["topics"][0]["id"], 387456)

    def test_max_five_topics_but_total_counts_all(self):
        many = {"topics": [topic(i, "2026-10-30T00:00:%02dZ" % i) for i in range(8)]}
        f = self.go(js=lambda u: many)["forum"]
        self.assertEqual((f["total"], len(f["topics"])), (8, 5))

    def test_http_401(self):
        r = self.go(head=lambda u: (401, None))
        self.assertIsNone(r["published"]); self.assertEqual(r["published_note"], "HTTP 401")
        self.assertEqual(r["forum"]["total"], 4)       # 上線日未知 → 論壇主題全留

    def test_missing_header_and_no_uri_and_timeout(self):
        self.assertEqual(self.go(head=lambda u: (200, None))["published_note"], "no Last-Modified")
        self.assertEqual(ur.fetch_risk("a", "1", None, None, head_ok, json_ok, NOW)["published_note"], "no uri")
        def boom(u): raise TimeoutError("timed out")
        self.assertEqual(self.go(head=boom)["published_note"], "timeout")

    def test_forum_error(self):
        def bad(u): raise OSError("HTTP Error 429")
        f = self.go(js=bad)["forum"]
        self.assertFalse(f["ok"]); self.assertIn("429", f["note"])
        self.assertEqual(self.go(js=lambda u: ["x"])["forum"]["ok"], False)

    def test_generic_package_has_no_forum_and_no_forum_request(self):
        def no(u): raise AssertionError("forum must not be queried")
        r = ur.fetch_risk("brave-browser", "1.98.63", "http://x", None, head_ok, no, NOW)
        self.assertIsNone(r["forum"]); self.assertEqual(r["published"], PUB)

    def test_published_is_immutable_and_forum_refetch_window(self):
        r1 = self.go()
        def head_fail(u): raise AssertionError("published must not be re-fetched")
        calls = []
        def js(u): calls.append(u); return {"topics": []}
        r2 = self.go(head=head_fail, js=js, prev=r1, now=NOW + 5 * 3600)
        self.assertEqual(calls, []); self.assertEqual(r2["forum"]["total"], 3)        # 6 小時內沿用
        r3 = self.go(head=head_fail, js=js, prev=r1, now=NOW + 6 * 3600 + 1)
        self.assertEqual(len(calls), 1); self.assertEqual(r3["forum"]["total"], 0)
        self.assertEqual(r3["published"], PUB)

    def test_failed_parts_retry_after_one_hour(self):
        bad = self.go(head=lambda u: (401, None), js=lambda u: (_ for _ in ()).throw(OSError("down")))
        self.assertFalse(ur.risk_is_stale(bad, *KERNEL[:1], KERNEL[1], NOW + 3000))
        self.assertTrue(ur.risk_is_stale(bad, KERNEL[0], KERNEL[1], NOW + 3601))
        r = self.go(prev=bad, now=NOW + 3601)
        self.assertEqual(r["published"], PUB); self.assertTrue(r["forum"]["ok"])

    def test_stale_rules(self):
        r = self.go()
        self.assertFalse(ur.risk_is_stale(r, KERNEL[0], KERNEL[1], NOW + 3600))
        self.assertTrue(ur.risk_is_stale(r, KERNEL[0], KERNEL[1], NOW + 6 * 3600 + 1))
        self.assertTrue(ur.risk_is_stale(None, KERNEL[0], KERNEL[1], NOW))


class ServerQueue(unittest.TestCase):
    def test_entry_without_risk_is_requeued_but_changelog_not_refetched(self):
        key = "pkgx 2.0"
        old = {"ok": True, "ts": 1.0, "urgency": "low", "summary": "s", "entries": 1, "cves": 0}
        submitted = []
        class Pool:
            def submit(self, fn, *a): submitted.append(a)
        saved = (server._CLMETA["cache"], server._CLMETA["pool"], set(server._CLMETA["pending"]))
        try:
            server._CLMETA["cache"] = {key: dict(old)}
            server._CLMETA["pool"] = Pool(); server._CLMETA["pending"].clear()
            out = server.updates_meta([{"name": "pkgx", "installed": "1.0", "candidate": "2.0", "uri": "http://u"}])
            self.assertEqual(out["meta"]["pkgx"], old)         # 舊項照回，畫面不空
            self.assertEqual(out["pending"], ["pkgx"])
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0][3], "http://u"); self.assertEqual(submitted[0][4], old)   # 帶著舊 changelog 只補 risk
            out = server.updates_meta([{"name": "pkgx", "installed": "1.0", "candidate": "2.0", "uri": "http://u"}])
            self.assertEqual(len(submitted), 1)                # 已在排隊，不重複丟
        finally:
            server._CLMETA["cache"], server._CLMETA["pool"] = saved[0], saved[1]
            server._CLMETA["pending"].clear(); server._CLMETA["pending"].update(saved[2])

    def test_fresh_risk_is_a_cache_hit(self):
        risk = {"ts": __import__("time").time(), "published": PUB, "published_note": None, "forum": None}
        hit = {"ok": True, "ts": 1.0, "urgency": None, "summary": "", "entries": 0, "cves": 0, "risk": risk}
        saved = (server._CLMETA["cache"], server._CLMETA["pool"])
        try:
            server._CLMETA["cache"] = {"pkgy 2.0": hit}; server._CLMETA["pool"] = None
            out = server.updates_meta([{"name": "pkgy", "installed": "1", "candidate": "2.0"}])
            self.assertEqual(out, {"meta": {"pkgy": hit}, "pending": []})
        finally:
            server._CLMETA["cache"], server._CLMETA["pool"] = saved


if __name__ == "__main__":
    unittest.main()
