"""配对：本机对「我要跟谁通讯」的决定。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import (
    asdict,
    dataclass,
    field,
)
from typing import Optional

from .relay_constants import (
    PAIR_CONFIRMED,
    PAIR_DISCOVERED,
    PAIR_REQUESTED,
    PAIR_REVOKED,
    PAIR_STATE_LABELS,
    RELAY_SCHEMA,
)
from . import relay_store


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
        data = self.storage.read() if self.storage is not None else (relay_store.read_json(self.path) or {})
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
            relay_store.atomic_write_json(self.path, {
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
            record = PairRecord(nodeId=node_id, state=PAIR_REQUESTED, requestedAt=relay_store.now_iso())
            self._records[node_id] = record
        elif record.state in (PAIR_DISCOVERED, PAIR_REVOKED):
            record.requestedAt = relay_store.now_iso()
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
        record.confirmedAt = relay_store.now_iso()
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
        record.revokedAt = relay_store.now_iso()
        return self.save()

    def forget(self, node_id: str) -> bool:
        self._ensure()
        return self._records.pop(str(node_id or ''), None) is not None and self.save()
