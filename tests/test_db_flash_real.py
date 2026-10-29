"""真实子进程输出、取消和失败边界；假工具不连接任何设备。"""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import RealExecutor, DBFlashService, SPECS


class ProcessTests(unittest.TestCase):
    def setUp(self):
        runtime = ROOT / '.agent-runtime/db-flash'
        runtime.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='real-executor-', dir=runtime)
        self.root = Path(self.temp.name).resolve()
        self.tool = self.root / 'fake_tool.py'
        self.tool.write_text('''import os, sys, time
mode = sys.argv[1]
print('连续中文输出', flush=True)
print('标准错误输出', file=sys.stderr, flush=True)
if mode == 'partial':
    os.write(1, '中'.encode('utf-8')[:1]); time.sleep(.1)
    os.write(1, '中'.encode('utf-8')[1:]); time.sleep(.4)
elif mode == 'wait':
    time.sleep(20)
elif mode == 'fail':
    sys.exit(9)
elif mode == 'download':
    print('PC: END OF THIS USB LOAD!', flush=True)
''', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def executor(self, mode, timeout=5):
        tool = str(self.tool)
        class FakeTools(RealExecutor):
            def command(self, _step):
                return [sys.executable, '-B', '-u', tool, mode]
            def _target(self, *_args):
                self._serial = 'TEST'
        return FakeTools(timeout=timeout)

    def step(self, id='prepare-spl'):
        return {'id':id, 'label':'测试步骤', 'cwd':str(self.root), 'argv':('python', 'unused')}

    def test_streams_before_process_finishes_and_binary_safe(self):
        executor = self.executor('partial')
        events, errors = [], []
        def execute():
            try:
                executor.execute(self.step('prepare-spl'), threading.Event(), lambda text, data=None: events.append((text, data)))
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=execute)
        thread.start()
        end = time.monotonic() + 3
        while not any('连续' in text for text, _data in events) and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue(thread.is_alive(), '输出应在工具退出前到达')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        text = ''.join(t for t, _d in events)
        self.assertIn('标准错误输出', text)
        self.assertIn('中', text)
        self.assertNotIn('\ufffd', text)
        raw = b''.join(d for _t, d in events if d)
        self.assertIn('连续中文输出'.encode(), raw)
        self.assertIn('未报告', errors[0])

    def test_download_success_requires_completion_marker(self):
        for mode, success in [('download', True), ('partial', False), ('fail', False)]:
            executor = self.executor(mode)
            output = lambda *_args: None
            if success:
                self.assertTrue(executor.execute(self.step(), threading.Event(), output))
            else:
                with self.assertRaises(RuntimeError):
                    executor.execute(self.step(), threading.Event(), output)

    def test_cancel_and_timeout_stop_actual_child(self):
        cancel = threading.Event()
        entered = threading.Event()
        executor = self.executor('wait')
        result = []
        thread = threading.Thread(target=lambda: result.append(executor.execute(self.step(), cancel, lambda *_args: entered.set())))
        thread.start()
        self.assertTrue(entered.wait(2))
        cancel.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        with self.assertRaisesRegex(RuntimeError, '超时'):
            self.executor('wait', timeout=.2).execute(self.step(), threading.Event(), lambda *_args: None)

    def test_unique_device_and_serial_binding(self):
        executor = RealExecutor(device_timeout=.01)
        cancel = threading.Event()
        output = lambda *_args: None
        with patch('db_flash.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'A\tfastboot\nB\tfastboot\n', b'')):
            with self.assertRaisesRegex(ValueError, '多台'):
                executor._target(['fastboot'], self.step(), cancel, output)
            executor._target(['fastboot', '-s', 'B'], self.step(), cancel, output)
            self.assertEqual(executor._serial, 'B')
        with patch('db_flash.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'', b'')):
            with self.assertRaisesRegex(RuntimeError, '超时'):
                executor._target(['fastboot'], self.step(), cancel, output)

    def test_frozen_fastboot_finds_bundled_mke2fs(self):
        bundle = self.root / 'frozen'
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(bundle), create=True):
            env = RealExecutor._environment('format-data')
            self.assertEqual(env['PATH'].split(os.pathsep)[0], str(bundle / 'adb'))
            self.assertEqual(env['MKE2FS_CONFIG'], str(bundle / 'adb' / 'mke2fs.conf'))
            for step_id in ('prepare-spl', 'flash-system', 'erase-data', 'flash-data'):
                other_env = RealExecutor._environment(step_id)
                self.assertEqual(other_env.get('PATH'), os.environ.get('PATH'))
                self.assertEqual(other_env.get('MKE2FS_CONFIG'), os.environ.get('MKE2FS_CONFIG'))

    def test_real_service_progress_and_fail_closed(self):
        fixture = self.root / 'DB'
        for name in ['flashall.bat', 'flash_all.sh', 'scripts/download_image.py',
                     'images/u-boot/u-boot.bin'] + [s.default for s in SPECS]:
            file = fixture / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'fake image')
        before = {p.relative_to(fixture).as_posix(): p.read_bytes() for p in fixture.rglob('*') if p.is_file()}
        executor = self.executor('fail')
        # 测试只模拟命令选择，执行与流读取为真实进程。
        executor._target = lambda *_args: None
        service = DBFlashService(executor)
        popen = subprocess.Popen
        def fake_fastboot(argv, **kwargs):
            # 将已验证的 -s 设备选择从假 Python 工具参数中剥离，不改变真实流读取。
            self.assertEqual(argv[1], '-s')
            return popen([argv[0], *argv[3:]], **kwargs)
        with patch('db_flash.subprocess.Popen', side_effect=fake_fastboot):
            result = service.call('start', {'toolDir':str(fixture), 'selected':['system'], 'options':{'prepare':False}})
            self.assertTrue(result['success'], result)
            service._thread.join(5)
        self.assertFalse(service._thread.is_alive())
        job = service.status()
        self.assertFalse(job['simulation'])
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['completed'], 0)
        self.assertEqual(len([e for e in job['events'] if e['kind'] == 'step-start']), 1)
        binary = b''.join(base64.b64decode(e['data']) for e in job['events'] if 'data' in e)
        self.assertIn('标准错误输出'.encode(), binary)
        self.assertEqual(before, {p.relative_to(fixture).as_posix(): p.read_bytes() for p in fixture.rglob('*') if p.is_file()})
        service.close()


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProcessTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
