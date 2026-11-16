"""数据面：单向追加流（StreamTail / StreamWriter / StreamChannel）。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .relay_constants import (
    RELAY_SCHEMA,
    STREAM_READ_CHUNK,
)
from . import relay_file_io
from . import relay_store


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
                # 直接读代替「先 stat 再 read」：网络盘上两者各是一次往返，
                # 而热点路径（有新数据）只需要一次。只有读到空时才回头 stat，
                # 判断是 EOF 还是文件被归档后同名重建；重建则归零并立即读到新内容。
                data = relay_file_io.read_bytes(self.path, self.offset, limit)
                if data:
                    self.offset += len(data)
                    return data
                size = relay_file_io.stat(self.path).st_size
                if size < self.offset:
                    self.offset = 0
                    self.rebuilt = True
                    data = relay_file_io.read_bytes(self.path, 0, limit)
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
            relay_store.atomic_write_json(self.meta_path, payload)
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
