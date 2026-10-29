"""米家页面的真实 Edge 交互，云端使用本地模拟。"""
import json
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_relay_row_layout import _edge_executable, _free_port, _start_edge


def run():
    import server
    from websockets.sync.client import connect
    old = server.HTTP_PORT
    server.HTTP_PORT = _free_port()
    httpd = edge = None
    try:
        httpd = server.start_http_server()
        with tempfile.TemporaryDirectory(prefix='mijia-ui-',ignore_cleanup_errors=True) as profile:
            edge = _start_edge(_edge_executable(), profile, 0, f'http://127.0.0.1:{httpd.server_address[1]}/')
            with connect(edge.wait_ready(25), proxy=None) as ws:
                number = 0
                def evaluate(expression):
                    nonlocal number
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True}}))
                    while True:
                        response = json.loads(ws.recv(timeout=20))
                        if response.get('id') == number:
                            result = response['result']
                            if result.get('exceptionDetails'): raise AssertionError(result['exceptionDetails'])
                            return result['result'].get('value')
                for _ in range(80):
                    if evaluate("!!document.querySelector('.mijia-overlay')"): break
                    time.sleep(.1)
                else: raise AssertionError('米家页面未加载')
                evaluate("""window.fakeOn=false; window.snapCount=0; callWS = async (type,payload) => {
                  if(payload.action==='login-status') return {success:true,data:{state:'waiting',qr:'PHN2Zy8+'}};
                  if(payload.action==='status') return {success:true,data:{loggedIn:true}};
                  if(payload.action==='snapshot') { window.snapCount++; return {success:true,data:{devices:[{did:'1',name:'客厅灯',room:'客厅',model:'x.light.a',online:true,on:window.fakeOn}],unavailable:0}}; }
                  if(payload.action==='set-switch') { window.mijiaSent=payload.arguments; window.fakeOn=payload.arguments.value; return {success:true,data:{on:payload.arguments.value}}; }
                  return {success:true,data:{state:'idle'}};
                }; document.getElementById('tools-mijia').click()""")
                evaluate("document.querySelector('.mijia-refresh').click()")
                for _ in range(50):
                    if evaluate("document.querySelector('.mijia-card button')?.textContent.includes('已关闭') || false"): break
                    time.sleep(.1)
                else: raise AssertionError('设备状态未呈现')
                assert evaluate("document.querySelectorAll('.mijia-card').length === 1 && document.querySelector('.mijia-note').textContent.includes('1 台可操作设备')")
                assert evaluate("getComputedStyle(document.querySelector('.mijia-panel')).backgroundColor === getComputedStyle(document.documentElement).getPropertyValue('--bg-dark').trim().replace('#1e1e2e', 'rgb(30, 30, 46)')")
                evaluate("window.fakeOn=true")
                time.sleep(5.5)
                assert evaluate("document.querySelector('.mijia-card button').textContent.includes('已关闭')"), '不应后台自动刷新状态'
                evaluate("document.querySelector('.mijia-refresh').click()")
                for _ in range(80):
                    if evaluate("document.querySelector('.mijia-card button')?.textContent.includes('已开启') || false"): break
                    time.sleep(.1)
                else: raise AssertionError('手动刷新未更新状态')
                evaluate("""window.raceBase=callWS; callWS=(type,payload) => {
                  if(payload.action==='snapshot') return new Promise(resolve=>window.resolveOldSnapshot=resolve);
                  if(payload.action==='set-switch') return new Promise(resolve=>window.resolveSwitch=resolve);
                  return window.raceBase(type,payload);
                }; document.querySelector('.mijia-refresh').click()""")
                for _ in range(40):
                    if evaluate("typeof window.resolveOldSnapshot === 'function'"): break
                    time.sleep(.1)
                else: raise AssertionError('旧刷新未启动')
                evaluate("document.querySelector('.mijia-card button').click()")
                for _ in range(40):
                    if evaluate("typeof window.resolveSwitch === 'function'"): break
                    time.sleep(.1)
                else: raise AssertionError('开关操作未启动')
                evaluate("window.resolveOldSnapshot({success:true,data:{devices:[{did:'1',name:'客厅灯',room:'客厅',model:'x.light.a',on:true}],unavailable:0}})")
                time.sleep(.2)
                assert evaluate("document.querySelector('.mijia-card button').disabled && document.querySelector('.mijia-card button').textContent.includes('正在操作')")
                evaluate("window.fakeOn=false; callWS=window.raceBase; window.resolveSwitch({success:true,data:{on:false}})")
                for _ in range(40):
                    if evaluate("document.querySelector('.mijia-card button')?.textContent.includes('已关闭') || false"): break
                    time.sleep(.1)
                else: raise AssertionError('开关结果未生效')
                before = evaluate("document.querySelector('.mijia-panel').getBoundingClientRect().x")
                evaluate("""(() => { const h=document.querySelector('.mijia-head'); const r=h.getBoundingClientRect();
                  h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:7,clientX:r.x+30,clientY:r.y+15}));
                  document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:7,clientX:r.x+110,clientY:r.y+15}));
                  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:7})); })()""")
                after = evaluate("document.querySelector('.mijia-panel').getBoundingClientRect().x")
                assert after > before + 40, (before, after)
                evaluate("document.querySelector('.mijia-card button').click()")
                for _ in range(50):
                    if evaluate("window.mijiaSent?.value === true && document.querySelector('.mijia-card button').textContent.includes('已开启')"): break
                    time.sleep(.1)
                else: raise AssertionError('开关操作未呈现')
                evaluate("""window.savedMijiaCall = callWS; callWS = (type,payload) => payload.action === 'snapshot'
                  ? new Promise(resolve => {window.finishMijiaRead = resolve;}) : window.savedMijiaCall(type,payload);
                  document.querySelector('.mijia-refresh').click()""")
                for _ in range(40):
                    if evaluate("typeof window.finishMijiaRead === 'function'"): break
                    time.sleep(.1)
                else: raise AssertionError('模拟慢速设备读取未启动')
                evaluate("document.querySelector('.mijia-close').click(); window.finishMijiaRead({success:true,data:{devices:[{did:'delayed',name:'旧结果',room:'客厅',model:'x.light.a',on:true}],unavailable:0}})")
                time.sleep(.2)
                assert evaluate("!document.querySelector('.mijia-card[data-did=delayed]')")
                evaluate("callWS = window.savedMijiaCall; document.getElementById('tools-mijia').click()")
                evaluate("document.querySelector('.mijia-login').click()")
                for _ in range(40):
                    if evaluate("document.querySelector('.mijia-qr img').getAttribute('src')?.startsWith('data:image/svg+xml;base64,') || false"): break
                    time.sleep(.1)
                else: raise AssertionError('扫码弹窗未显示二维码')
                evaluate("document.querySelector('.mijia-cancel').click()")
                assert evaluate("document.querySelector('.mijia-qr').hidden && !document.querySelector('.mijia-qr img').hasAttribute('src')")
                evaluate("document.querySelector('.mijia-close').click()")
                assert evaluate("document.querySelector('.mijia-overlay').hidden")
                closed_count = evaluate('window.snapCount')
                time.sleep(5.5)
                assert evaluate('window.snapCount') == closed_count, '关闭窗口后仍在轮询'
                print('[OK] 米家浮动窗拖动、设备筛选、开关、扫码与关闭')
            edge.close()
            edge = None
            time.sleep(1)
        return True
    finally:
        if edge: edge.close()
        if httpd: httpd.shutdown(); httpd.server_close()
        server.HTTP_PORT = old


if __name__ == '__main__': sys.exit(0 if run() else 1)
