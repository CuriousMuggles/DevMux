"""跨机在线判定：心跳时效必须落在共享目录的时基上，同机旧标识不得冒充对端。

这条回归来自一次真实故障（两台 PC + 一台文件服务器）：从机界面显示「已连接」，
主控界面却显示「尚未发现在线的从机」，两台机器都认为自己对。取证结果是**两个
独立缺陷叠加**：

1. **时效判据绑在了墙上时钟上。** ``is_stale`` 拿对端写在文件里的 ``heartbeatAt``
   减本机时钟，而那是**对端机器**的墙上时钟。现场实测：共享服务器比主控快 669 秒，
   从机比主控慢 415 秒。于是主控把活着的从机算成「466 秒没心跳」判离线，从机却把
   主控的**未来**时间戳当成「刚心跳过」判在线 —— 快的一方永远看不到慢的一方，
   慢的一方反而看到幽灵。修法是换成**共享目录的 mtime**：它由服务器打戳，是所有端
   共同的时基；本机时钟靠自己的心跳记录归算过去（``observer_clock_skew_ms``）。
   这条判据只依赖共享目录，不要求对端升级，所以**只修一端也能修好单向可见**。
2. **对端机器的旧标识冒充第二个对端。** ``peers/`` 没有清理机制，一台机器改过
   「本机标识」之后旧文件会永远留着。主控自己看得到它（``is_self_ghost``），但从机
   看不到 —— 从机看到两台机器都声明自己是主控，用户读到的就是「从机找到两个主机」。

反向验证同样是硬要求：旧判据（拿对端时间戳减本机时钟）在这组数据上必须判出
「离线 / 幽灵」，否则说明测试没测到点上。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import relay  # noqa: E402 - 依赖上面的 sys.path

# 现场实测的三个时钟（单位：秒）。本机 = 共享目录服务器 - SERVER_AHEAD_S；
# 对端 PC = 本机 - PEER_BEHIND_S。三个时钟互不相同，正是问题的来源。
SERVER_AHEAD_S = 669.0
PEER_BEHIND_S = 415.0
INTERVAL_S = relay.DEFAULT_HEARTBEAT_INTERVAL_S


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone().isoformat(
        timespec='milliseconds')


def _write_beat(peers_dir, *, peer_id, role, beat_local_ms, mtime_ms,
                started_at_ms=None, fingerprint='', host='', pid=0, devices=()):
    """写一份心跳，并把文件 mtime 钉成给定值（模拟共享目录服务器打戳）。

    ``beat_local_ms`` 是**写者机器**的墙上时钟，``mtime_ms`` 是**服务器**的时钟 ——
    两者故意不一致，这才是跨机的常态。
    """
    payload = {
        'schema': 1, 'peerId': peer_id, 'role': role, 'appVersion': 'test',
        'pid': pid,
        'startedAt': _iso(started_at_ms if started_at_ms is not None else beat_local_ms),
        'heartbeatAt': _iso(beat_local_ms),
        'devices': [dict(d) for d in devices],
        'fingerprint': fingerprint, 'carrier': 'shared-folder', 'linkRttMs': 1.0,
        'host': host,
    }
    path = os.path.join(peers_dir, f'{peer_id}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(payload))
    stamp = mtime_ms / 1000.0
    os.utime(path, (stamp, stamp))
    return path


def _legacy_stale(beat, now: float, interval_s: float = INTERVAL_S) -> bool:
    """**修复前**的判据（对端时间戳 vs 本机时钟），只用于反向验证。

    故意在这里重写一遍而不是调用产品代码：「旧行为是什么」是这段历史的证据，
    不能因为产品代码改了而跟着变。
    """
    beat_ms = relay._epoch_ms(beat.heartbeatAt)
    if beat_ms <= 0:
        return True
    return now * 1000.0 - beat_ms > max(1.0, interval_s) * relay.HEARTBEAT_MISS_COUNT * 1000.0


def _peer_of(peers, peer_id):
    return peers[peer_id]


def run() -> bool:
    from unittest.mock import patch
    from dataclasses import replace
    observer = relay.HeartbeatSequenceObserver()
    sample = relay.PeerHeartbeat(peerId='sequence-slave', role=relay.ROLE_SLAVE,
                                 nodeId='node-sequence', pid=1, startedAt='generation-a',
                                 heartbeatAt='2000-01-01T00:00:00+00:00', fileMtimeMs=1,
                                 heartbeatSeq=100, uptimeSeconds=3600)
    first = observer.observe(sample, now=100)
    assert first.is_stale(), '初见旧序号不能证明在线'
    for tick in range(1, 21):
        observed = observer.observe(replace(sample, heartbeatSeq=100 + tick), now=100 + tick)
        with patch.object(relay.time, 'monotonic', return_value=100 + tick + .5):
            assert not observed.is_stale(now=1e15, skew_ms=-1e15), '递增心跳不应受墙钟和旧mtime影响'
    frozen = observer.observe(replace(sample, heartbeatSeq=120), now=130)
    with patch.object(relay.time, 'monotonic', return_value=123):
        assert not frozen.is_stale(), '恰好3秒仍在期限内'
    with patch.object(relay.time, 'monotonic', return_value=123.001):
        assert frozen.is_stale(), '超过3秒未推进应离线'
    rolled_back = observer.observe(replace(sample, heartbeatSeq=105), now=131)
    with patch.object(relay.time, 'monotonic', return_value=131):
        assert frozen.is_stale() and rolled_back.is_stale(), '同值和旧缓存不能延长在线期限'
    restarted = replace(sample, pid=2, startedAt='generation-b', heartbeatSeq=1, uptimeSeconds=0)
    assert observer.observe(restarted, now=132).is_stale(), '重启需要重新观察序号推进'
    recovered = observer.observe(replace(restarted, heartbeatSeq=2), now=133)
    with patch.object(relay.time, 'monotonic', return_value=133):
        assert not recovered.is_stale(), '重启后序号推进应恢复在线'
        assert observer.observe(sample, now=134).is_stale(), '旧启动代次不能复活'
    assert 'lastSequenceAdvanceMono' not in recovered.to_dict() and 'sequenceObserved' not in recovered.to_dict()
    print('  [PASS] 序号推进不受时钟与mtime影响；冻结、缓存回退、重启和旧代次符合预期')
    ok = True
    root = tempfile.mkdtemp(prefix='relay-liveness-')
    try:
        now = time.time()
        server_now_ms = (now + SERVER_AHEAD_S) * 1000.0   # 共享目录服务器时钟

        # ------------------------------------------------------------------
        # 场景 A：主控的视角 —— 对端 PC 比本机慢 415 秒，但它每 2 秒都在心跳
        # ------------------------------------------------------------------
        dir_a = os.path.join(root, 'a', 'peers')
        os.makedirs(dir_a, exist_ok=True)
        _write_beat(dir_a, peer_id='me', role=relay.ROLE_MASTER,
                    beat_local_ms=now * 1000.0, mtime_ms=server_now_ms,
                    pid=os.getpid(), fingerprint='fp-me', host='PC-A')
        _write_beat(dir_a, peer_id='EXAMPLE-HOST', role=relay.ROLE_SLAVE,
                    beat_local_ms=(now - PEER_BEHIND_S) * 1000.0,   # 对端自己的墙上时钟
                    mtime_ms=server_now_ms,                          # 刚被服务器打戳 = 活着
                    pid=11388, fingerprint='fp-peer', host='EXAMPLE-HOST',
                    devices=[{'serial': 'de47c9b2', 'key': 'de47c9b2:tid=104'}])

        peers_a = relay.read_peers(dir_a)
        health_a = relay.evaluate_health(
            enabled=True, role=relay.ROLE_MASTER, self_id='me', peers=peers_a,
            carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED},
            now=now,
        )
        slave = next((v for v in health_a.peers if v.peerId == 'EXAMPLE-HOST'), None)
        if slave is not None and slave.online:
            print(f'  [PASS] 时钟慢 415s 的对端仍判在线（本机与服务器差 {SERVER_AHEAD_S:.0f}s）')
        else:
            print('  [FAIL] 时钟慢 415s 的在跑对端被判离线 —— 时钟耦合没修掉：'
                  f'{slave.online if slave else "缺对端"}')
            ok = False
        if slave is not None and 0 <= slave.ageMs < INTERVAL_S * 1000 * 2:
            print(f'  [PASS] ageMs 是真实静默时长（{slave.ageMs:.0f}ms），不再被时钟差污染')
        else:
            print(f'  [FAIL] ageMs 仍被时钟差污染：{slave.ageMs if slave else "缺对端"}')
            ok = False
        if health_a.state == relay.STATE_CONNECTED and '已连接 1 个从机' in health_a.hint:
            print(f'  [PASS] 主控状态推出「已连接」：{health_a.hint}')
        else:
            print(f'  [FAIL] 主控状态不对：{health_a.state} / {health_a.hint}')
            ok = False

        # 反向验证：同一份记录，按修复前的判据必须被判离线（用户看到的就是这个）
        legacy_stale = _legacy_stale(_peer_of(peers_a, 'EXAMPLE-HOST'), now)
        if legacy_stale:
            print('  [PASS] 反向验证：修复前的判据在同样数据上判「离线」（故障可复现）')
        else:
            print('  [FAIL] 反向验证失败：旧判据竟然判在线，说明这组数据没复现故障')
            ok = False

        # ------------------------------------------------------------------
        # 场景 B：从机的视角 —— 它比服务器慢 1084 秒，看得到主控与一条旧标识
        # ------------------------------------------------------------------
        dir_b = os.path.join(root, 'b', 'peers')
        os.makedirs(dir_b, exist_ok=True)
        me_b_ms = (now - PEER_BEHIND_S) * 1000.0            # 从机的墙上时钟
        # 主控两个标识同一台机器（同指纹同主机）：`-a` 是当前身份，`HOST-EXAMPLE`
        # 是改标识之前的旧身份，静默在 `-a` 起来之前 —— 现场就是这样。
        master_started_ms = me_b_ms - 60 * 1000.0
        _write_beat(dir_b, peer_id='HOST-EXAMPLE-a', role=relay.ROLE_MASTER,
                    beat_local_ms=(now - 1.0) * 1000.0,     # 主控时钟 ≈ 本机时钟
                    mtime_ms=server_now_ms,
                    started_at_ms=master_started_ms,
                    pid=29160, fingerprint='fp-master', host='HOST-EXAMPLE',
                    devices=[{'serial': 'f62ca3df', 'key': 'f62ca3df:tid=1'}])
        _write_beat(dir_b, peer_id='HOST-EXAMPLE', role=relay.ROLE_MASTER,
                    beat_local_ms=master_started_ms - 15 * 60 * 1000.0,   # 旧身份的最后一拍
                    mtime_ms=server_now_ms - 16 * 60 * 1000.0,            # 也停在 16 分钟前
                    started_at_ms=master_started_ms - 25 * 60 * 1000.0,
                    pid=26888, fingerprint='fp-master', host='HOST-EXAMPLE',
                    devices=[{'serial': 'f62ca3df', 'key': 'f62ca3df:tid=1'}])
        _write_beat(dir_b, peer_id='EXAMPLE-HOST', role=relay.ROLE_SLAVE,
                    beat_local_ms=me_b_ms, mtime_ms=server_now_ms,
                    pid=11388, fingerprint='fp-slave', host='EXAMPLE-HOST',
                    devices=[{'serial': 'de47c9b2', 'key': 'de47c9b2:tid=104'}])

        peers_b = relay.read_peers(dir_b)
        # 注意 ``now`` 必须传**观察者自己的**时钟：从机比本进程慢 415 秒，它的
        # 「现在」就是 ``me_b_ms``。混用两个时钟就会得出「对端也离线」的假结论。
        health_b = relay.evaluate_health(
            enabled=True, role=relay.ROLE_SLAVE, self_id='EXAMPLE-HOST', peers=peers_b,
            carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED},
            accepted_master_ids=['HOST-EXAMPLE-a'], now=me_b_ms / 1000.0,
        )
        visible = [v.peerId for v in health_b.peers]
        if visible == ['HOST-EXAMPLE-a']:
            print(f'  [PASS] 同一台机器的旧标识不进对端列表：{visible}')
        else:
            print(f'  [FAIL] 对端列表里仍有多余的主控：{visible}')
            ok = False
        if health_b.supersededPeers == {'HOST-EXAMPLE': 'HOST-EXAMPLE-a'}:
            print(f'  [PASS] 旧标识被点名取代者，供界面解释：{health_b.supersededPeers}')
        else:
            print(f'  [FAIL] 未识别同机旧标识：{health_b.supersededPeers}')
            ok = False
        if '旧标识' in health_b.hint and 'EXAMPLE-HOST' not in health_b.hint:
            print(f'  [PASS] 提示里点名了旧标识：{health_b.hint}')
        else:
            print(f'  [FAIL] 提示没说清旧标识：{health_b.hint}')
            ok = False
        if health_b.masters == ['HOST-EXAMPLE-a'] and health_b.onlineMasters == ['HOST-EXAMPLE-a'] \
                and health_b.conflict is False and health_b.state == relay.STATE_CONNECTED:
            print('  [PASS] 旧标识不参与仲裁：唯一主控、无角色冲突、状态为已连接')
        else:
            print(f'  [FAIL] 旧标识仍影响仲裁：masters={health_b.masters} '
                  f'conflict={health_b.conflict} state={health_b.state}')
            ok = False

        # 反向验证：把旧标识放回仲裁（= 修复前的可见结果）必须复现「两个主机」
        legacy_arb = relay.arbitrate_roles(
            'EXAMPLE-HOST', relay.ROLE_SLAVE, peers_b, interval_s=INTERVAL_S,
            now=me_b_ms / 1000.0,
            skew_ms=relay.observer_clock_skew_ms(peers_b, 'EXAMPLE-HOST'),
        )
        if legacy_arb.masters == ['HOST-EXAMPLE', 'HOST-EXAMPLE-a'] \
                and legacy_arb.staleMasters == ['HOST-EXAMPLE']:
            print('  [PASS] 反向验证：不过滤旧标识时确实出现两台「主控」'
                  f'（{legacy_arb.masters}，其中 {legacy_arb.staleMasters} 已离线）')
        else:
            print(f'  [FAIL] 反向验证失败：不过滤时没有复现两个主机：'
                  f'{legacy_arb.masters} / {legacy_arb.staleMasters}')
            ok = False

        # 反向验证 2：从机视角按旧判据看主控 —— 未来时间戳被当成「刚心跳过」
        legacy_online_lie = not _legacy_stale(_peer_of(peers_b, 'HOST-EXAMPLE-a'), me_b_ms / 1000.0)
        if legacy_online_lie:
            print('  [PASS] 反向验证：旧判据把主控的未来时间戳当成新鲜（从机谎报「已连接」）')
        else:
            print('  [FAIL] 反向验证失败：旧判据没有复现「从机谎报已连接」')
            ok = False

        # ------------------------------------------------------------------
        # 场景 C：对端真的停了 —— 必须如实判离线，且 ageMs 说明停了多久
        # ------------------------------------------------------------------
        dir_c = os.path.join(root, 'c', 'peers')
        os.makedirs(dir_c, exist_ok=True)
        _write_beat(dir_c, peer_id='me', role=relay.ROLE_MASTER,
                    beat_local_ms=now * 1000.0, mtime_ms=server_now_ms,
                    pid=os.getpid(), fingerprint='fp-me', host='PC-A')
        _write_beat(dir_c, peer_id='pc-stopped', role=relay.ROLE_SLAVE,
                    beat_local_ms=(now - PEER_BEHIND_S - 85.0) * 1000.0,
                    mtime_ms=server_now_ms - 85.0 * 1000.0,   # 服务器时钟上停了 85 秒
                    pid=777, fingerprint='fp-peer', host='pc-stopped')
        peers_c = relay.read_peers(dir_c)
        health_c = relay.evaluate_health(
            enabled=True, role=relay.ROLE_MASTER, self_id='me', peers=peers_c,
            carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED}, now=now,
        )
        stopped = next((v for v in health_c.peers if v.peerId == 'pc-stopped'), None)
        if stopped is not None and not stopped.online:
            print('  [PASS] 停止心跳 85 秒的对端判离线（时钟差不再掩盖真实静默）')
        else:
            print(f'  [FAIL] 停止心跳的对端仍被判在线：{stopped.online if stopped else "缺对端"}')
            ok = False
        if stopped is not None and 80_000 < stopped.ageMs < 95_000:
            print(f'  [PASS] ageMs 如实说明静默时长（{stopped.ageMs / 1000:.0f}s；'
                  f'旧算法会报 {PEER_BEHIND_S + 85:.0f}s，把时钟差也算成静默）')
        else:
            print(f'  [FAIL] ageMs 不真实：{stopped.ageMs if stopped else "缺对端"}')
            ok = False

        # ------------------------------------------------------------------
        # 场景 D：刚被关掉的同机兄弟实例不是「旧标识」，必须留在列表里灰显
        # ------------------------------------------------------------------
        dir_d = os.path.join(root, 'd', 'peers')
        os.makedirs(dir_d, exist_ok=True)
        me_d_ms = now * 1000.0
        _write_beat(dir_d, peer_id='me', role=relay.ROLE_MASTER,
                    beat_local_ms=me_d_ms, mtime_ms=server_now_ms,
                    pid=os.getpid(), fingerprint='fp-me', host='PC-A')
        sibling_started_ms = now * 1000.0 - 3600_000.0        # 一小时前就起来了
        _write_beat(dir_d, peer_id='pc-main', role=relay.ROLE_MASTER,
                    beat_local_ms=me_d_ms, mtime_ms=server_now_ms,
                    started_at_ms=sibling_started_ms,
                    pid=1001, fingerprint='fp-shared', host='PC-B')
        # 同机另一个实例：5 分钟前停了 —— 它的最后一拍**晚于** pc-main 的启动时刻
        _write_beat(dir_d, peer_id='pc-main-2', role=relay.ROLE_SLAVE,
                    beat_local_ms=me_d_ms - 300_000.0,
                    mtime_ms=server_now_ms - 300_000.0,
                    started_at_ms=now * 1000.0 - 7200_000.0,
                    pid=1002, fingerprint='fp-shared', host='PC-B',
                    devices=[{'serial': 'x', 'key': 'x:tid=9'}])
        peers_d = relay.read_peers(dir_d)
        health_d = relay.evaluate_health(
            enabled=True, role=relay.ROLE_MASTER, self_id='me', peers=peers_d,
            carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED}, now=now,
        )
        names = [v.peerId for v in health_d.peers]
        if names == ['pc-main', 'pc-main-2'] and health_d.supersededPeers == {}:
            print(f'  [PASS] 刚关掉的同机兄弟实例仍在对端列表里（灰显等待设备保留）：{names}')
        else:
            print(f'  [FAIL] 误吞了同机兄弟实例：{names} / superseded={health_d.supersededPeers}')
            ok = False

        # ------------------------------------------------------------------
        # 场景 E：慢一拍（设备枚举卡了 5 秒）不该让健康对端闪离线
        # ------------------------------------------------------------------
        dir_e = os.path.join(root, 'e', 'peers')
        os.makedirs(dir_e, exist_ok=True)
        _write_beat(dir_e, peer_id='me', role=relay.ROLE_MASTER,
                    beat_local_ms=me_d_ms, mtime_ms=server_now_ms,
                    pid=os.getpid(), fingerprint='fp-me', host='PC-A')
        _write_beat(dir_e, peer_id='pc-slow', role=relay.ROLE_SLAVE,
                    beat_local_ms=now * 1000.0 - 7000.0,
                    mtime_ms=server_now_ms - 7000.0,   # 服务器时钟上距上一拍 7 秒
                    pid=555, fingerprint='fp-peer', host='pc-slow')
        peers_e = relay.read_peers(dir_e)
        slow = peers_e['pc-slow']
        if not slow.is_stale(INTERVAL_S, now):
            print(f'  [PASS] 距上一拍 7 秒仍在容差内（阈值 '
                  f'{INTERVAL_S * relay.HEARTBEAT_MISS_COUNT * 1000 + relay.HEARTBEAT_GRACE_MS:.0f}ms）')
        else:
            print('  [FAIL] 7 秒就被判离线：跨机容差没生效')
            ok = False
        if _legacy_stale(slow, now):
            print('  [PASS] 反向验证：旧判据在 7 秒上会判离线（跨机时确实会闪）')
        else:
            print('  [FAIL] 反向验证失败：旧判据没判离线，场景构造不对')
            ok = False

        # ------------------------------------------------------------------
        # 不变量：心跳文件的 mtime 只是读取期的旁注，绝不能写进文件
        # ------------------------------------------------------------------
        written = json.loads(open(os.path.join(dir_a, 'me.json'), encoding='utf-8').read())
        serialized = relay.PeerHeartbeat(peerId='me', role=relay.ROLE_MASTER,
                                         heartbeatAt=relay.now_iso()).to_dict()
        if 'fileMtimeMs' not in written and 'fileMtimeMs' not in serialized:
            print('  [PASS] fileMtimeMs 是读取期旁注，不落进心跳文件（对端不必升级）')
        else:
            print('  [FAIL] fileMtimeMs 被写进了心跳文件')
            ok = False
        if peers_a['me'].fileMtimeMs > 0:
            print('  [PASS] read_peers 给每条心跳记下了共享目录 mtime')
        else:
            print('  [FAIL] read_peers 没有记录 mtime')
            ok = False
        role_peers = {
            'live-master': relay.PeerHeartbeat(peerId='live-master', role=relay.ROLE_MASTER, heartbeatAt=relay.now_iso()),
            'live-slave': relay.PeerHeartbeat(peerId='live-slave', role=relay.ROLE_SLAVE, heartbeatAt=relay.now_iso()),
            'old-master': relay.PeerHeartbeat(peerId='old-master', role=relay.ROLE_MASTER,
                                             heartbeatAt='2000-01-01T00:00:00+08:00'),
        }
        roles = relay.evaluate_health(enabled=True, role=relay.ROLE_SLAVE, self_id='observer',
                                      peers=role_peers, link={'state':relay.STATE_CONNECTED})
        if roles.onlineMasters == ['live-master'] and roles.onlineSlaves == ['live-slave'] and not roles.conflict:
            print('  [PASS] 在线主从名单按实际角色区分，历史离线主控不引起冲突')
        else:
            print('  [FAIL] 在线主从名单或历史记录仲裁混用')
            ok = False
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
