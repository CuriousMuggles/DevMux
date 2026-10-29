"""主后端固化接口回归：Origin、固定路由、异步进度和取消，不执行设备命令。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import DBFlashService, SimulationExecutor, SPECS


class EmptyWorker:
    terminal_hub = None

    def get_state(self):
        return [], []

    def start(self):
        pass

    def stop(self):
        return True


class FakeWS:
    def __init__(self, origin):
        self.request = SimpleNamespace(headers={'Origin': origin})
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime = ROOT / '.agent-runtime/db-flash'
        runtime.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='server-test-', dir=runtime)
        self.directory = Path(self.temp.name)
        for name in ['flashall.bat', 'flash_all.sh', 'scripts/download_image.py',
                     'images/u-boot/u-boot.bin'] + [s.default for s in SPECS]:
            file = self.directory / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'fixture')
        self.config = self.directory / 'relay.json'
        self.config.write_text('{"enabled": false}', encoding='utf-8')
        self.env = patch.dict(os.environ, {'RELAY_CONFIG': str(self.config), 'ADB_MANAGER_NO_LOCAL_DEVICES': '1',
                                          'ADB_AGENT_RUNTIME_DIR': str(self.directory / 'runtime')})
        self.env.start()
        import server
        self.server = server
        self.patches = [
            patch.object(server, 'ADBWorker', lambda **_kwargs: EmptyWorker()),
            patch.object(server, 'log', lambda *_args: None),
            patch.object(server, '_HTTP_PORT', 8182),
            patch.object(server, 'RealExecutor', lambda: SimulationExecutor()),
            patch('subprocess.run', side_effect=AssertionError('接口测试禁止启动外部命令')),
            patch('subprocess.Popen', side_effect=AssertionError('接口测试禁止启动外部命令')),
        ]
        for item in self.patches:
            item.start()
        self.manager = server.ManagerServer()
        self.manager.loop = asyncio.get_running_loop()
        self.ws = FakeWS('http://127.0.0.1:8182')
        self.manager.clients.add(self.ws)

    async def asyncTearDown(self):
        await self.manager._run_blocking(self.manager.db_flash.close)
        self.manager._cancel_pending_broadcasts()
        self.manager.agent.close()
        self.manager.windows.close()
        self.manager._executor.shutdown(wait=True, cancel_futures=True)
        for item in reversed(self.patches):
            item.stop()
        self.env.stop()
        self.temp.cleanup()

    async def call(self, action, arguments=None, ws=None, **extra):
        ws = ws or self.ws
        id = len(ws.messages) + 1000
        await self.manager._handle_message(ws, {'type': 'db-flash', 'id': id, 'action': action,
                                               'arguments': arguments if arguments is not None else {}, **extra})
        return next(message for message in ws.messages if message.get('id') == id)

    async def test_origin_and_local_target_gate(self):
        for origin in ('', 'https://example.com', 'http://127.0.0.1:8183', 'http://127.0.0.1:bad'):
            response = await self.call('status', ws=FakeWS(origin))
            self.assertFalse(response['success'])
        self.assertFalse((await self.call('status', target='computer:remote'))['success'])
        self.assertTrue((await self.call('status'))['success'])

    async def test_catalog_arguments_and_status_remain_usable(self):
        response = await self.call('catalog', {'toolDir': str(self.directory)})
        self.assertTrue(response['success'])
        self.assertEqual(len(response['data']['items']), 7)
        self.assertFalse((await self.call('catalog', []))['success'])
        self.assertFalse((await self.call('execute-command', {'argv': ['fastboot']}))['success'])
        self.assertTrue((await self.call('status'))['success'])

    async def test_start_is_nonblocking_and_progress_marshaled(self):
        self.manager.db_flash = DBFlashService(SimulationExecutor(delay=.04), self.manager._broadcast_async)
        response = await self.call('start', {'toolDir': str(self.directory)})
        self.assertTrue(response['success'])
        job = response['data']
        self.assertEqual(job['state'], 'running')
        self.assertEqual(job['total'], 14)
        self.assertTrue((await self.call('status', {'jobId': job['jobId']}))['success'])
        for _ in range(150):
            if self.manager.db_flash.status()['state'] != 'running':
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.03)
        events = [m for m in self.ws.messages if m.get('type') == 'db-flash-progress']
        self.assertEqual(events[-1]['kind'], 'succeeded')
        self.assertTrue(all(m['jobId'] == job['jobId'] for m in events))
        self.assertEqual([m['sequence'] for m in events], list(range(1, len(events) + 1)))

    async def test_wrong_job_cancel_and_shutdown(self):
        self.manager.db_flash = DBFlashService(SimulationExecutor(delay=.5), self.manager._broadcast_async)
        response = await self.call('start', {'toolDir': str(self.directory)})
        job_id = response['data']['jobId']
        self.assertFalse((await self.call('cancel', {'jobId': 'wrong'}))['success'])
        self.assertTrue((await self.call('cancel', {'jobId': job_id}))['success'])
        await self.manager._run_blocking(self.manager.db_flash.close)
        self.assertFalse(self.manager.db_flash._thread.is_alive())
        self.assertEqual(self.manager.db_flash.status()['state'], 'cancelled')


def run():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ServerTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
