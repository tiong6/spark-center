"""2026-11-21 資安互審（Claude×Codex）修掉的路徑，每條都用真的東西跑：本機滴流 HTTP 伺服器、失敗的刷新、結束與模型切換的時序、
跨帳號連線的 UID 判斷。不碰外網、不碰 apt、不真的退出。跑法：python3 tools/test_ai_security.py"""
import http.client
import http.server
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from backend import peer  # noqa: E402


class _Drip(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "100000"); self.end_headers()
        try:
            for _ in range(200):          # 每 0.05 秒一個位元組：每次 read 都在 socket timeout 之內
                self.wfile.write(b"x"); self.wfile.flush(); time.sleep(0.05)
        except OSError:
            pass

    def log_message(self, *a):
        pass


class _ChunkDrip(http.server.BaseHTTPRequestHandler):
    """Codex 第三輪：chunked 回應的 chunk 長度行逐字滴（chunk extension），read1 一次呼叫內就讀不完 framing。"""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        try:
            self.wfile.write(b"1;"); self.wfile.flush()
            for _ in range(200):
                self.wfile.write(b"a"); self.wfile.flush(); time.sleep(0.025)
        except OSError:
            pass

    def log_message(self, *a):
        pass


class _HeaderDrip(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n"); self.wfile.flush()
            for _ in range(200):
                self.wfile.write(b"X-A: b\r\n"); self.wfile.flush(); time.sleep(0.025)
        except OSError:
            pass

    def log_message(self, *a):
        pass


class _CloseChunkDrip(_ChunkDrip):
    """Codex 終審：Connection: close 時 getresponse 把 conn.sock 設成 None，計時器原本找不到 socket 可關。"""

    def do_GET(self):
        self.send_response(200); self.send_header("Transfer-Encoding", "chunked"); self.send_header("Connection", "close"); self.end_headers()
        try:
            self.wfile.write(b"1;"); self.wfile.flush()
            for _ in range(200):
                self.wfile.write(b"a"); self.wfile.flush(); time.sleep(0.025)
        except OSError:
            pass


class _ShortBody(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "100"); self.end_headers()
        self.wfile.write(b"abc"); self.wfile.flush()

    def log_message(self, *a):
        pass


class FetchTests(unittest.TestCase):
    def _deadline(self, handler):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(server, "AI_FETCH_DEADLINE", 0.4), mock.patch.object(server, "AI_FETCH_ALLOW_HTTP", True):
                t = time.monotonic()
                with self.assertRaises(TimeoutError):
                    server._http_get(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5)
                return time.monotonic() - t
        finally:
            srv.shutdown(); srv.server_close()

    def test_chunk_framing_drip_hits_deadline(self):
        self.assertLess(self._deadline(_ChunkDrip), 1.0)

    def test_header_drip_hits_deadline(self):
        self.assertLess(self._deadline(_HeaderDrip), 1.0)

    def test_connection_close_chunk_drip_hits_deadline(self):
        self.assertLess(self._deadline(_CloseChunkDrip), 1.0)

    def test_truncated_body_is_an_error(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ShortBody)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(server, "AI_FETCH_ALLOW_HTTP", True), self.assertRaises(http.client.IncompleteRead):
                server._http_get(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5)
        finally:
            srv.shutdown(); srv.server_close()

    def test_drip_hits_total_deadline(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(server, "AI_FETCH_DEADLINE", 0.4), mock.patch.object(server, "AI_FETCH_ALLOW_HTTP", True):
                t = time.monotonic()
                with self.assertRaises(TimeoutError):
                    server._http_get(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5)
                self.assertLess(time.monotonic() - t, 1.5)
        finally:
            srv.shutdown(); srv.server_close()


class _Redirect(http.server.BaseHTTPRequestHandler):
    target = "http://127.0.0.1:1/api/quit"

    def _go(self):
        self.send_response(307); self.send_header("Location", self.target); self.send_header("Content-Length", "0"); self.end_headers()
    do_GET = do_POST = _go

    def log_message(self, *a):
        pass


class RedirectTests(unittest.TestCase):
    """自己找到的（Codex 第四輪被 OpenAI 的內容過濾擋下、沒有結論）：被污染的來源用 307 把 POST 轉到本服務自己 → 繞過 UID 檢查。"""

    def _run(self, body):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(server, "AI_FETCH_ALLOW_HTTP", True):
                server._http_get(f"http://127.0.0.1:{srv.server_address[1]}/", body=body, timeout=5)
        finally:
            srv.shutdown(); srv.server_close()

    def test_post_redirect_refused(self):
        with self.assertRaisesRegex(ValueError, "redirect for a POST"):
            self._run({"x": 1})

    def test_plain_http_refused_in_production(self):
        with self.assertRaisesRegex(ValueError, "refusing http"):
            server._http_get("http://127.0.0.1:11001/api/job")


class ThrottleTests(unittest.TestCase):
    def test_failed_refresh_is_not_retried_immediately(self):
        calls = []

        def boom():
            calls.append(1); raise RuntimeError("polluted")
        with mock.patch.object(server, "ai_board", boom), mock.patch.dict(server._AIB, {"data": None, "ts": 0, "attempt": 0, "refreshing": False}):
            for _ in range(5):
                server.ai_board_status(force=True); time.sleep(0.05)
        self.assertEqual(len(calls), 1)


class QuitSwitchTests(unittest.TestCase):
    def test_switch_cannot_start_between_check_and_exit(self):
        """真的 _switch_start 在 start 途中（持鎖）時按結束：結束要等它放鎖，然後看到切換在跑而拒絕。"""
        state = {"status": "idle"}; results = {}

        def slow_start(*a, **k):
            time.sleep(0.3); state["status"] = "running"
        with mock.patch.object(server.llm_switch.SWITCH, "snapshot", lambda: dict(state)), \
                mock.patch.object(server.llm_switch.SWITCH, "start", slow_start):
            server.SHUTTING_DOWN.clear()
            th = threading.Thread(target=lambda: results.__setitem__("switch", server._switch_start("strata", None, "5m") or "started"))
            th.start(); time.sleep(0.05)
            results["quit"] = server._quit_busy()
            th.join()
            self.assertEqual(results, {"switch": "started", "quit": "llm_switch"})
            self.assertFalse(server.SHUTTING_DOWN.is_set())
            state["status"] = "idle"                                  # 反過來：先按結束，之後的切換要被拒
            self.assertIsNone(server._quit_busy())
            with self.assertRaises(server.llm_switch.SwitchError):
                server._switch_start("strata", None, "5m")
        server.SHUTTING_DOWN.clear()


class UidTests(unittest.TestCase):
    def test_own_connection_uid(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        cli = socket.create_connection(("127.0.0.1", srv.getsockname()[1])); conn, addr = srv.accept()
        try:
            self.assertEqual(peer.socket_uid(addr[:2], srv.getsockname()[:2]), os.getuid())
            self.assertIsNone(peer.socket_uid(addr[:2], ("127.0.0.1", 1)))   # 對不上就是 None → 呼叫端拒絕
        finally:
            for s in (conn, cli, srv):
                s.close()

    def test_same_port_other_ip_other_uid(self):
        """Codex 第三輪：只比埠號時，127.0.0.2:50000（uid 1001）會被認成 127.0.0.1:50000（uid 1000）那一列。"""
        import tempfile
        hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        row = lambda ip, uid, ino: f"   0: {peer._hex4(ip, 50000)} {peer._hex4('127.0.0.1', 11001)} 01 00000000:00000000 00:00000000 00000000  {uid}        0 {ino} 1 0 20 4 30 10 -1\n"
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "net"))
            with open(os.path.join(d, "net", "tcp"), "w") as f:
                f.write(hdr + row("127.0.0.1", 1000, 111) + row("127.0.0.2", 1001, 222))
            srv_end = ("127.0.0.1", 11001)
            self.assertEqual(peer.socket_uid(("127.0.0.2", 50000), srv_end, d), 1001)
            self.assertEqual(peer.socket_uid(("127.0.0.1", 50000), srv_end, d), 1000)
            with open(os.path.join(d, "net", "tcp"), "a") as f:       # 同一個四元組出現兩列（不該發生）→ 歧義就拒絕
                f.write(row("127.0.0.2", 1000, 333))
            self.assertIsNone(peer.socket_uid(("127.0.0.2", 50000), srv_end, d))
            with open(os.path.join(d, "net", "tcp6"), "w") as f:      # IPv4-mapped 的客戶端 socket 在 tcp6
                m = lambda ip, port: peer._hex6_mapped(ip, port)
                f.write(hdr + f"   0: {m('127.0.0.3', 50001)} {m('127.0.0.1', 11001)} 01 00000000:00000000 00:00000000 00000000  1000        0 444 1\n")
            self.assertEqual(peer.socket_uid(("127.0.0.3", 50001), srv_end, d), 1000)

    def test_scan_budget_stops_on_unreadable_dirs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for i in range(100):
                os.makedirs(os.path.join(d, str(i)))     # 沒有 fd 目錄：等同讀不到
            with mock.patch.object(peer, "SCAN_MAX_LINKS", 10):
                seen = []
                real = os.scandir

                def counting(path):
                    seen.append(path); return real(path)
                with mock.patch.object(peer.os, "scandir", counting):
                    self.assertIsNone(peer._owner("123", d))
                self.assertLess(len(seen), 15)


if __name__ == "__main__":
    unittest.main()
