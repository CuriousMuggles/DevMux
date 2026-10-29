"""继电器调用边界、超时和状态读取；测试不会切换真实硬件。"""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from usb_relay import USBRelayService


def tool_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    data = bytearray(128)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 80)
    data[80:84] = b'PE\0\0'
    struct.pack_into('<H', data, 84, 0x14c)
    for name in ('CommandApp_USBRelay.exe', 'usb_relay_device.dll'):
        (directory / name).write_bytes(data)


def run():
    try:
        # 源码回归不能依赖上一轮留下的构建产物。
        from relay_probe_build import build_relay_probe
        build_relay_probe(ROOT)
        with tempfile.TemporaryDirectory(prefix='usb-relay-test-') as temporary:
            root = Path(temporary)
            directory = root / '中文 空格'
            tool_files(directory)
            directory = directory.resolve()
            service = USBRelayService(root / 'instance-usb-relay.json')
            assert not service.call('set', {'operation': 'open', 'channel': 1})['success']
            assert not service.call('save', {'toolDir': str(root / 'missing')})['success']
            assert service.call('save', {'toolDir': str(directory)})['success']
            assert USBRelayService(service.config_path).tool_dir == str(directory.resolve())
            # 从工作树 build/ 目录直接启动会被当前Windows执行策略拒绝；冻结包同样从临时解压目录运行。
            helper = root / 'RelayProbe.exe'
            shutil.copyfile(build_relay_probe(ROOT), helper)
            # 无效 DLL 的文件检查仍成功，证明此阶段没有加载/初始化厂商库。
            check = subprocess.run([str(helper), 'check', str(directory)], capture_output=True,
                                   timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            assert check.returncode == 0 and json.loads(check.stdout)['stage'] == 'check'
            assert b'before-load' not in check.stderr
            # 真实调用接口必须为固定 EXE 参数，不再生成 PowerShell/C# 脚本。
            with patch.object(service, '_run_child', return_value=SimpleNamespace(
                    returncode=0, stdout=b'{"success":true,"mask":5}', stderr=b'')) as child:
                assert service._probe('status', 'abcde')['mask'] == 5
                argv = child.call_args.args[0]
                assert Path(argv[0]).name == 'RelayProbe.exe'
                builtin = ROOT / 'vendor' / 'usb-relay' / 'bin'
                assert argv[1:] == ['status', str(builtin), 'abcde']
                assert child.call_args.kwargs['cwd'] == str(builtin)
                assert not any('powershell' in str(arg).lower() or 'EncodedCommand' in str(arg) for arg in argv)
            with patch.object(service, '_run_child', return_value=SimpleNamespace(
                    returncode=1, stdout=b'{"success":false,"output":"probe-failed"}', stderr=b'')):
                try:
                    service._probe('scan')
                    raise AssertionError('未传播辅助程序错误')
                except RuntimeError as error:
                    assert 'probe-failed' in str(error)
            mask = [5]
            devices = [{'serial': 'abcde', 'channels': 8}]
            def probe(action, serial=''):
                return {'devices': devices} if action == 'scan' else {'mask': mask[0]}
            service._probe = probe
            with patch.object(service, '_probe', return_value={'success': True, 'stage': 'load'}) as diagnostic:
                snapshot = service._view()
                assert not service.call('diagnose', {'stage': 'set'})['success']
                assert not service.call('diagnose', {'stage': 'status', 'serial': 'bad;serial'})['success']
                diagnostic.assert_not_called()
                result = service.call('diagnose', {'stage': 'load'})
                assert result['success'] and result['data'] == snapshot
                diagnostic.assert_called_once_with('load', '')
            assert service.call('scan')['data']['devices'] == devices
            assert service.call('open', {'serial': 'abcde'})['data']['states'] == [True, False, True, False, False, False, False, False]
            with patch.object(service, '_probe', wraps=probe) as command:
                for args in ({'channel': 0, 'operation': 'open'}, {'channel': 9, 'operation': 'close'},
                             {'channel': True, 'operation': 'open'}, {'channel': 1, 'operation': 'bad'}):
                    assert not service.call('set', args)['success']
                command.assert_not_called()
                result = service.call('set', {'channel': 255, 'operation': 'close'})
                assert result['success']
                command.assert_called_once_with('set', 'abcde')
            captured = []
            def control_probe(action, serial=''):
                captured.append((action, serial, service._probe_channel, service._probe_operation))
                return {'success': True, 'mask': 2}
            with patch.object(service, '_probe', side_effect=control_probe):
                for channel, operation in ((1, 'open'), (1, 'close'), (255, 'open'), (255, 'close')):
                    result = service.call('set', {'channel': channel, 'operation': operation})
                    assert result['success'] and result['data']['states'][1]
                assert captured == [('set', 'abcde', c, op) for c, op in
                                    ((1, 'open'), (1, 'close'), (255, 'open'), (255, 'close'))]
            with patch.object(service, '_probe', side_effect=RuntimeError('控制失败')):
                result = service.call('set', {'channel': 1, 'operation': 'open'})
                assert not result['success'] and result['data']['states'] == [None]*8
            with patch.object(service, '_probe', side_effect=subprocess.TimeoutExpired('tool', 10)):
                result = service.call('set', {'channel': 1, 'operation': 'open'})
                assert not result['success'] and '未确认' in result['output']
            with patch.object(service, '_probe', return_value={'success': True, 'readError': '读取失败'}):
                result = service.call('set', {'channel': 1, 'operation': 'open'})
                assert result['success'] and '状态读取失败' in result['output'] and result['data']['states'] == [None]*8
            with patch.object(service, '_run_child') as command:
                assert service.call('close')['success']
                command.assert_not_called()
            service._probe = probe
            service.call('scan')
            service.call('open', {'serial': 'abcde'})
            devices.clear()
            result = service.call('scan')
            assert not result['data']['opened'] and not result['data']['devices']
            assert not service.call('unknown')['success']
        print('  [PASS] 继电器路径保存、通道校验、调用参数、状态读取、拔出及超时')
        return True
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f'  [FAIL] USB Relay: {exc}')
        return False


def check_ui(ws_url, url, manager):
    from tests import test_ui_e2e as base
    previous = manager.usb_relay
    with tempfile.TemporaryDirectory(prefix='usb-relay-ui-') as temporary:
        directory = Path(temporary) / '工具 中文'
        tool_files(directory)
        service = USBRelayService(Path(temporary) / 'ui-usb-relay.json')
        mask = [0]
        available = [True]
        reads = []
        slow_read = [False]
        read_started, release_read = threading.Event(), threading.Event()
        def probe(action, serial=''):
            reads.append(action)
            if action == 'set':
                channel, operation = service._probe_channel, service._probe_operation
                commands.append((serial, channel, operation))
                bit = 255 if channel == 255 else 1 << (channel-1)
                mask[0] = mask[0] | bit if operation == 'open' else mask[0] & ~bit
                return {'success': True, 'mask': mask[0]}
            if action == 'status' and slow_read[0]:
                slow_read[0] = False
                read_started.set()
                if not release_read.wait(15):
                    raise RuntimeError('测试读取等待超时')
            return ({'devices': [{'serial': 'RELAY1234', 'channels': 8}] if available[0] else []}
                    if action == 'scan' else {'mask': mask[0]})
        service._probe = probe
        actual_run = subprocess.run
        commands = []
        def execute(argv, **kwargs):
            if Path(argv[0]).name != 'CommandApp_USBRelay.exe':
                return actual_run(argv, **kwargs)
            commands.append(argv)
            channel = int(argv[3])
            bit = 255 if channel == 255 else 1 << (channel-1)
            mask[0] = mask[0] | bit if argv[2] == 'open' else mask[0] & ~bit
            return SimpleNamespace(returncode=0)
        manager.usb_relay = service
        previous_extension = manager.extension_tools.services['relay']
        manager.extension_tools.services['relay'] = service
        try:
            with patch.object(service, '_run_child', side_effect=execute):
                result = base._cdp_eval(ws_url, url, r'''(async () => {
                  const el = s => document.getElementById('usb-relay-'+s);
                  const wait = async predicate => { const end=Date.now()+10000;
                    while(Date.now()<end){if(predicate())return; await new Promise(r=>setTimeout(r,30));}
                    throw new Error('继电器 UI 等待超时：'+el('message').textContent); };
                  const click = async id => {el(id).click(); await wait(()=>document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='false');};
                  document.getElementById('tools-tab').click();
                  document.getElementById('tools-usb-relay').click();
                  await wait(()=>document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='false');
                  if (el('path') || el('browse') || el('save')) throw new Error('目录控件仍存在');
                  el('diagnostics').open=true; el('stage').value='check'; await click('diagnose');
                  const diagnostic=el('message').textContent.includes('诊断 check 完成') && el('close').disabled;
                  el('diagnostics').open=false;
                  await click('scan'); await click('open');
                  const rows=()=>[...el('channels').children];
                  const initial=rows().length===8 && rows().every(r=>r.textContent.includes('已断开'));
                  rows()[0].querySelector('[data-operation="open"]').click();
                  await wait(()=>document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='false');
                  const single=rows()[0].textContent.includes('已接通') && rows()[1].textContent.includes('已断开');
                  await click('all-open'); const all=rows().every(r=>r.textContent.includes('已接通'));
                  await click('all-close'); const off=rows().every(r=>r.textContent.includes('已断开'));
                  const sizes=[];
                  for(const width of [1280,360]) {
                    document.querySelector('.usb-relay-dialog').style.width=(width===360?'336px':'560px');
                    const dlg=document.querySelector('.usb-relay-dialog');
                    sizes.push({width,overflow:dlg.scrollWidth>dlg.clientWidth,
                      clipped:rows().some(r=>r.getBoundingClientRect().bottom>el('channels').getBoundingClientRect().bottom+1),
                      rows:rows().map(r=>({overflow:r.scrollWidth>r.clientWidth,buttons:[...r.querySelectorAll('button')].map(b=>({w:b.clientWidth,h:b.clientHeight}))}))});
                  }
                  document.querySelector('.usb-relay-dialog').style.width='';
                  await click('close'); const closed=el('all-open').disabled && rows().every(r=>r.textContent.includes('未知'));
                  await click('open');
                  return {ok:diagnostic&&initial&&single&&all&&off&&closed&&!sizes.some(s=>s.overflow||s.clipped||s.rows.some(r=>r.overflow)),sizes};
                })()'''.replace('PATH_PLACEHOLDER', json.dumps(str(directory))))
                assert result['ok'], result
                assert len(commands) == 3 and mask[0] == 0, commands
                # 打开诊断面板后跨过刷新周期，不得偷偷追加状态读取。
                base._cdp_eval(ws_url, url, "document.getElementById('usb-relay-diagnostics').open=true; true")
                read_count = len(reads)
                base._cdp_eval(ws_url, url, "(async()=>{await new Promise(r=>setTimeout(r,5500));return true;})()")
                assert len(reads) == read_count, reads
                # 模拟慢速自动读取：按钮仍可用且节点不重建；点击按顺序执行。
                slow_read[0] = True
                base._cdp_eval(ws_url, url, "document.getElementById('usb-relay-diagnostics').open=false; true")
                assert read_started.wait(8), '自动状态读取未触发'
                during_read = base._cdp_eval(ws_url, url, r'''(()=>{
                  const root=document.getElementById('usb-relay-overlay');
                  const button=document.querySelector('#usb-relay-channels [data-operation="open"]');
                  const ok=root.getAttribute('aria-busy')==='false' && !button.disabled && !document.getElementById('usb-relay-all-close').disabled;
                  button.focus(); window.relayRefreshButton=button; return ok;
                })()''')
                assert during_read, '自动刷新禁用了按钮'
                base._cdp_eval(ws_url, url, "window.relayRefreshButton.click(); true")
                assert len(commands) == 3, '读取完成前不应控制硬件'
                release_read.set()
                queued = base._cdp_eval(ws_url, url, r'''(async()=>{
                  const root=document.getElementById('usb-relay-overlay');
                  while(root.getAttribute('aria-busy')==='true') await new Promise(r=>setTimeout(r,30));
                  return document.getElementById('usb-relay-channels').firstElementChild.textContent.includes('已接通');
                })()''')
                assert queued and len(commands) == 4 and mask[0] == 1, commands
                # 可选截图使用模拟状态，不切换硬件。
                screenshot = os.environ.get('USB_RELAY_SCREENSHOT')
                if screenshot:
                    import base64
                    from websockets.sync.client import connect
                    mask[0] = 85
                    base._cdp_eval(ws_url, url, r'''(async()=>{
                      document.getElementById('relay-config-overlay').style.display='none';
                      document.getElementById('usb-relay-refresh').click();
                      while(document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='true') await new Promise(r=>setTimeout(r,30));
                    })()''')
                    with connect(ws_url, proxy=None, max_size=20*1024*1024) as ws:
                        def command(identifier, method, params):
                            ws.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
                            while True:
                                response = json.loads(ws.recv(timeout=15))
                                if response.get('id') == identifier:
                                    return response['result']
                        command(1, 'Emulation.setDeviceMetricsOverride', {'width': 1200, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                        bounds = command(2, 'Runtime.evaluate', {'expression': "(()=>{const r=document.querySelector('.usb-relay-dialog').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,scale:1};})()", 'returnByValue': True})['result']['value']
                        captured = command(3, 'Page.captureScreenshot', {'format': 'png', 'clip': bounds})
                        Path(screenshot).write_bytes(base64.b64decode(captured['data']))
                # 拔出时设备列表清空，禁止继续操作。
                available[0] = False
                removed = base._cdp_eval(ws_url, url, r'''(async()=>{
                  document.getElementById('usb-relay-scan').click();
                  while(document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='true') await new Promise(r=>setTimeout(r,30));
                  const ok=document.getElementById('usb-relay-all-open').disabled;
                  document.getElementById('usb-relay-dismiss').click(); return ok;
                })()''')
                assert removed
                print('  [PASS] 继电器真实 Edge + WS：配置、查找、打开、单路/全部开关、关闭、拔出及窄屏布局')
                return True
        finally:
            release_read.set()
            manager.usb_relay = previous
            manager.extension_tools.services['relay'] = previous_extension


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
