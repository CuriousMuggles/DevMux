"""Public SDK behavior in a real sandboxed browser iframe."""
import json
from pathlib import Path
import tempfile
import time
from tests.test_relay_row_layout import _edge_executable, _free_port, _start_edge


def run():
    import server
    from websockets.sync.client import connect
    old = server.HTTP_PORT
    server.HTTP_PORT = _free_port()
    httpd = edge = None
    try:
        httpd = server.start_http_server()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            edge = _start_edge(_edge_executable(), profile, 0, f'http://127.0.0.1:{httpd.server_address[1]}/')
            with connect(edge.wait_ready(25), proxy=None) as ws:
                number = 0
                def evaluate(expression):
                    nonlocal number
                    number += 1
                    ws.send(json.dumps({'id':number,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True}}))
                    while True:
                        response = json.loads(ws.recv(timeout=25))
                        if response.get('id') == number:
                            assert 'result' in response,response
                            result = response['result']
                            if result.get('exceptionDetails'): raise AssertionError(result['exceptionDetails'])
                            return result['result'].get('value')
                for _ in range(80):
                    if evaluate("document.readyState==='complete' && !!document.getElementById('tools-plugins')"):break
                    time.sleep(.1)
                sdk = (Path(__file__).resolve().parents[1]/'plugin_sdk/sdk.js').read_text(encoding='utf-8')
                scenario = r"""
                (async()=>{
                  const P=DevMuxPlugin, assert=(v,m)=>{if(!v)throw Error(m)};
                  assert(P.apiVersion===1,'version');
                  assert((await P.call('echo',{text:'hello'})).text==='hello','call unwrap');
                  assert((await P.hostCall('pick-file',{})).path==='fixture','host call');
                  try {await P.call('failure');throw Error('missing rejection')}catch(e){assert(e.message==='fixture failure','error message')}
                  const timed=await P.request('plugin',{action:'ignored'},10);assert(!timed.success&&timed.code==='timeout','timeout');
                  const denied=await P.request('unprovided-interface');assert(!denied.success,'unknown host interface');
                  const form=P.ui.form([{name:'count',type:'number'},{name:'enabled',type:'boolean'},{name:'mode',type:'select',options:[{value:'a',label:'A'}]}],{count:3,enabled:true,mode:'a'});
                  assert(JSON.stringify(form.values())===JSON.stringify({count:3,enabled:true,mode:'a'}),'form types');
                  const table=P.ui.table([{key:'text',label:'Text'}],[{text:'<img src=x onerror=alert(1)>'}]);assert(!table.querySelector('img'),'safe table text');
                  const log=P.ui.log({limit:2});log.appendLine('one');log.appendLine('two');log.appendLine('three');assert(log.textContent==='two\nthree','bounded log');
                  const s=P.ui.status('before');s.update('after','error');assert(s.textContent==='after'&&s.getAttribute('role')==='status','status');
                  const view=P.ui.mount({title:'SDK fixture',content:form});assert(view.body.contains(form),'mount');
                  P.ui.registerView('fixture',()=>document.createTextNode('registered'));const holder=document.createElement('div');P.ui.renderView('fixture',holder);assert(holder.textContent==='registered','view registration');
                  let events=0,ticks=0;const el=document.createElement('button');P.listen(el,'click',()=>events++);el.click();P.interval(()=>ticks++,5);
                  const cleaned=[];P.onDispose(()=>cleaned.push('first'));P.onDispose(()=>cleaned.push('last'));
                  await new Promise(r=>setTimeout(r,15));const pending=P.request('plugin',{action:'ignored'},10000);P.dispose();P.dispose();
                  assert((await pending).code==='disposed','pending disposed');const stopped=ticks;el.click();await new Promise(r=>setTimeout(r,15));
                  assert(events===1&&ticks===stopped,'resource cleanup');assert(cleaned.join(',')==='last,first','cleanup once/reverse');
                  parent.postMessage({type:'sdk-test-done',success:true},'*');
                })().catch(error=>parent.postMessage({type:'sdk-test-done',success:false,error:error.stack},'*'));
                """
                html = '<!doctype html><script>'+sdk+'</script><script>'+scenario+'</script>'
                result = evaluate("""(async()=>{
                  const frame=document.createElement('iframe');frame.sandbox='allow-scripts';
                  const done=new Promise(resolve=>window.addEventListener('message',event=>{
                    if(event.source!==frame.contentWindow)return;const q=event.data;
                    if(q.type==='sdk-test-done'){resolve(q);return;}
                    if(q.type==='devmux-plugin-call'&&q.action==='ignored')return;
                    const result=q.type==='devmux-host-call'?{success:true,data:{path:'fixture'}}:q.action==='failure'?{success:false,output:'fixture failure'}:{success:true,data:q.arguments};
                    frame.contentWindow.postMessage({type:'devmux-plugin-result',id:q.id,result},'*');
                  }));
                  frame.srcdoc="""+json.dumps(html)+""";document.body.append(frame);
                  return await Promise.race([done,new Promise(resolve=>setTimeout(()=>resolve({success:false,error:'SDK browser timeout'}),15000))]);
                })()""")
                assert result and result.get('success'), result
                print('[PASS] SDK browser RPC, components, timeout and resource disposal')
            edge.close(); edge = None
            time.sleep(1)
        return True
    finally:
        if edge: edge.close()
        if httpd: httpd.shutdown(); httpd.server_close()
        server.HTTP_PORT = old


if __name__ == '__main__':
    import sys
    sys.exit(0 if run() else 1)
