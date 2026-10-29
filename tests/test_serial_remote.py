"""真实共享目录、认证 WS/MCP/CLI 串口回归；硬件为假字节端口。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import time
import statistics

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import websockets
import server
import agent_api
import agent_client
import agent_mcp
import relay
from extension_tools import ExtensionTools, remote_call
from tests.test_serial_terminal import service
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='serial-relay-'); self.root=Path(self.temp.name)
        self.shared_temp=None
        share=os.environ.get('ADB_SERIAL_TEST_SHARE_ROOT')
        if share:
            self.shared_temp=tempfile.TemporaryDirectory(prefix='serial-receipt-test-',dir=share)
            assert Path(self.shared_temp.name).resolve().is_relative_to(Path(share).resolve())
        self.local, self.local_ports = service(self.root/'local.json')
        self.slave, self.slave_ports = service(self.root/'slave.json')
        self.registry = ExtensionTools({'serial':self.slave})
        self.nodes = _patch(list_devices=_fake_list_devices,execute=_fake_execute); self.nodes.__enter__()
        bus=Path(self.shared_temp.name) if self.shared_temp else self.root
        self.harness = _Harness(str(bus/'bus'),master_local_devices_enabled=False)
        self.harness.slave.extension_tools=self.registry
        await asyncio.to_thread(self.harness.start)
        await asyncio.to_thread(self.harness.confirm_both)
        self.assertTrue(await asyncio.to_thread(_wait_until,lambda:self.harness.master.node.connection_ready('pc-b')))
        self.target='computer:'+self.harness.slave.config.nodeId
        self.manager=server.ManagerServer.__new__(server.ManagerServer)
        from serial_recording import SerialRecordings
        self.manager.serial_recordings=SerialRecordings(server.strip_ansi,server.add_recording_timestamps)
        self.manager.relay=self.harness.master
        self.manager.extension_tools=ExtensionTools({'serial':self.local})
        self.manager._merged_devices=lambda:[]
        self.manager._pending_relay_requests=lambda _ids:[]
        self.manager._agent_token=uuid.uuid4().hex; self.manager._agent_instance=uuid.uuid4().hex
        async def blocking(func,*args,**kwargs): return await asyncio.to_thread(func,*args,**kwargs)
        self.manager._run_blocking=blocking
        self.manager.agent=agent_api.AgentAPI(self.manager)
        self.endpoint=await websockets.serve(self.manager._agent_handler,'127.0.0.1',0)
        runtime=self.root/'runtime'; runtime.mkdir()
        (runtime/'serial.json').write_text(json.dumps({'pid':os.getpid(),'instanceId':self.manager._agent_instance,
            'token':self.manager._agent_token,'wsPort':self.endpoint.sockets[0].getsockname()[1]}),encoding='utf-8')
        self.env=patch.dict(os.environ,{'ADB_AGENT_RUNTIME_DIR':str(runtime)});self.env.start()

    async def asyncTearDown(self):
        self.endpoint.close(); await self.endpoint.wait_closed();self.env.stop()
        await asyncio.to_thread(self.harness.stop);self.nodes.__exit__(None,None,None)
        self.local.close();self.slave.close()
        # 会话关闭后的异步归档可能短暂持有本测试目录文件；仅重试Windows共享冲突。
        # 不忽略清理失败，也不删除本测试临时目录之外的内容。
        for temporary in (self.temp,self.shared_temp):
            if not temporary:continue
            expected=self.root if temporary is self.temp else Path(self.shared_temp.name)
            self.assertEqual(Path(temporary.name).resolve(),expected.resolve())
            deadline=time.monotonic()+3
            while True:
                try:
                    await asyncio.to_thread(temporary.cleanup)
                    break
                except PermissionError as exc:
                    if getattr(exc,'winerror',None)!=32 or time.monotonic()>=deadline:raise
                    await asyncio.sleep(.05)

    async def agent(self,action,args=None,target=None):
        response=await agent_client.call('extension_call',{'target':target or self.target,
            'tool':'serial','action':action,'arguments':args or {}})
        self.assertTrue(response['success'],response)
        self.assertTrue(response['data']['success'],response)
        return response['data']['data']

    async def gui(self,action,args=None):
        return (await asyncio.to_thread(remote_call,self.harness.master,self.harness.slave.config.nodeId,
            'serial',action,args or {},caller='gui'))['data']

    async def test_gui_remote_configure_same_port_and_stream_generation(self):
        """远程连接参数在原COM应用，持续流更新配置并拒绝旧代次输入。"""
        from extension_tools import RemoteToolError
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        sid=opened['sessionId'];wire=self.slave_ports[0][2]
        self.assertEqual(opened['serialConfigProtocol'],1)
        updated=await self.gui('configure',{'sessionId':sid,'generation':opened['generation'],
            'baudrate':115200,'bytesize':7,'parity':'E','stopbits':2,'flowcontrol':'rtscts','dtr':True,'rts':True})
        self.assertEqual(updated['sessionId'],sid);self.assertIs(self.slave.ports['COM3']['transport'],wire)
        self.assertEqual(len(self.slave_ports),1);self.assertFalse(self.local_ports)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            current=await self.gui('read',{'sessionId':sid,'cursor':0})
            if current['generation']==updated['generation']:break
            await asyncio.sleep(.02)
        self.assertEqual(current['config']['baudrate'],115200)
        with self.assertRaisesRegex(RemoteToolError,'代次'):
            await self.gui('write',{'sessionId':sid,'generation':opened['generation'],'data':'OLD'})
        await self.gui('write',{'sessionId':sid,'generation':updated['generation'],'data':'NEW'})
        self.assertEqual(wire.writes,[b'NEW']);self.assertTrue(wire.settings['rtscts'])
        await self.gui('close',{'sessionId':sid})
        self.assertTrue(wire.closed)

    async def test_gui_remote_recording_reads_once_and_disconnect_cleans(self):
        """真实共享目录的远程输出只记录在主控文件，重读游标不重复，断开释放日志及COM。"""
        from desktop_terminals import DesktopTerminals
        self.registry.call('serial','permission',{'allowed':True})
        self.manager.clients=set();self.manager.worker=SimpleNamespace()
        self.manager.loop=asyncio.get_running_loop()
        self.manager.desktop_terminals=DesktopTerminals(self.manager,server._HTTP_PORT)
        async with websockets.serve(self.manager.ws_handler,'127.0.0.1',0) as endpoint:
            async with websockets.connect('ws://127.0.0.1:'+str(endpoint.sockets[0].getsockname()[1]),
                    origin=f'http://127.0.0.1:{server._HTTP_PORT}') as socket:
                counter=900
                async def request(typ, **fields):
                    nonlocal counter
                    counter+=1
                    await socket.send(json.dumps(dict(type=typ,id=counter,target=self.target,**fields)))
                    while True:
                        result=json.loads(await socket.recv())
                        if result.get('id')==counter:
                            self.assertTrue(result['success'],result)
                            return result
                async def serial(action, **args):
                    return (await request('extension-tool',tool='serial',action=action,arguments=args))['data']
                opened=await serial('open',port='COM3',requestId=uuid.uuid4().hex)
                sid=opened['sessionId'];path=self.root/'master-serial.log'
                await request('serial-recording',action='start',sessionId=sid,path=str(path),timestamps=False)
                await serial('write',sessionId=sid,generation=opened['generation'],format='text',data='远端日志\n')
                until=time.monotonic()+5;cursor=0
                while time.monotonic()<until:
                    data=await serial('read',sessionId=sid,cursor=cursor);cursor=data['nextCursor']
                    if path.read_text('utf-8')=='远端日志\n':break
                    await asyncio.sleep(.02)
                self.assertEqual(path.read_text('utf-8'),'远端日志\n')
                await serial('read',sessionId=sid,cursor=0)
                self.assertEqual(path.read_text('utf-8'),'远端日志\n')
                self.assertFalse(self.local_ports,'远程日志不应打开本机COM')
            self.assertTrue(await asyncio.to_thread(_wait_until,lambda:not self.manager.serial_recordings._records))
            self.assertTrue(await asyncio.to_thread(_wait_until,lambda:self.slave_ports[0][2].closed))

    async def test_gui_disconnect_closes_ordinary_remote_session(self):
        """真实GUI连接断开后，从机不应保留无窗口的普通串口接入。"""
        await self._assert_gui_disconnect_closes(self.target,self.slave,self.slave_ports)

    async def test_gui_disconnect_closes_local_session(self):
        await self._assert_gui_disconnect_closes('computer:local',self.local,self.local_ports)

    async def test_gui_disconnect_closes_legacy_remote_session(self):
        node=self.harness.master.node;submit=node.submit
        def legacy(*args,**kwargs):
            kwargs.get('payload',{}).pop('serialStreamProtocol',None)
            return submit(*args,**kwargs)
        with patch.object(node,'submit',side_effect=legacy):
            await self._assert_gui_disconnect_closes(self.target,self.slave,self.slave_ports)

    async def _assert_gui_disconnect_closes(self,target,service,ports):
        from desktop_terminals import DesktopTerminals
        self.registry.call('serial','permission',{'allowed':True})
        self.manager.clients=set();self.manager.worker=SimpleNamespace()
        self.manager.loop=asyncio.get_running_loop()
        self.manager.desktop_terminals=DesktopTerminals(self.manager,server._HTTP_PORT)
        endpoint=await websockets.serve(self.manager.ws_handler,'127.0.0.1',0)
        socket=None
        try:
            socket=await websockets.connect('ws://127.0.0.1:'+str(endpoint.sockets[0].getsockname()[1]),
                origin=f'http://127.0.0.1:{server._HTTP_PORT}')
            await socket.send(json.dumps(dict(type='extension-tool',id=801,tool='serial',target=target,
                action='open',arguments={'port':'COM3','requestId':uuid.uuid4().hex})))
            while True:
                response=json.loads(await socket.recv())
                if response.get('id')==801:break
            self.assertTrue(response['success'],response)
            identifier=response['data']['sessionId']
            self.assertFalse(ports[0][2].closed)
            await socket.close()
            for _ in range(150):
                if ports[0][2].closed:break
                await asyncio.sleep(.02)
            self.assertTrue(ports[0][2].closed,'GUI连接已结束，串口仍占COM')
            self.assertTrue(service.sessions[identifier]['closed'])
            if target!='computer:local':self.assertFalse(self.local_ports,'远程回收不应操作本机COM')
        finally:
            if socket:await socket.close()
            endpoint.close();await endpoint.wait_closed()

    async def test_gui_disconnect_preserves_other_page_and_agent(self):
        """同一COM的其他页面和Agent保持连接，最后接入关闭才释放物理端口。"""
        from desktop_terminals import DesktopTerminals
        self.registry.call('serial','permission',{'allowed':True})
        agent=await self.agent('open',{'port':'COM3'})
        self.manager.clients=set();self.manager.worker=SimpleNamespace()
        self.manager.loop=asyncio.get_running_loop()
        self.manager.desktop_terminals=DesktopTerminals(self.manager,server._HTTP_PORT)
        async with websockets.serve(self.manager.ws_handler,'127.0.0.1',0) as endpoint:
            url='ws://127.0.0.1:'+str(endpoint.sockets[0].getsockname()[1])
            async def call(socket,action,args):
                await socket.send(json.dumps(dict(type='extension-tool',id=802,tool='serial',target=self.target,
                    action=action,arguments=args)))
                while True:
                    result=json.loads(await socket.recv())
                    if result.get('id')==802:
                        self.assertTrue(result['success'],result);return result['data']
            async with websockets.connect(url,origin=f'http://127.0.0.1:{server._HTTP_PORT}') as first, \
                    websockets.connect(url,origin=f'http://127.0.0.1:{server._HTTP_PORT}') as second:
                a=await call(first,'open',{'port':'COM3','requestId':uuid.uuid4().hex})
                b=await call(second,'open',{'port':'COM3','requestId':uuid.uuid4().hex})
                await first.close()
                for _ in range(150):
                    if self.slave.sessions[a['sessionId']]['closed']:break
                    await asyncio.sleep(.02)
                self.assertTrue(self.slave.sessions[a['sessionId']]['closed'])
                self.assertFalse(self.slave.sessions[b['sessionId']]['closed'])
                self.assertFalse(self.slave.sessions[agent['sessionId']]['closed'])
                self.assertFalse(self.slave_ports[0][2].closed)
                closed=await call(second,'close',{'sessionId':b['sessionId']})
                self.assertTrue(closed['closed'])
                self.assertFalse(self.slave_ports[0][2].closed)
            await self.agent('close',{'sessionId':agent['sessionId']})
            self.assertTrue(self.slave_ports[0][2].closed)

    async def test_remote_bytes_gui_priority_agent_discovery(self):
        caps=await agent_client.call('extension_capabilities',{'target':self.target,'tool':'serial'})
        self.assertFalse(caps['data']['data']['allowed'])
        self.assertIn('write',caps['data']['data']['agentActions'])
        self.registry.call('serial','permission',{'allowed':True})
        mcp=await agent_mcp.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'extension_call','arguments':{'target':self.target,'tool':'serial','action':'ports','arguments':{}}}})
        self.assertFalse(mcp['result'].get('isError'),mcp)
        cli=await asyncio.to_thread(subprocess.run,[sys.executable,'-B',str(ROOT/'agent_cli.py'),
            'extension_call','--args',json.dumps({'target':self.target,'tool':'serial','action':'ports','arguments':{}})],
            capture_output=True,text=True,encoding='utf-8',timeout=15,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(cli.returncode,0,cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['data']['data']['ports'][0]['port'],'COM3')
        opened=await self.agent('open',{'port':'COM3'})
        self.assertEqual(opened['config']['baudrate'],921600)
        raw=bytes(range(256))
        await self.agent('write',{'sessionId':opened['sessionId'],'generation':opened['generation'],
            'data':base64.b64encode(raw).decode(),'format':'base64'})
        for _ in range(30):
            output=await self.agent('read',{'sessionId':opened['sessionId']})
            if output['nextCursor']==256: break
            await asyncio.sleep(.01)
        self.assertEqual(base64.b64decode(output['data']),raw)
        self.assertEqual(len(self.slave_ports),1);self.assertFalse(self.local_ports)
        await self.agent('write',{'sessionId':opened['sessionId'],'generation':opened['generation'],
            'data':'A中B','format':'text','interCharDelayMs':1})
        self.assertEqual(self.slave_ports[0][2].writes,[raw,b'A','中'.encode(),b'B'])
        gui=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        self.assertTrue(gui['writable'])
        self.assertEqual(gui['serialWritePacingProtocol'],1)
        rejected=await agent_client.call('extension_call',{'target':self.target,'tool':'serial','action':'write',
            'arguments':{'sessionId':opened['sessionId'],'generation':opened['generation'],'data':'OLD'}})
        self.assertFalse(rejected['data']['success'],rejected)
        await self.gui('write',{'sessionId':gui['sessionId'],'generation':gui['generation'],'format':'hex','data':'00 FF'})
        await self.gui('write',{'sessionId':gui['sessionId'],'generation':gui['generation'],
            'format':'text','data':'A中B','interCharDelayMs':1})
        self.assertEqual(self.slave_ports[0][2].writes,
            [raw,b'A','中'.encode(),b'B',b'\x00\xff',b'A','中'.encode(),b'B'])
        owner=self.harness.master.config.nodeId
        self.harness.slave.unpair_peer(owner)
        self.assertTrue(self.slave_ports[0][2].closed)
        self.assertTrue(all(h['closed'] for h in self.slave.sessions.values()))

    async def test_permission_generation_and_origin(self):
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.agent('open',{'port':'COM3'})
        self.registry.call('serial','permission',{'allowed':False})
        self.assertTrue(self.slave_ports[0][2].closed)
        with self.assertRaises(RuntimeError): await self.gui('ports')
        self.registry.call('serial','permission',{'allowed':True})
        local=self.local
        sent=[]
        class Socket:
            request=SimpleNamespace(headers={'Origin':'https://example.org'})
            async def send(self,data): sent.append(json.loads(data))
        socket=Socket()
        await self.manager._handle_message(socket,{'type':'extension-tool','id':'x','tool':'serial','target':'computer:local',
            'action':'open','arguments':{'port':'COM3','requestId':uuid.uuid4().hex}})
        self.assertFalse(sent[-1]['success']);self.assertFalse(self.local_ports)
        socket.request=SimpleNamespace(headers={'Origin':f'http://127.0.0.1:{server._HTTP_PORT}'})
        await self.manager._handle_message(socket,{'type':'extension-tool','id':'x','tool':'serial','target':'computer:local',
            'action':'open','arguments':{'port':'COM3','requestId':uuid.uuid4().hex}})
        self.assertTrue(sent[-1]['success'],sent[-1]);self.assertEqual(sent[-1]['data']['controller'],'gui')
        pair=self.harness.master.node.pairing.get(self.harness.slave.config.nodeId)
        stale=relay.new_request(relay.KIND_EXTENSION_TOOL,'pc-a',payload={'sourceNodeId':self.harness.master.config.nodeId,
            'targetNodeId':self.harness.slave.config.nodeId,'connectionId':pair.connectionId+'-old',
            'tool':'serial','action':'open','args':{'port':'COM3','requestId':uuid.uuid4().hex}})
        self.harness.slave.node._serve_request('fake-request.json',stale)
        self.assertEqual(len(self.slave_ports),1)
        opened=await self.agent('open',{'port':'COM3'})
        self.harness.slave.node.revoke_peer('pc-a')
        self.assertTrue(self.slave_ports[-1][2].closed)

    async def test_stream_latency_bytes_and_no_control_requests(self):
        self.registry.call('serial','permission',{'allowed':True})
        node=self.harness.master.node;submit=node.submit
        def legacy(*args,**kwargs):
            kwargs.get('payload',{}).pop('serialStreamProtocol',None)
            return submit(*args,**kwargs)
        async def samples(opened):
            cursor=0;times=[]
            for index in range(12):
                raw=bytes([index,0,255,0xe4]);start=time.monotonic()
                await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],format='base64',data=base64.b64encode(raw).decode()))
                received=b''
                while time.monotonic()-start<4 and len(received)<len(raw):
                    output=await self.gui('read',dict(sessionId=opened['sessionId'],cursor=cursor))
                    received+=base64.b64decode(output['data']);cursor=output['nextCursor']
                    await asyncio.sleep(.002)
                self.assertEqual(received,raw);times.append((time.monotonic()-start)*1000)
            return times[2:]
        with patch.object(node,'submit',side_effect=legacy):
            old=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
            old_times=await samples(old)
            await self.gui('close',dict(sessionId=old['sessionId']))
        new=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        self.assertEqual(new['serialStreamProtocol'],1)
        with patch.object(node,'submit',side_effect=AssertionError('持续收发不应写入请求目录')):
            new_times=await samples(new)
            await self.gui('close',dict(sessionId=new['sessionId']))
        self.assertLess(statistics.median(new_times),statistics.median(old_times))
        print('  [MEASURE] 同机共享目录/假COM 输入→完整字节回显：旧协议P50 %.1fms，新持续流P50 %.1fms / 最大 %.1fms（10次，非双机SLA）'%(
            statistics.median(old_times),statistics.median(new_times),max(new_times)),flush=True)

    async def test_stream_uncertain_no_replay_and_other_owner_survives(self):
        self.registry.call('serial','permission',{'allowed':True})
        local=self.registry.call('serial','open',{'port':'COM3','requestId':uuid.uuid4().hex},caller='gui')['data']
        self.registry.call('serial','control',{'sessionId':local['sessionId'],'generation':local['generation'],'operation':'release'},caller='gui')
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        client=self.harness.master._serial_clients[(self.harness.slave.config.nodeId,opened['sessionId'],'gui')]
        with patch('serial_stream.ACK_WAIT_SECONDS',.1),patch.object(client.channel,'read_meta',return_value=dict(client.meta)):
            with self.assertRaises(Exception) as missing:
                await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='ONE'))
            self.assertTrue(getattr(missing.exception,'operation_uncertain',False),missing.exception)
        self.assertEqual(self.slave_ports[0][2].writes,[b'ONE'])
        with self.assertRaises(Exception):
            await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='TWO'))
        self.assertEqual(self.slave_ports[0][2].writes,[b'ONE'])

        for _ in range(30):
            output=await self.gui('read',dict(sessionId=opened['sessionId']))
            if not output['inputUncertain']:break
            await asyncio.sleep(.02)
        self.assertFalse(output['inputUncertain'],'迟到的准确回执未解除待确认状态')
        await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='NEW'))
        self.assertEqual(self.slave_ports[0][2].writes,[b'ONE',b'NEW'],'恢复后重放了旧输入')
        await self.gui('close',dict(sessionId=opened['sessionId']))
        self.assertFalse(self.slave_ports[0][2].closed,'远端close关闭了本机接入')
        self.registry.call('serial','close',{'sessionId':local['sessionId']},caller='gui')
        self.assertTrue(self.slave_ports[0][2].closed)

    async def test_stream_replay_rejected_and_output_cursor_gap(self):
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        client=self.harness.master._serial_clients[(self.harness.slave.config.nodeId,opened['sessionId'],'gui')]
        # 不完整UTF-8和所有二进制值必须立即可读；历史裁剪保持执行端绝对游标。
        raw=bytes(range(256))*8192
        self.slave_ports[0][2].rx.put(raw)
        output=None
        for _ in range(150):
            output=await self.gui('read',dict(sessionId=opened['sessionId'],cursor=0,limit=65536))
            if output['nextCursor']>=1024*1024+65536:break
            await asyncio.sleep(.02)
        self.assertEqual(output['droppedBytes'],1024*1024)
        self.assertEqual(output['nextCursor'],1024*1024+65536)
        self.assertEqual(base64.b64decode(output['data']),bytes(range(256))*256)
        await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='ONCE'))
        # 重复输入帧不能再次执行，流必须关闭。
        client._append({'seq':client.sequence,'action':'write','args':{'generation':opened['generation'],'data':'REPLAY'}})
        for _ in range(100):
            if self.slave_ports[0][2].closed:break
            await asyncio.sleep(.02)
        self.assertTrue(self.slave_ports[0][2].closed)
        self.assertEqual(self.slave_ports[0][2].writes,[b'ONCE'])

    async def test_receipt_after_old_three_second_limit(self):
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        original=self.registry.call
        def delayed(tool,action,*args,**kwargs):
            if tool=='serial' and action=='write':time.sleep(3.3)
            return original(tool,action,*args,**kwargs)
        with patch.object(self.registry,'call',side_effect=delayed):
            result=await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='DELAY'))
        self.assertEqual(result['writtenBytes'],5)
        self.assertEqual(self.slave_ports[0][2].writes,[b'DELAY'])
        await self.gui('close',dict(sessionId=opened['sessionId']))

    async def test_receipt_does_not_regress_and_meta_keeps_file_identity(self):
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        client=self.harness.master._serial_clients[(self.harness.slave.config.nodeId,opened['sessionId'],'gui')]
        before=os.stat(client.channel.meta_path).st_ino
        await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],data='ONCE'))
        self.assertEqual(before,os.stat(client.channel.meta_path).st_ino,'更新回执替换了SMB文件对象')
        with patch.object(client.channel,'read_meta',return_value={**client.meta,'ackSeq':0}):
            client.update_meta()
        self.assertEqual(client.meta['ackSeq'],1,'过期回执回退了已确认序号')
        await self.gui('close',dict(sessionId=opened['sessionId']))


def run():
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RemoteTests))
    return result.wasSuccessful()


if __name__=='__main__':sys.exit(0 if run() else 1)
