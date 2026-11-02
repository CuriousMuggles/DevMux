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
                  if(type==='device-adb') {
                    if(payload.operation==='targets')return {targets:[{target:'device:test',label:'Test ARM64',online:true}]};
                    if(payload.operation==='deploy')return {success:true,data:{path:'/tmp/devmux-ebpf-'+ 'a'.repeat(24)}};
                    const command=payload.argv?.[1]||'';
                    if(command.startsWith('cat /proc/')){window.ebpfMapReads=(window.ebpfMapReads||0)+1;return {success:true,stdout:'00400000-00480000 r-xp 00000000 08:01 1 /tmp/fixture\\n'+(window.ebpfMapsChurn&&window.ebpfMapReads>=3?'00600000-00610000 rw-p 00000000 00:00 0 [heap]\\n':'')+(window.ebpfMapsChange&&window.ebpfMapReads>=3?'124':'123')};}
                    if(/^(readlink |stat |sha256sum |dd )/.test(command))throw Error('legacy ELF transfer must not run');
                    return {success:true,stdout:'aarch64\\n0\\nfixture'};
                  }
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
                window.addEventListener('message',async event=>{if(event.source!==frame.contentWindow)return;const q=event.data;if(q.type==='ebpf-fixture-maps-churn'){window.ebpfMapsChurn=q.value;window.ebpfMapReads=0;return;}if(q.type==='ebpf-fixture-no-symbols'){window.ebpfNoSymbols=q.value;return;}if(q.type==='ebpf-fixture-maps-change'){window.ebpfMapsChange=q.value;window.ebpfMapReads=0;return;}if(q.type==='ebpf-fixture-failure'){window.ebpfFailKernel=q.value;return;}if(!['devmux-plugin-call','devmux-host-call'].includes(q.type))return;let result;
                if(q.type==='devmux-host-call')result=await callWS(q.hostType,q.hostPayload);
                else if(window.pluginFixture.id==='ebpf'){
                  if(q.action==='asset')result=window.ebpfFailKernel&&q.arguments.mode==='kernel'?{success:false,output:'fixture kernel failure'}:{data:'fixture',sha256:'a'.repeat(64)};
                  else if(q.action==='plan')result={path:'/tmp/devmux-ebpf-'+ 'a'.repeat(24),ticket:'/tmp/test.pid',command:'fixture',cancel:'fixture',timeout:10,sessionId:'a'.repeat(32)};
                  else if(q.action==='analyze')result={schemaVersion:1,facts:[{metric:'completedCalls',value:12},{metric:'overflowCalls',value:2,lowerBoundNs:2031616}],overflowRatio:2/12,quality:{status:'partial',lostEvents:null,unmatchedExits:null,stackFailures:null},limitations:['不能据此判定根因'],recommendations:['关联慢调用栈']};
                  else if(q.action==='parse-session')result={...q.arguments.report,slowCalls:q.arguments.report.slowCalls.map(s=>({...s,durationNs:s.exitNs-s.enterNs}))};
                  else if(q.action==='symbol-chunk')result={token:'fixture-token'};
                  else if(q.action==='symbol-release')result={};
                  else if(q.action==='symbol-plan')result={path:'/tmp/devmux-ebpf-'+ 'a'.repeat(24),command:'fixture-symbols',timeout:8};
                  else if(q.action==='symbol-result')result={frames:q.arguments.addresses.map(address=>({address,symbol:address==='0xffff800081f850a4'||window.ebpfNoSymbols?null:q.arguments.kind==='kernel'?'bpf_trace_run2':'test_cycle',offset:16,status:address==='0xffff800081f850a4'?'kernel-address-unmapped':window.ebpfNoSymbols?(q.arguments.kind==='kernel'?'kernel-symbol-unavailable':'no-function-symbol'):'resolved'})),resolvedFrames:window.ebpfNoSymbols?0:q.arguments.addresses.length};
                  else if(q.action==='resolve-user')result={frames:[{symbol:'test_cycle',offset:16,status:'resolved'}],resolvedFrames:1};
                  else if(q.action==='parse'&&q.arguments.mode==='session')result={samples:12,buckets:[10,...Array(30).fill(0),2],raw:'fixture joint',session:{sessionId:'a'.repeat(32),pid:956,slowThresholdNs:2000000,kernelProbeRanges:[{name:'devmux_sys_enter',start:'0xffff800081f85000',size:512}],quality:{completedCalls:12,unmatchedExits:1,mapUpdateFailures:0,stackFailures:0,droppedSlowCalls:1},slowCalls:[{tid:958,syscallId:101,durationNs:5000000,kernelStack:['0xffff800000001000','0xffff800081f850a4'],userStack:['0x400590']}]}};
                  else if(q.action==='parse')result=q.arguments.mode==='latency'?{samples:12,buckets:[10,...Array(30).fill(0),2],raw:'fixture histogram'}:{frames:['0x0000000000400590','0x0000000000400814'],raw:'fixture stack'};
                }else result=(await callWS('plugin-manager',{action:'call'})).data;
                frame.contentWindow.postMessage({type:'devmux-plugin-result',id:q.id,result},'*');});""")
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
                selector={'mijia':'.mijia-login','db-flash':'.db-flash-header','usb-relay':'#usb-relay-scan','example':'.dmp-form','ebpf':'.ebpf-toolbar'}[identifier]
                number += 1
                request={'id':number,'method':'Runtime.evaluate','params':{'expression':"(() => {const el=document.querySelector("+json.dumps(selector)+");const page=document.querySelector('.dmp-page');return !!el && !!page && Math.abs(page.getBoundingClientRect().width-innerWidth)<3 && !!document.querySelector('.dmp-page-header') && !!window.DevMuxPlugin;})()",'returnByValue':True}}
                if session:request['sessionId']=session
                else:request['params']['contextId']=context
                ws.send(json.dumps(request))
                while True:
                    response=json.loads(ws.recv(timeout=20))
                    if response.get('id')==number:break
                assert response['result']['result'].get('value'), (identifier,response)
                if identifier=='ebpf':
                    number += 1
                    expression="""(async()=>{
                      const pid=document.querySelector('[name=pid]'),mode=document.querySelector('[name=mode]');if(mode.value!=='session')throw Error('joint diagnosis must be default');pid.value='956';
                      const userTab=Array.from(document.querySelectorAll('.ebpf-tabs button')).find(b=>b.textContent==='用户态');userTab.click();if(mode.value!=='user')throw Error('stack tab did not select capture mode');await new Promise(r=>setTimeout(r,0));mode.value='latency';
                      const run=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='开始采集');
                      const wait=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,30));}throw Error('UI fixture timeout');};
                      run.click();await wait(()=>document.querySelector('.ebpf-chart')||document.querySelector('.ebpf-panel svg'));
                      await wait(()=>!run.disabled);if(document.querySelectorAll('.ebpf-stat').length!==4)throw Error('summary missing');
                      if(!document.querySelector('.ebpf-analysis').textContent.includes('16.67%')||!document.querySelector('.ebpf-analysis').textContent.includes('未知'))throw Error('evidence or unknown quality missing');
                      mode.value='user';run.click();await wait(()=>document.querySelector('.ebpf-stack-body td'));await wait(()=>!run.disabled);
                      if(document.querySelectorAll('.ebpf-record').length!==2)throw Error('history missing');
                      const collapse=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='收起配置');collapse.click();if(!document.querySelector('.ebpf-shell.collapsed'))throw Error('collapse failed');await new Promise(r=>setTimeout(r,0));collapse.click();await new Promise(r=>setTimeout(r,0));if(document.querySelector('.ebpf-shell.collapsed'))throw Error('expand failed');
                      if(document.querySelector('.ebpf-raw').open)throw Error('raw must default collapsed');
                      Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='导出结果').click();
                      const exported=JSON.parse(document.querySelector('.ebpf-export').value);if(exported.mode!=='user'||exported.pid!==956)throw Error('export mismatch');
                      document.querySelector('dialog[open]').close();
                      if(Array.from(mode.options).some(o=>o.value==='all'))throw Error('duplicate one-click pipeline must be removed');
                      const report={schemaVersion:1,sessionId:'a'.repeat(32),pid:956,startNs:0,endNs:10000000,slowThresholdNs:1000000,quality:{completedCalls:1,unmatchedExits:0,mapUpdateFailures:0,stackFailures:1,droppedSlowCalls:0},slowCalls:[{sessionId:'a'.repeat(32),pid:956,tid:957,syscallId:63,enterNs:1000000,exitNs:5000000,kernelStack:['0xffff800000001000'],userStack:null}]};
                      const input=document.querySelector('input[type=file]'),transfer=new DataTransfer();transfer.items.add(new File([JSON.stringify(report)],'session.json',{type:'application/json'}));input.files=transfer.files;input.dispatchEvent(new Event('change'));
                      await wait(()=>document.querySelector('.ebpf-correlated-stacks').textContent.includes('TID 957'));
                      if(!document.querySelector('.ebpf-correlated-stacks').textContent.includes('未采到')||!document.querySelector('.ebpf-correlated-stacks').textContent.includes('4.000 ms'))throw Error('correlated duration or unknown stack missing');
                      mode.value='session';run.click();await wait(()=>!run.disabled);
                      if(!document.querySelector('.ebpf-correlated-stacks').textContent.includes('TID 958')||!document.querySelector('.ebpf-correlated-stacks').textContent.includes('5.000 ms'))throw Error('live joint report missing');if(!document.querySelector('.ebpf-correlated-stacks').textContent.includes('test_cycle+0x10'))throw Error('automatic userspace symbol missing');
                      if(!document.querySelector('.ebpf-stack-body').textContent.includes('test_cycle+0x10')||!document.querySelector('.ebpf-badge').textContent.includes('1/1'))throw Error('main stack panel must show resolved joint symbols');
                      Array.from(document.querySelectorAll('.ebpf-tabs button')).find(b=>b.textContent==='内核态').click();await new Promise(r=>setTimeout(r,0));if(mode.value!=='session'||!document.querySelector('.ebpf-stack-body').textContent.includes('bpf_trace_run2+0x10')||!document.querySelector('.ebpf-badge').textContent.includes('2/2')||!document.querySelector('.ebpf-stack-body').textContent.includes('devmux_sys_enter [采集探针]'))throw Error('kernel symbols missing or joint mode changed');userTab.click();await new Promise(r=>setTimeout(r,0));
                      Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='导出结果').click();
                      const joint=JSON.parse(document.querySelector('dialog[open] .ebpf-export').value);if(!joint.simultaneous||joint.mode!=='session'||joint.result.session.pid!==956)throw Error('joint export mismatch');document.querySelector('dialog[open]').close();
                      parent.postMessage({type:'ebpf-fixture-maps-change',value:true},'*');await new Promise(r=>setTimeout(r,30));run.click();await wait(()=>!run.disabled);
                      if(document.querySelector('.ebpf-correlated-stacks').textContent.includes('test_cycle')||!document.querySelectorAll('.ebpf-analysis')[1].textContent.includes('映射变化'))throw Error('mapping change did not invalidate symbols');
                      parent.postMessage({type:'ebpf-fixture-maps-change',value:false},'*');parent.postMessage({type:'ebpf-fixture-no-symbols',value:true},'*');await new Promise(r=>setTimeout(r,30));run.click();await wait(()=>!run.disabled);
                      if(document.querySelector('.ebpf-correlated-stacks').textContent.includes('test_cycle')||!document.querySelector('.ebpf-correlated-stacks').textContent.includes('没有函数符号'))throw Error('missing symbol reason not displayed');
                      parent.postMessage({type:'ebpf-fixture-no-symbols',value:false},'*');parent.postMessage({type:'ebpf-fixture-maps-churn',value:true},'*');await new Promise(r=>setTimeout(r,30));run.click();await wait(()=>!run.disabled);
                      if(!document.querySelector('.ebpf-correlated-stacks').textContent.includes('test_cycle+0x10'))throw Error('unrelated mapping change must not invalidate symbols');
                      return true;
                    })()"""
                    request={'id':number,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True}}
                    if session:request['sessionId']=session
                    else:request['params']['contextId']=context
                    ws.send(json.dumps(request))
                    while True:
                        response=json.loads(ws.recv(timeout=20))
                        if response.get('id')==number:break
                    assert response['result'].get('result',{}).get('value'),response
                evaluate("window.frame.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:99999'")
                if identifier=='ebpf':
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Emulation.setDeviceMetricsOverride','params':{'width':1400,'height':900,'deviceScaleFactor':1,'mobile':False}}))
                    while True:
                        response=json.loads(ws.recv(timeout=20))
                        if response.get('id')==number:break
                    time.sleep(.2)
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
    return all(run_for(identifier) for identifier in ('usb-relay','db-flash','mijia','example','ebpf'))

if __name__ == '__main__': sys.exit(0 if run() else 1)
