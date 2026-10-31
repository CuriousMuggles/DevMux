"""米家页面的真实 Edge 交互，云端使用本地模拟。"""
import json
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_relay_row_layout import _edge_executable, _free_port, _start_edge


def run_for(identifier):
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
                from plugin_pack import pack
                import zipfile
                with tempfile.TemporaryDirectory() as package_temp:
                    package=pack(Path(__file__).resolve().parents[1]/'plugins'/identifier,Path(package_temp)/'frontend.dmp')
                    with zipfile.ZipFile(package) as archive:
                        page=archive.read('index.html').decode('utf-8')
                manifest = json.loads((Path(__file__).resolve().parents[1] / 'plugins' / identifier / 'manifest.json').read_text(encoding='utf-8-sig'))
                manifest['enabled'] = True
                evaluate("window.pluginFixture=" + json.dumps(manifest) + ";window.pluginPage=" + json.dumps(page))
                evaluate("""callWS=async(type,payload)=>{
                  if(type==='extension-tool') {
                    if(payload.action==='targets')return {success:true,data:{targets:[{target:'computer:local',local:true,online:true,label:'本机',role:'slave'}]}};
                    if(payload.action==='capabilities')return {success:true,data:{allowed:true,remoteAllowed:false}};
                    return {success:true,data:{toolDir:'',opened:false,devices:[],states:[],job:null}};
                  }
                  if(payload.action==='list')return {success:true,data:[window.pluginFixture]};
                  if(payload.action==='page')return {success:true,data:{html:window.pluginPage}};
                  if(payload.action==='call')return {success:true,data:{success:true,data:{loggedIn:false}}};
                  return {success:true,data:{}};
                };document.getElementById('tools-plugins').click();""")
                time.sleep(.2)
                evaluate("""window.frame=document.createElement('iframe');frame.sandbox='allow-scripts';frame.srcdoc=window.pluginPage;frame.style.cssText='width:800px;height:600px';document.body.append(frame);
                window.addEventListener('message',async event=>{if(event.source!==frame.contentWindow)return;const q=event.data;if(!['devmux-plugin-call','devmux-host-call'].includes(q.type))return;const result=q.type==='devmux-host-call'?await callWS(q.hostType,q.hostPayload):(await callWS('plugin-manager',{action:'call'})).data;frame.contentWindow.postMessage({type:'devmux-plugin-result',id:q.id,result},'*');});""")
                time.sleep(.8)
                number += 1
                ws.send(json.dumps({'id':number,'method':'Page.getFrameTree'}))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number: break
                session = None
                if 'childFrames' not in response['result']['frameTree']:
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Target.getTargets'}))
                    while True:
                        response=json.loads(ws.recv(timeout=20))
                        if response.get('id')==number:break
                    target=next(t['targetId'] for t in response['result']['targetInfos'] if t['type']=='iframe')
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Target.attachToTarget','params':{'targetId':target,'flatten':True}}))
                    while True:
                        response=json.loads(ws.recv(timeout=20))
                        if response.get('id')==number:break
                    session=response['result']['sessionId']
                    context=None
                else:
                    frame=response['result']['frameTree']['childFrames'][0]['frame']['id']
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Page.createIsolatedWorld','params':{'frameId':frame,'worldName':'plugin-test'}}))
                    while True:
                        response=json.loads(ws.recv(timeout=20))
                        if response.get('id')==number:break
                    context=response['result']['executionContextId']
                selector={'mijia':'.mijia-login','db-flash':'.db-flash-header','usb-relay':'#usb-relay-scan','example':'.dmp-form'}[identifier]
                number += 1
                request={'id':number,'method':'Runtime.evaluate','params':{'expression':"(() => {const el=document.querySelector("+json.dumps(selector)+");const page=document.querySelector('.dmp-page');return !!el && !!page && Math.abs(page.getBoundingClientRect().width-innerWidth)<3 && !!document.querySelector('.dmp-page-header') && !!window.DevMuxPlugin;})()",'returnByValue':True}}
                if session:request['sessionId']=session
                else:request['params']['contextId']=context
                ws.send(json.dumps(request))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number:break
                assert response['result']['result'].get('value'), (identifier,response)
                evaluate("window.frame.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:99999'")
                number += 1
                ws.send(json.dumps({'id':number,'method':'Page.captureScreenshot','params':{'format':'png'}}))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number:break
                import base64
                output=Path(__file__).resolve().parents[1]/'.agent-runtime'/('plugin-sdk-'+identifier+'.png')
                output.parent.mkdir(parents=True,exist_ok=True)
                output.write_bytes(base64.b64decode(response['result']['data']))
                print('[OK] plugin frontend', identifier)

            edge.close()
            edge = None
            time.sleep(1)
        return True
    finally:
        if edge: edge.close()
        if httpd: httpd.shutdown(); httpd.server_close()
        server.HTTP_PORT = old


def run():
    return all(run_for(identifier) for identifier in ('usb-relay','db-flash','mijia','example'))

if __name__ == '__main__': sys.exit(0 if run() else 1)
