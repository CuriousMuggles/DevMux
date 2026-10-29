"""冻结原生PowerShell逐键回归；自有WebView2调试端口、独立配置，不接设备。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tests.test_serial_ui import exclusive_port
from websockets.sync.client import connect


class Page:
    def __init__(self,url):self.ws=connect(url,proxy=None,max_size=8*1024*1024);self.counter=0
    def command(self,method,params=None):
        self.counter+=1;identifier=self.counter
        self.ws.send(json.dumps(dict(id=identifier,method=method,params=params or {})))
        while True:
            result=json.loads(self.ws.recv(timeout=25))
            if result.get('id')==identifier:
                assert not result.get('error'),result
                return result.get('result',{})
    def evaluate(self,expression):
        result=self.command('Runtime.evaluate',dict(expression=expression,awaitPromise=True,returnByValue=True))
        assert 'exceptionDetails' not in result,result
        return result.get('result',{}).get('value')
    def type(self,text):
        for character in text:
            key,code,vk=('Enter','Enter',13) if character=='\r' else ('Backspace','Backspace',8) if character=='\b' else (character,'',0)
            args=dict(type='keyDown',key=key)
            if code:args.update(code=code,windowsVirtualKeyCode=vk)
            if character!='\b':args['text']=character
            self.command('Input.dispatchKeyEvent',args)
            self.command('Input.dispatchKeyEvent',dict(type='keyUp',key=key))
            time.sleep(.08)
            assert self.evaluate("document.activeElement===document.querySelector('.powershell-terminal-host .xterm-helper-textarea,.xterm-helper-textarea')"),'实际WebView2逐键输入失焦'
    def close(self):self.ws.close()


def run(executable):
    process=None;pages=[];record={}
    with tempfile.TemporaryDirectory(prefix='native-keyboard-',ignore_cleanup_errors=True) as temporary:
        runtime=Path(temporary);http=exclusive_port(23480);ws=exclusive_port(23780);debug=exclusive_port(23920)
        env={**os.environ,'ADB_MANAGER_HEADLESS':'0','ADB_MANAGER_NO_LOCAL_DEVICES':'1','RELAY_CONFIG':str(runtime/'relay.json'),
            'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'HTTP_PORT':str(http),'WS_PORT':str(ws),
            'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS':f'--remote-debugging-port={debug} --remote-allow-origins=*'}
        def wait(fn):
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                value=fn()
                if value:return value
                time.sleep(.05)
            raise AssertionError('冻结原生键盘等待超时')
        def targets():
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{debug}/json/list',timeout=1) as response:return json.load(response)
            except (OSError,ValueError):return []
        try:
            process=subprocess.Popen([str(Path(executable).resolve())],cwd=ROOT,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            target=wait(lambda:next((t for t in targets() if t.get('url','').startswith(f'http://127.0.0.1:{http}/') and '/desktop-terminal' not in t['url']),None))
            root=Page(target['webSocketDebuggerUrl']);pages.append(root)
            wait(lambda:root.evaluate("typeof ws!=='undefined'&&ws.readyState===1&&!!document.getElementById('window-powershell')"))
            root.evaluate("document.getElementById('window-powershell').click()")
            wait(lambda:root.evaluate("!document.getElementById('powershell-open').disabled"))
            root.evaluate("document.getElementById('powershell-open').click()")
            wait(lambda:root.evaluate("!!Object.values(state.terminals).find(t=>t.computer&&!t.serialPort)?.panel.querySelector('.xterm-screen')?.textContent.includes('PS ')"))
            key=root.evaluate("Object.keys(state.terminals).find(k=>state.terminals[k].computer&&!state.terminals[k].serialPort)")
            root.evaluate('state.terminals['+json.dumps(key)+'].term.focus()')
            root.type("[Console]::WriteLine('BEFORE_'+(2+3))\r")
            wait(lambda:root.evaluate("document.querySelector('.powershell-terminal-host .xterm-screen').textContent.includes('BEFORE_5')"))
            root.evaluate('state.terminals['+json.dumps(key)+'].windowManager.detach(200,150)')
            identifier=wait(lambda:root.evaluate('state.terminals['+json.dumps(key)+'].windowManager.windowId'))
            target=wait(lambda:next((t for t in targets() if '/desktop-terminal.html#'+identifier+':' in t.get('url','')),None))
            child=Page(target['webSocketDebuggerUrl']);pages.append(child)
            wait(lambda:child.evaluate("document.getElementById('desktop-message')?.hidden===true&&!!document.querySelector('.xterm-helper-textarea')"))
            child.evaluate("window.originalInput=document.querySelector('.xterm-helper-textarea');window.removedInput=0;new MutationObserver(rs=>{for(const r of rs)for(const n of r.removedNodes)if(n===originalInput||n.contains?.(originalInput))removedInput++;}).observe(document.getElementById('desktop-host'),{subtree:true,childList:true});originalInput.focus()")
            child.type('$nativeFocus=4x\b0\r')
            root.evaluate('state.terminals['+json.dumps(key)+'].header.dataset.keyboardCheck="更新状态"')
            time.sleep(.15)
            child.type("[Console]::WriteLine('NATIVE_TYPED='+($nativeFocus+2))\r")
            wait(lambda:child.evaluate("document.querySelector('.xterm-screen').textContent.includes('NATIVE_TYPED=42')"))
            child.type('Start-Sleep 60\r');time.sleep(.2)
            child.command('Input.dispatchKeyEvent',dict(type='keyDown',key='c',code='KeyC',windowsVirtualKeyCode=67,modifiers=2))
            child.command('Input.dispatchKeyEvent',dict(type='keyUp',key='c',code='KeyC',windowsVirtualKeyCode=67));time.sleep(.2)
            child.type("[Console]::WriteLine('AFTER_NATIVE_'+(3+4))\r")
            wait(lambda:child.evaluate("document.querySelector('.xterm-screen').textContent.includes('AFTER_NATIVE_7')"))
            assert child.evaluate('removedInput===0&&originalInput===document.activeElement'),'刷新断开活xterm'
            child.evaluate("document.querySelector('.terminal-dock').click()")
            wait(lambda:root.evaluate('!state.terminals['+json.dumps(key)+'].windowManager.windowId'))
            root.evaluate('state.terminals['+json.dumps(key)+'].term.focus()')
            root.type("[Console]::WriteLine('DOCK_AGAIN='+($nativeFocus+3))\r")
            wait(lambda:root.evaluate("document.querySelector('.powershell-terminal-host .xterm-screen').textContent.includes('DOCK_AGAIN=43')"))
            record.update(success=True,actualWebView2=True,typedBeforeAndAfterDetach=True,backspace=True,ctrlC=True,inputNodeNeverRemoved=True,sameSessionVariable=True)
            print('  [PASS] 冻结原生WebView2真实逐键/退格/Enter/Ctrl+C/状态刷新；返回同PowerShell变量与输入正常')
            return True
        except Exception as exc:
            record.update(success=False,error=repr(exc));raise
        finally:
            output=ROOT/'.agent-runtime/native-input-fix';output.mkdir(exist_ok=True)
            (output/'frozen-keyboard.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
            for page in pages:
                try:page.close()
                except Exception:pass
            if process and process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));process.wait(timeout=15)


if __name__=='__main__':sys.exit(0 if run(sys.argv[1]) else 1)
