"""冻结EXE真实原生子窗：端口/能力接入、桌面矩形和OS关闭，不开设备。"""
import base64
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import agent_client
from app_version import APP_TITLE
from tests.test_serial_ui import exclusive_port
from websockets.sync.client import connect


def run(executable):
    binary=Path(executable).resolve();process=None;result={}
    with tempfile.TemporaryDirectory(prefix='native-terminal-frozen-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory);http=exclusive_port(21480);ws_port=exclusive_port(21780)
        env={**os.environ,'ADB_MANAGER_HEADLESS':'0','ADB_MANAGER_NO_LOCAL_DEVICES':'1','RELAY_CONFIG':str(runtime/'relay.json'),
            'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'HTTP_PORT':str(http),'WS_PORT':str(ws_port)}
        try:
            process=subprocess.Popen([str(binary)],cwd=runtime,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with patch.dict(os.environ,{'ADB_AGENT_RUNTIME_DIR':env['ADB_AGENT_RUNTIME_DIR']}):
                deadline=time.monotonic()+30
                while not agent_client.instances() and time.monotonic()<deadline:time.sleep(.1)
                record=agent_client.choose();pid=record['pid']
                api=ctypes.WinDLL('user32',use_last_error=True)
                api.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
                api.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
                api.GetWindowRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
                api.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
                callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
                api.EnumWindows.argtypes=[callback,w.LPARAM]
                def find(title):
                    found=[]
                    @callback
                    def inspect(handle,_):
                        owner=w.DWORD();api.GetWindowThreadProcessId(handle,ctypes.byref(owner))
                        if owner.value==pid:
                            text=ctypes.create_unicode_buffer(600);api.GetWindowTextW(handle,text,len(text))
                            if text.value==title:found.append(handle)
                        return True
                    api.EnumWindows(inspect,0);return found[0] if found else None
                def rect(handle):
                    box=w.RECT();assert api.GetWindowRect(handle,ctypes.byref(box))
                    return dict(left=box.left,top=box.top,right=box.right,bottom=box.bottom)
                def wait(fn):
                    end=time.monotonic()+20
                    while time.monotonic()<end:
                        value=fn()
                        if value:return value
                        time.sleep(.05)
                    raise AssertionError('冻结原生窗口等待超时')
                parent=wait(lambda:find(APP_TITLE));parent_bounds=rect(parent)
                with connect(f'ws://127.0.0.1:{record["wsPort"]}',origin=f'http://127.0.0.1:{http}',proxy=None,max_size=8*1024*1024) as socket:
                    events=[]
                    def call(typ,**kwargs):
                        identifier=uuid.uuid4().hex;socket.send(json.dumps(dict(type=typ,id=identifier,**kwargs)))
                        while True:
                            message=json.loads(socket.recv(timeout=20))
                            if message.get('id')==identifier:
                                assert message.get('success'),message.get('output');return message.get('data',{})
                            events.append(message)
                    title='NATIVE-FROZEN-'+uuid.uuid4().hex[:10]
                    opened=call('desktop-terminal-open',key='test:native-view',title=title,x=100,y=100,width=700,height=500)
                    assert opened['native'],'冻结程序未建立原生窗口'
                    identifier=opened['windowId'];call('desktop-terminal-bind',windowId=identifier)
                    end=time.monotonic()+20
                    while not any(m.get('type')=='desktop-terminal-ready' for m in events) and time.monotonic()<end:
                        events.append(json.loads(socket.recv(timeout=20)))
                    assert any(m.get('type')=='desktop-terminal-ready' for m in events),'冻结子页面未接入正确端口'
                    html='<div class="terminal-panel"><div class="terminal-panel-header"><span class="header-title">冻结原生视图</span><button class="terminal-dock">↙</button></div><div class="terminal-cell"><div class="terminal-xterm"></div></div></div>'
                    call('desktop-terminal-publish',windowId=identifier,payload=dict(kind='snapshot',initial=True,html=html,controls={},
                        readonly=True,fontSize=13,theme={},cols=80,rows=24,seq=1,history=[base64.b64encode(b'FROZEN_NATIVE_OK\r\n').decode()],dropped=False))
                    end=time.monotonic()+20
                    while not any(m.get('type')=='desktop-terminal-event' and m.get('payload',{}).get('kind')=='resize' for m in events) and time.monotonic()<end:
                        events.append(json.loads(socket.recv(timeout=20)))
                    assert any(m.get('type')=='desktop-terminal-event' for m in events),'冻结视图未渲染终端'
                    child=wait(lambda:find(title))
                    call('desktop-terminal-move',windowId=identifier,x=parent_bounds['right']+80,y=40)
                    outside=rect(child);assert outside['left']>parent_bounds['right'],(parent_bounds,outside)
                    call('desktop-terminal-move',windowId=identifier,x=-300,y=100)
                    assert rect(child)['left']<0,'冻结原生坐标仍被钳制'
                    owner=w.DWORD();api.GetWindowThreadProcessId(child,ctypes.byref(owner));assert owner.value==pid
                    assert api.PostMessageW(child,0x10,0,0),'自有子窗OS关闭失败'
                    end=time.monotonic()+15
                    while not any(m.get('type')=='desktop-terminal-closed' for m in events) and time.monotonic()<end:
                        events.append(json.loads(socket.recv(timeout=15)))
                    assert any(m.get('type')=='desktop-terminal-closed' for m in events),'OS关闭没有通知原会话创建者'
                    result.update(success=True,parentBounds=parent_bounds,outsideBounds=outside,native=True,noDeviceOpened=True)
            print('  [PASS] 冻结原生子窗口实际移出主窗口、负桌面坐标、正确端口/能力接入、OS关闭通知；未打开设备')
            return True
        finally:
            output=ROOT/'.agent-runtime/native-terminal-windows';output.mkdir(parents=True,exist_ok=True)
            (output/'frozen-native.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            if process and process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process.wait(timeout=10)


if __name__=='__main__':sys.exit(0 if run(sys.argv[1]) else 1)
