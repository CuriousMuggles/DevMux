"""内置 ADB 缓存分版、并发、校验和解析入口；不连接设备或启动 ADB 服务。"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import adb_runtime
import adb_worker


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adb-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / '_MEI-test' / 'adb'
        self.bundle.mkdir(parents=True)
        for name in adb_runtime.ADB_RUNTIME_FILES:
            (self.bundle / name).write_bytes(('bundle-' + name).encode())
        (self.bundle / 'fastboot.exe').write_bytes(b'not-cached')
        (self.bundle / 'relay.json').write_bytes(b'not-cached')
        self.cache = self.root / 'persistent cache'
        self.env = patch.dict(os.environ, {'ADB_MANAGER_TOOL_CACHE': str(self.cache)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def resolve(self):
        return Path(adb_runtime.persistent_adb_path(self.bundle))

    def test_copy_and_reuse_without_rewriting(self):
        executable = self.resolve()
        self.assertTrue(executable.is_relative_to(self.cache))
        self.assertFalse(executable.is_relative_to(self.bundle.parent))
        self.assertEqual({p.name for p in executable.parent.iterdir()}, set(adb_runtime.ADB_RUNTIME_FILES))
        for name in adb_runtime.ADB_RUNTIME_FILES:
            self.assertEqual((executable.parent / name).read_bytes(), (self.bundle / name).read_bytes())
        stamp = executable.stat().st_mtime_ns
        with patch.object(adb_runtime.shutil, 'copyfile', side_effect=AssertionError('不应覆写')):
            self.assertEqual(self.resolve(), executable)
        self.assertEqual(executable.stat().st_mtime_ns, stamp)

    def test_new_version_does_not_overwrite_old(self):
        first = self.resolve()
        (self.bundle / 'AdbWinApi.dll').write_bytes(b'new-dll')
        second = self.resolve()
        self.assertNotEqual(first, second)
        self.assertEqual((first.parent / 'AdbWinApi.dll').read_bytes(), b'bundle-AdbWinApi.dll')
        self.assertEqual((second.parent / 'AdbWinApi.dll').read_bytes(), b'new-dll')

    def test_concurrent_publication(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.resolve(), range(16)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(list(self.cache.iterdir()), [results[0].parent])

    def test_corruption_fails_without_overwriting(self):
        executable = self.resolve()
        executable.write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeError, '校验失败'):
            self.resolve()
        self.assertEqual(executable.read_bytes(), b'corrupted')

    def test_copy_failure_cleans_staging_and_can_retry(self):
        with patch.object(adb_runtime.shutil, 'copyfile', side_effect=PermissionError('占用')):
            with self.assertRaisesRegex(RuntimeError, '持久运行目录'):
                self.resolve()
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertTrue(self.resolve().is_file())

    def test_unwritable_root_fails(self):
        self.cache.write_bytes(b'not-a-directory')
        with self.assertRaisesRegex(RuntimeError, '持久运行目录'):
            self.resolve()

    def test_worker_resolver_and_overrides(self):
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(self.bundle.parent), create=True), \
             patch.dict(os.environ, {'ADB_PATH': ''}):
            self.assertEqual(Path(adb_worker._resolve_adb_path()), self.resolve())
            with patch.dict(os.environ, {'ADB_PATH': 'custom/adb.exe'}), \
                 patch.object(adb_runtime, 'persistent_adb_path', side_effect=AssertionError('显式路径不应复制')):
                self.assertEqual(adb_worker._resolve_adb_path(), 'custom/adb.exe')
        with patch.object(sys, 'frozen', False, create=True), patch.dict(os.environ, {'ADB_PATH': ''}):
            self.assertEqual(adb_worker._resolve_adb_path(), 'adb')

    def test_default_cache_location(self):
        with patch.dict(os.environ, {'ADB_MANAGER_TOOL_CACHE': '', 'LOCALAPPDATA': str(self.root / 'local')}):
            self.assertTrue(self.resolve().is_relative_to(self.root / 'local' / 'ADBManager' / 'platform-tools'))
        with patch.dict(os.environ, {'ADB_MANAGER_TOOL_CACHE': '', 'LOCALAPPDATA': ''}), \
             patch.object(adb_runtime.tempfile, 'gettempdir', return_value=str(self.root / 'temp')):
            self.assertTrue(self.resolve().is_relative_to(self.root / 'temp' / 'ADBManager' / 'platform-tools'))


def run():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests))
    return result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
