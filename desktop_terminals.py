"""原生终端视图代理：后台会话由主页面持有，子窗不能另行开会话。"""
import asyncio
import json
import re
import secrets
import threading
from collections import deque
from urllib.parse import urlsplit


class DesktopTerminals:
    def __init__(self, manager, port):
        self.manager, self._port = manager, port
        self.webview = None
        self.records = {}
        self.lock = threading.RLock()

    @property
    def port(self):
        return self._port() if callable(self._port) else self._port

    def bind_native(self, webview):
        self.webview = webview

    def local(self, ws):
        try:
            origin = urlsplit(ws.request.headers.get('Origin', ''))
            return origin.scheme == 'http' and origin.hostname in ('127.0.0.1', 'localhost') and origin.port == self.port
        except (AttributeError, ValueError):
            return False

    def owned(self, ws, identifier):
        with self.lock:
            record = self.records.get(identifier)
            if not record or record['owner'] is not ws:
                raise PermissionError('独立窗口不属于当前页面')
            return record

    async def notify(self, record, kind, **payload):
        await self.manager._send(record['owner'], dict(type=kind, windowId=record['id'], key=record['key'], **payload))

    def native_closed(self, identifier):
        with self.lock:
            record = self.records.pop(identifier, None)
        loop = self.manager.loop
        if record and loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.notify(record, 'desktop-terminal-closed'), loop)
            asyncio.run_coroutine_threadsafe(self.end_view(record),loop)

    def dispose(self, record):
        with self.lock:
            self.records.pop(record['id'], None)
        loop=self.manager.loop
        if loop and loop.is_running():asyncio.run_coroutine_threadsafe(self.end_view(record),loop)
        if record.get('native'):
            record['native'].destroy()

    async def end_view(self, record):
        task=record.get('sender')
        if task and task is not asyncio.current_task():
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        record['queue'].clear();record['queuedBytes']=0
        if record.get('child'):await record['child'].close(code=1000,reason='独立视图已回收')

    async def output_loop(self, record, child):
        try:
            while self.records.get(record['id']) is record and record['child'] is child:
                await record['available'].wait()
                while record['queue']:
                    payload,size=record['queue'].popleft();record['queuedBytes']-=size
                    await asyncio.wait_for(self.manager._send(child,dict(type='desktop-terminal-view',payload=payload)),5)
                record['available'].clear()
        except Exception:
            if self.records.get(record['id']) is record:
                await self.notify(record,'desktop-terminal-dock')
                self.dispose(record)

    async def enqueue(self, record, payload):
        if not record['child']:return
        size=len(json.dumps(payload))
        initial=payload.get('kind') == 'snapshot' and payload.get('initial') is True
        if initial:
            record['queue'].clear();record['queuedBytes']=0;record['resync']=False
        elif record['resync']:return
        elif record['queuedBytes']+size>2*1024*1024:
            record['queue'].clear();record['queuedBytes']=0;record['resync']=True
            await self.notify(record,'desktop-terminal-resync');return
        record['queue'].append((payload,size));record['queuedBytes']+=size;record['available'].set()

    def close_all(self, owner=None):
        with self.lock:
            records = [r for r in self.records.values() if owner is None or r['owner'] is owner]
        for record in records:
            self.dispose(record)

    async def handle(self, ws, msg):
        if not self.local(ws):
            raise PermissionError('独立窗口只允许本机应用页面')
        action = msg['type'].removeprefix('desktop-terminal-')
        if action == 'open':
            key, title = msg.get('key'), msg.get('title')
            if not isinstance(key, str) or not key or len(key)>256 or not isinstance(title,str) or len(title)>512:
                raise ValueError('终端标识或标题无效')
            remote_page=None
            plugin_target=msg.get('target','computer:local')
            if msg.get('view')=='plugin' and plugin_target!='computer:local':
                if not key.startswith('plugin:'):raise ValueError('插件窗口标识无效')
                target=self.manager.agent.resolve(plugin_target,'computer')
                if target['local']:raise ValueError('远程插件目标无效')
                from remote_plugins import frontend
                remote_page=await self.manager._run_blocking(frontend,self.manager.plugins,self.manager.relay,target['nodeId'],key.removeprefix('plugin:'))
            with self.lock:
                if len(self.records)>=16:raise ValueError('独立终端窗口已达16个上限')
                if any(r['owner'] is ws and r['key']==key and r.get('plugin_target','computer:local')==plugin_target for r in self.records.values()):
                    raise ValueError('该终端已在独立窗口中')
                identifier=secrets.token_hex(16);token=secrets.token_hex(32)
                view = msg.get('view', 'terminal')
                if view not in ('terminal','plugin'):
                    raise ValueError('独立窗口类型无效')
                plugin_id = None
                plugin_tool = None
                if view == 'plugin':
                    if not key.startswith('plugin:'):
                        raise ValueError('插件窗口标识无效')
                    plugin_id=key.removeprefix('plugin:')
                    installed=remote_page or next((p for p in self.manager.plugins.dispatch('list',{}) if p['id']==plugin_id and p['enabled']),None)
                    if installed is None:raise ValueError('插件未启用')
                    plugin_tool=installed.get('tool',plugin_id)
                page = 'desktop-plugin.html' if view == 'plugin' else 'desktop-terminal.html'
                url=f'http://127.0.0.1:{self.port}/{page}#{identifier}:{token}'
                record=dict(id=identifier,key=key,plugin_target=msg.get('target','computer:local'),view=view,plugin_id=plugin_id,plugin_tool=plugin_tool,owner=ws,child=None,token=token,bound=False,native=None,maximized=False,
                    queue=deque(),queuedBytes=0,available=asyncio.Event(),sender=None,resync=False)
                record['remote_page']=remote_page
                if view=='plugin':record['plugin_permissions']=installed.get('permissions',['pick-file','pick-directory'])
                self.records[identifier]=record
            try:
                if self.webview:
                    width,height=msg.get('width',820),msg.get('height',600)
                    x,y=msg.get('x',100),msg.get('y',100)
                    if any(type(v) is not int for v in (width,height,x,y)):
                        raise ValueError('窗口尺寸和桌面坐标必须为整数')
                    def launch():
                        window=self.webview.create_window(title,url,width=max(320,min(10000,width)),
                            height=max(240,min(10000,height)),x=x,y=y,min_size=(320,240),resizable=True,text_select=True)
                        window.events.closed += lambda:self.native_closed(identifier)
                        record['native']=window
                    await self.manager._run_blocking(launch)
                if self.records.get(identifier) is not record:raise RuntimeError('独立窗口已关闭，终端保留在主界面')
                return dict(windowId=identifier,url=url,native=bool(record['native']))
            except Exception:
                self.dispose(record);raise
        record=self.owned(ws,msg.get('windowId'))
        if action=='bind':
            record['bound']=True
            if record['child']:await self.notify(record,'desktop-terminal-ready')
        elif action=='publish':
            payload=msg.get('payload')
            if not isinstance(payload,dict) or len(json.dumps(payload))>2*1024*1024:
                raise ValueError('独立终端显示消息过长或无效')
            await self.enqueue(record,payload)
        elif action=='destroy':
            await self.manager._run_blocking(self.dispose,record)
        elif action in ('raise','move'):
            native=record['native']
            if native:
                if action=='raise':
                    await self.manager._run_blocking(native.restore)
                    await self.manager._run_blocking(native.show)
                else:
                    x,y=msg.get('x'),msg.get('y')
                    if any(type(v) is not int or abs(v)>100000 for v in (x,y)):
                        raise ValueError('桌面位置无效')
                    await self.manager._run_blocking(native.move,x,y)
        else:raise ValueError('独立窗口动作无效')
        return {}

    async def plugin_request(self, record, child, payload):
        request=payload.get('request',{})
        identifier=request.get('id') if isinstance(request,dict) else None
        async def reply(result):
            await self.manager._send(child,dict(type='desktop-terminal-view',payload={'kind':'plugin-result','id':identifier,'result':result}))
        try:
            if not isinstance(request,dict):raise ValueError('插件请求无效')
            if request.get('type') == 'devmux-plugin-call':
                if record.get('remote_page'):
                    from extension_tools import remote_call
                    target=self.manager.agent.resolve(record['plugin_target'],'computer')
                    result=await self.manager._run_blocking(remote_call,self.manager.relay,target['nodeId'],record['plugin_tool'],request.get('action'),request.get('arguments',{}),caller='gui')
                else:
                    result=await self.manager._run_blocking(self.manager.plugins.dispatch,'call',{'id':record['plugin_id'],'action':request.get('action'),'arguments':request.get('arguments',{})})
                await reply(result)
            elif request.get('type') == 'devmux-host-call':
                kind=request.get('hostType')
                arguments=request.get('hostPayload',{})
                if kind not in ('pick-file','pick-directory','extension-tool'):raise PermissionError('插件不能调用该主程序接口')
                if kind in ('pick-file','pick-directory') and kind not in record.get('plugin_permissions',['pick-file','pick-directory']):
                    raise PermissionError('插件清单未声明宿主权限: '+kind)
                if kind=='extension-tool':
                    if arguments.get('tool')!=record['plugin_tool']:raise PermissionError('插件不能调用其他工具')
                    command={key:arguments.get(key) for key in ('tool','action','target','arguments')}
                    if record.get('remote_page') and command['action']!='targets' and command['target']!=record['plugin_target']:
                        raise PermissionError('远程插件窗口只能操作原从机')
                else:
                    command={'multi':True} if kind=='pick-file' else {'title':'选择目录'}
                class ReplySocket:
                    request=child.request
                    async def send(self, raw):
                        result=json.loads(raw)
                        if record.get('remote_page') and kind=='extension-tool' and command['action']=='targets':
                            data=result.get('data',{})
                            data['targets']=[item for item in data.get('targets',[]) if item['target']==record['plugin_target']]
                        await reply(result)
                await self.manager._handle_message(ReplySocket(),{'type':kind,'id':identifier,**command})
            else:raise ValueError('插件请求类型无效')
        except Exception as error:
            await reply({'success':False,'output':str(error)})

    async def child_handler(self, ws):
        record=None
        try:
            if not self.local(ws):raise PermissionError('子窗来源无效')
            msg=json.loads(await asyncio.wait_for(ws.recv(),10))
            with self.lock:
                record=self.records.get(msg.get('windowId'))
                if (msg.get('type')!='desktop-terminal-attach' or not record
                        or not isinstance(msg.get('token'),str) or not secrets.compare_digest(record['token'],msg['token'])
                        or record['child'] is not None):
                    raise PermissionError('子窗能力标识无效或已使用')
                record['child']=ws
                record['sender']=asyncio.create_task(self.output_loop(record,ws))
            if record.get('view') == 'plugin':
                page={'html':record['remote_page']['html']} if record.get('remote_page') else await self.manager._run_blocking(self.manager.plugins.dispatch,'page',{'id':record['plugin_id']})
                initial=json.dumps(record.get('plugin_target','computer:local')).replace('<','\\u003c')
                script='<script>window.DevMuxInitialTarget='+initial+';</script>'
                page['html']=re.sub(r'<!doctype\s+html\s*>',lambda match:match.group()+script,page['html'],count=1,flags=re.I) if re.search(r'<!doctype\s+html\s*>',page['html'],re.I) else script+page['html']
                await self.manager._send(ws,dict(type='desktop-terminal-view',payload={'kind':'plugin-page',**page}))
            if record['bound']:await self.notify(record,'desktop-terminal-ready')
            async for raw in ws:
                msg=json.loads(raw)
                if self.records.get(record['id']) is not record:break
                if msg.get('type')=='desktop-terminal-event':
                    payload=msg.get('payload')
                    if not isinstance(payload,dict) or len(json.dumps(payload))>262144:
                        raise ValueError('子窗输入消息无效')
                    if record.get('view') == 'plugin':
                        await self.plugin_request(record,ws,payload)
                    else:
                        await self.notify(record,'desktop-terminal-event',payload=payload)
                elif msg.get('type')=='desktop-terminal-action':
                    action=msg.get('action')
                    if action in ('dock','close'):
                        await self.notify(record,'desktop-terminal-dock' if action=='dock' else 'desktop-terminal-closed')
                        await self.manager._run_blocking(self.dispose,record)
                        break
                    elif action in ('minimize','maximize') and record['native']:
                        native=record['native']
                        if action=='minimize':await self.manager._run_blocking(native.minimize)
                        else:
                            await self.manager._run_blocking(native.restore if record['maximized'] else native.maximize)
                            record['maximized']=not record['maximized']
                    else:raise ValueError('子窗窗口动作无效')
                else:raise ValueError('子窗不能调用其他会话或工具')
        except Exception:
            await ws.close(code=1008,reason='独立终端连接已结束')
        finally:
            if record and record.get('child') is ws:
                record['child']=None
                await self.end_view(record)
                if self.records.get(record['id']) is record:
                    await self.notify(record,'desktop-terminal-dock')
                    self.dispose(record)
