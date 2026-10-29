"""ADB 原生 PTY 隔离进程：主程序只接触管道与 JSON，不调用原生终端接口。"""
import base64
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def host_command(argv, cwd=None, dimensions=(24, 80)):
    payload = base64.b64encode(json.dumps({'argv': argv, 'cwd': cwd, 'dimensions': dimensions},
                                        ensure_ascii=False).encode('utf-8')).decode('ascii')
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--adb-pty-host', payload]
    return [sys.executable, '-B', str(Path(__file__).resolve()), '--adb-pty-host', payload]


class IsolatedPtyProcess:
    isolated = True

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80), **kwargs):
        return cls(argv, cwd, env, dimensions)

    def __init__(self, argv, cwd=None, env=None, dimensions=(24, 80)):
        self.diagnostic_log = ''
        self.native_pid = None
        self.runtime_pid = None
        errors = None
        try:
            directory = (env or os.environ).get('ADB_DIAG_DIR')
            if directory:
                self.diagnostic_log = str(Path(directory)/f'pty-{os.getpid()}-{time.monotonic_ns()}.log')
                errors = open(self.diagnostic_log, 'ab')
            self._process = subprocess.Popen(host_command(argv, cwd, dimensions),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors or subprocess.DEVNULL,
                env=env, creationflags=CREATE_NO_WINDOW)
        finally:
            if errors:
                errors.close()
        self.pid = self._process.pid
        self._input_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._output = queue.Queue(maxsize=256)
        self.error = ''
        self.exit_code = None
        self._thread = threading.Thread(target=self._receive, daemon=True, name='ADB-PTY-pipe')
        self._thread.start()
        if not self._ready.wait(15) or self.error:
            self.terminate(force=True)
            raise RuntimeError(self.error or 'PTY 子进程启动超时')

    def _receive(self):
        try:
            for raw in self._process.stdout:
                message = json.loads(raw.decode('utf-8'))
                kind = message.get('type')
                if kind == 'ready':
                    self.native_pid = message.get('nativePid')
                    self.runtime_pid = message.get('hostPid')
                    self._ready.set()
                elif kind == 'error':
                    self.error = str(message.get('output') or 'PTY 子进程异常')
                    self._ready.set()
                elif kind == 'data':
                    while not self._closed.is_set():
                        try:
                            self._output.put(str(message['data']), timeout=.1)
                            break
                        except queue.Full:
                            pass
        except Exception as exc:
            self.error = f'PTY 管道读取失败：{exc}'
        finally:
            self._process.wait()
            self.exit_code = self._process.returncode
            import crash_diagnostics
            root = str(getattr(sys, '_MEIPASS', ''))
            crash_diagnostics.mark('pty-host-ended', childPid=self.pid, exitCode=self.exit_code,
                error=self.error, extractionRoot=root, extractionExists=bool(root and Path(root).exists()))
            if not self._ready.is_set():
                self.error = self.error or f'PTY 子进程启动失败（退出码 {self.exit_code}）'
                self._ready.set()
            self._closed.set()
            for stream in (self._process.stdin, self._process.stdout):
                try:
                    stream.close()
                except Exception:
                    pass

    def read(self, size=1024):
        while True:
            try:
                return self._output.get(timeout=.1)
            except queue.Empty:
                if self._closed.is_set():
                    raise EOFError(f'PTY 隔离进程结束（PID {self.pid}，退出码 {self.exit_code}，原因 {self.error or "未返回异常说明"}）')

    def _send(self, message):
        with self._input_lock:
            if not self.isalive():
                raise EOFError('PTY 隔离进程已结束')
            self._process.stdin.write((json.dumps(message, ensure_ascii=False)+'\n').encode('utf-8'))
            self._process.stdin.flush()

    def write(self, data):
        self._send({'type': 'input', 'data': data})
        return len(data)

    def setwinsize(self, rows, cols):
        self._send({'type': 'resize', 'rows': rows, 'cols': cols})

    def isalive(self):
        return self._process.poll() is None and not self._closed.is_set()

    def terminate(self, force=False):
        if self._process.poll() is None:
            try:
                self._send({'type': 'close'})
                self._process.wait(timeout=1.5)
            except (OSError, EOFError, subprocess.TimeoutExpired):
                if self._process.poll() is None:
                    self._process.kill()
                    self._process.wait(timeout=3)
        self._closed.set()
        self._thread.join(timeout=2)
        return self._process.poll() is not None

    def close(self, force=False):
        return self.terminate(force)


def _stdio(name, handle_id, mode):
    """windowed EXE 的 sys.stdin/stdout 为 None，重新包装继承的管道句柄。"""
    stream = getattr(sys, name)
    if stream is not None:
        stream.reconfigure(encoding='utf-8', write_through=True)
        return stream
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(handle_id & 0xffffffff)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise RuntimeError('PTY 辅助进程缺少管道句柄')
    flags = os.O_BINARY | (os.O_RDONLY if mode == 'rb' else os.O_WRONLY)
    return io.TextIOWrapper(os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, buffering=0),
                           encoding='utf-8', write_through=True)


def run_host(payload):
    # 只有该隔离进程加载并操作 winpty；原生退出不会影响桌面主进程。
    errors = _stdio('stderr', -12, 'wb')
    sys.stderr = errors
    def trace(message):
        errors.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] [PTY-HOST PID {os.getpid()} / {threading.current_thread().name}] {message}\n')
        errors.flush()
    try:
        import faulthandler
        faulthandler.enable(file=errors, all_threads=True)
    except Exception as exc:
        trace(f'原生堆栈捕获初始化失败：{exc}')
    trace(f'进入辅助进程，父 PID {os.getppid()}，冻结版 {bool(getattr(sys,"frozen",False))}')
    trace(f'打包运行环境：MEIPASS={getattr(sys,"_MEIPASS","")}，进程层级={os.environ.get("_PYI_PARENT_PROCESS_LEVEL","")}')
    input_stream = _stdio('stdin', -10, 'rb')
    output_stream = _stdio('stdout', -11, 'wb')
    output_lock = threading.Lock()
    def emit(kind, **values):
        with output_lock:
            output_stream.write(json.dumps({'type': kind, **values}, ensure_ascii=False)+'\n')
            output_stream.flush()
    try:
        from winpty import PtyProcess
        config = json.loads(base64.b64decode(payload).decode('utf-8'))
        trace('开始调用 winpty.spawn')
        process = PtyProcess.spawn(config['argv'], cwd=config.get('cwd'),
                                   dimensions=tuple(config['dimensions']))
        trace(f'winpty.spawn 返回，原生子进程 PID {process.pid}')
        emit('ready', nativePid=process.pid, hostPid=os.getpid())
        def output():
            try:
                while True:
                    data = process.read()
                    if data:
                        emit('data', data=data)
            except EOFError:
                trace('原生读取 EOF')
            except Exception as exc:
                trace(f'原生读取异常：{type(exc).__name__}: {exc}')
                emit('error', output=f'原生 PTY 读取结束：{exc}')
            # 避免原生对象在解释器退出时再次析构；资源由操作系统回收。
            trace('输出线程准备退出辅助进程，代码 0')
            os._exit(0)
        threading.Thread(target=output, daemon=True).start()
        for raw in input_stream:
            message = json.loads(raw)
            kind = message.get('type')
            if kind == 'input':
                trace(f'准备写入，字符数 {len(message["data"])}')
                process.write(message['data'])
            elif kind == 'resize':
                trace(f'调整尺寸 {message["rows"]} x {message["cols"]}')
                process.setwinsize(int(message['rows']), int(message['cols']))
            elif kind == 'close':
                trace('收到 close，准备调用原生 terminate')
                process.terminate(force=True)
                trace('原生 terminate 返回，准备退出代码 0')
                os._exit(0)
        trace('输入管道结束，准备调用原生 terminate')
        process.terminate(force=True)
    except Exception as exc:
        trace(f'辅助进程异常：{type(exc).__name__}: {exc}')
        emit('error', output=f'PTY 辅助进程失败：{type(exc).__name__}: {exc}')
    trace('准备退出代码 1')
    os._exit(1)


if __name__ == '__main__':
    run_host(sys.argv[-1])
