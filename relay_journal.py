"""共享控制通道：分段单写者日志，本机事务收件箱与执行去重。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import relay
import relay_file_io

VERSION = 1
SEGMENT_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 32
_NAME = re.compile(r'^([0-9a-f]{32})-([0-9]{8})\.jsonl$')


def _encode(body):
    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('控制消息超过日志单条大小限制')
    return json.dumps({'sha256': hashlib.sha256(raw).hexdigest(),
                       'body': raw.decode('utf-8')}, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8') + b'\n'


def _decode(line):
    envelope = json.loads(line)
    if not isinstance(envelope, dict) or not isinstance(envelope.get('body'), str):
        raise ValueError('控制日志记录格式非法')
    raw = envelope['body'].encode('utf-8')
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('控制日志记录超过大小限制')
    if hashlib.sha256(raw).hexdigest() != envelope.get('sha256'):
        raise ValueError('控制日志摘要不一致，已停止消费')
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError('控制日志内容不是对象')
    return body


class Writer:
    """一个方向一个写者，轮换和确认只按字节位置，不比较两机时间。"""

    def __init__(self, directory, epoch, *, segment_bytes=SEGMENT_BYTES,
                 max_segments=MAX_SEGMENTS):
        self.directory = Path(directory)
        self.epoch = epoch
        self.segment_bytes = segment_bytes
        self.max_segments = max_segments
        self.sequence = 0
        self.lock = threading.RLock()
        self.broken = False
        self.announced = -1
        self.last_collect = -1.0
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, sequence):
        return self.directory / f'{self.epoch}-{sequence:08d}.jsonl'

    def _collect(self, *, force=False):
        if not force and time.monotonic() - self.last_collect < 1:
            return
        self.last_collect = time.monotonic()
        # 确认文件由唯一接收实例写入；不删除未确认、当前仍可追加的段。
        for ack in self.directory.glob('*.ack'):
            epoch = ack.stem
            if not re.fullmatch('[0-9a-f]{32}', epoch):
                continue
            try:
                data = json.loads(relay_file_io.read_bytes(str(ack)))
            except FileNotFoundError:
                continue
            number, offset = data.get('segment', -1), data.get('offset', -1)
            if not isinstance(number, int) or not isinstance(offset, int):
                continue
            remains = False
            for path in self.directory.glob(epoch + '-*.jsonl'):
                match = _NAME.fullmatch(path.name)
                if not match:
                    continue
                seq = int(match[2])
                sealed = epoch != self.epoch or seq < self.sequence
                try:
                    size = relay_file_io.stat(str(path)).st_size
                except FileNotFoundError:
                    continue
                if sealed and (seq < number or (seq == number and size == offset)):
                    path.unlink()
                else:
                    remains = True
            if epoch != self.epoch and not remains:
                ack.unlink(missing_ok=True)

    def append(self, body):
        line = _encode(body)
        with self.lock:
            if self.broken:
                raise OSError('日志写入结果不确定，重启前不再追加')
            self._collect()
            path = self._path(self.sequence)
            size = path.stat().st_size if path.exists() else 0
            if size and size + len(line) > self.segment_bytes:
                self.sequence += 1
                self._collect(force=True)
                path = self._path(self.sequence)
            if not path.exists() and len(list(self.directory.glob('*.jsonl'))) >= self.max_segments:
                self._collect(force=True)
                if len(list(self.directory.glob('*.jsonl'))) >= self.max_segments:
                    raise OSError('控制日志未确认积压已满，本次未投递；请恢复对端连接')
            # head先发布，读端能按固定路径发现新段；空段/半条不会被确认消费。
            if self.announced != self.sequence:
                relay.atomic_write_json(str(self.directory / 'head.json'),
                                        {'writer': self.epoch, 'segment': self.sequence})
                self.announced = self.sequence
            try:
                with open(path, 'ab') as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                self.broken = True
                raise
            return str(path)


class Runtime:
    """状态库必须位于本机；共享目录只有消息日志、发现头和消费确认。"""

    def __init__(self, state_path, node_id):
        self.node_id = relay.check_peer_id(node_id, '实例键')
        if relay_file_io.is_remote(str(state_path)):
            raise ValueError('控制日志状态库必须位于本机磁盘')
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        # 同实例状态库只能有一个活跃执行端；否则重启恢复可能误判仍在运行的请求。
        self.owner = open(str(state_path) + '.owner', 'a+b')
        self.owner.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.owner.close()
                raise OSError('该实例的控制日志状态库正在被其他节点使用')
        else:
            import fcntl
            try:
                fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.owner.close()
                raise
        self.epoch = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.writers = {}
        self.acknowledged = {}
        self.notices = {}
        self.closed = False
        self.db = sqlite3.connect(str(state_path), check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, target TEXT, body TEXT, route TEXT,
                state TEXT, result TEXT, published INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, offset INTEGER);
        ''')
        # 在副作用发生与回执提交之间退出，不能推断执行失败，更不能再次执行。
        for (req_id,) in self.db.execute("SELECT id FROM inbox WHERE state='running'").fetchall():
            result = relay.Result(reqId=req_id, status=relay.STATUS_ERROR,
                                  error='执行端重启，原操作结果不确定，不会自动重发执行',
                                  extra={'outcomeUnknown': True}).to_dict()
            self.db.execute("UPDATE inbox SET state='done',result=?,published=0 WHERE id=?",
                            (json.dumps(result), req_id))
        self.db.commit()

    def writer(self, directory):
        key = os.path.abspath(directory)
        with self.lock:
            if key not in self.writers:
                self.writers[key] = Writer(key, self.epoch)
            return self.writers[key]

    def consume(self, directory, accept):
        """先事务提交内容与游标，再确认；失败保持原游标并交由下次轮询恢复。"""
        directory = Path(directory)
        paths = set(directory.glob('*.jsonl'))
        # 固定头通过无缓存读取，避免SMB目录缓存阻止发现刚创建的新段。
        try:
            head = json.loads(relay_file_io.read_bytes(str(directory / 'head.json')))
            if not isinstance(head, dict):
                raise ValueError('控制日志发现头格式非法')
            epoch, number = head.get('writer'), head.get('segment')
            if not isinstance(epoch, str) or not re.fullmatch('[0-9a-f]{32}', epoch) or not isinstance(number, int) or not 0 <= number < 100000000:
                raise ValueError('控制日志发现头非法')
            paths.add(directory / f'{epoch}-{number:08d}.jsonl')
        except FileNotFoundError:
            pass
        # 不能仅消费目录里可见的最新段：SMB枚举可能漏掉前一段。根据段号补齐，
        # 已删除段只有本机持久游标才能证明已消费，缺段绝不能推进确认。
        maxima = {}
        for path in paths:
            match = _NAME.fullmatch(path.name)
            if match:
                maxima[match[1]] = max(maxima.get(match[1], 0), int(match[2]))
        required = set()
        with self.lock:
            cursors = dict(self.db.execute('SELECT path,offset FROM cursors').fetchall())
        for epoch, maximum in maxima.items():
            known = [int(match[2]) for name in cursors
                     if Path(name).parent == directory and (match := _NAME.fullmatch(Path(name).name))
                     and match[1] == epoch]
            first = max(known) if known else 0
            if maximum - first > MAX_SEGMENTS:
                raise ValueError('控制日志缺少连续消费记录，拒绝跳段')
            for number in range(first, maximum + 1):
                path = directory / f'{epoch}-{number:08d}.jsonl'
                required.add(path)
                paths.add(path)
            paths = {path for path in paths if not (match := _NAME.fullmatch(path.name))
                     or match[1] != epoch or int(match[2]) >= first}
        for path in sorted(paths):
            match = _NAME.fullmatch(path.name)
            if not match:
                continue
            with self.lock:
                row = self.db.execute('SELECT offset FROM cursors WHERE path=?', (str(path),)).fetchone()
                offset = row[0] if row else 0
                try:
                    size = relay_file_io.stat(str(path)).st_size
                    if size < offset:
                        raise ValueError('控制日志被截断，已停止消费')
                    data = relay_file_io.read_bytes(str(path), offset, MAX_RECORD_BYTES * 2 + 1024)
                except FileNotFoundError:
                    if path in required and not row and int(match[2]) < maxima[match[1]]:
                        raise OSError('控制日志中间段暂不可见，等待恢复，不跳段消费')
                    continue  # 已确认段可由写者回收。
                complete = data.rfind(b'\n') + 1
                if not complete and len(data) > MAX_RECORD_BYTES * 2:
                    raise ValueError('控制日志未结束记录超过限制')
                if offset + complete < size and int(match[2]) < maxima[match[1]]:
                    raise ValueError('封闭日志段末尾不完整，已停止消费')
                end = offset + complete
                if complete:
                    try:
                        for line in data[:complete].splitlines():
                            accept(_decode(line))
                        self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (str(path), end))
                        self.db.commit()
                    except Exception:
                        self.db.rollback()
                        raise
                # 同一写者旧段不能让确认游标倒退；确认失败后下次仍会补发。
                ack_path = str(directory / (match[1] + '.ack'))
                position = (int(match[2]), end)
                if end and position > self.acknowledged.get(ack_path, (-1, -1)):
                    relay.atomic_write_json(str(directory / (match[1] + '.ack')),
                                            {'segment': int(match[2]), 'offset': end})
                    self.acknowledged[ack_path] = position

    def receive_requests(self, queue):
        if self.closed:
            return []
        base = Path(queue.requests_dir) / 'journal-v1'
        sources = {path.name for path in base.glob('*') if path.is_dir()}
        # 实例键来自心跳而非目录枚举，新通道首次连接也能按已知固定路径读。
        sources.update(getattr(queue, 'journal_sources', ()))
        for source in sorted(sources):
            if not relay.valid_peer_id(source):
                continue
            route = str(Path(queue.results_dir) / 'journal-v1' / source)

            def accept(body):
                request = relay.Request.from_dict(body)
                if request.payload.get('sourceNodeId') != source:
                    raise ValueError('控制日志来源实例不一致')
                previous = self.db.execute('SELECT body FROM inbox WHERE id=?', (request.reqId,)).fetchone()
                encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
                if previous and previous[0] != encoded:
                    raise ValueError('同一请求标识出现不同正文，已停止消费')
                self.db.execute('INSERT OR IGNORE INTO inbox(id,target,body,route,state) VALUES (?,?,?,?,?)',
                                (request.reqId, queue.peer_id, encoded, route, 'pending'))

            try:
                self.consume(base / source, accept)
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.notices[str(base / source)] = f'控制日志消费暂停：{exc}'
            else:
                self.notices.pop(str(base / source), None)
        self.flush_results(queue.peer_id)
        with self.lock:
            rows = self.db.execute("SELECT id,body FROM inbox WHERE target=? AND state='pending' ORDER BY rowid",
                                   (queue.peer_id,)).fetchall()
        return [('journal:' + req_id, relay.Request.from_dict(json.loads(body))) for req_id, body in rows]

    def claim(self, req_id):
        with self.lock, self.db:
            return self.db.execute("UPDATE inbox SET state='running' WHERE id=? AND state='pending'",
                                   (req_id,)).rowcount == 1

    def finish(self, result):
        with self.lock, self.db:
            self.db.execute("UPDATE inbox SET state='done',result=?,published=0 WHERE id=?",
                            (json.dumps(result.to_dict(), ensure_ascii=False), result.reqId))

    def flush_results(self, target):
        with self.lock:
            rows = self.db.execute("SELECT id,route,result FROM inbox WHERE target=? AND state='done' AND published=0",
                                   (target,)).fetchall()
            for req_id, route, body in rows:
                try:
                    self.writer(route).append(json.loads(body))
                except (OSError, ValueError) as exc:
                    self.notices[route] = f'回执发布暂停，原结果已保存在本机：{exc}'
                    continue
                self.notices.pop(route, None)
                self.db.execute('UPDATE inbox SET published=1 WHERE id=?', (req_id,))
                self.db.commit()

    def read_reply(self, queue, req_id):
        def accept(body):
            result = relay.Result.from_dict(body)
            self.db.execute('INSERT OR REPLACE INTO replies VALUES (?,?)',
                            (result.reqId, json.dumps(body, ensure_ascii=False)))

        with self.lock:
            if self.closed:
                return None
            self.consume(Path(queue.results_dir) / 'journal-v1' / self.node_id, accept)
            row = self.db.execute('SELECT body FROM replies WHERE id=?', (req_id,)).fetchone()
        return relay.Result.from_dict(json.loads(row[0])) if row else None

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.db.close()
                self.owner.close()


class JournalQueue(relay.RelayQueue):
    """接收双协议，发送仅按协商选择一种协议；流相关接口沿用旧队列。"""

    def __init__(self, root, peer_id, runtime, *, sending=False):
        super().__init__(root, peer_id)
        self.runtime = runtime
        self.sending = sending
        self.journal_sources = ()

    def submit(self, request):
        if not self.sending:
            return super().submit(request)
        directory = Path(self.requests_dir) / 'journal-v1' / self.runtime.node_id
        return self.runtime.writer(directory).append(request.to_dict())

    def pending_requests(self, source_peer_ids=None):
        return super().pending_requests(source_peer_ids) + self.runtime.receive_requests(self)

    def claim_request(self, path, request):
        return not path.startswith('journal:') or self.runtime.claim(request.reqId)

    def has_result(self, req_id):
        with self.runtime.lock:
            row = self.runtime.db.execute('SELECT state FROM inbox WHERE id=?', (req_id,)).fetchone()
        return bool(row and row[0] != 'pending') or super().has_result(req_id)

    def write_result(self, result):
        with self.runtime.lock:
            row = self.runtime.db.execute('SELECT 1 FROM inbox WHERE id=?', (result.reqId,)).fetchone()
        if not row:
            return super().write_result(result)
        self.runtime.finish(result)
        # 本机结果已持久提交，网络发布失败不覆盖原执行结果；轮询会重试发布。
        try:
            self.runtime.flush_results(self.peer_id)
        except OSError:
            pass
        return 'journal:' + result.reqId

    def read_result(self, req_id):
        return self.runtime.read_reply(self, req_id) if self.sending else super().read_result(req_id)

    def archive_file(self, path):
        if str(path).startswith('journal:') or (self.sending and os.path.dirname(path) == self.results_dir):
            return None
        return super().archive_file(path)
