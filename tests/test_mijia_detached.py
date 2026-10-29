"""真实 Edge 子窗和服务端通道，米家云与设备均为模拟。"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import server
from tests.test_ui_e2e import _FakeWorker, _edge_executable
from tests.test_serial_ui import exclusive_port
from tests.edge_test_support import EdgeSession
from websockets.sync.client import connect


class FakeMijia:
    def __init__(self): self.on=False; self.owner=None
    def cancel(self, owner): pass
    def call(self, owner, action, args):
        if action=='status': return {'loggedIn':True}
        if action=='snapshot': return {'devices':[{'did':'1','name':'客厅灯','room':'客厅','model':'x.light.a','online':True,'on':self.on}],'unavailable':0}
        if action=='read-switch': return {'on':self.on}
        if action=='set-switch': self.on=args['value']; return {'on':self.on}
        if action=='login': self.owner=owner; return {'state':'waiting'}
        if action=='login-status': return {'state':'waiting','qr':'PHN2Zy8+'} if self.owner is owner else {'state':'idle'}
        if action in ('cancel','logout'): return {'state':'idle'}
        raise ValueError(action)


def exercise(endpoint, fake):
    with connect(endpoint,proxy=None,max_size=20*1024*1024) as socket:
        number=0
        def command(method,params=None,session=None):
            nonlocal number
            number+=1; data={'id':number,'method':method,'params':params or {}}
            if session: data['sessionId']=session
            socket.send(json.dumps(data))
            while True:
                reply=json.loads(socket.recv(timeout=30))
                if reply.get('id')==number:
                    assert not reply.get('error'),reply
                    return reply['result']
        def evaluate(code,session=None):
            result=command('Runtime.evaluate',{'expression':code,'awaitPromise':True,'returnByValue':True},session)
            assert 'exceptionDetails' not in result,result
            return result['result'].get('value')
        def click(selector,session=None):
            rect=evaluate("(()=>{const r=document.querySelector("+json.dumps(selector)+").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()",session)
            command('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',clickCount=1,**rect),session)
            command('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',clickCount=1,**rect),session)
        command('Runtime.enable'); time.sleep(2)
        wait="window.waitMijia=async fn=>{for(let n=0;n<400;n++){if(fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('米家窗口等待超时');};"
        evaluate("(async()=>{"+wait+"await waitMijia(()=>typeof ws!=='undefined'&&ws?.readyState===1&&window.MijiaIntegration);document.getElementById('tools-mijia').click();document.querySelector('.mijia-refresh').click();await waitMijia(()=>document.querySelector('.mijia-card button'));return true})()")
        click('.mijia-detach')
        target=None
        for _ in range(200):
            target=next((t for t in command('Target.getTargets')['targetInfos'] if '/desktop-mijia.html' in t['url']),None)
            if target: break
            time.sleep(.05)
        assert target,'没有打开米家子窗口'
        child=command('Target.attachToTarget',{'targetId':target['targetId'],'flatten':True})['sessionId']
        evaluate("(async()=>{"+wait+"await waitMijia(()=>document.getElementById('mijia-child-message')?.hidden&&document.querySelector('.mijia-card button'));return true})()",child)
        root_window=command('Browser.getWindowForTarget',{})
        child_window=command('Browser.getWindowForTarget',{'targetId':target['targetId']})
        assert root_window['windowId']!=child_window['windowId'],'米家没有脱离为独立窗口'
        command('Browser.setWindowBounds',{'windowId':child_window['windowId'],'bounds':{'left':1200,'top':50,'width':650,'height':520}})
        assert command('Browser.getWindowBounds',{'windowId':child_window['windowId']})['bounds']['left']>=1198
        assert evaluate("typeof MIJIA_PAGE_TOKEN === 'undefined'",child)
        click('.mijia-card button',child)
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-card button')?.textContent.includes('已开启'));return true})()",child)
        assert evaluate("document.querySelector('.mijia-card button').textContent.includes('已开启')")
        fake.on = False
        click('.mijia-refresh',child)
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-card button')?.textContent.includes('已关闭'));return true})()",child)
        click('.mijia-card button',child)
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-card button')?.textContent.includes('已开启'));return true})()",child)
        click('.mijia-login',child)
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-qr img')?.getAttribute('src')?.startsWith('data:image/svg'));return true})()",child)
        click('.mijia-cancel',child)
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-qr')?.hidden);return true})()",child)
        click('.mijia-child-dock',child)
        evaluate("(async()=>{await waitMijia(()=>!document.querySelector('.mijia-overlay').hidden);return true})()")
        assert evaluate("document.querySelector('.mijia-card button').textContent.includes('已开启')")
        # 标题拖动超出主窗口边界，触发独立窗口创建。
        rect=evaluate("(()=>{const r=document.querySelector('.mijia-head').getBoundingClientRect();return {x:r.x+50,y:r.y+12};})()")
        command('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,**rect})
        command('Input.dispatchMouseEvent',{'type':'mouseMoved','button':'left','buttons':1,'x':rect['x']+20,'y':rect['y']+20})
        command('Input.dispatchMouseEvent',{'type':'mouseMoved','button':'left','buttons':1,'x':-20,'y':rect['y']+20})
        command('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,'x':-20,'y':rect['y']+20})
        evaluate("(async()=>{await waitMijia(()=>document.querySelector('.mijia-overlay').hidden);return true})()")
        print('[PASS] 米家独立浏览器窗口：按钮脱离、越界拖出、独立位置、扫码、开关、返回及页面令牌隔离')
        return True


def run():
    edge=_edge_executable()
    if not edge: print('[SKIP] 未找到 Edge'); return None
    previous=(server.HTTP_PORT,server.WS_PORT);browser=backend=http=None
    with tempfile.TemporaryDirectory(prefix='mijia-child-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        try:
            with patch.dict(os.environ,{'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}), patch.object(server,'ADBWorker',_FakeWorker), patch.object(server.ManagerServer,'_commands_path',return_value=str(runtime/'commands.json')):
                server.HTTP_PORT=exclusive_port(21580);server.WS_PORT=exclusive_port(21850)
                manager=server.ManagerServer();manager.mijia=FakeMijia()
                backend=threading.Thread(target=lambda:asyncio.run(manager.run()),daemon=True);backend.start()
                for _ in range(200):
                    if getattr(manager,'_ws_port',0): break
                    time.sleep(.05)
                else: raise AssertionError('WebSocket 未启动')
                http=server.start_http_server()
                browser=EdgeSession.start(edge,['--headless=new','--no-first-run','--remote-allow-origins=*',f'http://127.0.0.1:{server._HTTP_PORT}/'],runtime/'edge')
                return exercise(browser.wait_ready(20),manager.mijia)
        finally:
            if browser: browser.close(); time.sleep(1)
            if backend: manager.stop();backend.join(timeout=15)
            if http: http.shutdown();http.server_close()
            server.HTTP_PORT,server.WS_PORT=previous

if __name__=='__main__':sys.exit(0 if run() else 1)
