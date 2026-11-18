"""RelayController 的信任与配对面（B7 拆分，见 docs/internal/流程/优化建议清单.md）。

主控确认/撤销从机，从机维护自己的配对记录。准入判定本身在 relay_pairing。
共享 ``self``：配置、信任库与配对库都由 RelayController.__init__ 建立。
"""
import relay


class ControllerPairingMixin:
    # --- 信任 ---

    def confirm_peer(self, peer_id: str, accept: bool = True) -> tuple[bool, str]:
        if self.config.role != relay.ROLE_MASTER:
            return False, '从机等待主控选择并确认连接，无需在从机确认对端'
        node = self.node
        if node is None:
            return False, '跨机中转未启用'
        try:
            if accept:
                # 确认弹窗和连接列表是同一个用户决定，新版实例同时完成配对。
                target = next((item for item in node.pairing_records() if item['peerId'] == peer_id), None)
                if target is None or not target.get('nodeId') or not target.get('online'):
                    return False, '尚未发现在线实例，请等待对端启动；旧版对端需要升级后才能确认连接'
                node.pair_peer(target['nodeId'], peer_id=peer_id)
                record = node.confirm_peer(peer_id, note='用户在首连确认框中确认')
                with self._lock:
                    self._notified_peers.discard((peer_id, record.fingerprint))
                    self._notified_peers.discard(('wait-confirm', str(peer_id)))
                # 确认后立刻对齐一次：不必等下一轮轮询，界面上的远端终端会马上建起来。
                try:
                    self._reconcile_sessions()
                    self._emit_devices()
                except Exception:
                    pass
                self._emit_status(force=True)
                return True, '已确认'
            with self._lock:
                self._notified_peers.add((str(peer_id), ''))
            self._push_event({'kind': 'trust', 'text': f'已忽略对端 {peer_id}（不会接受其请求）'})
            return True, '已忽略'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def revoke_peer(self, peer_id: str) -> tuple[bool, str]:
        node = self.node
        if node is None:
            return False, '跨机中转未启用'
        done = node.revoke_peer(peer_id)
        self._emit_status(force=True)
        return done, '已移除' if done else '该对端不在信任名单中'

    def pair_peer(self, node_id: str, *, peer_id: str = '') -> tuple[bool, str]:
        """把某个实例加入配对名单。

        节点没在跑时也能配对（直接写本机的配对名单）：配对是长期决定，不该被
        「这一刻节点是否启动」挡住 —— 用户完全可能先配好再启用。
        """
        if self.config.role != relay.ROLE_MASTER:
            return False, '从机等待主控选择并确认连接，不能主动选择对端'
        node_id = str(node_id or '').strip()
        if not node_id:
            return False, '配对需要实例键'
        node = self.node
        try:
            if node is not None:
                node.pair_peer(node_id, peer_id=peer_id)
            else:
                store = self._pairing_store()
                store.confirm(node_id, peer_id=peer_id)
                self._push_event({'kind': 'trust', 'text': f'已配对实例 {node_id}（节点未运行）'})
            self._emit_status(force=True)
            return True, '已配对'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def unpair_peer(self, node_id: str) -> tuple[bool, str]:
        node_id = str(node_id or '').strip()
        if not node_id:
            return False, '需要实例键'
        node = self.node
        try:
            if node is not None:
                record = node.pairing.get(node_id)
                if self.config.role == relay.ROLE_MASTER and record and record.is_paired and record.peerId:
                    # 先通知在线从机关闭准入与 PTY；对端离线时仍立即撤销本机关系。
                    node.submit(record.peerId, relay.KIND_CLOSE_CONNECTION, timeout=2.0)
                done = node.unpair_peer(node_id)
            else:
                store = self._pairing_store()
                done = store.revoke(node_id)
            self._emit_status(force=True)
            return done, '已解除配对' if done else '该实例不在配对名单中'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def pairing_snapshot(self) -> list[dict]:
        """配对/发现名单。节点在跑就用节点的实时视图（含心跳里发现的实例），
        否则退回本机配对名单 —— 界面在两种状态下都能渲染同一形状的数据。"""
        node = self.node
        if node is not None:
            try:
                return node.pairing_records()
            except Exception:
                pass
        try:
            store = self._pairing_store()
            return [
                {
                    'nodeId': record.nodeId,
                    'peerId': record.peerId,
                    'fingerprint': record.fingerprint,
                    'host': record.host,
                    'state': record.state,
                    'stateLabel': record.stateLabel,
                    'paired': record.is_paired,
                    'confirmedAt': record.confirmedAt,
                    'revokedAt': record.revokedAt,
                    'discovered': False,
                }
                for record in sorted(store.items(), key=lambda item: item.nodeId)
            ]
        except Exception:
            return []
