"""Default data migration and official plugin release layout, using disposable directories."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import app_paths
import release_plugins


class ApplicationPathsTest(unittest.TestCase):
    def test_live_legacy_instance_blocks_migration_without_moving_data(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app_paths, 'application_directory', return_value=Path(temp)), patch.object(app_paths, '_pid_alive', return_value=True):
            base = Path(temp)
            (base / 'relay.json').write_text('legacy config', encoding='utf-8')
            (base / 'relay.lock').write_text(json.dumps({'pid': 12345}), encoding='utf-8')
            (base / 'commands.json').write_text('legacy commands', encoding='utf-8')
            plugin = base / 'plugins-123/.data/mijia'
            plugin.mkdir(parents=True)
            (plugin / 'credentials.dat').write_bytes(b'protected credentials')
            before = {path.relative_to(base): path.read_bytes() for path in base.rglob('*') if path.is_file()}
            with self.assertRaisesRegex(RuntimeError, '请先退出旧版 DevMux'):
                app_paths.data_directory()
            for relative, content in before.items():
                self.assertEqual((base / relative).read_bytes(), content)
            self.assertFalse((base / 'devmux-data/relay.json').exists())
            self.assertFalse((base / 'devmux-data/plugins-123').exists())

    def test_completed_migration_ignores_leftover_legacy_lock(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app_paths, 'application_directory', return_value=Path(temp)), patch.object(app_paths, '_pid_alive', return_value=True):
            base = Path(temp)
            target = base / 'devmux-data'
            target.mkdir()
            (target / 'relay.json').write_text('current config', encoding='utf-8')
            (base / 'relay.lock').write_text(json.dumps({'pid': 12345}), encoding='utf-8')
            self.assertEqual(app_paths.data_directory(), target)
            self.assertEqual((target / 'relay.json').read_text(encoding='utf-8'), 'current config')

    def test_concurrent_first_start_migrates_data_once(self):
        script = '''
import json,sys,time
from pathlib import Path
import app_paths
base = Path(sys.argv[1])
app_paths.application_directory = lambda: base
move = app_paths.shutil.move
def slow_move(*args, **kwargs):
    time.sleep(.05)
    return move(*args, **kwargs)
app_paths.shutil.move = slow_move
print('ready', flush=True)
sys.stdin.readline()
target = app_paths.data_directory()
print(json.dumps({'config': (target/'relay.json').read_text(), 'credentials': (target/'plugins-123/.data/mijia/credentials.dat').read_text()}), flush=True)
'''
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'relay.json').write_text('same config', encoding='utf-8')
            plugin = base / 'plugins-123/.data/mijia'
            plugin.mkdir(parents=True)
            (plugin / 'credentials.dat').write_text('same credentials', encoding='utf-8')
            processes = []
            try:
                for _ in range(2):
                    process = subprocess.Popen([sys.executable, '-c', script, str(base)], cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                    processes.append(process)
                for process in processes:
                    self.assertEqual(process.stdout.readline().strip(), 'ready')
                for process in processes:
                    process.stdin.write('go\n')
                    process.stdin.flush()
                for process in processes:
                    output, error = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, error)
                    self.assertEqual(json.loads(output), {'config': 'same config', 'credentials': 'same credentials'})
                self.assertFalse((base / 'relay.json').exists())
                self.assertFalse((base / 'plugins-123').exists())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_migrates_known_data_and_keeps_release_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app_paths, 'application_directory', return_value=Path(temp)):
            base = Path(temp)
            for name in ('relay.json', 'commands.json', 'devmux.log', 'crash_dump.txt'):
                (base / name).write_text(name, encoding='utf-8')
            old = base / 'plugins-123' / '.data' / 'mijia'
            old.mkdir(parents=True)
            (old / 'credentials.dat').write_bytes(b'encrypted-credentials')
            for name in ('DevMux.exe', 'RELEASE-NOTES.md', 'ebpf-0.0.1.dmp'):
                (base / name).write_text('release', encoding='utf-8')
            target = app_paths.data_directory()
            self.assertEqual(target, base / 'devmux-data')
            self.assertEqual((target / 'plugins-123/.data/mijia/credentials.dat').read_bytes(), b'encrypted-credentials')
            self.assertFalse((base / 'plugins-123').exists())
            self.assertEqual((target / 'relay.json').read_text(encoding='utf-8'), 'relay.json')
            self.assertTrue((base / 'DevMux.exe').exists())
            self.assertTrue((base / 'ebpf-0.0.1.dmp').exists())
            self.assertEqual(app_paths.data_directory(), target)

    def test_migration_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app_paths, 'application_directory', return_value=Path(temp)):
            base = Path(temp)
            (base / 'devmux-data').mkdir()
            (base / 'relay.json').write_text('old', encoding='utf-8')
            (base / 'devmux-data/relay.json').write_text('current', encoding='utf-8')
            target = app_paths.data_directory()
            self.assertEqual((target / 'relay.json').read_text(encoding='utf-8'), 'current')
            self.assertEqual((base / 'relay.json').read_text(encoding='utf-8'), 'old')

    def test_default_config_keeps_plugin_storage_identity(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app_paths, 'application_directory', return_value=Path(temp)), patch.dict('os.environ', {}, clear=True):
            base = Path(temp)
            old_hash = hashlib.sha256(str(base / 'relay.json').encode('utf-8')).hexdigest()[:16]
            self.assertEqual(app_paths.storage_identity(base / 'devmux-data/relay.json'), old_hash)
            with patch.dict('os.environ', {'RELAY_CONFIG': str(base / 'devmux-data/relay.json')}):
                explicit_hash = hashlib.sha256(str(base / 'devmux-data/relay.json').encode('utf-8')).hexdigest()[:16]
                self.assertEqual(app_paths.storage_identity(base / 'devmux-data/relay.json'), explicit_hash)

    def test_official_packages_include_ebpf_with_versioned_names(self):
        with tempfile.TemporaryDirectory() as temp:
            packages = release_plugins.build_official_plugins(Path(temp))
            identifiers = set()
            for package in packages:
                self.assertEqual(package.parent, (Path(temp) / 'plugin').resolve())
                with zipfile.ZipFile(package) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(package.name, f"{manifest['id']}-{manifest['version']}.dmp")
                identifiers.add(manifest['id'])
            self.assertEqual(identifiers, {'usb-relay', 'db-flash', 'mijia', 'ebpf'})
            self.assertEqual(list(Path(temp).glob('*.dmp')), [])


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ApplicationPathsTest)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
