"""PowerShell PATH缺失定位、能力一致性、真实任务与交互；不连接实体设备。"""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import windows_executor as executor


def wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.05)
    return False


class DiscoveryTests(unittest.TestCase):
    def test_pty_output_tail_without_blocking_eof_probe(self):
        from powershell_pty import PowerShellPtyProcess
        raw = Mock(pid=123)
        raw.read.side_effect = ['中文尾部', '']
        raw.isalive.return_value = False
        raw.iseof.side_effect = AssertionError('不能调用阻塞EOF探测')
        process = PowerShellPtyProcess(raw)
        self.assertEqual(process.read(2), '中文')
        self.assertEqual(process.read(2), '尾部')
        with self.assertRaises(EOFError):
            process.read()
        raw.iseof.assert_not_called()
        raw.read.assert_called_with(blocking=False)
        process.close(force=True)
        process.close(force=True)
        raw.cancel_io.assert_called_once()

    def test_pty_idle_read_waits_and_close_wakes_reader(self):
        from powershell_pty import PowerShellPtyProcess
        raw = Mock(pid=123)
        raw.read.return_value = ''
        raw.isalive.return_value = True
        process = PowerShellPtyProcess(raw)
        with patch.object(process._reader_stop, 'wait') as wait_idle:
            self.assertEqual(process.read(), '')
            wait_idle.assert_called_once_with(.01)
        with patch.object(process, 'terminate', return_value=True):
            process.close(force=True)
        with self.assertRaises(EOFError):
            process.read()

    def test_real_continuous_output(self):
        service = executor.WindowsService(self.root/'continuous')
        self.addCleanup(service.close)
        opened = service.session_open({}, 'continuous')
        args = {'sessionId': opened['sessionId']}
        def text():
            return service.session_action('session_read', args, 'continuous')['text']
        self.assertTrue(wait(lambda: 'PS ' in text()))
        for value in range(12):
            service.session_action('session_write', {**args, 'data':
                "[Console]::WriteLine('CONTINUOUS_'+("+str(value)+"+100))\r"}, 'continuous')
            self.assertTrue(wait(lambda: 'CONTINUOUS_'+str(value+100) in text()), text())
        # Ctrl+C仍由真实xterm UI检查；原生管道未实现ConPTY的终端查询应答。
        process = service.sessions[args['sessionId']]['proc']
        service.session_action('session_close', args, 'continuous')
        self.assertFalse(process.pty.isalive())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='powershell-discovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def install(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fake-executable')
        return path

    def test_path_precedence(self):
        with patch.object(executor.shutil, 'which', return_value='custom/powershell.exe'), \
             patch.object(executor, '_windows_directory', side_effect=AssertionError('不应检查标准目录')):
            self.assertEqual(executor.powershell(), 'custom/powershell.exe')

    def test_explicit_extension(self):
        with patch.object(executor.shutil, 'which', side_effect=lambda name: 'custom/powershell.exe' if name.endswith('.exe') else None):
            self.assertEqual(executor.powershell(), 'custom/powershell.exe')

    def test_standard_directories(self):
        for part in ('System32', 'Sysnative', 'SysWOW64'):
            executable = self.install(part, 'WindowsPowerShell', 'v1.0', 'powershell.exe')
            with patch.object(executor.shutil, 'which', return_value=None), \
                 patch.object(executor, '_windows_directory', return_value=str(self.root)):
                self.assertEqual(executor.powershell(), str(executable))
            executable.unlink()

    def test_pwsh_standard_path_and_no_edition_switch(self):
        executable = self.install('program-files', 'PowerShell', '7', 'pwsh.exe')
        with patch.object(executor.shutil, 'which', return_value=None), \
             patch.object(executor, '_windows_directory', return_value=str(self.root)), \
             patch.dict(os.environ, {'ProgramFiles':str(self.root/'program-files'), 'ProgramW6432':'', 'ProgramFiles(x86)':''}):
            self.assertEqual(executor.powershell('pwsh'), str(executable))
            with self.assertRaisesRegex(RuntimeError, '未找到可用的 powershell.exe'):
                executor.powershell()

    def test_missing_shell_reports_lookup_failure(self):
        with patch.object(executor.shutil, 'which', return_value=None), \
             patch.object(executor, '_windows_directory', return_value=str(self.root)):
            with self.assertRaisesRegex(RuntimeError, 'PATH 和标准安装目录') as error:
                executor.powershell()
            self.assertNotIn('未安装', str(error.exception))
        with self.assertRaises(ValueError):
            executor.powershell('cmd')

    def test_capabilities_agree_with_launch_resolver(self):
        executable = self.install('System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        service = executor.WindowsService(self.root/'jobs')
        self.addCleanup(service.close)
        with patch.object(executor.shutil, 'which', return_value=None), \
             patch.object(executor, '_windows_directory', return_value=str(self.root)), \
             patch.dict(os.environ, {'ProgramFiles':'', 'ProgramW6432':'', 'ProgramFiles(x86)':''}):
            self.assertEqual(service.call('capabilities', {}, 'test')['shells'], ['powershell'])
            self.assertEqual(executor.powershell(), str(executable))
            executable.unlink()
            self.assertEqual(service.call('capabilities', {}, 'test')['shells'], [])

    def test_real_job_and_pty_with_empty_path(self):
        service = executor.WindowsService(self.root/'jobs')
        self.addCleanup(service.close)
        with patch.dict(os.environ, {'PATH':'', 'PATHEXT':''}):
            self.assertIsNone(executor.shutil.which('powershell'))
            self.assertIn('powershell', service.call('capabilities', {}, 'test')['shells'])
            job = service.start({'script':"[Console]::WriteLine('LOOKUP_JOB_OK')"}, 'test')
            self.assertTrue(wait(lambda: service.status(job['jobId'], 'test')['state'] in executor.FINAL))
            self.assertEqual(service.status(job['jobId'], 'test')['state'], 'succeeded')
            self.assertIn('LOOKUP_JOB_OK', service.output({'jobId':job['jobId']}, 'test')['text'])
            opened = service.session_open({}, 'test')
            args = {'sessionId':opened['sessionId']}
            service.session_action('session_write', {**args,'data':"$lookupValue=600; [Console]::WriteLine('LOOKUP_PTY='+($lookupValue+1))\r"}, 'test')
            self.assertTrue(wait(lambda: 'LOOKUP_PTY=601' in service.session_action('session_read',args,'test')['text']))
            service.session_action('session_close', args, 'test')
            self.assertEqual(service.sessions, {})


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DiscoveryTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
