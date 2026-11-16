"""跨机时钟偏差：请求过期判定按两端心跳折算，别让「钟差几分钟」打死整条中转。

真实故障：换了一台从机后所有请求回执都是 ``status=expired / 请求已过期，未执行``，
界面却因为心跳做了偏差补偿而显示"已连接"。原因是 ``Request.is_expired`` 拿主控写下的
``expiresAt`` 比从机本地 ``time.time()``，而 TTL 只有 30 秒 —— 从机钟快 214 秒时，
每条请求一落地就已经"过期 184 秒"。
"""
import datetime
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
from relay import relay_node


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='milliseconds')


def beat(peer_id, role, node_id, local_now, server_now):
    """心跳：``heartbeatAt`` 是写者本机钟，``fileMtimeMs`` 是服务器打戳。"""
    return relay.PeerHeartbeat(peerId=peer_id, role=role, nodeId=node_id,
                               fingerprint=peer_id, host=peer_id, heartbeatAt=iso(local_now),
                               fileMtimeMs=server_now * 1000)


def run():
    checks = []

    def check(name, value):
        print(('  [PASS] ' if value else '  [FAIL] ') + name)
        checks.append(bool(value))

    now = time.time()
    skew_s = 214.0       # 从机比主控（=服务器）快 214 秒
    peers = {'slave': beat('slave', 'slave', 'node-slave', now + skew_s, now),
             'master': beat('master', 'master', 'node-master', now, now)}

    with tempfile.TemporaryDirectory() as root:
        slave = relay_node.RelayNode(
            peer_id='slave', role='slave', root=root, node_id='node-slave',
            passive_slave=True, pairing_required=True, local_devices_enabled=False,
            trust_path=os.path.join(root, 'trust.json'),
            pairing_path=os.path.join(root, 'pair.json'))
        try:
            slave._peers = dict(peers)

            # 主控时钟写的请求（TTL 30s），从机收到的"现在"比它快 214s。
            request = relay.Request(reqId='r1', kind=relay.KIND_EXEC, peer='master',
                                    device={}, payload={}, createdAt=iso(now),
                                    expiresAt=iso(now + relay.DEFAULT_REQUEST_TTL_S))
            offset = slave._request_clock_offset_ms(request)
            check('两端时钟差按心跳量出（对端−本机 ≈ -214s）', abs(offset + skew_s * 1000) < 2000)
            check('反向证明：不折算时，钟差 214s 会把刚下发的请求判为过期',
                  request.is_expired(now=now + skew_s) is True)
            check('折算后刚下发的请求不再被误判',
                  request.is_expired(now=now + skew_s, offset_ms=offset) is False)

            # fail-closed 不能被顺手废掉：真正堆积的请求照样过期。
            stale = relay.Request.from_dict({**request.to_dict(),
                                            'createdAt': iso(now - 600),
                                            'expiresAt': iso(now - 600 + relay.DEFAULT_REQUEST_TTL_S)})
            check('堆积 10 分钟的请求折算后仍然过期（fail-closed 保留）',
                  stale.is_expired(now=now + skew_s, offset_ms=offset) is True)

            # 量不出来时退化成老行为，不能凭空放行。
            ghost = relay.Request.from_dict({**request.to_dict(), 'peer': 'ghost'})
            check('来源心跳缺失时折算量为 0（退化成各机自比）',
                  slave._request_clock_offset_ms(ghost) == 0.0)
            no_stamp = relay.PeerHeartbeat(peerId='slave', role='slave', heartbeatAt=iso(now))
            slave._peers = {'slave': no_stamp, 'master': peers['master']}
            check('非共享目录载体（无服务器 mtime）时折算量为 0',
                  slave._request_clock_offset_ms(request) == 0.0)
        finally:
            slave.stop()
    return all(checks)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
