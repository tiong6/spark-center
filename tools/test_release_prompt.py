"""Ubuntu 版本升級提示的隔離測試：backend/release_prompt.py 與 server.release_prompt_set。跑法：python3 tools/test_release_prompt.py
不碰 /etc：設定檔、清單快取都指到暫存目錄，pkexec 換成複製檔案的假函式。
起因（2026-10-28）：常見問題把「旗標檔存在」說成偵測到新版（它每週都被改寫），Prompt 只認行首的 Prompt=，
空白、no、段落、註解都會讀錯。交叉比對那組直接跑本機裝的 UpdateManager.Core.MetaRelease，確認判斷和 Ubuntu 一樣（不下載任何東西）。"""
import contextlib
import datetime
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
from backend import release_prompt as rp  # noqa: E402

# ubuntu-release-upgrader-core 1:24.04.29 出貨的 /etc/update-manager/release-upgrades（dpkg 記錄的 md5 在 test_shipped_fixture）
SHIPPED = """# Default behavior for the release upgrader.

[DEFAULT]
# Default prompting and upgrade behavior, valid options:
#
#  never  - Never check for, or allow upgrading to, a new release.
#  normal - Check to see if a new release is available.  If more than one new
#           release is found, the release upgrader will attempt to upgrade to
#           the supported release that immediately succeeds the
#           currently-running release.
#  lts    - Check to see if a new LTS release is available.  The upgrader
#           will attempt to upgrade to the first LTS release available after
#           the currently-running one.  Note that if this option is used and
#           the currently-running release is not itself an LTS release the
#           upgrader will assume prompt was meant to be normal.
Prompt=lts
"""
SHIPPED_MD5 = "54fd85e86c6bc2db8d7cc3c18013e3aa"

# 名稱 → (內容, 生效值, raw, default_used)；內容 None＝檔案不存在
FIXTURES = {
    "never": ("[DEFAULT]\nPrompt=never\n", "never", "never", False),
    "NEVER": ("[DEFAULT]\nPrompt=NEVER\n", "never", "NEVER", False),
    "no": ("[DEFAULT]\nPrompt=no\n", "never", "no", False),
    "value_spaces": ("[DEFAULT]\nPrompt=  never  \n", "never", "never", False),
    "lowercase_key": ("[DEFAULT]\nprompt=never\n", "never", "never", False),
    "spaces_around_eq": ("[DEFAULT]\nPrompt = never\n", "never", "never", False),
    "colon": ("[DEFAULT]\nPrompt: never\n", "never", "never", False),
    "crlf": ("[DEFAULT]\r\nPrompt=never\r\n", "never", "never", False),
    "inline_comment": ("[DEFAULT]\nPrompt=never # c\n", "normal", "never # c", False),
    "empty": ("[DEFAULT]\nPrompt=\n", "normal", "", False),
    "false": ("[DEFAULT]\nPrompt=false\n", "normal", "false", False),
    "normal": ("[DEFAULT]\nPrompt=normal\n", "normal", "normal", False),
    "lts": ("[DEFAULT]\nPrompt=lts\n", "lts", "lts", False),
    "shipped": (SHIPPED, "lts", "lts", False),
    "continuation": ("[DEFAULT]\nPrompt=never\n  more\n", "normal", "never\nmore", False),
    "escaped_percent": ("[DEFAULT]\nPrompt=never%%\n", "normal", "never%", False),
    "no_prompt_line": ("[DEFAULT]\n# nothing\n", "normal", None, True),
    "only_other_section": ("[foo]\nPrompt=never\n", "normal", None, True),
    "no_section_header": ("Prompt=never\n", "malformed", None, False),
    "duplicate_prompt": ("[DEFAULT]\nPrompt=lts\nPrompt=never\n", "malformed", None, False),
    "percent": ("[DEFAULT]\nPrompt=never%\n", "malformed", "never%", False),
    "missing": (None, "normal", None, True),
}


# compose() 的逐字輸出：(內容, 改成 never 之後)
COMPOSE_CASES = [
    (None, "[DEFAULT]\nPrompt=never\n"),
    ("[DEFAULT]\n# nothing\n", "[DEFAULT]\nPrompt=never\n# nothing\n"),                     # 插在標題下一行
    ("[DEFAULT]", "[DEFAULT]\nPrompt=never\n"),                                              # 標題沒有換行
    ("[foo]\nPrompt=lts\n", "[foo]\nPrompt=lts\n\n[DEFAULT]\nPrompt=never\n"),               # 別的段落不動，最後補一段
    ("[foo]\na=1", "[foo]\na=1\n[DEFAULT]\nPrompt=never\n"),                                 # 最後一行沒換行：補上
    ("", "[DEFAULT]\nPrompt=never\n"),
    ("Prompt=lts\n", "[DEFAULT]\nPrompt=never\n"),                                           # 沒有段落標題：補 [DEFAULT]
    ("# c\nPrompt=lts\nFoo=1\n", "[DEFAULT]\n# c\nPrompt=never\nFoo=1\n"),
    ("[DEFAULT]\nPrompt=lts\nPrompt=never\n", "[DEFAULT]\nPrompt=never\n"),                  # 重複的刪掉
    ("[DEFAULT]\nPrompt=lts\n  more\n# c\n   and more\nFoo=1\n", "[DEFAULT]\nPrompt=never\n# c\nFoo=1\n"),   # 續行刪掉，註解留著
    ("[DEFAULT]\nPrompt=lts\nprompt = normal\n  cont\nX=1\n", "[DEFAULT]\nPrompt=never\nX=1\n"),
    ("[DEFAULT]\n# Prompt=lts\n; Prompt=lts\nPrompt=lts\n", "[DEFAULT]\n# Prompt=lts\n; Prompt=lts\nPrompt=never\n"),   # 註解不算
    ("[DEFAULT]\nPrompt=lts\n[foo]\nPrompt=lts\n", "[DEFAULT]\nPrompt=never\n[foo]\nPrompt=lts\n"),
    ("[DEFAULT]\n  Prompt=lts\n Foo=1\n", "[DEFAULT]\n  Prompt=never\n Foo=1\n"),        # 縮排照留，Foo 才不會變成續行
    ("[DEFAULT]\nPrompt=never%\n", "[DEFAULT]\nPrompt=never\n"),
    ("[DEFAULT]\nPrompt: lts\n", "[DEFAULT]\nPrompt=never\n"),
    ("[DEFAULT]\nPrompt=lts\n  # keep\nFoo=1\n", "[DEFAULT]\nPrompt=never\n  # keep\nFoo=1\n"),   # 縮排的註解也是註解，不是續行：留著
    ("[DEFAULT]\nPrompt=lts\n\n  more\n", "[DEFAULT]\nPrompt=never\n\n"),                # 空行不會結束續行（configparser 預設）：more 仍屬 Prompt
    ("[DEFAULT]\nPrompt=lts\n  [foo]\n", "[DEFAULT]\nPrompt=never\n"),                     # 縮排的段落標題是續行，不是新段落
]

class EffectiveTests(unittest.TestCase):
    def test_fixtures(self):
        for name, (text, mode, raw, default_used) in FIXTURES.items():
            with self.subTest(name):
                got = rp.effective(text)
                self.assertEqual(got[:3], (mode, raw, default_used))
                self.assertEqual(got[3] is not None, mode == "malformed", got)

    def test_shipped_fixture_is_the_dpkg_conffile(self):
        self.assertEqual(hashlib.md5(SHIPPED.encode()).hexdigest(), SHIPPED_MD5)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.conf = os.path.join(self.d, "release-upgrades")
        patches = [mock.patch.object(rp, "CONF", self.conf), mock.patch.object(rp, "CACHE_DIR", os.path.join(self.d, "var")),
                   mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.d, "cache")})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_never_has_no_popup_and_no_offer(self):
        Path(self.conf).write_text("[DEFAULT]\nPrompt=never\n")
        st = rp.status()
        self.assertEqual((st["mode"], st["popup"], st["offered"], st["exists"], st["readable"]), ("never", False, None, True, True))
        self.assertRegex(st["mtime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_missing_file_is_normal_default(self):
        st = rp.status()
        self.assertEqual((st["exists"], st["mode"], st["default_used"], st["popup"], st["mtime"]), (False, "normal", True, True, None))

    def test_unreadable_file_reads_like_missing_but_says_why(self):
        os.mkdir(self.conf)   # 存在但 open() 失敗：和權限不足一樣，configparser.read() 會安靜跳過
        st = rp.status()
        self.assertEqual((st["exists"], st["readable"], st["mode"], st["default_used"], st["popup"]), (True, False, "normal", True, True))
        self.assertTrue(st["error"])

    def test_not_utf8_is_malformed(self):
        Path(self.conf).write_bytes(b"[DEFAULT]\nPrompt=\xff\n")
        st = rp.status()
        self.assertEqual((st["readable"], st["mode"], st["popup"]), (True, "malformed", False))
        self.assertTrue(st["error"])

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_permission_denied(self):
        Path(self.conf).write_text("[DEFAULT]\nPrompt=never\n")
        os.chmod(self.conf, 0)
        self.addCleanup(os.chmod, self.conf, 0o644)
        st = rp.status()
        self.assertEqual((st["readable"], st["mode"], st["default_used"]), (False, "normal", True))


try:
    import apt
    from UpdateManager.Core.MetaRelease import MetaReleaseCore
    _UBUNTU = None
except Exception as e:   # 不是 Ubuntu（或沒裝 update-manager-core）：交叉比對跳過
    MetaReleaseCore, _UBUNTU = None, str(e)


@unittest.skipIf(MetaReleaseCore is None, f"UpdateManager.Core.MetaRelease not importable: {_UBUNTU}")
class UbuntuCrossCheckTests(unittest.TestCase):
    """同一份內容丟給本機裝的 MetaReleaseCore；_buildMetaReleaseFile 換成只記錄網址、回 False，所以不會下載。"""

    @classmethod
    def setUpClass(cls):
        cls.cache = apt.Cache()

    def ubuntu(self, text, d):
        path = os.path.join(d, "release-upgrades")
        if text is not None:
            Path(path).write_text(text, newline="")
        urls = []
        with mock.patch.object(MetaReleaseCore, "CONF", path if text is not None else path + ".missing"), \
                mock.patch.object(MetaReleaseCore, "_buildMetaReleaseFile", lambda self: urls.append(self.METARELEASE_URI) and False), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                m = MetaReleaseCore(cache=self.cache)
            except Exception:
                return "malformed", None   # 例如值裡的 %：檢查程式整個當掉
        if m.prompt == "never" and not urls:
            return "never", None
        if urls and m.prompt == "lts":
            return "lts", urls[0]
        if urls and m.prompt in ("normal", None):
            return "normal", urls[0]
        if not urls and m.prompt is None and not m.downloaded.is_set():
            return "malformed", None   # configparser 讀不了：提早 return，downloaded 永遠不會 set
        return f"unexpected prompt={m.prompt} urls={urls}", None

    def test_same_mode_as_ubuntu(self):
        for name, (text, _mode, _raw, _default) in FIXTURES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                ubuntu_mode, _url = self.ubuntu(text, d)
                self.assertEqual(rp.effective(text)[0], ubuntu_mode)

    def test_offered_reads_the_feed_ubuntu_would_download(self):
        """Ubuntu 決定下載哪個網址；offered() 要讀的正是那個網址檔名的快取。"""
        for name in ("lts", "normal", "missing", "inline_comment"):
            text = FIXTURES[name][0]
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                mode, url = self.ubuntu(text, d)
                feed = Path(d, "cache", "update-manager-core", os.path.basename(url))
                feed.parent.mkdir(parents=True)
                feed.write_text(FeedTests.feed(("noble", "24.04.5 LTS", "2024-04-25", 1), ("resolute", "26.04.1 LTS", "2026-04-23", 1)))
                with mock.patch.object(rp, "CACHE_DIR", os.path.join(d, "var")), \
                        mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(d, "cache"), "META_RELEASE_FAKE_CODENAME": "noble"}):
                    got = rp.offered(mode)
                self.assertIsNotNone(got)
                self.assertEqual(got["source"], str(feed))


class ComposeTests(unittest.TestCase):
    def test_shipped_lts_to_never_changes_exactly_one_line(self):
        out = rp.compose(SHIPPED, "never")
        a, b = SHIPPED.splitlines(keepends=True), out.splitlines(keepends=True)
        self.assertEqual(len(a), len(b))
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        self.assertEqual(diff, [("Prompt=lts\n", "Prompt=never\n")])

    def test_every_fixture_composes_to_target_and_is_idempotent(self):
        for name, (text, _m, _r, _d) in FIXTURES.items():
            for mode in ("never", "lts"):
                with self.subTest(f"{name}->{mode}"):
                    out = rp.compose(text, mode)
                    self.assertEqual(rp.effective(out)[0], mode)
                    self.assertEqual(rp.compose(out, mode), out)

    def test_exact_outputs(self):
        for text, want in COMPOSE_CASES:
            with self.subTest(text):
                self.assertEqual(rp.compose(text, "never"), want)

    def test_crlf_and_cr_line_endings_preserved(self):
        self.assertEqual(rp.compose("[DEFAULT]\r\nFoo=1\r\nPrompt=lts\r\n", "never"), "[DEFAULT]\r\nFoo=1\r\nPrompt=never\r\n")
        self.assertEqual(rp.compose("[DEFAULT]\r\n# x\r\n", "never"), "[DEFAULT]\r\nPrompt=never\r\n# x\r\n")
        self.assertEqual(rp.compose("Prompt=lts\r\n", "never"), "[DEFAULT]\r\nPrompt=never\r\n")
        self.assertEqual(rp.compose("[DEFAULT]\rPrompt=lts\r", "never"), "[DEFAULT]\rPrompt=never\r")
        out = rp.compose(SHIPPED.replace("\n", "\r\n"), "never")
        self.assertEqual(out, SHIPPED.replace("Prompt=lts", "Prompt=never").replace("\n", "\r\n"))

    def test_refuses_instead_of_writing_something_wrong(self):
        for text in ("[foo]\na=1\n[foo]\nb=2\n[DEFAULT]\nPrompt=lts\n",   # 重複段落：改 Prompt 救不了
                     "[DEFAULT]\nPrompt=lts\nbogus line\n",              # 看不懂的行
                     "[DEFAULT]\n  Foo=1\n"):                             # 插進去會把 Foo 吞成續行
            with self.subTest(text), self.assertRaises(ValueError):
                rp.compose(text, "never")
        for mode in ("normal", "no", None):
            with self.subTest(mode), self.assertRaises(ValueError):
                rp.compose("[DEFAULT]\nPrompt=lts\n", mode)


class FeedTests(unittest.TestCase):
    @staticmethod
    def date(iso):   # 和 Canonical 的清單同格式；不用 strftime 的 %a／%B，免得跟著 LC_TIME 變成中文
        d = datetime.date.fromisoformat(iso)
        months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
        return f"{('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[d.weekday()]}, {d.day:02d} {months[d.month - 1]} {d.year} 00:00:00 UTC"

    @staticmethod
    def feed(*dists, drop=None):
        out = []
        for name, version, date, supported in dists:
            st = {"Dist": name, "Name": name.title(), "Version": version, "Date": FeedTests.date(date), "Supported": str(supported)}
            if drop and drop[0] == name:
                del st[drop[1]]
            out.append("".join(f"{k}: {v}\n" for k, v in st.items()) + f"Description: This is the {version} release\n")
        return "\n".join(out)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.var = os.path.join(self.d, "var")
        self.user = os.path.join(self.d, "cache", "update-manager-core")
        os.makedirs(self.var)
        os.makedirs(self.user)
        patches = [mock.patch.object(rp, "CACHE_DIR", self.var), mock.patch.object(rp, "META_CONF", os.path.join(self.d, "meta-release")),
                   mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.d, "cache"), "META_RELEASE_FAKE_CODENAME": "noble"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def put(self, text, where=None, name="meta-release-lts", mtime=None):
        path = os.path.join(where or self.user, name)
        Path(path).write_text(text)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    NOBLE = ("noble", "24.04.5 LTS", "2024-04-25", 1)

    def test_newer_supported_release_is_offered(self):
        path = self.put(self.feed(("jammy", "22.04.5 LTS", "2022-04-21", 1), self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)))
        got = rp.offered("lts")
        self.assertEqual((got["version"], got["codename"], got["source"]), ("26.04.1 LTS", "resolute", path))
        self.assertEqual(got["as_of"], rp._iso(os.stat(path).st_mtime))

    def test_unsupported_newer_release_is_skipped(self):
        self.put(self.feed(self.NOBLE, ("oracular", "24.10", "2024-10-10", 0), ("questing", "25.10", "2025-10-09", 1)), name="meta-release")
        self.assertEqual(rp.offered("normal")["version"], "25.10")

    def test_first_newer_in_file_order_wins(self):
        """不是最新、也不是最近的那個：照檔案順序第一個比目前新的（兩種排列各擋一種錯誤寫法）。"""
        questing, resolute = ("questing", "25.10", "2025-10-09", 1), ("resolute", "26.04.1 LTS", "2026-04-23", 1)
        self.put(self.feed(self.NOBLE, questing, resolute), name="meta-release")
        self.assertEqual(rp.offered("normal")["codename"], "questing")
        self.put(self.feed(self.NOBLE, resolute, questing), name="meta-release")
        self.assertEqual(rp.offered("normal")["codename"], "resolute")

    def test_missing_key_in_any_stanza_voids_the_list(self):
        self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1), drop=("noble", "Supported")))
        self.assertIsNone(rp.offered("lts"))

    def test_running_release_not_in_list(self):
        self.put(self.feed(("jammy", "22.04.5 LTS", "2022-04-21", 1), ("resolute", "26.04.1 LTS", "2026-04-23", 1)))
        self.assertIsNone(rp.offered("lts"))

    def test_nothing_newer(self):
        self.put(self.feed(("jammy", "22.04.5 LTS", "2022-04-21", 1), self.NOBLE))
        self.assertIsNone(rp.offered("lts"))

    def test_no_cache_file_or_empty_file(self):
        self.assertIsNone(rp.offered("lts"))
        self.put("")
        self.assertIsNone(rp.offered("lts"))

    def test_bad_date_or_supported_voids_the_list(self):
        self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)).replace("Supported: 1\nDescription: This is the 26", "Supported: yes\nDescription: This is the 26"))
        self.assertIsNone(rp.offered("lts"))
        self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)).replace("Thu, 23 April 2026", "someday"))
        self.assertIsNone(rp.offered("lts"))

    def test_never_and_malformed_offer_nothing(self):
        self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)))
        self.assertIsNone(rp.offered("never"))
        self.assertIsNone(rp.offered("malformed"))

    def test_newest_cache_wins(self):
        self.put(self.feed(self.NOBLE, ("resolute", "26.04 LTS", "2026-04-23", 1)), where=self.var, mtime=1_700_000_000)
        user = self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)), mtime=1_800_000_000)
        self.assertEqual(rp.offered("lts")["source"], user)
        os.utime(user, (1_600_000_000, 1_600_000_000))
        self.assertEqual(rp.offered("lts")["version"], "26.04 LTS")

    def test_lts_feed_only_on_an_lts_release(self):
        """Prompt=lts 在非 LTS 版本上照 Ubuntu 退回一般清單。"""
        self.put(self.feed(("questing", "25.10", "2025-10-09", 1), ("resolute", "26.04.1 LTS", "2026-04-23", 1)), name="meta-release")
        with mock.patch.dict(os.environ, {"META_RELEASE_FAKE_CODENAME": "questing"}):
            self.assertEqual(rp.offered("lts")["source"], os.path.join(self.user, "meta-release"))

    def test_meta_release_conf_can_rename_the_feed(self):
        Path(rp.META_CONF).write_text("[METARELEASE]\nURI_LTS = https://example.invalid/custom-lts\n")
        path = self.put(self.feed(self.NOBLE, ("resolute", "26.04.1 LTS", "2026-04-23", 1)), name="custom-lts")
        self.assertEqual(rp.offered("lts")["source"], path)

    def test_untrusted_version_is_capped(self):
        self.put(self.feed(self.NOBLE, ("resolute", "<b>" * 100, "2026-04-23", 1)))
        self.assertLessEqual(len(rp.offered("lts")["version"]), rp.VERSION_MAX)


import server  # noqa: E402  與 test_gpu_hot.py 相同的載入方式
from backend.messages import msg  # noqa: E402


class SetTests(unittest.TestCase):
    """server.release_prompt_set：pkexec 換成「把暫存檔複製到 CONF」的假函式，看它被怎麼呼叫、結果怎麼回報。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.conf = os.path.join(self.d, "etc", "release-upgrades")
        os.makedirs(os.path.dirname(self.conf))
        self.calls, self.mode = [], "copy"
        patches = [mock.patch.object(rp, "CONF", self.conf), mock.patch.object(rp, "CACHE_DIR", os.path.join(self.d, "var")),
                   mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.d, "cache")}),
                   mock.patch.object(server, "_pkexec", self.fake_pkexec), mock.patch.object(server, "READONLY", False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_pkexec(self, args, timeout=300):
        src = args[7]
        self.calls.append({"args": list(args), "mode": os.stat(src).st_mode & 0o777, "text": Path(src).read_text(), "dir": os.path.dirname(src)})
        if self.mode == "cancel":
            return False, msg("auth_cancelled", server.LANG_DEFAULT)
        if self.mode == "copy":
            shutil.copyfile(src, args[8])
        return True, None   # "noop"：說成功但其實沒寫

    def test_success_writes_through_install_and_reads_back(self):
        Path(self.conf).write_text(SHIPPED)
        r = server.release_prompt_set("never", "en")
        self.assertEqual((r["ok"], r["error"], r["status"]["mode"]), (True, None, "never"))
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual(c["args"][:7] + c["args"][8:], ["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0644", self.conf])
        self.assertEqual(len(c["args"]), 9)
        self.assertEqual(c["mode"], 0o644)
        self.assertFalse(os.path.exists(c["dir"]), "temp dir removed")
        self.assertEqual(Path(self.conf).read_text(), SHIPPED.replace("Prompt=lts\n", "Prompt=never\n"))

    def test_already_set_does_not_ask_for_a_password(self):
        Path(self.conf).write_text("[DEFAULT]\nprompt = NEVER\n")
        r = server.release_prompt_set("never")
        self.assertEqual((r["ok"], r["status"]["mode"], self.calls), (True, "never", []))

    def test_readback_mismatch_is_a_failure(self):
        Path(self.conf).write_text(SHIPPED)
        self.mode = "noop"
        r = server.release_prompt_set("never", "en")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], msg("release_readback", "en", got="lts", want="never"))
        self.assertEqual(r["status"]["mode"], "lts")

    def test_cancelled_password_keeps_file(self):
        Path(self.conf).write_text(SHIPPED)
        self.mode = "cancel"
        r = server.release_prompt_set("never")
        self.assertEqual((r["ok"], r["error"], r["status"]["mode"]), (False, msg("auth_cancelled", server.LANG_DEFAULT), "lts"))
        self.assertEqual(Path(self.conf).read_text(), SHIPPED)

    def test_readonly_and_bad_mode_never_call_pkexec(self):
        Path(self.conf).write_text(SHIPPED)
        with mock.patch.object(server, "READONLY", True):
            r = server.release_prompt_set("never", "en")
        self.assertEqual((r["ok"], r["error"]), (False, msg("readonly_mode", "en")))
        for bad in ("normal", "", None, "NEVER"):
            r = server.release_prompt_set(bad, "en")
            self.assertEqual((r["ok"], r["error"]), (False, msg("release_bad_mode", "en")))
        self.assertEqual(self.calls, [])

    def test_unparseable_or_unreadable_file_is_not_touched(self):
        Path(self.conf).write_text("[foo]\na=1\n[foo]\nb=2\n")
        r = server.release_prompt_set("never", "en")
        self.assertEqual((r["ok"], r["error"]), (False, msg("release_cannot_compose", "en", file=self.conf, mode="never")))
        os.remove(self.conf)
        os.mkdir(self.conf)
        r = server.release_prompt_set("never", "en")
        self.assertEqual((r["ok"], r["error"]), (False, msg("release_unreadable", "en", file=self.conf)))
        self.assertEqual(self.calls, [])

    def test_light_get_endpoint_returns_status_without_apt(self):
        """前端回到視窗與每 5 分鐘打這支重查紅標：要回 status()，而且不能跑 apt（/api/updates 那份很重）。"""
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        Path(self.conf).write_text(SHIPPED)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        with mock.patch.object(server, "list_updates", side_effect=AssertionError("apt touched")):
            with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/setup/release-prompt", timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                j = json.loads(resp.read())
        self.assertTrue(j["ok"])
        self.assertEqual((j["status"]["file"], j["status"]["mode"], j["status"]["popup"]), (self.conf, "lts", True))
        self.assertEqual(self.calls, [])

    def test_missing_file_is_created(self):
        r = server.release_prompt_set("never")
        self.assertEqual((r["ok"], r["status"]["mode"]), (True, "never"))
        self.assertEqual(Path(self.conf).read_text(), "[DEFAULT]\nPrompt=never\n")

    def test_lts_round_trip(self):
        Path(self.conf).write_text(SHIPPED.replace("Prompt=lts\n", "Prompt=never\n"))
        self.assertTrue(server.release_prompt_set("lts")["ok"])
        self.assertEqual(Path(self.conf).read_text(), SHIPPED)


if __name__ == "__main__":
    unittest.main()
