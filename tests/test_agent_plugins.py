"""Authenticated MCP plugin lifecycle in a disposable store; no device operations."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import websockets
import agent_api
import agent_client
import agent_mcp
from plugin_manager import PluginManager
from plugin_pack import pack
import server

ROOT = Path(__file__).resolve().parents[1]


class AgentPluginsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = server.ManagerServer.__new__(server.ManagerServer)
        self.manager.plugins = PluginManager(self.root / 'plugins')
        self.manager._agent_token = 'isolated-plugin-token'
        self.manager._agent_instance = 'isolated-plugin-instance'
        async def blocking(function, *args):
            return await asyncio.to_thread(function, *args)
        self.manager._run_blocking = blocking
        self.manager.agent = agent_api.AgentAPI(self.manager)
        self.endpoint = await websockets.serve(self.manager._agent_handler, '127.0.0.1', 0)
        self.port = self.endpoint.sockets[0].getsockname()[1]
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'instance.json').write_text(json.dumps(dict(pid=os.getpid(), wsPort=self.port,
            instanceId=self.manager._agent_instance, token=self.manager._agent_token)), encoding='utf-8')
        self.environment = patch.dict(os.environ, {'ADB_AGENT_RUNTIME_DIR': str(runtime)})
        self.environment.start()
        self.package = base64.b64encode(pack(ROOT / 'plugins/example', self.root / 'example.dmp').read_bytes()).decode()

    async def asyncTearDown(self):
        self.endpoint.close()
        await self.endpoint.wait_closed()
        await asyncio.to_thread(self.manager.plugins.close)
        self.environment.stop()
        self.temp.cleanup()

    async def call(self, action, failed=False, **args):
        response = await agent_mcp.dispatch(dict(jsonrpc='2.0', id=1, method='tools/call',
            params=dict(name='plugins_manage', arguments=dict(action=action, **args))))
        self.assertEqual(response['result']['isError'], failed, response)
        return json.loads(response['result']['content'][0]['text']) if not failed else response

    async def test_lifecycle_update_and_verification(self):
        listed = await self.call('list')
        self.assertEqual(listed['data']['plugins'], [])
        installed = await self.call('install', package=self.package)
        self.assertEqual(installed['data']['plugins'][0]['version'], '1.0.0')
        enabled = await self.call('enable', id='example')
        self.assertTrue(enabled['data']['plugins'][0]['enabled'])
        await self.call('install', failed=True, package=self.package)
        await self.call('uninstall', failed=True, id='example')
        disabled = await self.call('disable', id='example')
        self.assertFalse(disabled['data']['plugins'][0]['enabled'])
        await self.call('install', package=self.package)
        removed = await self.call('uninstall', id='example')
        self.assertEqual(removed['data']['plugins'], [])

    async def test_active_task_protection(self):
        await self.call('install', package=self.package)
        await self.call('enable', id='example')
        process = self.manager.plugins.running['example']
        original = process.call
        def active(action, *args, **kwargs):
            return False if action == '__can_stop__' else original(action, *args, **kwargs)
        with patch.object(process, 'call', side_effect=active):
            await self.call('disable', failed=True, id='example')
        self.assertTrue((await self.call('list'))['data']['plugins'][0]['enabled'])

    async def test_parameters_and_remote_boundary(self):
        for args in ({'action':'list','target':'computer:slave'}, {'action':'permission'},
                     {'action':'enable'}, {'action':'install','package':False},
                     {'action':'list','id':'example'}, {'action':'enable','id':'../escape'}):
            response = await agent_mcp.dispatch(dict(jsonrpc='2.0', id=2, method='tools/call',
                params=dict(name='plugins_manage', arguments=args)))
            self.assertTrue(response['result']['isError'], response)
        await self.call('install', failed=True, package='not-base64')
        await self.call('enable', failed=True, id='missing')
        self.assertEqual((await self.call('list'))['data']['plugins'], [])

    async def test_authentication_and_origin(self):
        for origin in (None, 'https://foreign.example'):
            async with websockets.connect(f'ws://127.0.0.1:{self.port}/agent', origin=origin) as ws:
                if origin is None:
                    await ws.send(json.dumps({'type':'agent-auth','token':'wrong'}))
                with self.assertRaises(websockets.exceptions.ConnectionClosed):
                    await ws.recv()
        self.assertEqual((await self.call('list'))['data']['plugins'], [])


def run():
    import sys
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])).wasSuccessful()
