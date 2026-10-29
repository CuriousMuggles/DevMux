"""实际 pywebview 米家子窗测试，设备和登录均为模拟。"""
import asyncio, ctypes, json, os, subprocess, sys, tempfile, threading, time, traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def worker(output):
    import server, webview
    from tests.test_ui_e2e import _FakeWorker
    from tests.test_serial_ui import exclusive_port
    from tests.test_mijia_detached import FakeMijia
    result={};backend=http=None
    with tempfile.TemporaryDirectory(prefix='native-mijia-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        env={'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}
        with patch.dict(os.environ,env),patch.object(server,'ADBWorker',_FakeWorker):
            server.HTTP_PORT=exclusive_port(22580);server.WS_PORT=exclusive_port(22850)
            http=server.start_http_server(); manager=server.ManagerServer();manager.mijia=FakeMijia()
            backend=threading.Thread(target=lambda:asyncio.run(manager.run()),daemon=True);backend.start()
            def wait(fn):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if fn():return
                    time.sleep(.05)
                raise AssertionError('原生米家窗口等待超时')
            wait(lambda:getattr(manager,'_ws_port',0))
            root=webview.create_window('MIJIA-NATIVE-TEST',f'http://127.0.0.1:{server._HTTP_PORT}/',width=800,height=600,x=20,y=20,hidden=True,focus=False)
            def launch(*args,**kwargs):kwargs['focus']=False;return webview.create_window(*args,**kwargs)
            manager.desktop_terminals.bind_native(SimpleNamespace(create_window=launch))
            root.events.closed += lambda:manager.desktop_terminals.close_all()
            def rect(window):
                from ctypes import wintypes
                box=wintypes.RECT();handle=window.native.Handle.ToInt64()
                assert ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(handle),ctypes.byref(box))
                return dict(left=box.left,top=box.top,right=box.right,bottom=box.bottom)
            def verify():
                try:
                    wait(lambda:root.evaluate_js("typeof ws!=='undefined'&&ws?.readyState===1&&!!window.MijiaIntegration"))
                    root.evaluate_js("document.getElementById('tools-mijia').click();document.querySelector('.mijia-refresh').click()")
                    wait(lambda:root.evaluate_js("!!document.querySelector('.mijia-card button')"))
                    root.evaluate_js("""(() => {const h=document.querySelector('.mijia-head');
                        h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:9,clientX:30,clientY:100}));
                        document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:9,clientX:-5,clientY:100,screenX:300,screenY:200}));
                        document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:9}));})()""")
                    wait(lambda:any(r.get('native') and r.get('child') for r in manager.desktop_terminals.records.values()))
                    child=next(r['native'] for r in manager.desktop_terminals.records.values())
                    wait(lambda:child.evaluate_js("document.getElementById('mijia-child-message')?.hidden===true"))
                    main=rect(root);child.move(main['right']+80,main['top']+20);bounds=rect(child)
                    assert bounds['left']>main['right'],(main,bounds)
                    child.evaluate_js("document.querySelector('.mijia-card button').click()")
                    wait(lambda:manager.mijia.on)
                    wait(lambda:child.evaluate_js("document.querySelector('.mijia-card button')?.textContent.includes('已开启')"))
                    child_handle=child.native.Handle.ToInt64()
                    child.evaluate_js("document.querySelector('.mijia-child-dock').click()")
                    wait(lambda:root.evaluate_js("!document.querySelector('.mijia-overlay').hidden"))
                    wait(lambda:not manager.desktop_terminals.records)
                    wait(lambda:not ctypes.windll.user32.IsWindow(ctypes.c_void_p(child_handle)))
                    root.evaluate_js("MijiaIntegration.detach(320,220)")
                    wait(lambda:any(r.get('native') and r.get('child') for r in manager.desktop_terminals.records.values()))
                    record=next(r for r in manager.desktop_terminals.records.values() if r.get('child'))
                    child_handle=record['native'].native.Handle.ToInt64()
                    asyncio.run_coroutine_threadsafe(record['child'].close(),manager.loop).result(timeout=5)
                    wait(lambda:root.evaluate_js("!document.querySelector('.mijia-overlay').hidden"))
                    wait(lambda:not manager.desktop_terminals.records)
                    wait(lambda:not ctypes.windll.user32.IsWindow(ctypes.c_void_p(child_handle)))
                    root.evaluate_js("MijiaIntegration.detach(320,220)")
                    wait(lambda:any(r.get('native') and r.get('child') for r in manager.desktop_terminals.records.values()))
                    child=next(r['native'] for r in manager.desktop_terminals.records.values());child_handle=child.native.Handle.ToInt64();child.destroy()
                    wait(lambda:root.evaluate_js("document.querySelector('.mijia-overlay').hidden"))
                    wait(lambda:not manager.desktop_terminals.records)
                    wait(lambda:not ctypes.windll.user32.IsWindow(ctypes.c_void_p(child_handle)))
                    time.sleep(1)  # 等待 WebView2 的窗口关闭回调完成，再退出主窗口。
                    result.update(success=True,outsideBounds=bounds,deviceWrite=True,dock=True,disconnectRestored=True,osClose=True)
                except Exception as exc: result.update(success=False,error=repr(exc),traceback=traceback.format_exc())
                finally:
                    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    manager.desktop_terminals.close_all();root.destroy()
            try:webview.start(verify,private_mode=True)
            finally:
                manager.stop();backend.join(timeout=15);http.shutdown();http.server_close()
    return result.get('success',False)


def run():
    from tests.edge_test_support import webview_test_environment
    output_dir=ROOT/'.agent-runtime/mijia-native';output_dir.mkdir(parents=True,exist_ok=True)
    output=output_dir/f'native-{time.time_ns()}.json'
    proc=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),'--worker',str(output)],cwd=ROOT,capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env={**os.environ,**webview_test_environment()})
    output.with_suffix('.stdout.bin').write_bytes(proc.stdout);output.with_suffix('.stderr.bin').write_bytes(proc.stderr)
    data=json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'error':'子进程未返回结果'}
    ok=proc.returncode==0 and data.get('success',False)
    print('[PASS]' if ok else '[FAIL]','米家原生窗口:',data,'退出码:',proc.returncode)
    if not ok:print(proc.stderr.decode('utf-8',errors='replace')[-3000:])
    return ok
if __name__=='__main__':sys.exit(0 if (worker(sys.argv[2]) if '--worker' in sys.argv else run()) else 1)
