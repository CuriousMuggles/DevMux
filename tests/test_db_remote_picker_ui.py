"""Remote DB picker layout and selection in a real sandboxed iframe."""
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
                  const assert=(v,m)=>{if(!v)throw Error(m)};
                  const input=document.createElement('input');
                  const calls=[];
                  const picker=new DBRemoteFiles({el:()=>input,call:async(action,args)=>{calls.push({action,args});return action==='config'?{toolDir:'C:/images'}:({path:'C:/images',parent:'C:/',entries:Array.from({length:80},(_,i)=>({name:'image'+i+'.img',path:'C:/images/'+i+'.img',size:1024,isDir:false}))});}});
                  const pending=picker.open('files');
                  await new Promise(r=>setTimeout(r,30));
                  assert(input.value==='C:/images'&&calls[0].action==='config'&&calls[1].args.path==='C:/images','inherit slave default directory before browse');
                  const layer=picker.layer, list=picker.el('files');
                  const box=layer.getBoundingClientRect(), files=list.getBoundingClientRect(), accept=picker.el('accept').getBoundingClientRect();
                  assert(box.top===0&&Math.abs(box.right-innerWidth)<2&&box.height>=innerHeight-2,'full-height right sidebar');
                  assert(files.height>innerHeight*.4&&list.scrollHeight>list.clientHeight,'large scrollable file list');
                  assert(accept.bottom<=innerHeight&&accept.top>=files.bottom,'visible action below list');
                  list.querySelector('input').click();picker.el('accept').click();
                  assert((await pending).paths[0]==='C:/images/0.img','selected remote file');
                  await new Promise(r=>setTimeout(r,30));
                  const directory=picker.open('directory');await new Promise(r=>setTimeout(r,30));picker.el('accept').click();
                  assert((await directory).path==='C:/images','directory selection');
                  await new Promise(r=>setTimeout(r,30));
                  const cancel=picker.open('files');picker.floating.hide();assert(!(await cancel).success,'close cancels');
                  DevMuxPlugin.dispose();
                  parent.postMessage({type:'sdk-test-done',success:true},'*');
                })().catch(error=>parent.postMessage({type:'sdk-test-done',success:false,error:error.stack},'*'));
                """
                root = Path(__file__).resolve().parents[1]
                css = (root/"plugins/db-flash/db-flash.css").read_text(encoding="utf-8") + (root/"plugin_sdk/ui.css").read_text(encoding="utf-8")
                picker = (root/"plugins/db-flash/db-remote-files.js").read_text(encoding="utf-8")
                html = "<!doctype html><style>"+css+"</style><body><script>"+sdk+"</script><script>"+picker+"</script><script>"+scenario+"</script>"
                result = evaluate("""(async()=>{
                  const frame=document.createElement('iframe');frame.sandbox='allow-scripts';frame.style.cssText='width:900px;height:700px';
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
                print('[PASS] Remote DB picker sidebar geometry, scrolling, file/directory selection and cancellation')
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
