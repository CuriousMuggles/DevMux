"""把「按一次键」的延迟拆成三段：输入确认(ack)、回显到达、以及每字符的文件操作次数。

同一套 GUI→中转→串口 路径，跑 fake 与 loop 两种设备，用于区分
「中转/共享目录开销」与「真实设备回显延迟」。
"""
import asyncio
import base64
from collections import Counter
from contextlib import nullcontext
import json
import os
from pathlib import Path
import statistics
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_serial_remote import RemoteTests


def _patch_counters(counter, latency=0.0):
    """统计数据面四个门面的调用次数，并可给每次共享目录操作注入固定时延。"""
    from unittest.mock import patch
    import relay.relay_stream as rs

    originals = {
        'writer_append': (rs.StreamWriter, 'append'),
        'tail_read_new': (rs.StreamTail, 'read_new'),
        'write_meta': (rs.StreamChannel, 'write_meta'),
        'read_meta': (rs.StreamChannel, 'read_meta'),
    }
    patches = []
    for label, (owner, name) in originals.items():
        def make(inner=getattr(owner, name), key=label):
            def wrapper(*args, **kwargs):
                counter[key] += 1
                if latency:
                    time.sleep(latency)
                return inner(*args, **kwargs)
            return wrapper
        patches.append(patch.object(owner, name, make()))
    for item in patches:
        item.start()
    return patches


class EchoBreakdown(RemoteTests):
    push_mode = False
    latency = 0.0

    async def test_measure(self):
        if self.mode == 'loop':
            import serial
            self.slave.connector = lambda port, config: serial.serial_for_url(
                'loop://', baudrate=config['baudrate'], timeout=.05, write_timeout=1)
        self.registry.call('serial', 'permission', {'allowed': True})

        # 从机侧插桩：每轮 pump 的准入耗时与轮数，用来判断固定开销占比。
        import serial_stream as ss
        auth_stats = {'calls': 0, 'seconds': 0.0, 'maxMs': 0.0}
        original_auth = ss.SlaveSerialStream.authorize

        def timed_auth(inner_self):
            begin = time.perf_counter()
            try:
                return original_auth(inner_self)
            finally:
                cost = time.perf_counter() - begin
                auth_stats['calls'] += 1
                auth_stats['seconds'] += cost
                auth_stats['maxMs'] = max(auth_stats['maxMs'], cost * 1000.0)

        ss.SlaveSerialStream.authorize = timed_auth
        self.pushes = []
        if self.push_mode:
            self.harness.master.on_serial_data = self.pushes.append
        context = (nullcontext() if self.push_mode else
                   __import__('unittest.mock', fromlist=['patch']).patch.dict(
                       os.environ, {'DEVMUX_SERIAL_PUSH': '0'}))
        try:
            with context:
                opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
                session = opened['sessionId']
                generation = opened['generation']
                cursor = 0

                warmup = 3
                rounds = 20
                ack_ms, echo_ms = [], []
                counter = Counter()
                patches = _patch_counters(counter, self.latency)
                try:
                    for index in range(warmup + rounds):
                        raw = bytes([0x41 + (index % 26)])  # 一次按键 = 一个可打印字符
                        mark = len(self.pushes)
                        t0 = time.perf_counter()
                        await self.gui('write', dict(
                            sessionId=session, generation=generation,
                            format='base64', data=base64.b64encode(raw).decode()))
                        t_ack = (time.perf_counter() - t0) * 1000.0
                        received = bytearray()
                        while len(received) < len(raw) and time.perf_counter() - t0 < 3:
                            if self.push_mode:
                                for item in self.pushes[mark:]:
                                    received.extend(base64.b64decode(item.get('data') or ''))
                                mark = len(self.pushes)
                            else:
                                output = await self.gui('read', dict(sessionId=session, cursor=cursor))
                                received.extend(base64.b64decode(output['data']))
                                cursor = output['nextCursor']
                            if len(received) < len(raw):
                                time.sleep(.002)
                        t_echo = (time.perf_counter() - t0) * 1000.0
                        self.assertEqual(bytes(received), raw)
                        if index >= warmup:
                            ack_ms.append(t_ack)
                            echo_ms.append(t_echo)
                finally:
                    for item in patches:
                        item.stop()
                await self.gui('close', {'sessionId': session})
        finally:
            ss.SlaveSerialStream.authorize = original_auth

        self.result = dict(
            mode=self.mode,
            pushMode=self.push_mode,
            latencyMs=round(self.latency * 1000.0, 2),
            samples=rounds,
            ackMode='queued' if self.push_mode else 'sync',
            ackP50=round(statistics.median(ack_ms), 2),
            ackP95=round(sorted(ack_ms)[int(rounds * .95) - 1], 2),
            ackMax=round(max(ack_ms), 2),
            echoP50=round(statistics.median(echo_ms), 2),
            echoP95=round(sorted(echo_ms)[int(rounds * .95) - 1], 2),
            echoMax=round(max(echo_ms), 2),
            # 每字符新增的文件操作次数（含热身轮的固定开销，看量级即可）
            fileOpsPerChar={name: round(count / (warmup + rounds), 2)
                            for name, count in counter.items()},
            # 从机每轮准入的耗时：若接近 ack 延迟，说明瓶颈是这个固定开销
            authCallsPerChar=round(auth_stats['calls'] / (warmup + rounds), 2),
            authAvgMs=round(auth_stats['seconds'] * 1000.0 / max(1, auth_stats['calls']), 3),
            authMaxMs=round(auth_stats['maxMs'], 2),
        )


def run(label, latency=0.0, push=False):
    results = {}
    for mode in ('fake', 'loop'):
        case = EchoBreakdown('test_measure')
        case.mode = mode
        case.latency = latency
        case.push_mode = push
        outcome = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite([case]))
        assert outcome.wasSuccessful(), outcome
        results[mode] = case.result
    environment = '同机临时共享目录、GUI→WS→中转→串口服务、921600/50ms'
    if latency:
        environment += f'、每次共享目录操作注入 {latency * 1000:.0f}ms'
    environment += '、推送模式' if push else '、轮询与同步回执'
    results['environment'] = environment
    output = ROOT / '.agent-runtime' / 'serial-echo'
    output.mkdir(parents=True, exist_ok=True)
    (output / (label + '.json')).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    args = sys.argv[1:]
    label = args[0] if args and not args[0].startswith('--') else 'baseline'
    latency_ms = 0.0
    if '--latency-ms' in args:
        latency_ms = float(args[args.index('--latency-ms') + 1])
    run(label, latency=latency_ms / 1000.0, push='--push' in args)
