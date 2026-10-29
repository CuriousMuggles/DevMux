"""主控单端确认、从机被动准入、无设备验证、持久重连和连接代次回归。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_app
import relay_node
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until, _EXEC_LOG


def run() -> bool:
    checks = []
    def check(name, ok):
        checks.append(bool(ok))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')

    def connected(controller):
        return any(item.get('connectionState') == 'connected' for item in controller.status()['pairing'])

    with tempfile.TemporaryDirectory(prefix='relay-confirmation-') as directory:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness = _Harness(os.path.join(directory, 'devices'), master_local_devices_enabled=False)
            try:
                check('旧配置不能关闭主控确认门槛', all(
                    relay_app.RelayConfig.from_dict({'pairingRequired': value}).pairingEnabled
                    and relay_app.RelayConfig.from_dict({'pairingRequired': value}).to_dict()['pairingRequired']
                    for value in (None, False, True)))
                harness.start()
                check('未确认实例只进入发现名单', _wait_until(lambda:
                    bool(harness.master.status()['pairing']) and bool(harness.slave.status()['pairing']))
                    and not harness.master.status()['peers'])
                check('从机不弹确认、不允许主动配对', not harness.slave.node.confirmation_candidates()
                      and not harness.slave.status()['pendingConfirmation']
                      and not harness.slave.pair_peer(harness.master.config.nodeId, peer_id='pc-a')[0]
                      and not harness.slave.confirm_peer('pc-a')[0])
                check('从机发现主控后只显示等待主控', any(
                    item.get('connectionState') == 'waiting-master'
                    for item in harness.slave.status()['pairing']))
                harness.master.node.confirm_peer('pc-b')
                harness.slave.node.confirm_peer('pc-a')
                harness.master.node.refresh()
                harness.slave.node.refresh()
                check('历史身份信任不会自动变成实例配对', not harness.master.node.pairing.items()
                      and not harness.slave.node.pairing.items())
                _EXEC_LOG.clear()
                key = 'relay:pc-b:devB:tid=1'
                result = harness.master.remote_exec(key, ['shell', 'echo', 'premature'], timeout=1)
                check('主控确认前命令、远端设备和 PTY 全部被阻止', not result['success']
                      and not _EXEC_LOG and not harness.master.devices() and not harness.backend.live_handles())
                raw = relay.new_request(relay.KIND_EXEC, 'pc-a', payload={
                    'sourceNodeId': harness.master.config.nodeId,
                    'targetNodeId': harness.slave.config.nodeId,
                    'connectionId': 'not-verified', 'argv': ['shell', 'echo', 'blocked']})
                check('普通设备请求不能自动建立准入', bool(harness.slave.node._authorize(raw))
                      and not harness.slave.node.pairing.items())
                check('主控确认即可发起配对', harness.master.confirm_peer('pc-b')[0])
                check('从机无需操作，两端自动报告验证成功', _wait_until(lambda:
                    connected(harness.master) and connected(harness.slave)))
                pair = harness.master.node.pairing.get(harness.slave.config.nodeId)
                accepted = harness.slave.node.pairing.get(harness.master.config.nodeId)
                initial_id = pair.connectionId
                check('从机持久保存主控的连接代次', bool(initial_id)
                      and accepted.connectionId == initial_id and accepted.is_paired)
                check('验证完成后自动展示设备并打开终端', _wait_until(lambda:
                    bool(harness.master.devices()) and any(session['opened']
                        for session in harness.master.sessions_snapshot())))
                view = harness.master.status()['pairing'][0]
                check('逐对端状态保留验证时间与设备数量', view.get('reasonCode') == 'verified'
                      and bool(view.get('verifiedAt')) and view.get('deviceCount') == 1)
                wrong = relay.new_request(relay.KIND_CHECK_CONNECTION, 'pc-a', payload={
                    'sourceNodeId': harness.master.config.nodeId, 'targetNodeId': 'wrong-instance',
                    'connectionId': initial_id})
                check('实例键不匹配不准入，也不修改已确认代次', bool(harness.slave.node._authorize(wrong))
                      and accepted.connectionId == initial_id)
                harness.stop()
                harness.start()
                check('双方重启保留确认并自动重新验证', _wait_until(lambda:
                    connected(harness.master) and connected(harness.slave)))
                check('重启沿用同一确认代次', harness.master.node.pairing.get(
                    harness.slave.config.nodeId).connectionId == initial_id)
                harness.slave.unpair_peer(harness.master.config.nodeId)
                check('从机内部撤销立即关闭 PTY 并停止通讯', not harness.backend.live_handles()
                      and not connected(harness.slave))
                # 成功档复查从 5s 退避到 30s：从机单方撤销后，主控要等下一次验证
                # 往返才会发现（受控节奏是文件数量取舍的设计内容）。观察窗口按
                # OK 间隔 + 余量给足，钉住的属性不变：「自动重试不能恢复已解除连接」。
                check('旧主控自动重试不能恢复已解除连接', _wait_until(lambda:
                    not connected(harness.master) and not harness.master.devices(),
                    relay_node.CONNECTION_VERIFY_OK_INTERVAL_S + 8))
                old = relay.new_request(relay.KIND_CHECK_CONNECTION, 'pc-a', payload={
                    'sourceNodeId': harness.master.config.nodeId,
                    'targetNodeId': harness.slave.config.nodeId, 'connectionId': initial_id})
                check('已解除的旧确认请求被拒绝', bool(harness.slave.node._authorize(old)))
                check('主控重新确认产生新代次并自动接通', harness.master.pair_peer(
                    harness.slave.config.nodeId, peer_id='pc-b')[0]
                      and _wait_until(lambda: connected(harness.master) and connected(harness.slave)))
                check('恢复后仍拒绝退役代次，不能回滚当前连接', bool(harness.slave.node._authorize(old))
                      and harness.slave.node.pairing.get(harness.master.config.nodeId).connectionId != initial_id)
                check('主控解除通知从机关闭连接', harness.master.unpair_peer(
                    harness.slave.config.nodeId)[0] and not harness.backend.live_handles()
                      and not harness.slave.node.pairing.is_paired(harness.master.config.nodeId))
                _EXEC_LOG.clear()
                result = harness.master.remote_exec(key, ['shell', 'echo', 'after-revoke'], timeout=1)
                check('解除后设备命令继续被阻止', not result['success'] and not _EXEC_LOG)
                check('主控再次确认，无需从机操作即可恢复', harness.master.pair_peer(
                    harness.slave.config.nodeId, peer_id='pc-b')[0]
                      and _wait_until(lambda: connected(harness.master) and connected(harness.slave)))
            finally:
                harness.stop()

            idle = _Harness(os.path.join(directory, 'idle'), master_local_devices_enabled=False)
            idle.slave.local_devices_enabled = False
            try:
                idle.start()
                check('无设备场景也能发现对端', _wait_until(lambda: bool(idle.master.status()['pairing'])))
                idle.master.confirm_peer('pc-b')
                check('无设备也能主控一次确认后验证成功', _wait_until(lambda:
                    connected(idle.master) and connected(idle.slave)))
                check('连接验证不伪造设备或终端', not idle.master.devices()
                      and not idle.backend.live_handles() and not idle.master.sessions_snapshot())
            finally:
                idle.stop()
    return all(checks)


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
