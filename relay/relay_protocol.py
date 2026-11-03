"""控制面：请求与结果的消息模型、按对端分区与队列。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import (
    asdict,
    dataclass,
    field,
)
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Optional

from .relay_constants import (
    ARCHIVE_EXISTS_RETRIES,
    ARCHIVE_EXISTS_RETRY_S,
    ARCHIVE_RETENTION_DAYS,
    BASE_FEATURES,
    DEFAULT_REQUEST_TTL_S,
    FAST_JOURNAL_MAX_BYTES,
    LAYOUT_VERSION,
    RELAY_SCHEMA,
    SLOT_ARCHIVE,
    SLOT_PEERS,
    SLOT_REQUESTS,
    SLOT_RESULTS,
    SLOT_STREAMS,
    STATUS_OK,
)
from . import relay_file_io
from . import relay_store
from .relay_stream import StreamChannel


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
        deadline = relay_store._epoch_ms(self.expiresAt)
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
    stamp = int(relay_store._epoch_ms(request.createdAt))
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
            relay_store.atomic_write_json(self.manifest_path, {
                'schema': RELAY_SCHEMA,
                'layoutVersion': LAYOUT_VERSION,
                'createdAt': relay_store.now_iso(),
                'requiredFeatures': list(BASE_FEATURES),
            })

    # --- 操作端侧 ---
    def submit(self, request: Request) -> str:
        """写入一条请求，返回其正式文件路径。"""
        self.ensure_layout()
        self._seq += 1
        path = os.path.join(self.requests_dir, make_req_filename(request, self._seq))
        relay_store.atomic_write_json(path, request.to_dict())
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
        for path in relay_store.list_messages(self.requests_dir, 'r-'):
            data = relay_store.read_json(path)
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
        from . import relay_archive
        return relay_archive.has_archived_result(self.archive_dir, req_id)

    def write_result(self, result: Result) -> str:
        self.ensure_layout()
        path = self.result_path(result.reqId)
        relay_store.atomic_write_json(path, result.to_dict())
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
        data = relay_store.read_json(self.result_path(req_id))
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
