"""RelayNode 的入站请求服务（``NodeRequestsMixin``）：从机侧裁决与执行。

轮询 ``requests/<自己>/``，逐条过 ``relay.check_command`` 与「已确认主控」名单，
把结果写回 ``results/``。**安全边界就在这一层**：主控配置再宽松，也必须过这里。

``LogStreamReader`` 也放在这里 —— 它是日志请求的执行体（长驻 adb 命令 + 写流），
只在 ``_serve_start_log`` 里被创建，和这一层的其余部分同生共死。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import relay
from . import relay_adb
from . import relay_journal
from . import relay_session
from . import relay_session_clock
from . import relay_transfer
from .relay_adb import CREATE_NO_WINDOW, DEFAULT_ADB_TIMEOUT_S
from .relay_node_constants import STREAM_ID_RE


# ----------------------------------------------------------------------
# 从机侧：把设备端长驻命令的输出持续写进分区流
# ----------------------------------------------------------------------
class LogStreamReader:
    """一条远端日志流：跑一条长驻 adb 命令，把输出追加到 ``streams/<自己>/<id>.out``。

    单写者：只有本机（从机）写这条流，主控只读，因此不需要任何锁。
    """

    def __init__(
        self,
        queue: relay.RelayQueue,
        stream_id: str,
        device: dict,
        argv,
        adb_path: str,
        *,
        on_event: Optional[Callable] = None,
    ):
        self.queue = queue
        self.stream_id = stream_id
        self.device = dict(device or {})
        self.argv = [str(part) for part in (argv or [])]
        self.adb_path = adb_path
        self.on_event = on_event
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self.bytes_written = 0
        self.error = ''

    # --- 元信息（写在 ``streams/<自己>/<id>.meta.json``，主控可读）---
    def _meta(self, status: str) -> dict:
        return {
            'kind': 'log',
            'status': status,
            'streamId': self.stream_id,
            'deviceKey': self.device.get('key', ''),
            'argv': list(self.argv),
            'bytesWritten': self.bytes_written,
            'startedAt': self._started_at,
            'updatedAt': relay.now_iso(),
            'error': self.error,
        }

    def start(self) -> bool:
        tokens = [str(part) for part in self.argv]
        command = [self.adb_path, *relay_adb.device_selector_args(self.device), *tokens]
        try:
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.error = f'无法启动 adb：{exc}'
            return False
        self._started_at = relay.now_iso()
        self.queue.stream(self.stream_id).write_meta(self._meta('running'))
        self._thread = threading.Thread(
            target=self._pump, name=f'relay-log-{self.stream_id}', daemon=True,
        )
        self._thread.start()
        return True

    def _pump(self) -> None:
        writer = self.queue.stream(self.stream_id).output_writer()
        stdout = self._proc.stdout if self._proc else None
        try:
            while not self._stop_flag.is_set() and stdout is not None:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                if writer.append(chunk):
                    self.bytes_written += len(chunk)
        except (OSError, ValueError) as exc:
            self.error = f'流写入失败：{exc}'
        finally:
            self.queue.stream(self.stream_id).write_meta(
                self._meta('stopped' if self._stop_flag.is_set() else 'ended')
            )

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_flag.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class NodeRequestsMixin:
    """入站请求的裁决与执行。依赖 ``RelayNode.__init__`` 建立的属性。"""

    @property
    def session_clock(self) -> relay_session_clock.SessionClock:
        """会话级时钟估计（按对端各一份）。懒建，避免改动 ``RelayNode.__init__``。"""
        clock = getattr(self, '_session_clock', None)
        if clock is None:
            clock = self._session_clock = relay_session_clock.SessionClock()
        return clock

    def _expiry_exempt(self, request) -> bool:
        """建链/撤链的探针请求不判时效（见 ``CLOCK_EXEMPT_KINDS``）。"""
        return request.kind in relay.CLOCK_EXEMPT_KINDS

    def _request_clock_offset_ms(self, request) -> float:
        """本条请求的来源时钟相对本机的偏差（毫秒，对端 − 本机）。

        优先用会话级估计（``SessionClock``）：它由 ``check-connection`` 往返的
        四时间戳显式测出，取 RTT 最小的样本，并用本机单调钟推进、对"时间倒退"
        方向限速。估计尚未建立或已老化时，退回按心跳 mtime 现算一份，并顺手喂进
        估计里 —— 载体不是共享目录、心跳缺失时两者都量不出来，返回 0 退化成
        各机自比的老行为。
        """
        peer = str(request.peer or '')
        clock = self.session_clock
        cached = clock.clock(peer)
        # 只信握手样本：它有往返 RTT 证据，并且同时证明了「链路可执行」。mtime
        # 推导值只用于展示 —— 共享目录一旦不可用它就停更，把它缓存下来当判据，
        # 会在载体断开后继续用一个陈旧偏差。
        if (cached is not None and cached.samples > 0
                and cached.source == relay_session_clock.SOURCE_HANDSHAKE
                and cached.synced()):
            return cached.offset_ms
        with self._io_lock:
            own = self._peers.get(self.peer_id)
            beat = self._peers.get(peer)
        if own is None or beat is None or own.fileMtimeMs <= 0 or beat.fileMtimeMs <= 0:
            return 0.0
        offset = own.server_skew_ms - beat.server_skew_ms
        # 节流：同一秒内重复观测只会把"可倒退预算"的时间基准刷小，没有收益。
        existing = clock.clock(peer)
        if existing is None or existing.age_ms() >= 1000.0:
            clock.observe_mtime(peer, offset)
        return offset

    def _stamp_received(self, request) -> None:
        """记下「本机读到这条请求」的时刻，回执里作为 T2 回传给主控对时。

        必须在**读到**时记，不能等执行完再取 ``now``：那样 T2≈T3，对时公式里
        「处理耗时对消」这一项就失效了，误差会随执行时长线性放大。
        """
        stamps = getattr(self, '_recv_stamps', None)
        if stamps is None:
            stamps = self._recv_stamps = {}
        if len(stamps) > 512:
            stamps.clear()
        stamps[request.reqId] = relay.now_iso()

    # --- 从机：请求处理 ---
    def _process_requests(self) -> None:
        if self.control_journal and not isinstance(self.queue, relay_journal.JournalQueue):
            self.queue = self._journal_queue(self.peer_id)
        if isinstance(self.queue, relay_journal.JournalQueue):
            self.queue.journal_sources = [beat.nodeId for beat in self._peers.values() if beat.nodeId]
        pending = self.queue.pending_requests(source_peer_ids=list(self._peers))
        if isinstance(self.queue, relay_journal.JournalQueue):
            notices = set(self._journal_runtime.notices.values())
            for notice in notices - self._journal_notices:
                self._emit('error', notice)
            self._journal_notices = notices
        for path, request in pending:
            if request.reqId in self._inflight:
                continue
            # 幂等：结果已存在说明处理过（或处理到一半崩了），不重复执行。
            if self.queue.has_result(request.reqId):
                self.queue.archive_file(path)
                continue
            offset = self._request_clock_offset_ms(request)
            self._stamp_received(request)
            if not self._expiry_exempt(request) and request.is_expired(offset_ms=offset):
                # 过期即作废（fail-closed）：防止从机离线期间堆积的命令在恢复后
                # 批量执行 —— 比如一条早已过时的 reboot。
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_EXPIRED,
                        # 折算后仍过期时把钟差说出来，否则用户只能看到无法行动的"已过期"。
                        error='请求已过期，未执行' + (f'（两端时钟相差 {abs(offset) / 1000:.0f} 秒，请对时）'
                                                 if abs(offset) >= 5000 else ''),
                        agentId=self.peer_id,
                        finishedAt=relay.now_iso(),
                    ),
                    path,
                )
                self._emit('reject', f'丢弃过期请求 {request.reqId}（{request.kind}）')
                continue
            if isinstance(self.queue, relay_journal.JournalQueue) and not self.queue.claim_request(path, request):
                continue
            self._inflight.add(request.reqId)
            executor = self._terminal_pool if request.kind in (relay.KIND_OPEN_SESSION, relay.KIND_WINDOWS, relay.KIND_EXTENSION_TOOL) else self._pool
            executor.submit(self._serve_request, path, request)

    def _serve_request(self, path: str, request: relay.Request) -> None:
        try:
            reason = self._authorize(request)
            if reason:
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_REJECTED, error=reason,
                        agentId=self.peer_id, finishedAt=relay.now_iso(),
                    ),
                    path,
                )
                self._emit('reject', f'拒绝 {request.kind} 请求（来源 {request.peer}）：{reason}')
                return
            if request.kind == relay.KIND_CHECK_CONNECTION:
                self._mark_connection_verified(request.peer)
                self._write_result(relay.Result(
                    reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                    extra={'nodeId': self.node_id}, finishedAt=relay.now_iso()), path)
            elif request.kind == relay.KIND_CLOSE_CONNECTION:
                self.unpair_peer(self._node_id_for(request.peer))
                self._write_result(relay.Result(
                    reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                    finishedAt=relay.now_iso()), path)
            elif request.kind == relay_transfer.KIND_TRANSFER:
                self._serve_transfer(path, request)
            elif request.kind == relay_transfer.KIND_CLEANUP:
                directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer,
                                                        request.payload.get('transferId'))
                # 只删除从机自己写的下载包；上传包由主控清理。
                package = directory / 'download.zip'
                package.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
                self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
                                               agentId=self.peer_id), path)
            elif request.kind == relay.KIND_EXEC:
                self._serve_exec(path, request)
            elif request.kind == relay.KIND_WINDOWS:
                self._serve_windows(path, request)
            elif request.kind == relay.KIND_EXTENSION_TOOL:
                self._serve_extension_tool(path, request)
            elif request.kind == relay.KIND_WINDOWS_TRANSFER:
                self._serve_windows_transfer(path, request)
            elif request.kind == relay.KIND_START_LOG:
                self._serve_start_log(path, request)
            elif request.kind == relay.KIND_STOP_LOG:
                self._serve_stop_log(path, request)
            elif request.kind == relay.KIND_OPEN_SESSION:
                self._serve_open_session(path, request)
            elif request.kind == relay.KIND_SESSION_TAKE:
                ok, message = self._sessions.take_control(
                    request.payload.get('streamId', ''), request.payload.get('generation', '')) if self._sessions else (False, '终端服务未启用')
                self._write_result(relay.Result(reqId=request.reqId,
                    status=relay.STATUS_OK if ok else relay.STATUS_REJECTED,
                    error='' if ok else message, stdout=message, agentId=self.peer_id), path)
            elif request.kind == relay.KIND_SESSION_RESIZE:
                self._serve_session_resize(path, request)
            elif request.kind == relay.KIND_CLOSE_SESSION:
                self._serve_close_session(path, request)
            else:
                self._write_result(
                    relay.Result(
                        reqId=request.reqId, status=relay.STATUS_REJECTED,
                        error=f'本版本未实现该请求类型：{request.kind}', agentId=self.peer_id,
                        finishedAt=relay.now_iso(),
                    ),
                    path,
                )
        except Exception as exc:
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=f'从机处理异常：{type(exc).__name__}: {exc}', agentId=self.peer_id,
                    finishedAt=relay.now_iso(),
                ),
                path,
            )
        finally:
            self._inflight.discard(request.reqId)
            if (self._stop_flag.is_set() and not self._inflight
                    and self._journal_runtime is not None and not self.is_running()):
                self._journal_runtime.close()

    def _windows_owner(self, request, action):
        """电脑权限独立于 ADB；只接受当前已确认实例及连接代次。"""
        payload = request.payload or {}
        if self._stop_flag.is_set():
            raise PermissionError('中转节点已停止，拒绝过时的电脑请求')
        source = self._node_id_for(request.peer)
        record = self.pairing.get(source)
        if (not source or not record or not record.is_paired
                or payload.get('sourceNodeId') != source
                or payload.get('targetNodeId') != self.node_id
                or payload.get('connectionId') != record.connectionId):
            raise PermissionError('Windows 请求的实例或连接确认代次无效')
        if action != 'capabilities' and not self.allow_windows:
            raise PermissionError('从机未允许 Windows PowerShell，请在从机「窗口 → PowerShell」中启用')
        if self.windows_service is None:
            from windows_executor import WindowsService
            with self._io_lock:
                if self.windows_service is None:
                    from pathlib import Path
                    self.windows_service = WindowsService(self.windows_directory or
                        Path(self._default_trust_path()).parent / ('windows-jobs-'+self.node_id))
                    self._owns_windows_service = True
        return source + ':' + record.connectionId if action.startswith('session_') else source

    def _extension_owner(self, request, *, check_expiry=True):
        """控制请求与持续流共用执行端准入；流不延长原请求TTL。"""
        payload = request.payload or {}
        reason=self._authorize(request);source=self._node_id_for(request.peer);record=self.pairing.get(source)
        if (reason or (check_expiry and not self._expiry_exempt(request)
                       and request.is_expired(offset_ms=self._request_clock_offset_ms(request)))
                or self.role!=relay.ROLE_SLAVE or not record or not record.is_paired
                or payload.get('sourceNodeId')!=source or payload.get('targetNodeId')!=self.node_id
                or not record.connectionId or payload.get('connectionId')!=record.connectionId):
            raise PermissionError(reason or '扩展工具请求的实例、连接代次或有效期无效')
        return source+':'+record.connectionId if payload.get('tool')=='serial' else source

    def _serve_extension_tool(self, path, request):
        """固定扩展接口；串口打开时协商持续流，不逐字符投递请求。"""
        payload=request.payload or {}
        authorize=lambda:self._extension_owner(request)
        if self.extension_tools is None:
            raise RuntimeError('从机未启用扩展工具接口，请升级从机')
        data = self.extension_tools.call(payload.get('tool'), payload.get('action'), payload.get('args'),
                                         remote=True, authorize=authorize, caller=payload.get('caller', 'agent'))
        if (payload.get('tool')=='serial' and payload.get('action')=='open'
                and payload.get('serialStreamProtocol')==1 and data.get('data',{}).get('state')=='running'):
            from serial_stream import SlaveSerialStream
            opened=data['data'];identifier=opened['sessionId']
            with self._windows_stream_lock:
                self._serial_streams={key:stream for key,stream in self._serial_streams.items() if stream.thread.is_alive()}
                stream=self._serial_streams.get(identifier)
                if stream is None or not stream.thread.is_alive():
                    self._serial_streams[identifier]=SlaveSerialStream(self,request,opened)
            opened.update(serialStreamProtocol=1,serialWritePacingProtocol=1,streamId='serial-'+identifier)
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
            agentId=self.peer_id, finishedAt=relay.now_iso(), extra={'data': data}), path)

    def _serve_windows(self, path, request):
        payload = request.payload or {}
        action = str(payload.get('action') or '')
        owner = self._windows_owner(request, action)
        data = self.windows_service.call(action, dict(payload.get('args') or {}), owner)
        if action == 'session_open' and payload.get('streamProtocol') == 1:
            from windows_stream import SlaveStream
            identifier = data['sessionId']
            try:
                with self._windows_stream_lock:
                    self._windows_owner(request, 'session_write')
                    self._windows_streams = {key: stream for key, stream in self._windows_streams.items()
                                            if stream.thread.is_alive()}
                    self._windows_streams[identifier] = SlaveStream(self, request, identifier, owner)
                data.update(streamProtocol=1, streamId='win-' + identifier)
            except Exception:
                self.windows_service.session_action('session_close', {'sessionId': identifier}, owner)
                raise
        elif action == 'session_close':
            with self._windows_stream_lock:
                stream = self._windows_streams.pop((payload.get('args') or {}).get('sessionId'), None)
            if stream is not None:
                stream.stop()
        if action == 'capabilities':
            data['allowed'] = self.allow_windows
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
            agentId=self.peer_id, finishedAt=relay.now_iso(), extra={'data': data}), path)

    def _serve_windows_transfer(self, path, request):
        """复用已验证包协议，目标是电脑暂存目录，不执行 ADB。"""
        self._windows_owner(request, 'files')
        payload = request.payload or {}
        operation = payload.get('operation')
        relative_paths = payload.get('remotePaths')
        if operation not in ('push', 'pull') or not isinstance(relative_paths, list) or not relative_paths:
            raise ValueError('电脑文件传输参数无效')
        directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer, payload.get('transferId'))
        with tempfile.TemporaryDirectory(prefix='DevMux-windows-transfer-') as work:
            if operation == 'push':
                if len(relative_paths) != 1:
                    raise ValueError('上传只能指定一个目标目录')
                target = self.windows_service.file_path(relative_paths[0], create=True)
                paths = relay_transfer.receive_package(directory / 'upload.zip', work, payload.get('package') or {})
                # 大包读取之后再次检查撤销/连接换代，不能执行过时的落盘动作。
                reason = self._authorize(request)
                if reason:
                    raise PermissionError(reason)
                self._windows_owner(request, 'files')
                for source in paths:
                    existing = self.windows_service.file_path(str(Path(relative_paths[0]) / source.name))
                    if existing.exists():
                        for item in relay_transfer.tree(existing):
                            pass  # 校验已有目标树，防止 copytree 追随旧链接。
                relay_transfer.save_download(paths, target)
                extra = {'destination': str(target)}
            else:
                paths = [self.windows_service.file_path(value) for value in relative_paths]
                extra = {'package': relay_transfer.publish_upload(paths, directory / 'download.zip')}
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK, exitCode=0,
            stdout='电脑文件传输完成', extra=extra, agentId=self.peer_id, finishedAt=relay.now_iso()), path)

    def _authorize(self, request: relay.Request) -> str:
        """从机侧准入：返回拒绝原因，空字符串表示放行。"""
        if self._stop_flag.is_set():
            return '中转节点已停止，拒绝执行排队请求'
        if not request.peer:
            return '请求缺少来源标识'
        if not relay.valid_peer_id(request.peer):
            return f'来源标识非法：{request.peer!r}'
        if request.peer == self.peer_id:
            return '拒绝来源标识与本机相同的请求'
        if self.passive_slave:
            reason = self._accept_master_connection(request)
            if reason:
                return reason
        # 准入门槛排在仲裁之前：未配对的实例连对端都还不是，让它进仲裁等于让未授权
        # 的实例投票（它的角色字段还可能是「主控」，会被认成当前主控）。
        block = self._pairing_blocks(request.peer)
        if block:
            return block
        arbitration = relay.arbitrate_roles(
            self.peer_id, self.role, self._admissible_peers(),
            interval_s=self.heartbeat_interval_s,
            accepted_master_ids=self.accepted_master_ids(),
            skew_ms=relay.observer_clock_skew_ms(self._peers, self.peer_id),
        )
        if arbitration.conflict:
            return f'{arbitration.reason}；已暂停透传'
        if arbitration.master and request.peer != arbitration.master:
            return f'来源 {request.peer} 不是当前主控端（当前主控：{arbitration.master}）'
        trusted, reason = self.trust.check(request.peer, self._peer_fingerprint(request.peer))
        if not trusted:
            return f'主控端 {request.peer} 未确认：{reason}'
        return ''

    def _accept_master_connection(self, request: relay.Request) -> str:
        """主控确认后，只有无设备操作的验证请求能够建立从机准入记录。"""
        with self._io_lock:
            beat = self._peers.get(request.peer)
            skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
            if (beat is None or beat.role != relay.ROLE_MASTER or not beat.nodeId
                    or not beat.fingerprint or beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)):
                return '尚未发现在线且身份完整的主控，请等待主控重新验证'
            if request.peer in self._route_conflicts:
                return '主控路由标识重复，不能建立连接'
            payload = request.payload or {}
            connection_id = str(payload.get('connectionId') or '')
            if (payload.get('sourceNodeId') != beat.nodeId
                    or payload.get('targetNodeId') != self.node_id or not connection_id):
                return '连接实例或确认代次不匹配，请在主控端重新确认；旧版主控需升级'
            source_scope = beat.connectionScope
            if self._connection_scope and (not re.fullmatch(r'[0-9a-f]{16}', source_scope)
                    or not connection_id.startswith(source_scope + '.')):
                return '连接代次属于其他共享池，拒绝旧链接；请在当前池重新确认连接'
            record = self.pairing.get(beat.nodeId)
            if record and connection_id in record.retiredConnectionIds:
                return '旧连接确认已经失效，不能恢复设备通讯'
            if record and record.connectionId == connection_id:
                if not record.is_paired:
                    return '连接已解除，旧确认不能自动恢复，请在主控端重新确认连接'
                if record.fingerprint != beat.fingerprint:
                    return '主控身份发生变化，请在主控端重新确认连接'
                return ''
            if request.kind != relay.KIND_CHECK_CONNECTION:
                return '尚未验证主控连接，不能执行设备操作'
            candidates = {**self._admissible_peers(), request.peer: beat}
            arbitration = relay.arbitrate_roles(
                self.peer_id, self.role, candidates, interval_s=self.heartbeat_interval_s,
                accepted_master_ids=self.accepted_master_ids(), skew_ms=skew)
            if arbitration.conflict:
                return f'{arbitration.reason}；请先解除原主控连接'
            # 代次变化必须关闭旧 PTY，防止旧输入流继续操作设备。
            if record and self._sessions is not None:
                self._sessions.stop_all()
            if record and self.windows_service is not None:
                self.windows_service.close_sessions(beat.nodeId)
            if record and self.extension_tools is not None:
                self.extension_tools.close_owner(beat.nodeId)
            self.pair_peer(beat.nodeId, peer_id=request.peer, connection_id=connection_id)
            self._emit('trust', f'已自动接受主控连接：{self._peer_display_label(request.peer)}')
            return ''

    def _admissible_peers(self) -> dict:
        """参与角色仲裁的对端集合：未配对的实例一律排除。

        ``self._peers`` 是**总线上所有**心跳（含未配对），读侧过滤只在
        ``evaluate_health`` 里做，所以仲裁要单独过滤一次 —— 否则未配对实例的
        「主控」角色会挤进仲裁结果，把正常的两端判成角色冲突（而冲突是 fail-closed
        的，会直接停摆透传）。
        """
        return {
            peer_id: beat for peer_id, beat in self._peers.items()
            if not self._pairing_blocks(peer_id)
        }

    def _peer_fingerprint(self, peer_id: str) -> str:
        beat = self._peers.get(peer_id)
        return beat.fingerprint if beat else ''

    def _match_device(self, want_key: str, want_serial: str) -> Optional[dict]:
        candidates = self._managed_devices()
        for item in candidates:
            if want_key and item.get('key') == want_key:
                return item
        for item in candidates:
            if want_serial and item.get('serial') == want_serial:
                return item
        return None

    def _resolve_request_device(self, request: relay.Request) -> Optional[dict]:
        """把请求里的设备描述解析成本机设备记录。

        解析不到时**强制再刷一次设备列表**：``adb devices`` 在 adb server 忙碌
        时会瞬时返回空列表（实测首次调用即可复现），若只用 1~2 秒前的轮询快照
        判断，就会把「设备明明在」的请求误判成「没有匹配的设备」。请求是重要
        事件，值得为它多打一次 adb。
        """
        device = dict(request.device or {})
        want_key = str(device.get('key') or '')
        want_serial = str(device.get('serial') or '')
        resolved = self._match_device(want_key, want_serial)
        if resolved is not None:
            return resolved
        self._refresh_devices()
        return self._match_device(want_key, want_serial)

    def _describe_devices(self) -> str:
        """诊断用：当前本机设备列表与最近一次枚举错误。"""
        keys = [device.get('key', '') for device in self._managed_devices()]
        detail = f'本机当前设备：{keys or "（空）"}'
        if self._device_error:
            detail += f'；最近一次枚举报错：{self._device_error}'
        return detail

    def _serve_transfer(self, path, request):
        """文件请求仍经从机准入和命令白名单，不接受主控指定本机路径。"""
        payload = request.payload
        operation = payload.get('operation')
        if operation not in ('push', 'pull'):
            self._fail_request(path, request, '文件操作非法', rejected=True)
            return
        allowed, reason = relay.check_command([operation], allow_shell=self.allow_shell,
                                             allowed_subcommands=self.allowed_subcommands)
        if not allowed:
            self._fail_request(path, request, f'文件传输被策略拒绝：{reason}；请在从机命令白名单加入 {operation}', rejected=True)
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(path, request, '本机没有匹配的设备')
            return
        remote_paths = payload.get('remotePaths')
        if (not isinstance(remote_paths, list) or not remote_paths
                or any(not isinstance(p, str) or not p.startswith('/') or '\x00' in p
                       for p in remote_paths) or (operation == 'push' and len(remote_paths) != 1)):
            self._fail_request(path, request, '设备文件路径必须是绝对路径', rejected=True)
            return
        directory = relay_transfer.transfer_dir(self.root, self.peer_id, request.peer,
                                                payload.get('transferId'))
        directory.mkdir(parents=True, exist_ok=True)
        output = []
        deadline = time.monotonic() + relay_transfer.TIMEOUT

        def run(argv, *, raw=False):
            reason = self._authorize(request)
            if reason:
                raise RuntimeError(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('文件传输执行超时')
            # 固定 shell 辅助命令用独立 argv，设备路径不能变成 shell 语法。
            proc = subprocess.run([self.adb_path, *relay_adb.device_selector_args(device), *argv],
                                  capture_output=True, text=not raw, encoding=None if raw else 'utf-8',
                                  errors=None if raw else 'replace',
                                  timeout=remaining, creationflags=CREATE_NO_WINDOW)
            detail = ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', 'replace').strip() if raw else ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode:
                raise RuntimeError(detail or f'adb 退出码 {proc.returncode}')
            if detail and not raw:
                output.append(detail)
            return proc

        with tempfile.TemporaryDirectory(prefix='DevMux-transfer-') as work:
            work = Path(work)
            if operation == 'push':
                paths = relay_transfer.receive_package(directory / 'upload.zip', work, payload.get('package') or {})
                run(['push', *map(str, paths), remote_paths[0]])
                if payload.get('makeExecutable'):
                    import shlex
                    scripts = [p for p in paths if p.is_file() and p.suffix.lower() == '.sh']
                    if scripts:
                        remote = remote_paths[0]
                        check = subprocess.run([self.adb_path, *relay_adb.device_selector_args(device), 'shell',
                                                'test -d ' + shlex.quote(remote)], capture_output=True,
                                               timeout=15, creationflags=CREATE_NO_WINDOW)
                        for script in scripts:
                            target = remote.rstrip('/') + '/' + script.name if check.returncode == 0 else remote
                            run(['shell', 'chmod +x ' + shlex.quote(target)])
                        output.append('已添加脚本执行权限')
                run(['shell', 'sync'])
                output.append('上传成功；设备文件已同步')
                extra = {}
            else:
                paths = []
                for index, remote in enumerate(remote_paths):
                    target = work / str(index)
                    target.mkdir()
                    from adb_pull import pull_to_temp
                    paths.append(pull_to_temp(run, remote, target))
                extra = {'package': relay_transfer.publish_upload(paths, directory / 'download.zip')}
        self._write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK,
                                       exitCode=0, stdout='\n'.join(output), extra=extra,
                                       agentId=self.peer_id, finishedAt=relay.now_iso()), path)

    def _serve_exec(self, path: str, request: relay.Request) -> None:
        argv = request.payload.get('argv')
        allowed, reason = relay.check_command(
            argv, allow_shell=self.allow_shell, allowed_subcommands=self.allowed_subcommands,
        )
        if not allowed:
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_REJECTED,
                    error=f'命令被策略拒绝：{reason}', agentId=self.peer_id,
                    finishedAt=relay.now_iso(),
                ),
                path,
            )
            self._emit('reject', f'命令被策略拒绝：{reason}（argv={argv}）')
            return
        device = self._resolve_request_device(request)
        if device is None:
            detail = f'本机没有匹配的设备：{request.device}（{self._describe_devices()}）'
            self._write_result(
                relay.Result(
                    reqId=request.reqId, status=relay.STATUS_ERROR,
                    error=detail, agentId=self.peer_id, finishedAt=relay.now_iso(),
                ),
                path,
            )
            self._emit('reject', detail)
            return

        started_at = relay.now_iso()
        timeout = float(request.payload.get('timeout') or DEFAULT_ADB_TIMEOUT_S)
        self._emit('command', f'执行 {argv} @ {device.get("key")}')
        outcome = relay_adb.execute(
            self.adb_path, device, argv, timeout=timeout, max_bytes=self.max_output_bytes,
        )
        if outcome.timeout or outcome.error:
            status = relay.STATUS_ERROR
        else:
            status = relay.STATUS_OK
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=status,
                exitCode=outcome.exitCode, stdout=outcome.stdout, stderr=outcome.stderr,
                truncated=outcome.truncated, error=outcome.error, agentId=self.peer_id,
                startedAt=started_at, finishedAt=relay.now_iso(), durationMs=outcome.elapsedMs,
                extra={
                    'deviceKey': device.get('key', ''),
                    'exitCodeAvailable': outcome.exitCodeAvailable,
                },
            ),
            path,
        )
        self._emit('result', f'{argv} @ {device.get("key")} 执行完成，退出码 '
                             f'{outcome.exitCode if outcome.exitCode is not None else "不可得"}，'
                             f'耗时 {outcome.elapsedMs}ms')

    def _serve_start_log(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if not STREAM_ID_RE.match(stream_id):
            self._fail_request(path, request, f'流标识非法：{stream_id!r}')
            return
        if stream_id in self._streams:
            self._fail_request(path, request, f'流 {stream_id} 已存在')
            return
        argv = request.payload.get('argv') or ['logcat']
        allowed, reason = relay.check_command(
            argv, allow_shell=self.allow_shell, allowed_subcommands=self.allowed_subcommands,
        )
        if not allowed:
            self._fail_request(path, request, f'命令被策略拒绝：{reason}', rejected=True)
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(path, request, f'本机没有匹配的设备：{request.device}'
                                             f'（{self._describe_devices()}）')
            return

        stream = LogStreamReader(
            self.queue, stream_id, device, argv, self.adb_path, on_event=self._emit,
        )
        if not stream.start():
            self._fail_request(path, request, stream.error or '日志流启动失败')
            return
        self._streams[stream_id] = stream
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'日志流已启动：{stream_id}（{argv}）',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'deviceKey': device.get('key', '')},
            ),
            path,
        )
        self._emit('stream', f'日志流已启动 {stream_id}：{argv} @ {device.get("key")}，'
                             f'写入 streams/{self.peer_id}/{stream_id}.out')

    def _serve_stop_log(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            self._fail_request(path, request, f'没有正在运行的流：{stream_id}')
            return
        stream.stop()
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'日志流已停止：{stream_id}，累计 {stream.bytes_written} 字节',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'bytesWritten': stream.bytes_written},
            ),
            path,
        )
        self._emit('stream', f'日志流已停止 {stream_id}，累计 {stream.bytes_written} 字节')

    # --- 从机：远端终端会话 ---
    def _serve_open_session(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if not relay_session.valid_session_id(stream_id):
            self._fail_request(path, request, f'会话标识非法：{stream_id!r}')
            return
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端，无法接受远端终端会话')
            return
        device = self._resolve_request_device(request)
        if device is None:
            self._fail_request(
                path, request,
                f'本机没有匹配的设备：{request.device}（{self._describe_devices()}）',
            )
            return
        cols = int(request.payload.get('cols') or 120)
        rows = int(request.payload.get('rows') or 40)
        started = time.monotonic()
        ok, reason = self._sessions.open(stream_id, device, cols=cols, rows=rows,
                                         owner=self._session_owner(request))
        prepare_ms = round((time.monotonic() - started) * 1000)
        self._emit('info', f'终端准备耗时 {prepare_ms}ms（reqId {request.reqId}）')
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'远端终端会话已开启：{stream_id}',
                finishedAt=relay.now_iso(),
                extra={'streamId': stream_id, 'deviceKey': device.get('key', ''), 'cols': cols, 'rows': rows,
                       'prepareMs': prepare_ms},
            ),
            path,
        )

    def _serve_session_resize(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端')
            return
        ok, reason = self._sessions.resize(
            stream_id, int(request.payload.get('cols') or 120), int(request.payload.get('rows') or 40),
        )
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'窗口已调整：{stream_id}', finishedAt=relay.now_iso(),
                extra={'streamId': stream_id},
            ),
            path,
        )

    def _serve_close_session(self, path: str, request: relay.Request) -> None:
        stream_id = str(request.payload.get('streamId') or '')
        if self._sessions is None:
            self._fail_request(path, request, '本机未启用会话后端')
            return
        ok, reason = self._sessions.close(stream_id, str(request.payload.get('reason') or '操作端关闭了会话'),
                                          owner=self._session_owner(request))
        if not ok:
            self._fail_request(path, request, reason)
            return
        self._write_result(
            relay.Result(
                reqId=request.reqId, status=relay.STATUS_OK, agentId=self.peer_id,
                stdout=f'远端终端会话已关闭：{stream_id}', finishedAt=relay.now_iso(),
                extra={'streamId': stream_id},
            ),
            path,
        )

    def _session_owner(self, request):
        # 只使用已通过_authorize验证的实例与连接代次；旧协议未知所有者不自动替换。
        payload = request.payload or {}
        source, generation = payload.get('sourceNodeId'), payload.get('connectionId')
        record = self.pairing.get(self._node_id_for(request.peer))
        if (source and generation and record and record.is_paired
                and source == self._node_id_for(request.peer) and generation == record.connectionId):
            return (source, generation)
        return None

    def _fail_request(self, path: str, request: relay.Request, reason: str, *, rejected: bool = False) -> None:
        self._write_result(
            relay.Result(
                reqId=request.reqId,
                status=relay.STATUS_REJECTED if rejected else relay.STATUS_ERROR,
                error=reason, agentId=self.peer_id, finishedAt=relay.now_iso(),
            ),
            path,
        )
        self._emit('reject' if rejected else 'error', f'请求失败（{request.kind}）：{reason}')

    def _write_result(self, result: relay.Result, request_path: str = '') -> None:
        """写结果并归档对应请求。

        请求由主控写、结果由从机写，所以从机消费掉请求后由从机归档它 —— 主控
        不再需要这份文件；结果文件则由主控读到后自行归档。

        回执里带上 ``recvAt``（本机读到请求的时刻）：主控用它与自己写请求、读到
        回执的时刻配成四时间戳算钟差（见 ``relay_session_clock.handshake_offset_ms``）。
        旧主控忽略这个字段即可，不改变任何既有语义。
        """
        stamps = getattr(self, '_recv_stamps', None)
        stamp = stamps.pop(result.reqId, '') if stamps else ''
        if stamp:
            if result.extra is None:
                result.extra = {}
            result.extra.setdefault('recvAt', stamp)
        self.queue.write_result(result)
        if request_path:
            self.queue.archive_file(request_path)
