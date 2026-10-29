"""识别、默认 ALL、独立烧写、失败与取消回归；禁止调用真实设备工具。"""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import SPECS, DBFlashService, SimulationExecutor, build_plan, catalog, recognize


class FlashTests(unittest.TestCase):
    def setUp(self):
        runtime = ROOT / '.agent-runtime/db-flash'
        runtime.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='test-模拟-', dir=runtime)
        self.root = (Path(self.temporary.name) / 'DB 工具 中文').resolve()
        for path in ['flashall.bat', 'flash_all.sh', 'scripts/download_image.py',
                     'images/u-boot/u-boot.bin'] + [s.default for s in SPECS]:
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'fake fixture only')
        self.before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.args = {'toolDir': str(self.root)}

    def tearDown(self):
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.before, after, 'DB 脚本、工具和默认镜像必须保持只读')
        self.temporary.cleanup()

    def image(self, name):
        file = Path(self.temporary.name) / '外部 镜像' / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b'external fixture')
        return str(file.resolve())

    def test_recognition(self):
        for name, id in [('P500_SYSTEM_2026.IMG', 'system'), ('x_preboot_debug.itb', 'preboot'),
                         ('x_boot_2026.itb', 'boot'), ('a_spl_im_signed_版本.img', 'spl'),
                         ('new_userdata.img', 'data'), ('rootfs_debug.img', 'rootfs'), ('x_gpt.bin', 'gpt')]:
            result = recognize(name)
            self.assertEqual(result['state'], 'matched')
            self.assertEqual(result['matches'], [id])
        self.assertEqual(recognize('rootfs_system.img')['state'], 'ambiguous')
        self.assertEqual(recognize('system.txt')['state'], 'invalid')
        self.assertEqual(recognize('u-boot.bin')['state'], 'invalid')

    def test_default_all_matches_script_order(self):
        model = catalog(str(self.root))
        self.assertTrue(model['toolValid'])
        self.assertTrue(all(i['checked'] for i in model['items']))
        ids = [s['id'] for s in build_plan(self.args)['steps']]
        self.assertEqual(ids, ['prepare-spl', 'prepare-uboot', 'verify-mode', 'flash-mmc0boot0',
            'flash-mmc0boot1', 'flash-gpt', 'resize-gpt', 'erase-env', 'flash-preboot',
            'flash-boot', 'flash-rootfs', 'flash-system', 'erase-data', 'reboot'])
        self.assertNotIn('flash-data', ids)

    def test_single_external_direct_path(self):
        file = self.image('p500_system_新版.img')
        args = {**self.args, 'files': [file], 'serial': 'TEST-SERIAL'}
        model = catalog(str(self.root), [file])
        self.assertEqual([i['id'] for i in model['items'] if i['checked']], ['system'])
        plan = build_plan(args)
        self.assertEqual([s['id'] for s in plan['steps']], ['prepare-spl', 'prepare-uboot', 'verify-mode', 'flash-system'])
        command = plan['steps'][-1]['argv']
        self.assertEqual(command, ('fastboot', '-s', 'TEST-SERIAL', 'flash', 'system', file))
        self.assertFalse((self.root / Path(file).name).exists())

    def test_manual_uncheck_and_supplement_default(self):
        file = self.image('new_system.img')
        args = {**self.args, 'files': [file], 'selected': ['rootfs', 'system'],
                'options': {'prepare': False, 'eraseEnv': False, 'reboot': False}}
        plan = build_plan(args)
        self.assertEqual([s['id'] for s in plan['steps']], ['verify-mode', 'flash-rootfs', 'flash-system'])
        self.assertEqual(plan['steps'][1]['image'], str(self.root / 'images/rootfs.img'))
        args['selected'] = ['rootfs']
        self.assertNotIn('flash-system', [s['id'] for s in build_plan(args)['steps']])
        args['selected'] = []
        with self.assertRaises(ValueError):
            build_plan(args)

    def test_duplicates_and_ambiguous_manual_resolution(self):
        a = self.image('system_A.img')
        b = self.image('system_B.img')
        args = {**self.args, 'files': [a, b]}
        with self.assertRaisesRegex(ValueError, '多个候选'):
            build_plan(args)
        args['overrides'] = {'system': b}
        self.assertEqual(build_plan(args)['steps'][-1]['image'], b)
        unknown = self.image('other.img')
        args = {**self.args, 'files': [unknown], 'selected': ['rootfs'], 'overrides': {'rootfs': unknown},
                'options': {'prepare': False}}
        self.assertEqual(build_plan(args)['steps'][-1]['image'], unknown)

    def test_userdata_flash_and_options(self):
        file = self.image('userdata_custom.img')
        plan = build_plan({**self.args, 'files': [file]})
        ids = [s['id'] for s in plan['steps']]
        self.assertIn('flash-data', ids)
        self.assertNotIn('erase-data', ids)
        args = {**self.args, 'selected': ['system'], 'options': {'prepare': False, 'eraseEnv': True, 'reboot': True}}
        self.assertEqual([s['id'] for s in build_plan(args)['steps']], ['verify-mode', 'erase-env', 'flash-system', 'reboot'])

    def test_missing_disabled_file_does_not_block(self):
        file = self.image('system_external.img')
        args = {**self.args, 'selected': ['system'], 'overrides': {'system': file},
                'options': {'prepare': False}}
        (self.root / 'images/boot.itb').unlink()
        self.before.pop('images/boot.itb')
        self.assertEqual(build_plan(args)['steps'][-1]['image'], file)
        args['selected'] = ['boot']
        with self.assertRaises(ValueError):
            build_plan(args)
        args['selected'] = ['system']
        args['overrides'] = {'system': self.image('system.txt')}
        with self.assertRaises(ValueError):
            build_plan(args)

    def test_input_validation_and_fixed_order(self):
        plan = build_plan({**self.args, 'selected': ['system', 'spl', 'gpt'], 'options': {'prepare': False}})
        self.assertEqual([s['id'] for s in plan['steps']], ['verify-mode', 'flash-mmc0boot0', 'flash-mmc0boot1',
                                                        'flash-gpt', 'resize-gpt', 'flash-system'])
        for changes in [{'selected': ['evil']}, {'selected': ['spl', 'spl']},
                        {'options': {'reboot': 'false'}}, {'serial': 'a b'}, {'files': 'file.img'},
                        {'overrides': {'bad': 'x'}}, {'selected': [None]}]:
            with self.assertRaises(ValueError):
                build_plan({**self.args, **changes})

    def wait(self, service):
        service._thread.join(3)
        self.assertFalse(service._thread.is_alive())
        return service.status()

    def test_success_failure_and_readonly(self):
        args = {**self.args, 'selected': ['system'], 'options': {'prepare': False}}
        for failure in ('', 'verify-mode'):
            executor = SimulationExecutor(delay=0, fail_step=failure)
            events = []
            service = DBFlashService(executor, events.append)
            with patch('subprocess.run', side_effect=AssertionError('禁止真实执行')), patch('subprocess.Popen', side_effect=AssertionError('禁止真实执行')):
                result = service.call('start', args)
                self.assertTrue(result['success'])
                job = self.wait(service)
            self.assertEqual(job['state'], 'failed' if failure else 'succeeded')
            self.assertEqual(len(executor.calls), 1 if failure else 2)
            self.assertEqual([e['sequence'] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(job['completed'], 0 if failure else 2)
            self.assertFalse(service.call('cancel', {'jobId': job['jobId']})['success'])

    def test_cancellation_and_single_job(self):
        entered = threading.Event()

        class SlowExecutor(SimulationExecutor):
            def execute(self, step, cancel, output):
                self.calls.append(copy.deepcopy(step))
                entered.set()
                cancel.wait(2)
                return not cancel.is_set()

        executor = SlowExecutor()
        service = DBFlashService(executor)
        args = {**self.args, 'selected': ['system'], 'options': {'prepare': False}}
        result = service.call('start', args)
        self.assertTrue(entered.wait(1))
        self.assertFalse(service.call('start', args)['success'])
        self.assertFalse(service.call('cancel', {'jobId': 'wrong'})['success'])
        self.assertTrue(service.call('cancel', {'jobId': result['data']['jobId']})['success'])
        self.assertEqual(self.wait(service)['state'], 'cancelled')
        self.assertEqual(len(executor.calls), 1)
        self.assertFalse(service.call('status', {'jobId': 'wrong'})['success'])

    def test_close_cancels_and_rejects_new_job(self):
        entered = threading.Event()

        class WaitingExecutor(SimulationExecutor):
            def execute(self, step, cancel, output):
                entered.set()
                cancel.wait(2)
                return not cancel.is_set()

        service = DBFlashService(WaitingExecutor())
        args = {**self.args, 'selected': ['system'], 'options': {'prepare': False}}
        self.assertTrue(service.call('start', args)['success'])
        self.assertTrue(entered.wait(1))
        service.close()
        self.assertFalse(service._thread.is_alive())
        self.assertEqual(service.status()['state'], 'cancelled')
        self.assertFalse(service.call('start', args)['success'])

    def test_service_default_is_real_and_rejects_arbitrary_commands(self):
        with self.assertRaises(ValueError):
            DBFlashService(object())
        service = DBFlashService()
        self.assertFalse(service.executor.simulation)
        self.assertFalse(service.call('execute-command', {'argv': ['fastboot']})['success'])
        self.assertFalse(service.call('plan', [])['success'])


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FlashTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
