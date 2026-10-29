"""真实 PTY 输入/尺寸/退出，以及辅助进程异常不能带走主程序。"""
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pty_host import IsolatedPtyProcess, host_command
import adb_worker


def run():
    try:
        process = IsolatedPtyProcess.spawn([sys.executable, '-B', '-c',
            "import sys; print('READY',flush=True)\nfor line in sys.stdin: print('ECHO:'+line.strip(),flush=True)"])
        output, finished = [], threading.Event()
        def reader():
            try:
                while True:
                    output.append(process.read())
            except EOFError:
                finished.set()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            end = time.monotonic()+5
            while 'READY' not in ''.join(output) and time.monotonic()<end:
                time.sleep(.02)
            assert 'READY' in ''.join(output)
            process.setwinsize(30, 100)
            process.write('中文断线测试\r\n')
            end = time.monotonic()+5
            while 'ECHO:中文断线测试' not in ''.join(output) and time.monotonic()<end:
                time.sleep(.02)
            assert 'ECHO:中文断线测试' in ''.join(output), output
            # 模拟原生进程被终止；检查调用测试的主进程仍能创建下一终端。
            process._process.kill()
            assert finished.wait(4)
            process._thread.join(timeout=2)
            assert not process._thread.is_alive() and not process.isalive()
            assert process.exit_code != 0
        finally:
            process.terminate(force=True)
        ready, exited = threading.Event(), threading.Event()
        native_spawn = adb_worker.PtyProcess.spawn
        with patch.object(adb_worker.PtyProcess, 'spawn', side_effect=lambda *_: native_spawn(
                [sys.executable, '-B', '-c', "import time; print('SECOND',flush=True); time.sleep(30)"])), \
                patch.object(adb_worker, '_pty_child_handle', side_effect=AssertionError('隔离 PTY 不应额外申请终止句柄')):
            session = adb_worker.TerminalSession({'serial': 'after-crash'}, lambda *_: ready.set(), exited.set)
            assert session._child_handle is None
            assert ready.wait(5)
            session.close()
            assert exited.wait(3) and not session.is_alive()
        with patch.object(sys, 'frozen', True, create=True):
            command = host_command(['adb', 'shell'])
            assert command[:2] == [sys.executable, '--adb-pty-host']
        print('  [PASS] 隔离 PTY 中文输入、尺寸调整、异常退出、读取线程回收和下一终端恢复')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
