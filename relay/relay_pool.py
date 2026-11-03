"""池（pool）：实例键、注册表与槽位。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import (
    asdict,
    dataclass,
)
from typing import Optional

from .relay_constants import (
    HEARTBEAT_GRACE_MS,
    POOL_DIR,
    REGISTRY_LEASE_S,
    RELAY_SCHEMA,
    ROLE_SLAVE,
    SLOT_REGISTRY,
    normalize_role,
    role_label,
)
from . import relay_file_io
from .relay_protocol import (
    check_peer_id,
    valid_peer_id,
)
from . import relay_store


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
    stamp = relay_store._epoch_ms(stamp_iso)
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
        stamp = relay_store._epoch_ms(self.lastSeen)
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
        if not name.endswith('.json') or relay_store.is_provisional(name):
            continue
        path = os.path.join(directory, name)
        data = relay_store.read_json(path)
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
    relay_store.atomic_write_json(path, entry.to_dict())
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
