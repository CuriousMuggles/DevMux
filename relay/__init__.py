"""跨机中转协议层原语 —— 消息、流、心跳、命令策略、角色与连接状态。

设计要点（完整说明见 docs/需求与设计/v2-跨机中转透传-需求与设计.md）：

- **单写者原则**：每个文件只有一个写入者，因此不需要跨机文件锁（SMB 与云盘的
  锁语义不一致，做跨机互斥会引入不确定行为）。双向流用两个单向文件表示。
- **控制面**（``requests/`` / ``results/``）一条消息一个文件，保证可靠、幂等与有序。
- **数据面**（``streams/``）单向追加字节流 + offset 增量读，保证顺序与吞吐。
  终端按键不走控制面，否则高频输入会在共享目录产生海量小文件。
- **按对端分区**：允许多个从机接入，因此 ``requests/`` / ``results/`` /
  ``streams/`` / ``archive/`` 都再按 peerId 分一层，避免多个从机读到同一批请求。
- **主从角色**：角色是本机配置对外声明，经仲裁与首次确认后生效；多主控
  一律 fail-closed。

本模块是纯同步、无全局状态的库：所有路径显式传入，不碰 adb、不起线程、不接触
WebSocket，也不直接做文件 IO 之外的任何事，因此可以在本地临时目录里做完整的
端到端验证。具体链路（共享目录 / 以太网 / 串口）由 ``relay_carrier`` 提供。

实现按职责拆在包内的子模块里（见各模块首行说明），本模块只做再导出，
既有 ``relay.xxx`` 调用方式保持不变；新增代码请直接 import 具体子模块
（``from relay import relay_node``），不要往这里加实现。
"""
from __future__ import annotations

from .relay_constants import *  # noqa: F401,F403
from .relay_store import *  # noqa: F401,F403
from .relay_stream import *  # noqa: F401,F403
from .relay_protocol import *  # noqa: F401,F403
from .relay_pool import *  # noqa: F401,F403
from .relay_heartbeat import *  # noqa: F401,F403
from .relay_maintenance import *  # noqa: F401,F403
from .relay_trust import *  # noqa: F401,F403
from .relay_health import *  # noqa: F401,F403
from .relay_policy import *  # noqa: F401,F403
from .relay_pairing import *  # noqa: F401,F403

# 下划线开头的内部工具：历史上通过 relay._xxx 访问，保留以兼容既有调用点与测试。
from .relay_constants import _CONFLICT_COPY_RE, _TMP_MARKER  # noqa: F401
from .relay_heartbeat import _pid_alive  # noqa: F401
from .relay_maintenance import _cache_link, _cache_path, _slot_dir  # noqa: F401
from .relay_policy import _DEVICE_SELECTOR_TOKENS  # noqa: F401
from .relay_pool import _shared_age_ms  # noqa: F401
from .relay_protocol import _FAST_WRITE_LOCK, _PEER_ID_RE, _fit_peer_id  # noqa: F401
from .relay_store import _epoch_ms, _parse_iso  # noqa: F401

from .relay_cache import clear_cache_root  # noqa: F401  （实现住在 relay_cache，见 B3）

__all__ = [
    'ARCHIVE_EXISTS_RETRIES',
    'ARCHIVE_EXISTS_RETRY_S',
    'ARCHIVE_RETENTION_DAYS',
    'BASE_FEATURES',
    'DEFAULT_ALLOWED_SUBCOMMANDS',
    'DEFAULT_HEARTBEAT_INTERVAL_S',
    'DEFAULT_REQUEST_TTL_S',
    'FAST_JOURNAL_MAX_BYTES',
    'FEATURE_MESSAGES',
    'FEATURE_PEER_PARTITION',
    'FEATURE_POOL',
    'FEATURE_STREAMS',
    'HEARTBEAT_GRACE_MS',
    'HEARTBEAT_MISS_COUNT',
    'HeartbeatSequenceObserver',
    'KIND_CHECK_CONNECTION',
    'KIND_CLOSE_CONNECTION',
    'KIND_CLOSE_SESSION',
    'KIND_EXEC',
    'KIND_EXTENSION_TOOL',
    'KIND_OPEN_SESSION',
    'KIND_SESSION_RESIZE',
    'KIND_SESSION_TAKE',
    'KIND_START_LOG',
    'KIND_STOP_LOG',
    'KIND_WINDOWS',
    'KIND_WINDOWS_TRANSFER',
    'KNOWN_FEATURES',
    'LAYOUT_VERSION',
    'PAIR_CONFIRMED',
    'PAIR_DISCOVERED',
    'PAIR_REQUESTED',
    'PAIR_REVOKED',
    'PAIR_STATE_LABELS',
    'POOL_DIR',
    'PairRecord',
    'PairingStore',
    'PeerHeartbeat',
    'PeerView',
    'REGISTRY_LEASE_S',
    'RELAY_SCHEMA',
    'REPLACE_ATTEMPTS',
    'REPLACE_BACKOFF_S',
    'RESERVED_FEATURE_BLOBS',
    'ROLE_AGENT',
    'ROLE_LABELS',
    'ROLE_MASTER',
    'ROLE_OPERATOR',
    'ROLE_SLAVE',
    'RegistryEntry',
    'RelayHealth',
    'RelayQueue',
    'Request',
    'Result',
    'RoleArbitration',
    'SLOT_ARCHIVE',
    'SLOT_PEERS',
    'SLOT_REGISTRY',
    'SLOT_REQUESTS',
    'SLOT_RESULTS',
    'SLOT_STREAMS',
    'STATE_CONNECTED',
    'STATE_CONNECTING',
    'STATE_DEGRADED',
    'STATE_DISCONNECTED',
    'STATE_LABELS',
    'STATE_PENDING_CONFIRM',
    'STATE_ROLE_CONFLICT',
    'STATE_UNCONFIGURED',
    'STATE_WAITING_PEER',
    'STATUS_CANCELLED',
    'STATUS_ERROR',
    'STATUS_EXPIRED',
    'STATUS_OK',
    'STATUS_REJECTED',
    'STREAM_READ_CHUNK',
    'StreamChannel',
    'StreamTail',
    'StreamWriter',
    'TrustRecord',
    'TrustStore',
    'allocate_slot',
    'arbitrate_roles',
    'atomic_write_bytes',
    'atomic_write_json',
    'check_command',
    'check_peer_id',
    'choose_peer_id',
    'clean_relay_root',
    'clear_cache_root',
    'deployment_roots',
    'drop_own_pulse',
    'evaluate_health',
    'heartbeat_pulse_path',
    'host_fingerprint',
    'inspect_relay_root',
    'is_own_process_leftover',
    'is_provisional',
    'is_self_ghost',
    'is_superseded_ghost',
    'list_messages',
    'make_req_filename',
    'manifest_features',
    'new_node_id',
    'new_request',
    'normalize_role',
    'now_iso',
    'observer_clock_skew_ms',
    'observer_registry_skew_ms',
    'partition_dir',
    'peer_heartbeat_path',
    'pool_snapshot',
    'purge_dead_pulses',
    'purge_self_heartbeats',
    'read_heartbeat_pulse',
    'read_json',
    'read_peers',
    'read_registry',
    'registry_dir',
    'registry_path',
    'resolve_pool_root',
    'role_label',
    'valid_peer_id',
    'validate_cache_root',
    'write_heartbeat',
    'write_registry_entry',
]
