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

与 relay 门面 / relay_carrier.py 一脉相承的三条硬约束：

- 共享目录 IO 在后台线程发生，绝不能放进 asyncio 事件循环。心跳写入、逐对端
  读取、发现和命令分别执行；``self._io_lock`` 只保护内存状态，慢网络 IO
  不能占用它，以免文件传输阻塞在线判定或界面请求。
- 安全边界落在从机：主控配置再宽松，也必须过从机的 ``check_command`` 与
  「已确认主控」名单。
- 设备选择符（``-s`` / ``-t``）由**执行端**依据 ``device`` 字段自己拼装，
  请求里出现这些 token 一律拒绝（``relay.check_command`` 已覆盖）。

文件布局（B6 之后）：本模块是**门面与核心状态**，``RelayNode`` 由四个 mixin
拼成，各自一个文件，共享同一个 ``self``：

- ``relay_node_heartbeat``：启停、心跳、设备枚举刷新
- ``relay_node_requests``：入站请求裁决与执行（含 ``LogStreamReader``）
- ``relay_node_remote``：主控侧下发、等待结果、增量读流
- ``relay_node_pairing``：配对名单、连接验证、``health()`` 视图

拆的是**文件**不是对象：四组之间有 60 余处互相调用，共同的 90 个实例属性
（连接、线程池、锁、缓存）抽成协作对象只会换成一圈反向引用。
adb 执行原语在 ``relay_adb``，节点策略常量在 ``relay_node_constants``。
"""
from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import relay
from . import relay_carrier
from . import relay_file_io
from . import relay_journal
from . import relay_session

# adb 原语与常量：以下名字是**兼容再导出**，供 relay_app / 测试沿用旧路径。
# 真正读取它们的是各 mixin 模块，打桩请打在 relay_adb 上。
from .relay_adb import (  # noqa: F401
    CREATE_NO_WINDOW,
    DEFAULT_ADB_PATH,
    DEFAULT_ADB_TIMEOUT_S,
    DEFAULT_LOG_TIMEOUT_S,
    EXIT_MARKER,
    MAX_OUTPUT_BYTES,
    REMOTE_KEY_PREFIX,
    ExecOutcome,
    device_key,
    device_selector_args,
    execute,
    list_devices,
    parse_devices,
    remote_key,
    split_exit_marker,
    split_remote_key,
    truncate_text,
)
from .relay_node_constants import (  # noqa: F401
    APP_STARTED_MONOTONIC,
    APP_VERSION,
    CONNECTION_VERIFY_OK_INTERVAL_S,
    CONNECTION_VERIFY_RETRY_INTERVAL_S,
    DEVICE_MISS_THRESHOLD,
    REMOTE_DEVICE_LINGER_S,
    STREAM_ID_RE,
    STREAM_READ_LIMIT,
)
from .relay_node_heartbeat import NodeHeartbeatMixin
from .relay_node_pairing import NodePairingMixin
from .relay_node_remote import NodeRemoteMixin
from .relay_node_requests import LogStreamReader, NodeRequestsMixin  # noqa: F401


class RelayNode(NodeHeartbeatMixin, NodeRequestsMixin, NodeRemoteMixin, NodePairingMixin):
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
