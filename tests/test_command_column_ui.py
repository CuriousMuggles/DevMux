"""真实Edge输入事件验证Alt列编辑，假ADB/COM，不接触现场硬件。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_ui_e2e import _FakeWorker, _edge_executable
from tests.test_serial_ui import exclusive_port
from tests.test_serial_terminal import service
from tests.edge_test_support import EdgeSession


def check(endpoint, output):
    from websockets.sync.client import connect
    with connect(endpoint, proxy=None, max_size=20 * 1024 * 1024) as ws:
        identifier = 0
        def command(method, params=None, session=None):
            nonlocal identifier
            identifier += 1
            payload = dict(id=identifier, method=method, params=params or {})
            if session: payload['sessionId'] = session
            ws.send(json.dumps(payload))
            while True:
                response = json.loads(ws.recv(timeout=30))
                if response.get('id') == identifier:
                    assert not response.get('error'), response
                    return response['result']
        def evaluate(code, session=None):
            result = command('Runtime.evaluate', dict(expression=code, awaitPromise=True, returnByValue=True), session)
            assert 'exceptionDetails' not in result, result
            return result['result'].get('value')
        def key(name, ctrl=False, shift=False, session=None):
            modifiers = (2 if ctrl else 0) | (8 if shift else 0)
            command('Input.dispatchKeyEvent', dict(type='keyDown', key=name, code=name, modifiers=modifiers), session)
            command('Input.dispatchKeyEvent', dict(type='keyUp', key=name, code=name, modifiers=modifiers), session)
        def fill(text, session=None):
            evaluate("a.value=" + json.dumps(text) + ";a.dispatchEvent(new Event('input',{bubbles:true}));a.focus();a.scrollTop=0;a.scrollLeft=0", session)
        def drag(col1, row1, col2, row2, session=None, alt=True):
            bounds = evaluate("(()=>{const r=a.getBoundingClientRect(),s=getComputedStyle(a),c=document.createElement('canvas').getContext('2d');c.font=s.fontStyle+' '+s.fontWeight+' '+s.fontSize+' '+s.fontFamily;return {x:r.left+a.clientLeft+parseFloat(s.paddingLeft)-a.scrollLeft,y:r.top+a.clientTop+parseFloat(s.paddingTop)-a.scrollTop,w:c.measureText('0').width,h:parseFloat(s.lineHeight)};})()", session)
            x, y = bounds['x'] + col1*bounds['w'], bounds['y'] + (row1+.5)*bounds['h']
            end_x, end_y = bounds['x'] + col2*bounds['w'], bounds['y'] + (row2+.5)*bounds['h']
            modifiers = 1 if alt else 0
            command('Input.dispatchMouseEvent', dict(type='mousePressed',x=x,y=y,button='left',clickCount=1,modifiers=modifiers), session)
            for step in range(1, 6):
                command('Input.dispatchMouseEvent', dict(type='mouseMoved',x=x+(end_x-x)*step/5,y=y+(end_y-y)*step/5,button='left',buttons=1,modifiers=modifiers), session)
            command('Input.dispatchMouseEvent', dict(type='mouseReleased',x=end_x,y=end_y,button='left',clickCount=1,modifiers=modifiers), session)
        def value(expected, session=None):
            actual = evaluate('a.value', session)
            assert actual == expected, (expected, actual)
        for _ in range(100):
            try:
                if evaluate("document.readyState==='complete'&&typeof state!=='undefined'"): break
            except AssertionError as exc:
                if 'Execution context was destroyed' not in str(exc): raise
            time.sleep(.05)
        evaluate("(async()=>{window.waitColumn=async fn=>{for(let i=0;i<300;i++){if(fn())return;await new Promise(r=>setTimeout(r,30));}throw Error('列编辑等待超时')};await waitColumn(()=>typeof state!=='undefined'&&Object.values(state.terminals).length>=2&&ws.readyState===1);window.columnKey=Object.keys(state.terminals)[0];window.a=state.terminals[columnKey].panel.querySelector('.terminal-cmd-textarea');a.parentElement.style.height='180px';return true;})()")
        evaluate("window.columnEvents=0;a.addEventListener('input',()=>columnEvents++)")
        fill('abc123\ndef456\nghi789')
        drag(3,0,6,2)
        assert evaluate("document.querySelectorAll('.command-column-overlay').length") == 1
        evaluate('columnEvents=0')
        command('Input.insertText', dict(text='X'))
        value('abcX\ndefX\nghiX')
        assert evaluate('columnEvents') == 1, '列事务应只发一次input'
        key('z', ctrl=True); value('abc123\ndef456\nghi789')
        key('y', ctrl=True); value('abcX\ndefX\nghiX')
        key('Backspace'); value('abc\ndef\nghi')
        drag(1,2,2,0); key('Delete'); value('ac\ndf\ngi')
        fill('abc\ndef\nghi'); drag(1,0,1,2)
        evaluate("a.dispatchEvent(new ClipboardEvent('cut',{bubbles:true,cancelable:true,clipboardData:new DataTransfer()}))")
        value('abc\ndef\nghi')
        command('Input.insertText', dict(text='!')); value('a!bc\nd!ef\ng!hi')
        key('Escape'); command('Input.insertText',dict(text='n')); value('a!nbc\nd!ef\ng!hi')
        key('z',ctrl=True); value('a!bc\nd!ef\ng!hi')
        key('z',ctrl=True); value('abc\ndef\nghi')
        key('z',ctrl=True,shift=True); value('a!bc\nd!ef\ng!hi')
        fill('abc123\ndef456\nghi789'); drag(3,0,6,2)
        copied = evaluate("(()=>{const d=new DataTransfer();a.dispatchEvent(new ClipboardEvent('copy',{bubbles:true,cancelable:true,clipboardData:d}));return d.getData('text/plain')})()")
        assert copied == '123\n456\n789', copied
        evaluate("(()=>{const d=new DataTransfer();a.dispatchEvent(new ClipboardEvent('cut',{bubbles:true,cancelable:true,clipboardData:d}))})()")
        value('abc\ndef\nghi')
        evaluate(r"(()=>{const d=new DataTransfer();d.setData('text/plain','1\r\n22\r\n333');a.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:d}))})()")
        value('abc1\ndef22\nghi333')
        fill('abc\ndef\nghi'); drag(1,0,2,2)
        evaluate(r"(()=>{const d=new DataTransfer();d.setData('text/plain','1\n2');a.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:d}))})()")
        value('a1c\nd2f\ngi')
        fill('abc\nx\n'); drag(2,0,3,2); command('Input.insertText',dict(text='Z')); value('abZ\nxZ\nZ')
        fill('A😀B\nC😀D'); drag(1,0,1,1); key('Delete'); value('AB\nCD')
        fill('Ae\u0301B\nCe\u0301D'); drag(1,0,1,1); key('Delete'); value('AB\nCD')
        fill('中文AB\n中文CD'); drag(4,0,6,1); command('Input.insertText',dict(text='字')); value('中文字\n中文字')
        fill('\tAB\n\tCD'); drag(4,0,6,1); command('Input.insertText',dict(text='T')); value('\tT\n\tT')
        fill('ab12\ncd34'); drag(2,0,4,1)
        command('Input.imeSetComposition',dict(text='中',selectionStart=1,selectionEnd=1))
        evaluate("a.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}))")
        command('Input.insertText',dict(text='中文'))
        value('ab中文\ncd中文')
        key('Escape'); assert evaluate("!document.querySelector('.command-column-overlay')")
        fill('abc\ndef'); drag(0,0,1,1,alt=False); command('Input.insertText',dict(text='N')); value('Nef')
        fill('abc\ndef'); drag(1,0,2,1); evaluate('a.readOnly=true'); key('Delete'); command('Input.insertText',dict(text='Q')); value('abc\ndef'); evaluate('a.readOnly=false')
        fill('abc\ndef'); drag(1,0,2,1); evaluate("a.value='external';a.dispatchEvent(new Event('input',{bubbles:true}))"); key('z',ctrl=True); value('external')
        fill('\n'.join('0123456789'*20 for _ in range(15)))
        evaluate('a.scrollTop=8*parseFloat(getComputedStyle(a).lineHeight);a.scrollLeft=40')
        drag(8,8,9,9); command('Input.insertText',dict(text='X'))
        assert evaluate("a.value.split('\\n')[8][8]==='X'&&a.value.split('\\n')[9][8]==='X'")
        # 缩放/高DPI使用CSS坐标定位，选区层按设备像素绘制。
        command('Emulation.setDeviceMetricsOverride',dict(width=1100,height=900,deviceScaleFactor=1.5,mobile=False))
        fill('abc\ndef'); drag(1,0,2,1)
        screenshot = command('Page.captureScreenshot',dict(format='png'))
        (output/'column-selection.png').write_bytes(base64.b64decode(screenshot['data']))
        command('Input.insertText',dict(text='S')); value('aSc\ndSf')
        fill('send123\nsend456'); drag(4,0,7,1); command('Input.insertText',dict(text='X'))
        key('Enter'); value('')
        key('ArrowUp'); value('sendX\nsendX')
        key('Enter',ctrl=True); value('sendX\nsendX\n')
        key('Enter',shift=True); value('sendX\nsendX\n\n')
        # 三类命令输入框都通过共用样式/事件绑定，独立窗口复用原会话。
        evaluate("(async()=>{document.getElementById('window-serial').click();await waitColumn(()=>!document.getElementById('serial-open').disabled);document.getElementById('serial-open').click();await waitColumn(()=>SerialIntegration.windows.size===1);window.a=[...SerialIntegration.windows.values()][0].panel.querySelector('.terminal-cmd-textarea');a.parentElement.style.height='130px';a.scrollIntoView();return true})()")
        fill('COM123\nCOM456'); drag(3,0,6,1); command('Input.insertText',dict(text='X')); value('COMX\nCOMX')
        evaluate("(async()=>{document.getElementById('window-powershell').click();await waitColumn(()=>!document.getElementById('powershell-open').disabled);document.getElementById('powershell-open').click();await waitColumn(()=>Object.values(state.terminals).some(t=>t.computer&&!t.serialPort));window.a=Object.values(state.terminals).find(t=>t.computer&&!t.serialPort).panel.querySelector('.terminal-cmd-textarea');a.parentElement.style.height='130px';a.scrollIntoView();return true})()")
        fill('PS123\nPS456'); drag(2,0,5,1); command('Input.insertText',dict(text='X')); value('PSX\nPSX')
        evaluate("(async()=>{state.terminals[columnKey].panel.scrollIntoView();state.terminals[columnKey].windowManager.detach(50,50);await waitColumn(()=>state.terminals[columnKey].windowManager.windowId);return true})()")
        window_id = evaluate('state.terminals[columnKey].windowManager.windowId')
        target = None
        for _ in range(100):
            target = next((t for t in command('Target.getTargets')['targetInfos'] if '/desktop-terminal.html#'+window_id+':' in t['url']),None)
            if target: break
            time.sleep(.05)
        assert target
        child = command('Target.attachToTarget',dict(targetId=target['targetId'],flatten=True))['sessionId']
        for _ in range(100):
            try:
                if evaluate("document.readyState==='complete'&&!!document.getElementById('desktop-message')",child): break
            except AssertionError as exc:
                if 'Execution context was destroyed' not in str(exc): raise
            time.sleep(.05)
        evaluate("(async()=>{for(let i=0;i<300;i++){if(document.getElementById('desktop-message').hidden){window.a=document.querySelector('.terminal-cmd-textarea');return true}await new Promise(r=>setTimeout(r,25))}throw Error('独立窗口等待超时')})()",child)
        fill('ab12\ncd34',child); drag(2,0,4,1,child)
        command('Input.imeSetComposition',dict(text='中',selectionStart=1,selectionEnd=1),child)
        evaluate("state.terminals[columnKey].windowManager.snapshot()")
        time.sleep(.15)
        assert evaluate('CommandColumnEditor.isComposing(a)',child),'镜像快照打断输入法组合'
        command('Input.insertText',dict(text='中文'),child); value('ab中文\ncd中文',child)
        fill('child123\nchild456',child); drag(5,0,8,1,child); command('Input.insertText',dict(text='X'),child)
        value('childX\nchildX',child)
        evaluate("(async()=>{await waitColumn(()=>state.terminals[columnKey].panel.querySelector('.terminal-cmd-textarea').value==='childX\\nchildX');return true})()")
        time.sleep(.3)
        command('Input.insertText',dict(text='Y'),child); value('childXY\nchildXY',child)
        evaluate("(()=>{const m=state.terminals[columnKey].windowManager,publish=m.publish;m.publish=function(p){p.controlRevision=0;for(const v of Object.values(p.controls||{}))if(v.value!==undefined)v.value='旧草稿';publish.call(m,p)};m.snapshot();m.publish=publish})()")
        time.sleep(.1); value('childXY\nchildXY',child)
        key('Enter',session=child)
        evaluate("(async()=>{await waitColumn(()=>state.terminals[columnKey].panel.querySelector('.terminal-cmd-textarea').value==='');return true})()")
        # 移除带列选区的临时控件应回收覆盖层。
        evaluate("window.a=document.createElement('textarea');a.className='terminal-cmd-textarea';Object.assign(a.style,{position:'fixed',left:'30px',top:'30px',width:'400px',height:'120px',zIndex:999999});document.body.appendChild(a)")
        fill('abc\ndef'); drag(1,0,2,1); evaluate('a.remove()'); time.sleep(.1)
        assert evaluate("!document.querySelector('.command-column-overlay')")
        print('  [PASS] Alt正反/零宽、替换/删除、剪贴板、撤销重做、中文/Tab/Unicode/IME、短行、滚动/DPI、Enter/历史、只读/外部草稿、三类输入框、独立窗口同步/发送及移除回收')


def run():
    edge = _edge_executable()
    if not edge:
        print('  [SKIP] 未找到Edge，列编辑未验证'); return None
    import server
    from websockets.sync.client import connect
    browser = backend = http = endpoint = manager = None
    previous = server.HTTP_PORT, server.WS_PORT
    output = ROOT/'.agent-runtime/command-column-edit'; output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='column-ui-',ignore_cleanup_errors=True) as temp:
        runtime = Path(temp)
        server.HTTP_PORT = exclusive_port(24380); server.WS_PORT = exclusive_port(24650)
        fake, created = service(runtime/'serial.json')
        try:
            with patch.dict(os.environ,{'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}), patch.object(server,'ADBWorker',_FakeWorker), patch('serial_terminal.SerialService',side_effect=lambda path, *, config_storage=None:service(path, config_storage=config_storage)[0]):
                manager = server.ManagerServer(); manager.serial=fake; manager.extension_tools.services['serial']=fake
                manager._commands_path=lambda:str(runtime/'commands.json')
                failures=[]
                def start():
                    try: asyncio.run(manager.run())
                    except Exception as exc: failures.append(str(exc))
                backend=threading.Thread(target=start,daemon=True);backend.start()
                for _ in range(200):
                    if getattr(manager,'_ws_port',0): break
                    time.sleep(.05)
                assert getattr(manager,'_ws_port',0)
                http=server.start_http_server()
                browser=EdgeSession.start(edge,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-allow-origins=*','--window-size=1180,950',f'http://127.0.0.1:{server._HTTP_PORT}/'],runtime/'edge')
                endpoint=browser.wait_ready(20)
                check(endpoint,output)
                # 多行发送逐行等回显，UI清空不代表后台限速队列已经消费完成。
                for _ in range(100):
                    writes=b''.join(data for key,data in manager.worker.writes if key=='UI-TEST-1:tid=1')
                    if writes==b'sendX\nsendX\nchildXY\nchildXY\n': break
                    time.sleep(.05)
                assert writes==b'sendX\nsendX\nchildXY\nchildXY\n',writes
                assert not any(port.writes for _,_,port in created),'列编辑误发送串口数据'
                assert not failures,failures
                print('  [PASS] 后端逐字节确认：仅Enter发送两次正确命令，无列编辑自动发送/串口误写')
                return True
        finally:
            if browser:
                browser.close()
            if manager: manager.stop()
            if backend: backend.join(timeout=15)
            if http: http.shutdown();http.server_close()
            fake.close();server.HTTP_PORT,server.WS_PORT=previous


if __name__=='__main__':sys.exit(0 if run() is not False else 1)
