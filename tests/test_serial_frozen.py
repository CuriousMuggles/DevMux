"""隔离冻结程序只读冒烟：版本资源、串口扩展和 Windows COM 枚举。"""
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import agent_client
from tests.test_ui_e2e import _free_port


def run(executable=None):
    binary=Path(executable) if executable else ROOT/'.agent-runtime/serial/freeze/dist/DevMux.exe'
    if not binary.is_file():
        print('  [SKIP] 未提供本次串口冻结验证EXE');return None
    with tempfile.TemporaryDirectory(prefix='serial-frozen-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        env={**os.environ,'ADB_MANAGER_HEADLESS':'1','ADB_MANAGER_NO_LOCAL_DEVICES':'1',
             'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),
             'ADB_MANAGER_TOOL_CACHE':str(runtime/'tool-cache'),
             'ANDROID_USER_HOME':str(runtime/'android-user'),'ADB_MDNS_AUTO_CONNECT':'0',
             'HTTP_PORT':str(_free_port(19380)),'WS_PORT':str(_free_port(19650))}
        process=subprocess.Popen([str(binary.resolve())],env=env,cwd=str(runtime),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            with patch.dict(os.environ,{'ADB_AGENT_RUNTIME_DIR':env['ADB_AGENT_RUNTIME_DIR']}):
                deadline=time.monotonic()+30
                while not agent_client.instances() and time.monotonic()<deadline:time.sleep(.1)
                assert len(agent_client.instances())==1,'冻结程序未注册Agent实例'
                async def checks():
                    listed=await agent_client.call('extensions_list',{})
                    assert listed['success'] and any(t['tool']=='serial' for t in listed['data']['tools']),listed
                    async def call(action):
                        result=await agent_client.call('extension_call',{'target':'computer:local','tool':'serial',
                            'action':action,'arguments':{}})
                        assert result['success'] and result['data']['success'],result
                        return result['data']['data']
                    config=await call('config');assert config['defaults']['baudrate']==921600
                    ports=await call('ports');assert isinstance(ports['ports'],list)
                    return len(ports['ports'])
                count=asyncio.run(checks())
                with urllib.request.urlopen(f'http://127.0.0.1:{env["HTTP_PORT"]}/',timeout=5) as response:
                    html=response.read().decode('utf-8')
                assert 'window-serial' in html and 'serial-terminal.js' in html and 'terminal-window.js' in html and '__WS_PORT__' not in html
                from PyInstaller.archive.readers import CArchiveReader
                archive=CArchiveReader(str(binary.resolve()))
                assert 'serial_stream' in archive.open_embedded_archive('PYZ.pyz').toc,'冻结包漏掉持续流模块'
                assert 'renderer\\terminal-window.js' in archive.toc or 'renderer/terminal-window.js' in archive.toc,'冻结包漏掉统一窗口资源'
                print(f'  [PASS] 冻结WS/Agent扩展、921600默认值、Windows COM枚举（{count}口，只读）、窗口菜单资源')
                print('  EXE SHA256:',hashlib.sha256(binary.read_bytes()).hexdigest())
                return True
        finally:
            if process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process.wait(timeout=10)


if __name__=='__main__':sys.exit(0 if run(sys.argv[1] if len(sys.argv)>1 else None) is not False else 1)
