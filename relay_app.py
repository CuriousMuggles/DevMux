"""跨机中转的应用控制器（设计文档 §9.1 的会话层 + §12.3 的 WS 层之间的粘合）。

职责边界：

- **配置**：读写 ``relay.json``（与 ``commands.json`` 同级，冻结版在 exe 同目录）。
- **生命周期**：按配置起停 ``RelayNode``；配置变更即重建节点（热生效）。
- **远端终端会话对齐**：在线从机的每台设备各保持一条会话，掉线/回来都能自动
  恢复；会话失败有退避，不会变成重试风暴。
- **设备合并**：本机设备与远端设备合成一个列表，前端不需要区分来源。
- **选路**：按设备 key 决定「本机直连」还是「经从机执行」，并把操作端的设备
  选择符（``-s``/``-t``）剥掉 —— 设备由从机依据 device 字段自行拼装。
- **事件与状态**：把节点事件攒成本机可见的流水，并输出前端可直接渲染的状态快照。

本模块不碰 WebSocket、不碰 asyncio：回调由调用方（server.py）派发到自己的线程模型。
"""
from __future__ import annotations

import os
import platform
import hashlib
import concurrent.futures
import re
import threading
import time
from functools import wraps
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import relay
from relay_config_store import ConfigStore
import relay_node
import relay_session
import relay_transfer
import uuid
import tempfile
from pathlib import Path

APP_VERSION = relay_node.APP_VERSION
EVENT_LIMIT = 200
STATUS_POLL_INTERVAL_S = 1.0
SESSION_POLL_INTERVAL_S = 0.5
SESSION_REOPEN_BACKOFF_S = 5.0
SESSION_MAX_FAILURES = 5
# 超过快速重试上限后转慢速重试，而不是永久放弃。
#
# 设备或 PTY 暂不可用后可能自行恢复。若不设慢速重试，这一轮失败就
# 把该设备**永久**钉成「没有终端」，用户只能重启应用才能恢复。
SESSION_RETRY_SLOW_S = 60.0
# 仅给旧版本从机的拒绝原因提供升级提示，不影响准入判定。
# 仅用于把提示写得更可执行，不参与任何控制流判断。
PEER_UNCONFIRMED_MARKERS = ('未确认',)
SESSION_ID_PREFIX = 'sess'
# 操作端不允许自带设备选择符：设备必须由从机依据 device 字段自行拼装。
DEVICE_SELECTOR_TOKENS = frozenset({'-s', '--serial', '-t', '--transport-id'})

PEER_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')
DEFAULT_ALLOWED_SUBCOMMANDS = list(relay.DEFAULT_ALLOWED_SUBCOMMANDS)


def default_peer_id() -> str:
    """默认标识：主机名清洗成安全字符。"""
    raw = os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'pc'
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '-', str(raw)).strip('-.')
    return (cleaned or 'pc')[:32]


def default_session_id() -> str:
    return f'{SESSION_ID_PREFIX}-{int(time.time() * 1000)}-{os.urandom(3).hex()}'


def strip_device_selector(argv) -> list:
    """剥掉操作端自带的设备选择符。

    主控界面里的每条命令都带着 ``-s <serial>`` 或 ``-t <id>``；这两个值在从机
    上是**没有意义甚至错误**的（同一 serial 在从机上可能是另一台设备，transport
    id 更是每次连接都会变）。所以跨机执行时一律剥掉，由从机按 device 字段自选。

    **只剥开头连续的选择符对**，不碰子命令之后的同名参数：``logcat -t 20`` 里的
    ``-t`` 是「取最近 20 行」，``logcat -s TAG`` 是静默过滤器 —— 全表扫描式地删
    会静默改掉命令语义，比拒绝更难发现。设备选择符只有紧跟在 adb 之后才有全局
    含义，位置天然就在开头。
    """
    tokens = [str(part) for part in (argv or [])]
    index = 0
    while index < len(tokens):
        if tokens[index] in DEVICE_SELECTOR_TOKENS:
            index += 2  # 选择符 + 它的值
            continue
        break
    return tokens[index:]


def extract_argv(payload: dict) -> list:
    """从 ``exec-adb`` 的载荷里取出 argv（字符串形式走 shlex 拆分）。"""
    argv = payload.get('argv')
    if isinstance(argv, list):
        return [str(part) for part in argv]
    args = payload.get('args') or ''
    if not args:
        return []
    import shlex  # noqa: PLC0415 - 仅此处需要
    try:
        return shlex.split(str(args))
    except ValueError:
        return str(args).split()


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass
class RelayConfig:
    enabled: bool = False
    role: str = relay.ROLE_MASTER
    peerId: str = field(default_factory=default_peer_id)
    carrierKind: str = 'shared-folder'
    root: str = ''
    # 池标识：一个共享目录里放**多套**部署时用它隔开（数据根下沉到
    # ``<root>/pool/<poolId>/``）。留空 = 就是今天的行为，老配置零迁移。
    poolId: str = ''
    # 实例键：同机多开时区分两个实例。留空则在首次启动时生成并写回配置 ——
    # 生成后**永不变**（变了等于换身份，配对与槽位都会漂）。
    nodeId: str = ''
    # 保留旧字段以读取历史配置；应用始终要求配对，不能用 False/None 绕过。
    pairingRequired: Optional[bool] = None
    allowedSubcommands: list = field(default_factory=lambda: list(DEFAULT_ALLOWED_SUBCOMMANDS))
    allowShell: bool = True
    allowWindows: bool = True
    managedSerials: list = field(default_factory=list)
    heartbeatIntervalS: float = 1.0

    def to_dict(self) -> dict:
        return {
            'schema': 1,
            'enabled': bool(self.enabled),
            'role': self.role,
            'peerId': self.peerId,
            'carrierKind': self.carrierKind,
            'root': self.root,
            'poolId': self.poolId,
            'nodeId': self.nodeId,
            'pairingRequired': True,
            'allowedSubcommands': list(self.allowedSubcommands),
            'allowShell': bool(self.allowShell),
            'allowWindows': bool(self.allowWindows),
            'managedSerials': list(self.managedSerials),
            'heartbeatIntervalS': float(self.heartbeatIntervalS),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'RelayConfig':
        data = data or {}
        kind = str(data.get('carrierKind') or 'shared-folder')
        subs = data.get('allowedSubcommands')
        if not isinstance(subs, list) or not subs:
            subs = list(DEFAULT_ALLOWED_SUBCOMMANDS)
        serials = data.get('managedSerials')
        peer = str(data.get('peerId') or '').strip() or default_peer_id()
        pairing = data.get('pairingRequired')
        return cls(
            enabled=bool(data.get('enabled')),
            role=relay.normalize_role(data.get('role') or relay.ROLE_MASTER),
            peerId=peer,
            carrierKind=kind,
            root=str(data.get('root') or ''),
            poolId=str(data.get('poolId') or '').strip(),
            nodeId=str(data.get('nodeId') or '').strip(),
            pairingRequired=(None if pairing is None else bool(pairing)),
            allowedSubcommands=[str(x) for x in subs if str(x).strip()],
            allowShell=bool(data.get('allowShell', True)),
            allowWindows=bool(data.get('allowWindows', True)),
            managedSerials=[str(x) for x in (serials or []) if str(x).strip()],
            heartbeatIntervalS=min(1.0, float(data.get('heartbeatIntervalS') or 1.0)),
        )

    @property
    def pairingEnabled(self) -> bool:
        """应用统一要求主控确认；从机由验证请求准入，旧门槛字段不能绕过。"""
        return True

    def validate(self) -> tuple[bool, str]:
        if not PEER_ID_RE.match(self.peerId or ''):
            return False, '本机标识只能包含字母、数字、点、下划线和短横线，且不超过 32 字符'
        if self.poolId and not relay.valid_peer_id(self.poolId):
            return False, '池标识只能包含字母、数字、点、下划线和短横线，且不超过 41 字符'
        if self.nodeId and not relay.valid_peer_id(self.nodeId):
            return False, '实例键只能包含字母、数字、点、下划线和短横线，且不超过 41 字符'
        if self.enabled and not str(self.root or '').strip():
            return False, '启用跨机中转必须填写共享目录'
        if self.enabled:
            spec = carrier_spec(self.carrierKind)
            if spec is None:
                return False, f'不支持的连接方式：{self.carrierKind}'
            if not spec.implemented:
                return False, f'{spec.label}尚未实现（{spec.notes or "计划中"}），本版本请使用共享文件夹'
        return True, 'ok'


def carrier_spec(kind: str):
    try:
        import relay_carrier  # noqa: PLC0415 - 延后导入，避免测试替身被提前绑定
        return relay_carrier.get_carrier_spec(kind)
    except Exception:
        return None


def load_config(path: str) -> RelayConfig:
    data = ConfigStore(path).read()
    return RelayConfig.from_dict(data)


def save_config(path: str, config: RelayConfig) -> bool:
    try:
        ConfigStore(path).update_config(config.to_dict())
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------
# 多实例配置隔离：默认配置「一台机器一份」，第二个实例必须让出去
# ----------------------------------------------------------------------
RELAY_LOCK_FILENAME = 'relay.lock'
STILL_ACTIVE = 259  # Windows GetExitCodeProcess 的「还在跑」退出码


def _pid_alive(pid: int) -> bool:
    """Windows 下的进程存活探测（``os.kill(pid, 0)`` 在 Windows 上是**真杀**，不能用）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import ctypes  # noqa: PLC0415 - 仅 Windows 需要
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True  # 拿不准时按「还活着」处理：宁可多隔离，不可串配置


def _config_claim_lock_path(path: str) -> str:
    return os.path.splitext(os.path.abspath(path))[0] + '.lock'


def _migrate_legacy_instance(default_path: str, chosen: str) -> bool:
    """只迁移同一共享目录、已退出的旧临时实例；保留原实例键及准入记录。"""
    if os.path.exists(chosen):
        return False
    base = load_config(default_path)
    if not base.root:
        return False
    folder = Path(os.environ.get('TEMP') or os.path.dirname(default_path))
    used = {load_config(str(path)).nodeId for path in Path(default_path).parent.glob(
        Path(default_path).stem + '-instance-*.json')}
    candidates = []
    for path in folder.glob('ADBManager-relay-*.json'):
        match = re.fullmatch(r'ADBManager-relay-(\d+)\.json', path.name)
        if not match or _pid_alive(int(match.group(1))):
            continue
        try:
            config = load_config(str(path))
        except OSError:
            continue
        if (not config.enabled or not relay.valid_peer_id(config.nodeId)
                or config.nodeId in used
                or os.path.normcase(os.path.abspath(config.root)) != os.path.normcase(os.path.abspath(base.root))):
            continue
        candidates.append((path.stat().st_mtime, path, config))
    if not candidates:
        return False
    _, source, config = max(candidates, key=lambda item: item[0])
    # 先复制准入记录，最后落配置；目标已存在的文件绝不覆盖。
    names = [f'relay-pairing-{config.nodeId}.json']
    if relay.valid_peer_id(config.peerId):
        pattern = re.compile(r'relay-trust-' + re.escape(config.peerId) + r'(?:-\d+)?\.json')
        names += [path.name for path in source.parent.glob('relay-trust-*.json') if pattern.fullmatch(path.name)]
    for name in names:
        target = Path(chosen).parent / name
        data = relay.read_json(str(source.parent / name))
        if data is not None and not target.exists():
            relay.atomic_write_json(str(target), data)
    if not save_config(chosen, config):
        raise OSError('无法保存持久实例配置')
    return True


def claim_config_path(default_path: str, *, pid: Optional[int] = None) -> tuple[str, str]:
    """每个运行实例认领独立持久配置；空闲槽位重用原配置与实例键。"""
    pid = int(pid if pid is not None else os.getpid())
    default_path = os.path.abspath(default_path)
    os.makedirs(os.path.dirname(default_path), exist_ok=True)
    stem, extension = os.path.splitext(default_path)
    default_holder = 0
    for slot in range(1, 1001):
        chosen = default_path if slot == 1 else f'{stem}-instance-{slot}{extension}'
        lock_path = _config_claim_lock_path(chosen)
        existing = relay.read_json(lock_path)
        holder = int(existing.get('pid') or 0) if isinstance(existing, dict) else 0
        if slot == 1:
            default_holder = holder
        if holder == pid:
            return chosen, '' if slot == 1 else f'本窗口使用独立持久配置 {chosen}，重启保留设置和连接确认。'
        if holder and _pid_alive(holder):
            continue
        if os.path.exists(lock_path):
            # 空锁可能是另一个进程刚排他创建、尚未写完，不能抢占。
            if not holder and time.time() - os.path.getmtime(lock_path) < 5:
                continue
            if relay.read_json(lock_path) != existing:
                continue
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            import json
            json.dump({'pid': pid, 'configPath': chosen, 'startedAt': relay.now_iso()}, handle)
        if slot == 1:
            return chosen, ''
        try:
            migrated = _migrate_legacy_instance(default_path, chosen)
        except OSError:
            release_config_claim(chosen, pid=pid)
            raise
        notice = (f'默认配置由实例 pid {default_holder} 使用，本窗口使用独立持久配置 {chosen}，'
                  '关闭及重启后保留设置和连接确认。'
                  + ('已迁移原临时配置及准入记录。' if migrated else ''))
        return chosen, notice
    raise RuntimeError('没有可用的实例配置槽位')


def release_config_claim(default_path: str, *, pid: Optional[int] = None) -> None:
    """只归还当前实例实际认领的配置锁，不删除配置。"""
    pid = int(pid if pid is not None else os.getpid())
    lock_path = _config_claim_lock_path(default_path)
    existing = relay.read_json(lock_path)
    if isinstance(existing, dict) and int(existing.get('pid') or 0) == pid:
        try:
            os.remove(lock_path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# PTY 后端：把 adb_worker.TerminalSession 接进会话层
# ----------------------------------------------------------------------
class AdbPtyBackend(relay_session.SessionBackend):
    """用项目的 ConPTY 会话提供真正的设备终端。

    远端会话使用独立 adb shell，输出同时镜像到从机对应设备窗口。
    镜像只用于显示，不把主控按键再次写入本机自动会话。
    """

    def __init__(self, adb_path: str = '', *, on_local_output=None):
        self.adb_path = adb_path or ''
        self.on_local_output = on_local_output

    def open(self, device: dict, *, cols: int, rows: int, on_output, on_exit):
        import adb_worker  # noqa: PLC0415 - 延后导入：库可在无 pywinpty 环境被引用
        if not adb_worker._HAS_WINPTY:  # noqa: SLF001 - 明确的可用性探测
            raise RuntimeError('本机缺少 pywinpty，无法提供远端终端会话')
        local_key = adb_worker.make_key(device or {})

        def output(data):
            # PTY 回显已包含指令和回复；直接复制字节，避免双重回显和重复执行。
            # 不额外广播按键，密码等关闭回显的输入继续保持隐藏。
            if data and self.on_local_output:
                try:
                    self.on_local_output(local_key, data)
                except Exception as exc:
                    adb_worker.log('RELAY', f'从机终端同步显示失败：{exc}')
            on_output(data)

        session = adb_worker.TerminalSession(dict(device or {}), output, on_exit)
        try:
            session.resize(int(cols), int(rows))
        except Exception:
            pass
        return session

    def write(self, handle, data: bytes) -> None:
        handle.write(bytes(data))

    def resize(self, handle, cols: int, rows: int) -> None:
        handle.resize(int(cols), int(rows))

    def close(self, handle) -> None:
        handle.close()


# ----------------------------------------------------------------------
# 控制器
# ----------------------------------------------------------------------
def _serialize_lifecycle(method):
    """串行配置起停与人工全量清理，不在状态锁内等待工作线程。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)
    return wrapped


class RelayController:
    """一个应用实例上的跨机中转运行态。主控与从机是同一份代码。"""

    def __init__(
        self,
        *,
        config_path: str,
        adb_path: str = '',
        session_backend: Optional[object] = None,
        trust_path: str = '',
        pairing_path: str = '',
        on_devices_changed: Optional[Callable[[list, list], None]] = None,
        on_status: Optional[Callable[[dict], None]] = None,
        on_terminal_output: Optional[Callable[[str, bytes], None]] = None,
        on_terminal_exit: Optional[Callable[[str, str], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        on_peer_request: Optional[Callable[[dict], None]] = None,
        log: Optional[Callable[[str, str], None]] = None,
        local_devices_enabled: bool = True,
        host_label: str = '',
        host_fingerprint: str = '',
        config_notice: str = '',
    ):
        self.config_path = os.path.abspath(config_path)
        self.config_store = ConfigStore(self.config_path)
        self.config = load_config(self.config_path)
        self.adb_path = adb_path or relay_node.DEFAULT_ADB_PATH
        # 本机是否识别设备。``False`` 时节点不枚举、不共享本机设备（见 ADBWorker
        # 同名参数的说明）：这台机器只做主控，窗口里只有远端设备。
        self.local_devices_enabled = bool(local_devices_enabled)
        # 主机名与指纹默认从环境推导（真实运行）；显式传入只用于「一台机器上模拟
        # 多台电脑」的验证脚本 —— 那时必须给每个实例不同的指纹，否则它们会把对方
        # 当成「本机自己换过的旧身份」（见 relay.is_self_ghost）。
        self._host_label = str(host_label or '')
        self._host_fingerprint = str(host_fingerprint or '')
        # 多实例隔离：本实例被迫让出默认配置时，这里带一句给用户看的说明（见
        # claim_config_path）。为空表示本实例用的就是默认配置，一切如常。
        self.config_notice = str(config_notice or '')
        # 信任名单放配置目录旁边：它必须持久（重启后仍然信任已确认的对端），
        # 但绝不能进共享目录 —— 那是所有端都能写的地方，写在那里等于没有边界。
        self._trust_override = str(trust_path or '')
        # 配对名单同上：本机私有、必须持久、绝不进共享目录（那是所有端都能写的地方）。
        self._pairing_override = str(pairing_path or '')
        self.trust_path = ''
        self.session_backend = (session_backend if session_backend is not None else
                                AdbPtyBackend(self.adb_path, on_local_output=on_terminal_output))
        self.on_devices_changed = on_devices_changed
        self.on_status = on_status
        self.on_terminal_output = on_terminal_output
        self.on_terminal_exit = on_terminal_exit
        self.on_event = on_event
        self.on_peer_request = on_peer_request
        self.log = log or (lambda _cat, _msg: None)

        self.node: Optional[relay_node.RelayNode] = None
        # 实际生效的本机标识。配置里的 ``peerId`` 只是**首选值**：同机多开时它会被
        # 自动退让成 ``-2``/``-3``…（见 ``_local_taken_peer_ids``）。界面显示、
        # 信任名单命名都用这个生效值，否则「界面说你叫 pc-a、总线上的名字却是
        # pc-a-2」——排查时对不上号。
        self._effective_peer_id = ''
        self._effective_preference = ''
        self.trust_path = self._trust_override or self.config_path
        self._lifecycle_lock = threading.RLock()
        self._lock = threading.RLock()
        self._reconcile_lock = threading.Lock()
        self._directory_lock = threading.Lock()
        self._directory_cache = {}
        self._directory_pending = {}
        self._session_executor = None
        self._connection_executor = None
        self._maintenance_executor = None
        self._connection_futures = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._events: deque = deque(maxlen=EVENT_LIMIT)
        self._sessions: dict[str, dict] = {}
        self._retired_sessions = {}
        self._retired_windows = {}
        self._windows_clients = {}
        self._serial_clients = {}
        self._notified_peers: set = set()
        self._last_status_sig = ''
        self._last_devices_sig = ''
        self._start_error = ''

    # --- 生命周期 ---
    def _trust_path_for(self, peer_id: str) -> str:
        """信任名单路径：显式指定优先，否则与 relay.json 同目录、按本机标识区分。"""
        if self._trust_override:
            return self._trust_override
        return os.path.join(os.path.dirname(self.config_path), f'relay-trust-{peer_id}.json')

    def _pairing_path_for(self, node_id: str) -> str:
        """配对名单按本机实例和有效共享根隔离。

        根目录切换后必须重新确认，避免旧根的配对/连接代次自动授权新根。
        """
        if self._pairing_override:
            return self._pairing_override
        name = node_id or self.config.peerId
        root = relay.resolve_pool_root(self.config.root, self.config.poolId)
        canonical_root = os.path.normcase(os.path.realpath(os.path.abspath(root))) if root else ''
        scope = hashlib.sha256(canonical_root.encode('utf-8')).hexdigest()[:16] if canonical_root else 'unconfigured'
        return os.path.join(os.path.dirname(self.config_path), f'relay-pairing-{name}-{scope}.json')

    def _trust_storage_for(self, peer_id):
        if self._trust_override:
            return None
        return self.config_store.section('trust', peer_id, self._trust_path_for(peer_id))

    def _pairing_store(self):
        name = self.config.nodeId or self.config.peerId
        if self._pairing_override:
            # 显式路径是调用方指定的存储边界（主要供旧 API/测试集成使用）。
            storage_key = name
        else:
            root = relay.resolve_pool_root(self.config.root, self.config.poolId)
            canonical_root = os.path.normcase(os.path.realpath(os.path.abspath(root))) if root else ''
            scope = hashlib.sha256(canonical_root.encode('utf-8')).hexdigest()[:16] if canonical_root else 'unconfigured'
            storage_key = f'{name}@{scope}'
        storage = None if self._pairing_override else self.config_store.section(
            'pairings', storage_key, self._pairing_path_for(self.config.nodeId))
        return relay.PairingStore(self._pairing_path_for(self.config.nodeId), storage=storage)

    def _local_taken_peer_ids(self) -> set[str]:
        """共享目录里**同机且仍在运行**的实例已经占用的标识。

        两个条件缺一不可：

        * **同指纹** —— 同一台机器。跨机心跳里带的 pid 在本机没有意义，拿它探活
          只会乱判，所以先用指纹把范围收窄。
        * **pid 仍活着** —— 只有这一条能把「同机活着的兄弟」和「我自己上一轮运行
          留下的残留心跳」分开。缺了它，单实例重启后会把**自己上一轮的身份**当成
          别人占着名字，于是自己反倒退让成 ``-2``（越重启名字越漂）。

        名字撞车的代价不是「凑合一下」：两个重名实例会把心跳写进**同一个文件**
        （破坏单写者原则、互相覆盖），读的时候 ``peer_id == self_id`` 还会被当成
        「自己」跳过 —— 表现就是「本机多开的实例在配对列表里根本不出现」。
        """
        if not self.config.root:
            return set()
        fingerprint = self._host_fingerprint or relay.host_fingerprint()
        me = os.getpid()
        taken: set[str] = set()
        root = relay.resolve_pool_root(self.config.root, self.config.poolId)
        beats = relay.read_peers(os.path.join(root, relay.SLOT_PEERS))
        for beat in beats.values():
            if not beat.peerId or beat.fingerprint != fingerprint:
                continue
            pid = int(beat.pid or 0)
            if pid and pid != me and _pid_alive(pid):
                taken.add(beat.peerId)
        return taken

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def check_config(self, payload: dict) -> tuple[bool, str]:
        """预检同机同部署从机；不写配置、不停止当前节点。"""
        candidate = RelayConfig.from_dict({**self.config.to_dict(), **(payload or {})})
        ok, reason = candidate.validate()
        if not ok:
            return False, reason
        if not candidate.enabled or candidate.role != relay.ROLE_SLAVE:
            return True, 'ok'
        root = relay.resolve_pool_root(candidate.root, candidate.poolId)
        beats = relay.read_peers(os.path.join(root, relay.SLOT_PEERS))
        fingerprint = self._host_fingerprint or relay.host_fingerprint()
        host = self._host_label or platform.node()
        for beat in beats.values():
            if beat.role != relay.ROLE_SLAVE or beat.host.casefold() != host.casefold():
                continue
            # 同一实例重启及原配置保存不算重复；其他实例不得借相同 nodeId 绕过。
            if beat.pid == os.getpid() and beat.nodeId == self.config.nodeId:
                continue
            same_machine = beat.fingerprint == fingerprint
            if same_machine and beat.pid and _pid_alive(beat.pid):
                return False, (f'共享目录中已有「{host} / {candidate.poolId or "默认部署"}」从机实例正在运行，'
                               '无法保存并生效。请关闭已有实例，或选择不同的部署标识。')
        return True, 'ok'

    @_serialize_lifecycle
    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return True, 'ok'
            if not self.config.enabled:
                return False, '跨机中转未启用'
            ok, reason = self.config.validate()
            if not ok:
                return False, reason
            ok, reason = self.check_config(self.config.to_dict())
            if not ok:
                return False, reason
            # 实例键：首次启动时生成并**写回配置**，此后永不变。不落盘的话每次重启
            # 都换一个身份，配对名单与池内槽位会跟着漂（对端看到的是陌生实例），
            # 这比不隔离更难排查。
            if not self.config.nodeId:
                candidate = RelayConfig.from_dict(self.config.to_dict())
                candidate.nodeId = relay.new_node_id(self._host_label)
                if not save_config(self.config_path, candidate):
                    return False, '实例身份无法保存，未启动中转'
                self.config.nodeId = candidate.nodeId
                self._push_event({
                    'kind': 'info',
                    'text': f'已生成实例键 {self.config.nodeId} 并写入 {self.config_path}',
                })
            # 本机标识**自动生成**（配置里的值只是首选）：同机多开时自动退让成
            # -2/-3…，否则两个实例会写同一个心跳文件，且互相把对方当成「自己」
            # 跳过 —— 配对列表里就永远看不到对方。
            # 退让到临时配置的那个实例先把首选名让出去：两个实例几乎同时启动，
            # 这一刻共享目录里可能还没有对方的心跳，光靠扫描会双双拿到同一个名字。
            taken = self._local_taken_peer_ids()
            retained = self._effective_peer_id if self._effective_preference == self.config.peerId else ''
            if self.config_notice and not retained:
                taken.add(self.config.peerId)
            self._effective_peer_id = relay.choose_peer_id(
                retained or self.config.peerId, taken)
            self._effective_preference = self.config.peerId

            # 先验证/迁移本机记录，失败时不创建节点、心跳线程或共享目录内容。
            try:
                pairing_store = self._pairing_store()
                pairing_store.load()
                trust_store = relay.TrustStore(self._trust_path_for(self._effective_peer_id),
                    storage=self._trust_storage_for(self._effective_peer_id))
                trust_store.load()
            except OSError as exc:
                self._start_error = str(exc)
                return False, f'配置读取或迁移失败：{exc}'

            node = relay_node.RelayNode(
                peer_id=self._effective_peer_id,
                role=self.config.role,
                root=self.config.root,
                pool_id=self.config.poolId,
                node_id=self.config.nodeId,
                pairing_path=self._pairing_path_for(self.config.nodeId),
                pairing_store=pairing_store,
                pairing_required=self.config.pairingEnabled,
                passive_slave=self.config.role == relay.ROLE_SLAVE,
                adb_path=self.adb_path,
                carrier_kind=self.config.carrierKind,
                managed_serials=self.config.managedSerials,
                allow_shell=self.config.allowShell,
                allow_windows=self.config.allowWindows,
                windows_service=getattr(self, 'windows_service', None),
                extension_tools=getattr(self, 'extension_tools', None),
                windows_directory=os.path.join(os.path.dirname(self.config_path), 'windows-jobs-'+self.config.nodeId),
                allowed_subcommands=tuple(self.config.allowedSubcommands),
                heartbeat_interval_s=self.config.heartbeatIntervalS,
                poll_interval_s=0.05,
                trust_path=self._trust_path_for(self._effective_peer_id),
                trust_store=trust_store,
                event_sink=self._on_node_event,
                session_backend=self.session_backend if self.config.role == relay.ROLE_SLAVE else None,
                local_devices_enabled=self.local_devices_enabled,
                host_label=self._host_label,
                host_fingerprint=self._host_fingerprint,
            )
            started, reason = node.start()
            if not started:
                self._start_error = reason
                self._push_event({'kind': 'error', 'text': f'中转节点启动失败：{reason}'})
                return False, reason
            self._start_error = ''
            self.node = node
            self.trust_path = self._trust_override or self.config_path
            # 每次启动使用独立停止令牌；不能复活仍在等待网络的旧轮询线程。
            self._stop_flag = threading.Event()
            self._sessions.clear()
            self._retired_sessions.clear()
            self._retired_windows.clear()
            self._notified_peers.clear()
            self._session_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='RelaySessionOpen')
            self._connection_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='RelayConnectionCheck')
            self._maintenance_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix='RelayMaintenance')
            self._connection_futures = {}
            self._thread = threading.Thread(target=self._loop, args=(self._stop_flag,),
                                            name='relay-controller', daemon=True)
            self._thread.start()
        self._push_event({
            'kind': 'info',
            'text': f'跨机中转已启动：{relay.role_label(self.config.role)} '
                    f'{self.config.peerId}，共享目录 {self.config.root}'
                    + (f'，池 {self.config.poolId}' if self.config.poolId else '')
                    + (f'，实例 {self.config.nodeId}' if self.config.nodeId else ''),
        })
        return True, 'ok'

    @_serialize_lifecycle
    def stop(self, *, disable: bool = False) -> None:
        with self._lock:
            self._stop_flag.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            session_executor = self._session_executor
            self._session_executor = None
            connection_executor = self._connection_executor
            self._connection_executor = None
            maintenance_executor = self._maintenance_executor
            self._maintenance_executor = None
            self._connection_futures = {}
            node = self.node
            self.node = None
            self._windows_clients.clear()
            self._serial_clients.clear()
            self._sessions.clear()
            self._retired_sessions.clear()
            self._retired_windows.clear()
            self._notified_peers.clear()
        if session_executor is not None:
            session_executor.shutdown(wait=False, cancel_futures=True)
        if connection_executor is not None:
            connection_executor.shutdown(wait=False, cancel_futures=True)
        if maintenance_executor is not None:
            maintenance_executor.shutdown(wait=False, cancel_futures=True)
        if node is not None:
            try:
                node.stop()
            except Exception as exc:
                self.log('RELAY', f'节点停止异常：{type(exc).__name__}: {exc}')
        if disable:
            self._push_event({'kind': 'info', 'text': '跨机中转已停止'})

    def set_windows_permission(self, allowed: bool) -> bool:
        """单独保存电脑权限并即时生效，不重启中转或打断 ADB 会话。"""
        with self._lock:
            candidate = RelayConfig.from_dict({**self.config.to_dict(), 'allowWindows': allowed})
            if not save_config(self.config_path, candidate):
                raise OSError(f'配置无法写入 {self.config_path}')
            self.config = candidate
            if self.node is not None:
                self.node.allow_windows = allowed
            if not allowed:
                services = {getattr(self, 'windows_service', None),
                            getattr(self.node, 'windows_service', None)}
                for service in services:
                    if service is not None:
                        service.close_sessions()
        self._emit_status(force=True)
        return allowed

    @_serialize_lifecycle
    def apply_config(self, payload: dict) -> tuple[bool, str]:
        """保存新配置并热生效：节点按需重建，会话按需重开。"""
        candidate = RelayConfig.from_dict({**self.config.to_dict(), **(payload or {})})
        ok, reason = self.check_config(candidate.to_dict())
        if not ok:
            return False, reason
        was_running = self.is_running()
        self.stop()
        with self._lock:
            self.config = candidate
        saved = save_config(self.config_path, candidate)
        if not saved:
            return False, f'配置无法写入 {self.config_path}'
        if candidate.enabled:
            started, reason = self.start()
            if not started:
                return False, reason
        elif was_running:
            self._push_event({'kind': 'info', 'text': '跨机中转已关闭'})
        # 配置一动就立刻推一次状态，不等下一轮轮询：改的往往正是角色/标识这类
        # 只在签名里出现一次的东西，晚一拍用户就会看到「设了主控还是显示从机」。
        self._emit_status(force=True)
        return True, 'ok'

    @_serialize_lifecycle
    def reload_from_disk(self) -> tuple[bool, bool, str]:
        """从磁盘重读配置，与运行中的不一致时重新生效。

        返回 ``(是否有变化, 是否成功, 原因)``。界面的「刷新状态」走这里：``relay.json``
        就在 exe 同目录，可能被**另一个实例**或手工编辑改过；只拉状态是看不出角色变了的，
        必须把磁盘上的配置读回来比一次，不一样才重新生效（重启节点）。
        """
        path = self.config_path
        if not os.path.exists(path):
            return False, True, f'配置文件不存在：{path}'
        disk = load_config(path)
        with self._lock:
            current = self.config
        if disk.to_dict() == current.to_dict():
            return False, True, '配置无变化'
        changed = ', '.join(
            f'{key}: {current.to_dict().get(key)!r} → {value!r}'
            for key, value in disk.to_dict().items()
            if current.to_dict().get(key) != value
        )
        ok, reason = self.apply_config(disk.to_dict())
        return True, ok, f'配置已重新生效（{changed}）' if ok else reason

    # --- 共享目录的盘点与清空 ---
    def inspect_root(self, root: str) -> dict:
        """看看一个目录里有没有之前的中转数据（换共享目录时供前端询问沿用/清空）。"""
        return relay.inspect_relay_root(root)

    def clean_root(self, root: str) -> list[str]:
        """清空目录里的中转自有数据（白名单制，只动中转自己的文件），返回删了什么。"""
        removed = relay.clean_relay_root(root)
        if removed:
            self._push_event({
                'kind': 'info',
                'text': f'已清空 {root} 里的中转数据：{", ".join(removed)}',
            })
        return removed

    @_serialize_lifecycle
    def clear_cache(self, root: str, confirmed: bool = False) -> dict:
        """人工清理各部署闲置缓存；保持中转、配置和终端运行。"""
        if confirmed is not True:
            raise ValueError('清除历史缓存需要人工确认')
        if self.config.carrierKind != 'shared-folder':
            raise ValueError('当前连接方式不支持共享目录清理')
        protected = (self.config_path, self.trust_path,
                     self._pairing_path_for(self.config.nodeId),
                     self._trust_override, self._pairing_override,
                     str(Path(__file__).resolve().parent),
                     os.path.dirname(os.path.abspath(os.sys.executable)))
        target = relay.validate_cache_root(self.config.root, protected)
        if not root or not os.path.isabs(root) or relay.validate_cache_root(root, protected) != target:
            raise ValueError('确认目录与当前已保存共享目录不一致，请刷新配置后重试')
        peer = self._effective_peer_id or self.config.peerId
        def active_requests():
            node = self.node
            return set(tuple(node._inflight)) if node is not None else set()
        identity = (target, peer, self.config.poolId, id(self.node), getattr(self.node, "_started_at", None))
        if getattr(self, '_cache_scan_identity', None) != identity:
            self._cache_scan_identity = identity
            self._cache_scan_state = {}
        result = relay.clear_cache_root(target, protected, peer_id=peer, pool_id=self.config.poolId,
                                       active_requests=active_requests, scan_state=self._cache_scan_state)
        def size(value):
            return f'{value / (1024 * 1024):.2f} MiB' if value >= 1024 * 1024 else f'{value / 1024:.1f} KiB'
        counts = (f"扫描 {result['scanned']} 项，删除 {result['removedFiles']} 个闲置文件，"
                  f"精简 {result['trimmedFiles']} 个历史记录，减少 {size(result['freedBytes'])}，"
                  f"保留 {result['keptFiles']} 项（{size(result['keptBytes'])}）")
        headline = ('闲置缓存未完全清理：' if not result['success']
                    else '闲置缓存本批部分完成：' if not result['completed'] else '闲置缓存已清理：')
        result['output'] = headline + ('（继续上次扫描）' if result['resumed'] else '') + counts + '。'
        if result['keptReasons']:
            result['output'] += '保留原因：' + '；'.join(f'{reason} {count} 项' for reason, count in result['keptReasons'].items()) + '。'
        if result['errors']:
            result['output'] += '失败：' + '；'.join(item['path'] + '：' + item['error'] for item in result['errors']) + '。'
        result['output'] += '中转、终端和配置保持不变。' + result['notice']
        self._push_event({'kind': 'info' if result['success'] else 'error', 'text': result['output']})
        return result

    # --- 节点事件 ---
    def _on_node_event(self, payload: dict) -> None:
        self._push_event(payload)

    def _push_event(self, payload: dict) -> None:
        item = {
            'kind': str(payload.get('kind') or 'info'),
            'text': str(payload.get('text') or ''),
            'at': str(payload.get('at') or relay.now_iso()),
            'roleLabel': str(payload.get('roleLabel') or relay.role_label(self.config.role)),
        }
        with self._lock:
            self._events.append(item)
        if self.on_event:
            try:
                self.on_event(dict(item))
            except Exception:
                pass

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    # --- 主循环：刷新状态 + 对齐远端会话 ---
    def _loop(self, stop_flag=None) -> None:
        stop_flag = stop_flag or self._stop_flag
        next_status = 0.0
        next_session = 0.0
        while not stop_flag.is_set():
            now = time.time()
            node = self.node
            if node is None:
                break
            try:
                if now >= next_status:
                    node.refresh()
                    if stop_flag.is_set():
                        break
                    self._refresh_connections()
                    if stop_flag.is_set():
                        break
                    self._notice_peer_requests()
                    self._emit_status()
                    next_status = now + STATUS_POLL_INTERVAL_S
                if now >= next_session:
                    self._reconcile_sessions()
                    self._emit_devices()
                    next_session = now + SESSION_POLL_INTERVAL_S
            except Exception as exc:  # 单次异常不能让循环死掉
                self._push_event({'kind': 'error', 'text': f'中转轮询异常：{type(exc).__name__}: {exc}'})
            stop_flag.wait(0.2)

    def _notice_peer_requests(self) -> None:
        """发现「在线但未确认」的新对端 → 让前端弹首连确认（含主/从身份提示）。"""
        node = self.node
        if node is None or not self.on_peer_request:
            return
        for view in node.confirmation_candidates():
            if not view.needsConfirmation:
                continue
            token = (view.peerId, view.fingerprint)
            with self._lock:
                if token in self._notified_peers:
                    continue
                self._notified_peers.add(token)
            try:
                self.on_peer_request({
                    'peerId': view.peerId,
                    'role': view.role,
                    'roleLabel': view.roleLabel,
                    'fingerprint': view.fingerprint,
                    'host': view.host,
                    'deploymentId': view.deploymentId,
                    'displayLabel': view.displayLabel,
                    'deviceCount': view.deviceCount,
                    'online': bool(view.online),
                    'roleHint': f'本机为{relay.role_label(self.config.role)}，'
                                f'对端声明为{view.roleLabel}',
                    'detail': node.link_notice(view.peerId),
                    'carrier': self.config.carrierKind,
                    'latencyMs': float(node.carrier.status().get('latencyMs') or 0.0),
                })
            except Exception:
                pass

    # --- 远端设备与会话 ---
    def _refresh_connections(self) -> None:
        node = self.node
        if node is None or self.config.role != relay.ROLE_MASTER or node.health().conflict:
            return
        for peer in node.peers():
            if not (peer.role == relay.ROLE_SLAVE and peer.online and peer.trusted
                    and not peer.needsConfirmation):
                continue
            with node._io_lock:
                due = node.connection_check_due(peer.peerId)
                if not due:
                    # 验证绑定已过期（对端重启/换部署，端点与上次验证不一致）：
                    # 不能按旧节奏（成功档 30s）等下一轮复查，否则设备与会话要
                    # 消失半分钟才恢复。失败/未验证路径（无标记 + 5s 退避）不在
                    # 此列，仍保持密集重试节奏。
                    verified_endpoint = node._connection_verified.get(peer.peerId, ((), 0.0))[0]
                    if verified_endpoint != node._connection_endpoint(peer.peerId):
                        due = True
            if not due:
                continue
            executor = self._connection_executor
            if executor is None:
                continue
            token = (id(node), peer.peerId)
            with self._lock:
                pending = self._connection_futures.get(token)
                if pending is None or pending.done():
                    self._connection_futures[token] = executor.submit(node.verify_connection, peer.peerId)

    def remote_devices(self) -> list[dict]:
        node = self.node
        if node is None:
            return []
        return node.remote_devices()

    def served_sessions(self) -> list[dict]:
        """本机作为从机正在为对端提供的会话（供界面显示「谁在用我的设备」）。"""
        node = self.node
        if node is None:
            return []
        return node.served_sessions()

    def devices(self) -> list[dict]:
        """远端设备（供 server.py 与本机设备合并）。"""
        return self.remote_devices()

    def online_remote_devices(self) -> list[dict]:
        return [device for device in self.remote_devices() if (device.get('relay') or {}).get('online')]

    def _is_confirmed(self, device: dict) -> bool:
        meta = device.get('relay') or {}
        return not meta.get('needsConfirmation')

    def _reconcile_sessions(self) -> None:
        # 首连确认和后台轮询可能同时触发，避免为同一设备并发开多个 PTY。
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            self._reconcile_sessions_once()
        finally:
            self._reconcile_lock.release()

    def _queue_session_close(self, node, key, binding, reason):
        """保留待回收身份；关闭拒绝或超时不能被当作远端资源已释放。"""
        stream_id = binding.get('streamId')
        if not stream_id:
            return None
        token = (id(node), binding['peerId'], stream_id)
        with self._lock:
            self._retired_sessions.setdefault(token, dict(
                node=node, key=key, binding=binding, streamId=stream_id,
                reason=reason, nextAttempt=0.0, closing=False))
        node.detach_remote_session(binding['peerId'], stream_id)
        return token

    def _close_retired_session(self, token):
        with self._lock:
            item = self._retired_sessions.get(token)
            if item is None or item['binding'].get('opening'):
                return False
        ok = False
        try:
            result = item['node'].close_remote_session(
                item['binding']['peerId'], item['streamId'], reason=item['reason'])
            ok = bool(result and result.ok)
        except Exception as exc:
            self.log('RELAY', f'旧终端接入回收失败，保留重试：{type(exc).__name__}: {exc}')
        finally:
            with self._lock:
                if self._retired_sessions.get(token) is item:
                    if ok:
                        self._retired_sessions.pop(token, None)
                    else:
                        item.update(closing=False, nextAttempt=time.time() + SESSION_REOPEN_BACKOFF_S)
        return ok

    def _drain_session_closes(self):
        with self._lock:
            pending = [token for token, item in self._retired_sessions.items()
                       if not item['closing'] and not item['binding'].get('opening')
                       and time.time() >= item['nextAttempt']]
            for token in pending:
                self._retired_sessions[token]['closing'] = True
        for token in pending:
            executor = self._maintenance_executor
            if executor is None:
                self._close_retired_session(token)
            else:
                executor.submit(self._close_retired_session, token)
        self._drain_windows_closes()

    def _reconcile_sessions_once(self) -> None:
        """让「在线且已确认的远端设备」与「已开会话」保持一致，并把失败退避。

        对**尚未确认**的对端刻意不尝试开会话：从机一定会拒绝（准入裁决在它那边），
        徒留一串「会话未能开启」的噪声事件，而且用户还以为哪里坏了。等确认之后再开。
        这不是放宽边界 —— ``_authorize`` 始终是唯一裁决点。
        """
        node = self.node
        if node is None:
            return
        self._drain_session_closes()
        pending_peers: set = set()
        wanted: dict = {}
        retained = set()
        for device in self.remote_devices():
            metadata = device.get('relay') or {}
            if not metadata.get('online') and self._is_confirmed(device):
                retained.add(str(device.get('key') or ''))
        for device in self.online_remote_devices():
            key = str(device.get('key') or '')
            if not key:
                continue
            if not self._is_confirmed(device):
                pending_peers.add(str((device.get('relay') or {}).get('peerId') or ''))
                continue
            wanted[key] = device
        for peer_id in sorted(pending_peers):
            token = ('wait-confirm', peer_id)
            with self._lock:
                if token in self._notified_peers:
                    continue
                self._notified_peers.add(token)
            self._push_event({
                'kind': 'info',
                'text': f'对端 {peer_id} 待确认：确认后才会为它的设备开启终端会话',
            })
        # 1 关掉不再需要的会话（对端设备消失或对端被移除）
        with self._lock:
            # 心跳短暂中断保留已有流和输入代次；禁用新操作由在线校验负责。
            # 超过离线保留期、撤销或设备消失仍正常回收。
            stale = [key for key in self._sessions if key not in wanted and key not in retained]
        for key in stale:
            with self._lock:
                binding = self._sessions.pop(key, None)
            if not binding:
                continue
            self._queue_session_close(node, key, binding, '远端设备已不可用')
            self._push_event({'kind': 'session', 'text': f'远端终端会话已收起：{key}'})

        # 2 为缺会话的设备补开
        for key, device in wanted.items():
            binding = self._sessions.get(key)
            metadata = device.get('relay') or {}
            endpoint = (metadata.get('endpointRoot'), metadata.get('startedAt'), metadata.get('nodeId'))
            if binding is not None and binding.get('endpoint') != endpoint:
                # 对端换部署或重启：旧输出线程仍持有旧目录的 tail，必须丢弃旧 streamId。
                with self._lock:
                    self._sessions.pop(key, None)
                self._queue_session_close(node, key, binding, '对端部署或进程已变化')
                binding = None
                self._push_event({'kind': 'session', 'text':
                    f'对端部署或进程已变化，重建终端：{metadata.get("displayLabel") or key}'})
            if binding is not None and (binding.get('opened') or binding.get('opening')):
                continue
            with self._lock:
                if any(item['node'] is node and item['key'] == key
                       for item in self._retired_sessions.values()):
                    continue
            if binding is None:
                binding = {
                    'peerId': device['peerId'], 'localKey': device.get('localKey', ''),
                    'streamId': '', 'opened': False, 'failures': 0, 'nextAttempt': 0.0,
                    'cols': 120, 'rows': 40, 'lastReason': '',
                    'endpoint': endpoint,
                }
                self._sessions[key] = binding
            now = time.time()
            if now < float(binding.get('nextAttempt') or 0.0):
                continue
            binding['streamId'] = binding.get('streamId') or default_session_id()
            binding['opening'] = True
            executor = self._session_executor
            if executor is not None:
                executor.submit(self._open_session_task, key, binding, device, node)
            else:
                self._open_session_task(key, binding, device, node)

    def _open_session_task(self, key, binding, device, node):
        try:
            if self.node is node and self._sessions.get(key) is binding:
                self._open_session(key, binding, device, expected_node=node)
        finally:
            binding['opening'] = False
            if self.node is node:
                self._emit_devices()
                self._emit_status()

    def _open_session(self, remote_key: str, binding: dict, device: dict, expected_node=None) -> None:
        node = expected_node if expected_node is not None else self.node
        if node is None or self.node is not node:
            return
        stream_id = binding.get('streamId') or default_session_id()
        binding['streamId'] = stream_id
        cols = int(binding.get('cols') or 120)
        rows = int(binding.get('rows') or 40)

        def on_output(data: bytes) -> None:
            if (self.node is node and self._sessions.get(remote_key) is binding
                    and binding.get('streamId') == stream_id and self.on_terminal_output):
                try:
                    self.on_terminal_output(remote_key, data)
                except Exception:
                    pass

        def on_exit(reason: str) -> None:
            self._on_session_exit(remote_key, stream_id, reason,
                                  expected_node=node, expected_binding=binding)

        try:
            opened_at = time.monotonic()
            result = node.open_remote_session(
                binding['peerId'], binding.get('localKey') or '', stream_id,
                cols=cols, rows=rows, on_output=on_output, on_exit=on_exit,
            )
        except Exception as exc:
            result = relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'{type(exc).__name__}: {exc}', finishedAt=relay.now_iso(),
            )
        if (self.node is not node or self._sessions.get(remote_key) is not binding
                or binding.get('streamId') != stream_id):
            if self.node is node:
                self._queue_session_close(node, remote_key, binding, '过期终端开启已取消')
            else:
                node.close_remote_session(binding['peerId'], stream_id, reason='过期终端开启已取消')
            return
        if result is None or not result.ok:
            reason = (result.error if result is not None else '') or '未知原因'
            hint = ''
            if any(marker in reason for marker in PEER_UNCONFIRMED_MARKERS):
                # 首次连接时两端各自确认，先确认完的一端会先撞几次拒绝。这不是故障，
                # 缺的是有人去对端点一下，所以把提示写成能照做的动作，而不是一句
                # 让人以为哪里坏了的原文。
                hint = '　→ 从机可能仍使用旧版双端确认流程，请升级从机后等待自动重试'
            binding['failures'] = int(binding.get('failures') or 0) + 1
            delay = (SESSION_RETRY_SLOW_S if binding['failures'] >= SESSION_MAX_FAILURES
                     else SESSION_REOPEN_BACKOFF_S * binding['failures'])
            binding['nextAttempt'] = time.time() + delay
            repeated = binding.get('lastReason') == reason
            binding['lastReason'] = reason
            if not repeated:
                self._push_event({
                    'kind': 'reject',
                    'text': f'远端终端会话未能开启（{remote_key}）：{reason}{hint}',
                })
            return
        binding['opened'] = True
        binding['switchingControl'] = False
        binding.pop('controlSwitchStream', None)
        binding['resizeApplied'] = (int(result.extra.get('cols') or cols), int(result.extra.get('rows') or rows))
        binding['failures'] = 0
        binding['nextAttempt'] = 0.0
        binding['lastReason'] = ''
        self.log('RELAY', f'终端开启往返 {round((time.monotonic() - opened_at) * 1000)}ms，'
                 f'从机准备 {result.extra.get("prepareMs", "未知")}ms：{remote_key}')
        self._push_event({'kind': 'session', 'text': f'远端终端会话已开启：{remote_key}'})

    def _on_session_exit(self, remote_key: str, stream_id: str, reason: str,
                         *, expected_node=None, expected_binding=None) -> None:
        with self._lock:
            binding = self._sessions.get(remote_key)
            if (binding is None or binding.get('streamId') != stream_id
                    or (expected_node is not None and self.node is not expected_node)
                    or (expected_binding is not None and binding is not expected_binding)):
                return
            if binding is not None:
                if binding.get('controlSwitchStream') == stream_id:
                    reason = '终端控制权切换'
                # 保留绑定并置为未打开：下一轮对齐会重新开一条（设备仍在线时），
                # 让「设备掉线又回来」自动恢复成可用状态。
                binding['opened'] = False
                old_binding = dict(binding, opening=False)
                binding['streamId'] = ''
                switching = reason == '终端控制权切换'
                binding['switchingControl'] = switching
                binding['nextAttempt'] = 0.0 if switching else time.time() + SESSION_REOPEN_BACKOFF_S
            node = self.node
        if node is not None:
            self._queue_session_close(node, remote_key, old_binding, '会话结束，确认回收旧接入')
        with self._lock:
            if (self.node is not node or self._sessions.get(remote_key) is not binding
                    or binding.get('streamId')):
                return
            self._push_event({'kind': 'session', 'text': f'远端终端会话结束：{remote_key}（{reason}）'})
            if self.on_terminal_exit:
                try:
                    self.on_terminal_exit(remote_key, reason)
                except Exception:
                    pass

    def _shared_state(self, binding):
        node = self.node
        reader = (getattr(node, '_readers', {}).get((binding.get('peerId'), binding.get('streamId')))
                  if node else None)
        meta = getattr(reader, 'last_meta', {}) or {}
        return {'owner': meta.get('owner', 'remote'), 'generation': meta.get('generation', ''),
                'shared': bool(meta.get('shared'))}

    def session_generation(self, remote_key: str):
        binding = self._sessions.get(remote_key)
        if (self.node is None or not binding or not binding.get('streamId')
                or not (binding.get('opened') or binding.get('opening'))):
            return None
        return (id(self.node), binding['streamId'])

    def take_session_control(self, remote_key, generation):
        binding = self._sessions.get(remote_key)
        node = self.node
        state = self._shared_state(binding) if binding else {}
        if not binding or not binding.get('opened') or node is None:
            return False, '终端尚未接通，暂时不能接管'
        if not generation or state.get('generation') != generation:
            return False, '控制代次已变化，请等待状态刷新后重试'
        stream = binding['streamId']
        binding['controlSwitchStream'] = stream
        result = node.submit(binding['peerId'], relay.KIND_SESSION_TAKE,
            payload={'streamId': stream, 'generation': generation}, timeout=10)
        if not result.ok and binding.get('controlSwitchStream') == stream:
            binding.pop('controlSwitchStream', None)
        return bool(result.ok), result.stdout or result.error or '接管请求完成'

    def session_input_allowed(self, remote_key):
        binding = self._sessions.get(remote_key)
        return bool(binding and self._shared_state(binding)['owner'] == 'remote')

    def session_input_ready(self, remote_key: str) -> bool:
        binding = self._sessions.get(remote_key)
        return bool(binding and binding.get('opened') and self._shared_state(binding)['owner'] == 'remote')

    def session_input(self, remote_key: str, data: bytes) -> bool:
        if not self.session_input_allowed(remote_key):
            return False
        node = self.node
        binding = self._sessions.get(remote_key)
        if node is None or binding is None or not binding.get('streamId'):
            return False
        try:
            return node.send_remote_input(binding['peerId'], binding['streamId'], data)
        except Exception as exc:
            self.log('RELAY', f'发送远端按键失败：{type(exc).__name__}: {exc}')
            return False

    def session_resize(self, remote_key: str, cols: int, rows: int) -> bool:
        node = self.node
        binding = self._sessions.get(remote_key)
        if node is None or binding is None or not binding.get('streamId'):
            return False
        binding['cols'] = int(cols)
        binding['rows'] = int(rows)
        size = (int(cols), int(rows))
        if binding.get('opened') and binding.get('resizeApplied') == size:
            return True
        try:
            result = node.resize_remote_session(binding['peerId'], binding['streamId'], cols, rows)
            if result.ok:
                binding['resizeApplied'] = size
            return bool(result.ok)
        except Exception:
            return False

    def close_session(self, remote_key: str) -> bool:
        """主动关闭某台远端设备的会话（设备仍在时会由对齐循环重开）。"""
        node = self.node
        with self._lock:
            binding = self._sessions.pop(remote_key, None)
        if binding is None or node is None:
            return False
        stream_id = binding.get('streamId')
        if not stream_id:
            return False
        token = self._queue_session_close(node, remote_key, binding, '操作端关闭了会话')
        return self._close_retired_session(token)

    def sessions_snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    'key': key,
                    **self._shared_state(binding),
                    'peerId': binding.get('peerId', ''),
                    'streamId': binding.get('streamId', ''),
                    'opened': bool(binding.get('opened')),
                    'switchingControl': bool(binding.get('switchingControl')),
                    'failures': int(binding.get('failures') or 0),
                    'lastReason': binding.get('lastReason', ''),
                    'cols': int(binding.get('cols') or 0),
                    'rows': int(binding.get('rows') or 0),
                }
                for key, binding in self._sessions.items()
            ]

    # --- 选路执行 ---
    def is_remote_key(self, key: str) -> bool:
        peer_id, _local = relay_node.split_remote_key(str(key or ''))
        return bool(peer_id)

    def _queue_windows_close(self, node, peer, identifier):
        token = (id(node), peer, identifier)
        with self._lock:
            self._retired_windows.setdefault(token, dict(node=node, peer=peer, identifier=identifier,
                                                         nextAttempt=0.0, closing=False))
        return token

    def _close_retired_windows(self, token):
        with self._lock:
            item = self._retired_windows.get(token)
        if item is None:
            return
        ok = False
        try:
            result = item['node'].submit(item['peer'], relay.KIND_WINDOWS,
                payload={'action': 'session_close', 'args': {'sessionId': item['identifier']}}, timeout=5)
            ok = bool(result and result.ok)
        except Exception as exc:
            self.log('RELAY', f'旧PowerShell接入回收失败，保留重试：{type(exc).__name__}: {exc}')
        finally:
            with self._lock:
                if self._retired_windows.get(token) is item:
                    if ok:
                        self._retired_windows.pop(token, None)
                        key = next((key for key, client in self._windows_clients.items()
                                    if client.node is item['node'] and key[1] == item['identifier']), None)
                        if key is not None:
                            self._windows_clients.pop(key, None)
                    else:
                        item.update(closing=False, nextAttempt=time.time() + SESSION_REOPEN_BACKOFF_S)

    def _drain_windows_closes(self):
        with self._lock:
            pending = [token for token, item in self._retired_windows.items()
                       if not item['closing'] and time.time() >= item['nextAttempt']]
            for token in pending:
                self._retired_windows[token]['closing'] = True
        for token in pending:
            executor = self._maintenance_executor
            if executor is None:
                self._close_retired_windows(token)
            else:
                executor.submit(self._close_retired_windows, token)

    def windows_call(self, node_id, action, args):
        node = self.node
        if node is None:
            raise RuntimeError('跨机中转尚未启动')
        candidates = [peer for peer, beat in node._peers.items() if beat.nodeId == node_id]
        if len(candidates) != 1:
            raise ValueError('目标实例不存在或路由有歧义')
        peer = candidates[0]
        key = (node_id, (args or {}).get('sessionId'))
        with self._lock:
            client = self._windows_clients.get(key)
        if client is not None and client.node is node:
            if action == 'session_read':
                return client.read(args)
            if action in ('session_write', 'session_resize'):
                return client.send(action, args)
            if action == 'session_close':
                if client.meta['state'] != 'running':
                    with self._lock:
                        self._windows_clients.pop(key, None)
                    return {'sessionId': key[1], 'state': 'closed'}
        close_token = None
        if action == 'session_close':
            close_token = self._queue_windows_close(node, peer, (args or {}).get('sessionId'))
            with self._lock:
                self._retired_windows[close_token]['closing'] = True
        try:
            result = node.submit(candidates[0], relay.KIND_WINDOWS,
                                 payload={'action': action, 'args': args, 'streamProtocol': 1}, timeout=15)
        finally:
            if close_token is not None:
                with self._lock:
                    item = self._retired_windows.get(close_token)
                    if item is not None:
                        item.update(closing=False, nextAttempt=time.time() + SESSION_REOPEN_BACKOFF_S)
        if result.status != relay.STATUS_OK:
            raise RuntimeError(result.error or '远程 Windows 操作失败')
        if close_token is not None:
            with self._lock:
                self._retired_windows.pop(close_token, None)
                if self._windows_clients.get(key) is client:
                    self._windows_clients.pop(key, None)
        data = result.extra.get('data', {})
        if action == 'session_open' and data.get('streamProtocol') == 1:
            from windows_stream import MasterStream
            import re
            identifier = str(data.get('sessionId') or '')
            if not re.fullmatch(r'[a-f0-9]{32}', identifier) or data.get('streamId') != 'win-' + identifier:
                raise ValueError('从机返回了非法电脑流标识')
            try:
                client = MasterStream(node, peer, identifier, data['streamId'])
                with self._lock:
                    if self.node is not node:
                        raise RuntimeError('创建电脑终端期间中转节点已停止')
                    self._windows_clients[(node_id, identifier)] = client
            except Exception:
                if self.node is node:
                    token = self._queue_windows_close(node, peer, identifier)
                    self._close_retired_windows(token)
                else:
                    node.submit(peer, relay.KIND_WINDOWS,
                        payload={'action': 'session_close', 'args': {'sessionId': identifier}}, timeout=5)
                raise
        return data

    def remote_exec(self, remote_key: str, argv, timeout: float = relay_node.DEFAULT_ADB_TIMEOUT_S) -> dict:
        """把一条命令送到远端设备执行，返回与 ``exec-result`` 同构的结果。"""
        node = self.node
        peer_id, local_key = relay_node.split_remote_key(str(remote_key or ''))
        if not peer_id:
            return {'success': False, 'output': '该设备不是远端设备'}
        if node is None:
            return {'success': False, 'output': '跨机中转未启用'}
        tokens = strip_device_selector(argv)
        if not tokens:
            return {'success': False, 'output': '命令为空'}
        try:
            result = node.exec_remote(peer_id, local_key, tokens, timeout=timeout)
        except Exception as exc:
            return {'success': False, 'output': f'{type(exc).__name__}: {exc}'}
        return self._result_to_exec(result)

    def remote_list_dir(self, remote_key, path):
        """短时复用同目录结果，缓存绑定实例、连接代次和部署，不能跨撤销复用。"""
        import shlex
        node = self.node
        peer, local = relay_node.split_remote_key(str(remote_key or ''))
        argv = ['shell', 'ls -1F ' + shlex.quote(path)]
        if node is None or not peer or not node.connection_ready(peer):
            return self.remote_exec(remote_key, argv, 30.0)
        record = node.pairing.get(node._node_id_for(peer))
        generation = (id(node), node._connection_endpoint(peer), record.connectionId if record else '')
        key = (generation, local, path)
        with self._directory_lock:
            cached = self._directory_cache.get(key)
            if cached and time.monotonic() - cached[0] < 3.0:
                return dict(cached[1])
            pending = self._directory_pending.get(key)
            owner = pending is None
            if owner:
                pending = concurrent.futures.Future()
                self._directory_pending[key] = pending
        if not owner:
            return dict(pending.result(timeout=35))
        try:
            started = time.monotonic()
            result = self.remote_exec(remote_key, argv, 30.0)
            self.log('RELAY', f'远端目录读取往返 {round((time.monotonic() - started) * 1000)}ms：{remote_key} {path}')
            # 只缓存成功结果；身份或连接已变时由下次请求重新验证。
            with self._directory_lock:
                if result.get('success'):
                    if len(self._directory_cache) >= 64:
                        self._directory_cache.pop(next(iter(self._directory_cache)))
                    self._directory_cache[key] = (time.monotonic(), dict(result))
            pending.set_result(dict(result))
            return result
        except Exception as exc:
            result = {'success': False, 'output': f'远程目录读取失败：{exc}'}
            pending.set_result(result)
            return result
        finally:
            with self._directory_lock:
                self._directory_pending.pop(key, None)

    def _cleanup_download(self, node, peer_id, transfer_id):
        try:
            cleanup = node.submit(peer_id, relay_transfer.KIND_CLEANUP,
                                  payload={'transferId': transfer_id}, timeout=10)
            if not cleanup.ok:
                raise RuntimeError(cleanup.error or '对端未确认清理')
        except Exception as exc:
            self._push_event({'kind': 'error', 'text': f'文件传输临时包未清理：{exc}'})

    def windows_transfer(self, node_id, argv):
        node = self.node
        if node is None:
            return {'success': False, 'output': '跨机中转未启动'}
        peers = [peer for peer, beat in node._peers.items() if beat.nodeId == node_id]
        if len(peers) != 1:
            return {'success': False, 'output': '电脑目标不存在或路由有歧义'}
        return self.remote_transfer('relay:'+peers[0]+':windows', argv, computer=True)

    def remote_transfer(self, remote_key, argv, make_executable=False, *, computer=False):
        """沿用本机文件选择，把主控路径转为包引用，路径不会发送给从机。"""
        node = self.node
        peer_id, local_key = relay_node.split_remote_key(str(remote_key or ''))
        if node is None or not peer_id:
            return {'success': False, 'output': '跨机中转未启用或设备标识非法'}
        tokens = strip_device_selector(argv)
        if len(tokens) < 3 or tokens[0] not in ('push', 'pull'):
            return {'success': False, 'output': '上传/下载参数不完整'}
        operation = tokens[0]
        expected_endpoint = node._connection_endpoint(peer_id)
        expected_record = node.pairing.get(node._node_id_for(peer_id))
        expected_generation = expected_record.connectionId if expected_record else ''
        # 大文件打包前先验证连通性；从机对真正的文件请求再次准入。
        try:
            check = None if node.connection_ready(peer_id) else node.verify_connection(peer_id)
        except Exception as exc:
            return {'success': False, 'output': f'文件传输连接验证失败：{exc}'}
        if check is not None and not check.ok:
            return self._result_to_exec(check)
        transfer_id = uuid.uuid4().hex
        try:
            directory = relay_transfer.transfer_dir(node.channel_root(peer_id), peer_id, node.peer_id, transfer_id)
        except Exception as exc:
            return {'success': False, 'output': f'文件中转目录不可用：{exc}'}
        payload = {'transferId': transfer_id, 'operation': operation,
                   'makeExecutable': bool(make_executable),
                   'remotePaths': [tokens[-1]] if operation == 'push' else tokens[1:-1]}
        result = None
        try:
            if operation == 'push':
                started = time.monotonic()
                payload['package'] = relay_transfer.publish_upload(tokens[1:-1], directory / 'upload.zip')
                self.log('RELAY', f'上传包准备 {round((time.monotonic() - started) * 1000)}ms，'
                         f'{payload["package"]["size"]}字节：{remote_key}')
                if node._peer_offline_reason(peer_id):
                    self.log('RELAY', '上传包准备完毕，等待心跳序号恢复推进（最多3秒）')
                    node.wait_for_peer_heartbeat(peer_id, timeout=3.0)
            current_record = node.pairing.get(node._node_id_for(peer_id))
            if (self.node is not node or expected_endpoint != node._connection_endpoint(peer_id)
                    or expected_generation != (current_record.connectionId if current_record else '')):
                return {'success': False, 'output': '文件准备期间从机实例或连接已变化，请重新选择设备上传/下载'}
            result = node.submit(peer_id, relay.KIND_WINDOWS_TRANSFER if computer else relay_transfer.KIND_TRANSFER,
                                 device={} if computer else {'key': local_key}, payload=payload,
                                 timeout=relay_transfer.TIMEOUT + 30)
            if not result.ok:
                reply = self._result_to_exec(result)
                if '未实现该请求类型' in reply['output']:
                    reply['output'] += '；请升级从机以支持文件上传下载'
                self._push_event({'kind': 'error', 'text': f'远程文件传输失败：{reply["output"]}'})
                return reply
            if operation == 'pull':
                with tempfile.TemporaryDirectory(prefix='DevMux-download-') as work:
                    started = time.monotonic()
                    paths = relay_transfer.receive_package(directory / 'download.zip', work, result.extra.get('package') or {})
                    relay_transfer.save_download(paths, tokens[-1])
                    self.log('RELAY', f'下载包接收、校验与保存完成 {round((time.monotonic() - started) * 1000)}ms：{remote_key}')
                result.stdout += f'\n已保存到本机：{tokens[-1]}'
            return self._result_to_exec(result)
        except Exception as exc:
            return {'success': False, 'output': f'文件中转失败：{exc}'}
        finally:
            # 超时不能删仍可能被从机读取的上传包；保留供排查，不能误报成功。
            try:
                if operation == 'push' and not (result and result.extra.get('localTimeout')):
                    (directory / 'upload.zip').unlink(missing_ok=True)
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                # 明确拒绝没有生成下载包；无需再发清理请求拖慢错误提示。
                if operation == 'pull' and result and result.ok:
                    # 清理回执可能等待十秒，不能占用首次终端开启的工作线程。
                    executor = getattr(self, '_maintenance_executor', None)
                    if executor is not None:
                        executor.submit(self._cleanup_download, node, peer_id, transfer_id)
                    else:
                        self._cleanup_download(node, peer_id, transfer_id)
            except Exception as exc:
                self._push_event({'kind': 'error', 'text': f'文件传输临时包未清理：{exc}'})

    @staticmethod
    def _result_to_exec(result: relay.Result) -> dict:
        output = ''
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ('\n' if output else '') + result.stderr
        if result.truncated:
            output += '\n[输出已截断]'
        if not output:
            output = result.error or f'从机返回状态：{result.status}'
        elif result.error:
            output += f'\n{result.error}'
        return {'success': bool(result.ok), 'output': output, 'status': result.status, 'agentId': result.agentId}

    def exec_any(self, key: str, argv, timeout: float = relay_node.DEFAULT_ADB_TIMEOUT_S):
        """按 key 选路：远端走中转，本机返回 None 让调用方自己走本地路径。"""
        if not self.is_remote_key(key):
            return None
        return self.remote_exec(key, argv, timeout=timeout)

    # --- 信任 ---
    def confirm_peer(self, peer_id: str, accept: bool = True) -> tuple[bool, str]:
        if self.config.role != relay.ROLE_MASTER:
            return False, '从机等待主控选择并确认连接，无需在从机确认对端'
        node = self.node
        if node is None:
            return False, '跨机中转未启用'
        try:
            if accept:
                # 确认弹窗和连接列表是同一个用户决定，新版实例同时完成配对。
                target = next((item for item in node.pairing_records() if item['peerId'] == peer_id), None)
                if target is None or not target.get('nodeId') or not target.get('online'):
                    return False, '尚未发现在线实例，请等待对端启动；旧版对端需要升级后才能确认连接'
                node.pair_peer(target['nodeId'], peer_id=peer_id)
                record = node.confirm_peer(peer_id, note='用户在首连确认框中确认')
                with self._lock:
                    self._notified_peers.discard((peer_id, record.fingerprint))
                    self._notified_peers.discard(('wait-confirm', str(peer_id)))
                # 确认后立刻对齐一次：不必等下一轮轮询，界面上的远端终端会马上建起来。
                try:
                    self._reconcile_sessions()
                    self._emit_devices()
                except Exception:
                    pass
                self._emit_status(force=True)
                return True, '已确认'
            with self._lock:
                self._notified_peers.add((str(peer_id), ''))
            self._push_event({'kind': 'trust', 'text': f'已忽略对端 {peer_id}（不会接受其请求）'})
            return True, '已忽略'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def revoke_peer(self, peer_id: str) -> tuple[bool, str]:
        node = self.node
        if node is None:
            return False, '跨机中转未启用'
        done = node.revoke_peer(peer_id)
        self._emit_status(force=True)
        return done, '已移除' if done else '该对端不在信任名单中'

    # --- 配对（准入门槛）---
    def pair_peer(self, node_id: str, *, peer_id: str = '') -> tuple[bool, str]:
        """把某个实例加入配对名单。

        节点没在跑时也能配对（直接写本机的配对名单）：配对是长期决定，不该被
        「这一刻节点是否启动」挡住 —— 用户完全可能先配好再启用。
        """
        if self.config.role != relay.ROLE_MASTER:
            return False, '从机等待主控选择并确认连接，不能主动选择对端'
        node_id = str(node_id or '').strip()
        if not node_id:
            return False, '配对需要实例键'
        node = self.node
        try:
            if node is not None:
                node.pair_peer(node_id, peer_id=peer_id)
            else:
                store = self._pairing_store()
                store.confirm(node_id, peer_id=peer_id)
                self._push_event({'kind': 'trust', 'text': f'已配对实例 {node_id}（节点未运行）'})
            self._emit_status(force=True)
            return True, '已配对'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def unpair_peer(self, node_id: str) -> tuple[bool, str]:
        node_id = str(node_id or '').strip()
        if not node_id:
            return False, '需要实例键'
        node = self.node
        try:
            if node is not None:
                record = node.pairing.get(node_id)
                if self.config.role == relay.ROLE_MASTER and record and record.is_paired and record.peerId:
                    # 先通知在线从机关闭准入与 PTY；对端离线时仍立即撤销本机关系。
                    node.submit(record.peerId, relay.KIND_CLOSE_CONNECTION, timeout=2.0)
                done = node.unpair_peer(node_id)
            else:
                store = self._pairing_store()
                done = store.revoke(node_id)
            self._emit_status(force=True)
            return done, '已解除配对' if done else '该实例不在配对名单中'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def pairing_snapshot(self) -> list[dict]:
        """配对/发现名单。节点在跑就用节点的实时视图（含心跳里发现的实例），
        否则退回本机配对名单 —— 界面在两种状态下都能渲染同一形状的数据。"""
        node = self.node
        if node is not None:
            try:
                return node.pairing_records()
            except Exception:
                pass
        try:
            store = self._pairing_store()
            return [
                {
                    'nodeId': record.nodeId,
                    'peerId': record.peerId,
                    'fingerprint': record.fingerprint,
                    'host': record.host,
                    'state': record.state,
                    'stateLabel': record.stateLabel,
                    'paired': record.is_paired,
                    'confirmedAt': record.confirmedAt,
                    'revokedAt': record.revokedAt,
                    'discovered': False,
                }
                for record in sorted(store.items(), key=lambda item: item.nodeId)
            ]
        except Exception:
            return []

    # --- 状态输出 ---
    def status(self) -> dict:
        """前端可直接渲染的状态快照：所有结论在后端算好，前端只渲染。"""
        node = self.node
        config = self.config
        base = {
            'enabled': bool(config.enabled),
            'running': node is not None,
            'role': config.role,
            'roleLabel': relay.role_label(config.role),
            # 界面显示**生效值**（同机多开时会自动退让成 -2/-3，见 _effective_peer_id）：
            # 显示配置值会让用户拿一个总线上并不存在的名字去排查。
            'peerId': self._effective_peer_id or config.peerId,
            # 界面本机标识固定取计算机名，peerId 仅供内部文件路由使用。
            'machineName': self._host_label or os.environ.get('COMPUTERNAME') or default_peer_id(),
            'deployments': node.deployment_ids() if node is not None else list(relay.deployment_roots(config.root)),
            'carrierKind': config.carrierKind,
            'carrier': None,
            'root': config.root,
            # 池：``root`` 是配置里填的共享目录，``effectiveRoot`` 才是实际数据根
            # （启用池时会下沉一层到 <root>/pool/<poolId>/）。两者都给出，界面才能
            # 把「你填的」和「用在哪」分开显示 —— 只给一个的话，用户会以为程序没理会
            # 他填的路径。
            'poolId': config.poolId,
            'nodeId': config.nodeId,
            'pairingRequired': config.pairingEnabled,
            'effectiveRoot': relay.resolve_pool_root(config.root, config.poolId),
            'pool': None,
            'pairing': [],
            'unpairedPeers': [],
            'state': relay.STATE_UNCONFIGURED,
            'stateText': '未启用',
            'hint': '在「工具 → 跨机中转配置…」中启用并填写共享目录',
            'configNotice': self.config_notice,
            # 多开实例实际用的退让名。优先报**生效值**（可能是 -3 而不只是 -2：
            # 同机开到第三个实例时，前两个后缀都已被占），没起来时才退回按约定算一个。
            'suggestedPeerId': (
                self._effective_peer_id or (default_peer_id() + '-2')
            ) if self.config_notice else '',
            'peers': [],
            'pendingConfirmation': [],
            'conflict': False,
            # 未启用时也把这两个列表给全：前端与去抖签名都按「列表恒存在」处理，
            # 只在这里缺键会让界面/签名在不同状态下拿到不同形状。
            'selfGhosts': [],
            'staleMasters': [],
            'sessions': self.sessions_snapshot(),
            'servedSessions': self.served_sessions(),
            'events': self.events()[-40:],
            'startError': self._start_error,
            'appVersion': APP_VERSION,
            'heartbeatSeq': self.node._heartbeat_sequence if self.node else 0,
            'uptimeSeconds': max(0, int(time.monotonic() - relay_node.APP_STARTED_MONOTONIC)),
        }
        if node is None:
            if config.enabled and self._start_error:
                base.update({
                    'state': relay.STATE_DISCONNECTED,
                    'stateText': '启动失败',
                    'hint': self._start_error,
                })
            return base
        health = node.health()
        payload = health.to_dict() if hasattr(health, 'to_dict') else {}
        base.update(payload)
        base['running'] = True
        base['sessions'] = self.sessions_snapshot()
        base['servedSessions'] = self.served_sessions()
        base['events'] = self.events()[-40:]
        base['startError'] = self._start_error
        try:
            base['carrier'] = node.carrier.status()
        except Exception:
            base['carrier'] = None
        try:
            base['deviceCount'] = len(self.remote_devices())
            base['onlineDeviceCount'] = len(self.online_remote_devices())
        except Exception:
            pass
        # 池内全貌与配对名单：都由后端算好，前端只渲染。异常一律降级成空值 ——
        # 这两块是「锦上添花」的信息，缺了不该让整个状态快照拉不出来。
        if config.poolId:
            try:
                base['pool'] = node.pool_info()
            except Exception:
                base['pool'] = None
        try:
            base['pairing'] = self.pairing_snapshot()
            base['pendingConfirmation'] = [view.peerId for view in node.confirmation_candidates()]
        except Exception:
            base['pairing'] = []
        return base

    def _emit_status(self, *, force: bool = False) -> None:
        if not self.on_status:
            return
        status = self.status()
        # 日志跟随实际发送给界面的逐实例状态，不再只依赖较慢的发现扫描。
        notices = []
        with self._lock:
            previous = getattr(self, '_visible_peer_online', {})
            current = {}
            for item in status.get('pairing') or []:
                key = item.get('nodeId') or item.get('peerId')
                online = bool(item.get('online'))
                current[key] = online
                if key in previous and previous[key] != online:
                    label = '恢复在线' if online else '判定离线'
                    age = item.get('lastHeartbeatAgeMs')
                    notices.append(f'界面对端{label}：{item.get("peerId") or key}，'
                                   f'心跳序号 {item.get("heartbeatSeq")}，'
                                   f'距最后观测推进 {round(age) if age is not None else "未知"}ms，'
                                   f'最近心跳读取 {item.get("heartbeatReadMs") if item.get("heartbeatReadMs") is not None else "未知"}ms')
            self._visible_peer_online = current
        for notice in notices:
            self.log('RELAY', notice)
        # 签名必须覆盖界面真正会渲染的每一项。只数会话条数是错的：会话从「没开起来」
        # 变成「已开启」条数不变，去抖会把这次变化吞掉，界面就一直停在旧结论上
        # （例如永远显示「会话未能开启」）。所以逐条带上开启状态与最近一次原因。
        # 对端同理：界面上要显示角色、在线/确认标记与设备数，缺一项就会停在旧标签。
        # 离线对端还显示「最后心跳 x 前」，所以年龄要进签名 —— 但按 30 秒粒度折算，
        # 否则静默期间每来一条状态（每 2 秒）都会因为年龄变了而多推一次。
        peer_signature = sorted(
            (view.get('peerId'), view.get('online'), view.get('trusted'),
             view.get('needsConfirmation'), view.get('role'), view.get('deviceCount'),
             view.get('deploymentId'), view.get('displayLabel'), view.get('startedAt'),
             int((view.get('ageMs') or 0) // 30000))
            for view in (status.get('peers') or []) if isinstance(view, dict)
        )
        session_signature = sorted(
            (session.get('key'), session.get('opened'), session.get('failures'),
             session.get('lastReason'), session.get('cols'), session.get('rows'),
             session.get('owner'), session.get('generation'), session.get('switchingControl'))
            for session in (status.get('sessions') or []) if isinstance(session, dict)
        )
        # 本机自己那一行（角色/标识/状态文案/冲突/旧标识残留）也必须进签名。
        # 漏掉角色时有个很典型的后果：把「从机」改成「主控」后 ``state`` 往往仍是
        # ``waiting-peer``（两边都没对端），签名一模一样 → 这次推送被去抖吃掉，
        # 界面继续显示「从机」，而实际运行的是主控。
        self_signature = (
            status.get('role'), status.get('roleLabel'), status.get('peerId'),
            status.get('stateText'), status.get('hint'), status.get('conflict'),
            tuple(status.get('selfGhosts') or ()), status.get('running'),
        )
        served_signature = sorted(
            (session.get('streamId'), session.get('deviceKey'), session.get('cols'),
             session.get('rows'), session.get('owner'), session.get('generation'))
            for session in (status.get('servedSessions') or []) if isinstance(session, dict)
        )
        # 池与配对也要进签名：配对的「已配对/待确认/未配对」直接决定界面上按钮显示
        # 「配对」还是「解除」；池内实例上下线、槽位变化则决定池子那一块的列表内容。
        # 漏掉它们，用户点完「配对」会看到界面纹丝不动（去抖把这次推送吃了）。
        pool = status.get('pool') or {}
        pool_signature = (
            pool.get('count'), pool.get('onlineCount'),
            tuple(sorted(
                (entry.get('nodeId'), entry.get('slot'), entry.get('online'))
                for entry in (pool.get('entries') or []) if isinstance(entry, dict)
            )),
        )
        pairing_signature = sorted(
            (item.get('nodeId'), item.get('state'), item.get('paired'),
             item.get('deploymentId'), item.get('online'), item.get('host'),
             item.get('connectionState'), item.get('connectionReason'),
             item.get('heartbeatSeq'), item.get('uptimeSeconds'))
            for item in (status.get('pairing') or []) if isinstance(item, dict)
        )
        unpaired_signature = sorted(
            (item.get('peerId'), item.get('nodeId'), item.get('online'))
            for item in (status.get('unpairedPeers') or []) if isinstance(item, dict)
        )
        signature = (repr(peer_signature) + str(status.get('state'))
                     + repr(session_signature) + repr(served_signature)
                     + repr(pool_signature) + repr(pairing_signature)
                     + repr(unpaired_signature)
                     + repr(status.get('deployments'))
                     + repr((status.get('heartbeatSeq'), status.get('uptimeSeconds')))
                     + repr(self_signature))
        with self._lock:
            if not force and signature == self._last_status_sig:
                return
            self._last_status_sig = signature
        try:
            self.on_status(status)
        except Exception:
            pass

    def _emit_devices(self) -> None:
        """远端设备集合变化时通知上层重新合并设备列表。"""
        if not self.on_devices_changed:
            return
        from adb_worker import make_key  # noqa: PLC0415 - 只用它做 key 计算
        devices = []
        for device in self.remote_devices():
            item = dict(device)
            item['key'] = str(item.get('key') or make_key(item))
            devices.append(item)
        signature = repr(sorted(
            (device.get('key'), (device.get('relay') or {}).get('online'),
             (device.get('relay') or {}).get('deploymentId'),
             (device.get('relay') or {}).get('displayLabel'),
             (device.get('relay') or {}).get('endpointRoot'),
             (device.get('relay') or {}).get('startedAt')) for device in devices
        ))
        with self._lock:
            if signature == self._last_devices_sig:
                return
            self._last_devices_sig = signature
        try:
            self.on_devices_changed(devices, [device['key'] for device in devices])
        except Exception:
            pass
