"""Regression tests: fake HTTP services and tiny files, never load real models."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('SPARK_CENTER_NO_SWEEP', '1')
import server
from tools import model_inventory as inventory, ollama_guard as guard


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for k, v in {'DATA': tmp, 'LOCK': tmp/'lease', 'EVENT': tmp/'event', 'PENDING': tmp/'pending'}.items():
            self.stack.enter_context(patch.object(guard, k, v))
        self.stack.enter_context(patch.object(guard, 'record_block'))
        self.reason = self.stack.enter_context(patch.object(guard, 'blocked_reason', return_value='strata_resident'))
        self.calls = []
        calls = self.calls
        self.first = threading.Event()
        self.finish = threading.Event()
        first, finish = self.first, self.finish
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                j = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(j)
                if j.get('truncate'):
                    self.send_response(200); self.send_header('Content-Length','20')
                    self.end_headers(); self.wfile.write(b'abc'); self.wfile.flush()
                    self.close_connection=True
                elif j.get('stream'):
                    self.send_response(200)
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    self.wfile.write(b'4\r\none\n\r\n'); self.wfile.flush(); first.set()
                    finish.wait(3)
                    self.wfile.write(b'4\r\ntwo\n\r\n0\r\n\r\n'); self.wfile.flush()
                else:
                    body = b'{"done":true}'
                    self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.proxy = ThreadingHTTPServer(('127.0.0.1', 0), guard.Handler)
        for s in (self.upstream, self.proxy):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close); self.addCleanup(s.shutdown)
        self.stack.enter_context(patch.object(guard, 'UPSTREAM', self.upstream.server_address))

    def request(self, body):
        c = http.client.HTTPConnection(*self.proxy.server_address, timeout=5)
        c.request('POST', '/api/generate', json.dumps(body), {'Content-Type':'application/json'})
        r = c.getresponse(); status, raw = r.status, r.read(); c.close()
        return status, json.loads(raw)

    def test_resident_refuses_without_forwarding(self):
        status, j = self.request({'model':'test'})
        self.assertEqual(status, 409); self.assertEqual(j['code'], 'strata_resident'); self.assertEqual(self.calls, [])

    def test_truncated_fixed_length_response_retains_pending(self):
        self.reason.return_value = None
        with self.assertRaises(http.client.IncompleteRead):
            self.request({'model':'test','truncate':True})
        self.assertEqual(len(list(guard.PENDING.iterdir())),1)

    def test_unload_allowed_but_generation_keep_alive_zero_refused(self):
        self.assertEqual(self.request({'model':'test','keep_alive':0})[0], 200)
        self.assertEqual(self.request({'model':'test','keep_alive':0,'prompt':'generate'})[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_stream_holds_lease_until_upstream_eof(self):
        self.reason.return_value = None
        c = http.client.HTTPConnection(*self.proxy.server_address, timeout=5)
        c.request('POST','/api/generate',json.dumps({'model':'test','stream':True}))
        r = c.getresponse()
        self.assertEqual(r.read(4), b'one\n')
        self.assertIsNone(guard.lease(exclusive=True))
        self.finish.set()
        self.assertEqual(r.read(), b'two\n'); c.close()
        with guard.lease(exclusive=True): pass
        self.assertEqual(list(guard.PENDING.iterdir()), [])

    def test_existing_strata_lease_blocks_even_when_health_is_down(self):
        self.reason.return_value = None
        with guard.lease(exclusive=True):
            self.assertEqual(self.request({'model':'test'})[0],409)
        self.assertEqual(self.calls, [])

    def test_disconnect_does_not_release_lease_before_upstream_finishes(self):
        import socket
        import time
        self.reason.return_value = None
        c = http.client.HTTPConnection(*self.proxy.server_address, timeout=5)
        c.request('POST','/api/generate',json.dumps({'model':'test','stream':True}))
        r = c.getresponse(); self.assertEqual(r.read(4), b'one\n')
        # Force downstream disconnect while the upstream is still generating.
        r.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        r.close(); c.close()
        self.assertIsNone(guard.lease(exclusive=True))
        self.finish.set()
        for _ in range(100):
            hold = guard.lease(exclusive=True)
            if hold:
                hold.close(); break
            time.sleep(.01)
        else: self.fail('Lease not released after upstream EOF')
        self.assertEqual(list(guard.PENDING.iterdir()), [])

    def test_failed_forward_retains_pending_marker(self):
        self.reason.return_value = None
        with patch.object(guard.http.client.HTTPConnection, 'request', side_effect=ConnectionRefusedError()):
            # Execute handler route in isolation so the test client's connection
            # itself is not intercepted by the patched HTTPConnection.
            import io
            h = object.__new__(guard.Handler)
            raw = b'{"model":"test"}'
            h.path='/api/generate'; h.command='POST'
            h.headers={'Host':'127.0.0.1','Content-Length':str(len(raw))}
            h.rfile=io.BytesIO(raw); h.reply=lambda code, body: self.assertEqual(code,502)
            h.route()
        self.assertEqual(len(list(guard.PENDING.iterdir())),1)

    def test_strata_wrapper_refuses_resident_or_interrupted_ollama(self):
        with patch.object(guard, 'read_json', return_value={'models':[{'name':'test'}]}), patch.object(guard.subprocess, 'Popen') as child:
            self.assertEqual(guard.run_strata(['fake']),75); child.assert_not_called()
        guard.PENDING.mkdir(); (guard.PENDING/'interrupted').write_text('{}')
        with patch.object(guard, 'read_json', return_value={'models':[]}), patch.object(guard.subprocess, 'Popen') as child:
            self.assertEqual(guard.run_strata(['fake']),75); child.assert_not_called()


class LogicTests(unittest.TestCase):
    def test_actual_engine_names_detected_without_matching_search_arguments(self):
        class Cmdline:
            def __init__(self,args): self.args=args
            def read_bytes(self): return b'\0'.join(self.args)
        for args, expected in [([b'/project/Strata/engine/strata-uma-prefill',b'--serve'],True),
                               ([b'/project/Strata/engine/strata-vision'],True),
                               ([b'rg',b'/project/Strata/serve/server.py'],False)]:
            with patch.object(guard.Path,'glob',return_value=[Cmdline(args)]):
                self.assertEqual(guard.strata_process(),expected)

    def test_strata_wrapper_executes_small_child_when_ollama_empty(self):
        with tempfile.TemporaryDirectory() as d, patch.object(guard,'DATA',Path(d)), patch.object(guard,'LOCK',Path(d)/'lease'), patch.object(guard,'PENDING',Path(d)/'pending'), patch.object(guard,'read_json',return_value={'models':[]}):
            self.assertEqual(guard.run_strata([sys.executable,'-c','print("wrapper child executed; no model loaded")']),0)

    def test_health_unknown_fails_closed(self):
        with patch.object(guard, 'read_json', side_effect=TimeoutError()):
            self.assertEqual(guard.blocked_reason(), 'strata_unknown')
        with patch.object(guard, 'read_json', side_effect=ConnectionRefusedError()), patch.object(guard, 'strata_process', return_value=True):
            self.assertEqual(guard.blocked_reason(), 'strata_unknown')

    def test_ollama_failed_query_is_not_empty(self):
        def fake(url, *args, **kwargs):
            if url.endswith('/api/version'): return {'version':'test'}
            if url.endswith('/api/tags'): return {'models':[]}
            return None
        with patch.object(server, '_http_json', side_effect=fake), patch.object(inventory,'snapshot',return_value={}):
            ol = server.llm_status()[0]
            self.assertIsNone(ol['loaded']); self.assertEqual(ol['loaded_state'],'unknown')
            self.assertEqual(ol['installed'],[]); self.assertEqual(ol['state'],'unavailable')

    def test_benchmark_capability_and_guard_checked(self):
        with patch.object(server,'_http_json',return_value={'capabilities':['embedding']}), patch.object(server,'llm_guard') as g:
            self.assertEqual(server.llm_admission('embed',completion=True)[1],422); g.assert_not_called()
        with patch.object(server,'_http_json',return_value={'capabilities':['completion']}), patch.object(server,'llm_guard',return_value={'blocked':True,'reason':'strata_resident'}):
            self.assertEqual(server.llm_admission('text',completion=True)[1],409)

    def test_guard_unavailable_has_no_direct_fallback(self):
        with patch.object(server,'_http_json',return_value=None):
            self.assertEqual(server.llm_guard(), {'ok':False,'blocked':True,'reason':'guard_unavailable'})

    def test_concurrent_benchmark_preserves_admission_error(self):
        state={'lock':threading.Lock(),'state':{}}
        with patch.object(server,'_LLM_BENCH',state), patch.object(server,'llm_request',side_effect=[{'done':True},RuntimeError('blocked by test guard'),RuntimeError('blocked by test guard')]):
            server._llm_bench_run_concurrent('fake',2,128)
        self.assertEqual(state['state']['status'],'error')
        self.assertEqual(state['state']['error'],'blocked by test guard')

    def test_inventory_aliases_deduplicate_and_missing_blob_is_not_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'blobs').mkdir(); m=root/'manifests/registry.ollama.ai/library/test'; m.mkdir(parents=True)
            blob={'digest':'sha256:'+'a'*64,'size':7,'mediaType':'application/vnd.ollama.image.model'}
            (root/'blobs'/('sha256-'+'a'*64)).write_bytes(b'1234567')
            manifest={'config':blob,'layers':[blob]}
            for tag in ('one','alias'): (m/tag).write_text(json.dumps(manifest))
            spec={'id':'test','path':d,'type':'ollama','storage':'SSD'}
            result=inventory.scan(spec)
            self.assertEqual(result['bytes'],7); self.assertEqual(len(result['rows']),1)
            self.assertEqual(len(result['rows'][0]['aliases']),2)
            (root/'blobs'/('sha256-'+'a'*64)).unlink()
            result=inventory.scan(spec)
            self.assertIsNone(result['bytes']); self.assertEqual(result['rows'][0]['state'],'missing')

    def test_nas_not_mounted_never_accesses_archive(self):
        with patch.object(inventory,'mounted',return_value='autofs'), patch.object(Path,'read_text') as read:
            with self.assertRaisesRegex(OSError,'NAS not mounted'):
                inventory.scan({'path':'/not-mounted/archive','storage':'NAS','type':'ollama_archive'})
            read.assert_not_called()

    def test_failed_refresh_keeps_previous_snapshot(self):
        import time
        with tempfile.TemporaryDirectory() as d, patch.object(inventory,'DATA',Path(d)), patch.object(inventory,'SOURCES',Path(d)/'sources.json'), patch.object(inventory,'CACHE',Path(d)/'cache.json'):
            spec={'id':'offline','path':str(Path(d)/'gone.json'),'type':'strata','storage':'SSD'}
            inventory.SOURCES.write_text(json.dumps([spec]))
            inventory.CACHE.write_text(json.dumps({'sources':[{**spec,'state':'metadata','checked_at':1,'rows':[{'name':'remembered','bytes':17}]}]}))
            self.assertTrue(inventory.refresh())
            for _ in range(100):
                if not inventory.snapshot()['scanning']: break
                time.sleep(.02)
            snapshot=inventory.snapshot(); self.assertFalse(snapshot['scanning'])
            source=snapshot['sources'][0]
            self.assertEqual(source['state'],'unavailable'); self.assertEqual(source['checked_at'],1)
            self.assertEqual(source['rows'][0]['name'],'remembered')


if __name__ == '__main__': unittest.main(verbosity=2)
