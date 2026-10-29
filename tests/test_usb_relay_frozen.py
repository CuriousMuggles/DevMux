"""隔离冻结程序，只读验证内置继电器资源和真实设备枚举。"""
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import agent_client
from tests.test_ui_e2e import _free_port
from PyInstaller.archive.readers import CArchiveReader

def run(binary):
    binary = Path(binary).resolve()
    archive = CArchiveReader(str(binary))
    keys = {key.replace('\\', '/'): key for key in archive.toc}
    for name in ('relay/RelayProbe.exe', 'relay/usb_relay_device.dll', 'renderer/usb-relay.js'):
        assert name in keys, name
    assert archive.extract(keys['relay/usb_relay_device.dll']) == (ROOT/'vendor/usb-relay/bin/usb_relay_device.dll').read_bytes()
    js = archive.extract(keys['renderer/usb-relay.js'])
    assert js == (ROOT/'renderer/usb-relay.js').read_bytes() and b'usb-relay-path' not in js
    with tempfile.TemporaryDirectory(prefix='usbrelay-frozen-', ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        env = {**os.environ, 'ADB_MANAGER_HEADLESS':'1', 'ADB_MANAGER_NO_LOCAL_DEVICES':'1',
               'RELAY_CONFIG':str(root/'relay.json'), 'ADB_AGENT_RUNTIME_DIR':str(root/'runtime'),
               'ADB_MANAGER_TOOL_CACHE':str(root/'tool-cache'), 'ADB_DIAG_DIR':str(root/'diag'),
               'ANDROID_USER_HOME':str(root/'android-user'), 'ADB_MDNS_AUTO_CONNECT':'0',
               'HTTP_PORT':str(_free_port(19380)), 'WS_PORT':str(_free_port(19650))}
        proc = subprocess.Popen([str(binary)], cwd=str(root), env=env, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            with patch.dict(os.environ, {'ADB_AGENT_RUNTIME_DIR':env['ADB_AGENT_RUNTIME_DIR']}):
                deadline = time.monotonic()+30
                while not agent_client.instances() and time.monotonic()<deadline:
                    time.sleep(.1)
                assert len(agent_client.instances()) == 1, '冻结程序未启动'
                async def call(action, args=None):
                    result = await agent_client.call('extension_call', {'target':'computer:local', 'tool':'relay',
                                                                      'action':action, 'arguments':args or {}})
                    assert result['success'] and result['data']['success'], result
                    return result['data']['data']
                async def checks():
                    devices = (await call('scan'))['devices']
                    assert devices, '未发现现场继电器'
                    state = await call('open', {'serial':devices[0]['serial']})
                    assert len(state['states']) == devices[0]['channels']
                    await call('close')
                    return devices
                devices = asyncio.run(checks())
                print('PASS frozen scan/open/close:', devices)
                print('SHA256:', hashlib.sha256(binary.read_bytes()).hexdigest())
        finally:
            if proc.poll() is None:
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                proc.wait(timeout=10)

if __name__ == '__main__':
    run(sys.argv[1])
