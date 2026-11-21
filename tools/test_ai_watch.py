"""backend/ai_watch.py 的隔離測試：fixtures 是 2026-11-21 從各來源抓的真實資料剪出來的（tools/fixtures/ai_watch/），不碰網路。
跑法：python3 tools/test_ai_watch.py"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import ai_watch as aw  # noqa: E402

FX = ROOT / "tools" / "fixtures" / "ai_watch"
NOW = 1795228800   # 2026-11-21


class VersionTests(unittest.TestCase):
    def test_order(self):
        seq = ["0.170.0", "0.171.0-alpha.13", "0.171.0-alpha.13.1", "0.171.0", "0.172.0-alpha.1", "0.172.0"]
        self.assertEqual(sorted(reversed(seq), key=aw.vkey), seq)
        self.assertTrue(aw.newer("2.1.330", "2.1.318"))
        self.assertFalse(aw.newer("2.1.318", "2.1.318"))
        self.assertFalse(aw.newer(None, "1.0"))   # 不知道最新版就不說有新版
        self.assertFalse(aw.newer("1.0", None))


class MentionTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(aw.model_mentions("Added GPT-6.2 Sol and Astra to model catalogs"), ["GPT-6.2 Sol and Astra"])
        self.assertEqual(aw.model_mentions("Added Claude Fable 5.2 (`claude-fable-5-2`)"), ["Claude Fable 5.2", "claude-fable-5-2"])
        self.assertEqual(aw.model_mentions("nothing here, version 5.2"), [])

    def test_only_news_counts(self):
        news = "Added Claude Fable 5.2 (`claude-fable-5-2`), now the default Fable model"
        noise = "Fixed Claude sometimes failing to send you a file on its first try on Opus 4.8 and Opus 5"
        aside = "Added auto mode for Claude Opus 4.6 and Sonnet 4.6 on Amazon Bedrock"
        self.assertTrue(aw.is_model_news(news, aw.model_mentions(news)))
        self.assertFalse(aw.is_model_news(noise, aw.model_mentions(noise)))
        self.assertFalse(aw.is_model_news(aside, aw.model_mentions(aside)))


class ChangelogTests(unittest.TestCase):
    def test_claude_vscode_gap_names_fable(self):
        cl = aw.parse_claude_changelog((FX / "claude_changelog.md").read_text())
        self.assertEqual(cl[0][0], "2.1.330")
        n = aw.notes_between(cl, "2.1.318", "2.1.330")
        self.assertEqual(n["versions"], 10)   # fixture 有 2.1.317…2.1.330 共 12 段，比 2.1.318 新的是 10 段
        self.assertEqual([(m["version"], m["mentions"][0]) for m in n["models"]], [("2.1.321", "Claude Fable 5.2")])
        self.assertEqual(aw.notes_between(cl, "2.1.326", "2.1.330")["models"], [])   # CLI 已過 2.1.321
        self.assertEqual(aw.notes_between(cl, None, "2.1.330"), {"versions": 0, "models": []})

    def test_codex_gap_names_gpt62(self):
        rel = aw.parse_codex_atom((FX / "codex_releases.atom").read_text())
        self.assertTrue(all(r[2] == ("-" in r[0]) for r in rel))
        self.assertEqual(aw.latest_stable(rel), "0.171.0")
        n = aw.notes_between(rel, "0.171.0-alpha.13", "0.171.0")
        self.assertEqual([m["mentions"] for m in n["models"]], [["GPT-6.2 Sol and Astra"]])   # PR 清單那行不重複算


class MarketplaceTests(unittest.TestCase):
    def test_stable_install_ignores_preview(self):
        inst = [{"id": "anthropic.claude-code", "version": "2.1.318", "pre": False, "platform": "linux-arm64"},
                {"id": "openai.chatgpt", "version": "26.1117.41037", "pre": False, "platform": "linux-arm64"}]
        latest, missing = aw.marketplace_latest(json.loads((FX / "marketplace.json").read_text()), inst, "linux-arm64")
        self.assertEqual(latest["anthropic.claude-code"]["version"], "2.1.330")
        self.assertNotIn("openai.chatgpt", latest)       # 只回了預覽版 26.51117…，不能當新版
        self.assertEqual(missing, ["openai.chatgpt"])
        inst[1]["pre"] = True                              # 本機裝的是預覽版就認預覽版
        latest, missing = aw.marketplace_latest(json.loads((FX / "marketplace.json").read_text()), inst, "linux-arm64")
        self.assertEqual(latest["openai.chatgpt"]["version"], "26.51117.41037")

    def test_installed_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "extensions.json")
            with open(p, "w") as f:
                json.dump([{"identifier": {"id": "B.x"}, "version": "1", "metadata": {"targetPlatform": "undefined"}},
                           {"identifier": {"id": "a.y"}, "version": "2", "metadata": {"preRelease": True}}, {"bad": 1}], f)
            self.assertEqual(aw.vscode_installed(p), [{"id": "a.y", "version": "2", "pre": True, "platform": None},
                                                      {"id": "b.x", "version": "1", "pre": False, "platform": None}])
            self.assertIsNone(aw.vscode_installed(os.path.join(d, "missing.json")))


class ModelsTests(unittest.TestCase):
    def test_recent(self):
        data = json.loads((FX / "openrouter.json").read_text())
        data["data"].append({"id": "~openai/gpt-latest", "name": "alias", "created": NOW})
        ms = aw.recent_models(data, NOW)
        ids = [m["id"] for m in ms]
        self.assertEqual(ids.count("anthropic/claude-fable-5.2"), 1)   # :batch 併掉
        self.assertNotIn("~openai/gpt-latest", ids); self.assertNotIn("openrouter/auto", ids)
        self.assertEqual(ms, sorted(ms, key=lambda m: -m["created"]))
        fable = next(m for m in ms if m["id"] == "anthropic/claude-fable-5.2")
        self.assertEqual((fable["price_in"], fable["price_out"], fable["context"], fable["major"]), (8.0, 40.0, 1000000, True))
        kimi = next(m for m in ms if m["provider"] == "moonshotai")
        self.assertTrue(kimi["hf"])                                    # 開放權重
        self.assertFalse(next(m for m in ms if m["provider"] == "radixark")["major"])
        self.assertEqual(aw.recent_models(data, NOW + 400 * 86400), [])   # 超過 90 天就不列


if __name__ == "__main__":
    unittest.main()
