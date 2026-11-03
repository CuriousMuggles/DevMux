"""消息落盘原语：时间戳与原子写 / 读。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import (
    datetime,
    timezone,
)
from typing import Optional

from .relay_constants import (
    REPLACE_ATTEMPTS,
    REPLACE_BACKOFF_S,
    _CONFLICT_COPY_RE,
    _TMP_MARKER,
)
from . import relay_file_io


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
