"""真实pywebview原生窗口：越过主窗口边界、同串口句柄、停靠与OS关闭。"""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def worker(output):
    import webview
    import server
    from tests.test_ui_e2e import _FakeWorker
    from tests.test_serial_terminal import service
    from tests.test_serial_ui import exclusive_port
    result={};backend=http=None
    with tempfile.TemporaryDirectory(prefix='native-plugin-host-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        env={'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}
        with patch.dict(os.environ,env),patch.object(server,'ADBWorker',_FakeWorker):
            server.HTTP_PORT=exclusive_port(20480);server.WS_PORT=exclusive_port(20780)
            http=server.start_http_server();manager=server.ManagerServer()
            fake,created=service(runtime/'serial.json');manager.serial.close();manager.serial=fake;manager.extension_tools.services['serial']=fake
            backend=threading.Thread(target=lambda:asyncio.run(manager.run()),daemon=True);backend.start()
            def wait(fn):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if fn():return
                    time.sleep(.05)
                raise AssertionError('原生窗口等待超时')
            wait(lambda:getattr(manager,'_ws_port',0))
            root=webview.create_window('NATIVE-PLUGIN-TEST',f'http://127.0.0.1:{server._HTTP_PORT}/',width=800,height=600,x=20,y=20,hidden=True,focus=False)
            # 此项通过JS验证协议输入；不激活测试子窗，避免干扰操作者键盘。
            def background_window(*args, **kwargs):
                kwargs['focus'] = False
                return webview.create_window(*args, **kwargs)
            manager.desktop_terminals.bind_native(SimpleNamespace(create_window=background_window))
            root.events.closed += lambda:manager.desktop_terminals.close_all()
            def rect(window):
                from ctypes import wintypes
                box=wintypes.RECT();handle=window.native.Handle.ToInt64()
                assert ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(handle),ctypes.byref(box))
                return dict(left=box.left,top=box.top,right=box.right,bottom=box.bottom)
            def verify():
                try:
                    wait(lambda:root.evaluate_js("typeof SerialIntegration!=='undefined' && ws?.readyState===1"))
                    import base64
                    from plugin_pack import pack
                    checked=[]
                    for identifier in ('usb-relay','db-flash','mijia'):
                        package=pack(ROOT/'plugins'/identifier,runtime/(identifier+'.dmp'))
                        manager.plugins.dispatch('install',{'package':base64.b64encode(package.read_bytes()).decode()})
                        manager.plugins.dispatch('enable',{'id':identifier})
                        root.evaluate_js("window.PluginIntegration.refresh()")
                        wait(lambda:root.evaluate_js("!!document.querySelector('[data-plugin-menu="+identifier+"]')"))
                        root.evaluate_js("document.querySelector('[data-plugin-menu="+identifier+"]').click()")
                        wait(lambda:any(r.get('native') and r.get('child') for r in manager.desktop_terminals.records.values()))
                        child=next(r['native'] for r in manager.desktop_terminals.records.values())
                        wait(lambda:child.evaluate_js("document.body.dataset.pluginReady==='true'"))
                        assert root.evaluate_js("!document.querySelector('iframe')"),'主窗口中不应嵌套插件窗口'
                        main_bounds=rect(root);child.move(main_bounds['right']+80,main_bounds['top']+20)
                        bounds=rect(child);assert bounds['left']>main_bounds['right'],(main_bounds,bounds)
                        child.move(-300,100);assert rect(child)['left']<0
                        child.resize(700,500)
                        child.minimize();wait(lambda:ctypes.windll.user32.IsIconic(ctypes.c_void_p(child.native.Handle.ToInt64())))
                        child.restore();child.maximize();wait(lambda:ctypes.windll.user32.IsZoomed(ctypes.c_void_p(child.native.Handle.ToInt64())))
                        child.restore();child_handle=child.native.Handle.ToInt64();child.destroy()
                        wait(lambda:not manager.desktop_terminals.records)
                        wait(lambda:not root.evaluate_js("window.PluginIntegration.isOpen('"+identifier+"')"))
                        assert manager.plugins.running[identifier].process.poll() is None
                        manager.plugins.dispatch('disable',{'id':identifier})
                        checked.append(identifier)
                    result.update(success=True,plugins=checked,outsideMain=True,negativeCoordinates=True,osClosePreservesBackend=True)
                except Exception as exc:result.update(success=False,error=repr(exc),traceback=traceback.format_exc())
                finally:
                    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    manager.desktop_terminals.close_all();root.destroy()
            try:webview.start(verify,private_mode=True)
            finally:
                manager.stop();backend.join(timeout=15);fake.close();http.shutdown();http.server_close()
    return result.get('success',False)


def run():
    from tests.edge_test_support import webview_test_environment
    output_dir=ROOT/'.agent-runtime/native-plugin-windows';output_dir.mkdir(parents=True,exist_ok=True)
    # 每次独立结果，防止异常退出后误用上次成功JSON；系统组件可能输出本地编码。
    output=output_dir/f'native-host-{time.time_ns()}.json'
    result=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),'--worker',str(output)],cwd=ROOT,
        capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
        env={**os.environ, **webview_test_environment()})
    output.with_suffix('.stdout.bin').write_bytes(result.stdout)
    output.with_suffix('.stderr.bin').write_bytes(result.stderr)
    data=json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'error':'子进程未返回本次结果'}
    passed=result.returncode==0 and data.get('success',False)
    record=dict(exitCode=result.returncode,result=data,stdout=str(output.with_suffix('.stdout.bin')),
                stderr=str(output.with_suffix('.stderr.bin')))
    (output_dir/'native-host-process.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print('  ['+('PASS' if passed else 'FAIL')+'] 原生桌面窗口:',data,'退出码:',result.returncode)
    if not passed:
        print(result.stdout.decode('utf-8',errors='replace')[-2000:])
        print(result.stderr.decode('utf-8',errors='replace')[-4000:])
    return passed


if __name__=='__main__':sys.exit(0 if (worker(sys.argv[2]) if '--worker' in sys.argv else run()) else 1)
