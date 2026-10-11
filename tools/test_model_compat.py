"""Release compatibility checks, isolated from production services and policies."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import stability
from tools import model_capabilities as caps


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(stability, 'POLICY', self.tmp/'policy.json'))
        self.stack.enter_context(patch.dict(os.environ, {'SPARK_REQUIRE_GUARD':'0', 'SPARK_REQUIRE_MEMORY_POLICY':'0'}))
        self.events = self.stack.enter_context(patch.object(stability, 'event'))
        for name, value in [('_cached',None),('_when',0),('_last_route',None),('_drain_seen',False)]:
            self.stack.enter_context(patch.object(caps,name,value))

    def test_fresh_install_admission_is_optional(self):
        self.assertEqual(stability.admission()['state'], 'not_configured')
        self.assertTrue(stability.admission()['ok'])

    def test_strict_missing_or_invalid_policy_refuses(self):
        with patch.dict(os.environ, {'SPARK_REQUIRE_MEMORY_POLICY':'1'}):
            self.assertEqual(stability.admission()['reason'], 'memory_unknown')
        stability.POLICY.write_text('invalid json')
        self.assertFalse(stability.admission()['ok'])

    def test_absent_guard_direct_route_is_visible_and_event_is_not_flooded(self):
        with patch.object(caps,'probe',return_value=None):
            for _ in range(3):r=caps.route()
        self.assertEqual(r['endpoint'],caps.OLLAMA)
        self.assertFalse(r['guard']['blocked'])
        self.assertFalse(r['guard']['protected'])
        self.events.assert_called_once()

    def test_strict_guard_outage_never_falls_back(self):
        with patch.dict(os.environ, {'SPARK_REQUIRE_GUARD':'1'}), patch.object(caps,'probe',return_value=None):
            r=caps.route()
        self.assertEqual(r['endpoint'],caps.GUARD)
        self.assertTrue(r['guard']['blocked'])

    def test_live_guard_refusal_never_falls_back(self):
        with patch.object(caps,'probe',return_value={'ok':True,'blocked':True,'reason':'strata_resident'}):
            r=caps.route()
        self.assertEqual(r['endpoint'],caps.GUARD)
        self.assertEqual(r['guard']['reason'],'strata_resident')

    def test_old_strata_drain_absent_disables_switch(self):
        def probe(url):
            return {'ok':True,'blocked':False} if url.endswith('/guard/status') else None
        with patch.object(caps,'probe',side_effect=probe), patch.object(caps.llm_switch,'control_info',return_value={'available':True,'service_state':'active'}), patch.object(caps.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'inactive\n','')):
            c=caps.snapshot(force=True)
        self.assertFalse(c['drain']); self.assertFalse(c['switch_control']['available'])

    def test_known_drain_retained_during_our_stop_but_cleared_for_old_active_engine(self):
        info={'available':True,'service_state':'active'}
        reply={'draining':False,'active_requests':0}
        def probe(url):return {'ok':True,'blocked':False} if url.endswith('/guard/status') else reply
        with patch.object(caps,'probe',side_effect=probe), patch.object(caps.llm_switch,'control_info',return_value=info), patch.object(caps.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'inactive\n','')):
            self.assertTrue(caps.snapshot(force=True)['switch_control']['available'])
            info['service_state']='inactive';reply=None
            self.assertTrue(caps.snapshot(force=True)['switch_control']['available'])
            info['service_state']='active'
            self.assertFalse(caps.snapshot(force=True)['switch_control']['available'])

    def test_guard_installer_with_spaces_and_chinese_path_uses_user_unit_only(self):
        import shutil
        source=Path(__file__).resolve().parents[1]
        root=self.tmp/'中文 space & directory';root.mkdir()
        for name in ('install.sh','spark-ollama-guard.service.in'):shutil.copy(source/name,root/name)
        binary=self.tmp/'bin';binary.mkdir();log=self.tmp/'calls'
        shim=binary/'systemctl';shim.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n');shim.chmod(0o755)
        env={**os.environ,'HOME':str(self.tmp/'home'),'PATH':str(binary)+':'+os.environ['PATH'],'CALL_LOG':str(log)}
        subprocess.run(['sh',str(root/'install.sh'),'--guard'],env=env,check=True,capture_output=True)
        unit=(self.tmp/'home/.config/systemd/user/spark-ollama-guard.service').read_text()
        self.assertIn(str(root)+'/tools/ollama_guard.py',unit)
        self.assertIn('--user enable --now spark-ollama-guard.service',log.read_text())
        self.assertNotIn('spark-center.service',log.read_text())

    def test_earlyoom_without_policy_stops_before_apt_or_configuration(self):
        import shutil
        tools=self.tmp/'tools';tools.mkdir()
        source=Path(__file__).with_name('install_earlyoom.sh');shutil.copy(source,tools/source.name)
        binary=self.tmp/'bin';binary.mkdir()
        (binary/'id').write_text('#!/bin/sh\necho 0\n');(binary/'id').chmod(0o755)
        result=subprocess.run(['sh',str(tools/source.name)],env={**os.environ,'PATH':str(binary)+':'+os.environ['PATH']},capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertIn('Calibrate first',result.stderr)


if __name__=='__main__':unittest.main(verbosity=2)
