"""主程序升级：清单判定、白名单解压、替换与回滚。"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app_update import AppUpdater
import update_apply


def build_zip(path, version, extra=None, info_status='release'):
    """造一个最小发布包：exe + 构建信息 + 一个官方插件。"""
    path = Path(path)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('DevMux.exe', b'new-binary-' + version.encode())
        archive.writestr('BUILD-INFO.json', json.dumps({'version': version, 'status': info_status}))
        archive.writestr('SHA256SUMS.txt', '')
        archive.writestr('plugin/usb-relay-1.0.2.dmp', b'plugin')
        archive.writestr('使用说明.md', 'doc')
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return path


def manifest_for(package, version, **overrides):
    payload = {
        'schema': 1, 'channel': 'stable', 'version': version, 'tag': 'v' + version,
        'status': 'release', 'notes': '修复串口判断\n第二行说明',
        'assets': {'windows': {'name': Path(package).name, 'size': Path(package).stat().st_size,
                               'sha256': hashlib.sha256(Path(package).read_bytes()).hexdigest(),
                               'urls': [Path(package).resolve().as_uri()]}},
    }
    payload.update(overrides)
    return payload


class AppUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='devmux-app-update-'))
        self.data = self.temp / 'devmux-data'
        self.install = self.temp / 'install'
        (self.install / 'plugin').mkdir(parents=True)
        (self.install / 'DevMux.exe').write_bytes(b'old-binary')
        self.updater = AppUpdater(self.data, self.install, clock=lambda: 1000.0)
        self.package = build_zip(self.temp / 'DevMux-9.9.9-windows.zip', '9.9.9')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    def _stub(self, manifest):
        return patch('app_update.fetch_manifest', return_value=(manifest, 'test'))

    def _staged(self, version='9.9.9'):
        manifest = manifest_for(self.package, version)
        with self._stub(manifest):
            self.updater.check(force=True)
            return self.updater.download()

    # ---------- 检测 ----------

    def test_reports_newer_release(self):
        with self._stub(manifest_for(self.package, '9.9.9')):
            data = self.updater.check(force=True)
        self.assertTrue(data['available'])
        self.assertEqual(data['latest'], '9.9.9')
        self.assertEqual(data['size'], self.package.stat().st_size)
        self.assertIn('修复串口判断', data['notes'])

    def test_ignores_candidate_build(self):
        with self._stub(manifest_for(self.package, '9.9.9', status='candidate')):
            data = self.updater.check(force=True)
        self.assertFalse(data['available'])
        self.assertIn('候选构建', data['error'])

    def test_same_version_is_not_an_update(self):
        with self._stub(manifest_for(self.package, '3.1.7')):
            data = self.updater.check(force=True)
        self.assertFalse(data['available'])

    def test_skipped_version_stops_prompt(self):
        with self._stub(manifest_for(self.package, '9.9.9')):
            self.updater.check(force=True)
            self.updater.skip('9.9.9')
            data = self.updater.check(force=True)
        self.assertFalse(data['available'])
        self.assertTrue(data['skipped'])

    def test_network_failure_falls_back_to_last_result(self):
        with self._stub(manifest_for(self.package, '9.9.9')):
            self.updater.check(force=True)
        with patch('app_update.fetch_manifest', side_effect=RuntimeError('离线')):
            data = self.updater.check(force=True)
        self.assertTrue(data['available'])
        self.assertIn('离线', data['error'])

    def test_beta_channel_needs_opt_in(self):
        with self._stub(manifest_for(self.package, '9.9.9', channel='beta')):
            data = self.updater.check(force=True)
        self.assertFalse(data['available'])
        with self._stub(manifest_for(self.package, '9.9.9', channel='beta')):
            self.updater.set_beta(True)
            data = self.updater.check(force=True)
        self.assertTrue(data['available'])

    # ---------- 下载与解压 ----------

    def test_download_stages_whitelisted_files(self):
        pending = self._staged()
        self.assertEqual(pending['version'], '9.9.9')
        staged = Path(pending['staged'])
        self.assertEqual((staged / 'DevMux.exe').read_bytes(), b'new-binary-9.9.9')
        self.assertTrue((staged / 'plugin' / 'usb-relay-1.0.2.dmp').is_file())
        self.assertFalse((staged / '使用说明.md').exists())
        self.assertEqual(self.updater.status()['pending']['version'], '9.9.9')

    def test_download_reports_progress(self):
        seen = []
        manifest = manifest_for(self.package, '9.9.9')
        with self._stub(manifest):
            self.updater.check(force=True)
            self.updater.download(progress=lambda received, total: seen.append(received))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], self.package.stat().st_size)

    def test_bad_digest_leaves_nothing_behind(self):
        manifest = manifest_for(self.package, '9.9.9')
        manifest['assets']['windows']['sha256'] = '0' * 64
        with self._stub(manifest):
            self.updater.check(force=True)
            with self.assertRaises(RuntimeError):
                self.updater.download()
        self.assertIsNone(self.updater.status()['pending'])
        self.assertEqual(list((self.data / 'updates').rglob('*.part')), [])

    def test_ignores_non_whitelisted_entries(self):
        # 包里夹带脚本不能变成「写进安装目录的文件」，也不能让整包作废。
        self.package = build_zip(self.temp / 'extra.zip', '9.9.9', extra={'run.cmd': 'del /q *'})
        pending = self._staged()
        self.assertFalse((Path(pending['staged']) / 'run.cmd').exists())
        self.assertTrue((Path(pending['staged']) / 'DevMux.exe').is_file())

    def test_rejects_path_traversal(self):
        self.package = build_zip(self.temp / 'evil.zip', '9.9.9', extra={'../escape.txt': 'x'})
        with self.assertRaises(ValueError):
            self._staged()

    def test_rejects_version_mismatch(self):
        # 清单说 9.9.9，包里的构建信息却是 9.9.8：摘要对得上也不能装。
        self.package = build_zip(self.temp / 'DevMux-9.9.8-windows.zip', '9.9.8')
        manifest = manifest_for(self.package, '9.9.9')
        with self._stub(manifest):
            self.updater.check(force=True)
            with self.assertRaises(ValueError):
                self.updater.download()

    def test_pending_disappears_when_staged_removed(self):
        pending = self._staged()
        import shutil
        shutil.rmtree(Path(pending['staged']))
        self.assertIsNone(self.updater.status()['pending'])
        version_dir = self.data / 'updates' / '9.9.9'
        os.utime(version_dir, (0, 0))
        self.updater.cleanup()
        self.assertFalse(version_dir.exists())

    def test_cleanup_keeps_recent_downloads(self):
        # 多开时后启动的实例不能删掉另一个实例正在下载的包。
        target = self.data / 'updates' / '9.9.9'
        target.mkdir(parents=True)
        (target / 'package.part').write_bytes(b'x')
        self.updater.cleanup()
        self.assertTrue((target / 'package.part').exists())

    # ---------- 替换 ----------

    def test_swap_replaces_binary_and_keeps_backup(self):
        staged = self.temp / 'staged'
        staged.mkdir()
        (staged / 'DevMux.exe').write_bytes(b'new-binary')
        (staged / 'plugin').mkdir()
        (staged / 'plugin' / 'ebpf-1.0.1.dmp').write_bytes(b'dmp')
        with patch.object(update_apply, 'launch', return_value=True):
            result = update_apply.swap(staged, self.install, self.temp / 'result.json')
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.install / 'DevMux.exe').read_bytes(), b'new-binary')
        backups = list(self.install.glob('DevMux.exe.old-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'old-binary')
        self.assertTrue((self.install / 'plugin' / 'ebpf-1.0.1.dmp').is_file())
        saved = json.loads((self.temp / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'installed')

    def test_swap_rolls_back_when_new_binary_cannot_start(self):
        staged = self.temp / 'staged'
        staged.mkdir()
        (staged / 'DevMux.exe').write_bytes(b'broken')
        with patch.object(update_apply, 'launch', return_value=False):
            result = update_apply.swap(staged, self.install, self.temp / 'result.json')
        self.assertEqual(result['status'], 'rolled-back')
        self.assertEqual((self.install / 'DevMux.exe').read_bytes(), b'old-binary')
        self.assertEqual(list(self.install.glob('DevMux.exe.old-*')), [])

    def test_swap_waits_then_fails_when_still_locked(self):
        staged = self.temp / 'staged'
        staged.mkdir()
        (staged / 'DevMux.exe').write_bytes(b'new')
        with patch.object(update_apply, 'wait_unlocked', return_value=False):
            result = update_apply.swap(staged, self.install, self.temp / 'result.json')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('超时', result['error'])
        self.assertEqual((self.install / 'DevMux.exe').read_bytes(), b'old-binary')

    def test_environment_is_cleaned_for_new_instance(self):
        with patch.dict(os.environ, {'_PYI_APPLICATION_HOME_DIR': 'C:/Temp/_MEI1', '_MEIPASS2': 'x',
                                     'PATH': 'keep'}, clear=False):
            env = update_apply.clean_environment()
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR', env)
        self.assertNotIn('_MEIPASS2', env)
        self.assertEqual(env['PATH'], 'keep')

    def test_launch_updater_skips_in_source_mode(self):
        self._staged()
        self.updater.arm_restart()
        self.assertTrue(self.updater.restart_flag.exists())
        self.assertFalse(self.updater.launch_updater())
        self.assertFalse(self.updater.restart_flag.exists())

    def test_armed_restart_requires_downloaded_package(self):
        with self.assertRaises(ValueError):
            self.updater.arm_restart()

    def test_result_is_reported_once_after_ack(self):
        self.updater.consume_result()
        (self.data / 'updates').mkdir(parents=True, exist_ok=True)
        self.updater.result_path.write_text(json.dumps({'status': 'installed'}), encoding='utf-8')
        self.assertEqual(self.updater.consume_result()['status'], 'installed')
        self.assertFalse(self.updater.result_path.exists())
        self.assertFalse(self.updater.status()['result']['acknowledged'])
        self.updater.ack_result()
        self.assertTrue(self.updater.status()['result']['acknowledged'])


if __name__ == '__main__':
    unittest.main()
