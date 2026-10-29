"""同一共享目录下各 pool 根隔离，单根支持一主多从。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay
import relay_app
import relay_node
from tests.test_relay_app import FakeBackend, _patch, _fake_list_devices, _fake_execute, _wait_until


def run() -> bool:
    checks = []

    def check(name, ok):
        checks.append(bool(ok))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')

    with tempfile.TemporaryDirectory(prefix='relay-root-isolation-') as directory:
        share = os.path.join(directory, 'share')
        output = []
        controllers = []
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            for name, pool, role in (
                ('master-a', 'pool-a', relay.ROLE_MASTER),
                ('slave-a1', 'pool-a', relay.ROLE_SLAVE),
                ('slave-a2', 'pool-a', relay.ROLE_SLAVE),
                ('master-b', 'pool-b', relay.ROLE_MASTER),
                ('slave-b', 'pool-b', relay.ROLE_SLAVE),
            ):
                path = os.path.join(directory, name + '.json')
                relay_app.save_config(path, relay_app.RelayConfig(
                    enabled=True, peerId=name, nodeId='node-' + name,
                    poolId=pool, root=share, role=role, heartbeatIntervalS=0.5,
                ))
                backend = FakeBackend() if role == relay.ROLE_SLAVE else None
                controllers.append(relay_app.RelayController(
                    config_path=path, adb_path='fake-adb', host_label=name,
                    host_fingerprint='fp-' + name, local_devices_enabled=role == relay.ROLE_SLAVE,
                    session_backend=backend,
                    on_terminal_output=lambda key, data: output.append(bytes(data)),
                ))
            master_a, slave_a1, slave_a2, master_b, slave_b = controllers
            try:
                for controller in controllers:
                    check('启动有效根 ' + controller.config.poolId, controller.start()[0])
                check('同一根发现主机和两个从机', _wait_until(lambda:
                    {x['nodeId'] for x in master_a.status()['pairing']}
                    == {slave_a1.config.nodeId, slave_a2.config.nodeId}))
                check('配对候选等待心跳序号推进后才可用', _wait_until(lambda:
                    {x['nodeId'] for x in master_a.status()['pairing']}
                    == {slave_a1.config.nodeId, slave_a2.config.nodeId}
                    and {x['nodeId'] for x in master_b.status()['pairing']}
                    == {slave_b.config.nodeId}
                    and all(item.get('online') for controller in (master_a, master_b)
                            for item in controller.status()['pairing'])))
                check('不同 pool 的主机和从机不出现在当前配对候选', _wait_until(lambda:
                    {x['nodeId'] for x in master_b.status()['pairing']} == {slave_b.config.nodeId}
                    and all(x['nodeId'] not in {master_b.config.nodeId, slave_b.config.nodeId}
                            for x in master_a.status()['pairing'])))
                roles_ok = _wait_until(lambda:
                    not slave_a1.status()['conflict']
                    and not slave_a2.status()['conflict']
                    and not slave_b.status()['conflict'])
                check('跨池主机不进入从机仲裁且无多主冲突', roles_ok)
                if not roles_ok:
                    print('    pool roles:', [(c.config.poolId, c.config.peerId,
                        c.status().get('onlineMasters'), c.status().get('conflict'),
                        c.status().get('state'), c.status().get('hint'))
                        for c in (slave_a1, slave_a2, slave_b)])
                check('数据通道根固定为本池，不跨池路由',
                      master_a.node.channel_root('slave-a1') == slave_a1.node.root
                      and master_a.node.channel_root('slave-b') == master_a.node.root)

                # 模拟同一共享根在从机本地盘符与主控UNC路径下具有不同本机摘要。
                # 共享通道保持同一目录；跨端校验不能拿从机本机路径摘要作比较。
                slave_a1.node._connection_scope = '0123456789abcdef'
                check('同根两端允许不同本机路径代次摘要',
                      slave_a1.node._connection_scope != master_a.node._connection_scope)

                confirm_a1 = master_a.confirm_peer('slave-a1')
                confirm_a2 = master_a.confirm_peer('slave-a2')
                confirm_b = master_b.confirm_peer('slave-b')
                check('主控可以分别确认同根多个从机', confirm_a1[0] and confirm_a2[0] and confirm_b[0])
                if not (confirm_a1[0] and confirm_a2[0] and confirm_b[0]):
                    print('    confirm results:', confirm_a1, confirm_a2, confirm_b)
                    print('    master-b candidates:', master_b.status()['pairing'])
                check('一主多从连接均建立', _wait_until(lambda:
                    all(controller.node.pairing.is_paired(master_a.config.nodeId)
                        for controller in (slave_a1, slave_a2))
                    and slave_b.node.pairing.is_paired(master_b.config.nodeId)))
                check('同一主控的两个从机连接验证均完成', _wait_until(lambda:
                    all(any(item.get('peerId') == peer and item.get('connectionState') == 'connected'
                            for item in master_a.status()['pairing'])
                        for peer in ('slave-a1', 'slave-a2')),
                    12))
                check('连接后各从机只将本池主机列为在线主机', _wait_until(lambda:
                    slave_a1.status()['onlineMasters'] == ['master-a']
                    and slave_a2.status()['onlineMasters'] == ['master-a']
                    and slave_b.status()['onlineMasters'] == ['master-b']))

                current_id = slave_a1.node.pairing.get(master_a.config.nodeId).connectionId
                wrong_scope = relay.new_request(relay.KIND_CHECK_CONNECTION, 'master-a', payload={
                    'sourceNodeId': master_a.config.nodeId,
                    'targetNodeId': slave_a1.config.nodeId,
                    'connectionId': 'fedcba9876543210.old-generation',
                })
                check('同来源其他根旧代次被拒绝且当前连接保留',
                      bool(slave_a1.node._authorize(wrong_scope))
                      and slave_a1.node.pairing.get(master_a.config.nodeId).connectionId == current_id)

                cross_request = relay.new_request(relay.KIND_CHECK_CONNECTION, 'master-b', payload={
                    'sourceNodeId': master_b.config.nodeId,
                    'targetNodeId': slave_a1.config.nodeId,
                    'connectionId': 'old-root-connection',
                })
                reason = slave_a1.node._authorize(cross_request)
                check('其他 pool 的旧连接请求在目标根被拒绝', bool(reason)
                      and not slave_a1.node.pairing.is_paired(master_b.config.nodeId))

                # 同一实例切换根目录后，配对存储键变化，旧连接代次不会自动带入新根。
                old_store = master_a._pairing_store()
                old_pair = old_store.get(slave_a1.config.nodeId)
                old_path = master_a._pairing_path_for(master_a.config.nodeId)
                master_a.config = relay_app.RelayConfig.from_dict({
                    **master_a.config.to_dict(), 'poolId': 'pool-b'})
                new_store = master_a._pairing_store()
                new_path = master_a._pairing_path_for(master_a.config.nodeId)
                check('切换有效根后配对记录和代次隔离', old_pair is not None
                      and old_pair.connectionId and old_path != new_path
                      and new_store.get(slave_a1.config.nodeId) is None)
            except Exception as exc:
                print(f'  [FAIL] 根目录隔离验证异常：{type(exc).__name__}: {exc}')
                checks.append(False)
            finally:
                for controller in reversed(controllers):
                    controller.stop()
    return all(checks)


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
