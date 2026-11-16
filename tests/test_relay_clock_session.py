"""会话级时钟估计：把跨机钟差显式测出来、单调化，并给建链/撤链留一条豁免通道。

前置背景见 ``test_relay_request_clock``（钟差 214 秒打死整条中转）。这里补的是
P1 的三件事：

1. 四时间戳往返能对消处理耗时，算出准确钟差；
2. 偏移变化分方向处理 —— 前进（会让请求提前过期）即时生效，倒退（会让已过期
   请求复活）按真实经过时间限速，突变（系统时间被外部改过）硬跳跟随；
3. ``check-connection`` / ``close-connection`` 不判时效：否则「要建链先对时、
   要对时先建链」会死锁，断电恢复后旧连接也永远解不掉。
"""
import datetime
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
from relay import relay_node, relay_session_clock as sc


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='milliseconds')


def run():
    checks = []

    def check(name, value):
        print(('  [PASS] ' if value else '  [FAIL] ') + name)
        checks.append(bool(value))

    # --- 1. 四时间戳：处理耗时对消 ---
    t1 = 1000.0
    offset_true = 300.0
    # 对称路径：上行 200ms、对端处理 500ms、下行 200ms
    t2 = t1 + offset_true + 0.2
    t3 = t2 + 0.5
    t4 = t3 - offset_true + 0.2
    offset_ms, rtt_ms = sc.handshake_offset_ms(t1, t2, t3, t4)
    check('对称路径下钟差精确（+300s，处理耗时对消）', abs(offset_ms - offset_true * 1000) < 1)
    check('往返 = (t4-t1)-(t3-t2) = 400ms', abs(rtt_ms - 400.0) < 1)

    # 对端处理耗时拉到 5 秒，结果必须不变（公式里 t3-t2 与 t3-t4 各自对消）
    t3b = t2 + 5.0
    t4b = t3b - offset_true + 0.2
    offset_ms2, _ = sc.handshake_offset_ms(t1, t2, t3b, t4b)
    check('处理耗时 5s 时钟差仍精确', abs(offset_ms2 - offset_true * 1000) < 1)

    # 不对称路径：误差界 = 上下行之差的一半（NTP 固有，消不掉，只能靠择优）
    asym, _ = sc.handshake_offset_ms(t1, t2, t2 + 0.5, t1 + 1.0)
    check('不对称路径误差落在「差值一半」以内（实测 ~-50ms）',
          abs(asym - offset_true * 1000 + 49.85) < 5)

    # --- 2. 方向性单调化 ---
    clock = sc.SessionClock()
    clock.observe_handshake('p', -1000.0, rtt_ms=500.0, now_mono=10.0)
    check('首个样本直接采纳', clock.offset_ms('p', now_mono=10.0) == -1000.0)

    # 前进（对端变快 / 本机变慢）：立即生效
    clock.observe_handshake('p', -500.0, rtt_ms=400.0, now_mono=12.0)
    check('偏移前进立即生效（安全方向）', clock.offset_ms('p', now_mono=12.0) == -500.0)

    # 倒退 400ms，距上次 0.2s：预算 = max(20, 50*0.2=10) = 20ms，只能退 20ms
    clock.observe_handshake('p', -900.0, rtt_ms=400.0, now_mono=12.2)
    applied = clock.offset_ms('p', now_mono=12.2)
    check('偏移倒退被限速（只退 20ms）', abs(applied - (-520.0)) < 1)

    # 继续倒推到同一目标，再给 2 秒：预算 100ms，退 100ms
    clock.observe_handshake('p', -900.0, rtt_ms=400.0, now_mono=14.2)
    applied = clock.offset_ms('p', now_mono=14.2)
    check('限额按真实经过时间累积（再退 100ms）', abs(applied - (-620.0)) < 1)

    # 突变 6 秒：必须硬跳，不能慢吞吞追
    clock.observe_handshake('p', -6620.0, rtt_ms=400.0, now_mono=14.4)
    applied = clock.offset_ms('p', now_mono=14.4)
    check('突变超过阈值时硬跳跟随', abs(applied - (-6620.0)) < 1)
    check('硬跳被计数', clock.clock('p').jumps == 1)

    # mtime 推导值只做展示：它随共享目录可用性停更，不能拿来判时效
    clock.observe_mtime('p', -20000.0, now_mono=14.5)
    check('mtime 推导值不改变判定用的偏移',
          abs(clock.offset_ms('p', now_mono=14.5) - (-6620.0)) < 1)
    check('两个来源不一致时值都可见（供界面告警）',
          abs(clock.info('p', now_mono=14.5)['clockMtimeSkewMs'] + 20000.0) < 1)

    # --- 3. 坏样本过滤 ---
    clock2 = sc.SessionClock()
    clock2.observe_handshake('q', 100.0, rtt_ms=200.0, now_mono=1.0)
    clock2.observe_handshake('q', 900.0, rtt_ms=5000.0, now_mono=2.0)   # 25 倍 RTT，不可信
    check('明显偏慢的往返样本被丢弃',
          abs(clock2.offset_ms('q', now_mono=2.0) - 100.0) < 1)
    clock2.observe_handshake('q', 160.0, rtt_ms=250.0, now_mono=3.0)    # 合理抖动
    check('正常抖动样本仍被采纳',
          abs(clock2.offset_ms('q', now_mono=3.0) - 160.0) < 1)

    # --- 4. 老化后退回各机自比 ---
    clock3 = sc.SessionClock()
    clock3.observe_handshake('r', -214000.0, rtt_ms=300.0, now_mono=100.0)
    check('老之前可用', clock3.offset_ms('r', now_mono=100.0 + 299) == -214000.0)
    check('超过同步有效期后返回 0（宁可退化，不拿陈旧值判时效）',
          clock3.offset_ms('r', now_mono=100.0 + 301) == 0.0)
    check('老化后 synced=false', clock3.synced('r', now_mono=100.0 + 301) is False)

    # --- 5. 探针豁免 + 端到端 ---
    check('豁免名单包含建链与撤链',
          relay.CLOCK_EXEMPT_KINDS == frozenset((relay.KIND_CHECK_CONNECTION,
                                                 relay.KIND_CLOSE_CONNECTION)))
    now = time.time()
    skew_s = 214.0
    with tempfile.TemporaryDirectory() as root:
        slave = relay_node.RelayNode(
            peer_id='slave', role='slave', root=root, node_id='node-slave',
            passive_slave=True, pairing_required=True, local_devices_enabled=False,
            trust_path=os.path.join(root, 'trust.json'),
            pairing_path=os.path.join(root, 'pair.json'))
        try:
            probe = relay.Request(reqId='c1', kind=relay.KIND_CHECK_CONNECTION, peer='master',
                                  device={}, payload={}, createdAt=iso(now),
                                  expiresAt=iso(now + relay.DEFAULT_REQUEST_TTL_S))
            work = relay.Request(reqId='e1', kind=relay.KIND_EXEC, peer='master',
                                 device={}, payload={}, createdAt=iso(now),
                                 expiresAt=iso(now + relay.DEFAULT_REQUEST_TTL_S))
            # 从机钟快 214 秒：不折算就全过期
            check('反向证明：普通请求在 214s 钟差下会被判过期',
                  work.is_expired(now=now + skew_s) is True)
            exempt_probe = slave._expiry_exempt(probe)
            exempt_work = slave._expiry_exempt(work)
            check('建链探针不判时效（否则永远建不起链）', exempt_probe is True)
            check('普通请求仍要判时效（fail-closed 没被放开）', exempt_work is False)

            # 会话级估计一旦建立，普通请求按折算后判定
            slave.session_clock.observe_handshake('master', -skew_s * 1000,
                                                  rtt_ms=800.0, now_mono=time.monotonic())
            offset = slave._request_clock_offset_ms(work)
            check('会话级估计被优先采用', abs(offset + skew_s * 1000) < 2000)
            check('折算后刚下发的普通请求不再被误判',
                  work.is_expired(now=now + skew_s, offset_ms=offset) is False)
            stale = relay.Request.from_dict({**work.to_dict(),
                                             'createdAt': iso(now - 600),
                                             'expiresAt': iso(now - 600 + relay.DEFAULT_REQUEST_TTL_S)})
            check('真堆积 10 分钟的请求折算后仍然过期',
                  stale.is_expired(now=now + skew_s, offset_ms=offset) is True)
        finally:
            slave.stop()

    return all(checks)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
