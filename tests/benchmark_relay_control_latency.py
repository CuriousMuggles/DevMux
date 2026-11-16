"""同一口径量「远程控制面（ADB 类请求）」的往返，用于和串口回显延迟对照。

为什么量 ``check-connection``：它是最纯的一条控制面往返 —— 从机读到请求、立刻
写回执，不执行任何 adb 命令。所以它给出的就是「写请求 → 从机发现 → 写回执 →
主控读到」这一层的底座延迟，ADB 的 exec / 设备终端 / 串口都共用这一层。

对照结论（串口见 ``benchmark_serial_latency``）：
- 控制面按 ``poll_interval_s``（生产默认 0.25s）轮询请求；
- 串口数据面按 pump 的 15ms 节拍轮询。

这才是「串口单次往返更快、但连续交互更黏」的根源 —— 两者不是同一套节拍。

用法：``py tests/benchmark_relay_control_latency.py [poll_interval_s]``
"""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay  # noqa: E402
from relay import relay_node  # noqa: E402


def _wait(predicate, timeout=15.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def measure(poll_interval_s=0.25, rounds=25):
    with tempfile.TemporaryDirectory() as base:
        share = os.path.join(base, 'share')
        os.makedirs(share, exist_ok=True)
        nodes = {}
        for peer_id, role in (('pc-master', 'master'), ('pc-slave', 'slave')):
            nodes[peer_id] = relay_node.RelayNode(
                peer_id=peer_id, role=role, root=share,
                trust_path=os.path.join(base, f'{peer_id}-trust.json'),
                pairing_path=os.path.join(base, f'{peer_id}-pairing.json'),
                host_label=peer_id, host_fingerprint=f'fp-{peer_id}',
                heartbeat_interval_s=0.3, poll_interval_s=poll_interval_s,
                pairing_required=False, local_devices_enabled=False)
        master, slave = nodes['pc-master'], nodes['pc-slave']
        try:
            for node in (master, slave):
                ok, reason = node.start()
                if not ok:
                    raise SystemExit(f'节点启动失败：{reason}')
            if not _wait(lambda: 'pc-slave' in master._peers and 'pc-master' in slave._peers):
                raise SystemExit('两端未能互相发现（心跳未到达）')
            # 首次连接要两端各自确认身份，否则从机一律拒绝（准入门槛生效点）
            for node, other in ((master, 'pc-slave'), (slave, 'pc-master')):
                try:
                    node.confirm_peer(other)
                except Exception as exc:
                    print(f'确认 {other} 失败：{exc}', file=sys.stderr)
            # 让仲裁/健康判定先稳定一拍，避免第一次样本混进启动开销
            time.sleep(0.5)
            samples, errors = [], []
            for _ in range(rounds):
                start = time.perf_counter()
                # 走真实验证入口而不是裸 submit：它内部就是 check-connection 往返，
                # 同时把四时间戳喂给 SessionClock —— 顺带验证对时链路能跑通。
                result = master.verify_connection('pc-slave')
                samples.append((time.perf_counter() - start) * 1000.0)
                if result.status != relay.STATUS_OK:
                    errors.append(f'{result.status}: {result.error}')
                time.sleep(0.05)
            clock = master.session_clock.info('pc-slave')
        finally:
            for node in (master, slave):
                try:
                    node.stop()
                except Exception:
                    pass

    kept = samples[2:] or samples
    return {
        'pollIntervalS': poll_interval_s,
        'samples': len(kept),
        'p50Ms': round(statistics.median(kept), 2),
        'p95Ms': round(sorted(kept)[max(0, int(len(kept) * 0.95) - 1)], 2),
        'maxMs': round(max(kept), 2),
        'minMs': round(min(kept), 2),
        'errors': errors[:3],
        'clockSkewMs': clock.get('clockSkewMs'),
        'clockSamples': clock.get('clockSamples'),
        'environment': '同机临时共享目录、双实例（心跳+轮询全起）、无 adb 执行',
    }


if __name__ == '__main__':
    poll = float(sys.argv[1]) if len(sys.argv) > 1 else 0.25
    print(json.dumps(measure(poll), ensure_ascii=False, indent=2))
