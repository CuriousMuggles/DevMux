"""新串口操作的真实Edge/WS集成检查，端口与文件均为测试隔离资源。"""
import base64
import json
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_e2e import _cdp_eval
from tests.test_serial_xmodem import Wire
import server
import xmodem_transfer as xm

ROOT=Path(__file__).resolve().parent.parent

SETUP = r'''(async()=>{
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const wait=async fn=>{for(let i=0;i<600;i++){if(fn())return;await sleep(15);}throw Error('优化界面等待超时');};
 const assert=(v,m)=>{if(!v)throw Error(m);};
 for(const window of [...SerialIntegration.windows.values()]) await window.close();
 document.getElementById('window-serial').click();
 await wait(()=>!document.getElementById('serial-open').disabled);
 const baud=document.getElementById('serial-baudrate');
 assert(baud.list.options.length>=20,'单一波特率输入未提供常用值列表');
 baud.value='115200';assert(baud.value==='115200','常用波特率选取失败');
 baud.value='123456';assert(baud.value==='123456','自定义波特率输入失败');
 baud.value='921600';
 assert(!document.getElementById('serial-baudrate-presets')&&!document.getElementById('serial-dtr')&&!document.getElementById('serial-rts'),'波特率或流控仍有重复控件');
 document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();
 await wait(()=>SerialIntegration.windows.size===1);
 window.enhanced=[...SerialIntegration.windows.values()][0];window.ep=enhanced.panel;
 ep.querySelector('.serial-char-delay').value='0';
 ep.querySelector('.serial-shortcuts').click();await wait(()=>ep.querySelector('.serial-command-popup').style.display==='block');
 ep.querySelector('.cmd-popup-add').click();
 const dialog=document.querySelector('.cmd-edit-dialog');
 dialog.querySelector('input').value='串口<&"';dialog.querySelector('textarea').value='AT+TEST';
 [...dialog.querySelectorAll('button')].find(b=>b.textContent==='保存').click();
 await wait(()=>!document.querySelector('.cmd-edit-overlay'));
 assert(ep.querySelector('.cmd-btn-name').textContent==='串口<&"','名称未安全显示');
 assert(!ep.querySelector('.cmd-btn-name').querySelector('*'),'指令名称被解释为HTML');
 ep.querySelector('.cmd-popup-btn').click();await sleep(120);
 ep.querySelector('.serial-format').value='hex';ep.querySelector('.serial-format').dispatchEvent(new Event('change'));
 ep.querySelector('.cmd-popup-btn').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));
 document.querySelector('.cmd-edit-dialog textarea').value='00 FF 41';
 [...document.querySelectorAll('.cmd-edit-dialog button')].find(b=>b.textContent==='保存').click();
 await wait(()=>!document.querySelector('.cmd-edit-overlay'));ep.querySelector('.cmd-popup-btn').click();await sleep(120);
 ep.querySelector('.serial-format').value='text';ep.querySelector('.serial-format').dispatchEvent(new Event('change'));
 ep.querySelector('.group-add').click();document.querySelector('.cmd-edit-dialog input').value='调试';
 [...document.querySelectorAll('.cmd-edit-dialog button')].find(b=>b.textContent==='保存').click();await wait(()=>!document.querySelector('.cmd-edit-overlay'));
 assert(ep.querySelector('.cmd-group-select').selectedOptions[0].textContent==='调试','指令集新增失败');
 ep.querySelector('.group-rename').click();document.querySelector('.cmd-edit-dialog input').value='测试';
 [...document.querySelectorAll('.cmd-edit-dialog button')].find(b=>b.textContent==='保存').click();await wait(()=>!document.querySelector('.cmd-edit-overlay'));
 ep.querySelector('.group-delete').click();document.querySelector('.cmd-edit-dialog input').value='测试';
 [...document.querySelectorAll('.cmd-edit-dialog button')].find(b=>b.textContent==='保存').click();await wait(()=>!document.querySelector('.cmd-edit-overlay'));
 assert(ep.querySelector('.cmd-group-select').options.length===1,'指令集删除失败');
 ep.querySelector('.serial-shortcuts').click();ep.querySelector('.serial-xmodem-toggle').click();
 return {ok:true};
})()'''
SEND = r'''(async()=>{
 const input=ep.querySelector('.xmodem-file'),transfer=new DataTransfer();
 transfer.items.add(new File([Uint8Array.from({length:35001},(_,i)=>i%256)],'中文-串口.bin'));
 input.files=transfer.files;input.dispatchEvent(new Event('change'));
 for(let i=0;i<1000;i++){
   const state=enhanced.snapshot().xmodem;
   if(state?.state==='succeeded')return {ok:true,bytes:state.transferredBytes};
   if(state?.state==='failed')throw Error(state.error);
   await new Promise(r=>setTimeout(r,10));
 }
 throw Error('发送界面未成功');
})()'''
RECEIVE = r'''(async()=>{
 ep.querySelector('.xmodem-receive').click();
 for(let i=0;i<1000;i++){
   const state=enhanced.snapshot().xmodem;
   if(state?.direction==='receive'&&state.state==='succeeded'&&!ep.querySelector('.xmodem-download').disabled)return {ok:true,bytes:state.transferredBytes};
   if(state?.direction==='receive'&&state.state==='failed')throw Error(state.error);
   await new Promise(r=>setTimeout(r,10));
 }
 throw Error('接收界面未成功');
})()'''
CANCEL = r'''(async()=>{
 ep.querySelector('.xmodem-receive').click();
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 for(let i=0;i<200;i++) {if(enhanced.snapshot().xmodem?.state==='waiting')break;await sleep(10);}
 if(!ep.querySelector('.serial-send').disabled)throw Error('传输未禁用普通输入');
 ep.querySelector('.xmodem-cancel').click();
 for(let i=0;i<300;i++){
   if(enhanced.snapshot().xmodem?.state==='cancelled'&&!ep.querySelector('.serial-send').disabled)return {ok:true};
   await sleep(10);
 }
 throw Error('取消后未恢复输入');
})()'''


def check(endpoint, manager):
    wire=Wire();manager.serial.connector=lambda *_:wire
    peers=[]
    with tempfile.TemporaryDirectory(prefix='serial-enhanced-ui-') as temporary, \
            patch.object(server.ManagerServer,'_commands_path',return_value=str(Path(temporary)/'commands.json')), \
            patch.multiple(xm,TIMEOUT=.2,HANDSHAKE=2):
        try:
            assert _cdp_eval(endpoint,'',SETUP)['ok']
            assert wire.writes[:2]==[b'AT+TEST\r',bytes.fromhex('00 FF 41')],wire.writes
            stored=json.loads((Path(temporary)/'commands.json').read_text(encoding='utf-8'))
            assert stored['serial'][0]['commands'][0]['cmd']=='00 FF 41' and stored['adb'] is None
            def peer(direction, raw=b''):
                task=xm.Transfer(direction,len(raw),1024,'crc',uuid.uuid4().hex)
                if raw:task.upload(0,raw)
                wire.peer=task;peers.append(task)
                previous=manager.serial.ports['COM3'].get('transfer')
                def execute():
                    from tests.test_serial_terminal import wait
                    wait(lambda:manager.serial.ports['COM3'].get('transfer') is not previous
                        and manager.serial.ports['COM3']['transfer'].state in ('waiting','running'),8)
                    task.execute(lambda raw,**_:wire.rx.put(raw))
                task.thread=threading.Thread(target=execute,daemon=True)
                task.thread.start();return task
            receive=peer('receive')
            result=_cdp_eval(endpoint,'',SEND);assert result['ok'] and result['bytes']==35001,result
            receive.thread.join(3);assert receive.state=='succeeded',receive.error
            receive.file.seek(0);expected=bytes(i%256 for i in range(35001))
            assert receive.file.read()==expected.ljust(35072,b'\x1a')
            raw=bytes(range(256))*5+b'\x00\x1a'
            sender=peer('send',raw)
            result=_cdp_eval(endpoint,'',RECEIVE);assert result['ok'] and result['bytes']==2048,result
            destination=Path(temporary)/'接收文件.bin'
            with patch.object(server._FileDialog,'pick',return_value={'success':True,'path':str(destination)}):
                result=_cdp_eval(endpoint,'',r'''(async()=>{
                  ep.querySelector('.xmodem-download').click();
                  for(let i=0;i<500;i++){
                    if(ep.querySelector('.serial-status').textContent.includes('已保存'))return {ok:true};
                    await new Promise(r=>setTimeout(r,10));
                  }
                  throw Error('保存按钮未成功：'+ep.querySelector('.serial-status').textContent);
                })()''')
            assert result['ok'] and destination.read_bytes()==raw.ljust(2048,b'\x1a')
            # 重复保存不能覆盖已有文件。
            window_state=_cdp_eval(endpoint,'','enhanced.snapshot()')
            target={'local':True};tid=window_state['xmodem']['transferId'];sid=window_state['sessionId']
            with patch.object(server._FileDialog,'pick',return_value={'success':True,'path':str(destination)}):
                try:manager._save_serial_xmodem(target,sid,tid)
                except FileExistsError:pass
                else:raise AssertionError('不能覆盖已有文件')
            assert destination.read_bytes()==raw.ljust(2048,b'\x1a')
            with patch.object(server._FileDialog,'pick',return_value={'success':False,'path':''}):
                assert not manager._save_serial_xmodem(target,sid,tid)['success']
            wire.peer=None
            assert _cdp_eval(endpoint,'',CANCEL)['ok']
            assert _cdp_eval(endpoint,'',r'''(async()=>{
              const original=callWS;let lost=false,firstId;
              window.callWS=async(type,payload,...rest)=>{
                const result=await original(type,payload,...rest);
                if(type==='extension-tool'&&payload.action==='xmodem-start'&&!lost){lost=true;firstId=result.data.xmodem.transferId;return {success:false,operationUncertain:true,output:'测试丢失启动回执'};}
                return result;
              };
              try {
                ep.querySelector('.xmodem-receive').click();
                for(let i=0;i<300;i++){if(!ep.querySelector('.xmodem-resume').hidden)break;await new Promise(r=>setTimeout(r,10));}
                if(ep.querySelector('.xmodem-resume').hidden)throw Error('未知结果未提供查询');
                ep.querySelector('.xmodem-resume').click();
                for(let i=0;i<300;i++){if(enhanced.snapshot().xmodem?.state==='waiting')break;await new Promise(r=>setTimeout(r,10));}
                if(enhanced.snapshot().xmodem.transferId!==firstId)throw Error('查询启动了另一传输');
                ep.querySelector('.xmodem-cancel').click();
                for(let i=0;i<300;i++){if(enhanced.snapshot().xmodem.state==='cancelled')return {ok:true};await new Promise(r=>setTimeout(r,10));}
                throw Error('恢复查询后无法取消');
              } finally {window.callWS=original;}
            })()''')['ok']
            assert _cdp_eval(endpoint,'',r'''(async()=>{
              const original=callWS;let uploaded=false;
              window.callWS=async(type,payload,...rest)=>{
                const result=await original(type,payload,...rest);
                if(type==='extension-tool'&&payload.action==='xmodem-upload'&&!uploaded){uploaded=true;await new Promise(r=>setTimeout(r,250));}
                return result;
              };
              try {
                const input=ep.querySelector('.xmodem-file'),files=new DataTransfer();
                files.items.add(new File([new Uint8Array(70000)],'取消准备.bin'));input.files=files.files;input.dispatchEvent(new Event('change'));
                for(let i=0;i<300;i++){if(uploaded)break;await new Promise(r=>setTimeout(r,10));}
                if(!uploaded||ep.querySelector('.xmodem-cancel').disabled)throw Error('准备期间不可取消');
                ep.querySelector('.xmodem-cancel').click();
                for(let i=0;i<300;i++){
                  if(enhanced.snapshot().xmodem?.state==='cancelled'&&!ep.querySelector('.xmodem-send').disabled)return {ok:true};
                  await new Promise(r=>setTimeout(r,10));
                }
                throw Error('取消准备未恢复输入');
              } finally {window.callWS=original;}
            })()''')['ok']
            assert manager.serial.sessions[sid]['slot']['transfer'].thread is None,'取消准备不能启动协议线程'
            # 保存宽/窄视图，断言展开传输设置也保留终端区域。
            from websockets.sync.client import connect
            import time
            evidence=ROOT/'.agent-runtime/serial-enhancements';evidence.mkdir(parents=True,exist_ok=True)
            with connect(endpoint,proxy=None,max_size=12*1024*1024) as cdp:
                counter=0
                def command(method,params):
                    nonlocal counter
                    counter+=1;cdp.send(json.dumps({'id':counter,'method':method,'params':params}))
                    while True:
                        result=json.loads(cdp.recv(timeout=10))
                        if result.get('id')==counter:return result['result']
                for width in (1000,480):
                    command('Emulation.setDeviceMetricsOverride',{'width':width,'height':800,'deviceScaleFactor':1,'mobile':False})
                    command('Runtime.evaluate',{'expression':"selectActiveDevice(ep.dataset.key);setLayout('single');true"})
                    time.sleep(.2)
                    result=command('Runtime.evaluate',{'expression':"document.documentElement.scrollWidth<=innerWidth && ep.querySelector('.serial-terminal-host').clientHeight>=48",'returnByValue':True})
                    assert result['result']['value'],'展开工具栏挤占终端或窄屏溢出'
                    shot=command('Page.captureScreenshot',{'format':'png'})
                    (evidence/f'ui-{width}.png').write_bytes(base64.b64decode(shot['data']))
            # 主界面断连后，传输所属接入释放，后台线程与临时文件结束。
            _cdp_eval(endpoint,'',r'''(async()=>{ep.querySelector('.xmodem-receive').click();
              for(let i=0;i<200;i++){if(enhanced.snapshot().xmodem?.state==='waiting')return true;await new Promise(r=>setTimeout(r,10));}
              throw Error('断线测试传输未开始');})()''')
            slot=manager.serial.sessions[sid]['slot'];transfer=slot['transfer']
            _cdp_eval(endpoint,'','ws.close(); true')
            from tests.test_serial_terminal import wait
            wait(lambda:transfer.file.closed and wire.closed)
            _cdp_eval(endpoint,'',r'''(async()=>{for(let i=0;i<500;i++){if(ws.readyState===1)return true;await new Promise(r=>setTimeout(r,10));}throw Error('连接未恢复');})()''')
            print('  [PASS] 串口优化Edge：波特率/自定义、指令持久化/编辑/文本与HEX、XMODEM35KB发送/1K接收/原生保存/取消/断连回收')
        finally:
            for task in peers:
                task.cancel.set();task.thread.join(3);task.dispose()
