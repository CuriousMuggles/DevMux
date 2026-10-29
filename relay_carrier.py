"""跨机中转载体抽象层 —— 把「可靠消息」与「增量字节流」映射到具体链路。

分层模型（完整说明见 docs/需求与设计/v2-跨机中转透传-需求与设计.md §10）：

    第 0 层  链路 Link      具体连接：SMB 目录 / TCP 套接字 / 串口
    第 1 层  载体 Carrier   ← 本模块：命名消息槽 + 命名增量流
    第 2 层  协议 Protocol  relay.py：Request/Result/心跳/TTL/命令白名单
    第 3 层  会话 Session   设备端执行器（阶段 2）
    第 4 层  UI / WS        前端（阶段 3 / 4）

上层只依赖 :class:`RelayCarrier` 接口，不感知底层是什么链路。因此新增以太网
直连或串口直连时，协议、会话执行器与前端都不需要改动 —— 这是「连接方式可
扩展」这条需求的落点。

本模块同样保持**纯同步、无全局状态**：所有路径与配置显式传入，不起线程、
不碰 adb、不接触 WebSocket，因此可以在本地临时目录里做完整验证。
"""
from __future__ import annotations

import abc
import os
import relay_file_io
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

import relay
from relay import (
    STREAM_READ_CHUNK,
    atomic_write_bytes,
    now_iso,
    read_json,
    RelayQueue,
    StreamTail,
    StreamWriter,
)

# ----------------------------------------------------------------------
# 载体类型
# ----------------------------------------------------------------------
CARRIER_SHARED_FOLDER = 'shared-folder'
CARRIER_TCP = 'tcp'
CARRIER_SERIAL = 'serial'

# 拓扑：总线（多方共享，天然多对多）或点对点（一对一，多从机需要多条链路）
TOPO_BUS = 'bus'
TOPO_P2P = 'p2p'


class CarrierError(Exception):
    """载体使用错误（配置非法、槽名非法等）。"""


class CarrierUnsupported(CarrierError):
    """请求了一个尚未实现的载体类型。"""


class CarrierUnavailable(CarrierError):
    """载体当前不可用（共享目录失联、串口被占用等）。"""


@dataclass(frozen=True)
class CarrierSpec:
    """载体能力描述。

    设置页据此渲染表单、决定是否可选；协议层据此决定降级策略
    （例如不可持久投递的载体必须把请求 TTL 调小）。
    """

    kind: str
    label: str
    topology: str
    implemented: bool
    durable_messages: bool     # 消息落盘：断线期间的请求可在恢复后重投
    push_stream: bool          # 支持推送：低延迟，无需轮询
    throughput: str            # high | medium | low
    max_message_bytes: int
    option_fields: tuple[str, ...]
    notes: str = ''


CARRIER_SPECS: dict[str, CarrierSpec] = {
    CARRIER_SHARED_FOLDER: CarrierSpec(
        kind=CARRIER_SHARED_FOLDER,
        label='共享文件夹',
        topology=TOPO_BUS,
        implemented=True,
        durable_messages=True,
        push_stream=False,
        throughput='medium',
        max_message_bytes=1 * 1024 * 1024,
        option_fields=('root',),
        notes='SMB/UNC 共享目录，Windows 原生支持，天然支持一主多从；需同内网或 VPN',
    ),
    CARRIER_TCP: CarrierSpec(
        kind=CARRIER_TCP,
        label='以太网直连',
        topology=TOPO_P2P,
        implemented=False,
        durable_messages=False,
        push_stream=True,
        throughput='high',
        max_message_bytes=4 * 1024 * 1024,
        option_fields=('listen', 'host', 'port', 'sharedKey'),
        notes='计划中：点对点，主控监听、从机接入；多从机即多条链路',
    ),
    CARRIER_SERIAL: CarrierSpec(
        kind=CARRIER_SERIAL,
        label='串口直连',
        topology=TOPO_P2P,
        implemented=False,
        durable_messages=False,
        push_stream=True,
        throughput='low',
        max_message_bytes=4 * 1024,
        option_fields=('port', 'baud', 'sharedKey'),
        notes='计划中：需帧定界与校验；带宽低，仅适合命令与终端，不适合大量日志',
    ),
}


def carrier_specs() -> dict[str, CarrierSpec]:
    return dict(CARRIER_SPECS)


def get_carrier_spec(kind: str) -> Optional[CarrierSpec]:
    return CARRIER_SPECS.get(str(kind or ''))


# ----------------------------------------------------------------------
# 链路状态
# ----------------------------------------------------------------------
def make_link_status(
    state: str = relay.STATE_UNCONFIGURED,
    *,
    kind: str = '',
    detail: str = '',
    last_error: str = '',
    latency_ms: float = 0.0,
    since: str = '',
) -> dict:
    """统一的链路状态结构，前端直接渲染 ``stateText``。"""
    return {
        'state': state,
        'stateText': relay.STATE_LABELS.get(state, state),
        'kind': kind,
        'kindLabel': (get_carrier_spec(kind).label if get_carrier_spec(kind) else kind),
        'detail': detail,
        'lastError': last_error,
        'latencyMs': round(float(latency_ms), 1),
        'since': since or now_iso(),
    }


# ----------------------------------------------------------------------
# 载体接口
# ----------------------------------------------------------------------
# 槽名与键名最终会变成目录名和文件名，因此只允许安全字符，且显式拒绝 . 与 ..
_SAFE_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_FORBIDDEN_SEGMENTS = frozenset({'.', '..'})


def _split_slot(slot: str) -> list[str]:
    raw = str(slot or '').replace('\\', '/')
    parts = [part for part in raw.split('/') if part]
    if not parts:
        raise CarrierError('槽名不能为空')
    for part in parts:
        if part in _FORBIDDEN_SEGMENTS or not _SAFE_SEGMENT_RE.match(part):
            raise CarrierError(f'非法的槽名片段：{part!r}')
    return parts


def _check_key(key: str) -> str:
    text = str(key or '')
    if text in _FORBIDDEN_SEGMENTS or not _SAFE_SEGMENT_RE.match(text):
        raise CarrierError(f'非法的键名：{key!r}')
    return text


class RelayCarrier(abc.ABC):
    """中转载体：命名消息槽 + 命名增量流。

    两种语义对应两种数据：

    - **命名消息槽**（``put/read/list/drop/move_message``）：一条消息一个键，
      可靠、可枚举、可归档。用于 ``requests`` / ``results`` / ``peers``。
    - **命名增量流**（``append/read_stream``）：单向追加字节，读端自己持有游标
      （``read_stream`` 返回「自上次读以来的新字节」）。用于终端与日志。

    读游标由载体实例持有，因此共享目录的实现是「文件 + 偏移」，而点对点载体的
    实现是「接收缓冲区」—— 两者对上层完全一致。
    """

    kind: str = 'abstract'

    def __init__(self, peer_id: str):
        self.peer_id = str(peer_id or '')
        self._open = False
        self._status = make_link_status(kind=self.kind)

    # --- 生命周期 ---
    @abc.abstractmethod
    def open(self) -> bool:
        """建立链路并写入必要的布局。返回是否可用。"""

    @abc.abstractmethod
    def close(self) -> None:
        """释放链路。可重复调用。"""

    def is_open(self) -> bool:
        return self._open

    def status(self) -> dict:
        return dict(self._status)

    def _set_status(self, state: str, **kwargs) -> None:
        previous = self._status.get('state')
        status = make_link_status(state, kind=self.kind, **kwargs)
        if state == previous:
            status['since'] = self._status.get('since') or status['since']
        self._status = status

    # --- 命名消息槽 ---
    @abc.abstractmethod
    def put_message(self, slot: str, key: str, data: bytes) -> bool: ...

    @abc.abstractmethod
    def read_message(self, slot: str, key: str) -> Optional[bytes]: ...

    @abc.abstractmethod
    def list_keys(self, slot: str, prefix: str = '', suffix: str = '.json') -> list[str]: ...

    @abc.abstractmethod
    def drop_message(self, slot: str, key: str) -> bool: ...

    @abc.abstractmethod
    def move_message(self, slot: str, key: str, target_slot: str) -> Optional[str]: ...

    # --- 命名增量流 ---
    @abc.abstractmethod
    def append_stream(self, name: str, data: bytes) -> bool: ...

    @abc.abstractmethod
    def read_stream(self, name: str, limit: int = STREAM_READ_CHUNK) -> tuple[bytes, bool]: ...

    @abc.abstractmethod
    def reset_stream(self, name: str) -> None: ...


class SharedFolderCarrier(RelayCarrier):
    """共享目录载体：中转服务器的共享文件夹当消息总线。

    单写者原则下每端只写自己的子目录，因此这里**不做任何跨机文件锁**。

    **SMB 阻塞风险**：Windows 上对失联共享的文件访问可能阻塞数十秒。本类
    所有方法都是同步的，调用方必须把它们放进线程池执行，绝不能在 asyncio
    事件循环或 2 秒设备轮询线程里直接调用。
    """

    kind = CARRIER_SHARED_FOLDER

    LAYOUT_DIRS = (
        relay.SLOT_PEERS, relay.SLOT_REQUESTS, relay.SLOT_RESULTS,
        relay.SLOT_STREAMS, relay.SLOT_ARCHIVE, 'probe',
    )

    def __init__(self, peer_id: str, root: str):
        super().__init__(peer_id)
        # 注意：绝不能对空字符串做 abspath —— 那会退化成当前工作目录，把「未
        # 配置」当成「配置为程序自身目录」，进而在里面建出整套中转布局。
        raw_root = str(root or '').strip()
        self.root = os.path.abspath(raw_root) if raw_root else ''
        self._tails: dict[str, StreamTail] = {}
        self._writers: dict[str, StreamWriter] = {}

    # --- 路径 ---
    def slot_path(self, slot: str) -> str:
        return os.path.join(self.root, *_split_slot(slot))

    def _message_path(self, slot: str, key: str) -> str:
        return os.path.join(self.slot_path(slot), _check_key(key))

    def stream_path(self, name: str) -> str:
        return self._message_path(relay.SLOT_STREAMS, name)

    # --- 生命周期 ---
    def open(self) -> bool:
        if not self.root:
            self._set_status(relay.STATE_UNCONFIGURED, last_error='未配置共享目录')
            self._open = False
            return False
        self._set_status(relay.STATE_CONNECTING)
        try:
            for name in self.LAYOUT_DIRS:
                os.makedirs(self.slot_path(name), exist_ok=True)
        except OSError as exc:
            self._open = False
            self._set_status(relay.STATE_DISCONNECTED, detail=self.root, last_error=str(exc))
            return False

        ok, detail, latency = self.probe()
        self._open = ok
        if ok:
            self._set_status(
                relay.STATE_CONNECTED, detail=self.root, latency_ms=latency,
            )
        else:
            self._set_status(relay.STATE_DISCONNECTED, detail=self.root, last_error=detail)
        return ok

    def probe(self) -> tuple[bool, str, float]:
        """写-读-删一个小文件，确认目录真的可读写并测出一次往返延迟。

        只建目录不能证明可用：目录可能在本地被缓存，而共享已经掉线。
        """
        token = uuid.uuid4().hex
        path = os.path.join(self.root, 'probe', f'{self.peer_id or "peer"}.probe')
        started = time.time()
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(token)
            with open(path, 'r', encoding='utf-8') as handle:
                got = handle.read()
            os.remove(path)
        except OSError as exc:
            return False, f'{type(exc).__name__}: {exc}', 0.0
        if got != token:
            return False, '探针内容不一致（可能存在缓存或同步冲突）', 0.0
        return True, 'ok', (time.time() - started) * 1000.0

    def close(self) -> None:
        self._tails.clear()
        self._writers.clear()
        self._open = False
        self._set_status(relay.STATE_DISCONNECTED, detail=self.root)

    # --- 命名消息槽 ---
    # 命名约定：**槽名/键名非法一律抛 CarrierError，不与 IO 失败混淆**。名字由
    # 本机代码构造，非法即是调用方 bug，必须响亮地失败；只有 IO 故障返回
    # False/None。远端可控的 peerId 已在 relay 侧校验过，不会流到这里。
    def put_message(self, slot: str, key: str, data: bytes) -> bool:
        try:
            atomic_write_bytes(self._message_path(slot, key), data)
        except OSError:
            return False
        return True

    def read_message(self, slot: str, key: str) -> Optional[bytes]:
        try:
            return relay_file_io.read_bytes(self._message_path(slot, key))
        except OSError:
            return None

    def list_keys(self, slot: str, prefix: str = '', suffix: str = '.json') -> list[str]:
        directory = self.slot_path(slot)
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        picked = [
            name for name in names
            if name.startswith(prefix) and name.endswith(suffix) and not relay.is_provisional(name)
        ]
        picked.sort()
        return picked

    def drop_message(self, slot: str, key: str) -> bool:
        try:
            os.remove(self._message_path(slot, key))
        except OSError:
            return False
        return True

    def move_message(self, slot: str, key: str, target_slot: str) -> Optional[str]:
        """把消息移到另一个槽（归档用）。目标重名时加随机后缀，不覆盖。"""
        source = self._message_path(slot, key)
        target_dir = self.slot_path(target_slot)
        if not os.path.isfile(source):
            return None
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(source))
        if os.path.exists(target):
            target = f'{target}.{uuid.uuid4().hex[:6]}'
        try:
            os.replace(source, target)
        except OSError:
            return None
        return target

    # --- 命名增量流 ---
    def _tail(self, name: str) -> StreamTail:
        key = _check_key(name)
        tail = self._tails.get(key)
        if tail is None:
            tail = StreamTail(self.stream_path(key), 0)
            self._tails[key] = tail
        return tail

    def append_stream(self, name: str, data: bytes) -> bool:
        if not data:
            return True
        return StreamWriter(self.stream_path(_check_key(name))).append(data)

    def read_stream(self, name: str, limit: int = STREAM_READ_CHUNK) -> tuple[bytes, bool]:
        tail = self._tail(name)
        return tail.read_new(limit), tail.rebuilt

    def reset_stream(self, name: str) -> None:
        self._tails.pop(_check_key(name), None)


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def create_carrier(kind: str, peer_id: str, options: Optional[dict] = None) -> RelayCarrier:
    """按配置创建载体。

    新载体的接入点只有这里 + 一个新的 ``RelayCarrier`` 实现：协议层、会话层与
    前端都不需要改动。
    """
    options = dict(options or {})
    spec = get_carrier_spec(kind)
    if spec is None:
        raise CarrierUnsupported(f'未知的载体类型：{kind!r}')
    if not spec.implemented:
        raise CarrierUnsupported(f'载体「{spec.label}」尚未实现（{spec.notes}）')
    if kind == CARRIER_SHARED_FOLDER:
        root = options.get('root') or ''
        return SharedFolderCarrier(peer_id, root)
    raise CarrierUnsupported(f'载体类型缺少实现：{kind!r}')


def describe_carriers() -> list[dict]:
    """给设置页用的载体清单：已实现的与计划中的一起返回。"""
    out = []
    for spec in CARRIER_SPECS.values():
        item = asdict(spec)
        item['optionFields'] = list(spec.option_fields)
        out.append(item)
    return out


# ----------------------------------------------------------------------
# 清单协商
# ----------------------------------------------------------------------
def manifest_is_current(
    manifest: Optional[dict],
    carrier_kind: str,
    required_features: Optional[tuple] = None,
) -> bool:
    """清单是否已经是本版本当前形态（无需重写）。

    多实例（多台电脑）会同时在同一共享目录上启动，若每次都无条件重写清单，
    两个进程就会在同一毫秒里抢着替换同一个 ``relay.json`` —— Windows 上这是
    一次共享冲突，会把其中一端的启动直接打断。所以：形态一致就不写。

    ``requiredFeatures`` 按**超集**判：清单里已声明的特性多于本实例需要的，也算
    当前形态。特性只增不减 —— 声明过的能力永远不能被另一个实例悄悄降级回去，
    否则「老端读到未知特性会拒绝启动」这条保护会在某次并发启动后消失。
    """
    if not manifest:
        return False
    if not (
        manifest.get('layoutVersion') == relay.LAYOUT_VERSION
        and int(manifest.get('schema') or 0) == relay.RELAY_SCHEMA
        and manifest.get('carrierKind') == carrier_kind
    ):
        return False
    declared = set(manifest.get('requiredFeatures') or ())
    wanted = set(relay.BASE_FEATURES if required_features is None else required_features)
    return wanted <= declared


def ensure_manifest(queue: RelayQueue, carrier_kind: str, required_features: Optional[tuple] = None) -> dict:
    """写入或补齐 ``relay.json``：布局版本、载体类型与能力协商项。

    能力协商是「可扩展」的第二个落点：未来加 blob 搬运、加签名时，只在这里
    追加 ``requiredFeatures``，老版本读到不认识的特性会明确拒绝启动，而不是
    用一半功能跑出难以定位的错。

    ``required_features`` 缺省是 ``relay.BASE_FEATURES``（与老版本逐字节一致）；
    启用池（``poolId`` 非空）时由调用方追加 ``relay.FEATURE_POOL``。
    """
    wanted = set(relay.BASE_FEATURES if required_features is None else required_features)
    manifest = read_json(queue.manifest_path) or {}
    if manifest_is_current(manifest, carrier_kind, required_features):
        return manifest
    changed = False
    # 只并集、不收缩：见 ``manifest_is_current`` 的说明。
    #
    # 顺序也有讲究：先按 ``BASE_FEATURES`` 的声明顺序排（未启用池时写出来的清单与
    # 老版本**逐字节一致**，这条兼容承诺要靠它成立），额外特性再接在后面并按名排序
    # （保证「谁先启动都一样」的确定性，否则两端会为同一份清单反复互相重写）。
    extra = sorted(set(manifest.get('requiredFeatures') or ()) | wanted)
    features = [name for name in relay.BASE_FEATURES if name in set(extra)]
    features += [name for name in extra if name not in features]
    defaults = {
        'schema': relay.RELAY_SCHEMA,
        'layoutVersion': relay.LAYOUT_VERSION,
        'createdAt': manifest.get('createdAt') or now_iso(),
        'carrierKind': carrier_kind,
        'requiredFeatures': features,
    }
    for key, value in defaults.items():
        if manifest.get(key) != value:
            manifest[key] = value
            changed = True
    if changed:
        try:
            relay.atomic_write_json(queue.manifest_path, manifest)
        except OSError:
            # 另一个实例可能刚好赢了这次替换。只要落盘的那份可用就继续；
            # 落盘内容不可用才把错误抛出去（宁可不启动，也不半功能运行）。
            landed = read_json(queue.manifest_path)
            if not manifest_is_current(landed, carrier_kind):
                raise
            return landed
    return manifest


def check_manifest(manifest: Optional[dict], supported=None) -> tuple[bool, str]:
    """判断清单是否可被本版本处理。"""
    if not manifest:
        return False, '清单缺失或损坏'
    supported = set(relay.KNOWN_FEATURES if supported is None else supported)
    unknown = [name for name in (manifest.get('requiredFeatures') or []) if name not in supported]
    if unknown:
        return False, f'对端要求本版本未实现的特性：{"、".join(unknown)}'
    version = manifest.get('layoutVersion') or 0
    if int(version or 0) > relay.LAYOUT_VERSION:
        return False, f'对端布局版本 {version} 高于本机 {relay.LAYOUT_VERSION}'
    if int(manifest.get('schema') or 0) > relay.RELAY_SCHEMA:
        return False, f'对端协议版本 {manifest.get("schema")} 高于本机 {relay.RELAY_SCHEMA}'
    return True, 'ok'
