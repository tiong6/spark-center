"""Memory, history and switch failure injection; no production model/service actions."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import stability
from tools import llm_switch, ollama_guard


class Tests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(stability.os.environ, {'SPARK_REQUIRE_MEMORY_POLICY':'1'}))
        self.tmp=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name,value in {'DATA':self.tmp,'DB':self.tmp/'events.sqlite3','POLICY':self.tmp/'policy.json'}.items():
            self.stack.enter_context(patch.object(stability,name,value))
        stability.POLICY.write_text(json.dumps({'min_available_bytes':100,'mem_total_bytes':1000,'max_full_psi_avg10':10}))
        self.sample={'available_bytes':200,'total':1000,'at':time.time(),'psi':{'full':{'avg10':0}}}

    def test_low_memory_pressure_and_unknown_refuse(self):
        self.assertTrue(stability.admission(self.sample)['ok'])
        self.sample['available_bytes']=99
        self.assertEqual(stability.admission(self.sample)['reason'],'memory_low')
        self.sample['available_bytes']=200;self.sample['psi']['full']['avg10']=10
        self.assertEqual(stability.admission(self.sample)['reason'],'memory_pressure')
        stability.POLICY.unlink()
        self.assertEqual(stability.admission(self.sample)['reason'],'memory_unknown')

    def test_event_dedup_counter_survives_retention(self):
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        now=time.time()
        with patch.object(stability,'_LIMIT',2):
            for i in range(3):stability.event('nvrm_oom',{'boot':boot},key=str(i),at=now+i*10)
            stability.event('nvrm_oom',{'boot':boot},key='2',at=now+20)
        with stability.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],2)
            stats=json.loads(c.execute('SELECT value FROM meta WHERE key=?',('nvrm-'+boot,)).fetchone()[0])
        self.assertEqual(stats['lines'],3);self.assertEqual(stats['groups'],3)

    def test_reverse_journal_output_is_grouped_chronologically(self):
        now = time.time()
        records = [{'MESSAGE':'NV_ERR_NO_MEMORY', '__CURSOR':str(i),
                    '__REALTIME_TIMESTAMP':str(int((now+i*20)*1e6)),
                    '__MONOTONIC_TIMESTAMP':str(i*20000000)} for i in [2,1,0]]
        fake=type('R',(),{'returncode':0,'stdout':'\n'.join(json.dumps(r) for r in records)})()
        with patch.object(stability.subprocess,'run',return_value=fake):
            stability.kernel_events()
            stability.kernel_events()
        snap=stability.snapshot()
        self.assertEqual(snap['nvrm_groups'],3)
        self.assertEqual(snap['nvrm_lines'],3)

    def test_state_write_failure_does_not_start_worker_or_retain_lock(self):
        first=llm_switch.Switcher(self.tmp/'switch.json')
        with patch.object(first,'persist',side_effect=OSError('disk full')), patch.object(llm_switch.threading,'Thread') as worker:
            with self.assertRaises(llm_switch.SwitchError):first.start('strata')
            worker.assert_not_called()
        self.assertIsNone(first.hold)
        self.assertEqual(first.snapshot()['status'],'error')

    def test_empty_journal_window_is_success(self):
        fake=type('R',(),{'returncode':1,'stdout':'','stderr':''})()
        with patch.object(stability.subprocess,'run',return_value=fake):
            stability.kernel_events()

    def test_unreadable_journal_is_not_success(self):
        fake=type('R',(),{'returncode':1,'stdout':'','stderr':'Permission denied'})()
        with patch.object(stability.subprocess,'run',return_value=fake):
            with self.assertRaises(OSError):stability.kernel_events()

    def test_interrupted_switch_keeps_phase_and_private_owner(self):
        path=self.tmp/'switch.json'
        path.write_text(json.dumps({'status':'running','phase':'stopping_strata','drain_token':'secret'}))
        s=llm_switch.Switcher(path)
        self.assertEqual(s.snapshot()['error'],'interrupted')
        self.assertEqual(s.snapshot()['phase'],'stopping_strata')
        self.assertNotIn('drain_token',s.snapshot())
        self.assertEqual(s.state['drain_token'],'secret')

    def test_cross_process_lock_rejects_second_controller(self):
        path=self.tmp/'switch.json'
        first=llm_switch.Switcher(path);second=llm_switch.Switcher(path)
        with patch.object(llm_switch.threading,'Thread'):
            first.start('strata')
            try:
                with self.assertRaises(llm_switch.SwitchError) as caught:second.start('strata')
                self.assertEqual(caught.exception.code,'busy')
            finally:first.hold.close()

    def test_entering_during_idle_check_prevents_stop_and_resumes(self):
        s=llm_switch.Switcher()
        with patch.object(llm_switch,'read',side_effect=[{'service':'strata'},{'busy':False,'queued':0}]), \
             patch.object(llm_switch,'drain',return_value={'token':'owner','active_requests':1}), \
             patch.object(llm_switch,'service') as service:
            with self.assertRaises(llm_switch.SwitchError) as caught:
                s.to_ollama('local','5m',{'service_state':'active'})
        self.assertEqual(caught.exception.code,'strata_busy');service.assert_not_called()
        self.assertEqual(s.state['drain_token'],'owner')


if __name__ == '__main__':unittest.main(verbosity=2)
