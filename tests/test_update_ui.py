"""主程序升级提示的真实 Edge 交互：卡片渲染、按钮发出正确请求。"""
import json
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_relay_row_layout import _edge_executable, _free_port, _start_edge
from websockets.sync.client import connect


def run():
    import server
    old = server.HTTP_PORT
    server.HTTP_PORT = _free_port()
    httpd = edge = None
    try:
        httpd = server.start_http_server()
        with tempfile.TemporaryDirectory(prefix='app-update-ui-', ignore_cleanup_errors=True) as profile:
            edge = _start_edge(_edge_executable(), profile, 0, f'http://127.0.0.1:{httpd.server_address[1]}/')
            with connect(edge.wait_ready(25), proxy=None) as ws:
                number = 0

                def evaluate(expression):
                    nonlocal number
                    number += 1
                    ws.send(json.dumps({'id': number, 'method': 'Runtime.evaluate',
                                        'params': {'expression': expression, 'returnByValue': True,
                                                   'awaitPromise': True}}))
                    while True:
                        response = json.loads(ws.recv(timeout=20))
                        if response.get('id') == number:
                            result = response['result']
                            if result.get('exceptionDetails'):
                                raise AssertionError(result['exceptionDetails'])
                            return result['result'].get('value')

                for _ in range(100):
                    if evaluate("typeof window.AppUpdate === 'object' && typeof callWS === 'function'"):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('升级模块未加载')

                # 后端用本地桩替代：只验证界面发出的动作与渲染结果。
                evaluate("""window.updateCalls=[];window.updateStage='available';
                callWS=async(type,payload)=>{
                  window.updateCalls.push(payload.action||type);
                  if(type!=='app-update')return {success:true,data:{}};
                  const base={current:'3.1.7',latest:'9.9.9',notes:'修复串口判断',size:34603008,available:true,pending:null};
                  if(payload.action==='status'||payload.action==='check')return {success:true,data:window.updateStage==='pending'
                    ?{...base,available:false,pending:{version:'9.9.9',staged:'C:/tmp/staged'}}
                    :(window.updateStage==='none'?{...base,available:false,latest:'3.1.7'}:base)};
                  if(payload.action==='download'){await new Promise(r=>setTimeout(r,900));
                    return {success:true,data:{version:'9.9.9',staged:'C:/tmp/staged'}};}
                  return {success:true,data:{}};
                };""")

                evaluate("window.AppUpdate.refresh()")
                time.sleep(.4)
                assert evaluate("!document.querySelector('.app-update-card').hidden")
                assert evaluate("document.querySelector('.app-update-title').textContent==='发现新版本 v9.9.9'")
                assert evaluate("document.querySelector('.app-update-body').textContent.includes('33.0 MB')")
                assert evaluate("document.querySelector('.app-update-body').textContent.includes('修复串口判断')")
                assert evaluate("document.getElementById('help-about').classList.contains('has-update')")

                evaluate("document.querySelector('.app-update-btn.primary').click()")
                time.sleep(.2)
                assert evaluate("window.updateCalls.includes('download')")

                # 下载进度用广播事件推进，不依赖后端轮询。
                evaluate("window.AppUpdate.accept({type:'app-update-progress',percent:42})")
                time.sleep(.2)
                assert evaluate("document.querySelector('.app-update-bar').hidden===false")
                assert evaluate("document.querySelector('.app-update-bar>span').style.width==='42%'")

                # 等下载桩返回，卡片先回到「可下载」；再切到待重启状态验证文案与按钮。
                time.sleep(1.1)
                evaluate("window.updateStage='pending';window.AppUpdate.refresh()")
                time.sleep(.4)
                assert evaluate("document.querySelector('.app-update-title').textContent.includes('已下载完成')")
                assert evaluate("document.querySelector('.app-update-actions').textContent.includes('立即重启升级')")

                evaluate("document.querySelector('.app-update-close').click()")
                assert evaluate("document.querySelector('.app-update-card').hidden")

                # 关于对话框里的入口：点一次检查更新要变成版本可用。
                evaluate("window.updateStage='available'")
                evaluate("""window.aboutRow=document.createElement('div');window.aboutRow.className='about-row';
                document.body.append(window.aboutRow);window.AppUpdate.mountAbout(window.aboutRow);
                window.aboutRow.querySelector('.about-update-check').click();""")
                time.sleep(.5)
                assert evaluate("window.aboutRow.querySelector('.about-update-text').textContent==='v9.9.9 可用'")
                print('  [PASS] 升级卡片、下载按钮、进度与关于入口')
                return True
    finally:
        if edge is not None:
            edge.close()
        if httpd is not None:
            httpd.shutdown()
        server.HTTP_PORT = old


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
