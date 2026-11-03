"""RelayNode 的出站能力（``NodeRemoteMixin``）：主控侧下发与增量读流。

本机设备与远端设备在这里被拉平成同构的 key：本机是 ``serial:tid=2``，远端是
``relay:<peerId>:serial:tid=2``，调用方不需要区分两者。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import relay
from . import relay_adb
from . import relay_journal
from . import relay_session
from .relay_adb import DEFAULT_ADB_TIMEOUT_S, DEFAULT_LOG_TIMEOUT_S, ExecOutcome
from .relay_node_constants import (
    REMOTE_DEVICE_LINGER_S,
    STREAM_ID_RE,
    STREAM_READ_LIMIT,
)


class NodeRemoteMixin:
    """主控侧的下发、等待与读流。依赖 ``RelayNode.__init__`` 建立的属性。"""

    # --- 主控：本地设备 ---
    def run_local(self, device_key_or_serial: str, argv, timeout: float = DEFAULT_ADB_TIMEOUT_S) -> ExecOutcome:
        """主控直接操作自己的设备（不经中转）。"""
        device = self._match_device(str(device_key_or_serial), str(device_key_or_serial))
        if device is None:
            # 同 _resolve_request_device：一次轮询快照为空不代表设备不在。
            self._refresh_devices()
            device = self._match_device(str(device_key_or_serial), str(device_key_or_serial))
        if device is None:
            return relay_adb.ExecOutcome(
                error=f'本机没有匹配的设备：{device_key_or_serial}（{self._describe_devices()}）',
                elapsedMs=0,
            )
        self._emit('command', f'本机直接执行 {argv} @ {device.get("key")}')
        return relay_adb.execute(self.adb_path, device, argv, timeout=timeout, max_bytes=self.max_output_bytes)

    # --- 主控：远端设备 ---
    def remote_devices(self, linger_s: Optional[float] = None) -> list[dict]:
        """从机对外共享的设备，key 带 ``relay:<peerId>:`` 前缀。

        在线的从机正常列出；**离线但在保留窗口内**的从机，其设备仍列出并置
        ``relay.online = False``，由前端灰显并禁止发起新操作 —— 与本地设备离线
        时的表现保持一致。超过保留窗口（``REMOTE_DEVICE_LINGER_S``）视为对端已
        退场，不再列出。
        """
        window = REMOTE_DEVICE_LINGER_S if linger_s is None else float(linger_s)
        out: list[dict] = []
        for view in self._health.peers:
            if view.role != relay.ROLE_SLAVE:
                continue
            if not view.online and view.ageMs > window * 1000.0:
                continue
            if self.pairing_required:
                if view.online and not self.connection_ready(view.peerId):
                    continue
                if not view.online and self._connection_verified.get(view.peerId, ((), 0))[0] != self._connection_endpoint(view.peerId):
                    continue
            for device in view.devices:
                local_key = str((device or {}).get('key') or '')
                if not local_key:
                    continue
                item = dict(device)
                item['origin'] = 'relay'
                item['peerId'] = view.peerId
                item['peerRole'] = view.role
                item['localKey'] = local_key
                item['key'] = relay_adb.remote_key(view.peerId, local_key)
                item['relay'] = {
                    'peerId': view.peerId,
                    'role': view.role,
                    'host': view.host,
                    'deploymentId': view.deploymentId,
                    'displayLabel': view.displayLabel,
                    # 地址与启动代次任一变化都需要重开终端，不能沿用旧流的游标。
                    'endpointRoot': self.channel_root(view.peerId),
                    'startedAt': view.startedAt,
                    'nodeId': view.nodeId,
                    'online': bool(view.online),
                    'ageMs': float(view.ageMs),
                    # 信任状态一并带给上层：主控据此决定「是否值得为它开会话」。
                    # 这只是体验优化，真正的准入裁决始终在从机侧（见 _authorize）。
                    'trusted': bool(view.trusted),
                    'needsConfirmation': bool(view.needsConfirmation),
                }
                out.append(item)
        return out

    def local_devices(self) -> list[dict]:
        out: list[dict] = []
        for device in self._managed_devices():
            item = dict(device)
            item['origin'] = 'local'
            item['localKey'] = item.get('key', '')
            out.append(item)
        return out

    def wait_for_peer_heartbeat(self, peer_id, timeout=3.0):
        """大文件发布后等待独立读端观察新进展；不投递请求、不延长在线期限。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not self._stop_flag.is_set():
            if self._pairing_blocks(peer_id):
                return False
            if not self._peer_offline_reason(peer_id):
                return True
            if time.monotonic() >= deadline:
                return False
            self._stop_flag.wait(0.05)
        return False

    def _peer_offline_reason(self, peer_id: str) -> str:
        """对端不可用时返回中文原因，可用时返回空串。

        只判断**传输层事实**（是否发现、心跳是否中断、角色是否为从机）。刻意不在
        这里判断「是否已确认」：那是安全边界，必须由从机裁决 —— 主控侧的预检只是
        体验优化，绝不能成为唯一防线，所以未确认时仍照常投递，由从机拒绝。
        """
        # 操作检查与界面、授权仲裁都从同一观测状态重算，不沿用旧health快照。
        with self._io_lock:
            self._health = self._compute_health()
        view = None
        for candidate in self._health.peers:
            if candidate.peerId == peer_id:
                view = candidate
                break
        if view is None:
            return '尚未发现该从机（心跳未到达）'
        if not view.online:
            age_s = max(0.0, float(view.ageMs) / 1000.0)
            return f'心跳已中断 {age_s:.1f}s'
        if view.role != relay.ROLE_SLAVE:
            return f'对端角色是{view.roleLabel}，不是从机'
        return ''

    def all_devices(self) -> list[dict]:
        """统一设备视图：主控的窗口里本地设备与远端设备同列。"""
        return self.local_devices() + self.remote_devices()

    def submit(
        self,
        peer_id: str,
        kind: str,
        *,
        device: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
        ttl_s: Optional[float] = None,
    ) -> relay.Result:
        """向某个从机投递一条请求并等待结果。

        TTL 取 ``max(默认值, 等待超时 + 10s)``：不能让请求在主控还在等的时候就
        过期作废。

        离线对端**不发请求**：直接返回一条明确的失败，而不是投出去干等到超时。
        离线期间堆积的请求一旦在对方恢复后被执行，是最危险的一类副作用
        （见设计文档 §7.3），所以这里 fail-closed。

        未配对的对端同理不发请求（准入门槛的生效点之一）。少了这一步，虽然请求
        仍会被对端的 ``_authorize`` 拒掉，但每次操作都会在**对方分区目录里留一条
        垃圾请求**、并换回一条拒绝结果 —— 那是实打实的写放大，且会污染对方的
        ``requests/`` 视图。

        **门槛判定排在离线判定之前**：未配对的对端根本不在 ``health.peers`` 里
        （读侧过滤已经把它摘掉了），若先判离线，用户看到的原因是「尚未发现该从机
        （心跳未到达）」—— 那是在让他去查网络，而真实原因是他还没配对。
        """
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if self._stop_flag.is_set():
            return relay.Result(reqId='', status=relay.STATUS_ERROR,
                error='中转节点已停止，本次操作已取消', agentId=target,
                finishedAt=relay.now_iso(), extra={'localCancelled': True})
        pair_reason = self._pairing_blocks(target)
        if not pair_reason and self.pairing_required:
            trusted, reason = self.trust.check(target, self._peer_fingerprint(target))
            if not trusted:
                pair_reason = f'对端 {target} 待本机确认身份：{reason}'
        if pair_reason:
            self._emit('reject', f'不下发请求：{pair_reason}')
            return relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'已取消本次操作：{pair_reason}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'unpaired': True},
            )
        offline_reason = self._peer_offline_reason(target)
        if offline_reason:
            self._emit('reject', f'不下发请求给离线从机 {target}：{offline_reason}')
            return relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'从机 {target} 当前离线，已取消本次操作：{offline_reason}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'peerOffline': True},
            )
        beat = self._peers.get(target)
        use_journal = self.control_journal and beat is not None and beat.controlJournal == relay_journal.VERSION
        queue = (self._journal_queue(target, sending=True) if use_journal
                 else relay.RelayQueue.for_peer(self.channel_root(target), target))
        record = self.pairing.get(self._node_id_for(target))
        # 已确认的历史关系只补齐代次，不把旧身份信任升级成实例配对。
        if record and record.is_paired and not record.connectionId:
            record = self.pairing.confirm(record.nodeId, peer_id=record.peerId,
                                         fingerprint=record.fingerprint, host=record.host)
        request_payload = {**dict(payload or {}), 'sourceNodeId': self.node_id,
                           'targetNodeId': self._node_id_for(target),
                           'connectionId': record.connectionId if record else ''}
        request = relay.new_request(
            kind, self.peer_id, device=dict(device or {}), payload=request_payload,
            ttl_s=float(ttl_s or max(relay.DEFAULT_REQUEST_TTL_S, float(timeout) + 10.0)),
        )
        try:
            queue.submit(request)
        except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
            if not use_journal:
                raise
            return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                error=f'控制日志投递未确认，不会自动重发或切换通道：{exc}',
                agentId=target, finishedAt=relay.now_iso(),
                extra={'outcomeUnknown': True, 'journalError': True})
        # 终端每次读写都记事件会产生界面广播与日志风暴，拖慢交互。
        if kind != relay.KIND_WINDOWS or (payload or {}).get('action') not in {
                'session_read', 'session_write', 'session_resize'}:
            self._emit('info', f'下发 {kind} → {target}（reqId {request.reqId}）：'
                              f'{payload.get("argv") if payload else ""}')

        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            if self._stop_flag.is_set():
                return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                    error='中转节点已停止，本次等待已取消', agentId=target,
                    finishedAt=relay.now_iso(), extra={'localCancelled': True})
            # 日志回执按reqId从本机持久收件箱取回，旧端继续读独立文件。
            try:
                result = queue.read_result(request.reqId)
            except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
                if not use_journal:
                    raise
                return relay.Result(reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=f'控制日志回执未确认，不会自动重发：{exc}',
                    agentId=target, finishedAt=relay.now_iso(),
                    extra={'outcomeUnknown': True, 'journalError': True})
            if result is not None:
                # 回执已经读到，网络归档失败或缓慢不应延迟终端就绪。
                try:
                    if not use_journal:
                        future = self._archive_pool.submit(queue.archive_file, queue.result_path(request.reqId))
                        with self._archive_lock:
                            self._archive_futures.add(future)
                        future.add_done_callback(self._archive_finished)
                except RuntimeError:
                    pass  # 节点正在停止，保留回执供后续清理。
            if result is not None:
                return result
            self._stop_flag.wait(0.05)
        return relay.Result(
            reqId=request.reqId, status=relay.STATUS_ERROR,
            error=f'等待从机 {target} 响应超时（{timeout:g}s）',
            agentId=target, finishedAt=relay.now_iso(), extra={'localTimeout': True},
        )

    def _archive_finished(self, future):
        with self._archive_lock:
            self._archive_futures.discard(future)

    def exec_remote(
        self,
        peer_id: str,
        device_key: str,
        argv,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
    ) -> relay.Result:
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        return self.submit(
            peer_id, relay.KIND_EXEC,
            device=device, payload={'argv': [str(part) for part in argv], 'timeout': timeout},
            timeout=timeout,
        )

    def exec_any(self, device_key: str, argv, timeout: float = DEFAULT_ADB_TIMEOUT_S):
        """按设备 key 自动选路：本地设备直接执行，远端设备经从机执行。

        这是「主控窗口里两类设备操作无区别」的落点 —— 调用方只给 key。
        """
        peer_id, local_key = relay_adb.split_remote_key(device_key)
        if not peer_id:
            return self.run_local(device_key, argv, timeout=timeout)
        return self.exec_remote(peer_id, local_key, argv, timeout=timeout)

    def start_remote_log(
        self,
        peer_id: str,
        device_key: str,
        argv,
        stream_id: str,
        timeout: float = DEFAULT_LOG_TIMEOUT_S,
    ) -> relay.Result:
        if not STREAM_ID_RE.match(str(stream_id)):
            return relay.Result(
                reqId='', status=relay.STATUS_REJECTED,
                error=f'流标识非法：{stream_id!r}', finishedAt=relay.now_iso(),
            )
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        return self.submit(
            peer_id, relay.KIND_START_LOG, device=device,
            payload={'streamId': stream_id, 'argv': [str(part) for part in argv]},
            timeout=timeout,
        )

    def stop_remote_log(self, peer_id: str, stream_id: str, timeout: float = DEFAULT_LOG_TIMEOUT_S) -> relay.Result:
        return self.submit(
            peer_id, relay.KIND_STOP_LOG, payload={'streamId': stream_id}, timeout=timeout,
        )

    def read_remote_stream(self, peer_id: str, stream_id: str, limit: int = STREAM_READ_LIMIT):
        """增量读远端流：返回 ``(新字节, 新offset, 是否被重建)``。

        游标由本端持有；``size < offset`` 说明流文件被重建，offset 已归零重读。
        """
        target = relay.check_peer_id(peer_id, '目标从机标识')
        cache_key = (target, stream_id)
        tail = self._tails.get(cache_key)
        if tail is None:
            queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
            tail = queue.stream(stream_id).output_tail(0)
            self._tails[cache_key] = tail
        with self._tail_locks.setdefault(cache_key, threading.Lock()):
            data = tail.read_new(limit)
            rebuilt = tail.rebuilt
        return data, tail.offset, rebuilt

    def read_remote_stream_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        queue = relay.RelayQueue.for_peer(self.channel_root(peer_id), peer_id)
        return queue.stream(stream_id).read_meta()

    # --- 从机：本机正在为对端提供的会话 ---
    def served_sessions(self) -> list[dict]:
        """本机作为从机正在对外提供的终端会话。

        与 ``remote_devices`` 相反的方向：那是「别人共享给我的」，这是「我正在被
        谁使用」。两端状态里都要有，否则坐在被控机器前的人不知道有人在操作它。
        """
        if self._sessions is None:
            return []
        try:
            return self._sessions.snapshot()
        except Exception:
            return []

    # --- 主控：远端终端会话 ---
    def read_remote_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        target = relay.check_peer_id(peer_id, '目标从机标识')
        queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
        return queue.stream(stream_id).read_meta(report_missing=True)

    def attach_remote_session(
        self,
        peer_id: str,
        stream_id: str,
        *,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[str], None],
        on_event: Optional[Callable] = None,
    ) -> relay_session.RemoteSessionReader:
        """开始把远端会话的输出泵到本端。重复调用会替换旧读取线程。"""
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if not relay_session.valid_session_id(stream_id):
            raise ValueError(f'会话标识非法：{stream_id!r}')
        self.detach_remote_session(target, stream_id)
        reader = relay_session.RemoteSessionReader(
            target, stream_id,
            read_stream=lambda: self.read_remote_stream(target, stream_id),
            read_meta=lambda: self.read_remote_meta(target, stream_id),
            on_output=on_output,
            on_exit=on_exit,
            on_event=on_event or self._emit,
        )
        self._readers[(target, stream_id)] = reader
        reader.start()
        return reader

    def detach_remote_session(self, peer_id: str, stream_id: str) -> bool:
        """停止本端读取线程（不发关闭请求）。"""
        reader = self._readers.pop((str(peer_id), str(stream_id)), None)
        if reader is None:
            return False
        reader.stop()
        return True

    def open_remote_session(
        self,
        peer_id: str,
        device_key: str,
        stream_id: str,
        *,
        cols: int = 120,
        rows: int = 40,
        on_output: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
        timeout: float = DEFAULT_ADB_TIMEOUT_S,
    ) -> relay.Result:
        """请从机为某台远端设备开一条终端会话，并在本端开始读它的输出。"""
        if not relay_session.valid_session_id(stream_id):
            return relay.Result(
                reqId='', status=relay.STATUS_REJECTED,
                error=f'会话标识非法：{stream_id!r}', finishedAt=relay.now_iso(),
            )
        device = {'key': str(device_key), 'serial': str(device_key).split(':', 1)[0]}
        result = self.submit(
            peer_id, relay.KIND_OPEN_SESSION, device=device,
            payload={'streamId': stream_id, 'cols': int(cols), 'rows': int(rows)},
            timeout=timeout,
        )
        if result.ok and on_output is not None:
            self.attach_remote_session(
                peer_id, stream_id, on_output=on_output,
                on_exit=on_exit or (lambda _reason: None),
            )
        return result

    def send_remote_input(self, peer_id: str, stream_id: str, data: bytes) -> bool:
        """把按键写进远端会话的输入流。数据面追加，不走控制面请求。"""
        if not data:
            return True
        target = relay.check_peer_id(peer_id, '目标从机标识')
        if self._pairing_blocks(target):
            return False
        queue = relay.RelayQueue.for_peer(self.channel_root(target), target)
        # 每条输入流独立串行，不能与心跳、设备枚举及控制回执共用网络 IO 锁。
        with self._input_locks.setdefault((target, stream_id), threading.Lock()):
            return queue.stream(stream_id).input_writer().append(bytes(data))

    def resize_remote_session(
        self, peer_id: str, stream_id: str, cols: int, rows: int, timeout: float = 5.0,
    ) -> relay.Result:
        return self.submit(
            peer_id, relay.KIND_SESSION_RESIZE,
            payload={'streamId': stream_id, 'cols': int(cols), 'rows': int(rows)},
            timeout=timeout,
        )

    def close_remote_session(
        self, peer_id: str, stream_id: str, *, timeout: float = 5.0, reason: str = '',
    ) -> relay.Result:
        self.detach_remote_session(peer_id, stream_id)
        return self.submit(
            peer_id, relay.KIND_CLOSE_SESSION,
            payload={'streamId': stream_id, 'reason': reason}, timeout=timeout,
        )

    def _safe_read_meta(self, peer_id: str, stream_id: str) -> Optional[dict]:
        try:
            return self.read_remote_meta(peer_id, stream_id)
        except Exception:
            return None
