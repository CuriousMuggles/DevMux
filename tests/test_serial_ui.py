"""实际 Edge 页面和 WS 的串口窗口验证，使用注入假 COM。"""
import asyncio
import base64
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tests.test_ui_e2e import _FakeWorker, _free_port, _edge_executable, _cdp_eval
from tests.test_serial_terminal import service
from tests.edge_test_support import EdgeSession


def exclusive_port(start):
    # Windows 测试端口探测也使用独占标志，避免误借其他浏览器的CDP端口。
    for port in range(start,start+100):
        with socket.socket() as candidate:
            if os.name == 'nt': candidate.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:candidate.bind(('127.0.0.1',port));return port
            except OSError:pass
    raise RuntimeError('没有空闲的独占测试端口')


SCRIPT = r"""
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let step=0;
  const wait=async fn=>{step++;for(let i=0;i<300;i++){if(fn())return;await sleep(30);}throw Error('串口界面等待超时，步骤 '+step+'；'+document.getElementById('serial-select-status')?.textContent+'；'+document.querySelector('.serial-status')?.textContent);};
  const assert=(x,m)=>{if(!x)throw Error(m);};
  const el=id=>document.getElementById(id);
  await wait(()=>window.SerialIntegration && ws.readyState===1);
  assert(!el('tools-shell'),'旧工具菜单入口仍存在');
  document.querySelector('[data-tab="settings"]').click();
  el('window-tab').click();
  assert(el('window-menu').style.display==='block' && el('panel-terminal').classList.contains('active'),'窗口下拉与终端面板');
  el('window-powershell').click();
  await wait(()=>el('powershell-target').options.length>0);
  assert(el('window-powershell').textContent.includes('PowerShell'),'PowerShell菜单名称');
  el('powershell-select-close').click();
  el('window-tab').click();el('window-serial').click();
  await wait(()=>!el('serial-open').disabled);
  assert(el('serial-port').options.length===2 && el('serial-baudrate').value==='921600','端口枚举与默认波特率');
  el('serial-open').click();
  await wait(()=>SerialIntegration.windows.size===1);
  let window1=[...SerialIntegration.windows.values()][0];
  const panel=window1.panel;
  assert(panel.parentElement.id==='terminal-container' && !panel.classList.contains('tool-floating-window'),'串口默认主终端区');
  assert(document.querySelector('#device-tabs').textContent.includes('Serial · COM3'),'串口标签');
  const text=()=>{const b=window1.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join('\n');};
  const command=panel.querySelector('textarea.terminal-cmd-textarea');
  const send=panel.querySelector('.serial-send');
  const charDelay=panel.querySelector('.serial-char-delay');
  assert(charDelay && charDelay.value==='10','字符间隔默认10ms');
  state.terminals[[...SerialIntegration.windows.keys()][0]].nativeInput('TERM_PASTE');
  await wait(()=>text().includes('TERM_PASTE'));
  command.value='中文串口ABC';send.click();
  await wait(()=>text().includes('中文串口ABC'));
  charDelay.value='0';
  const format=panel.querySelector('.serial-format');format.value='hex';format.dispatchEvent(new Event('change'));
  command.value='00 FF 41 0D 0A';send.click();
  await wait(()=>text().includes('00 FF 41 0D 0A'));
  command.value='0G';send.click();await sleep(50);
  assert(panel.querySelector('.serial-status').textContent.includes('HEX'),'非法HEX没有明确拒绝');
  assert(panel.querySelector('.serial-control-toggle').textContent.includes('交还'),'持有输入权时按钮应显示交还');
  panel.querySelector('.serial-control-toggle').click();
  await wait(()=>!window1.snapshot().writable);
  assert(panel.querySelector('.serial-control-toggle').textContent.includes('接管'),'失去输入权时按钮应显示接管');
  command.value='42';send.click();
  await wait(()=>window1.snapshot().writable && text().includes('42'));
  panel.querySelector('.maximize').click();panel.querySelector('.maximize').click();
  panel.querySelector('.minimize').click();assert(panel.classList.contains('minimized'),'最小化');
  panel.querySelector('.minimize').click();assert(!panel.classList.contains('minimized'),'恢复');
  el('window-tab').click();el('window-serial').click();await wait(()=>!el('serial-open').disabled);
  el('serial-port').value='COM12';el('serial-open').click();await wait(()=>SerialIntegration.windows.size===2);
  assert(document.querySelector('#device-tabs').textContent.includes('Serial · COM12'),'第二串口标签');
  const windows=[...SerialIntegration.windows.values()];
  await windows[1].close();assert(SerialIntegration.windows.size===1,'关闭不应关闭另一COM');
  return {ok:true,checks:['菜单/PowerShell配置','COM枚举/921600','文本/HEX','非法HEX','GUI交还/自动接管','最小化/最大化','多COM/关闭隔离']};
})()
"""


def run(check_only=None):
    edge=_edge_executable()
    if not edge:
        print('  [SKIP] 未找到 Edge，串口界面未验证');return None
    import server
    from websockets.sync.client import connect
    backend=browser=http=endpoint=None
    previous=(server.HTTP_PORT,server.WS_PORT)
    with tempfile.TemporaryDirectory(prefix='serial-ui-',ignore_cleanup_errors=True) as temporary:
        runtime=Path(temporary)
        env={'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),
             'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}
        server.HTTP_PORT=exclusive_port(18380);server.WS_PORT=exclusive_port(18650)
        fake,created=service(runtime/'serial.json')
        try:
            with patch.dict(os.environ,env),patch.object(server,'ADBWorker',_FakeWorker), \
                    patch('serial_terminal.SerialService',side_effect=lambda path, *, config_storage=None:service(path, config_storage=config_storage)[0]):
                manager=server.ManagerServer()
                manager.serial=fake;manager.extension_tools.services['serial']=fake
                manager._commands_path=lambda:str(runtime/'commands.json')
                failures=[]
                def backend_run():
                    try:asyncio.run(manager.run())
                    except Exception as exc:failures.append(exc)
                backend=threading.Thread(target=backend_run,daemon=True);backend.start()
                end=time.monotonic()+10
                while not getattr(manager,'_ws_port',0) and time.monotonic()<end:time.sleep(.05)
                assert getattr(manager,'_ws_port',0),'WS 未就绪'
                http=server.start_http_server()
                browser=EdgeSession.start(edge,['--headless=new','--no-first-run',
                    '--no-default-browser-check','--remote-allow-origins=*','--window-size=1180,950',
                    f'http://127.0.0.1:{server._HTTP_PORT}/'],runtime/'edge')
                endpoint=browser.wait_ready(20)
                if check_only:
                    check_only(endpoint, manager)
                    return True
                try:
                    result=_cdp_eval(endpoint,'',SCRIPT)
                except Exception:
                    print('模拟串口写入:', [p[2].writes for p in created], flush=True)
                    try:
                        print('窗口快照:', _cdp_eval(endpoint,'',"JSON.stringify({url:location.href,ready:document.readyState,serial:typeof SerialIntegration,ws:typeof ws==='undefined'?null:ws.readyState,windows:typeof SerialIntegration==='undefined'?[]:[...SerialIntegration.windows.values()].map(w=>({state:w.snapshot(),text:Array.from({length:w.term.buffer.active.length},(_,i)=>w.term.buffer.active.getLine(i)?.translateToString(true)).join('|')}))})"), flush=True)
                    except Exception as diagnostic:
                        print('窗口快照失败（保留原异常）:',repr(diagnostic),flush=True)
                    raise
                assert result['ok'],result
                print('  [PASS] 实际 Edge/WS:',', '.join(result['checks']))
                text_chunks=[b'T',b'E',b'R',b'M',b'_',b'P',b'A',b'S',b'T',b'E']
                text_chunks += [value.encode() for value in ['中','文','串','口','A','B','C','\r']]
                assert created[0][2].writes[:18]==text_chunks,created[0][2].writes
                assert created[0][2].writes[18:]==[bytes.fromhex('00 FF 41 0D 0A'),b'B'],created[0][2].writes
                write_times=created[0][2].write_times
                assert all(b-a>=.007 for a,b in zip(write_times[:10],write_times[1:10])),write_times
                assert all(b-a>=.007 for a,b in zip(write_times[10:18],write_times[11:18])),write_times
                assert len(created)==2 and created[1][2].closed and not created[0][2].closed
                from tests.test_serial_receipt_ui import check as check_receipt
                check_receipt(endpoint)
                assert created[0][2].writes[-2:]==[b'ACK_TEST\r',b'AFTER_ACK\r'],created[0][2].writes
                from tests.test_serial_latency_ui import check as check_latency
                check_latency(endpoint)
                with connect(endpoint,proxy=None,max_size=12*1024*1024) as cdp:
                    def command(identifier,method,params):
                        cdp.send(json.dumps({'id':identifier,'method':method,'params':params}))
                        while True:
                            result=json.loads(cdp.recv(timeout=10))
                            if result.get('id')==identifier:return result['result']
                    command(1,'Emulation.setDeviceMetricsOverride',{'width':480,'height':780,'deviceScaleFactor':1,'mobile':False})
                    time.sleep(.15)
                    bounded=command(2,'Runtime.evaluate',{'expression':"document.documentElement.scrollWidth <= innerWidth && [...document.querySelectorAll('.serial-options')].every(e=>e.scrollWidth <= e.clientWidth+1)",'returnByValue':True})
                    assert bounded['result']['value'],'窄屏串口工具栏溢出'
                    shot=command(3,'Page.captureScreenshot',{'format':'png'})
                    output=ROOT/'.agent-runtime/serial';output.mkdir(parents=True,exist_ok=True)
                    (output/'serial-ui-480.png').write_bytes(base64.b64decode(shot['data']))
                _cdp_eval(endpoint,'',"(async()=>{await [...SerialIntegration.windows.values()][0].close();return {ok:SerialIntegration.windows.size===0};})()")
                assert created[0][2].closed
                restarted=_cdp_eval(endpoint,'',r"""(async()=>{
                  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
                  const wait=async fn=>{for(let i=0;i<500;i++){if(fn())return;await sleep(30);}throw Error('重启等待超时');};
                  document.getElementById('window-serial').click();
                  await wait(()=>!document.getElementById('serial-open').disabled);
                  document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();
                  await wait(()=>SerialIntegration.windows.size===1);
                  const result=await callWS('restart-server');
                  await wait(()=>[...SerialIntegration.windows.values()][0].term.options.disableStdin);
                  await wait(()=>ws.readyState===1);
                  await [...SerialIntegration.windows.values()][0].close();
                  return {ok:result.success};
                })()""")
                assert restarted['ok'] and len(created)==3 and created[2][2].closed
                assert manager.serial is not fake and not manager.serial._closed,'重启未重建串口服务'
                assert not failures,failures
                print('  [PASS] 480px无溢出；发送字节/最后接入关闭/重启回收及重建服务由后端核对')
                from tests.test_terminal_windows import check
                check(endpoint)
                adb_input=b''.join(data for key,data in manager.worker.writes if key=='UI-TEST-1:tid=1')
                assert adb_input==b'echo NATIVE_ADB\r',f'脱离ADB逐键输入缺失或重复：{adb_input!r}'
                print('  [PASS] 后端逐字节核对脱离ADB完整输入，未缺失或重复')
                from tests.test_serial_refresh_ui import check as check_refresh
                check_refresh(endpoint, manager)
                from tests.test_serial_close_ui import check as check_close
                check_close(endpoint, manager)
                from tests.test_serial_enhancements_ui import check as check_enhancements
                check_enhancements(endpoint, manager)
                return True
        finally:
            if browser:
                browser.close()
            if backend:
                manager.stop();backend.join(timeout=15)
            if http:http.shutdown();http.server_close()
            fake.close();server.HTTP_PORT,server.WS_PORT=previous


if __name__=='__main__':sys.exit(0 if run() is not False else 1)
