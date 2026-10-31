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
                    if evaluate("!!document.getElementById('tools-plugins') && typeof callWS === 'function'"): break
                    time.sleep(.1)
                evaluate("""window.pluginEnabled=false; window.pluginRemoved=false;
                window.open=()=>({close(){},focus(){}});window.nativeOpened=false;window.installs=[];
                callWS=async(type,payload)=>{
                  if(type==='desktop-terminal-open'){window.nativeOpened=true;window.nativeRequest=payload;return {success:true,data:{windowId:'plugin-window',native:true}};}
                  if(payload.action==='install'){const value=atob(payload.arguments.package);window.installs.push(value);return value==='bad'?{success:false,output:'包无效'}:{success:true,data:{}};}
                  if(payload.action==='list') return {success:true,data:window.pluginRemoved?[]:[{id:'example',name:'Example',version:'1',frontend:'index.html',enabled:window.pluginEnabled}]};
                  if(payload.action==='enable')window.pluginEnabled=true;
                  if(payload.action==='disable')window.pluginEnabled=false;
                  if(payload.action==='uninstall')window.pluginRemoved=true;
                  if(payload.action==='page')return {success:true,data:{html:'<h1>Plugin</h1>'}};
                  return {success:true,data:{}};
                };document.getElementById('tools-plugins').click();""")
                time.sleep(.2)
                assert evaluate("document.querySelector('[data-list]').textContent.includes('Example')")
                number += 1
                ws.send(json.dumps({'id':number,'method':'Page.captureScreenshot','params':{'format':'png'}}))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number:break
                import base64
                screenshot=Path(__file__).resolve().parents[1]/'.agent-runtime/plugin-manager.png'
                screenshot.parent.mkdir(parents=True,exist_ok=True)
                screenshot.write_bytes(base64.b64decode(response['result']['data']))

                assert evaluate("document.querySelector('#plugin-manager input[type=file]').multiple")
                evaluate("""const files=new DataTransfer();for(const [name,value] of [['a.dmp','one'],['bad.dmp','bad'],['b.dmp','two']])files.items.add(new File([value],name));const input=document.querySelector('#plugin-manager input[type=file]');input.files=files.files;input.dispatchEvent(new Event('change',{bubbles:true}));""")
                for _ in range(60):
                    if evaluate("document.querySelector('#plugin-manager [role=status]').textContent.includes('安装完成')"):break
                    time.sleep(.1)
                assert evaluate("JSON.stringify(window.installs)==='[\"one\",\"bad\",\"two\"]'")
                assert evaluate("document.querySelector('#plugin-manager [role=status]').textContent.includes('成功 2 个，失败 1 个') && document.querySelector('#plugin-manager [role=status]').textContent.includes('bad.dmp：包无效')")
                assert evaluate("!document.querySelector('#plugin-manager input').disabled && document.querySelector('#plugin-manager input').value === ''")
                evaluate("document.querySelector('[data-list] [data-action=enable]').click()")
                time.sleep(.2)
                assert evaluate("window.pluginEnabled")
                assert evaluate("document.querySelector('[data-plugin-menu=example]').textContent.includes('Example')")
                evaluate("document.querySelector('[data-plugin-menu=example]').click()")
                time.sleep(.2)
                assert evaluate("window.nativeOpened && window.nativeRequest.view==='plugin' && !document.querySelector('iframe')")
                evaluate("document.querySelector('[data-list] [data-action=disable]').click()")
                time.sleep(.2)
                assert evaluate("!document.querySelector('iframe') && !window.pluginEnabled")
                evaluate("window.confirm=()=>true;document.querySelector('[data-list] [data-action=uninstall]').click()")
                time.sleep(.2)
                assert evaluate("!document.querySelector('[data-plugin-menu=example]')")
                assert evaluate("document.querySelectorAll('#plugin-manager .plugin-install').length === 1 && !!document.querySelector('#plugin-manager .plugin-empty')")
                evaluate("""window.pluginRemoved=false;window.nativeOpened=false;window.pluginEnabled=true;window.remotePlugins=[{frontend:true,id:'example',tool:'example',name:'远程示例',version:'1',enabled:false,remoteAllowed:false,remoteCapable:true}];const original=callWS;callWS=async(type,payload)=>{if(type==='extension-tool'&&payload.action==='targets')return {success:true,data:{targets:[{local:true,target:'computer:local',label:'本机',online:true},{local:false,target:'computer:slave',label:'从机',role:'slave',online:true}]}};if(type==='plugin-manager'&&payload.target==='computer:slave'&&payload.action==='list')return {success:true,data:{plugins:window.remotePlugins,installAllowed:false}};return original(type,payload);};document.getElementById('tools-plugins').click();""")
                time.sleep(.2)
                evaluate("const target=document.querySelector('#plugin-manager [data-target]');target.value='computer:slave';target.dispatchEvent(new Event('change'))")
                time.sleep(.2)
                assert evaluate("document.querySelector('[data-list]').textContent.includes('远程示例') && document.querySelector('[data-list] [data-action=open]').disabled")
                assert evaluate("document.querySelector('#plugin-manager [data-policy]').hidden")
                evaluate("document.querySelector('[data-plugin-menu=example][data-plugin-target=\"computer:slave\"]').click()")
                time.sleep(.1)
                assert evaluate("!window.nativeOpened && document.querySelector('#plugin-manager [role=status]').textContent.includes('请在从机开启')")
                evaluate("window.remotePlugins=[];window.PluginIntegration.refresh()")
                time.sleep(.2)
                assert evaluate("!!document.querySelector('[data-plugin-menu=example][data-plugin-target=\"computer:local\"]') && !document.querySelector('[data-plugin-menu=example][data-plugin-target=\"computer:slave\"]')")
                evaluate("window.remotePlugins=[{frontend:true,id:'remote-only',tool:'relay',name:'从机独有工具',version:'1',enabled:true,remoteAllowed:true,remoteCapable:true}];window.PluginIntegration.refresh()")
                time.sleep(.3)
                assert evaluate("!!document.querySelector('[data-plugin-menu=example][data-plugin-target=\"computer:local\"]')")
                evaluate("document.querySelector('#plugin-manager .diagnostic-close').click();document.getElementById('tools-menu').style.display='block'")
                number += 1
                ws.send(json.dumps({'id':number,'method':'Page.captureScreenshot','params':{'format':'png'}}))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number:break
                (Path(__file__).resolve().parents[1]/'.agent-runtime/plugin-location-menu.png').write_bytes(base64.b64decode(response['result']['data']))
                evaluate("document.querySelector('[data-plugin-menu=remote-only]').click()")
                time.sleep(.2)
                assert evaluate("window.nativeRequest.target==='computer:slave' && window.nativeRequest.key==='plugin:remote-only'")
                print('[OK] 插件列表、批量安装、远程插件过滤、未启用提示与窗口清理')
            edge.close()
            edge = None
            time.sleep(1)
        return True
    finally:
        if edge: edge.close()
        if httpd: httpd.shutdown(); httpd.server_close()
        server.HTTP_PORT = old


if __name__ == '__main__': sys.exit(0 if run() else 1)
