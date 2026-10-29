"""串口命令输入定向回归：真实处理函数和Edge假COM链路。"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NODE = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('renderer/serial-terminal.js', 'utf8');
const commands = source.slice(source.indexOf('    const commandHistory = []'), source.indexOf('    const terminalInput ='));
const keys = source.slice(source.indexOf('    command.onkeydown ='), source.indexOf("    panel.querySelector('.serial-send')"));
function session() {
  const command = {value:'',selectionStart:0,selectionEnd:0,dispatchEvent(){},
    setRangeText(text,start,end){this.value=this.value.slice(0,start)+text+this.value.slice(end);this.selectionStart=this.selectionEnd=start+text.length;}};
  const writes = [], format={value:'text'}, newline={value:'cr'}, status={};
  const context={command,writes,format,newline,status,alive:true,failed:false,pendingReceipt:false,transferBusy:false,configBusy:false,accept:true,Event:class {},
    input(data,mode){if(!context.accept)return;writes.push({data,mode});return true;},
    lineEnding:()=>({cr:'\r',lf:'\n',crlf:'\r\n',none:''})[newline.value]};
  vm.createContext(context);vm.runInContext(commands+keys+';globalThis.send=sendCommand;',context);
  return {...context,key(key,extra={}){let prevented=false;command.onkeydown({key,preventDefault(){prevented=true;},...extra});return prevented;},context};
}
const a=session();
a.command.value='AXB';a.command.selectionStart=1;a.command.selectionEnd=2;
assert(a.key('Enter',{ctrlKey:true}));assert.equal(a.command.value,'A\nB');assert.equal(a.command.selectionStart,2);assert.equal(a.writes.length,0);
a.key('Enter');assert.equal(a.writes[0].data,'A\rB\r');
a.key('ArrowUp');assert.equal(a.command.value,'A\nB');a.key('ArrowDown');assert.equal(a.command.value,'');
assert(!a.key('Enter',{isComposing:true}));assert(!a.key('Enter',{keyCode:229}));assert.equal(a.writes.length,1);
for(const mode of ['cr','lf','crlf','none']){a.newline.value=mode;const e=a.lineEnding();a.send('one\r\ntwo\rthree\nfour\n');assert.equal(a.writes.at(-1).data,['one','two','three','four',''].join(e));}
a.format.value='hex';a.send('00 FF\n41');assert.equal(a.writes.at(-1).data,'00 FF\n41');assert.equal(a.writes.at(-1).mode,'hex');
let count=a.writes.length;a.command.value='0G';a.key('Enter');assert.equal(a.writes.length,count);assert.equal(a.command.value,'0G');
a.format.value='text';a.context.accept=false;a.command.value='KEEP';a.key('Enter');assert.equal(a.command.value,'KEEP');assert.equal(a.writes.length,count);
a.context.accept=true;a.context.pendingReceipt=true;a.key('Enter');assert.equal(a.writes.length,count);a.context.pendingReceipt=false;
a.context.transferBusy=true;a.key('Enter');assert.equal(a.writes.length,count);a.context.transferBusy=false;
a.command.value='';a.key('Enter');assert.equal(a.writes.length,count);
const b=session();b.key('ArrowUp');assert.equal(b.command.value,'');
for(let i=0;i<55;i++)b.send('cmd'+i);b.send('cmd54');for(let i=0;i<60;i++)b.key('ArrowUp');assert.equal(b.command.value,'cmd5');
for(let i=0;i<50;i++)b.key('ArrowDown');assert.equal(b.command.value,'');
b.command.value='xy';b.command.selectionStart=b.command.selectionEnd=1;b.key('Enter',{shiftKey:true});assert.equal(b.command.value,'x\ny');
console.log('[PASS] 串口历史50条/去重/窗口隔离、多种换行、HEX、选区换行、IME、拒绝发送与状态门控');
"""

SCRIPT = r"""
(async()=>{
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('命令输入等待超时');};
 const assert=(x,m)=>{if(!x)throw Error(m);};
 await wait(()=>window.SerialIntegration&&ws.readyState===1);
 document.getElementById('window-serial').click();await wait(()=>!document.getElementById('serial-open').disabled);
 document.getElementById('serial-open').click();await wait(()=>SerialIntegration.windows.size===1);
 const w=[...SerialIntegration.windows.values()][0], p=w.panel, c=p.querySelector('.terminal-cmd-textarea');
 const original=callWS, writes=[];
 callWS=async(type,args,...rest)=>{const r=await original(type,args,...rest);if(type==='extension-tool'&&args.action==='write'&&r.success)writes.push(args.arguments);return r;};
 const key=(key,extra={})=>c.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true,...extra}));
 c.value='AXB';c.setSelectionRange(1,2);key('Enter',{ctrlKey:true});
 assert(c.value==='A\nB'&&c.selectionStart===2&&!writes.length,'Ctrl+Enter必须换行且不发送');
 key('Enter');await wait(()=>writes.length===1);assert(writes[0].data==='A\rB\r','CR多行转换');
 key('ArrowUp');assert(c.value==='A\nB','上查历史');key('ArrowDown');assert(c.value==='','下查回到空框');
 c.value='输入法';key('Enter',{isComposing:true});assert(c.value==='输入法'&&writes.length===1,'输入法确认不得发送');
 p.querySelector('.serial-newline').value='crlf';c.value='one\ntwo\n';p.querySelector('.serial-send').click();
 await wait(()=>writes.length===2);assert(writes[1].data==='one\r\ntwo\r\n','CRLF末尾不得重复');
 const format=p.querySelector('.serial-format');format.value='hex';format.dispatchEvent(new Event('change'));
 c.value='00 FF\n41';key('Enter');await wait(()=>writes.length===3);assert(writes[2].format==='hex'&&writes[2].data==='00 FF\n41','HEX原始字节语义');
 await w.close();callWS=original;return true;
})()
"""


def run():
    result = subprocess.run(['node', '-e', NODE], cwd=ROOT, text=True, encoding='utf-8', capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    print(result.stdout, end='')
    assert result.returncode == 0, result.stderr
    from tests.test_serial_ui import run as ui_run
    from tests.test_ui_e2e import _cdp_eval
    def check(endpoint, manager):
        assert _cdp_eval(endpoint, '', SCRIPT)
        print('[PASS] 实际Edge/WS假COM命令输入、历史、CR/CRLF、HEX与IME')
    return ui_run(check_only=check)

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
