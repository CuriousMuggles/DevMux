"""RelayNode 的配对、信任与健康视图（``NodePairingMixin``）。

配对名单、连接验证节奏、以及 ``health()`` 这一份给界面看的状态快照都在这里。
连接验证成功后退避到 30s，失败时保持 5s 密集探测 —— 理由见
``relay_node_constants.CONNECTION_VERIFY_OK_INTERVAL_S`` 的注释。
"""
from __future__ import annotations

import os
import re
import time
import uuid
from typing import Optional

import relay
from . import relay_session_clock
from .relay_node_constants import (
    CONNECTION_VERIFY_OK_INTERVAL_S,
    CONNECTION_VERIFY_RETRY_INTERVAL_S,
)


class NodePairingMixin:
    """配对、信任与状态快照。依赖 ``RelayNode.__init__`` 建立的属性。"""

    def accepted_master_ids(self) -> list[str]:
        """从机接受哪些主控。名单 = 本机信任名单，未确认的主控一律拒绝。"""
        return [record.peerId for record in self.trust.items() if record.peerId]

    def paired_node_ids(self) -> set:
        """已配对的**实例键**集合。配对门槛为空集时（未启用）返回 ``None`` 语义的
        全集由 ``_compute_health`` 处理，这里只回答事实。"""
        return self.pairing.paired_node_ids()

    def _node_id_for(self, peer_id: str) -> str:
        """由显示名反查实例键。查不到返回空串（调用方按 fail-closed 处理）。"""
        beat = self._peers.get(str(peer_id or ''))
        return beat.nodeId if beat is not None else ''

    def _pairing_blocks(self, peer_id: str) -> str:
        """准入判定：返回拒绝原因，空串表示放行。

        与 ``_authorize`` 的区别是**判定方向**：这里用于「我要不要主动找它」，
        那边用于「它找过来我要不要理」。两处都必须判，缺一个就漏：
        只判投递，别人仍能往本机分区写请求；只判受理，本机仍会往未配对实例的
        分区里留垃圾请求。
        """
        if peer_id in self._route_conflicts:
            return f'对端路由标识 {peer_id} 重复，请更新重名实例后重新配对'
        cross_deployment = self.channel_root(peer_id) != self.root
        if not self.pairing_required and not cross_deployment:
            return ''
        node_id = self._node_id_for(peer_id)
        if not node_id:
            return f'对端 {peer_id} 未上报实例键（版本过旧），本机已启用配对'
        if not self.pairing.is_paired(node_id):
            record = self.pairing.get(node_id)
            state = record.stateLabel if record else '未配对'
            return f'对端 {peer_id}（实例 {node_id}）当前为「{state}」，未配对前不通讯'
        return ''

    def _compute_health(self) -> relay.RelayHealth:
        link = self.carrier.status()
        health = relay.evaluate_health(
            enabled=True,
            role=self.role,
            self_id=self.peer_id,
            peers={peer_id: beat for peer_id, beat in self._peers.items()
                   if self.channel_root(peer_id) == self.root
                   or self.pairing.is_paired(beat.nodeId)},
            link=link,
            trust=self.trust,
            interval_s=self.heartbeat_interval_s,
            accepted_master_ids=self.accepted_master_ids(),
            carrier_kind=self.carrier.kind,
            local_device_count=len(self._managed_devices()),
            self_fingerprint=self.host_fingerprint,
            self_pid=os.getpid(),
            self_started_at=self._started_at,
            # 准入门槛：未配对的实例不进对端列表、不进角色仲裁，只进 unpairedPeers。
            pairing_enabled=bool(self.pairing_required),
            paired_node_ids=(self.paired_node_ids() if self.pairing_required else None),
        )
        if self.passive_slave and health.state in (relay.STATE_WAITING_PEER, relay.STATE_PENDING_CONFIRM):
            health.hint = '本机已就绪，等待主控选择并确认连接；从机无需选择对端'
        for view in health.peers:
            view.deploymentId = self._peer_deployments.get(view.peerId, self.pool_id)
            view.displayLabel = self._peer_display_label(view.peerId)
        for item in health.unpairedPeers:
            item['displayLabel'] = self._peer_display_label(item['peerId'])
        # 用户看到的标识统一为「机器名 / 部署」，内部路由名只用于协议选路。
        if self._peers:
            pattern = '|'.join(re.escape(peer_id) for peer_id in sorted(self._peers, key=len, reverse=True))
            health.hint = re.sub(pattern, lambda match: self._peer_display_label(match.group()), health.hint)
        return health

    def _peer_display_label(self, peer_id: str) -> str:
        beat = self._peers.get(peer_id)
        machine = beat.host if beat and beat.host else '未知机器'
        deployment = self._peer_deployments.get(peer_id, self.pool_id) or '默认部署'
        return f'{machine} / {deployment}'

    def _connection_endpoint(self, peer_id: str) -> tuple:
        beat = self._peers.get(peer_id)
        return (self.channel_root(peer_id), beat.nodeId, beat.startedAt) if beat else ()

    def _mark_connection_verified(self, peer_id: str) -> None:
        with self._io_lock:
            self._connection_verified[peer_id] = (self._connection_endpoint(peer_id), time.monotonic())
            self._connection_verified_at[peer_id] = relay.now_iso()

    def _connection_peer_online(self, peer_id: str) -> bool:
        return any(view.peerId == peer_id and view.online and view.role != self.role
                   for view in self._health.peers)

    def connection_ready(self, peer_id: str) -> bool:
        """在线、主控确认后连接验证仍新鲜；不依赖设备数量或 PTY。"""
        if self._pairing_blocks(peer_id) or not self._connection_peer_online(peer_id) or self._health.conflict:
            return False
        if not self.trust.check(peer_id, self._peer_fingerprint(peer_id))[0]:
            return False
        endpoint, _ = self._connection_verified.get(peer_id, ((), 0.0))
        # 验证绑定实例与启动代次；在线期间的瞬时探测超时不能拆掉已验证会话。
        return bool(endpoint and endpoint == self._connection_endpoint(peer_id))

    def connection_check_due(self, peer_id: str) -> bool:
        return time.monotonic() >= self._connection_check_after.get(peer_id, 0.0)

    def _observe_handshake(self, peer_id: str, result: relay.Result,
                           t1: float, t4: float, t1_mono: float, t4_mono: float) -> None:
        """把一次验证往返变成一个钟差样本。

        ``t1``/``t4`` 是本端写请求与读到回执的墙钟；``t2``/``t3`` 取自回执
        （对端读到、对端完成）。四者配对能对消处理耗时，剩下的误差只有单向延迟
        不对称 —— 所以往返越短样本越可信，``SessionClock`` 会丢掉明显偏慢的样本。
        """
        extra = result.extra or {}
        t2 = relay._epoch_ms(extra.get('recvAt')) / 1000.0
        t3 = relay._epoch_ms(result.finishedAt) / 1000.0
        if t2 <= 0 or t3 <= 0 or t4 <= t1 or t4_mono < t1_mono:
            return
        offset_ms, rtt_ms = relay_session_clock.handshake_offset_ms(t1, t2, t3, t4)
        if rtt_ms > 60000.0:
            # 回执隔了很久才被读到，这种样本的单向延迟占比太高，不如不要。
            return
        self.session_clock.observe_handshake(peer_id, offset_ms, rtt_ms=rtt_ms)

    def verify_connection(self, peer_id: str) -> relay.Result:
        """独立于设备的准入往返验证，由从机 _authorize 作最后裁决。

        这次往返同时充当対时握手：本端在写请求前后取 T1/T4，回执里带回
        T2（对端读到请求）与 T3（对端完成），四时间戳配对就能算出两端钟差与
        往返耗时（见 ``relay_session_clock.handshake_offset_ms``）。不需要额外
        协议：旧主控读到 ``recvAt`` 只会忽略，旧从机不带该字段时退回 mtime 推导。
        """
        endpoint = self._connection_endpoint(peer_id)
        record = self.pairing.get(self._node_id_for(peer_id))
        generation = record.connectionId if record else ''
        t1_mono, t1_wall = time.monotonic(), time.time()
        result = self.submit(peer_id, relay.KIND_CHECK_CONNECTION, timeout=10.0)
        self._observe_handshake(peer_id, result, t1_wall, time.time(),
                                t1_mono, time.monotonic())
        if result.ok and result.extra.get('nodeId') != self._node_id_for(peer_id):
            result = relay.Result(reqId=result.reqId, status=relay.STATUS_ERROR,
                                  error='连接验证的对端实例不一致，请重新发现对端')
        with self._io_lock:
            current = self.pairing.get(self._node_id_for(peer_id))
            if (self._stop_flag.is_set() or endpoint != self._connection_endpoint(peer_id)
                    or generation != (current.connectionId if current else '')):
                # 旧实例、旧确认代次的回执不能清掉当前连接，也不能推迟新的验证。
                return result
            self._connection_checks[peer_id] = (endpoint, result.status, result.error or '')
            if result.ok and endpoint == self._connection_endpoint(peer_id):
                # 验证成功：连接已可用，退避到 OK 间隔再复查，避免每次成功验证
                # 都产生一对请求/回执文件（写放大）。失败/未验证路径保持密集重试。
                self._mark_connection_verified(peer_id)
                self._connection_check_after[peer_id] = time.monotonic() + CONNECTION_VERIFY_OK_INTERVAL_S
            else:
                self._connection_check_after[peer_id] = time.monotonic() + CONNECTION_VERIFY_RETRY_INTERVAL_S
                if not result.ok and not result.extra.get('localTimeout'):
                    self._connection_verified.pop(peer_id, None)
                    self._connection_verified_at.pop(peer_id, None)
        return result

    def connection_info(self, peer_id: str, node_id: str = '') -> dict:
        """每个实例只有一个可展示的连接结论；确认、在线、验证与设备数分开。"""
        beat = self._peers.get(peer_id)
        same_instance = bool(beat and (not node_id or beat.nodeId == node_id))
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        online = bool(same_instance and not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew))
        actual_node = node_id or (beat.nodeId if beat else '')
        local_confirmed = bool(actual_node and self.pairing.is_paired(actual_node)
                               and same_instance and self.trust.check(peer_id, beat.fingerprint)[0])
        clock_info = self.session_clock.info(peer_id)
        common = {
            'online': online,
            'localConfirmed': local_confirmed,
            # 三级状态分开暴露：能看见（心跳在推进）/ 能执行（握手回执通）/
            # 时间已同步（钟差测出来了）。混成一个布尔，正是「界面说已连接、
            # 实际命令全废」的根源。
            'connectionReady': self.connection_ready(peer_id),
            'clockSkewMs': clock_info.get('clockSkewMs'),
            'clockSynced': clock_info.get('clockSynced'),
            'clockRttMs': clock_info.get('clockRttMs'),
            'clockMeasuredAgeMs': clock_info.get('clockMeasuredAgeMs'),
            'clockSource': clock_info.get('clockSource'),
            'clockNote': clock_info.get('clockNote'),
            'lastHeartbeatAgeMs': (max(0.0, beat.age_ms(skew_ms=skew)) if same_instance else None),
            'deviceCount': len(beat.devices) if same_instance else 0,
            'verifiedAt': '',
            'heartbeatSeq': beat.heartbeatSeq if same_instance else 0,
            'heartbeatReadMs': self._heartbeat_read_metrics.get(peer_id),
            'uptimeSeconds': beat.uptimeSeconds if same_instance else 0,
        }

        def result(state: str, reason: str, action: str, code: str) -> dict:
            return {**common, 'connectionState': state, 'connectionReason': reason,
                    'reasonCode': code, 'nextAction': action}

        if not online:
            return result('offline', '对端离线，确认关系保留；上线后自动验证',
                          '启动对端并检查两端共享目录是否指向同一位置', 'peer-offline')
        if self._health.conflict or beat.role == self.role:
            return result('role-mismatch', '双方角色相同或主控角色冲突',
                          '让操作设备的一端为主控、连接设备的一端为从机', 'role-mismatch')
        if not local_confirmed:
            if self.passive_slave:
                return result('waiting-master', '本机已就绪，等待主控选择并确认连接',
                              '保持本机运行，在主控端选择本机并确认连接', 'waiting-master')
            block = self._pairing_blocks(peer_id)
            reason = block or '待本机确认对端身份'
            return result('local-confirmation', reason,
                          '核对机器名和部署后，在本机点击「确认连接」', 'local-confirmation')
        if self.connection_ready(peer_id):
            common['verifiedAt'] = self._connection_verified_at.get(peer_id, '')
            skew = common.get('clockSkewMs') or 0.0
            note = ''
            if abs(skew) >= relay.CLOCK_JUMP_ALERT_MS:
                # 已自动折算，所以通讯仍然可用；但让用户知道该去对时，否则一旦
                # 偏差越过 TTL 就会变成「能连上、命令全被丢弃」，届时更难查。
                direction = '本机偏快' if skew < 0 else '本机偏慢'
                note = (f'；两端时钟相差 {abs(skew) / 1000:.0f} 秒（{direction}），'
                        f'已自动折算，建议两端对时')
            return result('connected', '连接已验证，通讯可用' + note,
                          '在主控端管理设备' if self.role == relay.ROLE_MASTER else '保持本机运行，设备操作在主控端完成',
                          'verified')
        check = self._connection_checks.get(peer_id, ((), '', ''))
        if check[0] == self._connection_endpoint(peer_id) and check[2]:
            reason = check[2]
            if check[1] == relay.STATUS_REJECTED and ('未配对' in reason or '未确认' in reason or '已解除' in reason):
                return result('waiting-peer-confirmation', reason,
                              '在主控端重新确认连接；旧版从机需升级后才能自动接受', 'peer-not-confirmed')
            return result('check-failed', reason,
                          '检查对端状态和共享目录后等待自动重试', 'verification-failed')
        return result('checking', '等待主控连接验证' if self.passive_slave else '主控已确认，正在验证连接',
                      '等待自动验证；若持续未接通，请检查对端状态和共享目录', 'verifying')

    def _notice_state_change(self) -> None:
        if self._health.state != self._last_state:
            self._last_state = self._health.state
            self._emit('state', f'连接状态 → {self._health.stateText}（{self._health.hint}）')

    # --- 信任与状态 ---
    def confirmation_candidates(self) -> list[relay.PeerView]:
        """确认入口也要包含未准入实例；发现不等于允许执行设备操作。"""
        if self.passive_slave:
            return []
        candidates = {view.peerId: view for view in self.peers() if view.needsConfirmation and view.online}
        for item in self.pairing_records():
            if item.get('paired') or item.get('state') == relay.PAIR_REVOKED or not item.get('online'):
                continue
            beat = self._peers.get(item.get('peerId'))
            if beat is None or not beat.nodeId or beat.role == self.role:
                continue
            candidates[beat.peerId] = relay.PeerView(
                peerId=beat.peerId, nodeId=beat.nodeId, role=beat.role, online=True,
                needsConfirmation=True, fingerprint=beat.fingerprint, host=beat.host,
                deviceCount=len(beat.devices), deploymentId=item.get('deploymentId', ''),
                displayLabel=self._peer_display_label(beat.peerId))
        return list(candidates.values())

    def confirm_peer(self, peer_id: str, *, note: str = ''):
        """把对端加入信任名单（等同于用户在「首连确认框」里点确认）。"""
        target = relay.check_peer_id(peer_id, '对端标识')
        beat = self._peers.get(target)
        record = self.trust.confirm(
            target,
            fingerprint=(beat.fingerprint if beat else ''),
            role=(beat.role if beat else ''),
            note=note,
        )
        with self._io_lock:
            self._health = self._compute_health()
        self._emit('trust', f'已确认对端 {target}（指纹 {record.fingerprint or "未提供"}）')
        return record

    def revoke_peer(self, peer_id: str) -> bool:
        node_id = self._node_id_for(str(peer_id))
        done = self.trust.revoke(str(peer_id))
        if done:
            if self.windows_service is not None:
                self.windows_service.close_sessions(node_id)
            if self.extension_tools is not None:
                self.extension_tools.close_owner(node_id)
            self._connection_verified.pop(str(peer_id), None)
            self._connection_verified_at.pop(str(peer_id), None)
            # 解除连接后钟差要重测：新会话的两端实例、晶振基准都可能变了。
            self.session_clock.forget(str(peer_id))
            # 撤销即失效：复查节奏一起作废，重新确认后无需等旧成功节奏。
            self._connection_check_after.pop(str(peer_id), None)
            self._connection_checks.pop(str(peer_id), None)
            with self._io_lock:
                self._health = self._compute_health()
            self._emit('trust', f'已移除对端 {peer_id} 的信任')
        return done

    # --- 配对（准入门槛）---
    def pair_peer(self, node_id: str, *, peer_id: str = '', fingerprint: str = '', host: str = '',
                  connection_id: Optional[str] = None) -> dict:
        """把某个**实例键**加入配对名单（准入门槛的放行条件）。

        会顺带确认信任：站在用户角度「我跟它配对」和「我确认它是谁」是同一个决定
        （他是在池内列表里看着对方的标识/主机/指纹点的），要求点两次只是摩擦，不
        增加安全性。反过来 ``unpair_peer`` 也会撤掉信任，保持两侧对称。

        身份信息（显示名、指纹、主机）优先取实时心跳，其次用调用方传进来的 ——
        心跳里的才是当下事实，调用方传的可能是几秒前的界面快照。
        """
        node_id = str(node_id or '').strip()
        if not node_id:
            raise ValueError('配对需要实例键（nodeId）')
        resolved = peer_id
        if not resolved:
            for candidate, beat in self._peers.items():
                if beat.nodeId == node_id:
                    resolved = candidate
                    fingerprint = fingerprint or beat.fingerprint
                    host = host or beat.host
                    break
        beat = self._peers.get(resolved)
        if beat is not None and beat.nodeId == node_id:
            fingerprint = beat.fingerprint
            host = beat.host
        if connection_id is None and self._connection_scope:
            connection_id = f'{self._connection_scope}.{uuid.uuid4().hex}'
        record = self.pairing.confirm(node_id, peer_id=resolved, fingerprint=fingerprint, host=host,
                                      connection_id=connection_id)
        self._connection_verified.pop(resolved, None)
        self._connection_verified_at.pop(resolved, None)
        # 新确认代次必须立即重验：弹掉标记的同时把复查节奏作废，否则要等
        # 上次成功验证的旧节奏（成功档 30s）设备与会话才能恢复。
        if resolved:
            self._connection_check_after[resolved] = 0.0
            self._connection_checks.pop(resolved, None)
        if resolved:
            self.trust.confirm(
                resolved,
                fingerprint=fingerprint or self._peer_fingerprint(resolved),
                role=(self._peers[resolved].role if resolved in self._peers else ''),
                note=f'由配对操作确认（实例 {node_id}）',
            )
        with self._io_lock:
            self._health = self._compute_health()
        self._emit('trust', f'已配对实例 {node_id}（{resolved or "尚未心跳"}）')
        return record.view()

    def unpair_peer(self, node_id: str) -> bool:
        """解除配对（单方面立即生效）。同时撤掉信任，保持两侧对称。"""
        node_id = str(node_id or '')
        record = self.pairing.get(node_id)
        done = self.pairing.revoke(node_id)
        if record is not None and record.peerId:
            self.trust.revoke(record.peerId)
            self._connection_verified.pop(record.peerId, None)
            self._connection_verified_at.pop(record.peerId, None)
        if done:
            # 终端输入走追加流，不经过请求授权；撤销从机上的配对时也必须关闭已开会话。
            if self._sessions is not None:
                self._sessions.stop_all()
            if self.extension_tools is not None:
                self.extension_tools.close_owner(node_id)
            with self._io_lock:
                self._health = self._compute_health()
            self._emit('trust', f'已解除与实例 {node_id} 的配对')
        return done

    def pairing_records(self) -> list[dict]:
        """配对名单快照（含池内已发现但未配对的实例，供界面一次渲染完整）。"""
        # 三种来源共用 ``PairRecord.view()`` 的字段形状，只在下游补上各自的上下文
        # 标记（``discovered``/``legacy``）—— 同一份记录若对外有几种形状，前端迟早
        # 会在某一处读到 KeyError。
        known: dict[str, dict] = {}
        # 状态页已判定的旧身份不能从历史配对或注册表重新进入下拉列表。
        ignored_peers = set(self._health.selfGhosts) | set(self._health.supersededPeers)
        ignored_nodes = {beat.nodeId for peer, beat in self._peers.items()
                         if peer in ignored_peers and beat.nodeId}
        ignored_nodes -= {beat.nodeId for peer, beat in self._peers.items()
                          if peer not in ignored_peers and beat.nodeId}
        observed_nodes = {beat.nodeId for beat in self._peers.values() if beat.nodeId}
        for record in self.pairing.items():
            if record.nodeId == self.node_id or record.nodeId in ignored_nodes:
                continue
            if record.nodeId not in observed_nodes:
                # 本机早期测试身份已没有发现来源；撤销记录仍留盘防止旧代次复活。
                if (record.state == relay.PAIR_REVOKED
                        or (record.fingerprint == self.host_fingerprint
                            and record.host.casefold() == self.host_label.casefold())):
                    continue
            known[record.nodeId] = {**record.view(), 'discovered': False}
        # 注册表先于设备枚举和心跳写入；启动中的实例也应能成为配对候选。
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        registries = (self._presence_snapshot[1] if self._presence_snapshot is not None else
                      ({self.pool_id: relay.read_registry(self.root)} if self.pool_id else {}))
        for deployment, entries in registries.items():
            for entry in entries.values():
                if (entry.nodeId == self.node_id or entry.nodeId in ignored_nodes
                        or entry.is_stale(skew_ms=skew)):
                    continue
                record = self.pairing.get(entry.nodeId) or relay.PairRecord(
                    nodeId=entry.nodeId, peerId=entry.peerId,
                    fingerprint=entry.fingerprint, host=entry.host,
                )
                known[entry.nodeId] = {
                    **record.view(), 'peerId': entry.peerId, 'host': entry.host,
                    'deploymentId': deployment, 'role': entry.role, 'roleLabel': entry.roleLabel,
                    'online': True, 'discovered': True,
                }
        # 池内/总线上发现但没有记录的实例：列出来才有「可配对的目标」可点。
        # 同一实例改过路由名时，旧心跳不能覆盖仍在线的新身份。
        beats = sorted(self._peers.values(), key=lambda beat: (
            not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew),
            beat.fileMtimeMs, beat.heartbeatAt))
        for beat in beats:
            if (not beat.nodeId or beat.nodeId == self.node_id
                    or beat.peerId in ignored_peers or beat.nodeId in ignored_nodes):
                continue
            if (beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)
                    and not self.pairing.get(beat.nodeId)):
                continue
            view = self.pairing.get(beat.nodeId) or relay.PairRecord(
                nodeId=beat.nodeId, peerId=beat.peerId,
                fingerprint=beat.fingerprint, host=beat.host,
            )
            known[beat.nodeId] = {
                **view.view(), 'peerId': beat.peerId, 'host': beat.host,
                'fingerprint': beat.fingerprint,
                'deploymentId': self._peer_deployments.get(beat.peerId, self.pool_id),
                'role': beat.role, 'roleLabel': beat.roleLabel,
                'online': not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew),
                'discovered': True,
            }
        for item in self._health.unpairedPeers:
            node_id = str(item.get('nodeId') or '')
            if (not node_id or node_id == self.node_id or node_id in ignored_nodes
                    or node_id in known or not item.get('online')):
                continue
            view = relay.PairRecord(
                nodeId=node_id, peerId=item.get('peerId', ''), host=item.get('host', ''),
            )
            known[node_id] = {
                **view.view(), 'discovered': True, 'legacy': bool(item.get('legacy')),
            }
        records = [{**known[node_id], **self.connection_info(known[node_id].get('peerId', ''), node_id)}
                   for node_id in sorted(known)]
        if self.role != relay.ROLE_MASTER:
            return records
        unique = {}
        for item in sorted(records, key=lambda item: (
                item.get('connectionState') != 'connected', not item.get('online'),
                not item.get('paired'), item['nodeId'])):
            identity = (str(item.get('host') or item['nodeId']).casefold(), item.get('deploymentId') or '')
            unique.setdefault(identity, item)
        return list(unique.values())

    def health(self) -> relay.RelayHealth:
        return self._health

    def peers(self) -> list[relay.PeerView]:
        return list(self._health.peers)

    def wait_for_peers(self, timeout: float = 10.0, count: int = 1) -> bool:
        """等至少 ``count`` 个对端出现在线，用于演示/自动化编排。"""
        deadline = time.time() + max(0.2, timeout)
        while time.time() < deadline:
            with self._io_lock:
                self._peers = self._read_shared_peers()
                self._health = self._compute_health()
            if len([view for view in self._health.peers if view.online]) >= count:
                return True
            time.sleep(0.2)
        return False

    def pending_confirmations(self) -> list[str]:
        """在线但未确认的对端 —— 就是要弹「首连确认框」的那批。"""
        if self.passive_slave:
            return []
        return sorted(view.peerId for view in self._health.peers if view.needsConfirmation)

    def refresh(self) -> relay.RelayHealth:
        # 运行中设备与发现各由专用任务刷新，界面读取不能再次扫描整个共享目录。
        if not self.is_running():
            self._refresh_devices()
            peers = self._read_shared_peers()
            with self._io_lock:
                self._peers = peers
        with self._io_lock:
            self._health = self._compute_health()
        return self._health

    def status_line(self) -> str:
        return f'[{relay.role_label(self.role)} {self.peer_id}] {self._health.stateText} · {self._health.hint}'

    def link_notice(self, peer_id: str) -> str:
        """建立连接时的主/从身份提示（需求①的落点）。"""
        target = str(peer_id)
        view = next((item for item in self._health.peers if item.peerId == target), None)
        if view is None:
            return f'对端 {target} 不在心跳名单里，无法给出身份提示'
        local_devices = self._managed_devices()
        lines = [
            f'  本机：{self.peer_id}（{relay.role_label(self.role)}）'
            f'　指纹 {self.host_fingerprint}　主机 {self.host_label}',
            f'  对端：{view.peerId}（{view.roleLabel}）'
            f'　指纹 {view.fingerprint or "未提供"}　主机 {view.host or "未知"}',
            f'  载体：{self.carrier.status().get("kindLabel") or self.carrier.kind}'
            f'　往返延迟 {self.carrier.status().get("latencyMs")} ms',
            f'  本机设备 {len(local_devices)} 台　对端设备 {view.deviceCount} 台',
            f'  信任：{"已确认" if view.trusted else view.trustReason}',
        ]
        return '\n'.join(lines)
