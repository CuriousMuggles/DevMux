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
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import shutil
import platform
import re
import time
import threading
import uuid
import relay_file_io
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

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


# ----------------------------------------------------------------------
# 时间
# ----------------------------------------------------------------------
def now_iso() -> str:
    """本地时区、毫秒精度的 ISO 时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='milliseconds')


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
    return parsed


def _epoch_ms(value) -> float:
    parsed = _parse_iso(value)
    return parsed.timestamp() * 1000.0 if parsed else 0.0


# ----------------------------------------------------------------------
# 原子写 / 读
# ----------------------------------------------------------------------
def atomic_write_bytes(path: str, data: bytes, *, fsync: bool = False) -> None:
    """先写临时文件再改名。

    读取方只按正式名匹配，因此永远读不到写到一半的内容。这里依赖的是
    **改名的原子性**，而不是落盘持久性 —— 文件句柄关闭时内容已刷到共享
    服务器，所以默认不做 ``fsync``（SMB 上 fsync 代价明显）。

    改名在 Windows 上会因**另一个进程正好也在替换同一文件**而抛
    ``PermissionError``（WinError 5，共享冲突）。多实例同时启动时会稳定复现
    —— 两台电脑在同一共享目录上同时写 ``relay.json`` 就是这种情况。冲突窗口
    只有几毫秒，所以这里做有限次退避重试，而不是把一次正常的并发写变成启动失败。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f'{path}{_TMP_MARKER}{uuid.uuid4().hex[:8]}'
    try:
        with open(tmp, 'wb') as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        last_error: Optional[BaseException] = None
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # 共享冲突：短暂退避后重试
                last_error = exc
                time.sleep(REPLACE_BACKOFF_S * (attempt + 1))
        if last_error is not None:
            raise last_error
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, payload) -> None:
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def read_json(path: str) -> Optional[dict]:
    """读 JSON；文件不存在、瞬时不可读或内容损坏时返回 None。"""
    try:
        data = json.loads(relay_file_io.read_bytes(path).decode('utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_provisional(name: str) -> bool:
    """临时文件与云盘冲突副本都不是可消费的正式消息。"""
    return _TMP_MARKER in name or bool(_CONFLICT_COPY_RE.search(name))


def list_messages(directory: str, prefix: str, suffix: str = '.json') -> list[str]:
    """按文件名升序返回正式消息的完整路径。

    文件名里的时间戳是定宽 13 位毫秒，因此**字典序即时间序**。目录不可读时
    返回空列表，让调用方按「本轮没消息」处理。
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    picked = [
        name for name in names
        if name.startswith(prefix) and name.endswith(suffix) and not is_provisional(name)
    ]
    picked.sort()
    return [os.path.join(directory, name) for name in picked]


# ----------------------------------------------------------------------
# 控制面：请求与结果
# ----------------------------------------------------------------------
@dataclass
class Request:
    reqId: str
    kind: str
    peer: str
    device: dict
    payload: dict
    createdAt: str
    expiresAt: str

    def to_dict(self) -> dict:
        return {'schema': RELAY_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> 'Request':
        return cls(
            reqId=str(data.get('reqId', '')),
            kind=str(data.get('kind', '')),
            peer=str(data.get('peer', '')),
            device=dict(data.get('device') or {}),
            payload=dict(data.get('payload') or {}),
            createdAt=str(data.get('createdAt', '')),
            expiresAt=str(data.get('expiresAt', '')),
        )

    def is_expired(self, now: Optional[float] = None) -> bool:
        """是否已过期。

        ``expiresAt`` 缺失或无法解析时**按过期处理**（fail-closed）：宁可让用户
        重新下发，也不要执行一条 TTL 不明的命令。
        """
        deadline = _epoch_ms(self.expiresAt)
        if deadline <= 0:
            return True
        current = now if now is not None else time.time()
        return current * 1000.0 > deadline


@dataclass
class Result:
    reqId: str
    status: str
    exitCode: Optional[int] = None
    stdout: str = ''
    stderr: str = ''
    truncated: bool = False
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)
    agentId: str = ''
    startedAt: str = ''
    finishedAt: str = ''
    durationMs: int = 0

    def to_dict(self) -> dict:
        return {'schema': RELAY_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> 'Result':
        exit_code = data.get('exitCode')
        return cls(
            reqId=str(data.get('reqId', '')),
            status=str(data.get('status', '')),
            exitCode=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            stdout=str(data.get('stdout', '') or ''),
            stderr=str(data.get('stderr', '') or ''),
            truncated=bool(data.get('truncated')),
            error=data.get('error'),
            extra=dict(data.get('extra') or {}),
            agentId=str(data.get('agentId', '')),
            startedAt=str(data.get('startedAt', '')),
            finishedAt=str(data.get('finishedAt', '')),
            durationMs=int(data.get('durationMs') or 0),
        )

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def new_request(
    kind: str,
    peer: str,
    device: Optional[dict] = None,
    payload: Optional[dict] = None,
    ttl_s: float = DEFAULT_REQUEST_TTL_S,
) -> Request:
    created = datetime.now(timezone.utc).astimezone()
    ttl = max(1.0, float(ttl_s))
    return Request(
        reqId=uuid.uuid4().hex[:16],
        kind=kind,
        peer=peer,
        device=dict(device or {}),
        payload=dict(payload or {}),
        createdAt=created.isoformat(timespec='milliseconds'),
        expiresAt=(created + timedelta(seconds=ttl)).isoformat(timespec='milliseconds'),
    )


def make_req_filename(request: Request, seq: int) -> str:
    """``r-<ts_ms:13>-<seq:06>-<reqId>.json``：定宽保证字典序等于时间序。"""
    stamp = int(_epoch_ms(request.createdAt))
    if stamp <= 0:
        stamp = int(time.time() * 1000)
    return f'r-{stamp:013d}-{seq % 1000000:06d}-{request.reqId}.json'


# ----------------------------------------------------------------------
# 按对端分区
# ----------------------------------------------------------------------
# peerId 会被拼成目录名，而它最终来自「共享目录里读到的内容」——属于远端可控
# 输入。因此必须在入口处严格校验：写入端（配置）与读取端（心跳）都要过这道关。
_PEER_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$')


def valid_peer_id(value) -> bool:
    """peerId 是否可安全用作目录名。

    只允许字母数字开头、字母数字与 ``._-`` 组成、长度 ≤ 41，且不以 ``.`` 结尾
    （Windows 上以点结尾的目录名会被静默截断）。``.`` / ``..`` 因首字符限制
    天然被排除。
    """
    text = str(value or '')
    return bool(_PEER_ID_RE.match(text)) and not text.endswith('.')


def check_peer_id(value, what: str = 'peerId') -> str:
    text = str(value or '').strip()
    if not valid_peer_id(text):
        raise ValueError(
            f'{what} 非法：{value!r}（只允许字母数字开头，可含 . _ -，长度 1~41，不得以点结尾）'
        )
    return text


def choose_peer_id(preferred: str, taken=(), *, limit: int = 32) -> str:
    """给本实例挑一个**没被同机其他实例占用**的标识：``X`` → ``X-2`` → ``X-3``…

    为什么必须这么做：``peerId`` 是实例在总线上的身份 —— 心跳文件名、角色仲裁
    排序、远端设备 key 前缀都建在它上面。同一台机器开两个实例时，「退让到独立
    配置文件」并不能让它们天然区分：两个**重名**实例会把心跳写进**同一个文件**
    （破坏单写者原则，互相覆盖），读的时候 ``peer_id == self_id`` 还会被当成
    「自己」直接跳过。结果就是用户看到的「本机多开的实例在配对列表里根本不出现」。

    ``taken`` 由调用方从共享目录的心跳里筛出（同机、在线）。后缀从 ``-2`` 起，
    与界面既有的 ``suggestedPeerId`` 约定一致（第二个实例）。
    """
    base = _fit_peer_id(str(preferred or '').strip() or 'pc', limit)
    used = {str(item or '') for item in (taken or ())}
    if base not in used:
        return _fit_peer_id(base, limit)
    index = 2
    while True:
        suffix = f'-{index}'
        # **先给后缀腾出位置再裁**：名字已经顶到长度上限时，``base + '-2'`` 会被裁回
        # 与 base 一模一样的字符串 —— 那个候选永远在 used 里，循环就出不去了。
        head = _fit_peer_id(base, max(1, limit - len(suffix)))
        candidate = f'{head}{suffix}'
        if candidate not in used:
            return candidate
        index += 1


def _fit_peer_id(text: str, limit: int) -> str:
    """把标识裁进长度上限。首字符与非结尾点由调用方（清洗过的名字）保证。"""
    value = str(text or '').strip()
    return value[:limit].rstrip('.-_') or 'pc'


def partition_dir(root: str, slot: str, peer_id: Optional[str]) -> str:
    """把槽目录解析为按对端分区的路径。

    ``peer_id`` 为空时退回槽根目录（单从机的扁平布局与单元测试用）。
    """
    base = os.path.join(os.path.abspath(root), slot)
    if not peer_id:
        return base
    return os.path.join(base, check_peer_id(peer_id))


_FAST_WRITE_LOCK = threading.Lock()


class RelayQueue:
    """控制面：投递请求、幂等判定、回写结果、归档。

    各端只操作自己拥有的分区：主控只写 ``requests/<从机peerId>/``，从机只读
    该目录、只写 ``results/<自己>/``。归档与清理都只针对自己写入的文件。

    ``peer_id`` 为空时按槽根目录工作（兼容扁平布局）；实际运行一律用
    :meth:`for_peer` 取分区后的队列，否则多从机会读到同一批请求。
    """

    def __init__(self, root: str, peer_id: Optional[str] = None):
        self.root = os.path.abspath(root)
        self.peer_id = str(peer_id) if peer_id else None
        self.requests_dir = partition_dir(self.root, SLOT_REQUESTS, self.peer_id)
        self.results_dir = partition_dir(self.root, SLOT_RESULTS, self.peer_id)
        self.streams_dir = partition_dir(self.root, SLOT_STREAMS, self.peer_id)
        self.archive_dir = partition_dir(self.root, SLOT_ARCHIVE, self.peer_id)
        # 心跳是共享的总线：所有对端写同一个 peers/ 目录，靠文件名区分。
        self.peers_dir = partition_dir(self.root, SLOT_PEERS, None)
        self.manifest_path = os.path.join(self.root, 'relay.json')
        self._seq = 0
        self._fast_offsets = {}
        self._fast_seen = set()
        self._fast_results = {}
        self._completed_requests = set()

    def _append_fast(self, path, data):
        # 同一路由只有一个进程写入；锁只串行化本进程工作线程，不使用跨机锁。
        try:
            line = json.dumps(data, ensure_ascii=False).encode('utf-8') + b'\n'
            with _FAST_WRITE_LOCK:
                with open(path, 'ab') as stream:
                    # 通知日志只消费尾部：超限直接回卷（文件身份不变，同 pulse
                    # 套路），避免 .from-*.jsonl / .replies.jsonl 无限增长。
                    if stream.tell() >= FAST_JOURNAL_MAX_BYTES:
                        stream.truncate(0)
                    stream.write(line)
        except OSError:
            pass  # 通知通道不可用时仍可消费原来的独立JSON文件。

    def prepare_fast_results(self):
        self._append_fast(os.path.join(self.results_dir, '.replies.jsonl'), {})

    def _read_fast(self, path):
        """读取稳定文件名的追加通知，避免新文件负缓存；不依赖目录刷新。"""
        try:
            offset = self._fast_offsets.get(path)
            size = relay_file_io.stat(path).st_size
            if offset is None or offset > size:
                # 首次读取，或写入端已回卷（旧 offset 越过新文件末尾）：从尾部
                # 一段开始，既避免目录/大小陈旧属性，也让回卷后的日志立即可读。
                offset = max(0, size - 65536)
            data = relay_file_io.read_bytes(path, offset=offset)
            complete = data.rfind(b'\n') + 1
            if not complete:
                return []
            self._fast_offsets[path] = offset + complete
            lines = data[:complete].splitlines()
            messages = []
            for line in lines:
                try:
                    value = json.loads(line.decode('utf-8'))
                    if isinstance(value, dict):
                        messages.append(value)
                except (ValueError, UnicodeError):
                    pass  # 半条首记录（首次从文件末尾读取）或损坏记录不能阻塞后续。
            return messages
        except OSError:
            return []

    @classmethod
    def for_peer(cls, root: str, peer_id: str) -> 'RelayQueue':
        """取某个对端的分区队列。主控传从机 id，从机传自己的 id。"""
        return cls(root, peer_id=peer_id)

    def scoped(self, peer_id: str) -> 'RelayQueue':
        return RelayQueue(self.root, peer_id=peer_id)

    def stream(self, stream_id: str) -> 'StreamChannel':
        return StreamChannel(self.streams_dir, stream_id)

    def peer_ids(self) -> list[str]:
        """枚举共享目录里出现过的对端分区（含自己），用于发现与清理。"""
        try:
            names = os.listdir(os.path.join(self.root, SLOT_REQUESTS))
        except OSError:
            return []
        return sorted(name for name in names if os.path.isdir(os.path.join(self.root, SLOT_REQUESTS, name)))

    def ensure_layout(self) -> None:
        for directory in (
            self.requests_dir, self.results_dir, self.streams_dir,
            self.peers_dir, self.archive_dir,
        ):
            os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.manifest_path):
            atomic_write_json(self.manifest_path, {
                'schema': RELAY_SCHEMA,
                'layoutVersion': LAYOUT_VERSION,
                'createdAt': now_iso(),
                'requiredFeatures': list(BASE_FEATURES),
            })

    # --- 操作端侧 ---
    def submit(self, request: Request) -> str:
        """写入一条请求，返回其正式文件路径。"""
        self.ensure_layout()
        self._seq += 1
        path = os.path.join(self.requests_dir, make_req_filename(request, self._seq))
        atomic_write_json(path, request.to_dict())
        if valid_peer_id(request.peer):
            self._append_fast(os.path.join(self.requests_dir, f'.from-{request.peer}.jsonl'),
                              {'file': os.path.basename(path), 'request': request.to_dict()})
        return path

    def pending_requests(self, source_peer_ids=None) -> list[tuple[str, Request]]:
        """设备端取待处理请求，按时间序升序，损坏或半截的文件自动跳过。"""
        pending: list[tuple[str, Request]] = []
        sources = set(source_peer_ids or [])
        try:
            sources.update(name[6:-6] for name in os.listdir(self.requests_dir)
                           if name.startswith('.from-') and name.endswith('.jsonl'))
        except OSError:
            pass
        for source in sources:
            if not valid_peer_id(source):
                continue
            for data in self._read_fast(os.path.join(self.requests_dir, f'.from-{source}.jsonl')):
                filename = str(data.get('file') or '')
                body = data.get('request')
                if not filename.startswith('r-') or not filename.endswith('.json') or os.path.basename(filename) != filename:
                    continue
                if not isinstance(body, dict):
                    continue
                request = Request.from_dict(body)
                if request.peer != source or request.reqId in self._fast_seen:
                    continue
                self._fast_seen.add(request.reqId)
                if self.has_result(request.reqId):
                    continue
                pending.append((os.path.join(self.requests_dir, filename), request))
        fast_ids = {request.reqId for _, request in pending}
        for path in list_messages(self.requests_dir, 'r-'):
            data = read_json(path)
            if not data:
                continue
            request = Request.from_dict(data)
            if request.reqId not in fast_ids:
                pending.append((path, request))
        pending.sort(key=lambda item: os.path.basename(item[0]))
        return pending

    # --- 结果 ---
    def result_path(self, req_id: str) -> str:
        return os.path.join(self.results_dir, f'{req_id}.json')

    def has_result(self, req_id: str) -> bool:
        """幂等判定的依据：结果已存在即视为该请求已处理，不重复执行。"""
        if req_id in self._completed_requests or os.path.isfile(self.result_path(req_id)):
            return True
        return self._result_archived(req_id)

    def _result_archived(self, req_id):
        # 追加通知在重启后仍存在；已经消费并归档的结果也必须阻止重放。
        current = datetime.now()
        previous = current.replace(day=1) - timedelta(days=1)
        if any(os.path.isfile(os.path.join(self.archive_dir, stamp.strftime('%Y-%m'), f'{req_id}.json'))
               for stamp in (current, previous)):
            return True
        import relay_archive
        return relay_archive.has_archived_result(self.archive_dir, req_id)

    def write_result(self, result: Result) -> str:
        self.ensure_layout()
        path = self.result_path(result.reqId)
        atomic_write_json(path, result.to_dict())
        self._completed_requests.add(result.reqId)
        self._append_fast(os.path.join(self.results_dir, '.replies.jsonl'), result.to_dict())
        return path

    def read_result(self, req_id: str) -> Optional[Result]:
        for data in self._read_fast(os.path.join(self.results_dir, '.replies.jsonl')):
            if data.get('reqId'):
                self._fast_results[data['reqId']] = data
        if req_id in self._fast_results:
            if not self._result_archived(req_id):
                return Result.from_dict(self._fast_results[req_id])
            self._fast_results.pop(req_id, None)
        data = read_json(self.result_path(req_id))
        return Result.from_dict(data) if data else None

    # --- 归档与清理（各端只处理自己写的文件）---
    @staticmethod
    def _message_exists(path: str) -> bool:
        """共享目录下的存在性判定：绕过 SMB 负缓存，容忍短时传播延迟。

        普通 ``os.path.isfile`` 对“从没打开过”的远端新文件会命中负缓存而误判
        缺失 —— 主控经 jsonl 快速通道读回执时从不打开独立文件，结果文件因此
        一直滞留 ``results/``（实测近 2 万个）。这里用 ``relay_file_io`` 的
        无缓存 stat，并重试一次吸收属性传播延迟。
        """
        for _ in range(ARCHIVE_EXISTS_RETRIES):
            try:
                relay_file_io.stat(path)
                return True
            except OSError:
                time.sleep(ARCHIVE_EXISTS_RETRY_S)
        return False

    def archive_file(self, path: str) -> Optional[str]:
        """把已消费的消息移入 ``archive/<yyyy-mm>/``，不删除以保留审计痕迹。"""
        if not path or not self._message_exists(path):
            return None
        target_dir = os.path.join(self.archive_dir, time.strftime('%Y-%m'))
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(path))
        if os.path.exists(target):
            target = f'{target}.{uuid.uuid4().hex[:6]}'
        try:
            os.replace(path, target)
        except OSError:
            # 共享目录上 rename 偶尔因传播延迟失败，重试一次再放弃。
            try:
                time.sleep(ARCHIVE_EXISTS_RETRY_S)
                os.replace(path, target)
            except OSError:
                return None
        if os.path.dirname(path) == self.results_dir:
            self._fast_results.pop(os.path.basename(path).removesuffix('.json'), None)
        return target

    def prune_archive(self, retention_days: int = ARCHIVE_RETENTION_DAYS) -> int:
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        removed = 0
        for dirpath, _dirnames, filenames in os.walk(self.archive_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                        removed += 1
                except OSError:
                    continue
        return removed


# ----------------------------------------------------------------------
# 数据面：单向追加流
# ----------------------------------------------------------------------
class StreamTail:
    """按字节偏移增量读取一条单向追加流。

    两个容易踩的坑：

    1. 不用 ``os.path.getsize`` 取长度 —— Windows SMB 客户端会缓存目录元数据，
       可能读到陈旧的旧长度。改用**新打开的句柄** ``seek(0, SEEK_END)`` 取，
       网络文件需额外禁用系统数据缓存，并按目录项取最新长度。
    2. ``size < offset`` 说明文件被重建（归档后同名重建），此时 offset 归零重读；
       否则 seek 会越过 EOF，之后永久读到空。
    """

    def __init__(self, path: str, offset: int = 0):
        self.path = os.path.abspath(path)
        self.offset = max(0, int(offset))
        self.rebuilt = False

    def read_new(self, limit: int = STREAM_READ_CHUNK) -> bytes:
        self.rebuilt = False
        try:
            if relay_file_io.is_remote(self.path):
                size = relay_file_io.stat(self.path).st_size
                if size < self.offset:
                    self.offset = 0
                    self.rebuilt = True
                data = relay_file_io.read_bytes(self.path, self.offset, limit)
                self.offset += len(data)
                return data
            with open(self.path, 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size < self.offset:
                    self.offset = 0
                    self.rebuilt = True
                if size <= self.offset:
                    return b''
                handle.seek(self.offset)
                data = handle.read(limit)
        except OSError:
            return b''
        self.offset += len(data)
        return data

    def to_dict(self) -> dict:
        return {'offset': self.offset}


class StreamWriter:
    """追加写一条单向流。单写者，因此不需要加锁。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    def append(self, data: bytes) -> bool:
        if not data:
            return True
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, 'ab') as handle:
                handle.write(data)
        except OSError:
            return False
        return True


class StreamChannel:
    """一条会话/日志流的三个文件。

    ``.out`` 由设备端写、操作端读；``.in`` 由操作端写、设备端读；
    ``.meta.json`` 由设备端写。三者都是单写者，互不冲突。
    """

    def __init__(self, streams_dir: str, stream_id: str):
        self.streams_dir = os.path.abspath(streams_dir)
        self.stream_id = stream_id
        self.out_path = os.path.join(self.streams_dir, f'{stream_id}.out')
        self.in_path = os.path.join(self.streams_dir, f'{stream_id}.in')
        self.meta_path = os.path.join(self.streams_dir, f'{stream_id}.meta.json')

    def output_tail(self, offset: int = 0) -> StreamTail:
        return StreamTail(self.out_path, offset)

    def input_tail(self, offset: int = 0) -> StreamTail:
        return StreamTail(self.in_path, offset)

    def output_writer(self) -> StreamWriter:
        return StreamWriter(self.out_path)

    def input_writer(self) -> StreamWriter:
        return StreamWriter(self.in_path)

    def write_meta(self, meta: dict, *, in_place: bool = False) -> None:
        payload = {
            'schema': RELAY_SCHEMA,
            'streamId': self.stream_id,
            **meta,
        }
        if not in_place:
            atomic_write_json(self.meta_path, payload)
            return
        # 共享PTY状态由会话锁串行发布。保持文件对象，避免SMB继续指向替换前的对象。
        # 读端遇到正在写入的半条JSON会忽略，下一轮重读，不把它当作结束。
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        os.makedirs(self.streams_dir, exist_ok=True)
        try:
            handle = open(self.meta_path, 'r+b')
        except FileNotFoundError:
            handle = open(self.meta_path, 'w+b')
        with handle:
            handle.seek(0)
            handle.write(data)
            handle.truncate()

    def read_meta(self, *, report_missing: bool = False) -> Optional[dict]:
        # 控制权与退出状态不能沿用SMB缓存的旧对象；与终端字节流一样直接读取。
        try:
            data = json.loads(relay_file_io.read_bytes(self.meta_path).decode('utf-8'))
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return {'status': 'missing'} if report_missing else None
        except (OSError, ValueError, UnicodeError):
            # 访问故障/半条数据不能冒充明确的会话不存在。
            return None


# ----------------------------------------------------------------------
# 心跳
# ----------------------------------------------------------------------
@dataclass
class PeerHeartbeat:
    peerId: str
    role: str
    appVersion: str = ''
    pid: int = 0
    startedAt: str = ''
    heartbeatAt: str = ''
    devices: list = field(default_factory=list)
    # 以下字段用于「连接前提示主/从」与防重名：指纹变化即要求重新确认。
    fingerprint: str = ''
    carrier: str = ''
    linkRttMs: float = 0.0
    host: str = ''
    # **实例级**身份：同机多开时每个实例一个，持久化在本机、跨重启不变。
    #
    # 为什么不复用 ``fingerprint``：指纹是**机器级**的（由 MachineGuid 派生），同机
    # 多开的两个实例指纹一模一样，正是 ``is_self_ghost`` / ``is_superseded_ghost``
    # 用来识别「同机旧实例」的依据，语义不能动。而配对要的是「这个实例是谁」，
    # 必须能区分同机的两个实例 —— 所以另开一层。
    #
    # 老版本写的心跳没有这两个字段，读出来是空串/0；配对启用时它们会被判为
    # 「未配对」并**拒绝放行**（fail-closed），提示里会点明对端版本过旧。
    nodeId: str = ''
    nodeSlot: int = 0
    # 读取时由 ``read_peers`` 填上的**共享目录 mtime**，属于服务器时钟。它不是
    # 心跳内容的一部分，不写入文件（``to_dict`` 会剔除），只用于跨机时效判定。
    fileMtimeMs: float = 0.0
    heartbeatSeq: int = 0
    uptimeSeconds: int = 0
    # 可选控制通道能力；缺失表示只支持旧逐文件队列。
    controlJournal: int = 0
    # 写者当前根的代次前缀；本机路径可跨端不同，读端只与该来源的公告比较。
    connectionScope: str = ''
    # 只属于读端的观测状态，不能写回共享目录或配置。
    sequenceObserved: bool = False
    lastSequenceAdvanceMono: float = -1.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop('fileMtimeMs', None)
        data.pop('sequenceObserved', None)
        data.pop('lastSequenceAdvanceMono', None)
        return {'schema': RELAY_SCHEMA, **data}

    @classmethod
    def from_dict(cls, data: dict) -> 'PeerHeartbeat':
        devices = data.get('devices')
        return cls(
            peerId=str(data.get('peerId', '')),
            role=str(data.get('role', '')),
            appVersion=str(data.get('appVersion', '')),
            pid=int(data.get('pid') or 0),
            startedAt=str(data.get('startedAt', '')),
            heartbeatAt=str(data.get('heartbeatAt', '')),
            devices=list(devices) if isinstance(devices, list) else [],
            fingerprint=str(data.get('fingerprint', '')),
            carrier=str(data.get('carrier', '')),
            linkRttMs=float(data.get('linkRttMs') or 0.0),
            host=str(data.get('host', '')),
            nodeId=str(data.get('nodeId', '')),
            nodeSlot=int(data.get('nodeSlot') or 0),
            heartbeatSeq=max(0, int(data.get('heartbeatSeq') or 0)),
            uptimeSeconds=max(0, int(data.get('uptimeSeconds') or 0)),
            controlJournal=max(0, int(data.get('controlJournal') or 0)),
            connectionScope=str(data.get('connectionScope') or ''),
        )

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    @property
    def server_skew_ms(self) -> float:
        """写者本机时钟相对**共享目录服务器时钟**的偏差（服务器 - 本机，毫秒）。

        心跳文件的 mtime 由共享目录服务器打戳，而 ``heartbeatAt`` 是写者本机的
        墙上时钟；两者之差就是写者时钟相对服务器的偏差。量它不需要额外探针文件：
        本机自己写的心跳就在同一个目录里，它的 mtime 与本机写下的 ``heartbeatAt``
        一对，偏差就出来了，而且每 2 秒随心跳刷新（见 ``observer_clock_skew_ms``）。

        读不到 mtime（非共享目录载体、stat 失败）时返回 0：那种情况下两端本来就在
        同一台机器上，没有偏差可言。
        """
        if self.fileMtimeMs <= 0:
            return 0.0
        beat = _epoch_ms(self.heartbeatAt)
        if beat <= 0:
            return 0.0
        return float(self.fileMtimeMs) - beat

    def age_ms(self, now: Optional[float] = None, *, skew_ms: float = 0.0) -> float:
        """距上次心跳的时长（毫秒）。

        **不能**直接拿 ``heartbeatAt`` 减本机时钟：那是写者机器的墙上时钟，而两台
        机器的时钟差几百秒是常态（现场实测两台 PC 差 393s、PC 与共享服务器差 669s）。
        据此算出来的「年龄」完全不可用 —— 快的一方永远看不到慢的一方在线，慢的一方
        反而把快的一方当成刚心跳过，于是出现「从机说已连接、主控说没发现在线的从机」
        这种自相矛盾的状态。

        所以优先用**心跳文件的 mtime**：它由共享目录服务器打戳，是所有端共同的时基。
        把本机时钟按 ``skew_ms``（服务器时钟 - 本机时钟，由 ``observer_clock_skew_ms``
        从本机自己的心跳上量得）归算过去，再与该文件的 mtime 相减即可。

        mtime 拿不到时退回「对端时间戳 vs 本机时钟」的老算法：只有同机（同进程或
        同机双实例）才会走到这里，同机没有时钟偏差，老算法是对的。

        返回 ``-1.0`` 表示这条心跳既没有 mtime 也没有可解析的时间戳，调用方应判离线。
        """
        if self.sequenceObserved:
            if self.lastSequenceAdvanceMono < 0:
                return -1.0  # 初见旧值不能证明在线，必须观察到下一次推进。
            return max(0.0, (time.monotonic() - self.lastSequenceAdvanceMono) * 1000.0)
        return _shared_age_ms(self.fileMtimeMs, self.heartbeatAt, now, skew_ms)

    def is_stale(
        self,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        now: Optional[float] = None,
        *,
        skew_ms: float = 0.0,
    ) -> bool:
        """超过 ``interval × HEARTBEAT_MISS_COUNT + 容差`` 未刷新即判离线。

        容差是给**跨机共享目录**留的：SMB 客户端会缓存文件属性（默认 1 秒量级），
        再加上一次慢的设备枚举就可能把两拍心跳拉到 4~5 秒（实测 4.6s），刚好顶到
        6 秒门槛上。判离线本来就不需要精确到秒（新操作在本地 fail-fast，不等超时），
        所以宁可放宽，也不要让健康的对端在两拍之间闪一下离线。
        """
        age = self.age_ms(now, skew_ms=skew_ms)
        if age < 0:
            return True
        if self.sequenceObserved:
            # 每秒发布一次；连续3秒未观察到序号推进即离线，恢复推进自动上线。
            return age > 3000.0
        threshold = max(1.0, float(interval_s)) * HEARTBEAT_MISS_COUNT * 1000.0
        return age > threshold + HEARTBEAT_GRACE_MS


class HeartbeatSequenceObserver:
    """读端独立计时，只认同一启动代次递增序号，不使用墙钟或mtime。"""

    def __init__(self):
        self._states = {}
        self._retired = {}
        self._lock = threading.Lock()

    def observe(self, beat, *, channel='', now=None):
        if beat.heartbeatSeq <= 0:
            return beat  # 旧端保留原共享目录时基的兼容规则。
        observed_at = time.monotonic() if now is None else float(now)
        key = (channel, beat.peerId)
        identity = (beat.nodeId, beat.pid, beat.startedAt)
        with self._lock:
            state = self._states.get(key)
            if state is None or state[0] != identity:
                retired = self._retired.setdefault(key, set())
                if identity in retired:
                    return replace(beat, sequenceObserved=True, lastSequenceAdvanceMono=-1.0)
                if state is not None:
                    retired.add(state[0])
                state = (identity, beat.heartbeatSeq, -1.0, beat)
            elif beat.heartbeatSeq > state[1]:
                state = (identity, beat.heartbeatSeq, observed_at, beat)
            # 同值、缓存回退和迟到的旧快照都不能延长在线期限。
            self._states[key] = state
            return replace(state[3], sequenceObserved=True, lastSequenceAdvanceMono=state[2])


def host_fingerprint(salt: str = 'adb-manager-relay') -> str:
    """本机稳定指纹，用于识别「同一个 peerId 被两台机器使用」。

    **这不是安全凭证**：它由机器标识与主机名派生，可被伪造，只用于发现重名
    与配置漂移。真正的信任边界是显式确认过的 peerId 与命令白名单。
    """
    seed = ''
    if platform.system() == 'Windows':
        try:
            import winreg  # noqa: PLC0415 - 仅 Windows 需要

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Cryptography',
            ) as handle:
                seed = str(winreg.QueryValueEx(handle, 'MachineGuid')[0])
        except (OSError, ImportError, AttributeError):
            seed = ''
    if not seed:
        seed = platform.node() or 'unknown-host'
    raw = f'{salt}|{seed}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def peer_heartbeat_path(peers_dir: str, peer_id: str) -> str:
    return os.path.join(peers_dir, f'{check_peer_id(peer_id)}.json')


def write_heartbeat(peers_dir: str, beat: PeerHeartbeat, *, snapshot: bool = False,
                    pulse_only: bool = False, publish_pulse: bool = True) -> str:
    """写入自己的心跳。本机 peerId 非法属于配置错误，直接抛出而不是静默跳过。"""
    beat.peerId = check_peer_id(beat.peerId)
    path = peer_heartbeat_path(peers_dir, beat.peerId)
    if snapshot and publish_pulse:
        # 同名 JSON 的原子替换可能被 SMB 继续缓存旧对象；独立追加日志保持文件身份。
        pulse_path = heartbeat_pulse_path(peers_dir, beat)
        with open(pulse_path, 'ab') as pulse:
            # 同一文件对象内回卷，避免长期运行无限增长；绝不替换文件身份。
            if pulse.tell() >= 2 * 1024 * 1024:
                pulse.truncate(0)
            pulse.write((json.dumps(beat.to_dict(), ensure_ascii=False) + '\n').encode('utf-8'))
    if pulse_only:
        return path
    atomic_write_json(path, beat.to_dict())
    if snapshot:
        # 唯一文件名避免 SMB 长期复用同一路径的旧内容；旧端仍读取原文件。
        prefix = f'{beat.peerId}.beat-{beat.pid}-'
        snapshot_path = os.path.join(peers_dir, f'{prefix}{uuid.uuid4().hex}.beat')
        atomic_write_json(snapshot_path, beat.to_dict())
        # 只清理本机自己 peerId 的快照，保留最新三份供并发读取；不删除别人的历史。
        # 同一 peerId 是单写者（含重启后的新 pid 也属同一身份的延续），所以按
        # peerId 全量保留三份即可 —— 旧实现只清当前 pid 前缀，每次重启都会把
        # 上一个实例的快照永久留下（实测 peers/ 里累积了 250 个 500B 的 beat 文件）。
        # 一次目录枚举取得元数据，避免每个快照再发一次 SMB 查询。
        own = []
        beat_pattern = re.compile(rf'^{re.escape(beat.peerId)}\.beat-\d+-[0-9a-f]+\.beat$')
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                if not beat_pattern.match(entry.name):
                    continue
                try:
                    own.append((entry.stat().st_mtime, entry.path))
                except OSError:
                    continue
        own.sort()
        for _mtime, old in own[:-3]:
            try:
                os.unlink(old)
            except OSError:
                pass
    return path


def heartbeat_pulse_path(peers_dir: str, beat: PeerHeartbeat) -> str:
    generation = hashlib.sha256(beat.startedAt.encode('utf-8')).hexdigest()[:12]
    return os.path.join(peers_dir, f'{check_peer_id(beat.peerId)}.pulse-{int(beat.pid)}-{generation}')


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
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True  # 拿不准时按「还活着」处理：宁可留着，不可误删活跃对端


def purge_dead_pulses(peers_dir: str, peer_id: str, *, self_pid: int) -> list[str]:
    """清理本机自己 peerId 下、已停止进程遗留的 pulse 心跳日志，返回删除的文件名。

    ``peers/<peerId>.pulse-<pid>-<generation>`` 是实例运行期的追加日志（2MB 上限），
    正常退出时由 ``drop_own_pulse`` 删除；**异常退出**（崩溃、断电）会留下一个
    1-2MB 的文件，每次重启累积一个。同一 peerId 单写者，启动时把「pid 已不存活」
    的 pulse 清掉即可回收空间；pid 仍存活（防御性保留同机兄弟）的一律不碰。
    """
    removed: list[str] = []
    pattern = re.compile(rf'^{re.escape(check_peer_id(peer_id))}\.pulse-(\d+)-[0-9a-f]{{12}}$')
    try:
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                match = pattern.match(entry.name)
                if not match:
                    continue
                if int(match.group(1)) == int(self_pid) or _pid_alive(int(match.group(1))):
                    continue
                try:
                    os.unlink(entry.path)
                    removed.append(entry.name)
                except OSError:
                    continue
    except OSError:
        pass
    return removed


def drop_own_pulse(peers_dir: str, beat: PeerHeartbeat) -> bool:
    """正常退出时删除本机自己的 pulse 心跳日志（谁写谁清）。"""
    try:
        path = heartbeat_pulse_path(peers_dir, beat)
        if os.path.isfile(path):
            os.unlink(path)
            return True
    except OSError:
        pass
    return False


def read_heartbeat_pulse(peers_dir: str, beat: PeerHeartbeat) -> PeerHeartbeat:
    """只读尾部完整记录；大小属性陈旧时继续读到真实 EOF，不截掉新心跳。"""
    path = heartbeat_pulse_path(peers_dir, beat)
    try:
        info = relay_file_io.stat(path)
        offset = max(0, info.st_size - 65536)
        content = relay_file_io.read_bytes(path, offset)
        if offset and not content:
            # 回卷后旧大小属性可能仍大于真实文件，归零读取完整新一拍。
            content = relay_file_io.read_bytes(path)
        lines = content.split(b'\n')[:-1]
        for line in reversed(lines):
            try:
                latest = PeerHeartbeat.from_dict(json.loads(line.decode('utf-8')))
            except (ValueError, TypeError, UnicodeError):
                continue
            if (latest.peerId != beat.peerId or latest.pid != beat.pid
                    or latest.startedAt != beat.startedAt or latest.nodeId != beat.nodeId):
                continue
            if (latest.heartbeatSeq >= beat.heartbeatSeq if latest.heartbeatSeq
                    else latest.heartbeatAt >= beat.heartbeatAt):
                latest.fileMtimeMs = relay_file_io.stat(path).st_mtime * 1000.0
                return latest
            break
    except OSError:
        pass
    return beat


def read_peers(peers_dir: str) -> dict[str, PeerHeartbeat]:
    """读取全部对端心跳；损坏、半截或 peerId 非法的记录一律跳过。

    peerId 会被拼成目录名与文件名，而它来自共享目录里的内容，因此这里必须
    过滤：一个写着 ``../../`` 的心跳文件不能变成上层的路径片段。

    顺带把每个文件的 **mtime** 记进 ``PeerHeartbeat.fileMtimeMs``：它由共享目录
    服务器打戳，是跨机判断「这条心跳还新不新鲜」时唯一可信的时基（见
    ``PeerHeartbeat.age_ms``）。stat 失败只意味着退化成老算法，不该让整条心跳消失。
    """
    peers: dict[str, PeerHeartbeat] = {}
    # 按文件名先分组并选最新版本；旧快照只在新文件已消失或损坏时回退。
    # 共享目录的目录项本身携带 mtime，无需逐文件重复枚举整个目录。
    candidates = {}
    try:
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                if entry.name.endswith('.json'):
                    identity = entry.name[:-5]
                    priority = 0
                else:
                    match = re.fullmatch(r'(.+)\.beat-\d+-[0-9a-f]+\.beat', entry.name)
                    if not match:
                        continue
                    identity = match.group(1)
                    priority = 1
                if not valid_peer_id(identity):
                    continue
                try:
                    mtime = entry.stat().st_mtime * 1000.0
                except OSError:
                    continue
                candidates.setdefault(identity, []).append((mtime, priority, entry.path))
    except OSError:
        return peers
    for identity, versions in candidates.items():
        if relay_file_io.is_remote(peers_dir):
            # SMB 目录项可滞后数秒。直接查询稳定心跳的原生句柄补充最新 mtime，
            # 内容仍经无缓存读取；唯一快照作为兼容与瞬时替换的回退。
            stable_path = peer_heartbeat_path(peers_dir, identity)
            try:
                fresh_mtime = relay_file_io.stat(stable_path).st_mtime * 1000.0
                versions = [v for v in versions if v[2] != stable_path]
                versions.append((fresh_mtime, 0, stable_path))
            except OSError:
                pass
        for mtime, _priority, path in sorted(versions, reverse=True):
            data = read_json(path)
            if not data:
                continue
            beat = PeerHeartbeat.from_dict(data)
            if not valid_peer_id(beat.peerId):
                continue
            beat.fileMtimeMs = mtime
            beat = read_heartbeat_pulse(peers_dir, beat)
            previous = peers.get(beat.peerId)
            if previous is None or beat.fileMtimeMs >= previous.fileMtimeMs:
                peers[beat.peerId] = beat
            break
    return peers


def observer_clock_skew_ms(
    peers: Optional[dict[str, 'PeerHeartbeat']],
    self_id: str,
) -> float:
    """从**本机自己**的心跳记录量出「共享目录服务器时钟 - 本机时钟」（毫秒）。

    跨机时效判定要把本机时钟归算到共享目录服务器的时钟上，而服务器时钟拿不到
    ——除非借一份本机自己写的、由服务器打戳的文件。本机的心跳就是：它由
    ``RelayNode._beat`` 每 2 秒写一次，mtime 由服务器打戳，与本机写下的
    ``heartbeatAt`` 之差就是偏差。不需要额外探针文件，也不需要两端约定新字段，
    所以**只升级一端也能修好单向可见**。

    拿不到（本机心跳还没写、载体不是共享目录）时返回 0：那时退化成各机自比的
    老行为，对同机实例是对的。
    """
    beat = (peers or {}).get(str(self_id or ''))
    if beat is None:
        return 0.0
    return beat.server_skew_ms


def purge_self_heartbeats(
    peers_dir: str,
    *,
    fingerprint: str,
    pid: int,
    started_at: str,
    keep_peer_id: str,
) -> list[str]:
    """删掉**本机自己**旧标识残留的心跳，返回被删掉的标识。

    只动 ``is_own_process_leftover`` 判定为「同机同进程的另一次身份」的文件 —— 那本来
    就是本进程自己写的，符合「谁写谁清」。刻意**不**用更宽的 ``is_self_ghost``：那条
    还会把「上一个进程留下的残留」算进来，而别人的进程写的文件不该由本机删（单写者
    原则）。那种残留交给 ``evaluate_health`` 的幽灵过滤在显示层忽略，文件留着无害。
    文件读不到或删不掉（共享目录只读、被占用）都只是失败，不影响启动。
    """
    removed: list[str] = []
    for peer_id, beat in read_peers(peers_dir).items():
        if peer_id == keep_peer_id:
            continue
        if not is_own_process_leftover(
            beat, self_fingerprint=fingerprint, self_pid=pid,
            self_started_at=started_at,
        ):
            continue
        try:
            os.remove(peer_heartbeat_path(peers_dir, peer_id))
            removed.append(peer_id)
        except OSError:
            continue
    return removed


# ----------------------------------------------------------------------
# 共享目录的盘点与清空（换目录时的「沿用 / 清空后重建」）
# ----------------------------------------------------------------------
# 用户把共享目录指到一个用过（或别人用过）的文件夹时，里面可能躺着上一轮的
# 心跳、消息队列甚至别的机器的分区。沿用通常没问题（身份靠指纹与确认名单），
# 但用户也可能想从零开始。这里提供**只认中转自有文件**的盘点与清理 —— 绝不
# 碰目录里的其他文件，也绝不删目录本身。


def _slot_dir(root: str, slot: str) -> str:
    return os.path.join(root, slot)


def inspect_relay_root(root: str) -> dict:
    """盘点一个目录里有多少中转自有数据，供前端决定要不要问「沿用还是清空」。"""
    root = str(root or '')
    info = {
        'root': root,
        'exists': bool(root) and os.path.isdir(root),
        'hasData': False,
        'manifest': False,
        'peers': 0,
        'requests': 0,
        'results': 0,
        'streams': 0,
        'archive': 0,
        'registry': 0,
    }
    if not info['exists']:
        return info
    info['manifest'] = os.path.isfile(os.path.join(root, 'relay.json'))
    for slot in (SLOT_PEERS, SLOT_REQUESTS, SLOT_RESULTS, SLOT_STREAMS, SLOT_ARCHIVE, SLOT_REGISTRY):
        try:
            count = len(os.listdir(_slot_dir(root, slot)))
        except OSError:
            count = 0
        info[slot] = count
    info['hasData'] = bool(
        info['manifest'] or info['peers'] or info['requests']
        or info['results'] or info['streams'] or info['archive'] or info['registry']
    )
    return info


def _cache_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _cache_link(path: str) -> bool:
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def validate_cache_root(root: str, protected_paths=()) -> str:
    """校验全量清理的根，拒绝危险祖先和根路径上的目录链接。"""
    value = str(root or '').strip()
    if not value or not os.path.isabs(value):
        raise ValueError('清除缓存必须使用已保存的绝对共享目录')
    if value[:4] in ("\\" * 2 + "?" + "\\", "\\" * 2 + "." + "\\"):
        raise ValueError('共享目录不能使用设备命名空间路径')
    spelled = _cache_path(value)
    target = _cache_path(os.path.realpath(value))
    if os.path.dirname(spelled) == spelled:
        raise ValueError('不能清空盘根或共享根目录')
    protected = [os.path.expanduser('~'), os.environ.get('SystemRoot', '')]
    protected.extend(protected_paths)
    for item in protected:
        if not item:
            continue
        for path in (_cache_path(str(item)), _cache_path(os.path.realpath(str(item)))):
            try:
                contains = os.path.commonpath((target, path)) == target
            except ValueError:
                contains = False  # 不同盘符没有交集。
            if contains:
                raise ValueError('共享目录包含本机程序、配置或系统目录，不能全量清除')
    current = spelled
    while os.path.dirname(current) != current:
        if os.path.lexists(current) and _cache_link(current):
            raise ValueError('共享目录根路径不能经过符号链接或目录联接点')
        current = os.path.dirname(current)
    if os.path.lexists(target) and not os.path.isdir(target):
        raise ValueError('共享目录不是文件夹')
    return target


def clear_cache_root(root: str, protected_paths=(), *, peer_id: str, pool_id: str = '', active_requests=None, scan_state=None) -> dict:
    """人工清理共享根内可确认闲置的缓存，保留活动文件和最小去重依据。"""
    from relay_cache import clear_idle_cache
    return clear_idle_cache(root, protected_paths, peer_id=peer_id, pool_id=pool_id,
                            active_requests=active_requests, scan_state=scan_state)



def clean_relay_root(root: str) -> list[str]:
    """清空目录里的中转自有数据，返回删除的相对路径（供界面流水展示）。

    白名单制：只删 ``relay.json``（布局清单）和六个槽目录；目录本身和其它任何
    文件一概不碰。目录不存在或没有任何中转数据时返回空列表（不报错 —— 对调用
    方来说「本来就干净」和「没删成」都应该继续走保存流程，由前端提示差异）。

    注意 ``registry/`` 也在白名单里：清了共享目录就等于把池成员名册一起作废，这是
    「清空后重建」的应有语义（两端都停时才会有人这么做）。槽位由租约回收，不依赖
    这份名册被保留。
    """
    root = str(root or '')
    if not root or not os.path.isdir(root):
        return []
    removed: list[str] = []
    manifest = os.path.join(root, 'relay.json')
    if os.path.isfile(manifest):
        try:
            os.remove(manifest)
            removed.append('relay.json')
        except OSError:
            pass
    for slot in (SLOT_PEERS, SLOT_REQUESTS, SLOT_RESULTS, SLOT_STREAMS, SLOT_ARCHIVE, SLOT_REGISTRY):
        path = _slot_dir(root, slot)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
            removed.append(slot + '/')
        except OSError:
            continue  # 被占用（有实例正在用它）就留着 —— 清不干净不算致命
    return removed


# ----------------------------------------------------------------------
# 可信对端（首次连接确认）
# ----------------------------------------------------------------------
# 共享目录的写权限只是第一道防线：能写目录就等于能向设备下发命令，所以即
# 使在同一信任域内，**新出现的 peerId 也必须由人确认一次**后才接受其请求、
# 才向它投递。确认记录存在本机（不进共享目录），因此每个应用各自持有名单。
@dataclass
class TrustRecord:
    peerId: str
    fingerprint: str = ''
    role: str = ''
    confirmedAt: str = ''
    note: str = ''

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'TrustRecord':
        return cls(
            peerId=str(data.get('peerId', '')),
            fingerprint=str(data.get('fingerprint', '')),
            role=str(data.get('role', '')),
            confirmedAt=str(data.get('confirmedAt', '')),
            note=str(data.get('note', '')),
        )


class TrustStore:
    """本机维护的可信对端名单（本地文件，不落共享目录）。"""

    def __init__(self, path: str, *, storage=None):
        self.path = os.path.abspath(path)
        self.storage = storage
        self._records: dict[str, TrustRecord] = {}
        self._loaded = False

    def load(self) -> dict[str, TrustRecord]:
        data = self.storage.read() if self.storage is not None else (read_json(self.path) or {})
        records = data.get('peers') if isinstance(data.get('peers'), dict) else {}
        self._records = {
            str(key): TrustRecord.from_dict({**value, 'peerId': value.get('peerId') or key})
            for key, value in records.items() if isinstance(value, dict)
        }
        self._loaded = True
        return dict(self._records)

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> bool:
        if self.storage is not None:
            try:
                self.storage.write({
                    'schema': RELAY_SCHEMA,
                    'peers': {key: record.to_dict() for key, record in self._records.items()},
                })
            except OSError:
                # 写失败不能让内存中的确认绕过持久化或继续覆盖较新的撤销记录。
                self._records = {}
                self._loaded = False
                raise
            return True
        try:
            atomic_write_json(self.path, {
                'schema': RELAY_SCHEMA,
                'peers': {key: record.to_dict() for key, record in self._records.items()},
            })
        except OSError:
            return False
        return True

    def items(self) -> list[TrustRecord]:
        self._ensure()
        return list(self._records.values())

    def get(self, peer_id: str) -> Optional[TrustRecord]:
        self._ensure()
        return self._records.get(str(peer_id))

    def is_trusted(self, peer_id: str) -> bool:
        return self.check(peer_id)[0]

    def confirm(
        self,
        peer_id: str,
        *,
        fingerprint: str = '',
        role: str = '',
        note: str = '',
    ) -> TrustRecord:
        """把对端加入名单；重复确认视为更新（例如指纹或角色变化后重新确认）。"""
        self._ensure()
        record = TrustRecord(
            peerId=str(peer_id),
            fingerprint=str(fingerprint or ''),
            role=normalize_role(role) if role else '',
            confirmedAt=now_iso(),
            note=str(note or ''),
        )
        self._records[record.peerId] = record
        self.save()
        return record

    def revoke(self, peer_id: str) -> bool:
        self._ensure()
        return self._records.pop(str(peer_id), None) is not None and self.save()

    def check(self, peer_id: str, fingerprint: str = '') -> tuple[bool, str]:
        """返回 ``(是否可信, 原因)``。指纹变化要求重新确认，防 peerId 被冒用。"""
        record = self.get(peer_id)
        if record is None:
            return False, '首次连接，需确认对端身份'
        if fingerprint and record.fingerprint and record.fingerprint != fingerprint:
            return False, '对端指纹与确认记录不一致，需重新确认'
        return True, 'ok'


# ----------------------------------------------------------------------
# 角色仲裁
# ----------------------------------------------------------------------
@dataclass
class RoleArbitration:
    selfRole: str
    masters: list = field(default_factory=list)
    slaves: list = field(default_factory=list)
    onlineMasters: list = field(default_factory=list)
    staleMasters: list = field(default_factory=list)
    master: str = ''
    suggestedMaster: str = ''
    conflict: bool = False
    reason: str = ''

    @property
    def selfRoleLabel(self) -> str:
        return role_label(self.selfRole)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['selfRoleLabel'] = self.selfRoleLabel
        return data


def arbitrate_roles(
    self_id: str,
    self_role: str,
    peers: dict[str, 'PeerHeartbeat'],
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    accepted_master_ids: Optional[list] = None,
    now: Optional[float] = None,
    skew_ms: float = 0.0,
) -> RoleArbitration:
    """把「本机声明的角色 + 所有对端声明的角色」仲裁成一主多从拓扑。

    规则刻意做得**无协商、可复算**：不引入任何选举或租约文件（共享目录上做
    互斥会引入不确定行为），而是靠一条确定性约定 —— **在线的主控端里 peerId
    字典序最小者为唯一合法主控**。所有端按同一份心跳算出同一个结果。

    **只有在线的主控端参与仲裁与冲突判定**（``staleMasters`` 单独列出，仅供
    提示）。这不是优化，是必须的：``peers/<id>.json`` 没有清理机制，一台曾经
    作主控的机器退役或关机后，它的心跳文件会永远留在共享目录里并继续声明
    自己为主控。若把它算进 ``masters``，剩下的机器会永久判成角色冲突，
    而冲突是 fail-closed 的 —— 从机会拒绝每一个请求，用户还被提示去改一台
    已经关机的机器，自救无门。同理，已确认名单里那些不在线的旧主控也不能
    把主控位占住（名单顺序是"最早确认的在前"），否则在跑的主控会被判成
    "不是当前主控端"。

    多个**在线**主控时不做自动降级：``conflict`` 置位，由调用方 fail-closed
    （暂停投递与执行），并提示用户改配置 —— 悄悄改变用户的角色比直接报错更危险。

    「在线」按**共享目录的时基**判定（``skew_ms``：服务器时钟 - 本机时钟）：
    跨机时各机的墙上时钟不可比，直接拿对端时间戳减本机时钟会把在线判成离线
    （或反过来）。
    """
    roster: dict[str, str] = {}
    for peer_id, beat in (peers or {}).items():
        if peer_id and peer_id != self_id:
            roster[str(peer_id)] = normalize_role(beat.role)
    roster[str(self_id)] = normalize_role(self_role)

    masters = sorted(pid for pid, role in roster.items() if role == ROLE_MASTER)
    slaves = sorted(pid for pid, role in roster.items() if role != ROLE_MASTER)
    online_masters = sorted(
        pid for pid in masters
        if pid == self_id or not peers[pid].is_stale(interval_s, now, skew_ms=skew_ms)
    )
    stale_masters = [pid for pid in masters if pid not in set(online_masters)]

    accepted = [str(pid) for pid in (accepted_master_ids or []) if pid]
    # 已确认但当前不在线、或已改声明为从机的旧主控不占主控位。
    accepted_online = [pid for pid in accepted if pid in online_masters]
    if accepted_online:
        master = accepted_online[0]
        reason = f'按已确认名单选择主控端 {master}'
    elif online_masters:
        master = online_masters[0]
        if accepted:
            reason = f'已确认的主控端不在线，改用在线主控端 {master}'
        else:
            reason = f'按 peerId 字典序选定主控端 {master}'
    else:
        master = ''
        reason = '未发现在线的主控端' if masters else '未发现任何主控端'

    conflict = len(online_masters) > 1
    if conflict:
        reason = (f'检测到 {len(online_masters)} 个在线主控端'
                  f'（{"、".join(online_masters)}），建议保留 {online_masters[0]}')

    return RoleArbitration(
        selfRole=normalize_role(self_role),
        masters=masters,
        slaves=slaves,
        onlineMasters=online_masters,
        staleMasters=stale_masters,
        master=master,
        suggestedMaster=online_masters[0] if online_masters else '',
        conflict=conflict,
        reason=reason,
    )


# ----------------------------------------------------------------------
# 连接状态（供 UI 直接渲染）
# ----------------------------------------------------------------------
@dataclass
class PeerView:
    peerId: str
    role: str = ROLE_SLAVE
    online: bool = False
    trusted: bool = False
    needsConfirmation: bool = False
    trustReason: str = ''
    deviceCount: int = 0
    devices: list = field(default_factory=list)
    heartbeatAt: str = ''
    ageMs: float = -1.0
    carrier: str = ''
    fingerprint: str = ''
    host: str = ''
    # 实例级身份与它在池里占的槽位（老版本对端为空/0）。
    nodeId: str = ''
    nodeSlot: int = 0
    # 读取期信息：显示名称与终端恢复依据，不改变心跳的持久格式。
    deploymentId: str = ''
    displayLabel: str = ''
    startedAt: str = ''

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['roleLabel'] = self.roleLabel
        data['devices'] = list(self.devices)
        return data


@dataclass
class RelayHealth:
    enabled: bool = False
    state: str = STATE_UNCONFIGURED
    role: str = ROLE_SLAVE
    peerId: str = ''
    carrierKind: str = ''
    carrierLabel: str = ''
    hint: str = ''
    master: str = ''
    masters: list = field(default_factory=list)
    slaves: list = field(default_factory=list)
    onlineMasters: list = field(default_factory=list)
    staleMasters: list = field(default_factory=list)
    onlineSlaves: list = field(default_factory=list)
    pendingConfirmation: list = field(default_factory=list)
    conflict: bool = False
    deviceCount: int = 0
    # 本机**自己**换过标识后留在共享目录里的旧心跳。它们不是对端，绝不能进
    # ``peers``（否则界面会把「自己」显示成一台对端），但要让用户知道被忽略了。
    selfGhosts: list = field(default_factory=list)
    # **别的机器**换过标识后留下的旧心跳（同指纹同主机、已被该机器在线的标识
    # 取代）。同样不是对端：不滤掉的话，对端列表里会凭空多出一台永远离线的
    # 「主控」，用户看到的就是「从机找到两个主机」。
    supersededPeers: dict = field(default_factory=dict)
    # 池内**发现了但还没配对**的实例。它们是有意不进 ``peers`` 的：配对是准入门槛，
    # 未配对者既不进对端列表也不参与角色仲裁。但完全藏起来又会让人困惑
    # （「明明对端在跑，怎么什么都看不到」），所以单独列出来供界面提示。
    unpairedPeers: list = field(default_factory=list)
    peers: list = field(default_factory=list)
    link: dict = field(default_factory=dict)

    @property
    def stateText(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'state': self.state,
            'stateText': self.stateText,
            'role': self.role,
            'roleLabel': self.roleLabel,
            'peerId': self.peerId,
            'carrierKind': self.carrierKind,
            'carrierLabel': self.carrierLabel,
            'hint': self.hint,
            'master': self.master,
            'masters': list(self.masters),
            'slaves': list(self.slaves),
            'onlineMasters': list(self.onlineMasters),
            'staleMasters': list(self.staleMasters),
            'onlineSlaves': list(self.onlineSlaves),
            'pendingConfirmation': list(self.pendingConfirmation),
            'conflict': self.conflict,
            'deviceCount': self.deviceCount,
            'selfGhosts': list(self.selfGhosts),
            'supersededPeers': dict(self.supersededPeers),
            'unpairedPeers': [dict(item) for item in self.unpairedPeers],
            'peers': [view.to_dict() for view in self.peers],
            'link': dict(self.link),
        }


def is_own_process_leftover(
    beat: 'PeerHeartbeat',
    *,
    self_fingerprint: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
) -> bool:
    """这条心跳是不是**本进程自己**在另一次身份下写的？

    满足「同指纹 + 同 pid + 启动时刻不同」——即同一个进程改了 ``peerId`` 之后的
    旧标识记录。因为确实是自己写的，符合「谁写谁清」，可以安全删除（见
    ``purge_self_heartbeats``）。

    时间戳是身份的一部分：同一进程里每次启动节点都会重取 ``startedAt``，所以
    「同 pid 不同 startedAt」精确对应「同一进程的另一次身份」。
    """
    if not self_fingerprint or beat.fingerprint != self_fingerprint:
        return False
    if not self_pid or int(beat.pid or 0) != int(self_pid):
        return False
    return bool(beat.startedAt) and beat.startedAt != self_started_at


def is_self_ghost(
    beat: 'PeerHeartbeat',
    *,
    self_fingerprint: str = '',
    self_peer_id: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
    beat_alive: bool = False,
) -> bool:
    """这条心跳是不是「本机自己」不再使用的旧身份？

    同机改了 ``peerId``（设置里的「本机标识」）之后，旧标识的心跳文件会留在共享
    目录里 —— 单写者原则下谁也不能删别人的文件，而它确实也不算对端。它有两个
    来源，必须都认，否则界面会把「本机自己」显示成一台离线对端（角色还是改之前
    那个）——用户看到的就是「对端标识不对」：

    * **同一进程**改标识（改完在进程内重启中转节点）：``pid`` 相同、``startedAt``
      不同。改完立即出现，也由 ``purge_self_heartbeats`` 当场清掉。
    * **上一个进程**留下的：改完标识后整个应用重启过（或这条残留是带缺陷的旧版本
      写下的），``pid`` 已经对不上了。此时判据只能落在时间上：**它的最后心跳早于
      本机这次启动**。

    ``beat_alive`` 是这条时间判据的**一票否决**，别省：同机多开时，**先起**的那个
    实例的最后心跳必然早于后起实例的启动时刻 —— 那正是「残留」的时间特征。只看
    时间会把活着的兄弟吞掉（表现：后起的实例在头几秒看不见先起的那个，直到它下
    一拍心跳才恢复）。真正的残留特征是「**不再更新**」，所以还在跳的一律不是幽灵。

    只按指纹判断是不够的：**同一台机器上跑两个实例**（同机双实例验证）指纹相同，
    那是真对端，不能被吞掉 —— 所以两种情况都必须先排除「这就是我现在用的标识」。
    """
    if self_peer_id and beat.peerId == self_peer_id:
        return False
    if is_own_process_leftover(
        beat, self_fingerprint=self_fingerprint, self_pid=self_pid,
        self_started_at=self_started_at,
    ):
        return True
    # 还在跳动 ⇒ 是活着的同机兄弟，不是本机放弃的旧身份。
    if beat_alive:
        return False
    # 上一个进程的残留：同指纹、不同 pid、且在本机启动前就停了。
    if not self_fingerprint or beat.fingerprint != self_fingerprint:
        return False
    if not self_started_at:
        return False
    return 0.0 < _epoch_ms(beat.heartbeatAt) < _epoch_ms(self_started_at)


def is_superseded_ghost(beat: 'PeerHeartbeat', sibling: 'PeerHeartbeat') -> bool:
    """``beat`` 是不是「同一台机器上已被 ``sibling`` 取代」的旧标识？

    调用方负责先确认两者**同指纹同主机**（同一台机器，时钟也相同，下面的时间
    比较才成立）且 ``sibling`` 在线、``beat`` 不在线。这里只回答最后一步：

    它的最后一次心跳**早于**兄弟标识的启动时刻 —— 也就是说，在这台机器当前的
    标识起来之前它就已经不再心跳了，属于被取代的旧标识。

    这条时间判据不能省。同一台机器上**同时**跑两个实例（同机双实例验证）指纹也
    相同，但那是真对端：刚被关掉的那个兄弟实例，它的最后心跳必然**晚于**对方启动
    （对方在跑的时候它还在心跳），所以不会被这条吞掉，仍会留在列表里灰显。
    """
    beat_ms = _epoch_ms(beat.heartbeatAt)
    sibling_start_ms = _epoch_ms(sibling.startedAt)
    if beat_ms <= 0 or sibling_start_ms <= 0:
        return False
    return beat_ms < sibling_start_ms


def evaluate_health(
    *,
    enabled: bool,
    role: str,
    self_id: str,
    peers: Optional[dict[str, 'PeerHeartbeat']] = None,
    link: Optional[dict] = None,
    trust: Optional[TrustStore] = None,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    accepted_master_ids: Optional[list] = None,
    carrier_kind: str = '',
    error: str = '',
    local_device_count: int = 0,
    self_fingerprint: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
    now: Optional[float] = None,
    server_skew_ms: Optional[float] = None,
    paired_node_ids: Optional[set] = None,
    pairing_enabled: bool = False,
) -> RelayHealth:
    """把心跳、链路状态与信任名单汇总成一份可直接显示的连接状态。

    ``hint`` 是给用户看的一句话结论，UI 不需要自己拼装角色与在线数。

    ``pairing_enabled`` + ``paired_node_ids`` 是**准入门槛**（模型 A，见
    ``docs/v6`` §9）：启用配对后，不在名单里的实例一律不进 ``peers``、不进角色
    仲裁，只出现在 ``unpairedPeers`` 里供界面提示。名单按**实例键 nodeId** 匹配，
    不按 peerId —— peerId 是可变的显示名，改一次名就等于换了个身份；而老版本对端
    压根不上报 ``nodeId``（读到空串），此时按 fail-closed 判为未配对，并在提示里
    点明「对端版本过旧」，而不是默认放行。
    """
    peers = dict(peers or {})
    link = dict(link or {})
    self_role = normalize_role(role)
    health = RelayHealth(
        enabled=bool(enabled),
        role=self_role,
        peerId=str(self_id or ''),
        carrierKind=carrier_kind,
        link=link,
    )

    link_state = str(link.get('state') or '')

    # 时效判定先把本机时钟归算到**共享目录服务器的时钟**上：两台机器的墙上时钟
    # 差几百秒是常态（现场实测两台 PC 差 393s、PC 与共享服务器差 669s）。不归算
    # 的话，快的一方永远看不到慢的一方在线，慢的一方反而把快的一方当成刚心跳过，
    # 于是「从机说已连接、主控说尚未发现在线的从机」这种自相矛盾的状态必然出现。
    if server_skew_ms is None:
        server_skew_ms = observer_clock_skew_ms(peers, self_id)
    skew = float(server_skew_ms or 0.0)

    views: list[PeerView] = []
    ghosts: list[str] = []
    # 未配对的实例：既不进 ``views`` 也不进仲裁，只作为提示材料（见下面 pairing_enabled）。
    unpaired_ids: set[str] = set()
    unpaired: list[dict] = []
    # 先只判定「谁在线」，建视图放到过滤之后：同机旧标识要先被认出来，否则它会带着
    # 旧角色进列表、进仲裁。
    entries: list[tuple[str, PeerHeartbeat, bool]] = []
    for peer_id in sorted(peers):
        if peer_id == self_id:
            continue
        beat = peers[peer_id]
        # 先算时效：它既是「在不在线」，也是幽灵判定的**一票否决**（还在跳的同机
        # 实例是兄弟，不是本机放弃的旧身份 —— 见 is_self_ghost 的 beat_alive）。
        alive = not beat.is_stale(interval_s, now, skew_ms=skew)
        if is_self_ghost(
            beat, self_fingerprint=self_fingerprint, self_peer_id=self_id,
            self_pid=self_pid, self_started_at=self_started_at, beat_alive=alive,
        ):
            ghosts.append(peer_id)
            continue
        entries.append((peer_id, beat, alive))

    # **别的机器**的旧标识：同一台机器（同指纹同主机）有在线的标识时，那些已经
    # 静默的旧标识就不是对端 —— 用户改过「本机标识」的话，旧文件按单写者原则谁也
    # 不会去删，留着它只会让对端列表里多出一台永远离线的「主控」。
    online_by_machine: dict[tuple[str, str], str] = {}
    for peer_id, beat, online in entries:
        if online and beat.fingerprint:
            online_by_machine.setdefault((beat.fingerprint, beat.host), peer_id)
    superseded: dict[str, str] = {}
    for peer_id, beat, online in entries:
        if online or not beat.fingerprint:
            continue
        sibling_id = online_by_machine.get((beat.fingerprint, beat.host))
        if sibling_id and is_superseded_ghost(beat, peers[sibling_id]):
            superseded[peer_id] = sibling_id

    for peer_id, beat, online in entries:
        if peer_id in superseded:
            continue
        # --- 准入门槛（模型 A）---
        # 排在幽灵过滤**之后**：幽灵压根不是对端（连「未配对」都算不上），两件事
        # 混在一起只会让提示互相抵消。这里只做「能不能看见」，真正的「能不能说话」
        # 由 ``RelayNode._authorize`` 与投递前预检负责 —— 读侧过滤挡不住已经写进
        # 分区目录的请求。
        if pairing_enabled:
            allowed = paired_node_ids or set()
            if not beat.nodeId or beat.nodeId not in allowed:
                unpaired_ids.add(peer_id)
                unpaired.append({
                    'peerId': peer_id,
                    'nodeId': beat.nodeId,
                    'role': normalize_role(beat.role),
                    'roleLabel': role_label(beat.role),
                    'online': online,
                    'ageMs': beat.age_ms(now, skew_ms=skew),
                    'host': beat.host,
                    'deviceCount': len(beat.devices),
                    # 老版本对端不上报实例键：不是「另一个实例」，而是「版本过旧」。
                    # 分开标出来，用户才知道该升级哪一端，而不是反复点「配对」。
                    'legacy': not beat.nodeId,
                })
                continue
        trusted, trust_reason = (True, 'ok') if trust is None else trust.check(peer_id, beat.fingerprint)
        views.append(PeerView(
            peerId=peer_id,
            role=normalize_role(beat.role),
            online=online,
            trusted=trusted,
            needsConfirmation=online and not trusted,
            trustReason=trust_reason,
            deviceCount=len(beat.devices),
            devices=list(beat.devices),
            heartbeatAt=beat.heartbeatAt,
            ageMs=beat.age_ms(now, skew_ms=skew),
            carrier=beat.carrier,
            fingerprint=beat.fingerprint,
            host=beat.host,
            nodeId=beat.nodeId,
            nodeSlot=int(beat.nodeSlot or 0),
            startedAt=beat.startedAt,
        ))
    health.peers = views
    health.selfGhosts = sorted(ghosts)
    health.supersededPeers = dict(superseded)
    health.unpairedPeers = unpaired

    # 仲裁也不能看见自己的旧身份：那条心跳的角色是改之前那个，留在名单里会让从机
    # 以为自己有个「待确认的主控端」（或者反过来判成角色冲突、直接停摆透传）。
    # 别的机器的旧标识同理：它的旧角色（很可能也是主控）会凭空多出一个主控端，
    # 把在跑的两端直接判成角色冲突 —— 而冲突是 fail-closed 的。
    # 未配对的实例也不能进：它连对端都还不是，让它参与仲裁等于让未授权的实例投票。
    live_peers = {pid: beat for pid, beat in peers.items()
                  if pid not in ghosts and pid not in superseded and pid not in unpaired_ids}
    arbitration = arbitrate_roles(
        self_id, self_role, live_peers,
        interval_s=interval_s, accepted_master_ids=accepted_master_ids, now=now,
        skew_ms=skew,
    )
    health.masters = arbitration.masters
    health.slaves = arbitration.slaves
    health.onlineMasters = arbitration.onlineMasters
    health.onlineSlaves = sorted(v.peerId for v in views if v.role == ROLE_SLAVE and v.online)
    health.master = arbitration.master
    health.staleMasters = list(arbitration.staleMasters)
    health.conflict = arbitration.conflict
    health.pendingConfirmation = sorted(v.peerId for v in views if v.needsConfirmation)
    health.deviceCount = sum(v.deviceCount for v in views if v.online and v.role == ROLE_SLAVE)

    # 退役机器留在共享目录里的主控心跳：不影响结论，但要让用户知道它被忽略了，
    # 否则「对端列表里有个永远离线的已确认主控」会一直是个谜。
    stale_note = ''
    if arbitration.staleMasters:
        stale_note = (f'；另有 {len(arbitration.staleMasters)} 个已离线的主控端记录'
                      f'（{"、".join(arbitration.staleMasters)}），不参与仲裁')

    # 别的机器的旧标识：也要点名。不点的话，「我明明把标识改过了，怎么对端列表里
    # 还/不还多一台」都会变成谜 —— 它不是消失了，是被认成同一台机器的旧身份忽略了。
    superseded_note = ''
    if superseded:
        pairs = '、'.join(f'{pid}（已被 {superseded[pid]} 取代）' for pid in sorted(superseded))
        superseded_note = (f'；另有 {len(superseded)} 条同一台机器的旧标识记录：'
                           f'{pairs}，已忽略')

    # 本机自己的旧标识：不当作对端，但要在提示里点名，否则用户会以为有一台连不上
    # 的机器（典型来源就是在设置里改了「本机标识」）。
    ghost_note = ''
    if health.selfGhosts:
        ghost_note = (f'；另有 {len(health.selfGhosts)} 条本机旧标识的残留心跳'
                      f'（{"、".join(health.selfGhosts)}），已按「本机自己」忽略')

    # 未配对实例：必须点名。不点的话「对端明明在跑，为什么列表里没有」会是个谜 ——
    # 它不是没被发现，是被准入门槛挡在外面了（这是设计意图，不是故障）。
    unpaired_note = ''
    if unpaired:
        labels = '、'.join(
            f'{item["peerId"]}（{item["roleLabel"]}{"，离线" if not item["online"] else ""}）'
            for item in sorted(unpaired, key=lambda x: x['peerId'])
        )
        legacy = [item for item in unpaired if item['legacy']]
        unpaired_note = (f'；另有 {len(unpaired)} 个池内未配对实例：{labels}，'
                         f'未配对前不参与通讯与角色仲裁')
        if legacy:
            unpaired_note += (f'（其中 {len(legacy)} 个对端版本过旧，未上报实例键，'
                              f'需把该端也升级到本版本）')

    def _done() -> RelayHealth:
        for note in (stale_note, superseded_note, ghost_note, unpaired_note):
            if note and note not in health.hint:
                health.hint += note
        return health

    if not enabled:
        health.state = STATE_UNCONFIGURED
        health.hint = '跨机中转未启用：在设置页填写角色、本机标识与载体后启用'
        return _done()

    if link_state in (STATE_DISCONNECTED, STATE_UNCONFIGURED) or error:
        health.state = STATE_DISCONNECTED
        health.hint = f'载体不可用：{error or link.get("lastError") or link.get("detail") or "未知原因"}'
        return _done()

    if arbitration.conflict:
        health.state = STATE_ROLE_CONFLICT
        health.hint = f'{arbitration.reason}；已暂停透传，请把其余主控改为从机'
        return _done()

    if self_role == ROLE_MASTER:
        online_slaves = [v for v in views if v.role == ROLE_SLAVE and v.online]
        if online_slaves:
            health.state = STATE_CONNECTED
            health.hint = (
                f'本机为主控端，已连接 {len(online_slaves)} 个从机，'
                f'共 {health.deviceCount} 台远端设备'
            )
            if health.pendingConfirmation:
                health.state = STATE_PENDING_CONFIRM
                health.hint = (
                    f'有 {len(health.pendingConfirmation)} 个从机待确认：'
                    f'{"、".join(health.pendingConfirmation)}'
                )
        else:
            health.state = STATE_WAITING_PEER
            # 「等待对端」和「对端在跑但没配对」是两件完全不同的事，混成一句话会让
            # 用户去查网络/心跳。这里按在线未配对实例的有无分开表述。
            online_unpaired = [item for item in unpaired if item['online']]
            health.hint = (
                f'本机为主控端，发现 {len(online_unpaired)} 个未配对的实例，配对后才能通讯'
                if online_unpaired else '本机为主控端，尚未发现在线的从机'
            )
        return _done()

    # 从机
    if not arbitration.onlineMasters:
        health.state = STATE_WAITING_PEER
        online_unpaired = [item for item in unpaired if item['online']]
        health.hint = (
            f'本机为从机，发现 {len(online_unpaired)} 个未配对的主控端，配对后才接受其请求'
            if online_unpaired else '本机为从机，未发现在线的主控端'
        )
        return _done()
    accepted = [str(pid) for pid in (accepted_master_ids or []) if pid]
    if not accepted:
        health.state = STATE_PENDING_CONFIRM
        health.hint = f'本机为从机，主控端 {arbitration.master} 待确认后才接受其请求'
        return _done()
    health.state = STATE_CONNECTED
    local = max(0, int(local_device_count or 0))
    health.hint = (
        f'本机为从机，由主控端 {arbitration.master} 控制，{local} 台设备已对外共享'
        if local else f'本机为从机，由主控端 {arbitration.master} 控制'
    )
    return _done()


# ----------------------------------------------------------------------
# 命令策略
# ----------------------------------------------------------------------
# 保守的默认白名单：只放行**真实存在**的查询类与日志类子命令。需要 install、
# push 等变更类命令时，在 relay.json 里显式扩展，而不是放宽这里的默认值。
#
# 注意 ``getprop`` 不在这里：它不是 adb 的子命令（实测 adb 会回
# ``unknown command getprop``），设备属性要走 ``shell getprop <key>``。
# 把不存在的子命令留在白名单里，等于给调用方一个假的「已放行」信号。
DEFAULT_ALLOWED_SUBCOMMANDS: tuple[str, ...] = (
    'devices',
    'version',
    'get-state',
    'get-serialno',
    'logcat',
    'shell',
)

# 设备由设备端依据请求里的 device 字段自行拼装，不允许操作端指定目标设备。
# 只有**子命令位置**的选择符才算越权：设备端拼命令时形如
# ``adb <selector> <argv...>``，能被 adb 当全局选项解析的只有紧跟在 adb 后面的
# 那个 token，也就是 argv[0]。排在子命令之后的 ``-t`` / ``-s`` 是子命令自己的
# 合法参数（``logcat -t 20`` 取最近 20 行、``logcat -s TAG`` 静默过滤器），
# 一律拒绝会把正常用法一起挡掉。
_DEVICE_SELECTOR_TOKENS = frozenset({'-s', '--serial', '-t', '--transport-id'})


def check_command(
    argv,
    *,
    allow_shell: bool = False,
    allowed_subcommands: Optional[tuple[str, ...]] = None,
) -> tuple[bool, str]:
    """校验一条待透传的 adb 命令，返回 ``(是否放行, 原因)``。

    这个函数应在**设备端**调用：安全边界必须落在被控端，操作端的配置再宽松
    也不能覆盖设备端的拒绝。无论是否允许任意 shell，以下一律拒绝：

    - 空命令；
    - 子命令位置上出现设备选择符（``-s`` / ``--serial`` / ``-t`` / ``--transport-id``）；
    - 非白名单子命令；
    - 未开启开关时的 ``shell``。
    """
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)) or not argv:
        return False, '命令为空'
    tokens = [str(part) for part in argv]
    subcommand = tokens[0]
    if not subcommand:
        return False, '命令为空'

    # 先判选择符：命中时给的原因比「不在白名单内」更能说明问题。
    if subcommand in _DEVICE_SELECTOR_TOKENS:
        return False, (f'请求不得在子命令位置指定设备选择符（{subcommand}）：'
                       f'设备由设备端依据 device 字段决定')

    allowed = DEFAULT_ALLOWED_SUBCOMMANDS if allowed_subcommands is None else tuple(allowed_subcommands)
    if subcommand not in allowed:
        return False, f'子命令不在白名单内：{subcommand}'

    if subcommand == 'shell' and not allow_shell:
        return False, '设备端未开启「允许任意 shell」'

    return True, 'ok'


# ----------------------------------------------------------------------
# 池（pool）：实例键、注册表与槽位
# ----------------------------------------------------------------------
# 为什么需要池：一个共享目录只服务一套中转时，``peers/`` 这条**刻意不分区**的
# 公共总线不会有歧义。但同一个共享目录被多套部署共用时，总线就串了 —— 对端列表
# 互相污染、角色仲裁看见别人家的主控、跨部署同名 peerId 在分区目录里撞同一格。
# 池用「目录一层」把不同套隔开（``<base>/pool/<poolId>/``），再配一条准入门槛
# 把池内不该看见的实例挡掉。层次与档位的完整论证见
# ``docs/需求与设计/v6-配对池与双端派生路径设计.md`` §9。


def new_node_id(host_label: str = '') -> str:
    """生成一个**实例键**（同机多开时每个实例一个）。

    生成后必须持久化在本机。它一旦变化，配对名单与池内槽位都会跟着漂：对端看到
    的是一个陌生实例，而旧记录永远留在池里。这跟「改一次 peerId 多一条残留」是
    同一类问题，但更重（配对关系全断），所以它只生成一次、之后永不变。
    """
    slug = re.sub(r'[^A-Za-z0-9._-]', '-', str(host_label or '')).strip('-.')
    return f'{(slug or "node")[:16]}-{uuid.uuid4().hex[:8]}'


def resolve_pool_root(base: str, pool_id: str = '') -> str:
    """池的数据根。

    ``pool_id`` 为空时**就是** ``base`` —— 也就是今天的行为。这是刻意的兼容策略：
    老部署零迁移，不填池标识的目录结构与老版本逐字节一致。
    """
    base = str(base or '')
    pool_id = str(pool_id or '').strip()
    if not pool_id:
        return base
    return os.path.join(base, POOL_DIR, check_peer_id(pool_id, '池标识'))


def deployment_roots(base: str) -> dict[str, str]:
    """只读发现指定共享目录的部署；不创建目录、不进入链接或非法名称。"""
    if not str(base or '').strip():
        return {}
    roots = {'': str(base)} if base else {}
    directory = os.path.join(str(base), POOL_DIR)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if valid_peer_id(entry.name) and entry.is_dir(follow_symlinks=False):
                    roots[entry.name] = resolve_pool_root(base, entry.name)
    except OSError:
        pass
    return dict(sorted(roots.items()))


def registry_dir(root: str) -> str:
    return os.path.join(str(root or ''), SLOT_REGISTRY)


def registry_path(root: str, node_id: str) -> str:
    return os.path.join(registry_dir(root), f'{check_peer_id(node_id, "实例键")}.json')


def _shared_age_ms(file_mtime_ms: float, stamp_iso: str, now=None, skew_ms: float = 0.0) -> float:
    """共享目录上的时效判定（心跳与池注册项共用同一套算法）。

    优先用**文件 mtime**：它由共享目录服务器打戳，是所有端共同的时基。把本机时钟
    按 ``skew_ms``（服务器时钟 − 本机时钟）归算过去再相减即可。拿不到 mtime 时退回
    「对端时间戳 vs 本机时钟」的老算法 —— 只有同机（同进程或同机双实例）才会走到
    这里，同机没有时钟偏差，老算法是对的。

    返回 ``-1.0`` 表示既没有 mtime 也没有可解析的时间戳，调用方应判失联。
    """
    current = now if now is not None else time.time()
    if file_mtime_ms > 0:
        return max(0.0, current * 1000.0 + float(skew_ms) - float(file_mtime_ms))
    stamp = _epoch_ms(stamp_iso)
    if stamp <= 0:
        return -1.0
    return max(0.0, current * 1000.0 - stamp)


@dataclass
class RegistryEntry:
    """池内一条实例注册记录：谁在这个池子里、占哪个槽位、还在不在。

    与心跳的区别：心跳是**总线上的存在声明**（每个实例一份，用于发现对端）；
    注册项是**池成员名册**（用于分配槽位与展示池内全貌，配对之前就能看到）。
    两者都走「谁写谁清」的单写者原则，都存在共享目录里。
    """

    nodeId: str
    peerId: str = ''
    role: str = ROLE_SLAVE
    host: str = ''
    fingerprint: str = ''
    # 池内槽位（≥1）。同机多开时靠它区分两个实例；0 表示未分配。
    slot: int = 0
    pid: int = 0
    startedAt: str = ''
    lastSeen: str = ''
    appVersion: str = ''
    poolId: str = ''
    # 读取时由 ``read_registry`` 填上的共享目录 mtime，属于服务器时钟；不落盘。
    fileMtimeMs: float = 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop('fileMtimeMs', None)
        return {'schema': RELAY_SCHEMA, **data}

    @classmethod
    def from_dict(cls, data: dict) -> 'RegistryEntry':
        return cls(
            nodeId=str(data.get('nodeId', '')),
            peerId=str(data.get('peerId', '')),
            role=normalize_role(data.get('role') or ROLE_SLAVE),
            host=str(data.get('host', '')),
            fingerprint=str(data.get('fingerprint', '')),
            slot=int(data.get('slot') or 0),
            pid=int(data.get('pid') or 0),
            startedAt=str(data.get('startedAt', '')),
            lastSeen=str(data.get('lastSeen', '')),
            appVersion=str(data.get('appVersion', '')),
            poolId=str(data.get('poolId', '')),
        )

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    @property
    def server_skew_ms(self) -> float:
        """写者本机时钟相对共享目录服务器时钟的偏差（服务器 − 本机，毫秒）。"""
        if self.fileMtimeMs <= 0:
            return 0.0
        stamp = _epoch_ms(self.lastSeen)
        if stamp <= 0:
            return 0.0
        return float(self.fileMtimeMs) - stamp

    def age_ms(self, now: Optional[float] = None, *, skew_ms: float = 0.0) -> float:
        return _shared_age_ms(self.fileMtimeMs, self.lastSeen, now, skew_ms)

    def is_stale(
        self,
        now: Optional[float] = None,
        *,
        skew_ms: float = 0.0,
        lease_s: float = REGISTRY_LEASE_S,
    ) -> bool:
        """租约到期即视为该实例已退场，它占的槽位可以被复用。

        没有这层回收，池子会**结构性只增不减**（每个用过的实例留一条），槽位用满
        之后自动分配就失效了 —— 这是 v4 里「多余配置」的同一类问题，只是更硬。
        """
        age = self.age_ms(now, skew_ms=skew_ms)
        if age < 0:
            return True
        return age > max(1.0, float(lease_s)) * 1000.0 + HEARTBEAT_GRACE_MS


def read_registry(root: str) -> dict[str, RegistryEntry]:
    """读池内所有实例注册项。读不出 JSON 的记录一律跳过（与 ``read_peers`` 同策）。"""
    out: dict[str, RegistryEntry] = {}
    directory = registry_dir(root)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not name.endswith('.json') or is_provisional(name):
            continue
        path = os.path.join(directory, name)
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        entry = RegistryEntry.from_dict({**data, 'nodeId': data.get('nodeId') or name[:-5]})
        if not entry.nodeId:
            continue
        try:
            entry.fileMtimeMs = relay_file_io.stat(path).st_mtime * 1000.0
        except OSError:
            entry.fileMtimeMs = 0.0
        out[entry.nodeId] = entry
    return out


def write_registry_entry(root: str, entry: RegistryEntry) -> str:
    """写本机自己的注册项（单写者：只写以自己实例键命名的那个文件）。"""
    entry.nodeId = check_peer_id(entry.nodeId, '实例键')
    os.makedirs(registry_dir(root), exist_ok=True)
    path = registry_path(root, entry.nodeId)
    atomic_write_json(path, entry.to_dict())
    return path


def allocate_slot(
    entries: dict[str, RegistryEntry],
    *,
    self_node_id: str = '',
    now: Optional[float] = None,
    skew_ms: float = 0.0,
    lease_s: float = REGISTRY_LEASE_S,
) -> int:
    """给本实例分配池内槽位：取最小的、没有被**在册实例**占用的正整数。

    「在册」= 租约未过期。已退场实例占的槽位会被复用，否则池子会越用越满。

    **本机已经在册时保持原槽位**，不参与重新分配：否则一个低号槽位空出来，本实例
    的槽位就会在运行期往上跳，对别的实例来说那是「一个熟悉的成员忽然换了槽位」，
    排障时和自己的重启、改名混在一起分不清。
    """
    self_entry = entries.get(str(self_node_id or ''))
    if self_entry is not None and int(self_entry.slot or 0) > 0:
        if not self_entry.is_stale(now, skew_ms=skew_ms, lease_s=lease_s):
            return int(self_entry.slot)
    used: set[int] = set()
    for node_id, entry in entries.items():
        if node_id == str(self_node_id or ''):
            continue
        if entry.is_stale(now, skew_ms=skew_ms, lease_s=lease_s):
            continue
        used.add(int(entry.slot or 0))
    slot = 1
    while slot in used:
        slot += 1
    return slot


def observer_registry_skew_ms(entries: dict[str, RegistryEntry], self_node_id: str) -> float:
    """从**本机自己**的注册项量出「服务器时钟 − 本机时钟」。

    与心跳那套（``observer_clock_skew_ms``）同理：量偏差不需要额外探针，自己写的
    文件就在同一个目录里，mtime 与 ``lastSeen`` 一对就出来了。
    """
    entry = entries.get(str(self_node_id or ''))
    return entry.server_skew_ms if entry is not None else 0.0


def pool_snapshot(
    root: str,
    *,
    pool_id: str = '',
    self_node_id: str = '',
    now: Optional[float] = None,
    skew_ms: Optional[float] = None,
    lease_s: float = REGISTRY_LEASE_S,
    entries: Optional[dict] = None,
) -> dict:
    """池内全貌：谁在册、占哪个槽位、有没有撞车 —— 供界面展示与排障。

    这里顺带把两类**撞车**显性化，它们在过去只能靠肉眼对比心跳文件才发现：

    * **槽位撞车**：两个在册实例占了同一个槽位；
    * **标识撞车**：两个在册实例用了同一个 ``peerId`` 却是不同实例（不同实例键）。
      这是既有的已知债（全中文主机名会一起退化成 ``pc``），池注册表让它第一次
      变得可见 —— 池能挡住看见与说话，但挡不住传输层按 peerId 命名文件这件事。
    """
    entries = read_registry(root) if entries is None else entries
    if skew_ms is None:
        skew_ms = observer_registry_skew_ms(entries, self_node_id)
    items: list[dict] = []
    by_slot: dict[int, list[str]] = {}
    by_peer: dict[str, list[str]] = {}
    for node_id in sorted(entries):
        entry = entries[node_id]
        live = not entry.is_stale(now, skew_ms=skew_ms, lease_s=lease_s)
        if live:
            if int(entry.slot or 0) > 0:
                by_slot.setdefault(int(entry.slot), []).append(node_id)
            if entry.peerId:
                by_peer.setdefault(entry.peerId, []).append(node_id)
        items.append({
            'nodeId': node_id,
            'peerId': entry.peerId,
            'role': entry.role,
            'roleLabel': entry.roleLabel,
            'host': entry.host,
            'slot': int(entry.slot or 0),
            'pid': int(entry.pid or 0),
            'appVersion': entry.appVersion,
            'online': live,
            'isSelf': node_id == str(self_node_id or ''),
            'ageMs': entry.age_ms(now, skew_ms=skew_ms),
            'lastSeen': entry.lastSeen,
        })
    return {
        'root': str(root or ''),
        'poolId': str(pool_id or ''),
        'count': len(items),
        'onlineCount': sum(1 for item in items if item['online']),
        'entries': items,
        'staleEntries': sorted(item['nodeId'] for item in items if not item['online']),
        'slotConflicts': [
            {'slot': slot, 'nodeIds': sorted(ids)} for slot, ids in sorted(by_slot.items()) if len(ids) > 1
        ],
        'peerIdConflicts': [
            {'peerId': peer, 'nodeIds': sorted(ids)} for peer, ids in sorted(by_peer.items()) if len(ids) > 1
        ],
        'serverSkewMs': float(skew_ms or 0.0),
    }


# ----------------------------------------------------------------------
# 配对（pairing）：本机对「我要跟谁通讯」的决定
# ----------------------------------------------------------------------
# 为什么不放共享目录：这是**本机私有**的决定（「我允许谁跟我说话」），而且共享
# 目录里的东西不可依赖清理（见 v4 §0）。放本机即可，且天然满足「配对本机生效」。
#
# 配对解决的是第一档隔离「能不能说话」，顺带把第二档「能不能看见」也做了（未配对
# 的实例不进对端列表、不进角色仲裁）。它**不是安全边界**：真正的边界是共享目录
# 自身的 ACL 与从机侧的命令白名单。它的收益是**防误配、防串台** —— 说得再直白些，
# 防的是「两个不相干的应用共用一个共享目录时互相看见、互相干扰」，不是防恶意方。
@dataclass
class PairRecord:
    """一条配对记录，以**实例键**（``nodeId``）为主键。

    主键为什么不是 ``peerId``：peerId 是可变的显示名（用户可以改，同机多开会自动
    退让成 ``-2``）。拿可变标识当隔离键，改一次名就等于换了个空间、看不见老对端，
    而且旧记录永远清不掉。
    """

    nodeId: str
    peerId: str = ''
    fingerprint: str = ''
    state: str = PAIR_DISCOVERED
    host: str = ''
    requestedAt: str = ''
    confirmedAt: str = ''
    revokedAt: str = ''
    note: str = ''
    # 主控每次主动确认生成新代次；从机重试不能恢复已撤销的旧连接。
    connectionId: str = ''
    retiredConnectionIds: list[str] = field(default_factory=list)

    @property
    def is_paired(self) -> bool:
        return self.state == PAIR_CONFIRMED

    @property
    def stateLabel(self) -> str:
        return PAIR_STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict:
        """落盘形态：只有 dataclass 字段（``asdict`` 不含 property）。"""
        return asdict(self)

    def view(self) -> dict:
        """界面形态：在落盘字段之上补出派生字段（``stateLabel``/``paired``）。

        与 ``to_dict`` 分开，是因为两个用途不能共用一份形状：落盘要多一个字段都
        算改长期格式，而界面读的是人话标签。同一份「配对记录」若对外有两种形状，
        调用方迟早会在某一处读到 KeyError。
        """
        return {**asdict(self), 'stateLabel': self.stateLabel, 'paired': self.is_paired}

    @classmethod
    def from_dict(cls, data: dict) -> 'PairRecord':
        return cls(
            nodeId=str(data.get('nodeId', '')),
            peerId=str(data.get('peerId', '')),
            fingerprint=str(data.get('fingerprint', '')),
            state=str(data.get('state') or PAIR_DISCOVERED),
            host=str(data.get('host', '')),
            requestedAt=str(data.get('requestedAt', '')),
            confirmedAt=str(data.get('confirmedAt', '')),
            revokedAt=str(data.get('revokedAt', '')),
            note=str(data.get('note', '')),
            connectionId=str(data.get('connectionId', '')),
            retiredConnectionIds=[str(value) for value in data.get('retiredConnectionIds', [])
                                  if isinstance(value, str) and value],
        )


class PairingStore:
    """本机维护的配对名单（本地文件，不落共享目录）。

    状态机：``discovered → requested → confirmed → revoked``。
    ``confirmed`` 才放行；``revoke`` 是单方面立即生效的（但只能撤自己这一侧）。
    """

    def __init__(self, path: str, *, storage=None):
        self.path = os.path.abspath(path)
        self.storage = storage
        self._records: dict[str, PairRecord] = {}
        self._loaded = False

    def load(self) -> dict[str, PairRecord]:
        data = self.storage.read() if self.storage is not None else (read_json(self.path) or {})
        records = data.get('pairs') if isinstance(data.get('pairs'), dict) else {}
        self._records = {
            str(key): PairRecord.from_dict({**value, 'nodeId': value.get('nodeId') or key})
            for key, value in records.items() if isinstance(value, dict)
        }
        self._loaded = True
        return dict(self._records)

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> bool:
        if self.storage is not None:
            try:
                self.storage.write({
                    'schema': RELAY_SCHEMA,
                    'pairs': {key: record.to_dict() for key, record in self._records.items()},
                })
            except OSError:
                # 写失败不能让内存中的确认绕过持久化或继续覆盖较新的撤销记录。
                self._records = {}
                self._loaded = False
                raise
            return True
        try:
            atomic_write_json(self.path, {
                'schema': RELAY_SCHEMA,
                'pairs': {key: record.to_dict() for key, record in self._records.items()},
            })
        except OSError:
            return False
        return True

    def items(self) -> list[PairRecord]:
        self._ensure()
        return list(self._records.values())

    def get(self, node_id: str) -> Optional[PairRecord]:
        self._ensure()
        return self._records.get(str(node_id or ''))

    def find_by_peer_id(self, peer_id: str) -> Optional[PairRecord]:
        """按显示名反查（仅用于界面提示；准入与投递一律按实例键判）。"""
        self._ensure()
        for record in self._records.values():
            if record.peerId and record.peerId == str(peer_id or ''):
                return record
        return None

    def is_paired(self, node_id: str) -> bool:
        record = self.get(node_id)
        return bool(record and record.is_paired)

    def paired_node_ids(self) -> set:
        self._ensure()
        return {node_id for node_id, record in self._records.items() if record.is_paired}

    def request(self, node_id: str, *, peer_id: str = '', fingerprint: str = '', host: str = '') -> PairRecord:
        """记一条「已发现/已发起」。

        调用它就等于有人**主动发起了**，所以新记录直接落在 ``requested``。
        ``discovered`` 留给「只在心跳里见过、本机还没有任何记录」的状态 ——
        那种状态由 ``RelayNode.pairing_records`` 现算，不落盘（不存在的东西没法
        写日志，也就不该在名单里留一条谁都看不懂的空记录）。
        """
        self._ensure()
        node_id = str(node_id or '')
        record = self._records.get(node_id)
        if record is None:
            record = PairRecord(nodeId=node_id, state=PAIR_REQUESTED, requestedAt=now_iso())
            self._records[node_id] = record
        elif record.state in (PAIR_DISCOVERED, PAIR_REVOKED):
            record.requestedAt = now_iso()
            record.state = PAIR_REQUESTED
        if peer_id:
            record.peerId = str(peer_id)
        if fingerprint:
            record.fingerprint = str(fingerprint)
        if host:
            record.host = str(host)
        self.save()
        return record

    def confirm(self, node_id: str, *, peer_id: str = '', fingerprint: str = '', host: str = '',
                connection_id: Optional[str] = None) -> PairRecord:
        """把对端加入名单。重复确认视为更新（指纹/显示名变化后重新确认）。"""
        self._ensure()
        node_id = str(node_id or '')
        record = self._records.get(node_id) or PairRecord(nodeId=node_id)
        if peer_id:
            record.peerId = str(peer_id)
        if fingerprint:
            record.fingerprint = str(fingerprint)
        if host:
            record.host = str(host)
        record.state = PAIR_CONFIRMED
        record.confirmedAt = now_iso()
        record.revokedAt = ''
        next_connection = str(connection_id) if connection_id is not None else uuid.uuid4().hex
        if record.connectionId and record.connectionId != next_connection:
            record.retiredConnectionIds.append(record.connectionId)
        record.connectionId = next_connection
        self._records[node_id] = record
        self.save()
        return record

    def revoke(self, node_id: str) -> bool:
        """单方面解除。记录保留（改成 ``revoked``），下次再配对不必从头发现。"""
        self._ensure()
        record = self._records.get(str(node_id or ''))
        if record is None:
            return False
        record.state = PAIR_REVOKED
        record.revokedAt = now_iso()
        return self.save()

    def forget(self, node_id: str) -> bool:
        self._ensure()
        return self._records.pop(str(node_id or ''), None) is not None and self.save()
