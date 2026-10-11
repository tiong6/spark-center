"""Exercise switch failures and handoff ordering without touching real services."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import llm_switch as switch


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(switch.guard.stability, 'admission', return_value={'ok': True, 'reason': None}))
        self.stack.enter_context(patch.object(switch.guard.stability, 'event'))
        self.addCleanup(self.stack.close)
        d = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for k, v in {'DATA': d, 'LOCK': d/'lock', 'PENDING': d/'pending'}.items():
            self.stack.enter_context(patch.object(switch.guard, k, v))
        self.info = {'available': True, 'service_state': 'active'}
        self.stack.enter_context(patch.object(switch, 'service_info', side_effect=lambda: dict(self.info)))
        self.proc = self.stack.enter_context(patch.object(switch.guard, 'strata_process', return_value=False))
        self.blocked = self.stack.enter_context(patch.object(switch.guard, 'blocked_reason', return_value=None))
        self.resident = []
        self.busy = False
        self.calls = []
        self.read = self.stack.enter_context(patch.object(switch, 'read', side_effect=self.read_json))
        self.stack.enter_context(patch.object(switch.guard, 'read_json', side_effect=self.read_json))
        self.service = self.stack.enter_context(patch.object(switch, 'service', side_effect=self.control))
        self.post = self.stack.enter_context(patch.object(switch, 'post', side_effect=self.request))
        self.stack.enter_context(patch.object(switch, 'drain', return_value={'token': 'test', 'active_requests': 0}))
        self.s = switch.Switcher()

    def read_json(self, address, path):
        if path == '/guard/status': return {'ok': True}
        if path == '/api/tags': return {'models': [{'name': 'local:4b'}]}
        if path == '/api/ps': return {'models': [{'name': x} for x in self.resident]}
        if path == '/health': return {'service': 'strata', 'loaded': True}
        if path == '/status': return {'busy': self.busy, 'queued': 0}
        raise AssertionError(path)

    def control(self, action):
        self.calls.append(action)
        if action == 'stop': self.info['service_state'] = 'inactive'
        else:
            # The real wrapper must be able to acquire its lease at handoff.
            with switch.guard.lease(exclusive=True):
                self.assertEqual(self.resident, [])
            self.info['service_state'] = 'active'

    def request(self, address, path, body):
        self.calls.append((path, body))
        if path == '/api/generate':
            if body['keep_alive'] == 0:
                self.resident.remove(body['model'])
            else:
                self.assertEqual(address, switch.GUARDED)
                self.assertEqual(self.info['service_state'], 'inactive')
                self.resident.append(body['model'])
        return {'done': True}

    def run_switch(self, target='ollama', model='local:4b'):
        self.s.run(target, model, '5m')
        return self.s.snapshot()

    def test_stop_strata_before_loading_and_verify_residency(self):
        self.assertEqual(self.run_switch()['status'], 'done')
        self.assertEqual(self.calls[0], 'stop')
        self.assertEqual(self.resident, ['local:4b'])

    def test_busy_strata_is_not_stopped(self):
        self.busy = True
        self.assertEqual(self.run_switch()['error'], 'strata_busy')
        self.assertEqual(self.calls, [])

    def test_unknown_strata_status_is_not_stopped(self):
        original = self.read_json
        self.read.side_effect = lambda a, p: None if p == '/status' else original(a, p)
        self.assertEqual(self.run_switch()['error'], 'state_unknown')
        self.assertEqual(self.calls, [])

    def test_missing_model_does_not_stop_strata_or_download(self):
        self.assertEqual(self.run_switch(model='missing:120b')['error'], 'not_installed')
        self.assertEqual(self.calls, [])

    def test_failed_stop_never_loads_ollama(self):
        self.service.side_effect = switch.SwitchError('service_failed')
        self.assertEqual(self.run_switch()['error'], 'service_failed')
        self.post.assert_not_called()

    def test_remaining_strata_engine_prevents_ollama_load(self):
        self.proc.return_value = True
        def fail_wait(check, timeout, code):
            self.assertFalse(check())
            raise switch.SwitchError(code)
        with patch.object(switch, 'wait_for', side_effect=fail_wait):
            self.assertEqual(self.run_switch()['error'], 'strata_not_stopped')
        self.post.assert_not_called()

    def test_all_ollama_models_unloaded_before_strata_handoff(self):
        self.info['service_state'] = 'inactive'
        self.resident = ['one', 'two']
        self.assertEqual(self.run_switch('strata')['status'], 'done')
        self.assertEqual(self.calls[-1], 'start')
        self.assertEqual(len(self.calls), 3)

    def test_active_ollama_lease_refuses_without_unloading(self):
        self.info['service_state'] = 'inactive'
        self.resident = ['one']
        with switch.guard.lease():
            self.assertEqual(self.run_switch('strata')['error'], 'ollama_busy')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.resident, ['one'])

    def test_pending_record_refuses_without_service_changes(self):
        switch.guard.PENDING.mkdir()
        (switch.guard.PENDING/'interrupted').write_text('{}')
        self.assertEqual(self.run_switch('strata')['error'], 'pending')
        self.assertEqual(self.calls, [])

    def test_non_systemd_strata_prevents_second_instance(self):
        self.info['service_state'] = 'inactive'
        self.proc.return_value = True
        self.assertEqual(self.run_switch('strata')['error'], 'state_unknown')
        self.assertEqual(self.calls, [])

    def test_failed_unload_does_not_start_strata(self):
        self.info['service_state'] = 'inactive'
        self.resident = ['one']
        self.post.side_effect = switch.SwitchError('request_failed')
        self.assertEqual(self.run_switch('strata')['error'], 'request_failed')
        self.service.assert_not_called()
        with switch.guard.lease(exclusive=True): pass

    def test_unload_ack_without_release_does_not_start_strata(self):
        self.info['service_state'] = 'inactive'
        self.resident = ['one']
        self.post.side_effect = None
        self.post.return_value = {'done': True}
        def fail_wait(check, timeout, code):
            self.assertFalse(check())
            raise switch.SwitchError(code)
        with patch.object(switch, 'wait_for', side_effect=fail_wait):
            self.assertEqual(self.run_switch('strata')['error'], 'not_unloaded')
        self.service.assert_not_called()

    def test_handoff_race_reports_failure_not_success(self):
        self.info['service_state'] = 'inactive'
        self.service.side_effect = lambda _: self.info.update(service_state='failed')
        self.assertEqual(self.run_switch('strata')['error'], 'service_failed')

    def test_stopping_service_with_live_health_is_not_success(self):
        self.info['service_state'] = 'inactive'
        self.service.side_effect = lambda _: self.info.update(service_state='deactivating')
        self.assertEqual(self.run_switch('strata')['error'], 'service_failed')

    def test_missing_wrapper_never_changes_services(self):
        self.info['available'] = False
        self.assertEqual(self.run_switch()['error'], 'service_unconfigured')
        self.assertEqual(self.calls, [])

    def test_duplicate_and_invalid_submissions_do_not_start_workers(self):
        with patch.object(switch.threading, 'Thread') as worker:
            with self.assertRaises(switch.SwitchError): self.s.start('invalid')
            worker.assert_not_called()
            self.s.start('strata')
            with self.assertRaises(switch.SwitchError): self.s.start('ollama', 'local:4b')
            self.assertEqual(worker.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
