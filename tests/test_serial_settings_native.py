"""真实原生WebView2配置小窗；主窗与配置窗均隐藏，不触及实体COM。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def run():
    import webview
    import server
    from tests.test_serial_terminal import service
    from tests.test_serial_ui import exclusive_port
    from tests.test_ui_e2e import _FakeWorker
    from tests.edge_test_support import webview_test_environment
    with tempfile.TemporaryDirectory(prefix='serial-settings-native-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        previous=server.HTTP_PORT,server.WS_PORT
        server.HTTP_PORT=exclusive_port(28380);server.WS_PORT=exclusive_port(28650)
        env={'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1',
             **webview_test_environment()}
        result={}
        with patch.dict(os.environ,env),patch.object(server,'ADBWorker',_FakeWorker),patch.object(server.ManagerServer,'_commands_path',return_value=str(runtime/'commands.json')):
            manager=server.ManagerServer();manager.serial.close()
            fake,created=service(runtime/'serial.json');manager.serial=fake;manager.extension_tools.services['serial']=fake
            http=server.start_http_server()
            backend=threading.Thread(target=lambda:asyncio.run(manager.run()),daemon=True);backend.start()
            def wait(fn):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    value=fn()
                    if value:return value
                    time.sleep(.05)
                raise AssertionError('原生串口配置等待超时')
            wait(lambda:getattr(manager,'_ws_port',0))
            main=webview.create_window('串口配置验证',f'http://127.0.0.1:{server._HTTP_PORT}/',hidden=True,focus=False,width=900,height=680)
            def create(*args,**kwargs):
                kwargs.update(hidden=True,focus=False)
                return webview.create_window(*args,**kwargs)
            manager.desktop_terminals.bind_native(SimpleNamespace(create_window=create))
            def verify():
                try:
                    wait(lambda:main.evaluate_js("typeof SerialIntegration!=='undefined'&&ws?.readyState===1"))
                    main.evaluate_js("document.getElementById('window-serial').click()")
                    wait(lambda:main.evaluate_js("!document.getElementById('serial-open').disabled"))
                    main.evaluate_js("document.getElementById('serial-open').click()")
                    wait(lambda:main.evaluate_js('SerialIntegration.windows.size===1'))
                    main.evaluate_js("window.w=[...SerialIntegration.windows.values()][0];window.sid=w.sessionId;w.panel.querySelector('.serial-settings-toggle').click();true")
                    record=wait(lambda:next((r for r in manager.desktop_terminals.records.values() if r.get('native') and r.get('child')),None))
                    child=record['native'];handle=child.native.Handle.ToInt64()
                    wait(lambda:child.evaluate_js("document.getElementById('desktop-message')?.hidden===true&&!!document.querySelector('.serial-newline')"))
                    assert child.evaluate_js("!document.querySelector('.terminal-xterm')")
                    child.evaluate_js("(()=>{const e=document.querySelector('.serial-char-delay');e.value='20';e.dispatchEvent(new Event('change',{bubbles:true}));return true;})()")
                    wait(lambda:main.evaluate_js("w.panel.querySelector('.serial-char-delay').value==='20'"))
                    main.evaluate_js("w.panel.querySelector('.serial-settings-toggle').click()")
                    assert len(manager.desktop_terminals.records)==1 and child.native.Handle.ToInt64()==handle
                    child.destroy();wait(lambda:not manager.desktop_terminals.records)
                    assert main.evaluate_js('w.sessionId===sid') and not created[0][2].closed
                    main.evaluate_js("w.panel.querySelector('.serial-settings-toggle').click()")
                    wait(lambda:len(manager.desktop_terminals.records)==1)
                    main.evaluate_js('w.close();true')
                    wait(lambda:not manager.desktop_terminals.records and created[0][2].closed)
                    result.update(success=True,nativeWebView2=True,sameWindowHandle=True,sameSerialSession=True,settingsSync=True,settingsCloseKeepsCOM=True,serialCloseReclaimsSettings=True)
                except Exception as error:
                    import traceback
                    result.update(success=False,error=repr(error),traceback=traceback.format_exc())
                finally:
                    manager.desktop_terminals.close_all();main.destroy()
            try:
                webview.start(verify,private_mode=True)
            finally:
                manager.stop();backend.join(timeout=15);http.shutdown();http.server_close();fake.close()
                server.HTTP_PORT,server.WS_PORT=previous
        output=ROOT/'.agent-runtime/serial-log-toolbar';output.mkdir(parents=True,exist_ok=True)
        (output/'native.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        assert result.get('success'),result
        print('[PASS] 真实原生WebView2独立配置窗同步/复用/OS关闭不关COM、串口关闭回收小窗')
        return True

if __name__=='__main__':
    sys.exit(0 if run() else 1)
