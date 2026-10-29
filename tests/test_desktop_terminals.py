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

    async def test_mijia_view_requires_correct_key_and_resyncs(self):
        with self.assertRaises(ValueError):
            await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='other',title='x',view='mijia'))
        with self.assertRaises(ValueError):
            await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='mijia-panel',title='x',view='invalid'))
        opened=await self.service.handle(self.owner,dict(type='desktop-terminal-open',key='mijia-panel',title='米家',view='mijia'))
        self.assertIn('/desktop-mijia.html#',opened['url'])
        record=self.service.records[opened['windowId']]
        record['child']=SimpleNamespace()
        record['resync']=True
        await self.service.enqueue(record,dict(kind='mijia-snapshot',initial=True,html='<p>测试</p>'))
        self.assertFalse(record['resync'])
        self.service.records.pop(opened['windowId'])

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
