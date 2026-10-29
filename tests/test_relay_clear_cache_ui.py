"""真实Edge/HTTP/WS验证清除缓存按钮；目录、配置及ADB全部隔离。"""
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tests import test_ui_e2e as base
from tests.edge_test_support import EdgeSession
import relay_app
import relay
import relay_archive
import zipfile
from tests.test_relay_clear_cache import make_history

SCRIPT = r"""
(async () => {
  const delay = ms => new Promise(r=>setTimeout(r,ms));
  const wait = async fn => {for(let i=0;i<200;i++){if(fn())return;await delay(30);}throw new Error('清理页面等待超时');};
  const $ = id=>document.getElementById(id);
  const results=[];
  const record=(name,ok)=>{results.push({name,ok:!!ok});if(!ok)throw new Error(name);};
  await wait(()=>ws && ws.readyState===1);
  $('tools-relay').click();
  await wait(()=>$('relay-config-overlay').style.display==='flex' && state.relay.config);
  const button=$('relay-clear-cache'), outcome=$('relay-clear-cache-result');
  record('共享目录旁显示清除缓存按钮',button.textContent==='清理闲置缓存' && button.getBoundingClientRect().width>0);
  const dialog=document.querySelector('#relay-config-overlay .relay-config-dialog');
  const width=dialog.style.width;dialog.style.width='520px';
  const row=button.closest('.setting-row');
  record('窄配置窗共享目录按钮行无溢出',row.scrollWidth<=row.clientWidth);
  dialog.style.width=width;
  const original=callWS;
  let clears=0;
  callWS=(type,...args)=>{if(type==='relay-clear-cache')clears++;return original(type,...args);};
  const savedRoot=$('relay-root').value;
  $('relay-root').value=savedRoot+'-unsaved';
  $('relay-root').dispatchEvent(new Event('input',{bubbles:true}));
  button.click();
  record('未保存配置禁止清理',outcome.textContent.includes('未保存') && clears===0);
  $('relay-root').value=savedRoot;
  $('relay-root').dispatchEvent(new Event('input',{bubbles:true}));
  button.click();
  await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  record('确认框显示各部署闲置清理范围及保留项',$('relay-cache-confirm-path').textContent.includes(savedRoot)
    && $('relay-cache-confirm-overlay').textContent.includes('不可恢复'));
  const cancel=$('relay-cache-confirm-cancel');
  const rect=cancel.getBoundingClientRect();
  record('确认框实际处于浮窗上方',document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2)===cancel);
  cancel.click();
  await wait(()=>!button.disabled);
  record('取消不发送清理请求',clears===0);
  button.click();await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  await wait(()=>!button.disabled);
  record('Escape取消不发送清理请求',clears===0);
  $('tools-relay').click();
  await wait(()=>$('relay-config-overlay').style.display==='flex');
  button.click();await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  $('relay-cache-confirm-delete').click();
  await wait(()=>clears===1);
  record('执行期间禁止重复点击/保存/刷新',button.disabled && $('relay-save').disabled && $('relay-refresh').disabled);
  const quick = await callWS('get-commands', {}, 2000);
  record('清理等待期间同连接其他请求先完成',quick.success && button.disabled);
  const duplicate = await original('relay-clear-cache', {root:savedRoot,confirmed:true},2000);
  record('后端拒绝同连接重复清理',!duplicate.success && duplicate.output.includes('仍在进行'));
  button.click();
  await wait(()=>!button.disabled);
  record('清理成功且中转保持启用',outcome.textContent.includes('闲置缓存已清理') && outcome.textContent.includes('减少 2.00 MiB') && outcome.textContent.includes('保留原因') && $('relay-enabled').checked && clears===1);
  callWS=original;
  return {ok:results.every(x=>x.ok),results};
})()
"""
FAIL_SCRIPT = r"""
(async()=>{
  const delay=ms=>new Promise(r=>setTimeout(r,ms));
  const wait=async fn=>{for(let i=0;i<200;i++){if(fn())return;await delay(30);}throw new Error('失败界面等待超时');};
  const $=id=>document.getElementById(id);
  await wait(()=>ws&&ws.readyState===1);
  $('tools-relay').click();await wait(()=>$('relay-config-overlay').style.display==='flex'&&state.relay.config);
  $('relay-clear-cache').click();await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  $('relay-cache-confirm-delete').click();await wait(()=>!$('relay-clear-cache').disabled);
  const partial=$('relay-clear-cache-result').textContent.includes('未完全清理')&&$('relay-clear-cache-result').textContent.includes('sess-locked.zip');
  const original=callWS;let count=0;
  callWS=(type,...args)=>{if(type==='relay-clear-cache'){count++;return Promise.resolve({success:false,output:'timeout'});}return original(type,...args);};
  $('relay-clear-cache').click();await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  $('relay-cache-confirm-delete').click();await wait(()=>!$('relay-clear-cache').disabled);
  const unknown=$('relay-clear-cache-result').textContent.includes('结果未确认')&&$('relay-clear-cache-result').textContent.includes('不会自动重试')&&count===1;
  callWS=original;
  return {ok:partial&&unknown,partial,unknown};
})()
"""

RESUME_SCRIPT = r"""
(async()=>{
  const delay=ms=>new Promise(r=>setTimeout(r,ms));
  const wait=async fn=>{for(let i=0;i<240;i++){if(fn())return;await delay(30);}throw new Error('续扫界面等待超时');};
  const $=id=>document.getElementById(id);
  await wait(()=>ws&&ws.readyState===1);
  $('tools-relay').click();await wait(()=>$('relay-config-overlay').style.display==='flex'&&state.relay.config);
  $('relay-clear-cache').click();await wait(()=>$('relay-cache-confirm-overlay').style.display==='flex');
  $('relay-cache-confirm-delete').click();await wait(()=>!$('relay-clear-cache').disabled);
  return {text:$('relay-clear-cache-result').textContent,enabled:$('relay-enabled').checked};
})()
"""

def run():
    if os.name!='nt': return None
    import server as srv
    edge=base._edge_executable()
    if not edge: raise RuntimeError('Edge不可用')
    with tempfile.TemporaryDirectory(prefix='DevMux-cache-ui-') as folder:
        directory=Path(folder);share=directory/'share';share.mkdir()
        for name in ('custom.txt','.hidden','pool/a/one','pool/b/two'):
            path=share/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'cache')
        config=directory/'local'/'relay.json';config.parent.mkdir()
        relay_app.save_config(str(config),relay_app.RelayConfig(root=str(share),enabled=True,role='master',peerId='cache-ui',nodeId='node-cache-ui'))
        history=make_history(share, peer='cache-ui', age_s=60)
        other_history=make_history(share, peer='other-cache', pool_id='other-pool', age_s=60)
        active=share/'streams/cache-ui/sess-active.out'
        active.parent.mkdir(parents=True,exist_ok=True)
        active.write_bytes(b'ACTIVE_OUTPUT')
        active_meta=active.with_name('sess-active.meta.json')
        active_meta.write_text('{"status":"running"}')
        moment=time.time()-1200
        os.utime(active,(moment,moment));os.utime(active_meta,(moment,moment))
        compact=share/'archive/cache-ui/2026-10'/('compact-'+'a'*32+'.zip')
        compact.parent.mkdir(parents=True,exist_ok=True)
        index=json.dumps({'schema':1,'records':[{'reqId':'b'*16,'result':True}]}).encode()
        with zipfile.ZipFile(compact,'w') as package:
            package.writestr('__index__.json',index)
            package.writestr('records/old-output.bin',os.urandom(2*1024*1024))
        os.utime(compact,(moment,moment))
        compact_before=compact.stat().st_size
        config_before=config.read_bytes()
        process=httpd=manager=thread=None
        cdp=None
        handle=None
        old_ports=(srv.HTTP_PORT,srv.WS_PORT,srv._WS_PORT,srv.LOCAL_DEVICES_ENABLED)
        try:
            with patch.dict(os.environ,{'RELAY_CONFIG':str(config),'ADB_AGENT_RUNTIME_DIR':str(directory/'runtime')}),patch.object(srv,'ADBWorker',base._FakeWorker),patch.object(srv,'get_all_devices',lambda:[]):
                srv.LOCAL_DEVICES_ENABLED=False
                srv.HTTP_PORT=base._free_port(18480);srv.WS_PORT=base._free_port(18580);srv._WS_PORT=srv.WS_PORT
                httpd=srv.start_http_server();manager=srv.ManagerServer()
                ready=threading.Event()
                def serve():
                    async def loop():
                        task=asyncio.create_task(manager.run())
                        while not getattr(manager,'_ws_port',None): await asyncio.sleep(.01)
                        ready.set();await task
                    asyncio.run(loop())
                thread=threading.Thread(target=serve,daemon=True);thread.start()
                if not ready.wait(10): raise RuntimeError('测试服务启动超时')
                # 延时用于真实页面观察忙碌按钮；仍执行真实清理。
                original_clear=manager.relay.clear_cache
                def slow_clear(*args,**kwargs): time.sleep(1);return original_clear(*args,**kwargs)
                manager.relay.clear_cache=slow_clear
                url=f'http://127.0.0.1:{httpd.server_address[1]}/?_t=cache-ui'
                process=EdgeSession.start(edge,['--headless=old','--no-first-run','--no-default-browser-check',
                    '--remote-allow-origins=*','--window-size=1200,800',url],directory/'edge')
                target=process.wait_ready(20)
                cdp=process.port
                result=base._cdp_eval(target,url,SCRIPT)
                for item in result['results']: print(f'  [{"PASS" if item["ok"] else "FAIL"}] {item["name"]}')
                assert result['ok'],result
                assert not history.exists()
                assert not other_history.exists()
                assert compact.stat().st_size < compact_before/100
                with zipfile.ZipFile(compact) as package:
                    assert package.read('__index__.json')==index
                    assert package.namelist()==['__index__.json']
                relay_archive._CACHE.clear();relay_archive._BUNDLE_CACHE.clear()
                assert relay.RelayQueue.for_peer(str(share),'cache-ui').has_result('b'*16)
                assert active.read_bytes()==b'ACTIVE_OUTPUT'
                assert active_meta.read_text()=='{"status":"running"}'
                assert config.read_bytes()==config_before
                print(f'  [PASS] 实际UI跨部署清理、活动文件/配置不变及2MiB历史归档精简（{compact_before}→{compact.stat().st_size}字节），重启去重保持')
                assert (share/'custom.txt').read_bytes()==b'cache'
                assert (share/'pool/a/one').read_bytes()==b'cache'
                assert manager.relay.config.enabled
                assert relay_app.load_config(str(config)).enabled
                import relay_cache
                batched=[make_history(share,peer='cache-ui',session=f'sess-batch-{n}') for n in range(4)]
                with patch.object(relay_cache,'SCAN_LIMIT',1):
                    partial=base._cdp_eval(target,url,RESUME_SCRIPT)
                assert '本批部分完成' in partial['text'] and '再次确认' in partial['text'] and partial['enabled'],partial
                assert any(p.exists() for p in batched),'首次预算应保留后续文件'
                resumed=base._cdp_eval(target,url,RESUME_SCRIPT)
                assert '继续上次扫描' in resumed['text'] and '已清理' in resumed['text'],resumed
                assert all(not p.exists() for p in batched)
                assert active.read_bytes()==b'ACTIVE_OUTPUT' and config.read_bytes()==config_before
                print('  [PASS] 实际Edge部分完成提示及再次人工确认续扫，后续文件不饥饿、活动流/配置保持')
                # 真Win32句柄禁止删除，验证占用文件不会被伪报清理成功。
                locked=make_history(share,peer='cache-ui',session='sess-locked');locked_before=locked.read_bytes()
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
                kernel.CreateFileW.restype=ctypes.c_void_p
                kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                handle=kernel.CreateFileW(str(locked),0x80000000,1,None,3,0,None)
                assert handle not in (None,ctypes.c_void_p(-1).value),ctypes.get_last_error()
                result=base._cdp_eval(target,url,FAIL_SCRIPT)
                print(f'  [{"PASS" if result["ok"] else "FAIL"}] 真文件占用错误及超时未知结果提示 ({result})')
                assert result['ok'],result
                assert locked.read_bytes()==locked_before
                return True
        finally:
            if handle: kernel.CloseHandle(handle)
            if manager: manager.stop()
            if thread: thread.join(timeout=10)
            if process:
                process.close()
            # Edge AppContainer 的缓存ACL可能不向当前用户授予DELETE；仅修复自建profile。
            profile=(directory/'edge').resolve()
            assert profile.parent == directory.resolve() and directory.name.startswith('DevMux-cache-ui-')
            if profile.exists():
                account=os.environ['USERDOMAIN']+'\\'+os.environ['USERNAME']
                subprocess.run(['icacls',str(profile),'/grant',account+':(OI)(CI)F','/T','/C'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
            for attempt in range(30):
                try:
                    shutil.rmtree(directory/'edge')
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if attempt == 29: raise
                    time.sleep(.1)
            if httpd: httpd.shutdown();httpd.server_close()
            srv.HTTP_PORT,srv.WS_PORT,srv._WS_PORT,srv.LOCAL_DEVICES_ENABLED=old_ports

if __name__=='__main__': sys.exit(0 if run() else 1)
