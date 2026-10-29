"""跨机中转 —— 节点运行时（主控 / 从机共用的接入层）。

一个 :class:`RelayNode` 就是「一台电脑上的 DevMux 应用」接入中转之后的
那一层：它持有载体（共享文件夹）、心跳、本机设备列表与信任名单，并按角色提供
两种能力：

- **从机（slave）**：轮询 ``requests/<自己>/``，逐条裁决后在本机 adb 上执行，
  回写 ``results/<自己>/<reqId>.json``；日志类请求把设备端的持续输出追加到
  ``streams/<自己>/<streamId>.out``。
- **主控（master）**：枚举远端设备、下发请求并等待结果、按字节偏移增量读远端
  流；同时对自己的设备可以直接本地执行 —— 两类设备的操作**同构**，key 只差
  ``relay:<peerId>:`` 前缀。

分工说明（避免后来者困惑）：**链路语义**走载体（``RelayCarrier`` 的打开/状态/
消息槽），**分区路径语义**走协议层（``relay.RelayQueue``）。原因是载体的键名
刻意不允许斜杠（防目录穿越），承载不了 ``<槽>/<peerId>/<键>`` 这样的三层路径。

与 relay.py / relay_carrier.py 一脉相承的三条硬约束：

- 共享目录 IO 在后台线程发生，绝不能放进 asyncio 事件循环。心跳写入、逐对端
  读取、发现和命令分别执行；``self._io_lock`` 只保护内存状态，慢网络 IO
  不能占用它，以免文件传输阻塞在线判定或界面请求。
- 安全边界落在从机：主控配置再宽松，也必须过从机的 ``check_command`` 与
  「已确认主控」名单。
- 设备选择符（``-s`` / ``-t``）由**执行端**依据 ``device`` 字段自己拼装，
  请求里出现这些 token 一律拒绝（``relay.check_command`` 已覆盖）。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

import relay
import relay_file_io
import relay_journal
import relay_carrier
import relay_session
import relay_transfer
import tempfile
from pathlib import Path

# 连接验证节奏：验证成功后按 OK 间隔复查，失败/未验证时按重试间隔密集探测。
# 原实现无论成败都按 5s 复查，在线从机会以 ~6s 一次的频率持续产生
# check-connection 请求与回执文件（实测约 4000 文件/天），是共享目录小文件的
# 最大来源；验证成功后退避到 30s 可将该写放大降低约 10 倍，从机重启仍会在
# 一个 OK 间隔内被新代次请求拒绝（fail-closed），安全性不变。
CONNECTION_VERIFY_OK_INTERVAL_S = 30.0
CONNECTION_VERIFY_RETRY_INTERVAL_S = 5.0

APP_VERSION = '1.2'
APP_STARTED_MONOTONIC = time.monotonic()

# 所有 adb 子进程都必须带这个 flag，否则 GUI 版会弹出控制台窗口。
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0

DEFAULT_ADB_PATH = os.environ.get('ADB_PATH') or 'adb'
DEFAULT_ADB_TIMEOUT_S = 30.0
DEFAULT_LOG_TIMEOUT_S = 15.0
MAX_OUTPUT_BYTES = 64 * 1024
STREAM_READ_LIMIT = 16 * 1024

# 远端设备 key 的前缀：``relay:<peerId>:<本机key>``。
# peerId 经 check_peer_id 校验后不含冒号，而本机 key 形如 ``serial:tid=2``，
# 因此按**前两个冒号**切分即可无损还原，不会被 tid 里的冒号干扰。
REMOTE_KEY_PREFIX = 'relay'

# 从机离线后，其远端设备在设备栏里**保留多久**（秒）。
#
# 设计上从机离线不等于设备消失：本地设备离线也只是灰显，远端设备必须同构，
# 否则「操作无区别」在离线这一瞬就破了（设计文档 §5.4 / §7.3）。
# 但心跳文件会永远留在共享目录里，所以超过这个时长就当作对端已退场，不再列出，
# 避免换机器、改 peerId 之后留下永久幽灵设备。
REMOTE_DEVICE_LINGER_S = 300.0

# 设备枚举去抖：设备必须连续这么多次枚举都没出现，才算真的离线被移除。
# 与 adb_worker.py 的离线去抖同源 —— 「枚举漏了一次」和「设备真的拔了」必须分开。
DEVICE_MISS_THRESHOLD = 3

STREAM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

# 退出码标记：``adb shell`` 不回传设备端退出码，由设备端自己报。
EXIT_MARKER = '__ADM_EXIT:'


# ----------------------------------------------------------------------
# 设备枚举
# ----------------------------------------------------------------------
def device_key(serial: str, transport_id=None) -> str:
    """设备稳定标识，与 server.py / adb_worker.py 保持一致：``serial:tid=<id>``。"""
    text = str(serial or '')
    if transport_id:
        return f'{text}:tid={transport_id}'
    return text


def parse_devices(text: str) -> list[dict]:
    """解析 ``adb devices -l`` 的输出。

    只保留 ``device`` 状态的行交给上层使用，但 offline / unauthorized 也一并
    返回（state 字段区分），让主控能看到「从机有一台设备但不可用」。
    """
    devices: list[dict] = []
    for line in (text or '').splitlines():
        raw = line.strip()
        if not raw or raw.startswith('List of devices') or raw.startswith('*'):
            continue
        parts = raw.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        transport_id = None
        model = ''
        for token in parts[2:]:
            head, _, tail = token.partition(':')
            if head == 'transport_id':
                try:
                    transport_id = int(tail)
                except ValueError:
                    transport_id = None
            elif head == 'model':
                model = tail
        devices.append({
            'serial': serial,
            'state': state,
            'transportId': transport_id,
            'model': model,
            'key': device_key(serial, transport_id),
        })
    return devices


def list_devices(adb_path: str, timeout: float = 8.0) -> tuple[list[dict], str]:
    """执行 ``adb devices -l``，返回 ``(设备列表, 错误信息)``。

    注意这里**只读**，不启动/停止 adb server —— ADB server 的恢复逻辑仍然只在
    ``adb_worker`` 的轮询失败路径里，避免两处同时 kill/start 把 server 拉坏。
    """
    try:
        proc = subprocess.run(
            [adb_path, 'devices', '-l'],
            capture_output=True, timeout=max(1.0, float(timeout)),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return [], f'adb devices 超时（{timeout:g}s）'
    except OSError as exc:
        return [], f'无法启动 adb：{exc}'
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    if proc.returncode != 0:
        detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()
        return parse_devices(text), detail or f'adb devices 退出码 {proc.returncode}'
    return parse_devices(text), ''


def remote_key(peer_id: str, local_key: str) -> str:
    return f'{REMOTE_KEY_PREFIX}:{peer_id}:{local_key}'


def split_remote_key(key: str) -> tuple[str, str]:
    """把 ``relay:<peerId>:<本机key>`` 还原为 ``(peerId, 本机key)``。"""
    text = str(key or '')
    if not text.startswith(f'{REMOTE_KEY_PREFIX}:'):
        return '', text
    rest = text[len(REMOTE_KEY_PREFIX) + 1:]
    peer_id, _, local = rest.partition(':')
    return peer_id, local


# ----------------------------------------------------------------------
# 设备端命令执行
# ----------------------------------------------------------------------
@dataclass
class ExecOutcome:
    exitCode: Optional[int] = None
    stdout: str = ''
    stderr: str = ''
    truncated: bool = False
    elapsedMs: int = 0
    timeout: bool = False
    error: Optional[str] = None
    exitCodeAvailable: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """超长输出保留头尾、中间标注省略。

    单纯截头会让 ``logcat`` / ``dmesg`` 丢掉最有价值的末尾；单纯截尾又会让
    ``df -h`` 这类丢掉表头。头尾各留一段，并**明确写出省掉了多少**，让调用方
    知道结果不完整。
    """
    raw = text or ''
    size = len(raw.encode('utf-8', 'replace'))
    if size <= limit or limit <= 0:
        return raw, False
    head_len = int(limit * 0.6)
    tail_len = limit - head_len
    head = raw.encode('utf-8', 'replace')[:head_len].decode('utf-8', 'replace')
    tail = raw.encode('utf-8', 'replace')[-tail_len:].decode('utf-8', 'replace')
    omitted = max(0, size - len(head.encode('utf-8', 'replace')) - len(tail.encode('utf-8', 'replace')))
    return f'{head}\n…… 〔已省略 {omitted} 字节〕 ……\n{tail}', True


def split_exit_marker(text: str) -> tuple[str, Optional[int]]:
    """从设备端输出里剥离 ``__ADM_EXIT:<code>`` 标记行。

    用 ``rfind`` 取最后一个标记（命令自身的输出里若出现同名字符串，前面那些不会
    被误判）；标记后必须紧跟可解析的整数，否则视为没有标记并返回 ``None``
    —— 退出码**不可得就是不可得**，绝不假装成 0。
    """
    raw = text or ''
    index = raw.rfind(EXIT_MARKER)
    if index < 0:
        return raw, None
    tail = raw[index + len(EXIT_MARKER):]
    number = tail.split('\r', 1)[0].split('\n', 1)[0].strip()
    try:
        code = int(number)
    except ValueError:
        return raw, None
    return raw[:index].rstrip('\r\n'), code


def device_selector_args(device: Optional[dict]) -> list[str]:
    """由**执行端**依据设备记录拼装选择符，永远不接受请求方传入。"""
    if not device:
        return []
    transport_id = device.get('transportId')
    if transport_id:
        return ['-t', str(transport_id)]
    serial = str(device.get('serial') or '')
    return ['-s', serial] if serial else []


def execute(
    adb_path: str,
    device: Optional[dict],
    argv,
    *,
    timeout: float = DEFAULT_ADB_TIMEOUT_S,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> ExecOutcome:
    """在指定设备上执行一条 adb 命令。

    ``shell`` 子命令的语义 = 把 ``argv[1:]`` 用空格拼成一条设备端 shell 命令，
    并在尾部追加退出码标记；其它子命令直接透传，退出码取 adb 自身的返回码。
    """
    started = time.time()
    tokens = [str(part) for part in (argv or [])]
    if not tokens:
        return ExecOutcome(error='命令为空', elapsedMs=0)

    is_shell = tokens[0] == 'shell'
    selector = device_selector_args(device)
    if is_shell:
        inner = ' '.join(tokens[1:]).strip()
        if not inner:
            return ExecOutcome(error='shell 命令为空', elapsedMs=0)
        # 列表形式传参，不经过本地 shell；只有设备端 shell 会解析这条串。
        command = [adb_path, *selector, 'shell', f'{inner}; echo {EXIT_MARKER}$?']
    else:
        command = [adb_path, *selector, *tokens]

    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=max(1.0, float(timeout)),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ExecOutcome(
            timeout=True, error=f'执行超时（{timeout:g}s），已终止该命令',
            elapsedMs=int((time.time() - started) * 1000),
        )
    except OSError as exc:
        return ExecOutcome(
            error=f'无法启动 adb：{exc}', elapsedMs=int((time.time() - started) * 1000),
        )

    stdout = (proc.stdout or b'').decode('utf-8', 'replace').replace('\r\n', '\n')
    stderr = (proc.stderr or b'').decode('utf-8', 'replace').replace('\r\n', '\n')
    exit_code = proc.returncode
    exit_code_available = True
    if is_shell:
        stdout, parsed = split_exit_marker(stdout)
        if parsed is None:
            exit_code = None
            exit_code_available = False
        else:
            exit_code = parsed

    stdout, cut_out = truncate_text(stdout, max_bytes)
    stderr, cut_err = truncate_text(stderr, max_bytes)
    return ExecOutcome(
        exitCode=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=cut_out or cut_err,
        elapsedMs=int((time.time() - started) * 1000),
        exitCodeAvailable=exit_code_available,
    )


# ----------------------------------------------------------------------
# 从机侧：把设备端长驻命令的输出持续写进分区流
# ----------------------------------------------------------------------
class LogStreamReader:
    """一条远端日志流：跑一条长驻 adb 命令，把输出追加到 ``streams/<自己>/<id>.out``。

    单写者：只有本机（从机）写这条流，主控只读，因此不需要任何锁。
    """

    def __init__(
        self,
        queue: relay.RelayQueue,
        stream_id: str,
        device: dict,
        argv,
        adb_path: str,
        *,
        on_event: Optional[Callable] = None,
    ):
        self.queue = queue
        self.stream_id = stream_id
        self.device = dict(device or {})
        self.argv = [str(part) for part in (argv or [])]
        self.adb_path = adb_path
        self.on_event = on_event
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self.bytes_written = 0
        self.error = ''

    # --- 元信息（写在 ``streams/<自己>/<id>.meta.json``，主控可读）---
    def _meta(self, status: str) -> dict:
        return {
            'kind': 'log',
            'status': status,
            'streamId': self.stream_id,
            'deviceKey': self.device.get('key', ''),
            'argv': list(self.argv),
            'bytesWritten': self.bytes_written,
            'startedAt': self._started_at,
            'updatedAt': relay.now_iso(),
            'error': self.error,
        }

    def start(self) -> bool:
        tokens = [str(part) for part in self.argv]
        command = [self.adb_path, *device_selector_args(self.device), *tokens]
        try:
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.error = f'无法启动 adb：{exc}'
            return False
        self._started_at = relay.now_iso()
        self.queue.stream(self.stream_id).write_meta(self._meta('running'))
        self._thread = threading.Thread(
            target=self._pump, name=f'relay-log-{self.stream_id}', daemon=True,
        )
        self._thread.start()
        return True

    def _pump(self) -> None:
        writer = self.queue.stream(self.stream_id).output_writer()
        stdout = self._proc.stdout if self._proc else None
        try:
            while not self._stop_flag.is_set() and stdout is not None:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                if writer.append(chunk):
                    self.bytes_written += len(chunk)
        except (OSError, ValueError) as exc:
            self.error = f'流写入失败：{exc}'
        finally:
            self.queue.stream(self.stream_id).write_meta(
                self._meta('stopped' if self._stop_flag.is_set() else 'ended')
            )

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_flag.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# ----------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------
class RelayNode:
    """一台电脑上的应用接入中转之后的运行时。主控与从机是同一个类。"""

    def __init__(
        self,
        *,
        peer_id: str,
        role: str,
        root: str = '',
        pool_id: str = '',
        node_id: str = '',
        pairing_path: str = '',
        pairing_store=None,
        pairing_required: Optional[bool] = None,
        passive_slave: bool = False,
        adb_path: str = DEFAULT_ADB_PATH,
        carrier_kind: str = relay_carrier.CARRIER_SHARED_FOLDER,
        managed_serials: Optional[list] = None,
        allow_shell: bool = True,
        allow_windows: bool = False,
        windows_service=None,
        extension_tools=None,
        windows_directory='',
        allowed_subcommands: Optional[tuple] = None,
        trust_path: str = '',
        trust_store=None,
        host_label: str = '',
        host_fingerprint: str = '',
        heartbeat_interval_s: float = relay.DEFAULT_HEARTBEAT_INTERVAL_S,
        poll_interval_s: float = 0.25,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        max_workers: int = 2,
        event_sink: Optional[Callable] = None,
        session_backend: Optional[object] = None,
        local_devices_enabled: bool = True,
        control_journal: bool = True,
        journal_state_dir: str = '',
    ):
        self.peer_id = relay.check_peer_id(peer_id, '本机标识')
        self.role = relay.normalize_role(role)
        # ``base_root`` 是配置里填的共享目录；``root`` 是**实际数据根**。
        # 启用池（``pool_id`` 非空）时下沉一层到 ``<base>/pool/<poolId>/``，未启用时
        # 两者相同 —— 于是老部署零迁移。
        #
        # 关键在于 ``self.root`` 的语义**没有变**：它仍然是「所有槽目录的父目录」。
        # 所以 RelayQueue、carrier、分区逻辑一行都不用改，这正是模型 A 改动小的根源
        # （对比 docs/v6 §9.3 里模型 B 要同动七个子系统）。
        self.base_root = str(root or '')
        self.pool_id = str(pool_id or '').strip()
        self.root = relay.resolve_pool_root(self.base_root, self.pool_id)
        canonical_root = os.path.normcase(os.path.realpath(os.path.abspath(self.root))) if self.root else ''
        self._connection_scope = (hashlib.sha256(canonical_root.encode('utf-8')).hexdigest()[:16]
                                  if canonical_root else '')
        self._peer_roots: dict[str, str] = {}
        self._peer_deployments: dict[str, str] = {}
        self._route_conflicts: set[str] = set()
        self.adb_path = adb_path
        self.managed_serials = [str(x) for x in (managed_serials or []) if str(x)]
        # ``False`` = 本机不识别设备：既不上报设备数，也不共享任何设备（见
        # ``_refresh_devices``）。用于「这台机器不接设备，只做主控管别的机器」。
        # 名字必须避开 ``local_devices()``（返回本机设备列表的公开方法）——
        # 用 ``self.local_devices = ...`` 会把那个方法整个盖掉。
        self.local_devices_enabled = bool(local_devices_enabled)
        self.allow_shell = bool(allow_shell)
        self.allow_windows = bool(allow_windows)
        self.windows_service = windows_service
        self.extension_tools = extension_tools
        self.windows_directory = windows_directory
        self._owns_windows_service = False
        self.allowed_subcommands = allowed_subcommands
        self.host_label = host_label or os.environ.get('COMPUTERNAME') or 'localhost'
        self.host_fingerprint = host_fingerprint or relay.host_fingerprint()
        # 实例键：**同机多开时区分两个实例**的身份。指纹是机器级的（同机两实例
        # 完全相同），不能拿来配对，所以另开一层。由上层持久化后传入；直接构造
        # （测试、单实例演示）时临时生成一个，那种场景不跨重启，漂了也无所谓。
        self.node_id = relay.check_peer_id(node_id or relay.new_node_id(self.host_label), '实例键')
        # 配对门槛。默认跟随池：启用了池就是在跟「多套部署共用一个目录」打交道，
        # 那正是需要配对的场景；没启用池就是今天的单套部署，行为完全不变。
        self.pairing_required = bool(self.pool_id) if pairing_required is None else bool(pairing_required)
        # 应用从机由主控发起连接；底层库保留显式双端准入模式供旧调用方使用。
        self.passive_slave = bool(passive_slave and self.role == relay.ROLE_SLAVE)
        self.pairing = pairing_store if pairing_store is not None else relay.PairingStore(
            pairing_path or self._default_pairing_path())
        # 池内槽位（注册表里分配，≥1）。0 = 尚未注册。
        self._slot = 0
        self._registry_warned = False
        self.heartbeat_interval_s = min(1.0, max(0.5, float(heartbeat_interval_s)))
        self._heartbeat_sequence = 0
        self._sequence_observer = relay.HeartbeatSequenceObserver()
        self._monitor_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='relay-heartbeat-read')
        self._monitor_futures = {}
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self.event_sink = event_sink

        self.carrier = relay_carrier.create_carrier(carrier_kind, self.peer_id, {'root': self.root})
        # 自己的分区：requests/results/streams 都只碰 ``<槽>/<自己>/``。
        self.queue = relay.RelayQueue.for_peer(self.channel_root(self.peer_id), self.peer_id)
        self.control_journal = bool(control_journal)
        self._journal_runtime = None
        self._journal_queues = {}
        self._journal_lock = threading.RLock()
        self._journal_notices = set()
        # 状态跟随本机信任配置；配置在网络盘时改用本机数据目录。
        state_anchor = (self._default_trust_path() if relay_file_io.is_remote(trust_path or self._default_trust_path())
                        else trust_path or self._default_trust_path())
        state_base = journal_state_dir or os.path.join(
            os.path.dirname(os.path.abspath(state_anchor)), 'journal-state')
        self._journal_state_path = os.path.join(state_base, self.node_id + '.sqlite3')
        self.trust = trust_store if trust_store is not None else relay.TrustStore(
            trust_path or self._default_trust_path())

        self._io_lock = threading.RLock()
        self._heartbeat_read_metrics = {}
        self._heartbeat_slow_notices = {}
        self._heartbeat_diagnostics_lock = threading.Lock()
        self._device_refresh_lock = threading.Lock()
        self._device_refresh_future = None
        self._device_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='relay-devices')
        self._discovery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='relay-discovery')
        self._discovery_future = None
        self._presence_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='relay-presence')
        self._presence_future = None
        self._heartbeat_compat_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='relay-heartbeat-compat')
        self._heartbeat_compat_future = None
        self._heartbeat_compat_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix='relay-exec')
        # 开终端不排在耗时命令、连接探测和窗口调整后面；归档不占响应路径。
        self._terminal_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='relay-terminal')
        self._archive_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='relay-archive')
        self._archive_futures = set()
        self._archive_lock = threading.Lock()
        self._shared_release_future = None
        self._shared_release_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='relay-shared-release')
        self._inflight: set[str] = set()
        self._streams: dict[str, LogStreamReader] = {}
        self._windows_streams = {}
        self._serial_streams = {}
        self._windows_stream_lock = threading.Lock()
        self._tails: dict[tuple, relay.StreamTail] = {}

        self._devices: list[dict] = []
        # 每个设备的「连续未出现次数」，用于去抖（见 DEVICE_MISS_THRESHOLD）。
        self._device_miss: dict[str, int] = {}
        self._device_error = ''
        self._peers: dict[str, relay.PeerHeartbeat] = {}
        self._presence_snapshot = None
        # 只缓存连接验证事实，不在共享目录发布本机的信任/配对名单。
        self._connection_verified: dict[str, tuple] = {}
        self._connection_verified_at: dict[str, str] = {}
        self._connection_checks: dict[str, tuple] = {}
        self._connection_check_after: dict[str, float] = {}
        self._input_locks: dict[tuple, threading.Lock] = {}
        self._tail_locks: dict[tuple, threading.Lock] = {}
        self._health: relay.RelayHealth = relay.RelayHealth(role=self.role, peerId=self.peer_id)
        self._started_at = relay.now_iso()
        self._last_state = ''

        # 从机侧的远端终端会话（PTY 后端由应用注入；库本身不 import adb/winpty）。
        self._sessions: Optional[relay_session.SessionManager] = None
        if session_backend is not None:
            self._sessions = relay_session.SessionManager(
                self.queue, session_backend, on_event=self._emit,
            )
        # 主控侧的远端会话读取线程：streamId -> RemoteSessionReader
        self._readers: dict[tuple, relay_session.RemoteSessionReader] = {}

    def _default_trust_path(self) -> str:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'adb-manager', f'relay-trust-{self.peer_id}.json')

    def _journal_queue(self, peer_id, *, sending=False):
        with self._journal_lock:
            if self._journal_runtime is None:
                self._journal_runtime = relay_journal.Runtime(self._journal_state_path, self.node_id)
            root = self.channel_root(peer_id)
            key = (root, peer_id, sending)
            if key not in self._journal_queues:
                self._journal_queues[key] = relay_journal.JournalQueue(
                    root, peer_id, self._journal_runtime, sending=sending)
            return self._journal_queues[key]

    def _default_pairing_path(self) -> str:
        """配对名单按**实例键**命名。

        不按 peerId：同机多开的两个实例 peerId 会退让成 ``-2`` 后缀，那是临时
        让步、可能再变；而配对是长期决定，必须挂在不会漂的身份上。
        """
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'adb-manager', f'relay-pairing-{self.node_id}.json')

    def channel_root(self, peer_id: str = '') -> str:
        """请求、结果和终端流都在接收端所属的部署目录中。"""
        return self._peer_roots.get(peer_id, self.root)

    def _read_shared_peers(self) -> dict:
        """只发现当前有效根内的对端，其他池的心跳不参与状态或路由。"""
        peers = relay.read_peers(self.queue.peers_dir)
        skew = relay.observer_clock_skew_ms(peers, self.peer_id)
        # 同一根内，同机重复从机只取一个实例路由。
        if self.role == relay.ROLE_MASTER:
            selected = {}
            for peer_id, beat in sorted(peers.items(), key=lambda item: (
                    item[1].is_stale(self.heartbeat_interval_s, skew_ms=skew),
                    not self.pairing.is_paired(item[1].nodeId), item[1].startedAt, item[0])):
                if beat.role != relay.ROLE_SLAVE:
                    continue
                identity = beat.host.casefold() if beat.host else peer_id
                if identity in selected:
                    peers.pop(peer_id, None)
                else:
                    selected[identity] = peer_id
        self._peer_roots = {peer_id: self.root for peer_id in peers}
        self._peer_deployments = {peer_id: self.pool_id for peer_id in peers}
        self._route_conflicts = set()
        return {peer_id: self._sequence_observer.observe(beat, channel=self.root)
                for peer_id, beat in peers.items()}

    # --- 池注册表 ---
    def _refresh_registry(self) -> None:
        """写/刷新本机在池里的注册项，并顺带确定自己的槽位。

        注册项是**池成员名册**：它让「池里现在有哪些实例」在配对之前就可见，
        也让槽位有多一个可分配的依据。它不影响通讯，写不进去（只读共享目录）也
        不能拖垮启动，所以整段吞异常。

        槽位取「最小的、没被在册实例占用的正整数」。第一次调用时本机还没有注册项，
        量不出自己相对服务器时钟的偏差（判别人的租约要靠它），于是可能有一拍的
        误差；下一拍（2 秒后）本机的注册项已在册，偏差量得准了，分配即自我纠正。
        与之相对，把槽位持久化在本地反而会在换机器/克隆虚拟机时撞车。
        """
        entries = relay.read_registry(self.root)
        mine = entries.get(self.node_id)
        skew = mine.server_skew_ms if mine is not None else 0.0
        slot = relay.allocate_slot(entries, self_node_id=self.node_id, skew_ms=skew)
        self._slot = slot
        relay.write_registry_entry(self.root, relay.RegistryEntry(
            nodeId=self.node_id,
            peerId=self.peer_id,
            role=self.role,
            host=self.host_label,
            fingerprint=self.host_fingerprint,
            slot=slot,
            pid=os.getpid(),
            startedAt=self._started_at,
            lastSeen=relay.now_iso(),
            appVersion=APP_VERSION,
            poolId=self.pool_id,
        ))

    def release_registry(self) -> None:
        """退出时撤掉自己的注册项（谁写谁清）。

        槽位本来由租约回收，但优雅退出时立刻撤掉能让池子视图即时干净、槽位立刻可
        复用。删不掉（只读、被占用）不算故障 —— 下一拍租约到期一样会被回收。
        """
        try:
            os.remove(relay.registry_path(self.root, self.node_id))
        except OSError:
            pass

    def pool_info(self) -> dict:
        """池内全貌快照（供界面展示与排障）。"""
        return relay.pool_snapshot(
            self.root,
            pool_id=self.pool_id,
            self_node_id=self.node_id,
            lease_s=relay.REGISTRY_LEASE_S,
            entries=(self._presence_snapshot[1].get(self.pool_id, {})
                     if self._presence_snapshot is not None else None),
        )

    # --- 生命周期 ---
    def start(self) -> tuple[bool, str]:
        """打开载体、校验清单、启动后台循环。返回 ``(是否成功, 原因)``。"""
        with self._io_lock:
            if self.control_journal:
                try:
                    self._journal_queue(self.peer_id)
                except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
                    return False, f'控制日志本机状态库不可用：{exc}'
            if not self.carrier.open():
                status = self.carrier.status()
                reason = status.get('lastError') or status.get('detail') or '载体不可用'
                return False, reason
            self.queue.ensure_layout()
            # 启用池才把池特性写进清单（``manifest_features`` 是这条规则的唯一出口）。
            relay_carrier.ensure_manifest(
                self.queue, self.carrier.kind, relay.manifest_features(self.pool_id)
            )
            ok, reason = relay_carrier.check_manifest(relay.read_json(self.queue.manifest_path))
            if not ok:
                # 对端要求本版本不认识的特性时明确拒绝启动，而不是半功能运行。
                self.carrier.close()
                return False, reason
        self._started_at = relay.now_iso()
        # 改过「本机标识」再启动时，旧标识的心跳会留在共享目录里。单写者原则禁止删
        # 别人的文件，但那是**本进程自己**写的，谁写谁清。不清理它，界面就会把本机
        # 显示成一台离线「对端」（角色还是改之前那个），看起来就像对端标识不对。
        # 删不掉（只读共享目录、被占用）也不影响启动：evaluate_health 的幽灵过滤兜底。
        try:
            removed = relay.purge_self_heartbeats(
                self.queue.peers_dir,
                fingerprint=self.host_fingerprint,
                pid=os.getpid(),
                started_at=self._started_at,
                keep_peer_id=self.peer_id,
            )
        except Exception:
            removed = []
        if removed:
            self._emit('info', f'已清理本机旧标识的残留心跳：{"、".join(removed)}')
        # 异常退出（崩溃/断电）会在 peers/ 留下本机旧进程的 pulse 日志（1-2MB），
        # 正常退出路径已由 drop_own_pulse 删除；这里按「pid 已不存活」补收。
        try:
            dead_pulses = relay.purge_dead_pulses(self.queue.peers_dir, self.peer_id,
                                                  self_pid=os.getpid())
        except Exception:
            dead_pulses = []
        if dead_pulses:
            self._emit('info', f'已清理本机旧进程的 pulse 心跳日志：{len(dead_pulses)} 个')
        self.queue.prepare_fast_results()
        self._stop_flag.clear()
        self._heartbeat_write_lock = threading.Lock()
        # 心跳独立于设备枚举、目录扫描和请求处理，未配对也持续公告在线。
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f'relay-heartbeat-{self.peer_id}', daemon=True)
        self._heartbeat_thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor_heartbeats,
                                                name=f'relay-heartbeat-read-{self.peer_id}', daemon=True)
        self._monitor_thread.start()
        # 先打一拍：让 health() 立刻是真实结论，而不是 RelayHealth 的占位值。
        self._beat()
        self._thread = threading.Thread(target=self._loop, name=f'relay-{self.peer_id}', daemon=True)
        self._thread.start()
        self._emit('info', f'节点已启动：{self.host_label}，角色 {relay.role_label(self.role)}，'
                          f'载体 {self.carrier.status().get("kindLabel") or self.carrier.kind}')
        return True, 'ok'

    def stop(self) -> None:
        self._stop_flag.set()
        if self.windows_service is not None:
            self.windows_service.close_sessions()
        if self.extension_tools is not None:
            self.extension_tools.close_owner()
        if self.windows_service is not None:
            if self._owns_windows_service:
                self.windows_service.close()
        self._stop_flag.set()
        with self._windows_stream_lock:
            windows_streams = list(self._windows_streams.values())
            self._windows_streams.clear()
        for stream in windows_streams:
            stream.stop()
        with self._windows_stream_lock:
            serial_streams=list(self._serial_streams.values());self._serial_streams.clear()
        for stream in serial_streams:stream.stop()
        monitor_thread = getattr(self, '_monitor_thread', None)
        if monitor_thread is not None:
            monitor_thread.join(timeout=1.0)
        self._monitor_pool.shutdown(wait=False, cancel_futures=True)
        heartbeat_thread = getattr(self, '_heartbeat_thread', None)
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=3.0)
        # 先停日志流：每条流都会 terminate 自己的 adb 子进程并写一次 meta，
        # 顺序反了会留下「进程已死但 meta 还写着 running」的假状态。
        for stream_id in list(self._streams):
            stream = self._streams.pop(stream_id, None)
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:  # 清理路径绝不抛出
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        # 后台发现仍可能持有共享文件句柄；退出前给它们有限时间完成，避免旧任务
        # 在新实例启动后继续扫描，或让文件清理撞上尚未关闭的读取句柄。
        deadline = time.monotonic() + 3.0
        pending_readers = list(self._monitor_futures.values())
        pending_readers.extend((getattr(self, '_discovery_future', None), getattr(self, '_presence_future', None),
                                getattr(self, '_heartbeat_compat_future', None)))
        for future in pending_readers:
            if future is not None:
                try:
                    future.result(timeout=max(0.0, deadline - time.monotonic()))
                except Exception:
                    pass
        # 远端会话：先停本端读取线程，再回收从机侧 PTY（meta 要写进共享目录，
        # 必须赶在 carrier.close() 之前）。顺序反了会留下「PTY 已死但 meta 还
        # 写着 running」的假状态，主控那边就会一直等一个不会再来的输出。
        for key in list(self._readers):
            reader = self._readers.pop(key, None)
            if reader is not None:
                try:
                    reader.stop()
                except Exception:
                    pass
        if self._sessions is not None:
            try:
                self._sessions.stop_all()
            except Exception:
                pass
        self._device_pool.shutdown(wait=False, cancel_futures=True)
        self._discovery_pool.shutdown(wait=False, cancel_futures=True)
        self._presence_pool.shutdown(wait=False, cancel_futures=True)
        self._heartbeat_compat_pool.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False)
        self._terminal_pool.shutdown(wait=False, cancel_futures=True)
        self._archive_pool.shutdown(wait=False, cancel_futures=True)
        # 回执到达客户端后仍可能在归档；停止时给读写任务有限时间释放句柄。
        finish_deadline = time.monotonic() + 3.0
        while self._inflight and time.monotonic() < finish_deadline:
            time.sleep(.01)
        with self._archive_lock:
            archives = list(self._archive_futures)
        for future in archives:
            try:
                future.result(timeout=max(0.0, finish_deadline-time.monotonic()))
            except Exception:
                pass
        self._shared_release_pool.shutdown(wait=False, cancel_futures=True)
        # 正常退出删除本机自己的 pulse 心跳日志（谁写谁清），避免每次重启在
        # peers/ 里留下一个 1-2MB 的 pulse 文件。
        try:
            relay.drop_own_pulse(self.queue.peers_dir, relay.PeerHeartbeat(
                peerId=self.peer_id, role=self.role, pid=os.getpid(),
                nodeId=self.node_id, startedAt=self._started_at))
        except Exception:
            pass
        with self._io_lock:
            self.carrier.close()
        if self.pool_id:
            # 谁写谁清：退出时撤掉自己的注册项，槽位立即可复用。
            self.release_registry()
        self._emit('info', '节点已停止')
        if self._journal_runtime is not None and not self._inflight and not self.is_running():
            self._journal_runtime.close()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- 主循环 ---
    def _heartbeat_loop(self) -> None:
        """单独写本机心跳；单次共享目录故障后继续重试。"""
        while not self._stop_flag.is_set():
            started = time.monotonic()
            try:
                self._write_heartbeat(primary_only=True)
            except Exception as exc:
                self._emit('error', f'心跳写入失败：{type(exc).__name__}: {exc}')
            self._stop_flag.wait(max(0.0, self.heartbeat_interval_s - (time.monotonic() - started)))

    def _monitor_heartbeats(self):
        """已发现的对端只读自己的固定心跳路径，不等部署/设备/名册扫描。"""
        while not self._stop_flag.is_set():
            with self._io_lock:
                peers = list(self._peers.items())
            for peer_id, beat in peers:
                if peer_id == self.peer_id or beat.heartbeatSeq <= 0:
                    continue
                future = self._monitor_futures.get(peer_id)
                if future is None or future.done():
                    self._monitor_futures[peer_id] = self._monitor_pool.submit(
                        self._read_peer_sequence, peer_id, beat, self.channel_root(peer_id))
            self._stop_flag.wait(0.25)

    def _read_peer_sequence(self, peer_id, beat, root):
        started = time.monotonic()
        latest = relay.read_heartbeat_pulse(os.path.join(root, relay.SLOT_PEERS), beat)
        elapsed = round((time.monotonic() - started) * 1000)
        with self._io_lock:
            current = self._peers.get(peer_id)
            if (self._stop_flag.is_set() or current is None
                    or (current.nodeId, current.pid, current.startedAt) != (beat.nodeId, beat.pid, beat.startedAt)):
                return
            self._peers[peer_id] = self._sequence_observer.observe(latest, channel=root)
            self._heartbeat_read_metrics[peer_id] = elapsed
            self._health = self._compute_health()
            age = self._peers[peer_id].age_ms()
        self._notice_slow_heartbeat('读取', peer_id, elapsed, latest.heartbeatSeq, age)

    def _notice_slow_heartbeat(self, operation, peer_id, elapsed, sequence, age=None):
        """慢访问限频记录；日志不占共享目录锁，也不改变在线判定。"""
        if elapsed < 1000:
            return
        now = time.monotonic()
        key = (operation, peer_id)
        with self._heartbeat_diagnostics_lock:
            previous = self._heartbeat_slow_notices.get(key)
            if previous is not None and now - previous < 30:
                return
            self._heartbeat_slow_notices[key] = now
        suffix = f'，距最后观测推进 {max(0, round(age))}ms' if age is not None else ''
        self._emit('info', f'心跳{operation}耗时 {elapsed}ms：{peer_id}，序号 {sequence}{suffix}')

    def _beat(self, *, refresh_devices=True) -> None:
        """一次心跳：刷新本机设备 → 写心跳 → 读对端 → 重算状态。

        抽出来是为了让 ``start()`` 也能先打一拍：不这么做，新节点在循环首轮之前
        ``health()`` 还是占位值（``state=unconfigured``），刚保存完配置的界面会先
        闪一下「未启用」，甚至停在那个结论上（状态推送去抖只认变化）。
        """
        # 注册表在共享目录上，不放进 ``_io_lock``：那是网络 IO，占着锁会让界面拉
        # 状态的线程一起卡住。
        if self._stop_flag.is_set():
            return
        if refresh_devices:
            self._refresh_presence()
        # 设备枚举可能等待 adb 超时，不能占用流与控制回执的锁。
        if refresh_devices:
            self._refresh_devices()
        if self._stop_flag.is_set():
            return
        # 慢共享目录扫描不能占住准入及终端请求的锁。
        if refresh_devices:
            heartbeat_thread = getattr(self, '_heartbeat_thread', None)
            if heartbeat_thread is None or not heartbeat_thread.is_alive():
                self._write_heartbeat()
        peers = self._read_shared_peers()
        with self._io_lock:
            self._peers = peers
            self._health = self._compute_health()
        self._notice_state_change()

    def _refresh_presence(self) -> None:
        """刷新池注册项。失败不影响通讯（注册表只是名册），只在流水里报一次。"""
        if self._stop_flag.is_set():
            # 未启用池：目录结构与老版本**逐字节一致**，连 registry/ 都不建。
            # 这是「poolId 为空 = 今天的行为」这条兼容承诺的一部分。
            return
        try:
            if self.pool_id:
                self._refresh_registry()
            roots = relay.deployment_roots(self.base_root)
            # 其他池仍可出现在部署选择器中，但其注册项不能成为当前根的配对候选。
            registries = ({self.pool_id: relay.read_registry(self.root)} if self.pool_id else {})
            if not self._stop_flag.is_set():
                # 完整替换快照，状态线程只读内存；慢名册不能拖住连接状态刷新。
                self._presence_snapshot = (roots, registries)
        except Exception as exc:
            if not self._registry_warned:
                self._registry_warned = True
                self._emit('info', f'池注册表暂不可写（不影响通讯）：{type(exc).__name__}: {exc}')

    def deployment_ids(self) -> list[str]:
        return (list(self._presence_snapshot[0]) if self._presence_snapshot is not None
                else list(relay.deployment_roots(self.base_root)))

    def _loop(self) -> None:
        next_beat = 0.0
        while not self._stop_flag.is_set():
            try:
                if time.time() >= next_beat:
                    # 慢 adb devices 不能让控制队列和目录浏览等到下一轮。
                    if self._device_refresh_future is None or self._device_refresh_future.done():
                        self._device_refresh_future = self._device_pool.submit(self._refresh_devices)
                    if self._presence_future is None or self._presence_future.done():
                        self._presence_future = self._presence_pool.submit(self._refresh_presence)
                    if self._discovery_future is None or self._discovery_future.done():
                        self._discovery_future = self._discovery_pool.submit(self._beat, refresh_devices=False)
                    next_beat = time.time() + self.heartbeat_interval_s
                if self.role == relay.ROLE_SLAVE:
                    self._release_shared_on_master_loss()
                    self._process_requests()
            except Exception as exc:  # 单次异常不能让循环死掉
                self._emit('error', f'中转循环异常：{type(exc).__name__}: {exc}')
            self._stop_flag.wait(self.poll_interval_s)

    def _release_shared_on_master_loss(self):
        sessions = self._sessions
        if sessions is None or not getattr(sessions.backend, 'shared', False):
            return
        # 用已有心跳观测检查，主循环不增加任何共享目录读写。
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        online = any(beat.role == relay.ROLE_MASTER and not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)
                     for beat in self._admissible_peers().values())
        if online and not self._health.conflict:
            return
        if not sessions.snapshot():
            return
        future = self._shared_release_future
        if future is None or future.done():
            self._shared_release_future = self._shared_release_pool.submit(sessions.stop_all)

    def _refresh_devices(self) -> None:
        with self._device_refresh_lock:
            self._refresh_devices_once()

    def _refresh_devices_once(self) -> None:
        """刷新本机设备列表。

        带**去抖**：``adb devices -l`` 在 adb server 忙碌时会瞬时返回空列表或漏掉
        某台设备（实测首次调用即可复现）。若直接采信，一次抖动就会让「本机设备」
        在心跳里变成 0 台 —— 对端设备栏会凭空少一台再冒出来，从机自己还会把随后
        的请求误判成「没有匹配的设备」。

        所以：设备必须**连续 ``DEVICE_MISS_THRESHOLD`` 次枚举都没出现**才算真的
        离线，与 ``adb_worker.py`` 对离线设备的去抖语义一致。枚举**报错**时不计
        miss —— 列表本身不可信，不该据此丢设备。
        """
        if not self.local_devices_enabled:
            # 屏蔽本机设备：不去枚举，也不保留任何已知设备。否则对端会收到一份
            # 本机根本没打算共享（甚至本机根本不该看见）的设备清单。
            self._devices = []
            self._device_miss.clear()
            self._device_error = ''
            return
        devices, error = list_devices(self.adb_path)
        self._device_error = error
        if error:
            self._emit('error', f'枚举本机设备失败：{error}')
            return

        seen = {str(device.get('key') or ''): device for device in devices if device.get('key')}
        merged: list[dict] = []
        for key, device in seen.items():
            self._device_miss.pop(key, None)
            merged.append(device)
        # 本次没出现的已知设备：累计 miss，够阈值才剔除（保留上一次的字段，
        # 这样对端看到的仍是最后一台已知设备的状态，而不是空白记录）。
        for device in self._devices:
            key = str(device.get('key') or '')
            if not key or key in seen:
                continue
            misses = self._device_miss.get(key, 0) + 1
            self._device_miss[key] = misses
            if misses < DEVICE_MISS_THRESHOLD:
                self._emit('info', f'设备 {key} 未出现在枚举结果中（{misses}/{DEVICE_MISS_THRESHOLD}）')
                merged.append(device)
            else:
                self._emit('info', f'设备 {key} 连续 {misses} 次未出现，按离线移除')
                self._device_miss.pop(key, None)
        self._devices = merged

    def _managed_devices(self) -> list[dict]:
        """本机对外负责的设备。``managed_serials`` 为空表示管全部。"""
        if not self.managed_serials:
            return list(self._devices)
        wanted = set(self.managed_serials)
        return [
            device for device in self._devices
            if device.get('serial') in wanted or device.get('key') in wanted
        ]

    def _write_heartbeat(self, *, primary_only=False) -> None:
        heartbeat_lock = getattr(self, '_heartbeat_write_lock', None)
        if heartbeat_lock is None:
            self._write_heartbeat_once(primary_only=primary_only)
        else:
            with heartbeat_lock:
                self._write_heartbeat_once(primary_only=primary_only)

    def _write_heartbeat_once(self, *, primary_only=False) -> None:
        # 被停止的发现任务不能晚到写回心跳，复活旧实例或干扰退出清理。
        if self._stop_flag.is_set():
            return
        self._heartbeat_sequence += 1
        beat = relay.PeerHeartbeat(
            peerId=self.peer_id,
            role=self.role,
            appVersion=APP_VERSION,
            pid=os.getpid(),
            startedAt=self._started_at,
            heartbeatAt=relay.now_iso(),
            heartbeatSeq=self._heartbeat_sequence,
            uptimeSeconds=max(0, int(time.monotonic() - APP_STARTED_MONOTONIC)),
            devices=[dict(device) for device in self._managed_devices()],
            fingerprint=self.host_fingerprint,
            carrier=self.carrier.kind,
            linkRttMs=float(self.carrier.status().get('latencyMs') or 0.0),
            host=self.host_label,
            # 实例级身份：对端靠它做准入门判（配对按实例键匹配，不按可变的 peerId）。
            nodeId=self.node_id,
            nodeSlot=self._slot,
            controlJournal=relay_journal.VERSION if self.control_journal else 0,
            connectionScope=self._connection_scope,
        )
        started = time.monotonic()
        relay.write_heartbeat(self.queue.peers_dir, beat, snapshot=True, pulse_only=True)
        self._notice_slow_heartbeat('写入', self.peer_id,
                                   round((time.monotonic() - started) * 1000), beat.heartbeatSeq)
        if primary_only:
            pending = self._heartbeat_compat_future
            if pending is None or pending.done():
                self._heartbeat_compat_future = self._heartbeat_compat_pool.submit(self._publish_heartbeat_compat, beat)
        else:
            self._publish_heartbeat_compat(beat)

    def _publish_heartbeat_compat(self, beat) -> None:
        # 旧版 JSON、快照及清理可能慢；不能阻塞真正用于在线判定的追加心跳。
        with self._heartbeat_compat_lock:
            if not self._stop_flag.is_set():
                relay.write_heartbeat(self.queue.peers_dir, beat, snapshot=True, publish_pulse=False)

    def accepted_master_ids(self) -> list[str]:
        """从机接受哪些主控。名单 = 本机信任名单，未确认的主控一律拒绝。"""
        return [record.peerId for record in self.trust.items() if record.peerId]

    def paired_node_ids(self) -> set:
        """已配对的**实例键**集合。配对门槛为空集时（未启用）返回 ``None`` 语义的
        全集由 ``_compute_health`` 处理，这里只回答事实。"""
        return self.pairing.paired_node_ids()

    def _node_id_for(self, peer_id: str) -> str:
        """由显示名反查实例键。查不到返回空串（调用方按 fail-closed 处理）。"""
        beat = self._peers.get(str(peer_id or ''))
        return beat.nodeId if beat is not None else ''

    def _pairing_blocks(self, peer_id: str) -> str:
        """准入判定：返回拒绝原因，空串表示放行。

        与 ``_authorize`` 的区别是**判定方向**：这里用于「我要不要主动找它」，
        那边用于「它找过来我要不要理」。两处都必须判，缺一个就漏：
        只判投递，别人仍能往本机分区写请求；只判受理，本机仍会往未配对实例的
        分区里留垃圾请求。
        """
        if peer_id in self._route_conflicts:
            return f'对端路由标识 {peer_id} 重复，请更新重名实例后重新配对'
        cross_deployment = self.channel_root(peer_id) != self.root
        if not self.pairing_required and not cross_deployment:
            return ''
        node_id = self._node_id_for(peer_id)
        if not node_id:
            return f'对端 {peer_id} 未上报实例键（版本过旧），本机已启用配对'
        if not self.pairing.is_paired(node_id):
            record = self.pairing.get(node_id)
            state = record.stateLabel if record else '未配对'
            return f'对端 {peer_id}（实例 {node_id}）当前为「{state}」，未配对前不通讯'
        return ''

    def _compute_health(self) -> relay.RelayHealth:
        link = self.carrier.status()
        health = relay.evaluate_health(
            enabled=True,
            role=self.role,
            self_id=self.peer_id,
            peers={peer_id: beat for peer_id, beat in self._peers.items()
                   if self.channel_root(peer_id) == self.root
                   or self.pairing.is_paired(beat.nodeId)},
            link=link,
            trust=self.trust,
            interval_s=self.heartbeat_interval_s,
            accepted_master_ids=self.accepted_master_ids(),
            carrier_kind=self.carrier.kind,
            local_device_count=len(self._managed_devices()),
            self_fingerprint=self.host_fingerprint,
            self_pid=os.getpid(),
            self_started_at=self._started_at,
            # 准入门槛：未配对的实例不进对端列表、不进角色仲裁，只进 unpairedPeers。
            pairing_enabled=bool(self.pairing_required),
            paired_node_ids=(self.paired_node_ids() if self.pairing_required else None),
        )
        if self.passive_slave and health.state in (relay.STATE_WAITING_PEER, relay.STATE_PENDING_CONFIRM):
            health.hint = '本机已就绪，等待主控选择并确认连接；从机无需选择对端'
        for view in health.peers:
            view.deploymentId = self._peer_deployments.get(view.peerId, self.pool_id)
            view.displayLabel = self._peer_display_label(view.peerId)
        for item in health.unpairedPeers:
            item['displayLabel'] = self._peer_display_label(item['peerId'])
        # 用户看到的标识统一为「机器名 / 部署」，内部路由名只用于协议选路。
        if self._peers:
            pattern = '|'.join(re.escape(peer_id) for peer_id in sorted(self._peers, key=len, reverse=True))
            health.hint = re.sub(pattern, lambda match: self._peer_display_label(match.group()), health.hint)
        return health

    def _peer_display_label(self, peer_id: str) -> str:
        beat = self._peers.get(peer_id)
        machine = beat.host if beat and beat.host else '未知机器'
        deployment = self._peer_deployments.get(peer_id, self.pool_id) or '默认部署'
        return f'{machine} / {deployment}'

    def _connection_endpoint(self, peer_id: str) -> tuple:
        beat = self._peers.get(peer_id)
        return (self.channel_root(peer_id), beat.nodeId, beat.startedAt) if beat else ()

    def _mark_connection_verified(self, peer_id: str) -> None:
        with self._io_lock:
            self._connection_verified[peer_id] = (self._connection_endpoint(peer_id), time.monotonic())
            self._connection_verified_at[peer_id] = relay.now_iso()

    def _connection_peer_online(self, peer_id: str) -> bool:
        return any(view.peerId == peer_id and view.online and view.role != self.role
                   for view in self._health.peers)

    def connection_ready(self, peer_id: str) -> bool:
        """在线、主控确认后连接验证仍新鲜；不依赖设备数量或 PTY。"""
        if self._pairing_blocks(peer_id) or not self._connection_peer_online(peer_id) or self._health.conflict:
            return False
        if not self.trust.check(peer_id, self._peer_fingerprint(peer_id))[0]:
            return False
        endpoint, _ = self._connection_verified.get(peer_id, ((), 0.0))
        # 验证绑定实例与启动代次；在线期间的瞬时探测超时不能拆掉已验证会话。
        return bool(endpoint and endpoint == self._connection_endpoint(peer_id))

    def connection_check_due(self, peer_id: str) -> bool:
        return time.monotonic() >= self._connection_check_after.get(peer_id, 0.0)

    def verify_connection(self, peer_id: str) -> relay.Result:
        """独立于设备的准入往返验证，由从机 _authorize 作最后裁决。"""
        endpoint = self._connection_endpoint(peer_id)
        record = self.pairing.get(self._node_id_for(peer_id))
        generation = record.connectionId if record else ''
        result = self.submit(peer_id, relay.KIND_CHECK_CONNECTION, timeout=10.0)
        if result.ok and result.extra.get('nodeId') != self._node_id_for(peer_id):
            result = relay.Result(reqId=result.reqId, status=relay.STATUS_ERROR,
                                  error='连接验证的对端实例不一致，请重新发现对端')
        with self._io_lock:
            current = self.pairing.get(self._node_id_for(peer_id))
            if (self._stop_flag.is_set() or endpoint != self._connection_endpoint(peer_id)
                    or generation != (current.connectionId if current else '')):
                # 旧实例、旧确认代次的回执不能清掉当前连接，也不能推迟新的验证。
                return result
            self._connection_checks[peer_id] = (endpoint, result.status, result.error or '')
            if result.ok and endpoint == self._connection_endpoint(peer_id):
                # 验证成功：连接已可用，退避到 OK 间隔再复查，避免每次成功验证
                # 都产生一对请求/回执文件（写放大）。失败/未验证路径保持密集重试。
                self._mark_connection_verified(peer_id)
                self._connection_check_after[peer_id] = time.monotonic() + CONNECTION_VERIFY_OK_INTERVAL_S
            else:
                self._connection_check_after[peer_id] = time.monotonic() + CONNECTION_VERIFY_RETRY_INTERVAL_S
                if not result.ok and not result.extra.get('localTimeout'):
                    self._connection_verified.pop(peer_id, None)
                    self._connection_verified_at.pop(peer_id, None)
        return result

    def connection_info(self, peer_id: str, node_id: str = '') -> dict:
        """每个实例只有一个可展示的连接结论；确认、在线、验证与设备数分开。"""
        beat = self._peers.get(peer_id)
        same_instance = bool(beat and (not node_id or beat.nodeId == node_id))
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        online = bool(same_instance and not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew))
        actual_node = node_id or (beat.nodeId if beat else '')
        local_confirmed = bool(actual_node and self.pairing.is_paired(actual_node)
                               and same_instance and self.trust.check(peer_id, beat.fingerprint)[0])
        common = {
            'online': online,
            'localConfirmed': local_confirmed,
            'lastHeartbeatAgeMs': (max(0.0, beat.age_ms(skew_ms=skew)) if same_instance else None),
            'deviceCount': len(beat.devices) if same_instance else 0,
            'verifiedAt': '',
            'heartbeatSeq': beat.heartbeatSeq if same_instance else 0,
            'heartbeatReadMs': self._heartbeat_read_metrics.get(peer_id),
            'uptimeSeconds': beat.uptimeSeconds if same_instance else 0,
        }

        def result(state: str, reason: str, action: str, code: str) -> dict:
            return {**common, 'connectionState': state, 'connectionReason': reason,
                    'reasonCode': code, 'nextAction': action}

        if not online:
            return result('offline', '对端离线，确认关系保留；上线后自动验证',
                          '启动对端并检查两端共享目录是否指向同一位置', 'peer-offline')
        if self._health.conflict or beat.role == self.role:
            return result('role-mismatch', '双方角色相同或主控角色冲突',
                          '让操作设备的一端为主控、连接设备的一端为从机', 'role-mismatch')
        if not local_confirmed:
            if self.passive_slave:
                return result('waiting-master', '本机已就绪，等待主控选择并确认连接',
                              '保持本机运行，在主控端选择本机并确认连接', 'waiting-master')
            block = self._pairing_blocks(peer_id)
            reason = block or '待本机确认对端身份'
            return result('local-confirmation', reason,
                          '核对机器名和部署后，在本机点击「确认连接」', 'local-confirmation')
        if self.connection_ready(peer_id):
            common['verifiedAt'] = self._connection_verified_at.get(peer_id, '')
            return result('connected', '连接已验证，通讯可用',
                          '在主控端管理设备' if self.role == relay.ROLE_MASTER else '保持本机运行，设备操作在主控端完成',
                          'verified')
        check = self._connection_checks.get(peer_id, ((), '', ''))
        if check[0] == self._connection_endpoint(peer_id) and check[2]:
            reason = check[2]
            if check[1] == relay.STATUS_REJECTED and ('未配对' in reason or '未确认' in reason or '已解除' in reason):
                return result('waiting-peer-confirmation', reason,
                              '在主控端重新确认连接；旧版从机需升级后才能自动接受', 'peer-not-confirmed')
            return result('check-failed', reason,
                          '检查对端状态和共享目录后等待自动重试', 'verification-failed')
        return result('checking', '等待主控连接验证' if self.passive_slave else '主控已确认，正在验证连接',
                      '等待自动验证；若持续未接通，请检查对端状态和共享目录', 'verifying')

    def _notice_state_change(self) -> None:
        if self._health.state != self._last_state:
            self._last_state = self._health.state
            self._emit('state', f'连接状态 → {self._health.stateText}（{self._health.hint}）')

    # --- 从机：请求处理 ---
    def _process_requests(self) -> None:
        if self.control_journal and not isinstance(self.queue, relay_journal.JournalQueue):
            self.queue = self._journal_queue(self.peer_id)
        if isinstance(self.queue, relay_journal.JournalQueue):
            self.queue.journal_sources = [beat.nodeId for beat in self._peers.values() if beat.nodeId]
        pending = self.queue.pending_requests(source_peer_ids=list(self._peers))
        if isinstance(self.queue, relay_journal.JournalQueue):
            notices = set(self._journal_runtime.notices.values())
            for notice in notices - self._journal_notices:
                self._emit('error', notice)
            self._journal_notices = notices
        for path, request in pending:
            if request.reqId in self._inflight:
                continue
            # 幂等：结果已存在说明处理过（或处理到一半崩了），不重复执行。
            if self.queue.has_result(request.reqId):
                self.queue.archive_file(path)
                continue
            if request.is_expired():
                # 过期即作废（fail-closed）：防止从机离线期间堆积的命令在恢复后
                # 批量执行 —— 比如一条早已过时的 reboot。
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_EXPIRED,
                        error='请求已过期，未执行', agentId=self.peer_id,
                        finishedAt=relay.now_iso(),
                    ),
                    path,
                )
                self._emit('reject', f'丢弃过期请求 {request.reqId}（{request.kind}）')
                continue
            if isinstance(self.queue, relay_journal.JournalQueue) and not self.queue.claim_request(path, request):
                continue
            self._inflight.add(request.reqId)
            executor = self._terminal_pool if request.kind in (relay.KIND_OPEN_SESSION, relay.KIND_WINDOWS, relay.KIND_EXTENSION_TOOL) else self._pool
            executor.submit(self._serve_request, path, request)

    def _serve_request(self, path: str, request: relay.Request) -> None:
        try:
            reason = self._authorize(request)
            if reason:
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_REJECTED, error=reason,
                        agentId=self.peer_id, finishedAt=relay.now_iso(),
                    ),
                    path,
                )
                self._emit('reject', f'拒绝 {request.kind} 请求（来源 {request.peer}）：{reason}')
                return
            if request.kind == relay.KIND_CHECK_CONNECTION:
                self._mark_connection_verified(request.peer)
                self._write_result(relay.Result(
                    reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                    extra={'nodeId': self.node_id}, finishedAt=relay.now_iso()), path)
            elif request.kind == relay.KIND_CLOSE_CONNECTION:
                self.unpair_peer(self._node_id_for(request.peer))
                self._write_result(relay.Result(
                    reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                    finishedAt=relay.now_iso()), path)
            elif request.kind == relay_transfer.KIND_TRANSFER:
                self._serve_transfer(path, request)
            elif request.kind == relay_transfer.KIND_CLEANUP:
                directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer,
                                                        request.payload.get('transferId'))
                # 只删除从机自己写的下载包；上传包由主控清理。
                package = directory / 'download.zip'
                package.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
                self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
                                               agentId=self.peer_id), path)
            elif request.kind == relay.KIND_EXEC:
                self._serve_exec(path, request)
            elif request.kind == relay.KIND_WINDOWS:
                self._serve_windows(path, request)
            elif request.kind == relay.KIND_EXTENSION_TOOL:
                self._serve_extension_tool(path, request)
            elif request.kind == relay.KIND_WINDOWS_TRANSFER:
                self._serve_windows_transfer(path, request)
            elif request.kind == relay.KIND_START_LOG:
                self._serve_start_log(path, request)
            elif request.kind == relay.KIND_STOP_LOG:
                self._serve_stop_log(path, request)
            elif request.kind == relay.KIND_OPEN_SESSION:
                self._serve_open_session(path, request)
            elif request.kind == relay.KIND_SESSION_TAKE:
                ok, message = self._sessions.take_control(
                    request.payload.get('streamId', ''), request.payload.get('generation', '')) if self._sessions else (False, '终端服务未启用')
                self._write_result(relay.Result(reqId=request.reqId,
                    status=relay.STATUS_OK if ok else relay.STATUS_REJECTED,
                    error='' if ok else message, stdout=message, agentId=self.peer_id), path)
            elif request.kind == relay.KIND_SESSION_RESIZE:
                self._serve_session_resize(path, request)
            elif request.kind == relay.KIND_CLOSE_SESSION:
                self._serve_close_session(path, request)
            else:
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_REJECTED,
                        error=f'本版本未实现该请求类型：{request.kind}', agentId=self.peer_id,
                        finishedAt=relay.now_iso(),
                    ),
                    path,
                )
        except Exception as exc:
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=f'从机处理异常：{type(exc).__name__}: {exc}', agentId=self.peer_id,
                    finishedAt=relay.now_iso(),
                ),
                path,
            )
        finally:
            self._inflight.discard(request.reqId)
            if (self._stop_flag.is_set() and not self._inflight
                    and self._journal_runtime is not None and not self.is_running()):
                self._journal_runtime.close()

    def _windows_owner(self, request, action):
        """电脑权限独立于 ADB；只接受当前已确认实例及连接代次。"""
        payload = request.payload or {}
        if self._stop_flag.is_set():
            raise PermissionError('中转节点已停止，拒绝过时的电脑请求')
        source = self._node_id_for(request.peer)
        record = self.pairing.get(source)
        if (not source or not record or not record.is_paired
                or payload.get('sourceNodeId') != source
                or payload.get('targetNodeId') != self.node_id
                or payload.get('connectionId') != record.connectionId):
            raise PermissionError('Windows 请求的实例或连接确认代次无效')
        if action != 'capabilities' and not self.allow_windows:
            raise PermissionError('从机未允许 Windows PowerShell，请在从机「窗口 → PowerShell」中启用')
        if self.windows_service is None:
            from windows_executor import WindowsService
            with self._io_lock:
                if self.windows_service is None:
                    from pathlib import Path
                    self.windows_service = WindowsService(self.windows_directory or
                        Path(self._default_trust_path()).parent / ('windows-jobs-'+self.node_id))
                    self._owns_windows_service = True
        return source + ':' + record.connectionId if action.startswith('session_') else source

    def _extension_owner(self, request, *, check_expiry=True):
        """控制请求与持续流共用执行端准入；流不延长原请求TTL。"""
        payload = request.payload or {}
        reason=self._authorize(request);source=self._node_id_for(request.peer);record=self.pairing.get(source)
        if (reason or (check_expiry and request.is_expired()) or self.role!=relay.ROLE_SLAVE or not record or not record.is_paired
                or payload.get('sourceNodeId')!=source or payload.get('targetNodeId')!=self.node_id
                or not record.connectionId or payload.get('connectionId')!=record.connectionId):
            raise PermissionError(reason or '扩展工具请求的实例、连接代次或有效期无效')
        return source+':'+record.connectionId if payload.get('tool')=='serial' else source

    def _serve_extension_tool(self, path, request):
        """固定扩展接口；串口打开时协商持续流，不逐字符投递请求。"""
        payload=request.payload or {}
        authorize=lambda:self._extension_owner(request)
        if self.extension_tools is None:
            raise RuntimeError('从机未启用扩展工具接口，请升级从机')
        data = self.extension_tools.call(payload.get('tool'), payload.get('action'), payload.get('args'),
                                         remote=True, authorize=authorize, caller=payload.get('caller', 'agent'))
        if (payload.get('tool')=='serial' and payload.get('action')=='open'
                and payload.get('serialStreamProtocol')==1 and data.get('data',{}).get('state')=='running'):
            from serial_stream import SlaveSerialStream
            opened=data['data'];identifier=opened['sessionId']
            with self._windows_stream_lock:
                self._serial_streams={key:stream for key,stream in self._serial_streams.items() if stream.thread.is_alive()}
                stream=self._serial_streams.get(identifier)
                if stream is None or not stream.thread.is_alive():
                    self._serial_streams[identifier]=SlaveSerialStream(self,request,opened)
            opened.update(serialStreamProtocol=1,serialWritePacingProtocol=1,streamId='serial-'+identifier)
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
            agentId=self.peer_id, finishedAt=relay.now_iso(), extra={'data': data}), path)

    def _serve_windows(self, path, request):
        payload = request.payload or {}
        action = str(payload.get('action') or '')
        owner = self._windows_owner(request, action)
        data = self.windows_service.call(action, dict(payload.get('args') or {}), owner)
        if action == 'session_open' and payload.get('streamProtocol') == 1:
            from windows_stream import SlaveStream
            identifier = data['sessionId']
            try:
                with self._windows_stream_lock:
                    self._windows_owner(request, 'session_write')
                    self._windows_streams = {key: stream for key, stream in self._windows_streams.items()
                                            if stream.thread.is_alive()}
                    self._windows_streams[identifier] = SlaveStream(self, request, identifier, owner)
                data.update(streamProtocol=1, streamId='win-' + identifier)
            except Exception:
                self.windows_service.session_action('session_close', {'sessionId': identifier}, owner)
                raise
        elif action == 'session_close':
            with self._windows_stream_lock:
                stream = self._windows_streams.pop((payload.get('args') or {}).get('sessionId'), None)
            if stream is not None:
                stream.stop()
        if action == 'capabilities':
            data['allowed'] = self.allow_windows
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
            agentId=self.peer_id, finishedAt=relay.now_iso(), extra={'data': data}), path)

    def _serve_windows_transfer(self, path, request):
        """复用已验证包协议，目标是电脑暂存目录，不执行 ADB。"""
        self._windows_owner(request, 'files')
        payload = request.payload or {}
        operation = payload.get('operation')
        relative_paths = payload.get('remotePaths')
        if operation not in ('push', 'pull') or not isinstance(relative_paths, list) or not relative_paths:
            raise ValueError('电脑文件传输参数无效')
        directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer, payload.get('transferId'))
        with tempfile.TemporaryDirectory(prefix='DevMux-windows-transfer-') as work:
            if operation == 'push':
                if len(relative_paths) != 1:
                    raise ValueError('上传只能指定一个目标目录')
                target = self.windows_service.file_path(relative_paths[0], create=True)
                paths = relay_transfer.receive_package(directory / 'upload.zip', work, payload.get('package') or {})
                # 大包读取之后再次检查撤销/连接换代，不能执行过时的落盘动作。
                reason = self._authorize(request)
                if reason:
                    raise PermissionError(reason)
                self._windows_owner(request, 'files')
                for source in paths:
                    existing = self.windows_service.file_path(str(Path(relative_paths[0]) / source.name))
                    if existing.exists():
                        for item in relay_transfer.tree(existing):
                            pass  # 校验已有目标树，防止 copytree 追随旧链接。
                relay_transfer.save_download(paths, target)
                extra = {'destination': str(target)}
            else:
                paths = [self.windows_service.file_path(value) for value in relative_paths]
                extra = {'package': relay_transfer.publish_upload(paths, directory / 'download.zip')}
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK, exitCode=0,
            stdout='电脑文件传输完成', extra=extra, agentId=self.peer_id, finishedAt=relay.now_iso()), path)

    def _authorize(self, request: relay.Request) -> str:
        """从机侧准入：返回拒绝原因，空字符串表示放行。"""
        if self._stop_flag.is_set():
            return '中转节点已停止，拒绝执行排队请求'
        if not request.peer:
            return '请求缺少来源标识'
        if not relay.valid_peer_id(request.peer):
            return f'来源标识非法：{request.peer!r}'
        if request.peer == self.peer_id:
            return '拒绝来源标识与本机相同的请求'
        if self.passive_slave:
            reason = self._accept_master_connection(request)
            if reason:
                return reason
        # 准入门槛排在仲裁之前：未配对的实例连对端都还不是，让它进仲裁等于让未授权
        # 的实例投票（它的角色字段还可能是「主控」，会被认成当前主控）。
        block = self._pairing_blocks(request.peer)
        if block:
            return block
        arbitration = relay.arbitrate_roles(
            self.peer_id, self.role, self._admissible_peers(),
            interval_s=self.heartbeat_interval_s,
            accepted_master_ids=self.accepted_master_ids(),
            skew_ms=relay.observer_clock_skew_ms(self._peers, self.peer_id),
        )
        if arbitration.conflict:
            return f'{arbitration.reason}；已暂停透传'
        if arbitration.master and request.peer != arbitration.master:
            return f'来源 {request.peer} 不是当前主控端（当前主控：{arbitration.master}）'
        trusted, reason = self.trust.check(request.peer, self._peer_fingerprint(request.peer))
        if not trusted:
            return f'主控端 {request.peer} 未确认：{reason}'
        return ''

    def _accept_master_connection(self, request: relay.Request) -> str:
        """主控确认后，只有无设备操作的验证请求能够建立从机准入记录。"""
        with self._io_lock:
            beat = self._peers.get(request.peer)
            skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
            if (beat is None or beat.role != relay.ROLE_MASTER or not beat.nodeId
                    or not beat.fingerprint or beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)):
                return '尚未发现在线且身份完整的主控，请等待主控重新验证'
            if request.peer in self._route_conflicts:
                return '主控路由标识重复，不能建立连接'
            payload = request.payload or {}
            connection_id = str(payload.get('connectionId') or '')
            if (payload.get('sourceNodeId') != beat.nodeId
                    or payload.get('targetNodeId') != self.node_id or not connection_id):
                return '连接实例或确认代次不匹配，请在主控端重新确认；旧版主控需升级'
            source_scope = beat.connectionScope
            if self._connection_scope and (not re.fullmatch(r'[0-9a-f]{16}', source_scope)
                    or not connection_id.startswith(source_scope + '.')):
                return '连接代次属于其他共享池，拒绝旧链接；请在当前池重新确认连接'
            record = self.pairing.get(beat.nodeId)
            if record and connection_id in record.retiredConnectionIds:
                return '旧连接确认已经失效，不能恢复设备通讯'
            if record and record.connectionId == connection_id:
                if not record.is_paired:
                    return '连接已解除，旧确认不能自动恢复，请在主控端重新确认连接'
                if record.fingerprint != beat.fingerprint:
                    return '主控身份发生变化，请在主控端重新确认连接'
                return ''
            if request.kind != relay.KIND_CHECK_CONNECTION:
                return '尚未验证主控连接，不能执行设备操作'
            candidates = {**self._admissible_peers(), request.peer: beat}
            arbitration = relay.arbitrate_roles(
                self.peer_id, self.role, candidates, interval_s=self.heartbeat_interval_s,
                accepted_master_ids=self.accepted_master_ids(), skew_ms=skew)
            if arbitration.conflict:
                return f'{arbitration.reason}；请先解除原主控连接'
            # 代次变化必须关闭旧 PTY，防止旧输入流继续操作设备。
            if record and self._sessions is not None:
                self._sessions.stop_all()
            if record and self.windows_service is not None:
                self.windows_service.close_sessions(beat.nodeId)
            if record and self.extension_tools is not None:
                self.extension_tools.close_owner(beat.nodeId)
            self.pair_peer(beat.nodeId, peer_id=request.peer, connection_id=connection_id)
            self._emit('trust', f'已自动接受主控连接：{self._peer_display_label(request.peer)}')
            return ''

    def _admissible_peers(self) -> dict:
        """参与角色仲裁的对端集合：未配对的实例一律排除。

        ``self._peers`` 是**总线上所有**心跳（含未配对），读侧过滤只在
        ``evaluate_health`` 里做，所以仲裁要单独过滤一次 —— 否则未配对实例的
        「主控」角色会挤进仲裁结果，把正常的两端判成角色冲突（而冲突是 fail-closed
        的，会直接停摆透传）。
        """
        return {
            peer_id: beat for peer_id, beat in self._peers.items()
            if not self._pairing_blocks(peer_id)
        }

    def _peer_fingerprint(self, peer_id: str) -> str:
        beat = self._peers.get(peer_id)
        return beat.fingerprint if beat else ''

    def _match_device(self, want_key: str, want_serial: str) -> Optional[dict]:
        candidates = self._managed_devices()
        for item in candidates:
            if want_key and item.get('key') == want_key:
                return item
        for item in candidates:
            if want_serial and item.get('serial') == want_serial:
                return item
        return None

    def _resolve_request_device(self, request: relay.Request) -> Optional[dict]:
        """把请求里的设备描述解析成本机设备记录。

        解析不到时**强制再刷一次设备列表**：``adb devices`` 在 adb server 忙碌
        时会瞬时返回空列表（实测首次调用即可复现），若只用 1~2 秒前的轮询快照
        判断，就会把「设备明明在」的请求误判成「没有匹配的设备」。请求是重要
        事件，值得为它多打一次 adb。
        """
        device = dict(request.device or {})
        want_key = str(device.get('key') or '')
        want_serial = str(device.get('serial') or '')
        resolved = self._match_device(want_key, want_serial)
        if resolved is not None:
            return resolved
        self._refresh_devices()
        return self._match_device(want_key, want_serial)

    def _describe_devices(self) -> str:
        """诊断用：当前本机设备列表与最近一次枚举错误。"""
        keys = [device.get('key', '') for device in self._managed_devices()]
        detail = f'本机当前设备：{keys or "（空）"}'
        if self._device_error:
            detail += f'；最近一次枚举报错：{self._device_error}'
        return detail

    def _serve_transfer(self, path, request):
        """文件请求仍经从机准入和命令白名单，不接受主控指定本机路径。"""
        payload = request.payload
        operation = payload.get('operation')
        if operation not in ('push', 'pull'):
            self._fail_request(path, request, '文件操作非法', rejected=True)
            return
        allowed, reason = relay.check_command([operation], allow_shell=self.allow_shell,
                                             allowed_subcommands=self.allowed_subcommands)
        if not allowed:
            self._fail_request(path, request, f'文件传输被策略拒绝：{reason}；请在从机命令白名单加入 {operation}', rejected=True)
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(path, request, '本机没有匹配的设备')
            return
        remote_paths = payload.get('remotePaths')
        if (not isinstance(remote_paths, list) or not remote_paths
                or any(not isinstance(p, str) or not p.startswith('/') or '\x00' in p
                       for p in remote_paths) or (operation == 'push' and len(remote_paths) != 1)):
            self._fail_request(path, request, '设备文件路径必须是绝对路径', rejected=True)
            return
        directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer,
                                                payload.get('transferId'))
        directory.mkdir(parents=True, exist_ok=True)
        output = []
        deadline = time.monotonic() + relay_transfer.TIMEOUT

        def run(argv, *, raw=False):
            reason = self._authorize(request)
            if reason:
                raise RuntimeError(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('文件传输执行超时')
            # 固定 shell 辅助命令用独立 argv，设备路径不能变成 shell 语法。
            proc = subprocess.run([self.adb_path, *device_selector_args(device), *argv],
                                  capture_output=True, text=not raw, encoding=None if raw else 'utf-8',
                                  errors=None if raw else 'replace',
                                  timeout=remaining, creationflags=CREATE_NO_WINDOW)
            detail = ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', 'replace').strip() if raw else ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode:
                raise RuntimeError(detail or f'adb 退出码 {proc.returncode}')
            if detail and not raw:
                output.append(detail)
            return proc

        with tempfile.TemporaryDirectory(prefix='DevMux-transfer-') as work:
            work = Path(work)
            if operation == 'push':
                paths = relay_transfer.receive_package(directory / 'upload.zip', work, payload.get('package') or {})
                run(['push', *map(str, paths), remote_paths[0]])
                if payload.get('makeExecutable'):
                    import shlex
                    scripts = [p for p in paths if p.is_file() and p.suffix.lower() == '.sh']
                    if scripts:
                        remote = remote_paths[0]
                        check = subprocess.run([self.adb_path, *device_selector_args(device), 'shell',
                                                'test -d ' + shlex.quote(remote)], capture_output=True,
                                               timeout=15, creationflags=CREATE_NO_WINDOW)
                        for script in scripts:
                            target = remote.rstrip('/') + '/' + script.name if check.returncode == 0 else remote
                            run(['shell', 'chmod +x ' + shlex.quote(target)])
                        output.append('已添加脚本执行权限')
                run(['shell', 'sync'])
                output.append('上传成功；设备文件已同步')
                extra = {}
            else:
                paths = []
                for index, remote in enumerate(remote_paths):
                    target = work / str(index)
                    target.mkdir()
                    from adb_pull import pull_to_temp
                    paths.append(pull_to_temp(run, remote, target))
                extra = {'package': relay_transfer.publish_upload(paths, directory / 'download.zip')}
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
                                       exitCode=0, stdout='\n'.join(output), extra=extra,
                                       agentId=self.peer_id, finishedAt=relay.now_iso()), path)

    def _serve_exec(self, path: str, request: relay.Request) -> None:
        argv = request.payload.get('argv')
        allowed, reason = relay.check_command(
            argv, allow_shell=self.allow_shell, allowed_subcommands=self.allowed_subcommands,
        )
        if not allowed:
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_REJECTED,
                    error=f'命令被策略拒绝：{reason}', agentId=self.peer_id,
                    finishedAt=relay.now_iso(),
                ),
                path,
            )
            self._emit('reject', f'命令被策略拒绝：{reason}（argv={argv}）')
            return
        device = self._resolve_request_device(request)
        if device is None:
            detail = f'本机没有匹配的设备：{request.device}（{self._describe_devices()}）'
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=detail, agentId=self.peer_id, finishedAt=relay.now_iso(),
                ),
                path,
            )
            self._emit('reject', detail)
            return

        started_at = relay.now_iso()
        timeout = float(request.payload.get('timeout') or DEFAULT_ADB_TIMEOUT_S)
        self._emit('command', f'执行 {argv} @ {device.get("key")}')
        outcome = execute(
            self.adb_path, device, argv, timeout=timeout, max_bytes=self.max_output_bytes,
        )
        if outcome.timeout or outcome.error:
            status = relay.STATUS_ERROR
        else:
            status = relay.STATUS_OK
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=status,
                exitCode=outcome.exitCode, stdout=outcome.stdout, stderr=outcome.stderr,
                truncated=outcome.truncated, error=outcome.error, agentId=self.peer_id,
                startedAt=started_at, finishedAt=relay.now_iso(), durationMs=outcome.elapsedMs,
                extra={
                    'deviceKey': device.get('key', ''),
                    'exitCodeAvailable': outcome.exitCodeAvailable,
                },
            ),
            path,
        )
        self._emit('result', f'{argv} @ {device.get("key")} 执行完成，退出码 '
                             f'{outcome.exitCode if outcome.exitCode is not None else "不可得"}，'
                             f'耗时 {outcome.elapsedMs}ms')

    def _serve_start_log(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if not STREAM_ID_RE.match(stream_id):
            self._fail_request(path, request, f'流标识非法：{stream_id!r}')
            return
        if stream_id in self._streams:
            self._fail_request(path, request, f'流 {stream_id} 已存在')
            return
        argv = request.payload.get('argv') or ['logcat']
        allowed, reason = relay.check_command(
            argv, allow_shell=self.allow_shell, allowed_subcommands=self.allowed_subcommands,
        )
        if not allowed:
            self._fail_request(path, request, f'命令被策略拒绝：{reason}', rejected=True)
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(path, request, f'本机没有匹配的设备：{request.device}'
                                             f'（{self._describe_devices()}）')
            return

        stream = LogStreamReader(
            self.queue, stream_id, device, argv, self.adb_path, on_event=self._emit,
        )
        if not stream.start():
            self._fail_request(path, request, stream.error or '日志流启动失败')
            return
        self._streams[stream_id] = stream
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'日志流已启动：{stream_id}（{argv}）',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'deviceKey': device.get('key', '')},
            ),
            path,
        )
        self._emit('stream', f'日志流已启动 {stream_id}：{argv} @ {device.get("key")}，'
                             f'写入 streams/{self.peer_id}/{stream_id}.out')

    def _serve_stop_log(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            self._fail_request(path, request, f'没有正在运行的流：{stream_id}')
            return
        stream.stop()
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'日志流已停止：{stream_id}，累计 {stream.bytes_written} 字节',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'bytesWritten': stream.bytes_written},
            ),
            path,
        )
        self._emit('stream', f'日志流已停止 {stream_id}，累计 {stream.bytes_written} 字节')

    # --- 从机：远端终端会话 ---
    def _serve_open_session(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if not relay_session.valid_session_id(stream_id):
            self._fail_request(path, request, f'会话标识非法：{stream_id!r}')
            return
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端，无法接受远端终端会话')
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(
                path, request,
                f'本机没有匹配的设备：{request.device}（{self._describe_devices()}）',
            )
            return
        cols = int(request.payload.get('cols') or 120)
        rows = int(request.payload.get('rows') or 40)
        started = time.monotonic()
        ok, reason = self._sessions.open(stream_id, device, cols=cols, rows=rows,
                                         owner=self._session_owner(request))
        prepare_ms = round((time.monotonic() - started) * 1000)
        self._emit('info', f'终端准备耗时 {prepare_ms}ms（reqId {request.reqId}）')
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'远端终端会话已开启：{stream_id}',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'deviceKey': device.get('key', ''), 'cols': cols, 'rows': rows,
                       'prepareMs': prepare_ms},
            ),
            path,
        )

    def _serve_session_resize(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端')
            return
        ok, reason = self._sessions.resize(
            stream_id, int(request.payload.get('cols') or 120), int(request.payload.get('rows') or 40),
        )
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'窗口已调整：{stream_id}', finishedAt=relay.now_iso(),
                extra={'streamId': stream_id},
            ),
            path,
        )

    def _serve_close_session(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端')
            return
        ok, reason = self._sessions.close(stream_id, str(request.payload.get('reason') or '操作端关闭了会话'),
                                          owner=self._session_owner(request))
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'远端终端会话已关闭：{stream_id}', finishedAt=relay.now_iso(),
                extra={'streamId': stream_id},
            ),
            path,
        )

    def _session_owner(self, request):
        # 只使用已通过_authorize验证的实例与连接代次；旧协议未知所有者不自动替换。
        payload = request.payload or {}
        source, generation = payload.get('sourceNodeId'), payload.get('connectionId')
        record = self.pairing.get(self._node_id_for(request.peer))
        if (source and generation and record and record.is_paired
                and source == self._node_id_for(request.peer) and generation == record.connectionId):
            return (source, generation)
        return None

    def _fail_request(self, path: str, request: relay.Request, reason: str, *, rejected: bool = False) -> None:
        self._write_result(
            relay.Result(
                reqId=request.reqId,
                status=relay.STATUS_REJECTED if rejected else relay.STATUS_ERROR,
                error=reason, agentId=self.peer_id, finishedAt=relay.now_iso(),
            ),
            path,
        )
        self._emit('reject' if rejected else 'error', f'请求失败（{request.kind}）：{reason}')

    def _write_result(self, result: relay.Result, request_path: str = '') -> None:
        """写结果并归档对应请求。

        请求由主控写、结果由从机写，所以从机消费掉请求后由从机归档它 —— 主控
        不再需要这份文件；结果文件则由主控读到后自行归档。
        """
        self.queue.write_result(result)
        if request_path:
            self.queue.archive_file(request_path)

    # --- 主控：本地设备 ---
    def run_local(self, device_key_or_serial: str, argv, timeout: float = DEFAULT_ADB_TIMEOUT_S) -> ExecOutcome:
        """主控直接操作自己的设备（不经中转）。"""
        device = self._match_device(str(device_key_or_serial), str(device_key_or_serial))
        if device is None:
            # 同 _resolve_request_device：一次轮询快照为空不代表设备不在。
            self._refresh_devices()
            device = self._match_device(str(device_key_or_serial), str(device_key_or_serial))
        if device is None:
            return ExecOutcome(
                error=f'本机没有匹配的设备：{device_key_or_serial}（{self._describe_devices()}）',
                elapsedMs=0,
            )
        self._emit('command', f'本机直接执行 {argv} @ {device.get("key")}')
        return execute(self.adb_path, device, argv, timeout=timeout, max_bytes=self.max_output_bytes)

    # --- 主控：远端设备 ---
    def remote_devices(self, linger_s: Optional[float] = None) -> list[dict]:
        """从机对外共享的设备，key 带 ``relay:<peerId>:`` 前缀。

        在线的从机正常列出；**离线但在保留窗口内**的从机，其设备仍列出并置
        ``relay.online = False``，由前端灰显并禁止发起新操作 —— 与本地设备离线
        时的表现保持一致。超过保留窗口（``REMOTE_DEVICE_LINGER_S``）视为对端已
        退场，不再列出。
        """
        window = REMOTE_DEVICE_LINGER_S if linger_s is None else float(linger_s)
        out: list[dict] = []
        for view in self._health.peers:
            if view.role != relay.ROLE_SLAVE:
                continue
            if not view.online and view.ageMs > window * 1000.0:
                continue
            if self.pairing_required:
                if view.online and not self.connection_ready(view.peerId):
                    continue
                if not view.online and self._connection_verified.get(view.peerId, ((), 0))[0] != self._connection_endpoint(view.peerId):
                    continue
            for device in view.devices:
                local_key = str((device or {}).get('key') or '')
                if not local_key:
                    continue
                item = dict(device)
                item['origin'] = 'relay'
                item['peerId'] = view.peerId
                item['peerRole'] = view.role
                item['localKey'] = local_key
                item['key'] = remote_key(view.peerId, local_key)
                item['relay'] = {
                    'peerId': view.peerId,
                    'role': view.role,
                    'host': view.host,
                    'deploymentId': view.deploymentId,
                    'displayLabel': view.displayLabel,
                    # 地址与启动代次任一变化都需要重开终端，不能沿用旧流的游标。
                    'endpointRoot': self.channel_root(view.peerId),
                    'startedAt': view.startedAt,
                    'nodeId': view.nodeId,
                    'online': bool(view.online),
                    'ageMs': float(view.ageMs),
                    # 信任状态一并带给上层：主控据此决定「是否值得为它开会话」。
                    # 这只是体验优化，真正的准入裁决始终在从机侧（见 _authorize）。
                    'trusted': bool(view.trusted),
                    'needsConfirmation': bool(view.needsConfirmation),
                }
                out.append(item)
        return out

    def local_devices(self) -> list[dict]:
        out: list[dict] = []
        for device in self._managed_devices():
            item = dict(device)
            item['origin'] = 'local'
            item['localKey'] = item.get('key', '')
            out.append(item)
        return out

    def wait_for_peer_heartbeat(self, peer_id, timeout=3.0):
        """大文件发布后等待独立读端观察新进展；不投递请求、不延长在线期限。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not self._stop_flag.is_set():
            if self._pairing_blocks(peer_id):
                return False
            if not self._peer_offline_reason(peer_id):
                return True
            if time.monotonic() >= deadline:
                return False
            self._stop_flag.wait(0.05)
        return False

    def _peer_offline_reason(self, peer_id: str) -> str:
        """对端不可用时返回中文原因，可用时返回空串。

        只判断**传输层事实**（是否发现、心跳是否中断、角色是否为从机）。刻意不在
        这里判断「是否已确认」：那是安全边界，必须由从机裁决 —— 主控侧的预检只是
        体验优化，绝不能成为唯一防线，所以未确认时仍照常投递，由从机拒绝。
        """
        # 操作检查与界面、授权仲裁都从同一观测状态重算，不沿用旧health快照。
        with self._io_lock:
            self._health = self._compute_health()
        view = None
        for candidate in self._health.peers:
            if candidate.peerId == peer_id:
                view = candidate
                break
        if view is None:
            return '尚未发现该从机（心跳未到达）'
        if not view.online:
            age_s = max(0.0, float(view.ageMs) / 1000.0)
            return f'心跳已中断 {age_s:.1f}s'
        if view.role != relay.ROLE_SLAVE:
            return f'对端角色是{view.roleLabel}，不是从机'
        return ''

    def all_devices(self) -> list[dict]:
        """统一设备视图：主控的窗口里本地设备与远端设备同列。"""
        return self.local_devices() + self.remote_devices()

    def submit(
        self,
        peer_id: str,
        kind: str,
        *,
        device: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
        ttl_s: Optional[float] = None,
    ) -> relay.Result:
        """向某个从机投递一条请求并等待结果。

        TTL 取 ``max(默认值, 等待超时 + 10s)``：不能让请求在主控还在等的时候就
        过期作废。

        离线对端**不发请求**：直接返回一条明确的失败，而不是投出去干等到超时。
        离线期间堆积的请求一旦在对方恢复后被执行，是最危险的一类副作用
        （见设计文档 §7.3），所以这里 fail-closed。

        未配对的对端同理不发请求（准入门槛的生效点之一）。少了这一步，虽然请求
        仍会被对端的 ``_authorize`` 拒掉，但每次操作都会在**对方分区目录里留一条
        垃圾请求**、并换回一条拒绝结果 —— 那是实打实的写放大，且会污染对方的
        ``requests/`` 视图。

        **门槛判定排在离线判定之前**：未配对的对端根本不在 ``health.peers`` 里
        （读侧过滤已经把它摘掉了），若先判离线，用户看到的原因是「尚未发现该从机
        （心跳未到达）」—— 那是在让他去查网络，而真实原因是他还没配对。
        """
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if self._stop_flag.is_set():
            return relay.Result(reqId='', status=relay.STATUS_ERROR,
                error='中转节点已停止，本次操作已取消', agentId=target,
                finishedAt=relay.now_iso(), extra={'localCancelled': True})
        pair_reason = self._pairing_blocks(target)
        if not pair_reason and self.pairing_required:
            trusted, reason = self.trust.check(target, self._peer_fingerprint(target))
            if not trusted:
                pair_reason = f'对端 {target} 待本机确认身份：{reason}'
        if pair_reason:
            self._emit('reject', f'不下发请求：{pair_reason}')
            return relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'已取消本次操作：{pair_reason}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'unpaired': True},
            )
        offline_reason = self._peer_offline_reason(target)
        if offline_reason:
            self._emit('reject', f'不下发请求给离线从机 {target}：{offline_reason}')
            return relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'从机 {target} 当前离线，已取消本次操作：{offline_reason}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'peerOffline': True},
            )
        beat = self._peers.get(target)
        use_journal = self.control_journal and beat is not None and beat.controlJournal == relay_journal.VERSION
        queue = (self._journal_queue(target, sending=True) if use_journal
                 else relay.RelayQueue.for_peer(self.channel_root(target), target))
        record = self.pairing.get(self._node_id_for(target))
        # 已确认的历史关系只补齐代次，不把旧身份信任升级成实例配对。
        if record and record.is_paired and not record.connectionId:
            record = self.pairing.confirm(record.nodeId, peer_id=record.peerId,
                                         fingerprint=record.fingerprint, host=record.host)
        request_payload = {**dict(payload or {}), 'sourceNodeId': self.node_id,
                           'targetNodeId': self._node_id_for(target),
                           'connectionId': record.connectionId if record else ''}
        request = relay.new_request(
            kind, self.peer_id, device=dict(device or {}), payload=request_payload,
            ttl_s=float(ttl_s or max(relay.DEFAULT_REQUEST_TTL_S, float(timeout) + 10.0)),
        )
        try:
            queue.submit(request)
        except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
            if not use_journal:
                raise
            return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                error=f'控制日志投递未确认，不会自动重发或切换通道：{exc}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'outcomeUnknown': True, 'journalError': True})
        # 终端每次读写都记事件会产生界面广播与日志风暴，拖慢交互。
        if kind != relay.KIND_WINDOWS or (payload or {}).get('action') not in {
                'session_read', 'session_write', 'session_resize'}:
            self._emit('info', f'下发 {kind} → {target}（reqId {request.reqId}）：'
                              f'{payload.get("argv") if payload else ""}')

        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            if self._stop_flag.is_set():
                return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                    error='中转节点已停止，本次等待已取消', agentId=target,
                    finishedAt=relay.now_iso(), extra={'localCancelled': True})
            # 日志回执按reqId从本机持久收件箱取回，旧端继续读独立文件。
            try:
                result = queue.read_result(request.reqId)
            except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
                if not use_journal:
                    raise
                return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=f'控制日志回执未确认，不会自动重发：{exc}',
                    agentId=target, finishedAt=relay.now_iso(),
                    extra={'outcomeUnknown': True, 'journalError': True})
            if result is not None:
                # 回执已经读到，网络归档失败或缓慢不应延迟终端就绪。
                try:
                    if not use_journal:
                        future = self._archive_pool.submit(queue.archive_file, queue.result_path(request.reqId))
                        with self._archive_lock:
                            self._archive_futures.add(future)
                        future.add_done_callback(self._archive_finished)
                except RuntimeError:
                    pass  # 节点正在停止，保留回执供后续清理。
            if result is not None:
                return result
            self._stop_flag.wait(0.05)
        return relay.Result(
            reqId=request.reqId, status=relay.STATUS_ERROR,
            error=f'等待从机 {target} 响应超时（{timeout:g}s）',
            agentId=target, finishedAt=relay.now_iso(), extra={'localTimeout': True},
        )

    def _archive_finished(self, future):
        with self._archive_lock:
            self._archive_futures.discard(future)

    def exec_remote(
        self,
        peer_id: str,
        device_key: str,
        argv,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
    ) -> relay.Result:
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        return self.submit(
            peer_id, relay.KIND_EXEC,
            device=device, payload={'argv': [str(part) for part in argv], 'timeout': timeout},
            timeout=timeout,
        )

    def exec_any(self, device_key: str, argv, timeout: float = DEFAULT_ADB_TIMEOUT_S):
        """按设备 key 自动选路：本地设备直接执行，远端设备经从机执行。

        这是「主控窗口里两类设备操作无区别」的落点 —— 调用方只给 key。
        """
        peer_id, local_key = split_remote_key(device_key)
        if not peer_id:
            return self.run_local(device_key, argv, timeout=timeout)
        return self.exec_remote(peer_id, local_key, argv, timeout=timeout)

    def start_remote_log(
        self,
        peer_id: str,
        device_key: str,
        argv,
        stream_id: str,
        timeout: float = DEFAULT_LOG_TIMEOUT_S,
    ) -> relay.Result:
        if not STREAM_ID_RE.match(str(stream_id)):
            return relay.Result(
                reqId='', status=relay.STATUS_REJECTED,
                error=f'流标识非法：{stream_id!r}', finishedAt=relay.now_iso(),
            )
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        return self.submit(
            peer_id, relay.KIND_START_LOG, device=device,
            payload={'streamId': stream_id, 'argv': [str(part) for part in argv]},
            timeout=timeout,
        )

    def stop_remote_log(self, peer_id: str, stream_id: str, timeout: float = DEFAULT_LOG_TIMEOUT_S) -> relay.Result:
        return self.submit(
            peer_id, relay.KIND_STOP_LOG, payload={'streamId': stream_id}, timeout=timeout,
        )

    def read_remote_stream(self, peer_id: str, stream_id: str, limit: int = STREAM_READ_LIMIT):
        """增量读远端流：返回 ``(新字节, 新offset, 是否被重建)``。

        游标由本端持有；``size < offset`` 说明流文件被重建，offset 已归零重读。
        """
        target = relay.check_peer_id(peer_id, '目标从机标识')
        cache_key = (target, stream_id)
        tail = self._tails.get(cache_key)
        if tail is None:
            queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
            tail = queue.stream(stream_id).output_tail(0)
            self._tails[cache_key] = tail
        with self._tail_locks.setdefault(cache_key, threading.Lock()):
            data = tail.read_new(limit)
            rebuilt = tail.rebuilt
        return data, tail.offset, rebuilt

    def read_remote_stream_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        queue = relay.RelayQueue.for_peer(self.channel_root(peer_id), peer_id)
        return queue.stream(stream_id).read_meta()

    # --- 从机：本机正在为对端提供的会话 ---
    def served_sessions(self) -> list[dict]:
        """本机作为从机正在对外提供的终端会话。

        与 ``remote_devices`` 相反的方向：那是「别人共享给我的」，这是「我正在被
        谁使用」。两端状态里都要有，否则坐在被控机器前的人不知道有人在操作它。
        """
        if self._sessions is None:
            return []
        try:
            return self._sessions.snapshot()
        except Exception:
            return []

    # --- 主控：远端终端会话 ---
    def read_remote_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        target = relay.check_peer_id(peer_id, '目标从机标识')
        queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
        return queue.stream(stream_id).read_meta(report_missing=True)

    def attach_remote_session(
        self,
        peer_id: str,
        stream_id: str,
        *,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[str], None],
        on_event: Optional[Callable] = None,
    ) -> relay_session.RemoteSessionReader:
        """开始把远端会话的输出泵到本端。重复调用会替换旧读取线程。"""
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if not relay_session.valid_session_id(stream_id):
            raise ValueError(f'会话标识非法：{stream_id!r}')
        self.detach_remote_session(target, stream_id)
        reader = relay_session.RemoteSessionReader(
            target, stream_id,
            read_stream=lambda: self.read_remote_stream(target, stream_id),
            read_meta=lambda: self.read_remote_meta(target, stream_id),
            on_output=on_output,
            on_exit=on_exit,
            on_event=on_event or self._emit,
        )
        self._readers[(target, stream_id)] = reader
        reader.start()
        return reader

    def detach_remote_session(self, peer_id: str, stream_id: str) -> bool:
        """停止本端读取线程（不发关闭请求）。"""
        reader = self._readers.pop((str(peer_id), str(stream_id)), None)
        if reader is None:
            return False
        reader.stop()
        return True

    def open_remote_session(
        self,
        peer_id: str,
        device_key: str,
        stream_id: str,
        *,
        cols: int = 120,
        rows: int = 40,
        on_output: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
    ) -> relay.Result:
        """请从机为某台远端设备开一条终端会话，并在本端开始读它的输出。"""
        if not relay_session.valid_session_id(stream_id):
            return relay.Result(
                reqId='', status=relay.STATUS_REJECTED,
                error=f'会话标识非法：{stream_id!r}', finishedAt=relay.now_iso(),
            )
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        result = self.submit(
            peer_id, relay.KIND_OPEN_SESSION, device=device,
            payload={'streamId': stream_id, 'cols': int(cols), 'rows': int(rows)},
            timeout=timeout,
        )
        if result.ok and on_output is not None:
            self.attach_remote_session(
                peer_id, stream_id, on_output=on_output,
                on_exit=on_exit or (lambda _reason: None),
            )
        return result

    def send_remote_input(self, peer_id: str, stream_id: str, data: bytes) -> bool:
        """把按键写进远端会话的输入流。数据面追加，不走控制面请求。"""
        if not data:
            return True
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if self._pairing_blocks(target):
            return False
        queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
        # 每条输入流独立串行，不能与心跳、设备枚举及控制回执共用网络 IO 锁。
        with self._input_locks.setdefault((target, stream_id), threading.Lock()):
            return queue.stream(stream_id).input_writer().append(bytes(data))

    def resize_remote_session(
        self, peer_id: str, stream_id: str, cols: int, rows: int, timeout: float = 5.0,
    ) -> relay.Result:
        return self.submit(
            peer_id, relay.KIND_SESSION_RESIZE,
            payload={'streamId': stream_id, 'cols': int(cols), 'rows': int(rows)},
            timeout=timeout,
        )

    def close_remote_session(
        self, peer_id: str, stream_id: str, *, timeout: float = 5.0, reason: str = '',
    ) -> relay.Result:
        self.detach_remote_session(peer_id, stream_id)
        return self.submit(
            peer_id, relay.KIND_CLOSE_SESSION,
            payload={'streamId': stream_id, 'reason': reason}, timeout=timeout,
        )

    def _safe_read_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        try:
            return self.read_remote_meta(peer_id, stream_id)
        except Exception:
            return None

    # --- 信任与状态 ---
    def confirmation_candidates(self) -> list[relay.PeerView]:
        """确认入口也要包含未准入实例；发现不等于允许执行设备操作。"""
        if self.passive_slave:
            return []
        candidates = {view.peerId: view for view in self.peers() if view.needsConfirmation and view.online}
        for item in self.pairing_records():
            if item.get('paired') or item.get('state') == relay.PAIR_REVOKED or not item.get('online'):
                continue
            beat = self._peers.get(item.get('peerId'))
            if beat is None or not beat.nodeId or beat.role == self.role:
                continue
            candidates[beat.peerId] = relay.PeerView(
                peerId=beat.peerId, nodeId=beat.nodeId, role=beat.role, online=True,
                needsConfirmation=True, fingerprint=beat.fingerprint, host=beat.host,
                deviceCount=len(beat.devices), deploymentId=item.get('deploymentId', ''),
                displayLabel=self._peer_display_label(beat.peerId))
        return list(candidates.values())

    def confirm_peer(self, peer_id: str, *, note: str = ''):
        """把对端加入信任名单（等同于用户在「首连确认框」里点确认）。"""
        target = relay.check_peer_id(peer_id, '对端标识')
        beat = self._peers.get(target)
        record = self.trust.confirm(
            target,
            fingerprint=(beat.fingerprint if beat else ''),
            role=(beat.role if beat else ''),
            note=note,
        )
        with self._io_lock:
            self._health = self._compute_health()
        self._emit('trust', f'已确认对端 {target}（指纹 {record.fingerprint or "未提供"}）')
        return record

    def revoke_peer(self, peer_id: str) -> bool:
        node_id = self._node_id_for(str(peer_id))
        done = self.trust.revoke(str(peer_id))
        if done:
            if self.windows_service is not None:
                self.windows_service.close_sessions(node_id)
            if self.extension_tools is not None:
                self.extension_tools.close_owner(node_id)
            self._connection_verified.pop(str(peer_id), None)
            self._connection_verified_at.pop(str(peer_id), None)
            # 撤销即失效：复查节奏一起作废，重新确认后无需等旧成功节奏。
            self._connection_check_after.pop(str(peer_id), None)
            self._connection_checks.pop(str(peer_id), None)
            with self._io_lock:
                self._health = self._compute_health()
            self._emit('trust', f'已移除对端 {peer_id} 的信任')
        return done

    # --- 配对（准入门槛）---
    def pair_peer(self, node_id: str, *, peer_id: str = '', fingerprint: str = '', host: str = '',
                  connection_id: Optional[str] = None) -> dict:
        """把某个**实例键**加入配对名单（准入门槛的放行条件）。

        会顺带确认信任：站在用户角度「我跟它配对」和「我确认它是谁」是同一个决定
        （他是在池内列表里看着对方的标识/主机/指纹点的），要求点两次只是摩擦，不
        增加安全性。反过来 ``unpair_peer`` 也会撤掉信任，保持两侧对称。

        身份信息（显示名、指纹、主机）优先取实时心跳，其次用调用方传进来的 ——
        心跳里的才是当下事实，调用方传的可能是几秒前的界面快照。
        """
        node_id = str(node_id or '').strip()
        if not node_id:
            raise ValueError('配对需要实例键（nodeId）')
        resolved = peer_id
        if not resolved:
            for candidate, beat in self._peers.items():
                if beat.nodeId == node_id:
                    resolved = candidate
                    fingerprint = fingerprint or beat.fingerprint
                    host = host or beat.host
                    break
        beat = self._peers.get(resolved)
        if beat is not None and beat.nodeId == node_id:
            fingerprint = beat.fingerprint
            host = beat.host
        if connection_id is None and self._connection_scope:
            connection_id = f'{self._connection_scope}.{uuid.uuid4().hex}'
        record = self.pairing.confirm(node_id, peer_id=resolved, fingerprint=fingerprint, host=host,
                                      connection_id=connection_id)
        self._connection_verified.pop(resolved, None)
        self._connection_verified_at.pop(resolved, None)
        # 新确认代次必须立即重验：弹掉标记的同时把复查节奏作废，否则要等
        # 上次成功验证的旧节奏（成功档 30s）设备与会话才能恢复。
        if resolved:
            self._connection_check_after[resolved] = 0.0
            self._connection_checks.pop(resolved, None)
        if resolved:
            self.trust.confirm(
                resolved,
                fingerprint=fingerprint or self._peer_fingerprint(resolved),
                role=(self._peers[resolved].role if resolved in self._peers else ''),
                note=f'由配对操作确认（实例 {node_id}）',
            )
        with self._io_lock:
            self._health = self._compute_health()
        self._emit('trust', f'已配对实例 {node_id}（{resolved or "尚未心跳"}）')
        return record.view()

    def unpair_peer(self, node_id: str) -> bool:
        """解除配对（单方面立即生效）。同时撤掉信任，保持两侧对称。"""
        node_id = str(node_id or '')
        record = self.pairing.get(node_id)
        done = self.pairing.revoke(node_id)
        if record is not None and record.peerId:
            self.trust.revoke(record.peerId)
            self._connection_verified.pop(record.peerId, None)
            self._connection_verified_at.pop(record.peerId, None)
        if done:
            # 终端输入走追加流，不经过请求授权；撤销从机上的配对时也必须关闭已开会话。
            if self._sessions is not None:
                self._sessions.stop_all()
            if self.extension_tools is not None:
                self.extension_tools.close_owner(node_id)
            with self._io_lock:
                self._health = self._compute_health()
            self._emit('trust', f'已解除与实例 {node_id} 的配对')
        return done

    def pairing_records(self) -> list[dict]:
        """配对名单快照（含池内已发现但未配对的实例，供界面一次渲染完整）。"""
        # 三种来源共用 ``PairRecord.view()`` 的字段形状，只在下游补上各自的上下文
        # 标记（``discovered``/``legacy``）—— 同一份记录若对外有几种形状，前端迟早
        # 会在某一处读到 KeyError。
        known: dict[str, dict] = {}
        # 状态页已判定的旧身份不能从历史配对或注册表重新进入下拉列表。
        ignored_peers = set(self._health.selfGhosts) | set(self._health.supersededPeers)
        ignored_nodes = {beat.nodeId for peer, beat in self._peers.items()
                         if peer in ignored_peers and beat.nodeId}
        ignored_nodes -= {beat.nodeId for peer, beat in self._peers.items()
                          if peer not in ignored_peers and beat.nodeId}
        observed_nodes = {beat.nodeId for beat in self._peers.values() if beat.nodeId}
        for record in self.pairing.items():
            if record.nodeId == self.node_id or record.nodeId in ignored_nodes:
                continue
            if record.nodeId not in observed_nodes:
                # 本机早期测试身份已没有发现来源；撤销记录仍留盘防止旧代次复活。
                if (record.state == relay.PAIR_REVOKED
                        or (record.fingerprint == self.host_fingerprint
                            and record.host.casefold() == self.host_label.casefold())):
                    continue
            known[record.nodeId] = {**record.view(), 'discovered': False}
        # 注册表先于设备枚举和心跳写入；启动中的实例也应能成为配对候选。
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        registries = (self._presence_snapshot[1] if self._presence_snapshot is not None else
                      ({self.pool_id: relay.read_registry(self.root)} if self.pool_id else {}))
        for deployment, entries in registries.items():
            for entry in entries.values():
                if (entry.nodeId == self.node_id or entry.nodeId in ignored_nodes
                        or entry.is_stale(skew_ms=skew)):
                    continue
                record = self.pairing.get(entry.nodeId) or relay.PairRecord(
                    nodeId=entry.nodeId, peerId=entry.peerId,
                    fingerprint=entry.fingerprint, host=entry.host,
                )
                known[entry.nodeId] = {
                    **record.view(), 'peerId': entry.peerId, 'host': entry.host,
                    'deploymentId': deployment, 'role': entry.role, 'roleLabel': entry.roleLabel,
                    'online': True, 'discovered': True,
                }
        # 池内/总线上发现但没有记录的实例：列出来才有「可配对的目标」可点。
        # 同一实例改过路由名时，旧心跳不能覆盖仍在线的新身份。
        beats = sorted(self._peers.values(), key=lambda beat: (
            not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew),
            beat.fileMtimeMs, beat.heartbeatAt))
        for beat in beats:
            if (not beat.nodeId or beat.nodeId == self.node_id
                    or beat.peerId in ignored_peers or beat.nodeId in ignored_nodes):
                continue
            if (beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)
                    and not self.pairing.get(beat.nodeId)):
                continue
            view = self.pairing.get(beat.nodeId) or relay.PairRecord(
                nodeId=beat.nodeId, peerId=beat.peerId,
                fingerprint=beat.fingerprint, host=beat.host,
            )
            known[beat.nodeId] = {
                **view.view(), 'peerId': beat.peerId, 'host': beat.host,
                'fingerprint': beat.fingerprint,
                'deploymentId': self._peer_deployments.get(beat.peerId, self.pool_id),
                'role': beat.role, 'roleLabel': beat.roleLabel,
                'online': not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew),
                'discovered': True,
            }
        for item in self._health.unpairedPeers:
            node_id = str(item.get('nodeId') or '')
            if (not node_id or node_id == self.node_id or node_id in ignored_nodes
                    or node_id in known or not item.get('online')):
                continue
            view = relay.PairRecord(
                nodeId=node_id, peerId=item.get('peerId', ''), host=item.get('host', ''),
            )
            known[node_id] = {
                **view.view(), 'discovered': True, 'legacy': bool(item.get('legacy')),
            }
        records = [{**known[node_id], **self.connection_info(known[node_id].get('peerId', ''), node_id)}
                   for node_id in sorted(known)]
        if self.role != relay.ROLE_MASTER:
            return records
        unique = {}
        for item in sorted(records, key=lambda item: (
                item.get('connectionState') != 'connected', not item.get('online'),
                not item.get('paired'), item['nodeId'])):
            identity = (str(item.get('host') or item['nodeId']).casefold(), item.get('deploymentId') or '')
            unique.setdefault(identity, item)
        return list(unique.values())

    def health(self) -> relay.RelayHealth:
        return self._health

    def peers(self) -> list[relay.PeerView]:
        return list(self._health.peers)

    def wait_for_peers(self, timeout: float = 10.0, count: int = 1) -> bool:
        """等至少 ``count`` 个对端出现在线，用于演示/自动化编排。"""
        deadline = time.time() + max(0.2, timeout)
        while time.time() < deadline:
            with self._io_lock:
                self._peers = self._read_shared_peers()
                self._health = self._compute_health()
            if len([view for view in self._health.peers if view.online]) >= count:
                return True
            time.sleep(0.2)
        return False

    def pending_confirmations(self) -> list[str]:
        """在线但未确认的对端 —— 就是要弹「首连确认框」的那批。"""
        if self.passive_slave:
            return []
        return sorted(view.peerId for view in self._health.peers if view.needsConfirmation)

    def refresh(self) -> relay.RelayHealth:
        # 运行中设备与发现各由专用任务刷新，界面读取不能再次扫描整个共享目录。
        if not self.is_running():
            self._refresh_devices()
            peers = self._read_shared_peers()
            with self._io_lock:
                self._peers = peers
        with self._io_lock:
            self._health = self._compute_health()
        return self._health

    def status_line(self) -> str:
        return f'[{relay.role_label(self.role)} {self.peer_id}] {self._health.stateText} · {self._health.hint}'

    def link_notice(self, peer_id: str) -> str:
        """建立连接时的主/从身份提示（需求①的落点）。"""
        target = str(peer_id)
        view = next((item for item in self._health.peers if item.peerId == target), None)
        if view is None:
            return f'对端 {target} 不在心跳名单里，无法给出身份提示'
        local_devices = self._managed_devices()
        lines = [
            f'  本机：{self.peer_id}（{relay.role_label(self.role)}）'
            f'　指纹 {self.host_fingerprint}　主机 {self.host_label}',
            f'  对端：{view.peerId}（{view.roleLabel}）'
            f'　指纹 {view.fingerprint or "未提供"}　主机 {view.host or "未知"}',
            f'  载体：{self.carrier.status().get("kindLabel") or self.carrier.kind}'
            f'　往返延迟 {self.carrier.status().get("latencyMs")} ms',
            f'  本机设备 {len(local_devices)} 台　对端设备 {view.deviceCount} 台',
            f'  信任：{"已确认" if view.trusted else view.trustReason}',
        ]
        return '\n'.join(lines)

    # --- 事件 ---
    def _emit(self, kind: str, text: str, **extra) -> None:
        if not self.event_sink:
            return
        payload = {
            'peerId': self.peer_id,
            'role': self.role,
            'roleLabel': relay.role_label(self.role),
            'kind': kind,
            'text': text,
            'at': relay.now_iso(),
        }
        payload.update(extra)
        try:
            self.event_sink(payload)
        except Exception:  # 事件回调出错不能影响节点运行
            pass
