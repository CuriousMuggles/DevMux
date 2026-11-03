"""常量与枚举：版本、槽位、特性、角色、状态、命令类别、结果状态。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import re


RELAY_SCHEMA = 1

# 改名重试：Windows 上 os.replace 撞上「另一个进程正在替换同一文件」会抛
# PermissionError（共享冲突），冲突窗口只有毫秒级。
REPLACE_ATTEMPTS = 6
REPLACE_BACKOFF_S = 0.03
# 布局版本：v1 是扁平目录（单从机），v2 起按 peerId 分区（一主多从）。
LAYOUT_VERSION = 2

DEFAULT_REQUEST_TTL_S = 30.0
DEFAULT_HEARTBEAT_INTERVAL_S = 2.0
HEARTBEAT_MISS_COUNT = 3
# 跨机时效判定的额外容差（毫秒）。为什么不把容差并进 MISS_COUNT：这两件事的
# 成因不同 —— MISS_COUNT 是「丢了几拍心跳」，容差是「跨机共享目录的观测延迟」
# （SMB 属性缓存 + 一次慢枚举）。分开写，将来调哪个都说得清。
HEARTBEAT_GRACE_MS = 2000.0
STREAM_READ_CHUNK = 64 * 1024
ARCHIVE_RETENTION_DAYS = 7
# 追加通知日志（.from-*.jsonl / .replies.jsonl）只消费尾部：读端每次只取末尾
# 64KiB（见 _read_fast），日志本身无需无限增长 —— 超过该上限直接回卷为空文件
# 继续追加（文件身份不变，与心跳 pulse 的回卷同一套路），读端按“offset 越界
# 即回退到尾”自然恢复，不需要轮换协议。
FAST_JOURNAL_MAX_BYTES = 2 * 1024 * 1024
# 归档存在性判定：共享目录对“从未打开过”的远端文件有负缓存，普通 isfile 会把
# 刚写好的结果文件误判为缺失（实测 results/ 近 2 万个回执因此从不归档）。
# 用 relay_file_io 的无缓存 stat，并容忍短时传播延迟重试一次。
ARCHIVE_EXISTS_RETRIES = 2
ARCHIVE_EXISTS_RETRY_S = 0.2

# 共享目录里的槽名（子目录）。载体层把它们映射到具体链路。
SLOT_PEERS = 'peers'
SLOT_REQUESTS = 'requests'
SLOT_RESULTS = 'results'
SLOT_STREAMS = 'streams'
SLOT_ARCHIVE = 'archive'

# 池（pool）与配对（pairing）：一个共享目录服务于多套部署时的隔离机制。
#
# 目录层次（``poolId`` 为空时整体退回今天的行为，老部署零迁移）：
#
#     <root>/                          ← poolId 为空：就是今天的数据根
#     <root>/pool/<poolId>/            ← poolId 非空：池自己的数据根
#         ├── registry/<nodeId>.json   ← 池内实例注册表（谁在这个池子里）
#         └── peers/ requests/ …       ← 与今天完全同构的槽目录
#
# 隔离分三档，本版只做前两档（「能不能说话」「能不能看见」）：
#   ① 能不能说话 —— 未配对的来源一律拒绝（``_authorize`` / 投递前预检）
#   ② 能不能看见 —— 未配对的对端不进对端列表、不进角色仲裁
#   ③ 数据是否同目录 —— **不做**，那需要每条配对一条独立路径（架构级）
# 详细论证见 ``docs/需求与设计/v6-配对池与双端派生路径设计.md`` §9。
SLOT_REGISTRY = 'registry'
POOL_DIR = 'pool'
# 池注册项的租约：注册项靠定时重写刷新，超过这个时长没刷新即视为该实例已退场，
# 它占的槽位可以被后来者复用。判定必须用**共享目录 mtime 时基**，不能信墙上时钟
# （现场实测跨机时钟差几百秒，见 ``PeerHeartbeat.age_ms`` 的注释）。
REGISTRY_LEASE_S = 30.0

# 配对状态机。``confirmed`` 才放行；撤销是单方面立即生效的。
PAIR_DISCOVERED = 'discovered'
PAIR_REQUESTED = 'requested'
PAIR_CONFIRMED = 'confirmed'
PAIR_REVOKED = 'revoked'
PAIR_STATE_LABELS = {
    PAIR_DISCOVERED: '已发现',
    PAIR_REQUESTED: '待确认',
    PAIR_CONFIRMED: '已配对',
    PAIR_REVOKED: '已解除',
}

# 能力协商：清单里要求的特性，本版本必须全部认识才能启动。
FEATURE_MESSAGES = 'messages'
FEATURE_STREAMS = 'streams'
FEATURE_PEER_PARTITION = 'peer-partition'
# 池与配对。**刻意不进** ``BASE_FEATURES``：只有真正启用了池（``poolId`` 非空）
# 才会把它写进清单的 ``requiredFeatures``。这样未启用池的部署清单与老版本逐字节
# 一致（老端照常跑），而启用之后老端读到不认识的特性会**明确拒绝启动**并给出可读
# 原因 —— 把「两端都要换」从诡异故障变成一条提示。
FEATURE_POOL = 'pool-isolation'
BASE_FEATURES: tuple[str, ...] = (FEATURE_MESSAGES, FEATURE_STREAMS, FEATURE_PEER_PARTITION)
KNOWN_FEATURES: tuple[str, ...] = BASE_FEATURES + (FEATURE_POOL,)
# 预留：跨机文件体搬运（blobs）。加入时同时进 BASE_FEATURES 并提升布局版本。
RESERVED_FEATURE_BLOBS = 'blobs'


def manifest_features(pool_id: str = '') -> tuple[str, ...]:
    """本实例应当写进清单 ``requiredFeatures`` 的特性集合。

    集中一处，是因为「按需声明」是兼容承诺的一半：未启用池时必须与老版本**逐字节
    一致**（老端照常跑），启用之后才把池特性加进去（老端读到未知特性明确拒绝启动）。
    任何调用点自己拼一遍都可能漏掉或多写。
    """
    if str(pool_id or '').strip():
        return BASE_FEATURES + (FEATURE_POOL,)
    return BASE_FEATURES

# 主从角色。角色是本机配置对外声明，写进心跳供对端读取。
ROLE_MASTER = 'master'
ROLE_SLAVE = 'slave'
ROLE_LABELS = {ROLE_MASTER: '主控', ROLE_SLAVE: '从机'}
# 旧称别名（操作端 / 设备端），语义等价，保留以免散落各处的引用失效。
ROLE_OPERATOR = ROLE_MASTER
ROLE_AGENT = ROLE_SLAVE

# 连接状态。UI 直接渲染 STATE_LABELS，不做二次拼装。
STATE_UNCONFIGURED = 'unconfigured'
STATE_CONNECTING = 'connecting'
STATE_CONNECTED = 'connected'
STATE_DEGRADED = 'degraded'
STATE_DISCONNECTED = 'disconnected'
STATE_ROLE_CONFLICT = 'role-conflict'
STATE_WAITING_PEER = 'waiting-peer'
STATE_PENDING_CONFIRM = 'pending-confirm'
STATE_LABELS = {
    STATE_UNCONFIGURED: '未启用',
    STATE_CONNECTING: '连接中',
    STATE_CONNECTED: '已连接',
    STATE_DEGRADED: '已连接（降级）',
    STATE_DISCONNECTED: '连接断开',
    STATE_ROLE_CONFLICT: '角色冲突',
    STATE_WAITING_PEER: '等待对端',
    STATE_PENDING_CONFIRM: '等待确认对端',
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(normalize_role(role), str(role or ''))


def normalize_role(value) -> str:
    """把配置里的角色写法归一。

    只认 ``master`` / ``主控`` / ``主`` 为主控，**其余一律视为从机**：这是
    fail-closed 的方向选择 —— 配置写错时宁可不是主控，也不要误变成一个可以
    向别人设备下发命令的主控。
    """
    text = str(value or '').strip().lower()
    if text in ('master', '主控', '主', 'operator', '主机'):
        return ROLE_MASTER
    return ROLE_SLAVE

# 命令类别
KIND_EXEC = 'exec'
KIND_WINDOWS = 'windows-control'
KIND_EXTENSION_TOOL = 'extension-tool'
KIND_WINDOWS_TRANSFER = 'windows-file-transfer'
KIND_CHECK_CONNECTION = 'check-connection'
KIND_CLOSE_CONNECTION = 'close-connection'
KIND_OPEN_SESSION = 'open-session'
KIND_SESSION_RESIZE = 'session-resize'
KIND_SESSION_TAKE = 'session-take-control'
KIND_CLOSE_SESSION = 'close-session'
KIND_START_LOG = 'start-log'
KIND_STOP_LOG = 'stop-log'

# 结果状态
STATUS_OK = 'ok'
STATUS_ERROR = 'error'
STATUS_EXPIRED = 'expired'
STATUS_REJECTED = 'rejected'
STATUS_CANCELLED = 'cancelled'

# 正式文件之外的两种名字：本模块写的临时文件，以及云盘同步产生的冲突副本。
_TMP_MARKER = '.tmp-'
_CONFLICT_COPY_RE = re.compile(r' \(\d+\)(\.\w+)?$')
