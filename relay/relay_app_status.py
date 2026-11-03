"""RelayController 的状态与事件面（B7 拆分，见 docs/优化建议清单.md）。

把节点事件攒成本机可见的流水，并输出前端可直接渲染的状态快照。
共享 ``self``：状态缓存（上次签名、事件队列）都由 RelayController.__init__ 建立。
"""
import os
import time

import relay
from . import relay_node
from . import relay_app_common
from .relay_app_common import default_peer_id


class ControllerStatusMixin:
    # --- 节点事件 ---

    def _on_node_event(self, payload: dict) -> None:
        self._push_event(payload)

    def _push_event(self, payload: dict) -> None:
        item = {
            'kind': str(payload.get('kind') or 'info'),
            'text': str(payload.get('text') or ''),
            'at': str(payload.get('at') or relay.now_iso()),
            'roleLabel': str(payload.get('roleLabel') or relay.role_label(self.config.role)),
        }
        with self._lock:
            self._events.append(item)
        if self.on_event:
            try:
                self.on_event(dict(item))
            except Exception:
                pass

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    # --- 主循环：刷新状态 + 对齐远端会话 ---

    def _loop(self, stop_flag=None) -> None:
        stop_flag = stop_flag or self._stop_flag
        next_status = 0.0
        next_session = 0.0
        while not stop_flag.is_set():
            now = time.time()
            node = self.node
            if node is None:
                break
            try:
                if now >= next_status:
                    node.refresh()
                    if stop_flag.is_set():
                        break
                    self._refresh_connections()
                    if stop_flag.is_set():
                        break
                    self._notice_peer_requests()
                    self._emit_status()
                    next_status = now + relay_app_common.STATUS_POLL_INTERVAL_S
                if now >= next_session:
                    self._reconcile_sessions()
                    self._emit_devices()
                    next_session = now + relay_app_common.SESSION_POLL_INTERVAL_S
            except Exception as exc:  # 单次异常不能让循环死掉
                self._push_event({'kind': 'error', 'text': f'中转轮询异常：{type(exc).__name__}: {exc}'})
            stop_flag.wait(0.2)

    def _notice_peer_requests(self) -> None:
        """发现「在线但未确认」的新对端 → 让前端弹首连确认（含主/从身份提示）。"""
        node = self.node
        if node is None or not self.on_peer_request:
            return
        for view in node.confirmation_candidates():
            if not view.needsConfirmation:
                continue
            token = (view.peerId, view.fingerprint)
            with self._lock:
                if token in self._notified_peers:
                    continue
                self._notified_peers.add(token)
            try:
                self.on_peer_request({
                    'peerId': view.peerId,
                    'role': view.role,
                    'roleLabel': view.roleLabel,
                    'fingerprint': view.fingerprint,
                    'host': view.host,
                    'deploymentId': view.deploymentId,
                    'displayLabel': view.displayLabel,
                    'deviceCount': view.deviceCount,
                    'online': bool(view.online),
                    'roleHint': f'本机为{relay.role_label(self.config.role)}，'
                                f'对端声明为{view.roleLabel}',
                    'detail': node.link_notice(view.peerId),
                    'carrier': self.config.carrierKind,
                    'latencyMs': float(node.carrier.status().get('latencyMs') or 0.0),
                })
            except Exception:
                pass

    # --- 状态输出 ---

    def status(self) -> dict:
        """前端可直接渲染的状态快照：所有结论在后端算好，前端只渲染。"""
        node = self.node
        config = self.config
        base = {
            'enabled': bool(config.enabled),
            'running': node is not None,
            'role': config.role,
            'roleLabel': relay.role_label(config.role),
            # 界面显示**生效值**（同机多开时会自动退让成 -2/-3，见 _effective_peer_id）：
            # 显示配置值会让用户拿一个总线上并不存在的名字去排查。
            'peerId': self._effective_peer_id or config.peerId,
            # 界面本机标识固定取计算机名，peerId 仅供内部文件路由使用。
            'machineName': self._host_label or os.environ.get('COMPUTERNAME') or default_peer_id(),
            'deployments': node.deployment_ids() if node is not None else list(relay.deployment_roots(config.root)),
            'carrierKind': config.carrierKind,
            'carrier': None,
            'root': config.root,
            # 池：``root`` 是配置里填的共享目录，``effectiveRoot`` 才是实际数据根
            # （启用池时会下沉一层到 <root>/pool/<poolId>/）。两者都给出，界面才能
            # 把「你填的」和「用在哪」分开显示 —— 只给一个的话，用户会以为程序没理会
            # 他填的路径。
            'poolId': config.poolId,
            'nodeId': config.nodeId,
            'pairingRequired': config.pairingEnabled,
            'effectiveRoot': relay.resolve_pool_root(config.root, config.poolId),
            'pool': None,
            'pairing': [],
            'unpairedPeers': [],
            'state': relay.STATE_UNCONFIGURED,
            'stateText': '未启用',
            'hint': '在「工具 → 跨机中转配置…」中启用并填写共享目录',
            'configNotice': self.config_notice,
            # 多开实例实际用的退让名。优先报**生效值**（可能是 -3 而不只是 -2：
            # 同机开到第三个实例时，前两个后缀都已被占），没起来时才退回按约定算一个。
            'suggestedPeerId': (
                self._effective_peer_id or (default_peer_id() + '-2')
            ) if self.config_notice else '',
            'peers': [],
            'pendingConfirmation': [],
            'conflict': False,
            # 未启用时也把这两个列表给全：前端与去抖签名都按「列表恒存在」处理，
            # 只在这里缺键会让界面/签名在不同状态下拿到不同形状。
            'selfGhosts': [],
            'staleMasters': [],
            'sessions': self.sessions_snapshot(),
            'servedSessions': self.served_sessions(),
            'events': self.events()[-40:],
            'startError': self._start_error,
            'appVersion': relay_app_common.APP_VERSION,
            'heartbeatSeq': self.node._heartbeat_sequence if self.node else 0,
            'uptimeSeconds': max(0, int(time.monotonic() - relay_node.APP_STARTED_MONOTONIC)),
        }
        if node is None:
            if config.enabled and self._start_error:
                base.update({
                    'state': relay.STATE_DISCONNECTED,
                    'stateText': '启动失败',
                    'hint': self._start_error,
                })
            return base
        health = node.health()
        payload = health.to_dict() if hasattr(health, 'to_dict') else {}
        base.update(payload)
        base['running'] = True
        base['sessions'] = self.sessions_snapshot()
        base['servedSessions'] = self.served_sessions()
        base['events'] = self.events()[-40:]
        base['startError'] = self._start_error
        try:
            base['carrier'] = node.carrier.status()
        except Exception:
            base['carrier'] = None
        try:
            base['deviceCount'] = len(self.remote_devices())
            base['onlineDeviceCount'] = len(self.online_remote_devices())
        except Exception:
            pass
        # 池内全貌与配对名单：都由后端算好，前端只渲染。异常一律降级成空值 ——
        # 这两块是「锦上添花」的信息，缺了不该让整个状态快照拉不出来。
        if config.poolId:
            try:
                base['pool'] = node.pool_info()
            except Exception:
                base['pool'] = None
        try:
            base['pairing'] = self.pairing_snapshot()
            base['pendingConfirmation'] = [view.peerId for view in node.confirmation_candidates()]
        except Exception:
            base['pairing'] = []
        return base

    def _emit_status(self, *, force: bool = False) -> None:
        if not self.on_status:
            return
        status = self.status()
        # 日志跟随实际发送给界面的逐实例状态，不再只依赖较慢的发现扫描。
        notices = []
        with self._lock:
            previous = getattr(self, '_visible_peer_online', {})
            current = {}
            for item in status.get('pairing') or []:
                key = item.get('nodeId') or item.get('peerId')
                online = bool(item.get('online'))
                current[key] = online
                if key in previous and previous[key] != online:
                    label = '恢复在线' if online else '判定离线'
                    age = item.get('lastHeartbeatAgeMs')
                    notices.append(f'界面对端{label}：{item.get("peerId") or key}，'
                                   f'心跳序号 {item.get("heartbeatSeq")}，'
                                   f'距最后观测推进 {round(age) if age is not None else "未知"}ms，'
                                   f'最近心跳读取 {item.get("heartbeatReadMs") if item.get("heartbeatReadMs") is not None else "未知"}ms')
            self._visible_peer_online = current
        for notice in notices:
            self.log('RELAY', notice)
        # 签名必须覆盖界面真正会渲染的每一项。只数会话条数是错的：会话从「没开起来」
        # 变成「已开启」条数不变，去抖会把这次变化吞掉，界面就一直停在旧结论上
        # （例如永远显示「会话未能开启」）。所以逐条带上开启状态与最近一次原因。
        # 对端同理：界面上要显示角色、在线/确认标记与设备数，缺一项就会停在旧标签。
        # 离线对端还显示「最后心跳 x 前」，所以年龄要进签名 —— 但按 30 秒粒度折算，
        # 否则静默期间每来一条状态（每 2 秒）都会因为年龄变了而多推一次。
        peer_signature = sorted(
            (view.get('peerId'), view.get('online'), view.get('trusted'),
             view.get('needsConfirmation'), view.get('role'), view.get('deviceCount'),
             view.get('deploymentId'), view.get('displayLabel'), view.get('startedAt'),
             int((view.get('ageMs') or 0) // 30000))
            for view in (status.get('peers') or []) if isinstance(view, dict)
        )
        session_signature = sorted(
            (session.get('key'), session.get('opened'), session.get('failures'),
             session.get('lastReason'), session.get('cols'), session.get('rows'),
             session.get('owner'), session.get('generation'), session.get('switchingControl'))
            for session in (status.get('sessions') or []) if isinstance(session, dict)
        )
        # 本机自己那一行（角色/标识/状态文案/冲突/旧标识残留）也必须进签名。
        # 漏掉角色时有个很典型的后果：把「从机」改成「主控」后 ``state`` 往往仍是
        # ``waiting-peer``（两边都没对端），签名一模一样 → 这次推送被去抖吃掉，
        # 界面继续显示「从机」，而实际运行的是主控。
        self_signature = (
            status.get('role'), status.get('roleLabel'), status.get('peerId'),
            status.get('stateText'), status.get('hint'), status.get('conflict'),
            tuple(status.get('selfGhosts') or ()), status.get('running'),
        )
        served_signature = sorted(
            (session.get('streamId'), session.get('deviceKey'), session.get('cols'),
             session.get('rows'), session.get('owner'), session.get('generation'))
            for session in (status.get('servedSessions') or []) if isinstance(session, dict)
        )
        # 池与配对也要进签名：配对的「已配对/待确认/未配对」直接决定界面上按钮显示
        # 「配对」还是「解除」；池内实例上下线、槽位变化则决定池子那一块的列表内容。
        # 漏掉它们，用户点完「配对」会看到界面纹丝不动（去抖把这次推送吃了）。
        pool = status.get('pool') or {}
        pool_signature = (
            pool.get('count'), pool.get('onlineCount'),
            tuple(sorted(
                (entry.get('nodeId'), entry.get('slot'), entry.get('online'))
                for entry in (pool.get('entries') or []) if isinstance(entry, dict)
            )),
        )
        pairing_signature = sorted(
            (item.get('nodeId'), item.get('state'), item.get('paired'),
             item.get('deploymentId'), item.get('online'), item.get('host'),
             item.get('connectionState'), item.get('connectionReason'),
             item.get('heartbeatSeq'), item.get('uptimeSeconds'))
            for item in (status.get('pairing') or []) if isinstance(item, dict)
        )
        unpaired_signature = sorted(
            (item.get('peerId'), item.get('nodeId'), item.get('online'))
            for item in (status.get('unpairedPeers') or []) if isinstance(item, dict)
        )
        signature = (repr(peer_signature) + str(status.get('state'))
                     + repr(session_signature) + repr(served_signature)
                     + repr(pool_signature) + repr(pairing_signature)
                     + repr(unpaired_signature)
                     + repr(status.get('deployments'))
                     + repr((status.get('heartbeatSeq'), status.get('uptimeSeconds')))
                     + repr(self_signature))
        with self._lock:
            if not force and signature == self._last_status_sig:
                return
            self._last_status_sig = signature
        try:
            self.on_status(status)
        except Exception:
            pass

    def _emit_devices(self) -> None:
        """远端设备集合变化时通知上层重新合并设备列表。"""
        if not self.on_devices_changed:
            return
        from adb_worker import make_key  # noqa: PLC0415 - 只用它做 key 计算
        devices = []
        for device in self.remote_devices():
            item = dict(device)
            item['key'] = str(item.get('key') or make_key(item))
            devices.append(item)
        signature = repr(sorted(
            (device.get('key'), (device.get('relay') or {}).get('online'),
             (device.get('relay') or {}).get('deploymentId'),
             (device.get('relay') or {}).get('displayLabel'),
             (device.get('relay') or {}).get('endpointRoot'),
             (device.get('relay') or {}).get('startedAt')) for device in devices
        ))
        with self._lock:
            if signature == self._last_devices_sig:
                return
            self._last_devices_sig = signature
        try:
            self.on_devices_changed(devices, [device['key'] for device in devices])
        except Exception:
            pass
