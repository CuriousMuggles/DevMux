"""
ADB Worker — device polling + ConPTY terminal lifecycle.
Runs in the main thread (coordinated by server.py via asyncio loop executor).
"""
import subprocess
import threading
import time
import os
import sys
import traceback
import ctypes
import socket
from ctypes import wintypes
from typing import Callable, Dict, List, Optional

# Suppress console windows for child processes when running as a GUI (windowed) app
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0

try:
    import winpty
    from pty_host import IsolatedPtyProcess as PtyProcess
    _HAS_WINPTY = True
except ImportError:
    PtyProcess = None
    _HAS_WINPTY = False


def _resolve_adb_path() -> str:
    """优先使用环境变量；冻结版则使用随程序打包的 platform-tools。"""
    override = os.environ.get('ADB_PATH')
    if override:
        return override
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, 'adb', 'adb.exe')
        if os.path.isfile(bundled):
            # ADB server 可在窗口关闭后常驻，不能占用本次 _MEI 解包目录。
            from adb_runtime import persistent_adb_path
            return persistent_adb_path(os.path.dirname(bundled))
    return 'adb'


ADB_PATH = _resolve_adb_path()
POLL_INTERVAL = 2.0       # seconds
CONFIRM_COUNT = 3         # debounce offline count
POLL_STOP_JOIN_TIMEOUT = 3.0
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)), 'devmux.log')
DIAGNOSTIC_LOG_FILE = None
LOG_FILE_LOCK = threading.RLock()

# ADB server 共用本机 5037 端口。Android Studio 或旧实例遗留的异常服务可能
# 重置连接，令所有 `adb devices` 调用超时；恢复必须限频，避免多个轮询周期
# 持续重启用户的 ADB 服务。
_ADB_RECOVERY_LOCK = threading.Lock()
_last_adb_recovery = 0.0
ADB_RECOVERY_INTERVAL = 30.0


def log(category: str, msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [{category}] [PID {os.getpid()} / {threading.current_thread().name}] {msg}'
    # Windows 控制台可能仍使用 GBK；日志里出现 ✓、中文等字符时，
    # 控制台输出不能反过来中断 PTY 读取线程。文件日志始终保留 UTF-8 原文。
    try:
        print(line)
    except UnicodeEncodeError:
        try:
            print(line.encode('ascii', errors='backslashreplace').decode('ascii'))
        except Exception:
            pass
    except Exception:
        pass
    if DIAGNOSTIC_LOG_FILE:
        try:
            with open(DIAGNOSTIC_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass
    try:
        with LOG_FILE_LOCK:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def read_log_tail(limit=500):
    """与日志写入共用持久路径和锁，冻结版不会读临时解包目录。"""
    from collections import deque
    with LOG_FILE_LOCK:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as stream:
            return '\n'.join(line.rstrip('\r\n') for line in deque(stream, maxlen=limit))


def clear_log_file():
    """只清本实例日志；失败由调用方报告，不能伪报成功。"""
    with LOG_FILE_LOCK:
        with open(LOG_FILE, 'w', encoding='utf-8'):
            pass


def _run(args: List[str], timeout=5) -> Optional[str]:
    """运行 ADB 子命令；成功时返回 stdout，失败时返回 None。"""
    try:
        result = subprocess.run(
            [ADB_PATH] + args,
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip().replace('\n', ' ')
            log('ADB', f'run {args} exited {result.returncode}: {detail[:500]}')
            return None
        return result.stdout or ''
    except subprocess.TimeoutExpired as e:
        detail = (e.stderr or e.stdout or '')
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        log('ADB', f'run {args} timed out after {timeout}s: {str(detail).strip()[:500]}')
        return None
    except Exception as e:
        log('ADB', f'run {args} failed: {e}')
        return None


def _recover_adb_server():
    """在设备探测连续失败后，限频重建本机 ADB server。"""
    global _last_adb_recovery
    with _ADB_RECOVERY_LOCK:
        now = time.monotonic()
        if now - _last_adb_recovery < ADB_RECOVERY_INTERVAL:
            return
        _last_adb_recovery = now

    log('ADB', '设备探测失败，尝试重建本机 ADB server')
    for args, timeout in ((['kill-server'], 5), (['start-server'], 10)):
        _run(args, timeout=timeout)


def get_all_devices() -> List[dict]:
    """
    Fetch all devices.
    First try `adb devices -l` for transport_id.
    Fallback to `adb devices` if -l fails.
    """
    out = _run(['devices', '-l'], timeout=8)
    devices = []
    for line in (out or '').splitlines():
        line = line.strip()
        if not line or line.startswith('List'):
            continue
        # Try to parse transport_id
        m = None
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            state = parts[1]
            transport_id = None
            for p in parts[2:]:
                if p.startswith('transport_id:'):
                    try:
                        transport_id = int(p.split(':', 1)[1])
                    except ValueError:
                        pass
            devices.append({
                'serial': serial,
                'state': state,
                'transportId': transport_id,
            })
    if devices:
        return devices

    # Fallback: adb devices (no -l)
    fallback = _run(['devices'], timeout=8)
    if out is None and fallback is None:
        _recover_adb_server()
        return devices

    for line in (fallback or '').splitlines():
        line = line.strip()
        if not line or line.startswith('List'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({
                'serial': parts[0],
                'state': parts[1],
                'transportId': None,
            })
    return devices


def make_key(dev: dict) -> str:
    tid = dev.get('transportId')
    if tid is not None:
        return f"{dev['serial']}:tid={tid}"
    return dev['serial']


def _pty_child_handle(pid):
    """在创建时保留子进程句柄，避免离线清理时 PID 已被复用。"""
    if os.name != 'nt' or not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    return kernel.OpenProcess(0x101001, False, pid) or None


def _release_pty_child(handle, terminate=False):
    """仅结束已保存句柄指向的子进程，不向断线 PTY 写入退出命令。"""
    if not handle:
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    try:
        if terminate and kernel.WaitForSingleObject(handle, 0) == 258:
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.WaitForSingleObject(handle, 1000)
    finally:
        kernel.CloseHandle(handle)


class TerminalSession:
    """ADB shell over winpty — provides a real PTY so the device-side shell
    uses line-buffered I/O with proper echo and terminal control."""

    def __init__(self, device: dict, on_output: Callable[[bytes], None], on_exit: Callable[[], None]):
        self.device = device
        self.key = make_key(device)
        self.on_output = on_output
        self.on_exit = on_exit
        self.proc: Optional[PtyProcess] = None
        self._lock = threading.Lock()
        self._alive = True
        self._child_handle = None
        self._exit_notified = False
        self._spawn()

    def _spawn(self):
        dev = self.device
        if dev.get('transportId') is not None:
            cmdline = f'{ADB_PATH} -t {dev["transportId"]} shell'
        else:
            cmdline = f'{ADB_PATH} -s {dev["serial"]} shell'

        spawn_started = time.monotonic()
        log('PTY', f'spawn {self.key}: {cmdline}')
        try:
            self.proc = PtyProcess.spawn(cmdline)
            # 隔离进程由 Popen 已保存的句柄管理，不重复申请终止权限。
            if not getattr(self.proc, 'isolated', False):
                self._child_handle = _pty_child_handle(getattr(self.proc, 'pid', None))
        except BaseException as e:
            log('PTY', f'spawn failed {self.key}: {e}')
            import traceback
            log('PTY', traceback.format_exc())
            return

        log('PTY', f'{self.key} 创建完成，耗时 {(time.monotonic() - spawn_started) * 1000:.0f}ms')
        if getattr(self.proc, 'isolated', False):
            log('PTY', f'{self.key} 隔离进程 PID {self.proc.pid}，运行 PID {self.proc.runtime_pid}，原生 ADB PID {self.proc.native_pid}，诊断日志 {self.proc.diagnostic_log or "未启用"}')
        process = self.proc
        # 首包与后续输出统一由后台读取；构造函数不能等提示符或握手输出。
        # Reader thread: polls proc.read() for PTY output
        def reader():
            log('PTY', f'{self.key} reader started')
            first_output = True
            while self._alive:
                try:
                    data = process.read()
                    if data:
                        if first_output:
                            first_output = False
                            log('PTY', f'{self.key} 首包回显，启动后 {(time.monotonic() - spawn_started) * 1000:.0f}ms')
                        # 原始回显日志按需启用，避免每个字符同步写网络目录日志。
                        if os.environ.get('ADB_PTY_TRACE') == '1':
                            log('PTY', f'{self.key} output ({len(data)} bytes): {data[:100]}')
                        with self._lock:
                            callback = self.on_output if self._alive else None
                        # 共享目录/前端回调可能很慢，绝不能占住按键写入使用的锁。
                        if callback:
                            callback(data.encode('utf-8', errors='replace'))
                except EOFError as exc:
                    log('PTY', f'{self.key} EOF：{exc}')
                    break
                except Exception as e:
                    log('PTY', f'{self.key} reader error: {type(e).__name__}: {e}')
                    # 非 EOF 的底层 PTY 读取异常通常也不可恢复；继续空转会占满
                    # 一个后台线程并快速膨胀日志文件。
                    break
            # Process exited
            with self._lock:
                self._alive = False
                self.proc = None
                handle, self._child_handle = self._child_handle, None
            _release_pty_child(handle)
            self._notify_exit()

        threading.Thread(target=reader, daemon=True).start()

    def write(self, data: bytes):
        with self._lock:
            if self.proc and self._alive:
                try:
                    self.proc.write(data.decode('utf-8', errors='replace'))
                except Exception as e:
                    log('PTY', f'write {self.key} error: {e}')

    def resize(self, cols: int, rows: int):
        with self._lock:
            if self.proc and self._alive:
                try:
                    self.proc.setwinsize(rows, cols)
                except Exception:
                    pass

    def close(self):
        with self._lock:
            process = self.proc
            handle, self._child_handle = self._child_handle, None
            self._alive = False
            self.proc = None
        if process:
            log('PTY', f'{self.key} 关闭终端：停止输入并回收子进程')
            try:
                if getattr(process, 'isolated', False):
                    process.terminate(force=True)
                    _release_pty_child(handle)
                    log('PTY', f'{self.key} 隔离进程结束，退出码 {process.exit_code}')
                elif os.name == 'nt':
                    _release_pty_child(handle, terminate=True)
                    # 子进程已停止后再取消读，避免仍可写入的会话与清理并发。
                    pty = getattr(process, 'pty', None)
                    if pty is not None:
                        pty.cancel_io()
                    for name in ('fileobj', '_server'):
                        stream = getattr(process, name, None)
                        if stream is not None:
                            try:
                                stream.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            stream.close()
                else:
                    process.terminate(force=True)
            except Exception as exc:
                log('PTY', f'{self.key} 子进程清理失败：{exc}')
        self._notify_exit()

    def _notify_exit(self):
        with self._lock:
            if self._exit_notified:
                return
            self._exit_notified = True
            callback = self.on_exit
        if callback:
            try:
                callback()
            except Exception:
                pass

    def is_alive(self) -> bool:
        with self._lock:
            return self._alive and self.proc is not None


class ADBWorker:
    """
    Manages device polling and terminal sessions.
    All callbacks are invoked from the internal polling thread.
    The consumer (server.py) should marshal them to its own thread/loop.
    """

    def __init__(
        self,
        on_devices_changed: Callable[[List[dict], List[str]], None],
        on_terminal_output: Callable[[str, bytes], None],
        on_terminal_exit: Callable[[str], None],
        *,
        local_devices_enabled: bool = True,
        on_terminal_control=None,
    ):
        self.on_devices_changed = on_devices_changed
        self.on_terminal_output = on_terminal_output
        self.on_terminal_exit = on_terminal_exit
        from shared_terminal import TerminalHub
        self.terminal_hub = TerminalHub(on_terminal_control)
        # ``False`` = 本机不识别设备（窗口里只剩远端设备）。见 server.py 的
        # ``DEVMUX_NO_LOCAL_DEVICES``（兼容旧变量）。显式入参而不是在这里读环境变量：
        # 同机双实例验证时两个实例同进程/同环境，只有调用方能区分谁该被屏蔽。
        self.local_devices_enabled = bool(local_devices_enabled)

        self._known_devices: Dict[str, dict] = {}   # key -> device
        self._terminals: Dict[str, TerminalSession] = {}   # key -> TerminalSession
        self._pending_remove: Dict[str, int] = {}   # key -> offline counter
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self):
        with self._lifecycle_lock:
            # 不能复用正在退出的线程及其 Event。否则 stop() 超时后再次 clear()
            # 会让旧轮询线程恢复工作，并与新线程并存。
            if self._poll_thread and self._poll_thread.is_alive():
                log('ADB', 'Worker 仍在停止，拒绝重复启动轮询线程')
                return False
            self._stop_event = threading.Event()
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()
        # _poll_loop 在线程启动后会立即执行首次探测。不要在调用线程重复执行，
        # 否则 ADB 卡顿时会阻塞 WebSocket 初始化，导致桌面窗口迟迟不出现。
        return True

    def stop(self):
        with self._lifecycle_lock:
            stop_event = self._stop_event
            poll_thread = self._poll_thread
            stop_event.set()
        if poll_thread:
            poll_thread.join(timeout=POLL_STOP_JOIN_TIMEOUT)
        with self._lock:
            for key, session in list(self._terminals.items()):
                self.terminal_hub.remove(key)
                session.close()
            self._terminals.clear()
            # 清空已知设备/离线计数，使重启后（worker.start() 再次调用）能重新 spawn 会话
            self._known_devices.clear()
            self._pending_remove.clear()
        return not (poll_thread and poll_thread.is_alive())

    def write_terminal(self, key: str, data: bytes, *, generation=None):
        return self.terminal_hub.local_write(key, data, generation)

    def resize_terminal(self, key: str, cols: int, rows: int, *, generation=None):
        return self.terminal_hub.resize(key, cols, rows, generation=generation)

    def close_terminal(self, key: str):
        with self._lock:
            session = self._terminals.pop(key, None)
            self.terminal_hub.remove(key)
            if session:
                session.close()
            self._known_devices.pop(key, None)
            self._pending_remove.pop(key, None)

    def get_state(self):
        with self._lock:
            devices = list(self._known_devices.values())
            keys = list(self._known_devices.keys())
            return devices, keys

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _poll_loop(self):
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(POLL_INTERVAL)

    def _tick(self):
        # 屏蔽本机设备时不调用 adb（也顺带不做 server 重建）：本机设备列表恒空，
        # 下面的逻辑会把任何已知设备按正常的离线去抖流程清掉。
        devices = get_all_devices() if self.local_devices_enabled else []
        # stop 可能发生在阻塞的 ADB 调用期间。返回后必须直接退出，不能再创建
        # PTY 会话或广播旧设备状态。
        if self._stop_event.is_set():
            return
        online: Dict[str, dict] = {make_key(d): d for d in devices}

        # Dedup: if a serial already has transport_id entries, remove entries without tid
        serials_with_tid = set()
        for key, dev in list(online.items()):
            if dev.get('transportId') is not None:
                serials_with_tid.add(dev['serial'])
        # Also check known devices
        for key, dev in list(self._known_devices.items()):
            if dev.get('transportId') is not None:
                serials_with_tid.add(dev['serial'])
        for key, dev in list(online.items()):
            if dev.get('transportId') is None and dev['serial'] in serials_with_tid:
                log('ADC', f'skip {key} (same serial has transport_id)')
                del online[key]

        with self._lock:
            if self._stop_event.is_set():
                return
            # --- detect offline ---
            to_remove = []
            for key in list(self._known_devices.keys()):
                if key not in online:
                    count = self._pending_remove.get(key, 0) + 1
                    self._pending_remove[key] = count
                    if count >= CONFIRM_COUNT:
                        log('ADC', f'offline {key}')
                        to_remove.append(key)
                        self._pending_remove.pop(key, None)
                else:
                    if key in self._pending_remove:
                        log('ADC', f'cancel offline {key}')
                        self._pending_remove.pop(key, None)

            for key in to_remove:
                self._known_devices.pop(key, None)
                session = self._terminals.pop(key, None)
                self.terminal_hub.remove(key)
                if session:
                    session.close()
                # notify exit
                if self.on_terminal_exit:
                    try:
                        self.on_terminal_exit(key)
                    except Exception:
                        pass

            # --- detect new devices ---
            for key, dev in online.items():
                if key not in self._known_devices and key not in self._pending_remove:
                    log('ADC', f'new {key}')
                    self._known_devices[key] = dev
                    self._spawn_terminal(key, dev)

            # --- notify device list change ---
            all_devs = list(self._known_devices.values())
            all_keys = list(self._known_devices.keys())

        # callbacks outside lock
        if self.on_devices_changed:
            try:
                self.on_devices_changed(all_devs, all_keys)
            except Exception:
                pass

    def _spawn_terminal(self, key: str, dev: dict):
        output_lock = threading.Lock()
        early = []
        ready = [False]
        session_ref = [None]
        exited_early = [False]

        def on_output(data: bytes):
            with output_lock:
                if session_ref[0] is not None and not getattr(session_ref[0], '_alive', True):
                    return
                if ready[0]:
                    self.terminal_hub.publish(key, data, session_ref[0])
                else:
                    early.append(bytes(data))
            if self.on_terminal_output:
                try:
                    self.on_terminal_output(key, data)
                except Exception:
                    pass

        def on_exit():
            # close与读取线程可能各通知一次；旧PTY退出不能撤掉新的设备代次。
            if session_ref[0] is not None:
                self.terminal_hub.remove(key, session_ref[0])
            else:
                exited_early[0] = True

        try:
            session = TerminalSession(dev, on_output, on_exit)
            self._terminals[key] = session
            with output_lock:
                session_ref[0] = session
                if not exited_early[0] and getattr(session, '_alive', True):
                    self.terminal_hub.register(key, session)
                ready[0] = True
                for data in early:
                    self.terminal_hub.publish(key, data, session)
                early.clear()
        except BaseException as e:
            log('PTY', f'TerminalSession init failed for {key}: {e}')
            import traceback
            log('PTY', traceback.format_exc())


if __name__ == '__main__':
    # Simple smoke test
    def on_change(devs, keys):
        print(f'[DEVICES] {len(devs)} devices, keys={keys}')

    def on_output(key, data):
        sys.stdout.buffer.write(data)

    def on_exit(key):
        print(f'[EXIT] {key}')

    worker = ADBWorker(on_change, on_output, on_exit)
    worker.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        worker.stop()
