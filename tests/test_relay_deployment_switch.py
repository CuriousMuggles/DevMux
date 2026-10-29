"""切换有效共享根后旧链接失效，重新确认后恢复通讯。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


def run() -> bool:
    checks = []

    def check(name, ok):
        checks.append(bool(ok))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')

    with tempfile.TemporaryDirectory(prefix='relay-switch-') as directory:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            for paired in (False, True):
                scenario = '旧信任经人工确认' if not paired else '已有双向配对'
                harness = _Harness(os.path.join(directory, str(paired)), master_local_devices_enabled=False)
                try:
                    harness.start()
                    check(scenario + '：发现对端', _wait_until(lambda:
                        bool(harness.master.status()['pairing']) and bool(harness.slave.status()['pairing'])))
                    # 先让从机准入主控，避免主控同步开终端时触发首连拒绝与重试退避。
                    harness.slave.node.confirm_peer('pc-a')
                    harness.master.node.confirm_peer('pc-b')
                    if not paired:
                        harness.master.node.refresh()
                        check(scenario + '：旧信任不自动升级为配对',
                              not harness.master.node.pairing.is_paired(harness.slave.config.nodeId)
                              and not harness.backend.live_handles())
                    harness.master.pair_peer(harness.slave.config.nodeId, peer_id='pc-b')
                    check(scenario + '：初始终端可用', _wait_until(lambda: bool(harness.backend.live_handles())))
                    key = harness.remote_key()
                    initial_node_id = harness.slave.config.nodeId
                    initial_route = harness.slave.status()['peerId']
                    old_handle = harness.backend.handle_for('devB:tid=1')
                    old_stream = harness.master.sessions_snapshot()[0]['streamId']
                    old_connection = harness.master.node.pairing.get(initial_node_id).connectionId
                    check(scenario + '：从机切换到 test 生效', harness.slave.apply_config({'poolId': 'test'})[0])
                    check(scenario + '：实例身份与路由名保持不变', harness.slave.config.nodeId == initial_node_id
                          and harness.slave.status()['peerId'] == initial_route)
                    old_request = relay.new_request(relay.KIND_CHECK_CONNECTION, 'pc-a', payload={
                        'sourceNodeId': harness.master.config.nodeId,
                        'targetNodeId': initial_node_id,
                        'connectionId': old_connection,
                    })
                    check(scenario + '：新根不显示或接受旧根链接',
                          not harness.slave.node.pairing.is_paired(harness.master.config.nodeId)
                          and bool(harness.slave.node._authorize(old_request)))
                    check(scenario + '：主控切换到同一新根', harness.master.apply_config({'poolId': 'test'})[0])
                    check(scenario + '：新根下发现在线候选', _wait_until(lambda: any(
                        item.get('peerId') == 'pc-b' and item.get('online')
                        for item in harness.master.status()['pairing'])))
                    check(scenario + '：新根下必须重新确认', harness.master.confirm_peer('pc-b')[0]
                          and _wait_until(lambda: harness.master.node.pairing.is_paired(initial_node_id)
                              and harness.slave.node.pairing.is_paired(harness.master.config.nodeId)))
                    new_connection = harness.master.node.pairing.get(initial_node_id).connectionId
                    check(scenario + '：重连生成不同的连接代次', bool(new_connection)
                          and new_connection != old_connection
                          and bool(harness.slave.node._authorize(old_request)))
                    def live_handle():
                        return next((handle for handle in harness.backend.live_handles()
                                     if harness.backend.handles[handle]['device']['key'] == 'devB:tid=1'), '')

                    check(scenario + '：重新确认后重开终端且换用新 streamId', _wait_until(lambda:
                        live_handle() not in ('', old_handle)
                        and any(session.get('opened') and session['streamId'] != old_stream
                                for session in harness.master.sessions_snapshot())))
                    handle = live_handle()
                    harness.backend.emit(handle, b'after-switch\r\n')
                    check(scenario + '：新部署终端输出抵达主控', _wait_until(lambda:
                        b'after-switch' in harness.output_text()))
                    harness.master.session_input(key, b'after-switch-input\n')
                    check(scenario + '：新部署终端输入抵达 PTY', _wait_until(lambda:
                        b'after-switch-input' in harness.backend.written(handle)))
                    result = harness.master.remote_exec(key, ['shell', 'echo', 'after-switch'], timeout=2)
                    check(scenario + '：新根内命令正常执行', result.get('success'))
                    devices = harness.master.remote_devices()
                    check(scenario + '：从机显示机器名 / 部署标识', bool(devices)
                          and devices[0]['relay'].get('displayLabel') == 'pc-b / test')
                    check(scenario + '：从机状态按同一规则显示主控',
                          'pc-a / test' in harness.slave.status()['hint'])
                    harness.slave.unpair_peer(harness.master.config.nodeId)
                    harness.slave.node.refresh()
                    check(scenario + '：撤销记录不会被旧信任自动恢复',
                          not harness.slave.node.pairing.is_paired(harness.master.config.nodeId))
                    result = harness.master.remote_exec(key, ['shell', 'echo', 'revoked'], timeout=2)
                    check(scenario + '：从机单方解除仍拒绝命令', not result.get('success'))
                except Exception as exc:
                    print(f'  [FAIL] {scenario}：{type(exc).__name__}: {exc}')
                    checks.append(False)
                finally:
                    harness.stop()
    return all(checks)


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
