"""退出观察器、阶段快照、继电器子进程日志与 PTY 原生 stderr 的针对性验证。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import crash_diagnostics as diagnostics
from usb_relay import USBRelayService
from pty_host import IsolatedPtyProcess


def run():
    try:
        # 观察器以明文 .ps1 经 -File 启动，不得再使用 -EncodedCommand；
        # 退出码捕获保留 Add-Type P/Invoke（读取外部进程退出码的唯一可靠途径）。
        assert 'Add-Type' in diagnostics.WATCH_SCRIPT
        assert 'EncodedCommand' not in diagnostics.WATCH_SCRIPT
        spawn_argv = {}
        with tempfile.TemporaryDirectory() as td, patch('subprocess.Popen') as popen:
            diagnostics.start_observer(424242, Path(td), Path(td))
            spawn_argv['argv'] = list(popen.call_args.args[0])
        assert '-File' in spawn_argv['argv'], spawn_argv['argv']
        assert '-EncodedCommand' not in spawn_argv['argv'], spawn_argv['argv']
        with tempfile.TemporaryDirectory(prefix='adb-diag-test-') as temporary:
            root = Path(temporary)
            # 默认只保存日志，不启动脚本观察器；显式诊断开关才启用。
            import adb_worker
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root), 'ADB_MANAGER_EXIT_OBSERVER': '0'}), \
                    patch.object(diagnostics, '_directory', None), patch.object(diagnostics, '_state', {}), \
                    patch.object(adb_worker, 'DIAGNOSTIC_LOG_FILE', None), \
                    patch.object(diagnostics, 'start_observer') as observer:
                directory = diagnostics.initialize(lambda *args: None, root)
                assert directory is not None
                observer.assert_not_called()
                assert not json.loads((directory/'run.json').read_text(encoding='utf-8'))['exitObserverEnabled']
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root), 'ADB_MANAGER_EXIT_OBSERVER': '1'}), \
                    patch.object(diagnostics, '_directory', None), patch.object(diagnostics, '_state', {}), \
                    patch.object(adb_worker, 'DIAGNOSTIC_LOG_FILE', None), \
                    patch.object(diagnostics, 'start_observer', return_value=(SimpleNamespace(pid=123), 'test')) as observer:
                directory = diagnostics.initialize(lambda *args: None, root)
                observer.assert_called_once()
                assert json.loads((directory/'run.json').read_text(encoding='utf-8'))['exitObserverEnabled']
            with patch.object(diagnostics, '_directory', root), patch.object(diagnostics, '_state', {}):
                logs = []
                service = USBRelayService(root/'config.json', log=lambda *items: logs.append(' '.join(items)))
                result = service._run_child([sys.executable, '-B', '-c', "import sys;print('result');sys.stderr.write('diagnostic-marker')"],
                    timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                assert result.returncode == 0 and b'result' in result.stdout
                assert any('子进程启动 PID' in line for line in logs)
                assert any('diagnostic-marker' in line for line in logs)
                state = json.loads((root/'phases.json').read_text(encoding='utf-8'))
                assert any(value['stage']=='usb-child-returned' for value in state.values())
                try:
                    service._run_child([sys.executable, '-B', '-c', 'import time;time.sleep(20)'], timeout=.1,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    raise AssertionError('未触发超时')
                except subprocess.TimeoutExpired:
                    pass
                assert any('子进程超时 PID' in line for line in logs)
            with patch.dict(os.environ, {'ADB_DIAG_DIR': str(root)}):
                p = IsolatedPtyProcess.spawn([sys.executable, '-B', '-c', "import time;print('ready',flush=True);time.sleep(20)"])
                try:
                    assert p.native_pid and p.runtime_pid
                    assert p.read()
                finally:
                    p.terminate(force=True)
                trace = Path(p.diagnostic_log).read_text(encoding='utf-8')
                assert 'winpty.spawn 返回' in trace and '收到 close' in trace and '父 PID' in trace
            if os.name == 'nt':
                local = root/'run-test'; local.mkdir()
                child = subprocess.Popen([sys.executable, '-B', '-c', 'import os,time;time.sleep(4);os._exit(7)'],
                    creationflags=subprocess.CREATE_NO_WINDOW)
                (local/'run.json').write_text(json.dumps({'revision':diagnostics.REVISION,'runId':local.name,
                    'pid':child.pid,'exe':sys.executable,'meipass':''}),encoding='utf-8')
                observer, mode = diagnostics.start_observer(child.pid, local, root/'export')
                try:
                    child.wait(timeout=8)
                    observer.wait(timeout=20)
                    report = json.loads((local/'exit-report.json').read_text(encoding='utf-8'))
                    assert report['state']=='exited' and report['exitCode']==7 and report['exitCodeHex']=='0x00000007', report
                    exported = root/'export'/local.name/'exit-report.json'
                    assert exported.is_file()
                    assert json.loads(exported.read_text(encoding='utf-8'))['exitCode']==7
                finally:
                    if child.poll() is None: child.kill();child.wait(timeout=3)
                    if observer.poll() is None: observer.kill();observer.wait(timeout=3)
        print('  [PASS] 诊断阶段快照、USB 子进程 PID/耗时/stderr/超时、PTY 原生日志、独立退出码捕获及证据导出')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
