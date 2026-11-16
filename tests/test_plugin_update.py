"""官方插件独立升级：版本判定、备份回滚、清单过滤与失败保护。"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from plugin_manager import PluginManager
from plugin_pack import pack
from plugin_store import PluginStore
from plugin_update import PluginUpdater, seed_official_plugins
import versions

ROOT = Path(__file__).resolve().parents[1]


def make_package(directory, identifier, version, base='example'):
    """以示例插件为骨架造一个官方 ID 的包，避免测试依赖真实插件内容。"""
    source = Path(directory) / f'src-{identifier}-{version}'
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(ROOT / 'plugins' / base, source)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    manifest.update(id=identifier, name='测试插件 ' + identifier, version=version)
    manifest.pop('minAppVersion', None)
    (source / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return pack(source, Path(directory) / f'{identifier}-{version}.dmp')


def entry_for(package, version, **extra):
    data = Path(package).read_bytes()
    return {'file': Path(package).name, 'version': version, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'urls': [Path(package).resolve().as_uri()],
            **extra}


def manifest_with(plugins, status='release'):
    return {'schema': 1, 'channel': 'stable', 'version': '3.1.7', 'tag': 'v3.1.7',
            'status': status, 'plugins': plugins}


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(versions.compare('3.1.10', '3.1.9'), 0)
        self.assertEqual(versions.compare('v3.1.7', '3.1.7.0'), 0)
        self.assertLess(versions.compare('1.0.0-beta.2', '1.0.0'), 0)
        self.assertGreater(versions.compare('1.0.0', '1.0.0-rc.1'), 0)
        self.assertTrue(versions.is_newer('1.0.10', '1.0.9'))
        self.assertFalse(versions.is_newer('1.0.0', '1.0.0'))

    def test_invalid_and_minimum(self):
        with self.assertRaises(ValueError):
            versions.parse('latest')
        self.assertTrue(versions.satisfies_min('3.1.7', None))
        self.assertTrue(versions.satisfies_min('3.1.7', '3.1.0'))
        self.assertFalse(versions.satisfies_min('3.1.7', '3.2.0'))


class PluginUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = PluginManager(self.root / 'plugins')
        self.addCleanup(self.manager.close)
        self.install = make_package(self.root, 'mijia', '1.0.0')
        self.upgrade = make_package(self.root, 'mijia', '1.0.1')

    def install_package(self, package, enable=False):
        manifest = self.manager.dispatch('install', {
            'package': base64.b64encode(Path(package).read_bytes()).decode('ascii')})
        if enable:
            self.manager.dispatch('enable', {'id': manifest['id']})
        return manifest

    def updater(self, manifest):
        path = self.root / 'latest.json'
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        environment = patch.dict(os.environ, {'DEVMUX_UPDATE_MANIFEST': path.resolve().as_uri()})
        environment.start()
        self.addCleanup(environment.stop)
        return PluginUpdater(self.manager)

    def current_version(self, identifier='mijia'):
        return next(item for item in self.manager.dispatch('list', {}) if item['id'] == identifier)['version']

    def test_upgrade_replaces_package_keeps_enabled_and_writes_backup(self):
        self.install_package(self.install, enable=True)
        updater = self.updater(manifest_with({'mijia': entry_for(self.upgrade, '1.0.1')}))
        report = updater.check(force=True)
        self.assertEqual([item['id'] for item in report['updates']], ['mijia'])
        result = updater.apply(['mijia'])['results'][0]
        self.assertTrue(result['success'], result)
        self.assertEqual(self.current_version(), '1.0.1')
        self.assertIn('mijia', self.manager.enabled)
        self.assertEqual([item['version'] for item in self.manager.store.backups('mijia')], ['1.0.0'])
        # 升完再查应回到「没有更新」，否则界面会一直挂着更新按钮。
        self.assertEqual(updater.check(force=True)['updates'], [])

    def test_restore_returns_previous_version(self):
        self.install_package(self.install)
        self.manager.dispatch('install', {
            'package': self._encoded(self.upgrade), 'keepBackup': True})
        self.assertEqual(self.current_version(), '1.0.1')
        self.manager.dispatch('restore', {'id': 'mijia'})
        self.assertEqual(self.current_version(), '1.0.0')

    def test_uninstalled_and_third_party_are_ignored(self):
        self.install_package(self.install)
        other = make_package(self.root, 'example', '9.9.9')
        updater = self.updater(manifest_with({
            'mijia': entry_for(self.upgrade, '1.0.1'),
            'ebpf': entry_for(self.upgrade, '9.9.9'),
            'example': entry_for(other, '9.9.9'),
        }))
        report = updater.check(force=True)
        self.assertEqual([item['id'] for item in report['updates']], ['mijia'])

    def test_checksum_mismatch_leaves_plugin_untouched(self):
        self.install_package(self.install, enable=True)
        broken = entry_for(self.upgrade, '1.0.1')
        broken['sha256'] = '0' * 64
        updater = self.updater(manifest_with({'mijia': broken}))
        result = updater.apply(['mijia'])['results'][0]
        self.assertFalse(result['success'])
        self.assertIn('SHA-256', result['error'])
        self.assertEqual(self.current_version(), '1.0.0')
        self.assertIn('mijia', self.manager.enabled)

    def test_minimum_app_version_blocks_upgrade(self):
        self.install_package(self.install)
        updater = self.updater(manifest_with({
            'mijia': entry_for(self.upgrade, '1.0.1', minAppVersion='99.0.0')}))
        report = updater.check(force=True)
        self.assertEqual(report['updates'], [])
        self.assertEqual(report['blocked'][0]['id'], 'mijia')
        result = updater.apply(['mijia'])['results'][0]
        self.assertFalse(result['success'])
        self.assertIn('99.0.0', result['error'])

    def test_candidate_manifest_is_not_used_for_upgrade(self):
        self.install_package(self.install)
        updater = self.updater(manifest_with({'mijia': entry_for(self.upgrade, '1.0.1')},
                                             status='candidate'))
        report = updater.check(force=True)
        self.assertEqual(report['updates'], [])
        self.assertIn('候选', report['error'])

    def test_install_rejects_unexpected_package(self):
        payload = {'package': self._encoded(self.upgrade), 'expectId': 'usb-relay'}
        with self.assertRaises(ValueError):
            self.manager.dispatch('install', payload)
        payload = {'package': self._encoded(self.upgrade), 'expectId': 'mijia', 'expectVersion': '9.9.9'}
        with self.assertRaises(ValueError):
            self.manager.dispatch('install', payload)
        self.assertEqual(self.manager.dispatch('list', {}), [])

    def _encoded(self, package):
        return base64.b64encode(Path(package).read_bytes()).decode('ascii')


class StoreBackupTests(unittest.TestCase):
    def test_only_two_generations_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            store = PluginStore(Path(temp) / 'plugins')
            versions_to_install = ['1.0.0', '1.0.1', '1.0.2', '1.0.3']
            packages = [make_package(temp, 'mijia', version) for version in versions_to_install]
            for package in packages:
                store.install(io.BytesIO(Path(package).read_bytes()), keep_backup=True)
            kept = [item['version'] for item in store.backups('mijia')]
            self.assertEqual(len(kept), 2)
            self.assertNotIn('1.0.0', kept)
            self.assertIn('1.0.2', kept)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = PluginManager(self.root / 'installed')
        self.addCleanup(self.manager.close)
        self.plugin_dir = self.root / 'plugin'
        self.plugin_dir.mkdir(parents=True)

    def _ids(self):
        return [item['id'] for item in self.manager.dispatch('list', {})]

    def test_imports_official_and_is_idempotent(self):
        make_package(self.plugin_dir, 'mijia', '1.0.0')
        seed_official_plugins(self.manager, self.plugin_dir)
        self.assertEqual(self._ids(), ['mijia'])
        seed_official_plugins(self.manager, self.plugin_dir)
        self.assertEqual(self._ids(), ['mijia'])

    def test_skips_non_official(self):
        make_package(self.plugin_dir, 'example', '9.9.9')
        seed_official_plugins(self.manager, self.plugin_dir)
        self.assertEqual(self._ids(), [])

    def test_missing_dir_is_noop(self):
        seed_official_plugins(self.manager, self.root / 'nonexistent')
        self.assertEqual(self._ids(), [])

    def test_does_not_overwrite_installed(self):
        package = make_package(self.root, 'mijia', '1.0.0')
        self.manager.dispatch('install', {'package': base64.b64encode(package.read_bytes()).decode()})
        make_package(self.plugin_dir, 'mijia', '1.0.1')
        seed_official_plugins(self.manager, self.plugin_dir)
        self.assertEqual(self._ids(), ['mijia'])
        version = next(item for item in self.manager.dispatch('list', {}) if item['id'] == 'mijia')['version']
        self.assertEqual(version, '1.0.0')


def run():
    import sys
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])).wasSuccessful()
