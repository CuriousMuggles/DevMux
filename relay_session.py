"""跨机终端会话层（设计文档 §6.2 与 §9.1 的第 3 层）。

一条远端终端会话被拆成**三个单写者文件**，因此两端都不需要锁：

```text
streams/<从机id>/<会话id>.out        从机写（PTY 输出）  主控读
streams/<从机id>/<会话id>.in         主控写（按键、Ctrl+C）从机读
streams/<从机id>/<会话id>.meta.json  从机写（状态与结束原因）主控读
```

为什么按键不走控制面：一次交互会在共享目录上产生海量小文件并触发同步风暴
（见设计文档 §4）。数据面是单向追加 + 字节偏移增量读，两端各自持有游标。

本模块**不含任何 adb / winpty / 线程池实现细节**：真正的 PTY 由调用方注入的
``backend`` 提供（应用里是 ``adb_worker.TerminalSession``，测试里是假对象）。
这样会话层可以在临时目录里完整验证，不需要真机。
"""
from __future__ import annotations

import os
import re
import threading
import time
import zipfile
from typing import Callable, Optional

import relay

# 会话标识直接复用「流标识」的字符集：它会被拼进文件名，必须保守。
SESSION_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

INPUT_POLL_INTERVAL_S = 0.03
OUTPUT_READ_LIMIT = 64 * 1024
# 会话结束后延迟搬走流文件：主控的读取线程可能正在读最后一段输出。
ARCHIVE_DELAY_S = 1.5
META_STATUS_RUNNING = 'running'
META_STATUS_ENDED = 'ended'
META_STATUS_CLOSED = 'closed'


def valid_session_id(value: str) -> bool:
    return bool(SESSION_ID_RE.match(str(value or '')))


class SessionBackend:
    """PTY 后端契约。实现方负责真正的进程/伪终端生命周期。

    四个方法都必须在**后端自己的线程模型**下安全：会话层会从不同线程调用它们。
    """

    def open(self, device: dict, *, cols: int, rows: int,
             on_output: Callable[[bytes], None],
             on_exit: Callable[[], None]):
        """启动一个设备 shell 并返回句柄。失败应抛异常而不是返回 None。"""
        raise NotImplementedError

    def write(self, handle, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, handle, cols: int, rows: int) -> None:
        raise NotImplementedError

    def close(self, handle) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------------
# 从机侧：一条远端会话
# ----------------------------------------------------------------------
class RemotePtySession:
    """从机侧的一条远端终端会话：本机 PTY ⇄ 共享目录流文件。

    刻意**不复用本机设备已有的终端会话**：那条会话属于坐在本机的人，让他和
    远端操作者共写同一个 PTY 会出现输入交错、回显互相覆盖，谁也无法排障。
    每条远端会话开自己的 PTY，语义就是一个独立的 adb shell。
    """

    def __init__(
        self,
        queue: 'relay.RelayQueue',
        stream_id: str,
        device: dict,
        backend: SessionBackend,
        *,
        cols: int = 120,
        rows: int = 40,
        on_event: Optional[Callable] = None,
    ):
        if not valid_session_id(stream_id):
            raise ValueError(f'会话标识非法：{stream_id!r}')
        self.queue = queue
        self.stream_id = stream_id
        self.device = dict(device or {})
        self.backend = backend
        self.cols = max(20, int(cols or 120))
        self.rows = max(5, int(rows or 40))
        self.on_event = on_event

        self._handle = None
        self._alive = False
        self._stop_flag = threading.Event()
        self._lock = threading.RLock()
        self._pump_thread: Optional[threading.Thread] = None
        self.bytes_out = 0
        self.bytes_in = 0
        self.error = ''
        self._started_at = ''
        self._starting = False
        self._startup_output = bytearray()
        self._meta_write_lock = threading.Lock()
        self._published_control = None
        self._control_thread = None
        self._meta_error = ''

    # --- 元信息：主控靠它判断会话是否还活着 ---
    def meta(self, status: str, *, reason: str = '') -> dict:
        return {
            'kind': 'session',
            'status': status,
            'streamId': self.stream_id,
            'deviceKey': self.device.get('key', ''),
            'serial': self.device.get('serial', ''),
            'cols': self.cols,
            'rows': self.rows,
            'bytesOut': self.bytes_out,
            'bytesIn': self.bytes_in,
            'startedAt': self._started_at,
            'updatedAt': relay.now_iso(),
            'error': self.error,
            'reason': reason,
            **(self._handle.state() if self._handle is not None and hasattr(self._handle, 'state') else {}),
        }

    def _write_meta(self, status: str, *, reason: str = '') -> None:
        # 尺寸调整和接管通知在取得发布锁后再读取实际控制权。
        with self._meta_write_lock:
            if status == META_STATUS_RUNNING and not self._alive:
                return
            payload = self.meta(status, reason=reason)
            try:
                self.queue.stream(self.stream_id).write_meta(payload, in_place=True)
            except OSError as exc:
                message = f'{type(exc).__name__}: {exc}'
                if message != self._meta_error:
                    self._emit('error', f'会话状态发布失败，等待重试：{self.stream_id} {message}')
                    self._meta_error = message
                return
            control = tuple(payload.get(k) for k in ('owner', 'generation', 'shared', 'held'))
            if self._published_control is not None and control != self._published_control:
                self._emit('session', f'终端控制权已发布：{payload.get("deviceKey")}，'
                           f'{"从机控制" if payload.get("owner") == "local" else "主机控制"}，'
                           f'代次 {payload.get("generation")}')
            self._published_control = control
            self._meta_error = ''

    def _control_loop(self):
        """独立检查控制权；失败不标记已同步，下轮继续重试。"""
        while not self._stop_flag.wait(0.25):
            handle = self._handle
            if handle is None or not self._alive:
                return
            current = handle.state()
            control = tuple(current.get(k) for k in ('owner', 'generation', 'shared', 'held'))
            if control != self._published_control:
                self._write_meta(META_STATUS_RUNNING)

    def _archive_streams(self) -> None:
        """把会话的三个文件打包成一个压缩包移进归档目录。

        延迟执行（见 ARCHIVE_DELAY_S）：主控的读取线程可能正好在读最后一段
        输出，立刻搬走会丢掉那几 KB 的尾巴。打包为单个 zip 而不是三个散文件：
        archive/ 顶层的 ``sess-*`` 散文件不在月份目录内、从不参与 compact 合并，
        会话关闭时直接合并成包，避免共享目录再为每个会话积累 3 个小文件。
        """
        channel = self.queue.stream(self.stream_id)
        archive_dir = self.queue.archive_dir
        bundle = os.path.join(archive_dir, f'{self.stream_id}.zip')
        parts = []
        for path in (channel.out_path, channel.in_path, channel.meta_path):
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'rb') as handle:
                    parts.append((os.path.basename(path), handle.read()))
            except OSError:
                continue
        if not parts:
            return
        if os.path.exists(bundle):
            bundle = f'{bundle}.{os.urandom(3).hex()}'
        temporary = bundle + '.partial'
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                for name, content in parts:
                    package.writestr(name, content)
            os.replace(temporary, bundle)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            # 打包失败退回逐个搬移，避免会话审计信息丢失。
            for path in (channel.out_path, channel.in_path, channel.meta_path):
                if not os.path.isfile(path):
                    continue
                target = os.path.join(archive_dir, os.path.basename(path))
                if os.path.exists(target):
                    target = f'{target}.{os.urandom(3).hex()}'
                try:
                    os.replace(path, target)
                except OSError:
                    pass
            return
        # 压缩包发布成功后再删除原散文件。
        for path in (channel.out_path, channel.in_path, channel.meta_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    # --- 生命周期 ---
    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._alive:
                return True, 'ok'
            self._starting = True
            self._startup_output.clear()
            try:
                self._handle = self.backend.open(
                    self.device, cols=self.cols, rows=self.rows,
                    on_output=self._on_output, on_exit=self._on_exit,
                )
            except Exception as exc:  # 后端启动失败：明确告诉主控原因
                self._starting = False
                self._startup_output.clear()
                self.error = f'{type(exc).__name__}: {exc}'
                return False, f'无法启动设备终端：{self.error}'
            if self._handle is None:
                self._starting = False
                self._startup_output.clear()
                self.error = 'PTY 后端未返回会话句柄'
                return False, self.error
            self._alive = True
            self._starting = False
            self._stop_flag.clear()
            self._started_at = relay.now_iso()
            self._write_meta(META_STATUS_RUNNING)
            # 后端可在 open 返回前产生首包，不能丢掉终端握手和提示符。
            initial = bytes(self._startup_output)
            self._startup_output.clear()
            if initial:
                self._on_output(initial)
            self._pump_thread = threading.Thread(
                target=self._input_pump, name=f'relay-session-in-{self.stream_id}', daemon=True,
            )
            self._pump_thread.start()
            if hasattr(self._handle, 'state'):
                self._control_thread = threading.Thread(target=self._control_loop,
                    name=f'relay-session-control-{self.stream_id}', daemon=True)
                self._control_thread.start()
        self._emit('session', f'远端会话已开启：{self.stream_id} @ {self.device.get("key")}')
        return True, 'ok'

    def _on_output(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if not self._alive:
                if self._starting:
                    self._startup_output.extend(data)
                return
        if self.queue.stream(self.stream_id).output_writer().append(data):
            self.bytes_out += len(data)

    def _on_exit(self) -> None:
        """PTY 自己结束（设备掉线、shell 退出）。"""
        reason = getattr(self._handle, 'exit_reason', '') or '设备终端已结束'
        self._finish(META_STATUS_CLOSED if reason == '终端控制权切换' else META_STATUS_ENDED, reason)

    def _finish(self, status: str, reason: str) -> None:
        with self._lock:
            if not self._alive and status != META_STATUS_CLOSED:
                return
            self._alive = False
            handle = self._handle
            self._handle = None
        self._stop_flag.set()
        if handle is not None:
            try:
                self.backend.close(handle)
            except Exception:  # 清理路径绝不抛出
                pass
        self._write_meta(status, reason=reason)
        self._emit('session', f'远端会话已结束：{self.stream_id}（{reason}）')
        timer = threading.Timer(ARCHIVE_DELAY_S, self._archive_streams)
        timer.daemon = True
        timer.start()

    def close(self, reason: str = '操作端关闭了会话') -> None:
        self._finish(META_STATUS_CLOSED, reason)

    def resize(self, cols: int, rows: int) -> tuple[bool, str]:
        with self._lock:
            if not self._alive or self._handle is None:
                return False, '会话不在运行状态'
            size = (max(20, int(cols)), max(5, int(rows)))
            if size == (self.cols, self.rows):
                return True, 'ok'
            self.cols, self.rows = size
            try:
                self.backend.resize(self._handle, self.cols, self.rows)
            except Exception as exc:
                return False, f'调整窗口失败：{type(exc).__name__}: {exc}'
            self._write_meta(META_STATUS_RUNNING)
        return True, 'ok'

    def is_alive(self) -> bool:
        with self._lock:
            return self._alive

    # --- 输入泵：读本机分区里的 .in，写进 PTY ---
    def _input_pump(self) -> None:
        channel = self.queue.stream(self.stream_id)
        tail = channel.input_tail(0)
        active_until = 0.0
        while not self._stop_flag.is_set():
            try:
                data = tail.read_new(OUTPUT_READ_LIMIT)
            except Exception as exc:
                self._emit('error', f'读取会话输入失败：{type(exc).__name__}: {exc}')
                break
            if data:
                active_until = time.monotonic() + 0.5
                with self._lock:
                    handle = self._handle if self._alive else None
                if handle is not None:
                    try:
                        if self.backend.write(handle, data) is not False:
                            self.bytes_in += len(data)
                    except Exception as exc:
                        self._emit('error', f'写入设备终端失败：{type(exc).__name__}: {exc}')
            self._stop_flag.wait(0.01 if time.monotonic() < active_until else INPUT_POLL_INTERVAL_S)

    def _emit(self, kind: str, text: str) -> None:
        if not self.on_event:
            return
        try:
            self.on_event({'kind': kind, 'text': text, 'streamId': self.stream_id})
        except Exception:
            pass


class SessionManager:
    """从机侧接入表：后端负责复用本地PTY或创建独立会话。"""

    def __init__(self, queue: 'relay.RelayQueue', backend: SessionBackend,
                 *, on_event: Optional[Callable] = None):
        self.queue = queue
        self.backend = backend
        self.on_event = on_event
        self._sessions: dict[str, RemotePtySession] = {}
        self._lock = threading.RLock()
        self._open_locks = {}
        self._generation = 0
        self._stream_versions = {}
        self._owners = {}
        self._device_locks = {}

    def open(self, stream_id: str, device: dict, *, cols: int = 120, rows: int = 40,
             owner=None) -> tuple[bool, str]:
        if not valid_session_id(stream_id):
            return False, f'会话标识非法：{stream_id!r}'
        with self._lock:
            generation = (self._generation, self._stream_versions.get(stream_id, 0))
            open_lock = self._open_locks.setdefault(stream_id, threading.Lock())
            device_key = str(device.get('key') or device.get('serial') or '')
            device_lock = self._device_locks.setdefault(device_key, threading.Lock())
        with open_lock, device_lock:
            with self._lock:
                if generation != (self._generation, self._stream_versions.get(stream_id, 0)):
                    return False, '等待接入期间连接已关闭'
                existing = self._sessions.get(stream_id)
                if owner is not None and generation[1]:
                    return False, '会话标识已退役，拒绝迟到的开启请求'
            if existing is not None and existing.is_alive():
                existing_key = str(existing.device.get('key') or existing.device.get('serial') or '')
                if existing_key != device_key or self._owners.get(stream_id) != owner:
                    return False, '会话标识已属于其他设备或接入所有者'
                return True, '会话已存在'
            # 仅替换当前已准入实例、同连接代次的旧订阅；不同所有者仍由独占门禁拒绝。
            if owner is not None:
                with self._lock:
                    if any(item.is_alive() and self._owners.get(sid) != owner
                           and str(item.device.get('key') or item.device.get('serial') or '') == device_key
                           for sid, item in self._sessions.items()):
                        return False, '该设备已有其他接入所有者，拒绝替换'
                    old_ids = [sid for sid, item in self._sessions.items()
                               if sid != stream_id and self._owners.get(sid) == owner
                               and str(item.device.get('key') or item.device.get('serial') or '') == device_key]
                for sid in old_ids:
                    self.close(sid, '同一主控重新接入，回收旧会话', owner=owner)
            # 不同设备可以并行启动，慢 PTY 创建不能占住整个会话表。
            session = RemotePtySession(
                self.queue, stream_id, device, self.backend,
                cols=cols, rows=rows, on_event=self.on_event,
            )
            ok, reason = session.start()
            if not ok:
                return False, reason
            with self._lock:
                admitted = generation == (self._generation, self._stream_versions.get(stream_id, 0))
                if admitted:
                    self._sessions[stream_id] = session
                    self._owners[stream_id] = owner
            if not admitted:
                session.close('初始化期间连接已关闭')
                return False, '初始化期间连接已关闭'
            return True, 'ok'

    def resize(self, stream_id: str, cols: int, rows: int) -> tuple[bool, str]:
        with self._lock:
            session = self._sessions.get(stream_id)
        if session is None:
            return False, f'会话不存在：{stream_id}'
        return session.resize(cols, rows)

    def take_control(self, stream_id, generation):
        with self._lock:
            session = self._sessions.get(stream_id)
        if session is None or not session.is_alive() or session._handle is None:
            return False, '远端终端接入已失效'
        take = getattr(self.backend, 'take_control', None)
        if take is None:
            return False, '从机不支持共享终端接管，请升级从机'
        return take(session._handle, generation)

    def close(self, stream_id: str, reason: str = '操作端关闭了会话', *, owner=None) -> tuple[bool, str]:
        with self._lock:
            if stream_id in self._owners and self._owners[stream_id] is not None and self._owners[stream_id] != owner:
                return False, '会话不属于当前接入所有者'
            self._stream_versions[stream_id] = self._stream_versions.get(stream_id, 0) + 1
            session = self._sessions.pop(stream_id, None)
            self._owners.pop(stream_id, None)
        if session is None:
            return True, '会话已不存在'
        session.close(reason)
        return True, 'ok'

    def active_ids(self) -> list[str]:
        with self._lock:
            return sorted(sid for sid, session in self._sessions.items() if session.is_alive())

    def snapshot(self) -> list[dict]:
        """正在对外提供的会话（用 ``meta`` 的口径，和主控看到的字段一致）。

        从机操作者需要知道「现在谁在用我的设备」，窗口里的会话表因此不能空着 ——
        那会让人以为没有人在操作，甚至误以为中转坏了。
        """
        with self._lock:
            sessions = [
                (sid, session) for sid, session in self._sessions.items() if session.is_alive()
            ]
        return [session.meta(META_STATUS_RUNNING) for _sid, session in sorted(sessions)]

    def refresh_control_meta(self, key):
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.device.get('key') == key and session.is_alive():
                session._write_meta(META_STATUS_RUNNING)

    def stop_all(self) -> None:
        with self._lock:
            self._generation += 1
            sessions = list(self._sessions.values())
            for sid in self._sessions:
                self._stream_versions[sid] = self._stream_versions.get(sid, 0) + 1
            self._sessions.clear()
            self._owners.clear()
        for session in sessions:
            try:
                session.close('从机停止中转')
            except Exception:
                pass


# ----------------------------------------------------------------------
# 主控侧：读远端会话
# ----------------------------------------------------------------------
class RemoteSessionReader:
    """主控侧读取一条远端会话：增量读 ``.out``、靠 ``.meta.json`` 判结束。

    游标由本端持有；``read_stream`` 返回的 ``rebuilt`` 说明对端重建了文件
    （会话 id 复用），此时游标已归零，按新会话继续读即可。
    """

    def __init__(
        self,
        peer_id: str,
        stream_id: str,
        *,
        read_stream: Callable[[], tuple[bytes, int, bool]],
        read_meta: Callable[[], Optional[dict]],
        on_output: Callable[[bytes], None],
        on_exit: Callable[[str], None],
        poll_interval_s: float = 0.03,
        on_event: Optional[Callable] = None,
    ):
        self.peer_id = str(peer_id)
        self.stream_id = str(stream_id)
        self._read_stream = read_stream
        self._read_meta = read_meta
        self.on_output = on_output
        self.on_exit = on_exit
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.on_event = on_event

        self.bytes_read = 0
        self.offset = 0
        self.rebuilds = 0
        self.last_meta = {}
        self._missing_meta_since = None
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._exited = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f'relay-session-out-{self.stream_id}', daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        next_meta = 0.0
        active_until = 0.0
        while not self._stop_flag.is_set():
            try:
                data, offset, rebuilt = self._read_stream()
            except Exception as exc:
                self._emit('error', f'读取远端会话输出失败：{type(exc).__name__}: {exc}')
                if not self._stop_flag.is_set():
                    self._finish('远端会话输出读取失败，重新建立终端接入')
                return
            if rebuilt and self.offset:
                self.rebuilds += 1
                self._emit('session', f'远端会话 {self.stream_id} 的输出被重建，游标从 0 重读')
                self.offset = 0
            self.offset = offset
            if data:
                self.bytes_read += len(data)
                try:
                    self.on_output(data)
                except Exception:
                    pass
            now = time.monotonic()
            if now >= next_meta:
                # 生命周期状态不必每个回显包都读，避免 SMB 元数据 IO 挤占字符流。
                meta = self._safe_meta()
                if meta is not None and meta.get('status') == 'missing':
                    if self._missing_meta_since is None:
                        self._missing_meta_since = time.monotonic()
                    elif time.monotonic() - self._missing_meta_since >= 3.0:
                        self._finish('远端会话文件已丢失，重新建立终端接入')
                        return
                elif meta is not None:
                    self._missing_meta_since = None
                    self.last_meta = meta
                else:
                    self._missing_meta_since = None
                next_meta = time.monotonic() + 0.5
                if meta is not None and str(meta.get('status')) not in ('', META_STATUS_RUNNING, 'missing'):
                    self._finish(self._exit_reason(meta))
                    return
            if data:
                active_until = time.monotonic() + 0.5
                continue  # 已有数据立即继续排空，不逐块额外等待。
            delay = min(self.poll_interval_s, 0.01) if time.monotonic() < active_until else self.poll_interval_s
            self._stop_flag.wait(delay)

    def _safe_meta(self) -> Optional[dict]:
        try:
            return self._read_meta()
        except Exception:
            return None

    def _exit_reason(self, meta: dict) -> str:
        reason = str(meta.get('reason') or '').strip()
        if reason:
            return reason
        status = str(meta.get('status') or '')
        return '远端会话已结束' if status == META_STATUS_ENDED else '远端会话已关闭'

    def _finish(self, reason: str) -> None:
        if self._exited:
            return
        self._exited = True
        try:
            self.on_exit(reason)
        except Exception:
            pass

    def stop(self, reason: str = '') -> None:
        """停止本地读取线程。**不通知 on_exit**：调用方主动关闭时无需再报退出。"""
        self._stop_flag.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
            self._thread = None
        if reason:
            self._emit('session', reason)

    def is_exited(self) -> bool:
        return self._exited

    def _emit(self, kind: str, text: str) -> None:
        if not self.on_event:
            return
        try:
            self.on_event({'kind': kind, 'text': text, 'streamId': self.stream_id})
        except Exception:
            pass
