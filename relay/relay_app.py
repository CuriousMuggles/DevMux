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

本模块是本层唯一的入口（门面）：配置、单实例声明、生命周期与设备合并留在这里；
四类运行态各自成模块，通过 mixin 共享同一个 ``self``（B7）：

- ``relay_app_status``：事件流水与状态快照
- ``relay_app_session``：远端会话对齐与重试退避
- ``relay_app_windows``：选路执行与文件传输
- ``relay_app_pairing``：信任与配对
- ``relay_app_common``：共享常量与纯函数（**调整策略值的唯一生效点**）

本模块不碰 WebSocket、不碰 asyncio：回调由调用方（server.py）派发到自己的线程模型。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import platform
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

import relay
from . import relay_app_common
from . import relay_node
from . import relay_session
from .relay_config_store import ConfigStore

# 兼容再导出：server / adb_service / 测试一直从 relay_app 取这些名字。
# **改策略值请改 relay_app_common** —— mixin 按模块属性读取，给这里赋值不生效。
from .relay_app_common import (  # noqa: F401
    APP_VERSION, DEFAULT_ALLOWED_SUBCOMMANDS, DEVICE_SELECTOR_TOKENS, EVENT_LIMIT,
    PEER_ID_RE, PEER_UNCONFIRMED_MARKERS, SESSION_ID_PREFIX, SESSION_MAX_FAILURES,
    SESSION_POLL_INTERVAL_S, SESSION_REOPEN_BACKOFF_S, SESSION_RETRY_SLOW_S,
    STATUS_POLL_INTERVAL_S, default_peer_id, default_session_id, extract_argv,
    strip_device_selector,
)
from .relay_app_pairing import ControllerPairingMixin
from .relay_app_session import ControllerSessionMixin
from .relay_app_status import ControllerStatusMixin
from .relay_app_windows import ControllerWindowsMixin

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
    allowedSubcommands: list = field(default_factory=lambda: list(relay_app_common.DEFAULT_ALLOWED_SUBCOMMANDS))
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
            subs = list(relay_app_common.DEFAULT_ALLOWED_SUBCOMMANDS)
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
        if not relay_app_common.PEER_ID_RE.match(self.peerId or ''):
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
        from . import relay_carrier  # noqa: PLC0415 - 延后导入，避免测试替身被提前绑定
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


class RelayController(ControllerStatusMixin, ControllerSessionMixin, ControllerWindowsMixin, ControllerPairingMixin):
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
        on_serial_data: Optional[Callable[[dict], None]] = None,
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
        self.on_serial_data = on_serial_data
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
        self._events: deque = deque(maxlen=relay_app_common.EVENT_LIMIT)
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
            for stream in self._serial_clients.values():
                try:stream.stop(wait=False)
                except Exception:pass
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
                     str(Path(__file__).resolve().parent.parent),  # 程序根，不是 relay/
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
