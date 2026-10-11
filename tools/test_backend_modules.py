"""Regression checks for extracted backend state; no hardware mutations or NAS scans."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import hardware, disk, system


class ModuleTests(unittest.TestCase):
    def test_standalone_import_has_no_server_or_apt_dependency(self):
        code = "from backend import hardware, disk; import sys; assert 'server' not in sys.modules; assert 'apt' not in sys.modules"
        subprocess.run([sys.executable, '-c', code], cwd=ROOT, check=True)

    def test_server_uses_public_feature_interfaces(self):
        tree = ast.parse((ROOT / 'server.py').read_text())
        private_accesses = [f'{node.value.id}.{node.attr}' for node in ast.walk(tree)
                            if isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id in {'hardware', 'disk'}
                            and node.attr.startswith('_')]
        self.assertEqual(private_accesses, [])

    def test_data_paths_still_use_project_root(self):
        self.assertEqual(Path(system.HERE), ROOT)
        self.assertEqual(Path(hardware.HW_SNAPSHOT), ROOT / 'data/hardware.json')
        self.assertEqual(Path(hardware.USBC_MAP_FILE), ROOT / 'data/usbc-map.json')

    def test_snapshot_rejects_previous_boot_and_reuses_current_boot(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'hardware.json'
            snapshot = {'generated': 'test', 'boot_id': 'old', 'system': {'hostname': 'test'}, 'printers': {}}
            with patch.object(hardware, 'HW_SNAPSHOT', str(path)), patch.object(hardware, '_boot_id', return_value='current'), patch.object(hardware, '_HW_CACHE', {'ts': 0, 'data': None}):
                path.write_text(json.dumps(snapshot))
                hardware.load_snapshot()
                self.assertIsNone(hardware._HW_CACHE['data'])
                snapshot['boot_id'] = 'current'
                path.write_text(json.dumps(snapshot))
                hardware.load_snapshot()
                with patch.object(hardware, '_run', side_effect=AssertionError('cached lookup must not rescan')):
                    data = hardware.hardware_static()
                self.assertEqual(data['system']['hostname'], 'test')
                self.assertNotIn('printers', data)

    def test_disk_cache_invalidated_and_single_background_scan_requested(self):
        state = {'lock': threading.Lock(), 'data': {'categories': []}, 'ts': 100, 'scanning': False}
        with patch.object(disk, '_DISK', state), patch.object(disk.time, 'time', return_value=101), patch.object(disk.threading, 'Thread') as worker:
            self.assertFalse(disk.disk_status()['scanning'])
            worker.assert_not_called()
            disk.invalidate()
            with patch.object(disk.time, 'time', return_value=1000):
                self.assertTrue(disk.disk_status()['scanning'])
            worker.assert_called_once_with(target=disk.disk_scan, daemon=True)
            state['scanning'] = True
            disk.disk_status(force=True)
            self.assertEqual(worker.call_count, 1)

    def test_failed_disk_scan_releases_flag_and_keeps_previous_data(self):
        old = {'categories': []}
        state = {'lock': threading.Lock(), 'data': old, 'ts': 1, 'scanning': False}
        with patch.object(disk, '_DISK', state), patch.object(disk.os, 'statvfs', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                disk.disk_scan()
            self.assertFalse(state['scanning'])
            self.assertIs(state['data'], old)
            self.assertTrue(state['lock'].acquire(blocking=False))
            state['lock'].release()

    def test_du_children_does_not_follow_symlink_to_nas(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'local').mkdir()
            (root / 'nas').symlink_to('/mnt/unavailable-nas', target_is_directory=True)
            with patch.object(disk, '_du', return_value=10) as du:
                result = disk._du_children(td)
            du.assert_called_once_with(str(root / 'local'))
            self.assertEqual([v['name'] for v in result], ['local'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
