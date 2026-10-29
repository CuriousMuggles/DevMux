"""断电/拔线清理不向失效 PTY 写入，不重复通知或误杀复用 PID。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import adb_worker
import server


def run():
    try:
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        notifications, calls = [], []
        class DeadPipe:
            pid = 12345
            def read(self):
                entered.set()
                release.wait(3)
                raise EOFError()
            def write(self, data):
                raise AssertionError('离线清理不能写入失效 PTY')
            def terminate(self, **kwargs):
                raise AssertionError('Windows 清理不能调用 PTY terminate')
        def cleanup(handle, terminate=False):
            calls.append((handle, terminate))
            release.set()
        with patch.object(adb_worker.PtyProcess, 'spawn', return_value=DeadPipe()), \
             patch.object(adb_worker, '_pty_child_handle', return_value='saved-handle'), \
             patch.object(adb_worker, '_release_pty_child', side_effect=cleanup):
            session = adb_worker.TerminalSession({'serial': 'power-cut'}, lambda *_: None,
                lambda: (notifications.append('exit'), ended.set()))
            assert entered.wait(1)
            session.close()
            session.close()
            assert ended.wait(1)
            assert notifications == ['exit']
            assert calls[0] == ('saved-handle', True)
            assert calls.count(('saved-handle', True)) == 1
            assert not session.is_alive()
        assert adb_worker._pty_child_handle(os.getpid()) is None
        # 独立子进程验证 Win32 句柄清理，真实终止后主进程必须仍正常执行。
        if os.name == 'nt':
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                     creationflags=adb_worker.CREATE_NO_WINDOW)
            try:
                handle = adb_worker._pty_child_handle(child.pid)
                assert handle
                adb_worker._release_pty_child(handle, terminate=True)
                child.wait(timeout=3)
                assert child.returncode is not None
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
            # 使用真实 ConPTY，让 ADB shell 等价子进程在关闭时断线。
            spawn = adb_worker.PtyProcess.spawn
            for index in range(3):
                output_ready = threading.Event()
                ended = threading.Event()
                processes = []
                def real_spawn(_command):
                    process = spawn([sys.executable, '-B', '-c', 'import time; print("ready", flush=True); time.sleep(30)'])
                    processes.append(process)
                    return process
                with patch.object(adb_worker.PtyProcess, 'spawn', side_effect=real_spawn):
                    session = adb_worker.TerminalSession({'serial': f'native-disconnect-{index}'},
                        lambda *_: output_ready.set(), ended.set)
                    try:
                        assert output_ready.wait(4), '真实 PTY 未输出'
                        session.close()
                        assert ended.wait(2) and not processes[0].isalive()
                        processes[0]._thread.join(timeout=2)
                        assert not processes[0]._thread.is_alive(), '原生 PTY 读取线程未释放'
                    finally:
                        session.close()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(Path(temporary) / 'DevMux.exe')):
            assert server._crash_dump_path() == str(Path(temporary) / 'crash_dump.txt')
        print('  [PASS] 断线 PTY 不写入，按保存句柄清理子进程并回收读取线程，退出仅通知一次，冻结版崩溃记录持久化')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
