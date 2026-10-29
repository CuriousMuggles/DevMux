"""认证 Agent/MCP/CLI 经过真实中转访问扩展，硬件与烧写执行全部模拟。"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import websockets
import agent_api
import agent_client
import agent_mcp
import server
from db_flash import DBFlashService, SimulationExecutor
from db_flash_extension import DBFlashExtension
from extension_tools import ExtensionTools, RemoteToolError
from usb_relay import USBRelayService
from tests.test_db_flash_remote import fixture
from tests.test_usb_relay import tool_files
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


class ExtensionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-extension-')
        self.root = Path(self.temp.name)
        self.tools = self.root / 'DB固化包'; fixture(self.tools)
        self.executor = SimulationExecutor(delay=.1)
        self.db = DBFlashService(self.executor)
        self.adapter = DBFlashExtension(self.db, self.root / 'slave-db.json')
        self.local_db = DBFlashService(SimulationExecutor(delay=.02))
        self.local_adapter = DBFlashExtension(self.local_db, self.root / 'local-db.json')
        self.local_relay = USBRelayService(self.root / 'local-relay.json')
        self.remote_relay = USBRelayService(self.root / 'slave-relay.json')
        self.local = ExtensionTools({'relay': self.local_relay, 'db-flash': self.local_adapter})
        self.remote = ExtensionTools({'relay': self.remote_relay, 'db-flash': self.adapter})
        self.mask, self.commands = 0, []
        def probe(action, serial=''):
            if action == 'set':
                channel = self.remote_relay._probe_channel
                operation = self.remote_relay._probe_operation
                self.commands.append((serial, channel, operation))
                bits = 3 if channel == 255 else 1 << (channel-1)
                self.mask = self.mask | bits if operation == 'open' else self.mask & ~bits
                return {'success':True, 'mask':self.mask}
            return {'devices':[{'serial':'RELAY1234','channels':2}]} if action == 'scan' else {'mask':self.mask}
        def control(argv, **kwargs):
            assert Path(argv[0]).name == 'CommandApp_USBRelay.exe'
            self.commands.append(list(argv)); channel = int(argv[-1])
            bits = 3 if channel == 255 else 1 << (channel-1)
            self.mask = self.mask | bits if argv[-2] == 'open' else self.mask & ~bits
            return SimpleNamespace(returncode=0)
        self.hardware = patch.multiple(self.remote_relay, _probe=probe, _run_child=control)
        self.hardware.start()
        self.nodes = _patch(list_devices=_fake_list_devices, execute=_fake_execute); self.nodes.__enter__()
        self.harness = _Harness(str(self.root / 'bus'), master_local_devices_enabled=False)
        self.harness.slave.extension_tools = self.remote
        self.harness.start(); self.harness.confirm_both()
        assert _wait_until(lambda:self.harness.master.node.connection_ready('pc-b'))
        async def invoke(function, *args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)
        self.manager = server.ManagerServer.__new__(server.ManagerServer)
        self.manager.extension_tools = self.local
        self.manager.relay = self.harness.master
        self.manager._merged_devices = lambda:[]
        self.manager._run_blocking = invoke
        self.manager._agent_token = uuid.uuid4().hex
        self.manager._agent_instance = uuid.uuid4().hex
        self.manager.agent = agent_api.AgentAPI(self.manager)
        self.drop = False
        async def handler(ws):
            if not self.drop:
                return await self.manager._agent_handler(ws)
            self.drop = False
            auth = json.loads(await ws.recv())
            assert auth['token'] == self.manager._agent_token
            await ws.send(json.dumps({'type':'agent-auth','success':True}))
            message = json.loads(await ws.recv())
            await self.manager.agent.call(message['name'], message['arguments'])
            await ws.close()  # 模拟动作已接受但响应丢失。
        self.endpoint = await websockets.serve(handler, '127.0.0.1', 0)
        runtime = self.root / 'runtime'; runtime.mkdir()
        (runtime/'test.json').write_text(json.dumps({'pid':os.getpid(), 'instanceId':self.manager._agent_instance,
            'token':self.manager._agent_token, 'wsPort':self.endpoint.sockets[0].getsockname()[1]}),encoding='utf-8')
        self.environment = patch.dict(os.environ, {'ADB_AGENT_RUNTIME_DIR':str(runtime)})
        self.environment.start()
        self.target = 'computer:' + self.harness.slave.config.nodeId

    async def asyncTearDown(self):
        self.endpoint.close(); await self.endpoint.wait_closed()
        self.environment.stop()
        await asyncio.to_thread(self.harness.stop)
        self.nodes.__exit__(None,None,None); self.hardware.stop()
        self.db.close(); self.local_db.close()
        self.temp.cleanup()

    async def call(self, tool, action, arguments=None, target=None):
        return await agent_client.call('extension_call', {'target':target or self.target,
            'tool':tool, 'action':action, 'arguments':arguments or {}})

    def result(self, response):
        self.assertTrue(response['success'],response)
        self.assertTrue(response['data']['success'],response)
        return response['data']['data']

    async def test_discovery_mcp_cli_and_validation(self):
        listed = await agent_mcp.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/list'})
        names = {item['name'] for item in listed['result']['tools']}
        self.assertTrue({'extensions_list','extension_capabilities','extension_call'} <= names)
        discovered = await agent_client.call('extensions_list',{})
        self.assertEqual({item['tool'] for item in discovered['data']['tools']},{'relay','db-flash','mijia'})
        caps = await agent_client.call('extension_capabilities',{'target':self.target,'tool':'db-flash'})
        data = self.result(caps)
        self.assertFalse(data['allowed'])
        self.assertIn('start', data['agentActions'])
        self.assertIn('requestId',data['agentActions']['start']['inputSchema']['required'])
        cli = await asyncio.to_thread(subprocess.run,[sys.executable,'-B',str(ROOT/'agent_cli.py'),
            'extensions_list'],capture_output=True,text=True,encoding='utf-8',timeout=15,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(cli.returncode,0,cli.stderr)
        self.assertEqual(len(json.loads(cli.stdout)['data']['tools']),3)
        with self.assertRaises(ValueError):
            await self.manager.agent.call('extension_call',{'target':self.target,'tool':'relay',
                'action':'set','arguments':False})
        class Future:
            NAME='未来工具'; ACTIONS={'ping'}; remote_allowed=False; _lock=threading.RLock()
            def call(self, action, arguments): return {'success':True,'data':arguments}
        self.local.services['future']=Future()
        self.assertIn('future',{item['tool'] for item in (await agent_client.call('extensions_list',{}))['data']['tools']})
        self.assertEqual(self.result(await self.call('future','ping',{'value':3},'computer:local')),{'value':3})
        with patch('agent_extensions.remote_call',side_effect=RemoteToolError('结果未确认',uncertain=True)):
            uncertain = await self.call('relay','set',{'channel':1,'operation':'open'})
        self.assertTrue(uncertain['data']['operationUncertain'])
        self.assertFalse(uncertain['data']['success'])

    async def test_relay_mcp_remote_permission_no_local_fallback(self):
        denied = await self.call('relay','scan')
        self.assertFalse(denied['data']['success']); self.assertFalse(denied['data']['operationUncertain'])
        self.remote_relay.set_remote_permission(True)
        tools = self.root / '继电器工具'; tool_files(tools)
        self.result(await self.call('relay','save',{'toolDir':str(tools)}))
        self.result(await self.call('relay','scan'))
        self.result(await self.call('relay','open',{'serial':'RELAY1234'}))
        response = await agent_mcp.dispatch({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'extension_call','arguments':{'target':self.target,'tool':'relay','action':'set',
                'arguments':{'channel':1,'operation':'open'}}}})
        self.assertFalse(response['result']['isError'],response)
        self.assertEqual(self.mask,1); self.assertEqual(len(self.commands),1)
        self.result(await self.call('relay','close'))
        self.assertEqual(self.mask,1,'关闭设备不能改变触点')
        self.assertEqual(self.local_relay.tool_dir,'','不能回退本机')
        forbidden = await self.call('relay','permission',{'allowed':False})
        self.assertFalse(forbidden['data']['success']); self.assertTrue(self.remote_relay.remote_allowed)
        self.remote_relay.set_remote_permission(False)
        self.assertFalse((await self.call('relay','set',{'channel':1,'operation':'close'}))['data']['success'])
        self.assertEqual(len(self.commands),1)
        self.result(await self.call('relay','permission',{'allowed':True},'computer:local'))
        self.assertTrue(self.local_relay.remote_allowed)

    async def test_db_workflow_identity_dedup_and_progress(self):
        self.adapter.set_remote_permission(True)
        self.result(await self.call('db-flash','save',{'toolDir':str(self.tools)}))
        self.assertIn('images',[item['name'] for item in self.result(await self.call('db-flash','browse',{'path':str(self.tools)}))['entries']])
        args = {'toolDir':str(self.tools),'selected':['system'],'options':{'prepare':False}}
        self.result(await self.call('db-flash','catalog',{'toolDir':str(self.tools)}))
        self.result(await self.call('db-flash','plan',args))
        started = await self.call('db-flash','start',args)
        job = self.result(started); request = started['data']['requestId']
        self.assertEqual(self.adapter.owner,self.harness.master.config.nodeId)
        same = self.result(await self.call('db-flash','start',{**args,'requestId':request}))
        self.assertEqual(same['jobId'],job['jobId'])
        await asyncio.to_thread(self.db._thread.join,3)
        count = len(self.executor.calls); self.assertGreater(count,0)
        status = self.result(await self.call('db-flash','status',{'requestId':request,'after':0}))
        self.assertLessEqual(len(status['events']),32)
        self.result(await self.call('db-flash','start',{**args,'requestId':request}))
        self.assertEqual(len(self.executor.calls),count)
        forbidden = await self.call('db-flash','start',{**args,'fastbootPath':'cmd.exe'})
        self.assertFalse(forbidden['data']['success']); self.assertEqual(len(self.executor.calls),count)
        self.assertFalse(self.remote.call('db-flash','cancel',{'jobId':job['jobId']},remote=True,
            authorize=lambda:'other-master')['success'])
        self.executor.delay = 1
        running = self.result(await self.call('db-flash','start',args))
        self.result(await self.call('db-flash','cancel',{'jobId':running['jobId']}))
        await asyncio.to_thread(self.db._thread.join,3)
        self.assertEqual(self.db.status()['state'],'cancelled')
        self.adapter.set_remote_permission(False)
        self.assertFalse((await self.call('db-flash','status',{'requestId':request}))['data']['success'])

    async def test_lost_response_tracks_start_and_unknown_cancel(self):
        args={'toolDir':str(self.tools),'selected':['system'],'options':{'prepare':False}}
        with self.assertRaises(ValueError):
            await self.manager.agent.call('extension_call',{'target':'computer:local','tool':'db-flash',
                'action':'start','arguments':args})
        self.assertFalse(self.local_db.executor.calls)
        self.drop = True
        lost = await self.call('db-flash','start',args,'computer:local')
        self.assertFalse(lost['success']); self.assertTrue(lost['data']['operationUncertain'])
        request = lost['data']['requestId']; self.assertEqual(len(request),32)
        status = self.result(await self.call('db-flash','status',{'requestId':request},'computer:local'))
        self.assertIn(status['state'],('running','succeeded'))
        unknown = uuid.uuid4().hex
        self.result(await self.call('db-flash','cancel',{'requestId':unknown},'computer:local'))
        rejected = self.result(await self.call('db-flash','start',{**args,'requestId':unknown},'computer:local'))
        self.assertEqual(rejected['state'],'cancelled','迟到 start 不能开始烧写')


def run():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ExtensionsTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
