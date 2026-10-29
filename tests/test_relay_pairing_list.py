"""配对下拉旧身份过滤与多实例保留回归。"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
from relay_node import RelayNode


def run():
    checks = []
    def check(name, value):
        checks.append(bool(value))
        print(('  [PASS] ' if value else '  [FAIL] ') + name, flush=True)
    with tempfile.TemporaryDirectory() as root:
        node = RelayNode(peer_id='master', role='master', root=root,
            node_id='node-master', host_label='PC-LOCAL', host_fingerprint='local-fp',
            local_devices_enabled=False, pairing_required=True,
            pairing_path=os.path.join(root, 'pairs.json'),
            trust_path=os.path.join(root, 'trust.json'))
        records = [
            relay.PairRecord(nodeId='old-local', peerId='old-local',
                host='pc-local', fingerprint='local-fp', state=relay.PAIR_CONFIRMED),
            relay.PairRecord(nodeId='revoked', peerId='revoked', state=relay.PAIR_REVOKED,
                retiredConnectionIds=['old-generation']),
            relay.PairRecord(nodeId='remote-offline', peerId='remote-offline',
                host='离线远端', fingerprint='remote-offline-fp', state=relay.PAIR_CONFIRMED),
            relay.PairRecord(nodeId='ghost', peerId='ghost', host='PC-LOCAL',
                fingerprint='local-fp', state=relay.PAIR_CONFIRMED),
        ]
        node.pairing._records = {r.nodeId: r for r in records}
        node.pairing._loaded = True
        node.pairing.save()
        original = Path(node.pairing.path).read_bytes()
        def beat(peer, instance, online=True, host='PC-LOCAL', fingerprint='local-fp'):
            return relay.PeerHeartbeat(peerId=peer, nodeId=instance, host=host,
                fingerprint=fingerprint, role='slave', startedAt=relay.now_iso(),
                heartbeatAt=relay.now_iso() if online else '2000-01-01T00:00:00+00:00',
                fileMtimeMs=time.time() * 1000 if online else 1)
        node._peers = {
            'master': beat('master', 'node-master'),
            'ghost': beat('ghost', 'ghost', False),
            'stale': beat('stale', 'stale', False),
            'live-1': beat('live-1', 'live-1'),
            'live-2': beat('live-2', 'live-2'),
            'renamed-old': beat('renamed-old', 'renamed', False, '远端', 'remote-fp'),
            'renamed-new': beat('renamed-new', 'renamed', True, '远端', 'remote-fp'),
        }
        node._health.selfGhosts = ['ghost', 'renamed-old']
        node._health.unpairedPeers = [{'nodeId': 'stale', 'peerId': 'stale', 'online': False}]
        # 注册来源和心跳同时出现，同一 nodeId 必须仍只有一条。
        entries = {'live-1': relay.RegistryEntry(nodeId='live-1', peerId='live-1',
            host='PC-LOCAL', fingerprint='local-fp', role='slave',
            fileMtimeMs=time.time() * 1000)}
        with patch.object(relay, 'read_registry', return_value=entries):
            views = node.pairing_records()
        ids = [item['nodeId'] for item in views]
        check('不显示本机和没有发现来源的本机历史配对', 'node-master' not in ids and 'old-local' not in ids)
        check('已被过滤的旧心跳不从配对记录重新出现', 'ghost' not in ids)
        check('离线且未配对的历史心跳自动退出候选列表', 'stale' not in ids)
        check('无发现来源的已撤销记录不占下拉列表', 'revoked' not in ids)
        check('已确认远端离线仍保留自动重连关系', 'remote-offline' in ids)
        check('同机同部署多个从机只显示一项', len({'live-1', 'live-2'} & set(ids)) == 1)
        check('注册表和心跳同一实例只显示一次', ids.count('live-1') == 1)
        check('同一实例改名保留在线的新身份', next((v['peerId'] for v in views if v['nodeId'] == 'renamed'), '') == 'renamed-new')
        check('自动清理显示不会删除确认或撤销代次', Path(node.pairing.path).read_bytes() == original
              and node.pairing.get('revoked').retiredConnectionIds == ['old-generation'])
        node._peer_deployments['live-2'] = 'another-deployment'
        with patch.object(relay, 'read_registry', return_value=entries):
            separate = node.pairing_records()
        check('同机不同部署分别显示，按部署区分',
              {'live-1', 'live-2'} <= {item['nodeId'] for item in separate})
    return all(checks)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
