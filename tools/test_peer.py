"""backend/peer.py：開一條真的本機連線，確認反查得到自己這個程序。跑法：python3 tools/test_peer.py"""
import os
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import peer  # noqa: E402


class PeerTests(unittest.TestCase):
    def test_finds_own_process(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        cli = socket.create_connection(("127.0.0.1", port))
        conn, addr = srv.accept()
        try:
            ch = peer.chain(addr[:2], ("127.0.0.1", port))
            self.assertTrue(ch, "should find the client socket owner")
            self.assertEqual(ch[0]["pid"], os.getpid())
            self.assertEqual(ch[0]["ppid"], os.getppid())
            self.assertIn(str(os.getpid()), peer.describe(ch))
            self.assertEqual(peer.chain(addr[:2], ("127.0.0.1", port + 1 if port < 65535 else port - 1)), [])   # 伺服器埠不對就不認
        finally:
            for s in (conn, cli, srv):
                s.close()

    def test_unknown_port(self):
        self.assertEqual(peer.chain(("127.0.0.1", 1), ("127.0.0.1", 2)), [])

    def test_describe_trims_chrome_args(self):
        d = peer.describe([{"pid": 9, "comm": "chrome", "ppid": 1, "args": ["/opt/chrome", "--type=utility", "--lang=zh", "--user-data-dir=/x/chrome-profile"]}])
        self.assertEqual(d, "9 chrome --type=utility --user-data-dir=/x/chrome-profile")


class LeakTests(unittest.TestCase):
    """2026-11-21 資安互審：describe 曾把整條指令列寫進 journal，curl -H 的 token、-d 的密碼都會外洩；程序名或 UA 帶換行可以偽造一行假紀錄。"""

    def test_no_secrets_from_argv(self):
        d = peer.describe([
            {"pid": 1, "comm": "curl", "ppid": 0, "args": ["curl", "-H", "Authorization: Bearer sk-SECRET", "-d", '{"password":"hunter2"}', "http://x"]},
            {"pid": 2, "comm": "python3", "ppid": 0, "args": ["python3", "-c", "token='abc'"]},
            {"pid": 3, "comm": "python3", "ppid": 0, "args": ["/usr/bin/python3", "/home/u/agent.py", "--key", "SECRET2"]},
            {"pid": 4, "comm": "chrome", "ppid": 0, "args": ["/opt/chrome", "--type=utility", "--token=SECRET3", "--user-data-dir=/p"]},
            {"pid": 6, "comm": "bash", "ppid": 0, "args": ["bash", "-lc", "token='SECRET4'; echo ok"]},      # Codex 第二輪重現
            {"pid": 7, "comm": "python3", "ppid": 0, "args": ["python3", "-W", "SECRET5", "script.py"]},     # Codex 第二輪重現
            {"pid": 8, "comm": "curl", "ppid": 0, "args": ["curl", "--user-data-dir=SECRET6"]}])             # Chrome 旗標只對 Chrome 收
        for s in ("SECRET", "hunter2", "abc", "Bearer"):
            self.assertNotIn(s, d)
        self.assertIn("agent.py", d); self.assertIn("<args>", d); self.assertIn("--user-data-dir=/p", d)

    def test_control_chars_escaped(self):
        self.assertEqual(peer.clean("ua\r\n forged"), "ua\\x0d\\x0a forged")
        self.assertNotIn("\n", peer.describe([{"pid": 5, "comm": "fake\nFORGED", "ppid": 0, "args": []}]))
        self.assertEqual(peer.clean("a\x85b\u2028c"), "a\\x85b\\u2028c")   # C1 與行分隔（Codex 終審）


if __name__ == "__main__":
    unittest.main()
