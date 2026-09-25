#!/usr/bin/python3
"""隔離來源檔／備份／apt 命令的回歸測試；絕不呼叫 pkexec 或修改系統來源。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server

spec = importlib.util.spec_from_file_location('node_source', ROOT / 'tools/node_source.py')
ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns)

SOURCE_TEXT = ('Types: deb\nURIs: https://deb.nodesource.com/node_22.x\nSuites: nodistro\n'
               'Components: main\nArchitectures: arm64\nSigned-By: /usr/share/keyrings/nodesource.gpg\n')


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='spark-node-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'sources.list.d/nodesource.sources'
        self.source.parent.mkdir()
        self.source.write_text(SOURCE_TEXT)
        self.store = self.root / 'state'
        self.version = '22.1.0-1nodesource1'
        self.payload = b'isolated signed package fixture'
        self.commands = []
        self.update_fails = 0
        self.install_fails = False
        self.bad_hash = False
        self.simulation = 'Inst nodejs [22.1.0] (24.1.0)\n'
        for p in (patch.object(ns, 'SOURCE', self.source), patch.object(ns, 'STORE', self.store),
                  patch.object(ns.os, 'geteuid', return_value=0), patch.object(ns, 'installed', side_effect=self.installed),
                  patch.object(ns, 'execute', side_effect=self.execute)):
            p.start(); self.addCleanup(p.stop)

    def installed(self):
        old = Obj(version=self.version, sha256=hashlib.sha256(self.payload).hexdigest(),
                  size=len(self.payload), origins=[Obj(trusted=True, site='deb.nodesource.com')])
        return Obj(installed=old, candidate=Obj(version='24.1.0-1nodesource1', origins=old.origins))

    def execute(self, cmd, *, capture=True, cwd=None):
        self.commands.append(cmd)
        if '--print-architecture' in cmd:
            return 'arm64\n'
        if 'download' in cmd:
            Path(cwd, 'previous.deb').write_bytes(b'corrupt' if self.bad_hash else self.payload)
        if 'update' in cmd and self.update_fails:
            self.update_fails -= 1
            raise RuntimeError('node_command_failed')
        if '-s' in cmd:
            return self.simulation
        if 'install' in cmd:
            if self.install_fails:
                raise RuntimeError('node_command_failed')
            self.version = '22.1.0-1nodesource1' if str(self.store / 'previous.deb') in cmd else '24.1.0-1nodesource1'
        return ''

    def run_action(self, action, target=24):
        plan = ns.preview(action, target)
        ns.mutate(action, target, plan['token'])
        return ns.state_load()

    def test_prepare_install_restore(self):
        self.assertEqual(self.run_action('prepare')['phase'], 'prepared')
        self.assertEqual(self.version, '22.1.0-1nodesource1')
        self.assertIn('node_24.x', self.source.read_text())
        self.assertEqual(self.run_action('install')['phase'], 'installed')
        self.assertEqual(self.version, '24.1.0-1nodesource1')
        self.assertEqual(self.run_action('restore')['phase'], 'restored')
        self.assertEqual(self.source.read_text(), SOURCE_TEXT)
        self.assertEqual(self.version, '22.1.0-1nodesource1')
        for cmd in self.commands:
            if 'install' in cmd:
                self.assertIn('--no-remove', cmd)
                self.assertIn('Dpkg::Options::=--force-confold', cmd)

    def test_next_major_can_replace_completed_upgrade(self):
        self.run_action('prepare')
        self.run_action('install')
        with patch.object(ns, 'target_version', return_value='26.1.0-1nodesource1'):
            state = self.run_action('prepare', 26)
        self.assertEqual(state['old_version'], '24.1.0-1nodesource1')
        self.assertIn('node_26.x', self.source.read_text())

    def test_preview_is_read_only_and_stale_confirmation_refused(self):
        plan = ns.preview('prepare', 24)
        self.assertFalse(self.store.exists())
        self.source.write_text(SOURCE_TEXT + '# changed\n')
        with self.assertRaisesRegex(RuntimeError, 'node_plan_changed'):
            ns.mutate('prepare', 24, plan['token'])
        self.assertFalse(any('download' in cmd for cmd in self.commands))

    def test_bad_backup_does_not_switch_source(self):
        self.bad_hash = True
        with self.assertRaisesRegex(RuntimeError, 'node_backup_invalid'):
            self.run_action('prepare')
        self.assertEqual(self.source.read_text(), SOURCE_TEXT)
        self.assertIsNone(ns.state_load())

    def test_refresh_failure_restores_source(self):
        self.update_fails = 1
        with self.assertRaises(RuntimeError):
            self.run_action('prepare')
        self.assertEqual(self.source.read_text(), SOURCE_TEXT)
        self.assertEqual(ns.state_load()['phase'], 'restored')

    def test_restore_refresh_failure_remains_retryable(self):
        self.update_fails = 2
        with self.assertRaises(RuntimeError):
            self.run_action('prepare')
        self.assertEqual(ns.state_load()['phase'], 'restore_failed')
        self.assertEqual(self.run_action('restore')['phase'], 'restored')

    def test_unavailable_target_restores_source(self):
        with patch.object(ns, 'target_version', side_effect=RuntimeError('node_target_unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'node_target_unavailable'):
                self.run_action('prepare')
        self.assertEqual(self.source.read_text(), SOURCE_TEXT)

    def test_cancel_preparation_does_not_install(self):
        self.run_action('prepare')
        self.run_action('restore')
        self.assertFalse(any('install' in cmd for cmd in self.commands))

    def test_failed_install_keeps_backup_and_allows_restore(self):
        self.run_action('prepare')
        self.install_fails = True
        with self.assertRaises(RuntimeError):
            self.run_action('install')
        self.assertEqual(ns.state_load()['phase'], 'install_failed')
        self.assertTrue(Path(ns.verify_backup(ns.state_load())).is_file())
        self.install_fails = False
        self.assertEqual(self.run_action('restore')['phase'], 'restored')

    def test_changed_source_is_not_overwritten(self):
        self.run_action('prepare')
        changed = self.source.read_text() + '# administrator change\n'
        self.source.write_text(changed)
        with self.assertRaisesRegex(RuntimeError, 'node_source_changed'):
            ns.preview('restore', 24)
        self.assertEqual(self.source.read_text(), changed)

    def test_ambiguous_source_is_rejected(self):
        for suffix in ('Types: deb\n', 'Trusted: yes\n'):
            self.source.write_text(SOURCE_TEXT + suffix)
            with self.assertRaisesRegex(RuntimeError, 'node_source_unsupported'):
                ns.source_read()
        self.source.write_text(SOURCE_TEXT)
        (self.source.parent / 'another.list').write_text('deb https://deb.nodesource.com/node_20.x nodistro main')
        with self.assertRaisesRegex(RuntimeError, 'node_source_unsupported'):
            ns.source_read()

    def test_corrupt_saved_deb_blocks_install_and_restore(self):
        self.run_action('prepare')
        (self.store / 'previous.deb').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'node_backup_invalid'):
            ns.preview('install', 24)
        self.version = '24.1.0-1nodesource1'
        with self.assertRaisesRegex(RuntimeError, 'node_backup_invalid'):
            ns.preview('restore', 24)


class ApiTest(unittest.TestCase):
    def test_only_official_target_can_prepare(self):
        with patch.object(server, 'node_status', return_value={'ok': True, 'target': 24}), patch.object(server, 'node_read') as read:
            self.assertFalse(server.node_plan('prepare', 99)['ok'])
            self.assertFalse(server.node_plan('prepare', True)['ok'])
            read.assert_not_called()

    def test_nvm_is_not_changed(self):
        with patch.object(server.shutil, 'which', return_value='/tmp/nvm/bin/node'), patch.object(server.subprocess, 'run') as run:
            self.assertFalse(server.node_read('status')['ok'])
            run.assert_not_called()

    def test_npm_impact_checks_installed_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, constraint in [('new-tool', '>=24'), ('compatible', '>=20')]:
                p = Path(tmp, 'lib/node_modules', name); p.mkdir(parents=True)
                (p / 'package.json').write_text(json.dumps({'engines': {'node': constraint}}))
            r = subprocess.CompletedProcess([], 0, json.dumps({'dependencies': {n: {} for n in ['new-tool', 'compatible', 'unreadable']}}))
            with patch.object(server.subprocess, 'run', return_value=r), patch.object(server, '_run', return_value=tmp), patch.object(server, '_node_semver_satisfies', return_value={'new-tool': False, 'compatible': True}):
                impact = server.node_npm_impact('22.1.0-1nodesource1')
            self.assertEqual([x['name'] for x in impact], ['new-tool', 'unreadable'])
            self.assertTrue(impact[1]['unknown'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
