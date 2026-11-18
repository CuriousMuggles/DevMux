"""RelayController 的远端会话对齐（B7 拆分，见 docs/internal/流程/优化建议清单.md）。

在线从机的每台设备各保持一条会话；掉线/回来自动恢复，失败退避而不放弃。
共享 ``self``：会话表、执行器与重试计数都由 RelayController.__init__ 建立。

**调整退避与重试策略要改 ``relay_app_common``**：本模块按模块属性读取，
``relay_app`` 里的同名符号只是兼容再导出，改它不会生效。
"""
import time

import relay
from . import relay_app_common
from .relay_app_common import default_session_id


class ControllerSessionMixin:
    # --- 远端设备与会话 ---

    def _refresh_connections(self) -> None:
        node = self.node
        if node is None or self.config.role != relay.ROLE_MASTER or node.health().conflict:
            return
        for peer in node.peers():
            if not (peer.role == relay.ROLE_SLAVE and peer.online and peer.trusted
                    and not peer.needsConfirmation):
                continue
            with node._io_lock:
                due = node.connection_check_due(peer.peerId)
                if not due:
                    # 验证绑定已过期（对端重启/换部署，端点与上次验证不一致）：
                    # 不能按旧节奏（成功档 30s）等下一轮复查，否则设备与会话要
                    # 消失半分钟才恢复。失败/未验证路径（无标记 + 5s 退避）不在
                    # 此列，仍保持密集重试节奏。
                    verified_endpoint = node._connection_verified.get(peer.peerId, ((), 0.0))[0]
                    if verified_endpoint != node._connection_endpoint(peer.peerId):
                        due = True
            if not due:
                continue
            executor = self._connection_executor
            if executor is None:
                continue
            token = (id(node), peer.peerId)
            with self._lock:
                pending = self._connection_futures.get(token)
                if pending is None or pending.done():
                    self._connection_futures[token] = executor.submit(node.verify_connection, peer.peerId)

    def remote_devices(self) -> list[dict]:
        node = self.node
        if node is None:
            return []
        return node.remote_devices()

    def served_sessions(self) -> list[dict]:
        """本机作为从机正在为对端提供的会话（供界面显示「谁在用我的设备」）。"""
        node = self.node
        if node is None:
            return []
        return node.served_sessions()

    def devices(self) -> list[dict]:
        """远端设备（供 server.py 与本机设备合并）。"""
        return self.remote_devices()

    def online_remote_devices(self) -> list[dict]:
        return [device for device in self.remote_devices() if (device.get('relay') or {}).get('online')]

    def _is_confirmed(self, device: dict) -> bool:
        meta = device.get('relay') or {}
        return not meta.get('needsConfirmation')

    def _reconcile_sessions(self) -> None:
        # 首连确认和后台轮询可能同时触发，避免为同一设备并发开多个 PTY。
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            self._reconcile_sessions_once()
        finally:
            self._reconcile_lock.release()

    def _queue_session_close(self, node, key, binding, reason):
        """保留待回收身份；关闭拒绝或超时不能被当作远端资源已释放。"""
        stream_id = binding.get('streamId')
        if not stream_id:
            return None
        token = (id(node), binding['peerId'], stream_id)
        with self._lock:
            self._retired_sessions.setdefault(token, dict(
                node=node, key=key, binding=binding, streamId=stream_id,
                reason=reason, nextAttempt=0.0, closing=False))
        node.detach_remote_session(binding['peerId'], stream_id)
        return token

    def _close_retired_session(self, token):
        with self._lock:
            item = self._retired_sessions.get(token)
            if item is None or item['binding'].get('opening'):
                return False
        ok = False
        node = item['node']
        try:
            # 对端已离线时不再下发关闭请求：对端进程不在，请求只会被本地拦成
            # 「不下发请求给离线从机」，每轮重试再推一条可见提醒 —— 长时间离线
            # 就会刷屏。保留待回收记录，心跳恢复后照常回收。
            offline_check = getattr(node, '_peer_offline_reason', None)
            offline = offline_check(item['binding']['peerId']) if callable(offline_check) else ''
            if offline:
                if not item.get('offlineNoted'):
                    item['offlineNoted'] = True
                    self.log('RELAY', f'对端离线，暂缓回收旧终端接入（心跳恢复后继续）：{item["streamId"]}')
            else:
                item.pop('offlineNoted', None)
                result = node.close_remote_session(
                    item['binding']['peerId'], item['streamId'], reason=item['reason'])
                ok = bool(result and result.ok)
        except Exception as exc:
            self.log('RELAY', f'旧终端接入回收失败，保留重试：{type(exc).__name__}: {exc}')
        finally:
            with self._lock:
                if self._retired_sessions.get(token) is item:
                    if ok:
                        self._retired_sessions.pop(token, None)
                    else:
                        item.update(closing=False, nextAttempt=time.time() + relay_app_common.SESSION_REOPEN_BACKOFF_S)
        return ok

    def _drain_session_closes(self):
        with self._lock:
            pending = [token for token, item in self._retired_sessions.items()
                       if not item['closing'] and not item['binding'].get('opening')
                       and time.time() >= item['nextAttempt']]
            for token in pending:
                self._retired_sessions[token]['closing'] = True
        for token in pending:
            executor = self._maintenance_executor
            if executor is None:
                self._close_retired_session(token)
            else:
                executor.submit(self._close_retired_session, token)
        self._drain_windows_closes()

    def _reconcile_sessions_once(self) -> None:
        """让「在线且已确认的远端设备」与「已开会话」保持一致，并把失败退避。

        对**尚未确认**的对端刻意不尝试开会话：从机一定会拒绝（准入裁决在它那边），
        徒留一串「会话未能开启」的噪声事件，而且用户还以为哪里坏了。等确认之后再开。
        这不是放宽边界 —— ``_authorize`` 始终是唯一裁决点。
        """
        node = self.node
        if node is None:
            return
        self._drain_session_closes()
        pending_peers: set = set()
        wanted: dict = {}
        retained = set()
        for device in self.remote_devices():
            metadata = device.get('relay') or {}
            if not metadata.get('online') and self._is_confirmed(device):
                retained.add(str(device.get('key') or ''))
        for device in self.online_remote_devices():
            key = str(device.get('key') or '')
            if not key:
                continue
            if not self._is_confirmed(device):
                pending_peers.add(str((device.get('relay') or {}).get('peerId') or ''))
                continue
            wanted[key] = device
        for peer_id in sorted(pending_peers):
            token = ('wait-confirm', peer_id)
            with self._lock:
                if token in self._notified_peers:
                    continue
                self._notified_peers.add(token)
            self._push_event({
                'kind': 'info',
                'text': f'对端 {peer_id} 待确认：确认后才会为它的设备开启终端会话',
            })
        # 1 关掉不再需要的会话（对端设备消失或对端被移除）
        with self._lock:
            # 心跳短暂中断保留已有流和输入代次；禁用新操作由在线校验负责。
            # 超过离线保留期、撤销或设备消失仍正常回收。
            stale = [key for key in self._sessions if key not in wanted and key not in retained]
        for key in stale:
            with self._lock:
                binding = self._sessions.pop(key, None)
            if not binding:
                continue
            self._queue_session_close(node, key, binding, '远端设备已不可用')
            self._push_event({'kind': 'session', 'text': f'远端终端会话已收起：{key}'})

        # 2 为缺会话的设备补开
        for key, device in wanted.items():
            binding = self._sessions.get(key)
            metadata = device.get('relay') or {}
            endpoint = (metadata.get('endpointRoot'), metadata.get('startedAt'), metadata.get('nodeId'))
            if binding is not None and binding.get('endpoint') != endpoint:
                # 对端换部署或重启：旧输出线程仍持有旧目录的 tail，必须丢弃旧 streamId。
                with self._lock:
                    self._sessions.pop(key, None)
                self._queue_session_close(node, key, binding, '对端部署或进程已变化')
                binding = None
                self._push_event({'kind': 'session', 'text':
                    f'对端部署或进程已变化，重建终端：{metadata.get("displayLabel") or key}'})
            if binding is not None and (binding.get('opened') or binding.get('opening')):
                continue
            with self._lock:
                if any(item['node'] is node and item['key'] == key
                       for item in self._retired_sessions.values()):
                    continue
            if binding is None:
                binding = {
                    'peerId': device['peerId'], 'localKey': device.get('localKey', ''),
                    'streamId': '', 'opened': False, 'failures': 0, 'nextAttempt': 0.0,
                    'cols': 120, 'rows': 40, 'lastReason': '',
                    'endpoint': endpoint,
                }
                self._sessions[key] = binding
            now = time.time()
            if now < float(binding.get('nextAttempt') or 0.0):
                continue
            binding['streamId'] = binding.get('streamId') or default_session_id()
            binding['opening'] = True
            executor = self._session_executor
            if executor is not None:
                executor.submit(self._open_session_task, key, binding, device, node)
            else:
                self._open_session_task(key, binding, device, node)

    def _open_session_task(self, key, binding, device, node):
        try:
            if self.node is node and self._sessions.get(key) is binding:
                self._open_session(key, binding, device, expected_node=node)
        finally:
            binding['opening'] = False
            if self.node is node:
                self._emit_devices()
                self._emit_status()

    def _open_session(self, remote_key: str, binding: dict, device: dict, expected_node=None) -> None:
        node = expected_node if expected_node is not None else self.node
        if node is None or self.node is not node:
            return
        stream_id = binding.get('streamId') or default_session_id()
        binding['streamId'] = stream_id
        cols = int(binding.get('cols') or 120)
        rows = int(binding.get('rows') or 40)

        def on_output(data: bytes) -> None:
            if (self.node is node and self._sessions.get(remote_key) is binding
                    and binding.get('streamId') == stream_id and self.on_terminal_output):
                try:
                    self.on_terminal_output(remote_key, data)
                except Exception:
                    pass

        def on_exit(reason: str) -> None:
            self._on_session_exit(remote_key, stream_id, reason,
                                  expected_node=node, expected_binding=binding)

        try:
            opened_at = time.monotonic()
            result = node.open_remote_session(
                binding['peerId'], binding.get('localKey') or '', stream_id,
                cols=cols, rows=rows, on_output=on_output, on_exit=on_exit,
            )
        except Exception as exc:
            result = relay.Result(
                reqId='', status=relay.STATUS_ERROR,
                error=f'{type(exc).__name__}: {exc}', finishedAt=relay.now_iso(),
            )
        if (self.node is not node or self._sessions.get(remote_key) is not binding
                or binding.get('streamId') != stream_id):
            if self.node is node:
                self._queue_session_close(node, remote_key, binding, '过期终端开启已取消')
            else:
                node.close_remote_session(binding['peerId'], stream_id, reason='过期终端开启已取消')
            return
        if result is None or not result.ok:
            reason = (result.error if result is not None else '') or '未知原因'
            hint = ''
            if any(marker in reason for marker in relay_app_common.PEER_UNCONFIRMED_MARKERS):
                # 首次连接时两端各自确认，先确认完的一端会先撞几次拒绝。这不是故障，
                # 缺的是有人去对端点一下，所以把提示写成能照做的动作，而不是一句
                # 让人以为哪里坏了的原文。
                hint = '　→ 从机可能仍使用旧版双端确认流程，请升级从机后等待自动重试'
            binding['failures'] = int(binding.get('failures') or 0) + 1
            delay = (relay_app_common.SESSION_RETRY_SLOW_S if binding['failures'] >= relay_app_common.SESSION_MAX_FAILURES
                     else relay_app_common.SESSION_REOPEN_BACKOFF_S * binding['failures'])
            binding['nextAttempt'] = time.time() + delay
            repeated = binding.get('lastReason') == reason
            binding['lastReason'] = reason
            if not repeated:
                self._push_event({
                    'kind': 'reject',
                    'text': f'远端终端会话未能开启（{remote_key}）：{reason}{hint}',
                })
            return
        binding['opened'] = True
        binding['switchingControl'] = False
        binding.pop('controlSwitchStream', None)
        binding['resizeApplied'] = (int(result.extra.get('cols') or cols), int(result.extra.get('rows') or rows))
        binding['failures'] = 0
        binding['nextAttempt'] = 0.0
        binding['lastReason'] = ''
        self.log('RELAY', f'终端开启往返 {round((time.monotonic() - opened_at) * 1000)}ms，'
                 f'从机准备 {result.extra.get("prepareMs", "未知")}ms：{remote_key}')
        self._push_event({'kind': 'session', 'text': f'远端终端会话已开启：{remote_key}'})

    def _on_session_exit(self, remote_key: str, stream_id: str, reason: str,
                         *, expected_node=None, expected_binding=None) -> None:
        with self._lock:
            binding = self._sessions.get(remote_key)
            if (binding is None or binding.get('streamId') != stream_id
                    or (expected_node is not None and self.node is not expected_node)
                    or (expected_binding is not None and binding is not expected_binding)):
                return
            if binding is not None:
                if binding.get('controlSwitchStream') == stream_id:
                    reason = '终端控制权切换'
                # 保留绑定并置为未打开：下一轮对齐会重新开一条（设备仍在线时），
                # 让「设备掉线又回来」自动恢复成可用状态。
                binding['opened'] = False
                old_binding = dict(binding, opening=False)
                binding['streamId'] = ''
                switching = reason == '终端控制权切换'
                binding['switchingControl'] = switching
                binding['nextAttempt'] = 0.0 if switching else time.time() + relay_app_common.SESSION_REOPEN_BACKOFF_S
            node = self.node
        if node is not None:
            self._queue_session_close(node, remote_key, old_binding, '会话结束，确认回收旧接入')
        with self._lock:
            if (self.node is not node or self._sessions.get(remote_key) is not binding
                    or binding.get('streamId')):
                return
            self._push_event({'kind': 'session', 'text': f'远端终端会话结束：{remote_key}（{reason}）'})
            if self.on_terminal_exit:
                try:
                    self.on_terminal_exit(remote_key, reason)
                except Exception:
                    pass

    def _shared_state(self, binding):
        node = self.node
        reader = (getattr(node, '_readers', {}).get((binding.get('peerId'), binding.get('streamId')))
                  if node else None)
        meta = getattr(reader, 'last_meta', {}) or {}
        return {'owner': meta.get('owner', 'remote'), 'generation': meta.get('generation', ''),
                'shared': bool(meta.get('shared'))}

    def session_generation(self, remote_key: str):
        binding = self._sessions.get(remote_key)
        if (self.node is None or not binding or not binding.get('streamId')
                or not (binding.get('opened') or binding.get('opening'))):
            return None
        return (id(self.node), binding['streamId'])

    def take_session_control(self, remote_key, generation):
        binding = self._sessions.get(remote_key)
        node = self.node
        state = self._shared_state(binding) if binding else {}
        if not binding or not binding.get('opened') or node is None:
            return False, '终端尚未接通，暂时不能接管'
        if not generation or state.get('generation') != generation:
            return False, '控制代次已变化，请等待状态刷新后重试'
        stream = binding['streamId']
        binding['controlSwitchStream'] = stream
        result = node.submit(binding['peerId'], relay.KIND_SESSION_TAKE,
            payload={'streamId': stream, 'generation': generation}, timeout=10)
        if not result.ok and binding.get('controlSwitchStream') == stream:
            binding.pop('controlSwitchStream', None)
        return bool(result.ok), result.stdout or result.error or '接管请求完成'

    def session_input_allowed(self, remote_key):
        binding = self._sessions.get(remote_key)
        return bool(binding and self._shared_state(binding)['owner'] == 'remote')

    def session_input_ready(self, remote_key: str) -> bool:
        binding = self._sessions.get(remote_key)
        return bool(binding and binding.get('opened') and self._shared_state(binding)['owner'] == 'remote')

    def session_input(self, remote_key: str, data: bytes) -> bool:
        if not self.session_input_allowed(remote_key):
            return False
        node = self.node
        binding = self._sessions.get(remote_key)
        if node is None or binding is None or not binding.get('streamId'):
            return False
        try:
            return node.send_remote_input(binding['peerId'], binding['streamId'], data)
        except Exception as exc:
            self.log('RELAY', f'发送远端按键失败：{type(exc).__name__}: {exc}')
            return False

    def session_resize(self, remote_key: str, cols: int, rows: int) -> bool:
        node = self.node
        binding = self._sessions.get(remote_key)
        if node is None or binding is None or not binding.get('streamId'):
            return False
        binding['cols'] = int(cols)
        binding['rows'] = int(rows)
        size = (int(cols), int(rows))
        if binding.get('opened') and binding.get('resizeApplied') == size:
            return True
        try:
            result = node.resize_remote_session(binding['peerId'], binding['streamId'], cols, rows)
            if result.ok:
                binding['resizeApplied'] = size
            return bool(result.ok)
        except Exception:
            return False

    def close_session(self, remote_key: str) -> bool:
        """主动关闭某台远端设备的会话（设备仍在时会由对齐循环重开）。"""
        node = self.node
        with self._lock:
            binding = self._sessions.pop(remote_key, None)
        if binding is None or node is None:
            return False
        stream_id = binding.get('streamId')
        if not stream_id:
            return False
        token = self._queue_session_close(node, remote_key, binding, '操作端关闭了会话')
        return self._close_retired_session(token)

    def sessions_snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    'key': key,
                    **self._shared_state(binding),
                    'peerId': binding.get('peerId', ''),
                    'streamId': binding.get('streamId', ''),
                    'opened': bool(binding.get('opened')),
                    'switchingControl': bool(binding.get('switchingControl')),
                    'failures': int(binding.get('failures') or 0),
                    'lastReason': binding.get('lastReason', ''),
                    'cols': int(binding.get('cols') or 0),
                    'rows': int(binding.get('rows') or 0),
                }
                for key, binding in self._sessions.items()
            ]
