"""实际Edge鼠标拖拽：同会话停靠/悬浮、关闭广播隔离及手动恢复。"""
import base64
import json
import time
from pathlib import Path
from websockets.sync.client import connect


def check(endpoint):
    with connect(endpoint,proxy=None,max_size=20*1024*1024) as socket:
        counter=0
        def command(method,params,session=None):
            nonlocal counter
            counter+=1;request=dict(id=counter,method=method,params=params)
            if session:request['sessionId']=session
            socket.send(json.dumps(request))
            while True:
                response=json.loads(socket.recv(timeout=40))
                if response.get('id')==counter:
                    assert not response.get('error'),response
                    return response['result']
        def evaluate(script,session=None):
            response=command('Runtime.evaluate',dict(expression=script,awaitPromise=True,returnByValue=True),session)
            assert 'exceptionDetails' not in response,response
            return response['result'].get('value')
        def drag(x,y,dx,dy):
            command('Input.dispatchMouseEvent',dict(type='mousePressed',x=x,y=y,button='left',clickCount=1))
            for step in range(1,9):
                command('Input.dispatchMouseEvent',dict(type='mouseMoved',x=x+dx*step/8,y=y+dy*step/8,button='left',buttons=1))
            command('Input.dispatchMouseEvent',dict(type='mouseReleased',x=x+dx,y=y+dy,button='left',clickCount=1))
        command('Emulation.setDeviceMetricsOverride',dict(width=1280,height=980,deviceScaleFactor=1,mobile=False))
        keys=evaluate(r"""(async()=>{
          window.twWait=async fn=>{for(let n=0;n<400;n++){if(fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('统一终端等待超时');};
          document.getElementById('window-serial').click();await twWait(()=>!document.getElementById('serial-open').disabled);
          document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();await twWait(()=>SerialIntegration.windows.size===1);
          document.getElementById('window-powershell').click();await twWait(()=>!document.getElementById('powershell-open').disabled);
          window.twErrors=[];window.addEventListener('unhandledrejection',e=>twErrors.push(String(e.reason?.stack||e.reason)));
          document.getElementById('powershell-open').click();await twWait(()=>Object.values(state.terminals).some(t=>t.computer&&!t.serialPort)||twErrors.length);
          if(twErrors.length)throw Error(twErrors.join('|'));
          setLayout('grid');
          handleWSMessage({type:'devices-updated',devices:[{serial:'UI-TEST-1',transportId:'1',state:'device'},{serial:'UI-TEST-2',transportId:'2',state:'device'}],knownKeys:['UI-TEST-1:tid=1','UI-TEST-2:tid=2']});
          const keys=[state.knownKeys[0],Object.keys(state.terminals).find(k=>state.terminals[k].computer&&!state.terminals[k].serialPort),[...SerialIntegration.windows.keys()][0]];
          if(keys.some(k=>!state.terminals[k]))throw Error('窗口尚未注册 '+JSON.stringify({keys,known:state.knownKeys,all:Object.keys(state.terminals),max:state.maxDevices,status:document.getElementById('powershell-permission-status').textContent}));
          window.twOriginal=keys.map(k=>({panel:state.terminals[k].panel,term:state.terminals[k].term,key:k}));
          return keys;
        })()""")
        root_bounds=command('Browser.getWindowForTarget',{})['bounds']
        for index,key in enumerate(keys):
            header=evaluate("(()=>{const r=state.terminals["+json.dumps(key)+"].header.getBoundingClientRect();return {x:r.x+110,y:r.y+14};})()")
            drag(header['x'],header['y'],160,90)
            evaluate("(async()=>{await twWait(()=>state.terminals["+json.dumps(key)+"].windowManager.windowId);return true;})()")
            identifier=evaluate("state.terminals["+json.dumps(key)+"].windowManager.windowId")
            deadline=time.monotonic()+12;target=None
            while not target and time.monotonic()<deadline:
                target=next((t for t in command('Target.getTargets',{})['targetInfos'] if '/desktop-terminal.html#'+identifier+':' in t['url']),None)
                if not target:time.sleep(.05)
            assert target,'未打开独立浏览器窗口'
            child=command('Target.attachToTarget',dict(targetId=target['targetId'],flatten=True))['sessionId']
            evaluate("(async()=>{for(let n=0;n<300;n++){if(document.querySelector('.xterm-helper-textarea')&&document.getElementById('desktop-message').hidden)return true;await new Promise(r=>setTimeout(r,20));}throw Error('子窗接入超时');})()",child)
            window=command('Browser.getWindowForTarget',dict(targetId=target['targetId']))
            root_window=command('Browser.getWindowForTarget',{})
            assert window['windowId']!=root_window['windowId'],'终端仍在主浏览器窗口内'
            left=root_bounds.get('left',0)+root_bounds['width']+60
            command('Browser.setWindowBounds',dict(windowId=window['windowId'],bounds=dict(left=left,top=50,width=720,height=620)))
            bounds=command('Browser.getWindowBounds',dict(windowId=window['windowId']))['bounds']
            assert bounds['left']>=left-2 and bounds['width']<=740,'独立窗口位置/尺寸仍受主应用限制'
            value=['echo NATIVE_ADB\r',"[Console]::WriteLine('NATIVE_PS_'+(8+1))\r",'NATIVE_SERIAL\r'][index]
            evaluate("(()=>{window.focusProbe=document.querySelector('.xterm-helper-textarea');window.focusProbeRemoved=0;new MutationObserver(records=>{for(const r of records)for(const n of r.removedNodes)if(n===focusProbe||n.contains?.(focusProbe))focusProbeRemoved++;}).observe(document.getElementById('desktop-host'),{subtree:true,childList:true});focusProbe.focus();})()",child)
            # 按真实用户逐键输入，不能一次派发整行后掩盖状态同步导致的焦点丢失。
            for character in value:
                if character=='\r':
                    command('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'),child)
                    command('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13),child)
                else:
                    command('Input.dispatchKeyEvent',dict(type='keyDown',key=character,text=character),child)
                    command('Input.dispatchKeyEvent',dict(type='keyUp',key=character),child)
                time.sleep(.08)
                assert evaluate("document.activeElement===document.querySelector('.xterm-helper-textarea')",child),'独立终端逐键输入丢失焦点，类型 '+str(index)
            evaluate("state.terminals["+json.dumps(key)+"].header.dataset.focusProbe='状态变化'",None)
            time.sleep(.15)
            assert evaluate("focusProbeRemoved===0&&focusProbe===document.activeElement",child),'状态刷新重建或断开了活xterm'
            expected=['ui-self-test','NATIVE_PS_9','NATIVE_SERIAL'][index]
            try:
                evaluate("(async()=>{for(let n=0;n<300;n++){if(document.querySelector('.xterm-screen').textContent.includes("+json.dumps(expected)+"))return true;await new Promise(r=>setTimeout(r,25));}throw Error('独立窗口没有收到回显');})()",child)
            except Exception:
                print('独立窗口失败类型',index,'父状态',evaluate("(()=>{const t=state.terminals["+json.dumps(key)+"];return {input:typeof t.nativeInput,readonly:t.term.options.disableStdin,text:Array.from({length:t.term.buffer.active.length},(_,i)=>t.term.buffer.active.getLine(i)?.translateToString(true)).join('|').slice(-1800)};})()"),'子状态',evaluate("({text:document.querySelector('.xterm-screen').textContent.slice(-1800),readonly:document.querySelector('.xterm-helper-textarea').readOnly})",child),flush=True)
                raise
            assert evaluate("twOriginal.every(o=>o.panel===state.terminals[o.key].panel&&o.term===state.terminals[o.key].term)"),'独立窗口重新创建了主会话'
            evaluate("document.querySelector('.terminal-cmd-textarea').focus()",child)
            evaluate("state.terminals["+json.dumps(key)+"].windowManager.snapshot()")
            time.sleep(.15)
            assert evaluate("document.activeElement===document.querySelector('.terminal-cmd-textarea')",child),'刷新抢走命令框焦点'
            if index==2:
                shot=command('Page.captureScreenshot',dict(format='png'),child)
                output=Path(__file__).resolve().parent.parent/'.agent-runtime/native-terminal-windows'
                output.mkdir(parents=True,exist_ok=True);(output/'desktop-serial.png').write_bytes(base64.b64decode(shot['data']))
            evaluate("document.querySelector('.terminal-dock').click()",child)
            evaluate("(async()=>{await twWait(()=>!state.terminals["+json.dumps(key)+"].windowManager.windowId);return true;})()")
            assert evaluate("!state.terminals["+json.dumps(key)+"].panel.classList.contains('native-detached')"),'返回主终端区失败'
        result=evaluate(r"""(async()=>{
          if(!twOriginal.every(o=>o.panel===state.terminals[o.key].panel&&o.term===state.terminals[o.key].term))throw Error('停靠替换会话');
          for(const o of twOriginal){o.panel.querySelector('.minimize').click();document.querySelector('#device-tabs .device-tab[data-key="'+o.key+'"]').click();if(o.panel.classList.contains('minimized'))throw Error('标签未恢复最小化终端');}
          const serial=twOriginal[2],ps=twOriginal[1],adb=twOriginal[0];
          const serialText=()=>{const b=serial.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join('|');};
          serial.panel.querySelector('textarea.terminal-cmd-textarea').value='DOCK_ECHO';serial.panel.querySelector('.serial-send').click();
          await twWait(()=>serialText().includes('DOCK_ECHO'));
          ps.term._core.coreService.triggerDataEvent("[Console]::WriteLine('DOCK_'+(4+5))\r");
          await twWait(()=>ps.panel.querySelector('.xterm-screen').textContent.includes('DOCK_9'));
          const probe={key:'UI-TEST-3',serial:'UI-TEST-3',state:'device'};state.devices.push(probe);state.knownKeys.push(probe.key);updateTerminalPanels();
          setLayout('grid');
          const bounds=document.getElementById('terminal-container').getBoundingClientRect();
          if([...document.querySelectorAll('#terminal-container>.terminal-panel')].some(p=>p.getBoundingClientRect().bottom>bounds.bottom+2))throw Error('五窗口网格溢出');
          for(const layout of ['horizontal','vertical','single','grid']){setLayout(layout);selectActiveDevice(serial.key);applyLayout(layout);if(serial.panel.style.display==='none')throw Error('串口未参加布局');}
          await dismissTerminal(probe.key);
          adb.panel.querySelector('.close').click();await twWait(()=>!state.terminals[adb.key]);
          document.querySelector('#device-tabs .device-tab[data-key="'+ps.key+'"] .close-btn').click();await twWait(()=>!state.terminals[ps.key]);
          serial.panel.querySelector('.close').click();await twWait(()=>!state.terminals[serial.key]);
          await new Promise(r=>setTimeout(r,200));
          for(let n=0;n<10;n++)handleWSMessage({type:'devices-updated',devices:state.devices,knownKeys:state.devices.map(_deviceKey)});
          if([adb,ps,serial].some(o=>state.terminals[o.key]))throw Error('广播恢复了手动关闭窗口');
          document.getElementById('device-refresh-btn').click();
          await twWait(()=>state.terminals[adb.key]&&document.querySelector('.powershell-terminal-panel'));
          // 明确关闭串口后刷新不重开，ADB/PowerShell仍保持刷新恢复。
          await new Promise(r=>setTimeout(r,200));
          if(SerialIntegration.windows.size!==0)throw Error('刷新重新打开已关闭串口');
          if(TerminalWindows.closed.size)throw Error('刷新没有清除恢复配方');
          const reopened=Object.values(state.terminals).filter(t=>t.computer);
          if(reopened.length!==1||reopened.some(t=>t.panel.parentElement.id!=='terminal-container'))throw Error('手动刷新重复或未停靠');
          await Promise.all(reopened.map(t=>t.close()));return {ok:true};
        })()""")
        assert result['ok'],result
        shot=command('Page.captureScreenshot',dict(format='png'))
        output=Path(__file__).resolve().parent.parent/'.agent-runtime/terminal-followup'
        output.mkdir(parents=True,exist_ok=True);(output/'terminal-windows.png').write_bytes(base64.b64decode(shot['data']))
        print('  [PASS] 实际鼠标拖出/缩放/返回三类同会话；停靠后串口与PowerShell回显；五窗口布局；关闭广播抑制/手动刷新恢复')

