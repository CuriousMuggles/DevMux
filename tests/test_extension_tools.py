"""真实共享目录中转继电器，硬件为模拟；覆盖权限、代次与不回退本机。"""
import json
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
from extension_tools import ExtensionTools, remote_call
from usb_relay import USBRelayService
from tests.test_usb_relay import tool_files
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


def run():
    with tempfile.TemporaryDirectory(prefix='extension-relay-') as temp:
        root = Path(temp)
        tools = root / '从机工具'; tool_files(tools)
        slave = USBRelayService(root/'slave-tool.json')
        master = USBRelayService(root/'master-tool.json')
        registry = ExtensionTools({'relay': slave})
        async def check_origin():
            import server
            manager = server.ManagerServer.__new__(server.ManagerServer)
            manager.extension_tools = registry
            def resolve(target, kind):
                if target != 'computer:local':
                    raise ValueError('目标不存在')
                return {'local':True}
            manager.agent = SimpleNamespace(resolve=resolve)
            async def invoke(func, *args): return func(*args)
            manager._run_blocking = invoke
            responses = []
            async def send(value): responses.append(json.loads(value))
            for origin in ('https://foreign.example', '', 'http://127.0.0.1:not-a-port'):
                ws = SimpleNamespace(request=SimpleNamespace(headers={'Origin':origin}), send=send)
                for kind in ('extension-tool', 'usb-relay'):
                    await manager._handle_message(ws, {'type':kind,'tool':'relay','action':'permission',
                        'target':'computer:local','arguments':{'allowed':True}})
                    assert not responses[-1]['success'] and not slave.remote_allowed
            ws = SimpleNamespace(request=SimpleNamespace(headers={'Origin':f'http://127.0.0.1:{server._HTTP_PORT}'}),send=send)
            await manager._handle_message(ws, {'type':'extension-tool','tool':'relay','action':'capabilities'})
            assert not responses[-1]['success'], '缺少目标时不能默认执行本机'
            await manager._handle_message(ws, {'type':'extension-tool','tool':'relay','action':'capabilities','target':'computer:local'})
            assert responses[-1]['success'] and responses[-1]['data']['allowed']
        asyncio.run(check_origin())
        assert not registry.call('relay', 'capabilities', remote=True)['data']['allowed']
        for action, args in [('set', {'channel': 1, 'operation': 'open'}), ('permission', {'allowed': True})]:
            try:
                registry.call('relay', action, args, remote=True)
                raise AssertionError('从机权限没有阻止操作')
            except PermissionError: pass
        registry.call('relay', 'permission', {'allowed': True})
        assert USBRelayService(slave.config_path).remote_allowed
        mask = [0]; commands = []
        def probe(action, serial=''):
            if action == 'set':
                channel = slave._probe_channel
                operation = slave._probe_operation
                commands.append((serial, channel, operation))
                bit = 255 if channel == 255 else 1 << (channel-1)
                mask[0] = mask[0] | bit if operation == 'open' else mask[0] & ~bit
                return {'success': True, 'mask': mask[0]}
            return {'devices': [{'serial':'RELAY1234', 'channels':8}]} if action == 'scan' else {'mask':mask[0]}
        def control(argv, **kwargs):
            commands.append(argv)
            bit = 255 if int(argv[3]) == 255 else 1 << (int(argv[3])-1)
            mask[0] = mask[0] | bit if argv[2] == 'open' else mask[0] & ~bit
            return SimpleNamespace(returncode=0)
        slave._probe = probe
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute), patch.object(slave, '_run_child', side_effect=control):
            harness = _Harness(str(root/'bus'), master_local_devices_enabled=False)
            harness.slave.extension_tools = registry
            harness.master.extension_tools = ExtensionTools({'relay': master})
            try:
                harness.start(); harness.confirm_both()
                assert _wait_until(lambda: harness.master.node.connection_ready('pc-b')), '连接尚未验证'
                node_id = harness.slave.config.nodeId
                def call(action, args=None):
                    return remote_call(harness.master, node_id, 'relay', action, args or {})
                assert call('capabilities')['data']['allowed']
                assert call('save', {'toolDir': str(tools)})['success']
                assert slave.tool_dir == str(tools.resolve()) and master.tool_dir == ''
                assert call('scan')['data']['devices'][0]['serial'] == 'RELAY1234'
                assert call('open', {'serial':'RELAY1234'})['success']
                assert call('set', {'channel':1,'operation':'open'})['data']['states'][0]
                assert len(commands) == 1 and mask[0] == 1
                assert call('close')['success'] and mask[0] == 1
                assert call('open', {'serial':'RELAY1234'})['success']
                registry.call('relay', 'permission', {'allowed':False})
                try:
                    call('set', {'channel':1,'operation':'close'})
                    raise AssertionError('撤销权限后仍执行控制')
                except RuntimeError as error: assert '未允许' in str(error)
                assert len(commands) == 1 and mask[0] == 1
                try:
                    call('permission', {'allowed':True})
                    raise AssertionError('远程可以修改本机准入权限')
                except RuntimeError as error: assert '本机修改' in str(error)
                registry.call('relay', 'permission', {'allowed':True})
                pair = harness.master.node.pairing.get(node_id)
                stale = relay.new_request(relay.KIND_EXTENSION_TOOL, 'pc-a', payload={
                    'sourceNodeId':harness.master.config.nodeId,'targetNodeId':node_id,
                    'connectionId':pair.connectionId+'-old','tool':'relay','action':'set',
                    'args':{'channel':1,'operation':'close'}})
                harness.slave.node._serve_request('fake-request.json', stale)
                assert len(commands) == 1
                harness.slave.unpair_peer(harness.master.config.nodeId)
                try:
                    call('set', {'channel':1,'operation':'close'})
                    raise AssertionError('解除连接后仍可远程控制')
                except RuntimeError: pass
                assert len(commands) == 1 and master.states == []
                print('  [PASS] 共享目录继电器：从机路径、枚举、单路控制、关闭保持、权限撤销、旧代次和解除连接')
            finally: harness.stop()
        # 进入服务锁之后再检查权限/代次，验证等待期间的拒绝不会落到控制调用。
        with patch.object(slave, 'call') as invoke:
            try:
                registry.call('relay', 'set', {}, remote=True,
                              authorize=lambda: (_ for _ in ()).throw(PermissionError('已撤销连接')))
                raise AssertionError('执行锁内未重查准入')
            except PermissionError: pass
            invoke.assert_not_called()
        return True


def check_ui(ws_url, url, manager):
    from tests import test_ui_e2e as base
    previous = manager.relay
    with tempfile.TemporaryDirectory(prefix='relay-remote-ui-') as temp:
        root = Path(temp); tools = root/'从机继电器'; tool_files(tools)
        service = USBRelayService(root/'tool.json')
        mask = [0]; commands = []
        def probe(action, serial=''):
            if action == 'set':
                commands.append((serial, service._probe_channel, service._probe_operation))
                mask[0] = 2
                return {'success': True, 'mask': mask[0]}
            return {'devices':[{'serial':'REMOTE1','channels':8}]} if action == 'scan' else {'mask':mask[0]}
        service._probe = probe
        def execute(argv, **kwargs):
            commands.append(argv); mask[0] = 2
            return SimpleNamespace(returncode=0)
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute), patch.object(service, '_run_child', side_effect=execute):
            harness = _Harness(str(root/'bus'), master_local_devices_enabled=False)
            harness.slave.extension_tools = ExtensionTools({'relay':service})
            try:
                harness.start(); harness.confirm_both()
                assert _wait_until(lambda:harness.master.node.connection_ready('pc-b'))
                manager.relay = harness.master
                target = 'computer:'+harness.slave.config.nodeId
                # 前端正常选择，未允许时操作区必须禁用，不能改为本机执行。
                result = base._cdp_eval(ws_url, url, r'''(async()=>{
                  const el=s=>document.getElementById('usb-relay-'+s);
                  const wait=async()=>{for(let i=0;i<500;i++){if(el('overlay').getAttribute('aria-busy')==='false')return;await new Promise(r=>setTimeout(r,30));}throw new Error('远程 UI 等待超时');};
                  document.getElementById('tools-usb-relay').click();await wait();
                  const select=el('target-context').querySelector('select'); select.value=TARGET;
                  select.dispatchEvent(new Event('change'));await wait();
                  return select.value===TARGET && el('scan').disabled && el('target-context').textContent.includes('未允许') && !el('browse');
                })()'''.replace('TARGET',json.dumps(target)))
                assert result
                service.set_remote_permission(True)
                result = base._cdp_eval(ws_url, url, r'''(async()=>{
                  const el=s=>document.getElementById('usb-relay-'+s);
                  const wait=async()=>{for(let i=0;i<500;i++){if(el('overlay').getAttribute('aria-busy')==='false')return;await new Promise(r=>setTimeout(r,30));}throw new Error('远程 UI 等待超时');};
                  const click=async id=>{el(id).click();await wait();};
                  el('target-context').querySelector('button').click();await wait();
                  const remote=!el('path') && !el('browse') && !el('save');
                  await click('scan');await click('open');
                  el('channels').children[1].querySelector('[data-operation="open"]').click();await wait();
                  return remote && el('channels').children[1].textContent.includes('已接通');
                })()'''.replace('PATH',json.dumps(str(tools))))
                assert result and len(commands)==1 and service.tool_dir==''
                service.call('close')
                closed=base._cdp_eval(ws_url,url,r'''(async()=>{
                  for(let i=0;i<300;i++){if(document.getElementById('usb-relay-all-open').disabled)return true;await new Promise(r=>setTimeout(r,30));}
                  return false;
                })()''')
                assert closed, '从机本地关闭设备后主控按钮未同步禁用'
                harness.slave.unpair_peer(harness.master.config.nodeId)
                result=base._cdp_eval(ws_url,url,r'''(async()=>{
                  const root=document.getElementById('usb-relay-target-context');
                  root.querySelector('button').click();
                  for(let i=0;i<500;i++){if(document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='false')break;await new Promise(r=>setTimeout(r,30));}
                  const ok=root.querySelector('select').value===TARGET && document.getElementById('usb-relay-all-open').disabled;
                  document.getElementById('usb-relay-dismiss').click();return ok;
                })()'''.replace('TARGET',json.dumps(target)))
                assert result and len(commands)==1
                print('  [PASS] 远程继电器 Edge/WS：权限提示、从机目录、远程单路控制、解除连接不回退本机')
                return True
            finally:
                manager.relay=previous; harness.stop()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
