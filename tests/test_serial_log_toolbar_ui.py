"""真实Edge/WS与假COM验证串口日志、图标及独立配置窗。"""
import base64
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from websockets.sync.client import connect
from tests.test_serial_ui import run as ui_run
import server


def check(endpoint, manager):
    with tempfile.TemporaryDirectory(prefix='serial-log-check-') as directory, connect(endpoint, proxy=None, max_size=20*1024*1024) as socket:
        counter = 0
        def command(method, params=None, session=None):
            nonlocal counter
            counter += 1
            payload = dict(id=counter, method=method, params=params or {})
            if session: payload['sessionId'] = session
            socket.send(json.dumps(payload))
            while True:
                result = json.loads(socket.recv(timeout=30))
                if result.get('method') == 'Inspector.targetCrashed': raise AssertionError('Edge目标崩溃')
                if result.get('id') == counter:
                    assert not result.get('error'), result
                    return result['result']
        def evaluate(code, session=None):
            result = command('Runtime.evaluate', dict(expression=code, awaitPromise=True, returnByValue=True), session)
            assert 'exceptionDetails' not in result, result
            return result['result'].get('value')
        def wait(fn):
            # 该组依次启动三个真实Edge子窗口，完整套件负载下给窗口创建保留30秒。
            until = time.monotonic()+30
            while time.monotonic()<until:
                result = fn()
                if result:return result
                time.sleep(.03)
            raise AssertionError('日志/配置独立窗等待超时')
        def click(selector, session=None):
            xy=evaluate("(()=>{const e=document.querySelector("+json.dumps(selector)+");e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()",session)
            command('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',clickCount=1,**xy),session)
            command('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',clickCount=1,**xy),session)
        def child(identifier):
            target=wait(lambda:next((t for t in command('Target.getTargets')['targetInfos'] if '/desktop-terminal.html#'+identifier+':' in t['url']),None))
            sid=command('Target.attachToTarget',dict(targetId=target['targetId'],flatten=True))['sessionId']
            wait(lambda:evaluate("document.getElementById('desktop-message')?.hidden===true",sid))
            return sid
        command('Runtime.enable')
        wait(lambda:evaluate("typeof SerialIntegration!=='undefined'&&ws?.readyState===1"))
        evaluate("document.getElementById('window-serial').click()")
        wait(lambda:evaluate("!document.getElementById('serial-open').disabled"))
        evaluate("document.getElementById('serial-open').click()")
        wait(lambda:evaluate('SerialIntegration.windows.size===1'))
        evaluate("window.w=[...SerialIntegration.windows.values()][0];window.p=w.panel;window.originalSession=w.sessionId;window.originalTerm=w.term;true")
        serial_id=evaluate('originalSession')
        wire=manager.serial.ports['COM3']['transport']
        assert evaluate("[...p.querySelectorAll('.serial-toolbar button')].map(e=>e.textContent.trim())")==['⇄','📋','⚡','⌨交还','⚙']
        assert evaluate("!p.querySelector('.serial-settings-content .serial-control-toggle')&&p.querySelector('.serial-control-toggle').dataset.state==='release'")
        assert evaluate("p.querySelector('.serial-settings-panel').hidden")
        click('.serial-settings-toggle')
        identifier=wait(lambda:next((k for k,v in manager.desktop_terminals.records.items() if v['key'].startswith('settings:')),None))
        cfg=child(identifier)
        output=ROOT/'.agent-runtime/serial-log-toolbar';output.mkdir(parents=True,exist_ok=True)
        (output/'settings.png').write_bytes(base64.b64decode(command('Page.captureScreenshot',dict(format='png'),cfg)['data']))
        (output/'toolbar.png').write_bytes(base64.b64decode(command('Page.captureScreenshot',dict(format='png'))['data']))
        assert evaluate("!document.querySelector('.terminal-xterm')&&document.querySelector('.serial-connection-info').textContent.includes('COM3')",cfg)
        evaluate("(()=>{const e=document.querySelector('.serial-newline');e.value='crlf';e.dispatchEvent(new Event('change',{bubbles:true}));return true;})()",cfg)
        wait(lambda:evaluate("p.querySelector('.serial-newline').value==='crlf'"))
        click('.serial-settings-toggle')
        assert len(manager.desktop_terminals.records)==1
        # 物理参数显式应用且保留原transport/session；未应用草稿不被轮询覆盖。
        evaluate("(()=>{for(const [name,value] of Object.entries({baudrate:'115200',bytesize:'7',parity:'E',stopbits:'2',flowcontrol:'rtscts'})){const e=document.querySelector('.serial-config-'+name);e.value=value;e.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()",cfg)
        click('.serial-config-apply',cfg)
        wait(lambda:manager.serial.ports['COM3']['config']['baudrate']==115200)
        wait(lambda:evaluate("p.querySelector('.serial-config-result').textContent==='连接参数已应用'"))
        assert manager.serial.ports['COM3']['transport'] is wire and evaluate('w.sessionId===originalSession')
        assert manager.serial.ports['COM3']['config']['parity']=='E' and wire.settings['rtscts']
        wait(lambda:evaluate("!document.querySelector('.serial-config-baudrate').disabled",cfg))
        evaluate("(()=>{const e=document.querySelector('.serial-config-baudrate');e.focus();e.value='57600';e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()",cfg)
        time.sleep(.2);assert evaluate("document.querySelector('.serial-config-baudrate').value",cfg)=='57600', (evaluate("document.querySelector('.serial-config-baudrate').value",cfg), evaluate("p.querySelector('.serial-config-baudrate').value"))
        click('.serial-config-reset',cfg)
        wait(lambda:evaluate("document.querySelector('.serial-config-baudrate').value",cfg)=='115200')
        click('.serial-xmodem-icon')
        x_id=wait(lambda:next((k for k,v in manager.desktop_terminals.records.items() if v['key'].startswith('settings:xmodem:')),None))
        xchild=child(x_id)
        assert evaluate("!!document.querySelector('.serial-xmodem')&&!document.querySelector('.serial-config-baudrate')",xchild)
        # 独立小窗无串口终端祖先；换行按钮不得与说明/状态/进度交叠。
        for width in (320,460,520,720):
            command('Emulation.setDeviceMetricsOverride',dict(width=width,height=560,deviceScaleFactor=1,mobile=False),xchild)
            layout=evaluate("""(()=>{
              const q=s=>document.querySelector(s),rect=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right};};
              q('.xmodem-resume').hidden=false;
              q('.serial-transfer-status').textContent='接收 · 请求结果待查询，不会自动重启 · '+ '长状态验证'.repeat(20);
              const row=rect(q('.serial-xmodem .serial-options')),note=rect(q('.serial-transfer-note')),status=rect(q('.serial-transfer-status')),progress=rect(q('.xmodem-progress'));
              const controls=[...q('.serial-xmodem .serial-options').querySelectorAll('strong,label,button')].filter(e=>!e.hidden).map(rect);
              return {row,note,status,progress,controls,width:innerWidth};
            })()""",xchild)
            assert all(r['bottom']<=layout['row']['bottom']+.5 for r in layout['controls']),layout
            assert layout['note']['top']>=layout['row']['bottom']-.5,layout
            assert layout['status']['top']>=layout['note']['bottom']-.5,layout
            assert layout['progress']['top']>=layout['status']['bottom']-.5,layout
            assert all(r['right']<=width+.5 and r['left']>=0 for r in layout['controls']),layout
            (output/f'xmodem-{width}.png').write_bytes(base64.b64decode(command('Page.captureScreenshot',dict(format='png'),xchild)['data']))
        click('.terminal-panel-header .close',xchild)
        wait(lambda:len(manager.desktop_terminals.records)==1)

        # 日志文件选择复用现有本机保存窗口接口，测试替换对话框而不弹系统框。
        log_path=Path(directory)/'serial.log';log_path.write_text('已有记录\n',encoding='utf-8')
        original_picker=manager._pick_recording_file
        async def picker(_name=''):return dict(success=True,path=str(log_path))
        manager._pick_recording_file=picker
        try:
            click('.serial-log');click('.serial-terminal-panel .log-browse')
            wait(lambda:evaluate('p.querySelector(".log-save-path").value')==str(log_path))
            click('.serial-terminal-panel .log-toggle')
            wait(lambda:evaluate("p.querySelector('.log-toggle').dataset.running==='true'"))
            wait(lambda:evaluate("document.querySelector('.serial-format').disabled",cfg))
            raw='中文输出\n'.encode();wire.rx.put(raw[:2]);time.sleep(.12);wire.rx.put(raw[2:])
            wait(lambda:'中文输出' in log_path.read_text('utf-8'))
            assert log_path.read_text('utf-8').startswith('已有记录\n[')
            # 另一个同Origin页面也不能录制当前GUI所属的接入。
            with connect(f'ws://127.0.0.1:{manager._ws_port}',origin=f'http://127.0.0.1:{server._HTTP_PORT}',proxy=None) as other:
                other.send(json.dumps(dict(type='serial-recording',id='deny',action='start',target='computer:local',sessionId=serial_id,path=str(Path(directory)/'forbidden.log'))))
                while True:
                    reply=json.loads(other.recv(timeout=10))
                    if reply.get('id')=='deny':break
                assert not reply['success'] and not (Path(directory)/'forbidden.log').exists(),reply
            click('.serial-terminal-panel .log-toggle');wait(lambda:evaluate("p.querySelector('.log-toggle').dataset.running==='false'"))
            before=log_path.read_bytes();wire.rx.put(b'NOT_RECORDED\n');time.sleep(.2);assert log_path.read_bytes()==before
            click('.terminal-panel-header .close',cfg)
            wait(lambda:not manager.desktop_terminals.records)
            assert not wire.closed
            # 从独立终端打开设置，也保留同一终端和串口接入。
            evaluate('w.floating.detach(250,180)')
            terminal_id=wait(lambda:evaluate('w.floating.windowId'))
            detached=child(terminal_id)
            click('.serial-settings-toggle',detached)
            settings_id=wait(lambda:next((k for k,v in manager.desktop_terminals.records.items() if v['key'].startswith('settings:')),None))
            cfg=child(settings_id)
            assert evaluate('w.sessionId===originalSession&&w.term===originalTerm')
            assert len(manager.serial.ports)==1 and len(manager.desktop_terminals.records)==2
            # 配置小窗不进入终端标签，不新建COM；关闭串口同时回收小窗。
            evaluate('w.close();true')
            wait(lambda:wire.closed and not manager.desktop_terminals.records)
            assert not manager.serial_recordings._records
            output=ROOT/'.agent-runtime/serial-log-toolbar';output.mkdir(parents=True,exist_ok=True)
            (output/'captured.log').write_bytes(before)
            print('[PASS] 实际Edge停靠/独立串口图标、独立配置同步复用/回收、同会话、日志追加/时间戳/中文/停止与WS所属拒绝')
        finally:
            manager._pick_recording_file=original_picker


def run():
    return ui_run(check_only=check)

if __name__=='__main__':
    sys.exit(0 if run() else 1)
