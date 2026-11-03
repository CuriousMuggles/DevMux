"""验证单条消息处理失败不会中断整条连接。

背景：``ws_handler`` 的消息循环原先直接 ``await self._handle_message(...)``，
而 48 个分派分支里只有 9 个带 try。任一分支抛异常会穿出循环、回收连接，
用户看到的是「整页终端突然全掉」。这里钉住补上的兜底行为：

1. 带 id 的请求失败时，必须回一帧 ``exec-result`` 且保留原 id，前端 Promise 才能落地；
2. 不带 id 的单向通知失败时不回帧（前端没有等待者）；
3. 连接已断开时回帧失败不能再抛，否则兜底本身变成新的崩溃点。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeWS:
    def __init__(self, closed=False):
        self.sent = []
        self.closed = closed

    async def send(self, raw):
        if self.closed:
            raise ConnectionResetError('连接已断开')
        self.sent.append(json.loads(raw))


def run() -> bool:
    from server import ManagerServer

    async def scenario():
        # 只测回帧逻辑，不跑 __init__，避免起 worker 线程与中转。
        server = object.__new__(ManagerServer)

        with_id = _FakeWS()
        await ManagerServer._fail_message(
            server, with_id, {'type': 'exec-adb', 'id': 'req-7'}, ValueError('boom'))

        no_id = _FakeWS()
        await ManagerServer._fail_message(
            server, no_id, {'type': 'devices-updated'}, ValueError('boom'))

        closed = _FakeWS(closed=True)
        await ManagerServer._fail_message(
            server, closed, {'type': 'exec-adb', 'id': 'req-8'}, ValueError('boom'))

        return with_id.sent, no_id.sent, closed.sent

    sent, notify_sent, closed_sent = asyncio.run(scenario())
    frame = sent[0] if sent else {}

    checks = [
        ('带 id 的失败回一帧', len(sent) == 1),
        ('回帧类型为 exec-result', frame.get('type') == 'exec-result'),
        ('回帧保留原请求 id', frame.get('id') == 'req-7'),
        ('回帧标记失败', frame.get('success') is False),
        ('回帧带异常类型与信息', 'ValueError' in str(frame.get('output', ''))
         and 'boom' in str(frame.get('output', ''))),
        ('无 id 的通知不回帧', notify_sent == []),
        ('连接已断开时回帧不抛异常', closed_sent == []),
    ]

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
