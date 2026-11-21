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
            ch = peer.chain(addr[1], port)
            self.assertTrue(ch, "should find the client socket owner")
            self.assertEqual(ch[0]["pid"], os.getpid())
            self.assertEqual(ch[0]["ppid"], os.getppid())
            self.assertIn(str(os.getpid()), peer.describe(ch))
            self.assertEqual(peer.chain(addr[1], port + 1 if port < 65535 else port - 1), [])   # 伺服器埠不對就不認
        finally:
            for s in (conn, cli, srv):
                s.close()

    def test_unknown_port(self):
        self.assertEqual(peer.chain(1, 2), [])

    def test_describe_trims_chrome_args(self):
        d = peer.describe([{"pid": 9, "comm": "chrome", "ppid": 1, "args": ["/opt/chrome", "--type=utility", "--lang=zh", "--user-data-dir=/x/chrome-profile"]}])
        self.assertEqual(d, "9 chrome --type=utility --user-data-dir=/x/chrome-profile")


if __name__ == "__main__":
    unittest.main()
