"""DB 远程任务通过真实共享目录中转，设备执行全部使用假执行器。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
from db_flash import DBFlashService, SimulationExecutor, SPECS
from db_flash_extension import DBFlashExtension
from extension_tools import ExtensionTools, remote_call
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


def fixture(root):
    for name in ['flashall.bat', 'flash_all.sh', 'scripts/download_image.py', 'images/u-boot/u-boot.bin'] + [s.default for s in SPECS]:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'fake image and tools; no hardware')


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='db-remote-', dir=ROOT / '.agent-runtime/db-flash')
        self.root = Path(self.temp.name)
        self.tools = self.root / '从机 DB'; fixture(self.tools)
        self.executor = SimulationExecutor(delay=.08)
        self.service = DBFlashService(self.executor)
        self.adapter = DBFlashExtension(self.service, self.root / 'slave-db.json')
        self.registry = ExtensionTools({'db-flash': self.adapter})
        self.args = {'toolDir':str(self.tools), 'selected':['system'], 'options':{'prepare':False}, 'requestId':uuid.uuid4().hex}

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_config_browse_permissions_and_remote_program_gate(self):
        self.assertFalse(self.registry.call('db-flash','capabilities',remote=True)['data']['allowed'])
        with self.assertRaises(PermissionError):
            self.registry.call('db-flash','start',self.args,remote=True,authorize=lambda:'master')
        self.assertTrue(self.registry.call('db-flash','save',{'toolDir':str(self.tools)})['success'])
        self.registry.call('db-flash','permission',{'allowed':True})
        self.assertTrue(DBFlashExtension(self.service,self.adapter.config_path).remote_allowed)
        result = self.registry.call('db-flash','browse',{'path':str(self.tools / 'images')},remote=True,authorize=lambda:'master')
        self.assertTrue(result['success'])
        self.assertIn('system.img', [i['name'] for i in result['data']['entries']])
        rejected = self.registry.call('db-flash','start',{**self.args,'fastbootPath':'cmd.exe'},remote=True,authorize=lambda:'master')
        self.assertFalse(rejected['success'])
        self.assertFalse(self.executor.calls)
        with self.assertRaises(PermissionError):
            self.registry.call('db-flash','permission',{'allowed':True},remote=True,authorize=lambda:'master')

    def test_idempotency_owner_restart_and_cursor(self):
        self.adapter.set_remote_permission(True)
        def call(action, args=None, owner='master'):
            return self.registry.call('db-flash',action,args,remote=True,authorize=lambda:owner)
        start = call('start',self.args)
        self.assertTrue(start['success'])
        again = call('start',self.args)
        self.assertEqual(start['data']['jobId'], again['data']['jobId'])
        self.assertFalse(call('cancel',{'jobId':start['data']['jobId']},owner='other')['success'])
        self.service._thread.join(3)
        self.assertEqual(len(self.executor.calls),2)
        status = call('status',{'requestId':self.args['requestId'],'after':2})['data']
        self.assertTrue(all(e['sequence']>2 for e in status['events']))
        self.assertNotIn('plan',status)
        self.assertTrue(self.adapter.call('status',{'jobId':start['data']['jobId']})['success'])
        restarted_service = DBFlashService(SimulationExecutor())
        restarted = DBFlashExtension(restarted_service,self.adapter.config_path)
        try:
            recovered = restarted.call_for_owner('start',self.args,owner='master')
            self.assertEqual(recovered['data']['state'],'interrupted')
            self.assertFalse(restarted_service.executor.calls, '重启后重复请求不能再次烧写')
        finally: restarted_service.close()

    def test_shared_folder_job_cancel_old_generation_and_unpair(self):
        self.adapter.set_remote_permission(True)
        entered = threading.Event()
        class WaitingExecutor(SimulationExecutor):
            def execute(self, step, cancel, output):
                self.calls.append(step)
                output('从机工具连续输出')
                entered.set()
                return not cancel.wait(5)
        self.service.close()
        self.executor = WaitingExecutor()
        self.service = DBFlashService(self.executor)
        self.adapter._service = self.service
        snapshot = {p.relative_to(self.tools).as_posix():p.read_bytes() for p in self.tools.rglob('*') if p.is_file()}
        with _patch(list_devices=_fake_list_devices,execute=_fake_execute):
            harness = _Harness(str(self.root / 'bus'),master_local_devices_enabled=False)
            harness.slave.extension_tools = self.registry
            try:
                harness.start(); harness.confirm_both()
                self.assertTrue(_wait_until(lambda:harness.master.node.connection_ready('pc-b')))
                node_id = harness.slave.config.nodeId
                def call(action,args=None):
                    return remote_call(harness.master,node_id,'db-flash',action,args or {})
                self.assertTrue(call('save',{'toolDir':str(self.tools)})['success'])
                start = call('start',self.args)['data']
                self.assertTrue(entered.wait(2))
                self.assertEqual(call('start',self.args)['data']['jobId'],start['jobId'])
                status = call('status',{'jobId':start['jobId'],'after':0})['data']
                self.assertEqual(status['state'],'running')
                self.assertTrue(any('从机工具' in e.get('text','') for e in status['events']))
                self.assertEqual(self.executor.calls[0]['cwd'],str(self.tools.resolve()))
                self.assertTrue(call('cancel',{'jobId':start['jobId']})['success'])
                self.service._thread.join(3)
                self.assertEqual(self.service.status()['state'],'cancelled')
                self.assertEqual(len(self.executor.calls),1)
                pair = harness.master.node.pairing.get(node_id)
                stale = relay.new_request(relay.KIND_EXTENSION_TOOL,'pc-a',payload={
                    'sourceNodeId':harness.master.config.nodeId,'targetNodeId':node_id,
                    'connectionId':pair.connectionId+'-old','tool':'db-flash','action':'start',
                    'args':{**self.args,'requestId':uuid.uuid4().hex}})
                harness.slave.node._serve_request('fake.json',stale)
                self.assertEqual(len(self.executor.calls),1)
                self.adapter.set_remote_permission(False)
                with self.assertRaises(RuntimeError): call('start',{**self.args,'requestId':uuid.uuid4().hex})
                self.adapter.set_remote_permission(True)
                harness.slave.unpair_peer(harness.master.config.nodeId)
                with self.assertRaises(RuntimeError): call('start',{**self.args,'requestId':uuid.uuid4().hex})
                self.assertEqual(snapshot,{p.relative_to(self.tools).as_posix():p.read_bytes() for p in self.tools.rglob('*') if p.is_file()})
            finally: harness.stop()

    def test_cancel_unknown_request_blocks_late_start(self):
        self.adapter.set_remote_permission(True)
        cancelled=self.adapter.call_for_owner('cancel',{'requestId':self.args['requestId']},owner='master')
        self.assertTrue(cancelled['success'])
        later=self.adapter.call_for_owner('start',self.args,owner='master')
        self.assertEqual(later['data']['state'],'cancelled')
        self.assertFalse(self.executor.calls)


def check_ui(ws_url,url,manager):
    from tests import test_ui_e2e as base
    previous_relay, previous_service = manager.relay, manager.db_flash
    with tempfile.TemporaryDirectory(prefix='remote-db-ui-',dir=ROOT / '.agent-runtime/db-flash') as temporary:
        root = Path(temporary)
        tools = root / '从机 DB 工具'; fixture(tools)
        outside = root / '从机镜像'; outside.mkdir()
        (outside / 'SYSTEM_新版.img').write_bytes(b'fake external image')
        service = DBFlashService(SimulationExecutor(delay=.6))
        adapter = DBFlashExtension(service,root / 'slave-db.json')
        adapter.call('save',{'toolDir':str(tools)})
        harness = _Harness(str(root / 'nodes'),master_local_devices_enabled=False)
        harness.slave.local_devices_enabled = False
        harness.slave.extension_tools = ExtensionTools({'db-flash':adapter})
        local_service = DBFlashService(SimulationExecutor())
        manager.db_flash = local_service
        try:
            with _patch(list_devices=_fake_list_devices,execute=_fake_execute):
                harness.start(); harness.confirm_both()
                assert _wait_until(lambda:harness.master.node.connection_ready('pc-b'))
                manager.relay = harness.master
                target = 'computer:' + harness.slave.config.nodeId
                setup = r'''(async()=>{
 const p=window.DBFlashIntegration.panel;
 const wait=async f=>{for(let i=0;i<400;i++){if(f())return;await new Promise(r=>setTimeout(r,30));}throw new Error('远程烧写界面等待超时');};
 document.getElementById('tools-db-flash').click();
 await wait(()=>p.context.targets.some(t=>t.target===TARGET));
 p.context.select.value=TARGET; await p.context.choose();
 return !p.context.ready && p.el('start').disabled && p.context.permissionRow.hidden;
})()'''.replace('TARGET',json.dumps(target))
                assert base._cdp_eval(ws_url,url,setup), '从机权限未阻止主控烧写'
                adapter.set_remote_permission(True)
                expression = r'''(async()=>{
 const p=window.DBFlashIntegration.panel;
 const wait=async f=>{for(let i=0;i<600;i++){if(f())return;await new Promise(r=>setTimeout(r,30));}throw new Error('远程烧写等待超时：'+p.el('message').textContent);};
 await p.context.refresh(); await wait(()=>p.context.ready);
 p.el('confirm').click(); await wait(()=>!p.busy && p.model?.toolValid);
 if(p.model.toolDir!==TOOLS)throw new Error('目录没有使用从机路径');
 p.el('pick').click(); await wait(()=>p.remoteFiles.current);
 p.remoteFiles.el('path').value=OUTSIDE; p.remoteFiles.el('load').click();
 await wait(()=>p.remoteFiles.current?.path===OUTSIDE);
 const check=p.remoteFiles.el('files').querySelector('input[type=checkbox]');
 check.click(); p.remoteFiles.el('accept').click(); await wait(()=>!p.busy);
 if(p.model.items.filter(i=>i.checked).map(i=>i.id).join()!=='system')throw new Error('从机镜像识别失败');
 for(const name of ['prepare','erase-env','reboot'])p.el(name).checked=false;
 p.el('start').click(); await wait(()=>!p.busy && p.running);
 p.close(); document.getElementById('tools-db-flash').click();
 await wait(()=>p.context.ready);
 await wait(()=>!p.running);
 if(!p.el('log').textContent.includes('模拟执行完成'))throw new Error('从机日志未回传');
 if(!p.el('message').textContent.includes('未进行硬件验证'))throw new Error('远程任务未完成');
 document.getElementById('tools-relay').click(); await wait(()=>window.ToolPanels.relayConfig.visible);
 document.getElementById('device-diagnostic-btn').click();
 const all=[document.getElementById('db-flash-overlay'),document.getElementById('relay-config-overlay'),document.getElementById('diag-overlay')];
 if(!all.every(x=>x.getAttribute('aria-modal')==='false'&&x.querySelector('.tool-window-resize-se')))throw new Error('工具未统一浮窗');
 document.getElementById('diag-close').click(); document.getElementById('relay-config-close').click();
 return p.context.target===TARGET && !p.context.local;
})()'''.replace('TOOLS',json.dumps(str(tools.resolve()))).replace('OUTSIDE',json.dumps(str(outside.resolve()))).replace('TARGET',json.dumps(target))
                assert base._cdp_eval(ws_url,url,expression)
                assert not local_service.executor.calls, '远程烧写回退到了主控'
                assert service.executor.calls[-1]['image']==str((outside/'SYSTEM_新版.img').resolve())
                assert not (tools/'SYSTEM_新版.img').exists()
                adapter.set_remote_permission(False)
                revoked = base._cdp_eval(ws_url,url,"(async()=>{const p=window.DBFlashIntegration.panel;await p.context.refresh();return !p.context.ready && p.el('start').disabled && p.context.target==="+json.dumps(target)+";})()")
                assert revoked, '撤销权限后应保留远程目标并禁用烧写'
                print('  [PASS] 远程 DB Edge/WS：从机权限、文件浏览批选、识别、开始、日志和关闭重开；全部工具浮窗')
                return True
        finally:
            harness.stop(); service.close(); local_service.close()
            manager.relay = previous_relay; manager.db_flash = previous_service


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RemoteTests)).wasSuccessful()


if __name__=='__main__':
    sys.exit(0 if run() else 1)
