"""授权仲裁使用共享时钟，慢共享目录回执仍可完成连接验证。"""
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_node


def run():
    checks = []
    def check(name, value):
        print(('  [PASS] ' if value else '  [FAIL] ') + name)
        checks.append(bool(value))
    with tempfile.TemporaryDirectory() as root:
        slave = relay_node.RelayNode(peer_id='slave', role='slave', root=root,
            node_id='node-slave', passive_slave=True, pairing_required=True,
            local_devices_enabled=False, trust_path=os.path.join(root, 'trust.json'),
            pairing_path=os.path.join(root, 'pair.json'))
        now = time.time()
        def beat(peer, role, node, age):
            return relay.PeerHeartbeat(peerId=peer, role=role, nodeId=node,
                fingerprint=peer, host=peer, heartbeatAt=relay.now_iso(),
                connectionScope=slave._connection_scope,
                fileMtimeMs=(now + 1200 - age) * 1000)
        slave._peers = {'slave': beat('slave', 'slave', 'node-slave', 0),
                        'old': beat('old', 'master', 'node-old', 60),
                        'new': beat('new', 'master', 'node-new', 0)}
        slave.pair_peer('node-old', peer_id='old')
        check('反向证明：漏传共享时钟会将离线主控判为在线冲突',
              relay.arbitrate_roles('slave', 'slave', slave._peers).conflict)
        request = relay.new_request(relay.KIND_CHECK_CONNECTION, 'new', payload={
            'sourceNodeId': 'node-new', 'targetNodeId': 'node-slave',
            'connectionId': f'{slave._connection_scope}.new-generation'})
        check('服务器快20分钟，离线旧主控不阻止新主控准入',
              slave._authorize(request) == '')
        request.kind = relay.KIND_EXEC
        check('普通设备请求授权同样不受离线旧主控阻止',
              slave._authorize(request) == '')
        slave.stop()

        nodes = []
        try:
            for name, role in [('master', 'master'), ('agent', 'slave')]:
                nodes.append(relay_node.RelayNode(peer_id=name, role=role,
                    root=os.path.join(root, 'slow'), node_id='node-' + name,
                    host_label=name, host_fingerprint=name,
                    local_devices_enabled=False, heartbeat_interval_s=0.5,
                    poll_interval_s=0.05, pairing_required=False,
                    trust_path=os.path.join(root, name + '-trust.json')))
            master, agent = nodes
            original = agent._serve_request
            def delayed(path, request):
                if request.kind == relay.KIND_CHECK_CONNECTION:
                    time.sleep(2.5)
                original(path, request)
            agent._serve_request = delayed
            for node in nodes:
                node.start()
            check('延迟场景双端已发现', master.wait_for_peers() and agent.wait_for_peers())
            master.confirm_peer('agent')
            agent.confirm_peer('master')
            started = time.monotonic()
            result = master.verify_connection('agent')
            check('真实回执晚于2秒仍完成验证',
                  result.ok and time.monotonic() - started >= 2.5
                  and master.connection_ready('agent'))
        finally:
            for node in nodes:
                node.stop()
    return all(checks)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
