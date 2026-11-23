"""远程串口推送链路：主控读取线程、异步输入、失败锁存、录制与降级开关。"""
import asyncio
import base64
import os
import sys
from pathlib import Path
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_serial_remote import RemoteTests


class PushTests(RemoteTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pushes = []
        self.harness.master.on_serial_data = self.pushes.append
        self.registry.call('serial', 'permission', {'allowed': True})

    def client(self, session_id):
        key = (self.harness.slave.config.nodeId, session_id, 'gui')
        return self.harness.master._serial_clients[key]

    async def wait_push(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in list(self.pushes):
                if predicate(item):
                    return item
            await asyncio.sleep(.02)
        raise AssertionError('等待推送超时')

    async def test_gui_push_open_write_and_echo(self):
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid = opened['sessionId']
        self.assertEqual(opened.get('serialPushProtocol'), 1)
        result = await self.gui('write', {'sessionId': sid, 'generation': opened['generation'],
                                          'format': 'text', 'data': 'A'})
        self.assertTrue(result.get('queued'))
        self.assertNotIn('writtenBytes', result)
        message = await self.wait_push(lambda item: item.get('type') == 'serial-data'
                                       and base64.b64decode(item.get('data') or '') == b'A')
        self.assertEqual(message['key'], f'serial:computer:{self.harness.slave.config.nodeId}:{sid}')
        self.assertEqual(message['identity'], [False, self.harness.slave.config.nodeId, sid])
        self.assertEqual(message['state'], 'running')

    async def test_read_uses_memory_buffer_without_consuming_stream(self):
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid = opened['sessionId']
        await self.gui('write', {'sessionId': sid, 'generation': opened['generation'],
                                 'format': 'text', 'data': 'B'})
        await self.wait_push(lambda item: base64.b64decode(item.get('data') or '') == b'B')
        first = await self.gui('read', {'sessionId': sid, 'cursor': 0})
        again = await self.gui('read', {'sessionId': sid, 'cursor': 0})
        self.assertEqual(first['data'], again['data'])
        self.assertEqual(first['nextCursor'], again['nextCursor'])
        self.assertEqual(base64.b64decode(first['data']), b'B')

    async def test_gui_burst_input_async_ack(self):
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid, generation = opened['sessionId'], opened['generation']
        text = 'ABCDEFGHIJKLMNOPQRST'
        start = time.perf_counter()
        for char in text:
            data = await self.gui('write', {'sessionId': sid, 'generation': generation,
                                           'format': 'text', 'data': char})
            self.assertTrue(data.get('queued'))
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.5)
        stream = self.client(sid)
        deadline = time.time() + 3
        while time.time() < deadline and stream.meta.get('ackSeq', 0) < len(text):
            await asyncio.sleep(.02)
        self.assertEqual(stream.meta.get('ackSeq'), len(text))
        self.assertEqual(b''.join(self.slave_ports[0][2].writes), text.encode())

    async def test_ack_failure_latches_and_pushes(self):
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid = opened['sessionId']
        await self.gui('write', {'sessionId': sid, 'generation': 'stale-generation',
                                 'format': 'text', 'data': 'X'})
        message = await self.wait_push(lambda item: item.get('inputUncertain') is True)
        self.assertTrue(message.get('ackError'))
        self.assertTrue(self.client(sid).uncertain)
        with self.assertRaises(Exception):
            # 锁存后禁止继续输入，避免静默丢失。
            await self.gui('write', {'sessionId': sid, 'generation': opened['generation'],
                                     'format': 'text', 'data': 'Y'})

    async def test_recording_is_fed_by_push(self):
        self.harness.master.on_serial_data = self.manager._on_serial_data
        self.manager.loop = None
        owner = object()
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid = opened['sessionId']
        identity = (False, self.harness.slave.config.nodeId, sid)
        self.manager._serial_owner_add(identity, owner)
        path = Path(self.root) / 'push.log'
        await asyncio.to_thread(self.manager.serial_recordings.start, owner, identity,
                                str(path), 0, 'text', 'utf-8', False)
        await self.gui('write', {'sessionId': sid, 'generation': opened['generation'],
                                 'format': 'text', 'data': 'C'})
        deadline = time.time() + 3
        while time.time() < deadline and not (path.exists() and path.read_text(encoding='utf-8')):
            await asyncio.sleep(.02)
        self.assertEqual(path.read_text(encoding='utf-8'), 'C')

    async def test_close_stops_reader_thread(self):
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        sid = opened['sessionId']
        thread = self.client(sid).thread
        self.assertIsNotNone(thread)
        await self.gui('close', {'sessionId': sid})
        deadline = time.time() + 3
        while thread.is_alive() and time.time() < deadline:
            await asyncio.sleep(.05)
        self.assertFalse(thread.is_alive())

    async def test_env_switch_disables_push(self):
        with patch.dict(os.environ, {'DEVMUX_SERIAL_PUSH': '0'}):
            opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
            self.assertNotIn('serialPushProtocol', opened)
            result = await self.gui('write', {'sessionId': opened['sessionId'],
                                              'generation': opened['generation'],
                                              'format': 'text', 'data': 'D'})
            self.assertNotIn('queued', result)
            self.assertEqual(result.get('writtenBytes'), 1)

    async def test_legacy_slave_keeps_polling_path(self):
        # 去掉请求里的持续流协商，等价于旧从机：打开结果不带 serialStreamProtocol。
        node = self.harness.slave.node
        original = node._serve_extension_tool

        def legacy(path, request):
            (request.payload or {}).pop('serialStreamProtocol', None)
            return original(path, request)

        node._serve_extension_tool = legacy
        opened = await self.gui('open', {'port': 'COM3', 'requestId': uuid.uuid4().hex})
        self.assertNotIn('serialPushProtocol', opened)
        result = await self.gui('write', {'sessionId': opened['sessionId'],
                                          'generation': opened['generation'],
                                          'format': 'text', 'data': 'E'})
        self.assertNotIn('queued', result)
        self.assertEqual(result.get('writtenBytes'), 1)

    async def test_broadcast_filters_by_owner(self):
        sent = {'owner': [], 'other': []}

        class Client:
            def __init__(self, name, sessions):
                self.name, self._serial_gui_sessions = name, sessions

            async def send(self, message):
                sent[self.name].append(message)

        identity = (False, 'node-b', 'a' * 32)
        manager = self.manager
        manager.clients = {Client('owner', {identity}), Client('other', set())}
        await manager._broadcast({'type': 'serial-data', 'identity': list(identity), 'data': ''})
        self.assertEqual(len(sent['owner']), 1)
        self.assertEqual(sent['other'], [])


_OWN_TESTS = (
    'test_gui_push_open_write_and_echo',
    'test_read_uses_memory_buffer_without_consuming_stream',
    'test_gui_burst_input_async_ack',
    'test_ack_failure_latches_and_pushes',
    'test_recording_is_fed_by_push',
    'test_close_stops_reader_thread',
    'test_env_switch_disables_push',
    'test_legacy_slave_keeps_polling_path',
    'test_broadcast_filters_by_owner',
)


def load_tests(loader, tests, pattern):
    """PushTests 继承 RemoteTests 只为复用一主一从夹具，不重复跑基类用例。"""
    return unittest.TestSuite(PushTests(name) for name in _OWN_TESTS)


def run():
    """run_all 入口：只跑本模块自有用例，不重复执行基类用例。"""
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(PushTests(name) for name in _OWN_TESTS))
    return result.wasSuccessful()


if __name__ == '__main__':
    unittest.main()
