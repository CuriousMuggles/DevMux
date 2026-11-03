"""可信对端：首次连接确认与名单。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import os
from dataclasses import (
    asdict,
    dataclass,
)
from typing import Optional

from .relay_constants import (
    RELAY_SCHEMA,
    normalize_role,
)
from . import relay_store


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
        data = self.storage.read() if self.storage is not None else (relay_store.read_json(self.path) or {})
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
            relay_store.atomic_write_json(self.path, {
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
            confirmedAt=relay_store.now_iso(),
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
