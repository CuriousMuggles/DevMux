"""窗口代理身份、事件选路及父子窗口资源回收。"""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from desktop_terminals import DesktopTerminals


class Event:
    def __init__(self):self.callbacks=[]
    def __iadd__(self,callback):self.callbacks.append(callback);return self
    def fire(self):
        for callback in self.callbacks:callback()


class Window:
    def __init__(self):self.events=SimpleNamespace(closed=Event());self.position=None;self.destroyed=False
    def move(self,x,y):self.position=(x,y)
    def restore(self):pass
    def show(self):pass
    def destroy(self):self.destroyed=True;self.events.closed.fire()


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages=[]
        async def send(ws,msg):self.messages.append((ws,msg))
        async def blocking(function,*args):return function(*args)
        self.manager=SimpleNamespace(_send=send,_run_blocking=blocking,loop=asyncio.get_running_loop())
        self.service=DesktopTerminals(self.manager,8080)
        self.owner=SimpleNamespace(request=SimpleNamespace(headers={'Origin':'http://127.0.0.1:8080'}))

    async def test_owner_origin_and_cleanup(self):
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='serial:x',title='Serial'))
        other=SimpleNamespace(request=self.owner.request)
        with self.assertRaises(PermissionError):await self.service.handle(other,dict(type='desktop-terminal-destroy',windowId=opened['windowId']))
        other.request=SimpleNamespace(headers={'Origin':'https://example.org'})
        with self.assertRaises(PermissionError):await self.service.handle(other,dict(type='desktop-terminal-open',key='x',title='x'))
        self.service.close_all(other);self.assertEqual(len(self.service.records),1)
        self.service.close_all(self.owner);self.assertFalse(self.service.records)

    async def test_plugin_native_window_can_move_outside_main_window(self):
        def dispatch(action,args):
            if action=='list':return [{'id':'example','enabled':True,'tool':'example'}]
            return {'success':True,'data':{'text':'ok'}}
        self.manager.plugins=SimpleNamespace(dispatch=dispatch)
        window=Window()
        self.service.bind_native(SimpleNamespace(create_window=lambda *a,**k:window))
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',view='plugin',key='plugin:example',title='插件'))
        self.assertTrue(opened['native']);self.assertIn('/desktop-plugin.html#',opened['url'])
        await self.service.handle(self.owner,dict(type='desktop-terminal-move',windowId=opened['windowId'],x=-900,y=300))
        self.assertEqual(window.position,(-900,300))
        record=self.service.records[opened['windowId']]
        await self.service.plugin_request(record,self.owner,{'request':{'type':'devmux-host-call','id':1,'hostType':'extension-tool','hostPayload':{'tool':'serial','action':'open'}}})
        self.assertFalse(self.messages[-1][1]['payload']['result']['success'])
        self.service.close_all(self.owner);self.assertTrue(window.destroyed)

    async def test_remote_only_windows_are_separate_per_machine(self):
        from unittest.mock import Mock,patch
        dispatch=Mock(side_effect=AssertionError('remote frontend must not use local installation'))
        self.manager.plugins=SimpleNamespace(dispatch=dispatch)
        self.manager.relay=object()
        self.manager.agent=SimpleNamespace(resolve=lambda target,kind:{'local':False,'nodeId':target})
        with patch('remote_plugins.frontend',return_value={'id':'relay','tool':'relay','html':'<!doctype html>'}):
            for target in ('computer:a','computer:b'):
                await self.service.handle(self.owner,dict(type='desktop-terminal-open',view='plugin',key='plugin:relay',title='Relay',target=target))
            with self.assertRaises(ValueError):
                await self.service.handle(self.owner,dict(type='desktop-terminal-open',view='plugin',key='plugin:relay',title='Relay',target='computer:a'))
        dispatch.assert_not_called()
        dispatch.side_effect=None;dispatch.return_value=[{'id':'relay','tool':'relay','enabled':True}]
        await self.service.handle(self.owner,dict(type='desktop-terminal-open',view='plugin',key='plugin:relay',title='Local'))
        self.assertEqual(len(self.service.records),3)
        self.service.close_all(self.owner)

    async def test_remote_plugin_success_uses_original_node_and_filters_targets(self):
        import json
        from unittest.mock import Mock,patch
        async def blocking(function,*args,**kwargs):return function(*args,**kwargs)
        self.manager._run_blocking=blocking
        self.manager.plugins=SimpleNamespace(dispatch=Mock(side_effect=AssertionError('local backend used')))
        self.manager.relay=object()
        self.manager.agent=SimpleNamespace(resolve=lambda target,kind:{'nodeId':'slave-node','local':False})
        record={'plugin_id':'relay','plugin_tool':'relay','plugin_target':'computer:slave','remote_page':{'html':'test'}}
        with patch('extension_tools.remote_call',return_value={'success':True,'data':{'remote':True}}) as remote:
            await self.service.plugin_request(record,self.owner,{'request':{'type':'devmux-plugin-call','id':1,'action':'config','arguments':{'x':1}}})
            remote.assert_called_once_with(self.manager.relay,'slave-node','relay','config',{'x':1},caller='gui')
            self.assertTrue(self.messages[-1][1]['payload']['result']['success'])
        async def handle(ws,message):
            await ws.send(json.dumps({'success':True,'data':{'targets':[{'target':'computer:local'},{'target':'computer:slave'},{'target':'computer:other'}]}}))
        self.manager._handle_message=handle
        await self.service.plugin_request(record,self.owner,{'request':{'type':'devmux-host-call','id':2,'hostType':'extension-tool','hostPayload':{'tool':'relay','action':'targets'}}})
        self.assertEqual(self.messages[-1][1]['payload']['result']['data']['targets'],[{'target':'computer:slave'}])

    async def test_plugin_manifest_file_permissions_are_enforced(self):
        import json
        calls=[]
        async def handle(ws,message):
            calls.append(message['type']);await ws.send(json.dumps({'success':True,'data':{'path':'fixture'}}))
        self.manager._handle_message=handle
        record={'plugin_id':'example','plugin_tool':'example','plugin_permissions':[]}
        request={'type':'devmux-host-call','id':1,'hostType':'pick-file','hostPayload':{}}
        await self.service.plugin_request(record,self.owner,{'request':request})
        self.assertFalse(self.messages[-1][1]['payload']['result']['success'])
        self.assertEqual(calls,[])
        record['plugin_permissions']=['pick-file']
        await self.service.plugin_request(record,self.owner,{'request':request})
        self.assertTrue(self.messages[-1][1]['payload']['result']['success'])
        self.assertEqual(calls,['pick-file'])

    async def test_remote_plugin_host_rejects_local_and_cross_tool_routes(self):
        record={'plugin_id':'relay','plugin_tool':'relay','plugin_target':'computer:slave','remote_page':{'html':'test'}}
        for tool,target in [('relay','computer:local'),('serial','computer:slave'),('relay','computer:other')]:
            await self.service.plugin_request(record,self.owner,{'request':{'type':'devmux-host-call','id':1,'hostType':'extension-tool','hostPayload':{'tool':tool,'target':target,'action':'config'}}})
            self.assertFalse(self.messages[-1][1]['payload']['result']['success'])

    async def test_removed_builtin_mijia_view_is_rejected(self):
        for key in ('other','mijia-panel'):
            with self.assertRaises(ValueError):
                await self.service.handle(self.owner,dict(type='desktop-terminal-open',key=key,title='米家',view='mijia'))

    async def test_native_coordinates_and_os_close(self):
        window=Window();calls=[]
        def create(title,url,**options):calls.append(options);return window
        self.service.bind_native(SimpleNamespace(create_window=create))
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='adb:x',title='ADB',x=-600,y=2500,width=700,height=600))
        self.assertTrue(opened['native']);self.assertEqual(calls[0]['x'],-600)
        await self.service.handle(self.owner,dict(type='desktop-terminal-move',windowId=opened['windowId'],x=3500,y=-200))
        self.assertEqual(window.position,(3500,-200),'桌面位置不能受主窗口大小钳制')
        window.events.closed.fire();await asyncio.sleep(.01)
        self.assertFalse(self.service.records)
        self.assertEqual(self.messages[-1][1]['type'],'desktop-terminal-closed')

    async def test_child_token_and_single_input_route(self):
        import json
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='powershell:x',title='PowerShell'))
        record=self.service.records[opened['windowId']]
        class Socket:
            request=self.owner.request
            def __init__(self,messages):self.messages=iter(messages);self.closed=False
            async def recv(self):return json.dumps(next(self.messages))
            def __aiter__(self):return self
            async def __anext__(self):
                try:return json.dumps(next(self.messages))
                except StopIteration:raise StopAsyncIteration
            async def close(self,**kwargs):self.closed=True
        invalid=Socket([dict(type='desktop-terminal-attach',windowId=record['id'],token='wrong')])
        await self.service.child_handler(invalid);self.assertTrue(invalid.closed)
        self.assertEqual(len(self.service.records),1,'无效能力标识关闭了其他页面的终端')
        await self.service.handle(self.owner,dict(type='desktop-terminal-bind',windowId=record['id']))
        child=Socket([dict(type='desktop-terminal-attach',windowId=record['id'],token=record['token']),
            dict(type='desktop-terminal-event',payload=dict(kind='input',data='ONCE')),
            dict(type='desktop-terminal-action',action='dock')])
        await self.service.child_handler(child)
        events=[m for ws,m in self.messages if m['type']=='desktop-terminal-event']
        self.assertEqual(len(events),1);self.assertIs(self.messages[0][0],self.owner)
        self.assertEqual(events[0]['payload']['data'],'ONCE')
        self.assertFalse(self.service.records)

    async def test_slow_view_is_bounded_and_resyncs_display_only(self):
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='adb:slow',title='ADB'))
        record=self.service.records[opened['windowId']]
        blocked=asyncio.Event()
        class Child:
            closed=False
            async def close(self,**kwargs):self.closed=True
        child=Child();record['child']=child
        send=self.manager._send
        async def slow(ws,msg):
            if ws is child:await blocked.wait()
            else:await send(ws,msg)
        self.manager._send=slow;record['sender']=asyncio.create_task(self.service.output_loop(record,child))
        for index in range(12):
            await self.service.handle(self.owner,dict(type='desktop-terminal-publish',windowId=record['id'],payload=dict(kind='data',seq=index,data='A'*400000)))
            await asyncio.sleep(0)
        self.assertLessEqual(record['queuedBytes'],2*1024*1024)
        self.assertEqual([m['type'] for ws,m in self.messages].count('desktop-terminal-resync'),1)
        self.assertFalse(any(m['type']=='desktop-terminal-event' for ws,m in self.messages),'显示补齐不能变成设备输入')
        await self.service.handle(self.owner,dict(type='desktop-terminal-publish',windowId=record['id'],payload=dict(kind='snapshot',initial=True,history=[])))
        self.assertFalse(record['resync'])
        self.service.close_all(self.owner);await asyncio.sleep(.03)
        self.assertTrue(child.closed)


def run():return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesktopTests)).wasSuccessful()
if __name__=='__main__':sys.exit(0 if run() else 1)
