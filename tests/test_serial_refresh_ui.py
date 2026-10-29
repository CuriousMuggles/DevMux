"""真实Edge/共享目录：串口手动重开后刷新不重复，同名COM按目标隔离。"""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from extension_tools import ExtensionTools
from tests.test_serial_terminal import service, wait
from tests.test_ui_e2e import _cdp_eval
from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


def check(endpoint, manager):
    original = manager.relay
    with tempfile.TemporaryDirectory(prefix='serial-refresh-ui-') as directory, \
         _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        root = Path(directory)
        first, created = service(root/'first.json')
        second, created2 = service(root/'second.json')
        registries = [ExtensionTools({'serial': item}) for item in (first, second)]
        for registry in registries:
            registry.call('serial', 'permission', {'allowed': True})
        harness = _Harness(str(root/'bus'), master_local_devices_enabled=False)
        other = harness._make('pc-c', 'node-c', 'slave', .5, None, False)
        harness.slave.extension_tools = registries[0]
        other.extension_tools = registries[1]
        try:
            harness.start(); other.start()
            assert _wait_until(lambda: len([p for p in harness.master.status()['pairing'] if p.get('online')]) == 2, 10)
            harness.confirm_both()
            ok, reason = harness.master.confirm_peer('pc-c')
            assert ok, reason
            assert _wait_until(lambda: harness.master.node.connection_ready('pc-b') and harness.master.node.connection_ready('pc-c'))
            manager.relay = harness.master
            targets = ['computer:'+item.config.nodeId for item in (harness.slave, other)]
            result = _cdp_eval(endpoint, '', r"""(async()=>{
              window.rfWait=async(fn,label)=>{for(let n=0;n<600;n++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('刷新串口等待超时：'+label);};
              await rfWait(()=>window.SerialIntegration&&typeof ws!=='undefined'&&ws.readyState===1,'应用就绪');
              TerminalWindows.closed.clear();
              window.rfTargets="""+json.dumps(targets)+r""";
              window.rfOpens=0;window.rfOriginalCall=window.callWS;
              window.callWS=(type,payload,...rest)=>{
                if(type==='extension-tool'&&payload.tool==='serial'&&payload.action==='open')rfOpens++;
                return rfOriginalCall(type,payload,...rest);
              };
              window.rfOpen=async(target,port)=>{
                document.getElementById('window-serial').click();await SerialIntegration.context.refresh();
                SerialIntegration.context.select.value=target;await SerialIntegration.context.choose();
                await rfWait(()=>!document.getElementById('serial-open').disabled,'打开按钮');
                const count=SerialIntegration.windows.size;
                document.getElementById('serial-port').value=port;document.getElementById('serial-open').click();
                await rfWait(()=>SerialIntegration.windows.size===count+1,'窗口打开');
              };
              window.rfRefresh=async()=>{
                document.getElementById('device-refresh-btn').click();
                document.getElementById('device-refresh-btn').click();
                await rfWait(()=>TerminalWindows.closed.size===0,'恢复完成');
                await new Promise(r=>setTimeout(r,120));
              };
              const ids=()=>[...SerialIntegration.windows.keys()].sort().join('|');
              await rfOpen(rfTargets[0],'COM3');await rfOpen(rfTargets[0],'COM12');
              let before=ids(),count=rfOpens;
              await rfRefresh();
              if(SerialIntegration.windows.size!==2||ids()!==before||rfOpens!==count)throw Error('首次刷新重复创建已打开窗口');
              // 用户显式关闭串口窗口后，刷新不能再次打开它；手动打开仍正常。
              for(const key of [...SerialIntegration.windows.keys()])await dismissTerminal(key);
              await rfWait(()=>SerialIntegration.windows.size===0,'关闭两口');
              if(TerminalWindows.closed.size)throw Error('serial close queued for refresh');
              await rfOpen(rfTargets[0],'COM3');await rfOpen(rfTargets[0],'COM12');
              before=ids();count=rfOpens;
              await rfRefresh();
              if(SerialIntegration.windows.size!==2||ids()!==before||rfOpens!==count)throw Error('手动重开两口后刷新又创建同样的窗口：'+SerialIntegration.windows.size);
              return {ok:true,keys:before};
            })()""")
            assert result['ok']
            assert len(created) == 4 and sum(not h['closed'] for h in first.sessions.values()) == 2
            result = _cdp_eval(endpoint, '', r"""(async()=>{
              await rfOpen(rfTargets[1],'COM3');
              const keep=[...SerialIntegration.windows.entries()].filter(([,w])=>w.target.target===rfTargets[1]||w.snapshot().port==='COM12').map(([k])=>k);
              const key=[...SerialIntegration.windows.entries()].find(([,w])=>w.target.target===rfTargets[0]&&w.snapshot().port==='COM3')[0];
              window.rfCloseCount=rfOpens;window.rfKeep=keep;
              await dismissTerminal(key);
              await rfWait(()=>SerialIntegration.windows.size===2,'serial close');
              if(TerminalWindows.closed.size)throw Error('closed serial left a refresh recipe');
              return {ok:true,windows:SerialIntegration.windows.size};
            })()""")
            assert result['ok'] and result['windows'] == 2
            assert 'COM3' not in first.ports and 'COM12' in first.ports
            assert sum(not h['closed'] for h in first.sessions.values()) == 1
            assert sum(not h['closed'] for h in second.sessions.values()) == 1
            result = _cdp_eval(endpoint, '', r"""(async()=>{
              await Promise.all([TerminalWindows.refreshClosed(),TerminalWindows.refreshClosed()]);
              await rfRefresh();
              if(SerialIntegration.windows.size!==2||window.rfKeep.some(k=>!SerialIntegration.windows.has(k))||rfOpens!==window.rfCloseCount)throw Error('refresh reopened a dismissed serial or changed another window');
              await rfOpen(rfTargets[0],'COM3');
              if(SerialIntegration.windows.size!==3||rfOpens!==window.rfCloseCount+1)throw Error('manual serial reopen failed');
              // Generic terminal recovery bookkeeping remains supported.
              const identity='test-refresh-generation';
              TerminalWindows.remember('test-old',async()=>{
                TerminalWindows.remember('test-new',async()=>true,identity);TerminalWindows.dismiss('test-new');return true;
              },identity);
              TerminalWindows.dismiss('test-old');await TerminalWindows.refreshClosed();
              if(!TerminalWindows.closed.has(identity))throw Error('旧恢复完成误删新关闭记录');
              await TerminalWindows.refreshClosed();
              if(TerminalWindows.closed.size)throw Error('新恢复记录未处理');
              return {ok:true,windows:SerialIntegration.windows.size};
            })()""")
            assert result['ok'] and result['windows'] == 3
            assert sum(not h['closed'] for h in first.sessions.values()) == 2
            assert sum(not h['closed'] for h in second.sessions.values()) == 1
            assert 'COM3' in first.ports and 'COM12' in first.ports
            print('  [PASS] 真实Edge远程串口显式关闭释放COM且刷新不重开；手动重开、其他串口和同名COM跨目标隔离通过')
        finally:
            try:
                _cdp_eval(endpoint, '', "(async()=>{if(window.rfOriginalCall)window.callWS=rfOriginalCall;await Promise.all([...SerialIntegration.windows.values()].map(w=>w.close()));TerminalWindows.closed.clear();return true;})()")
            finally:
                manager.relay = original
                other.stop(); harness.stop(); first.close(); second.close()
            _cdp_eval(endpoint, '', "(async()=>{SerialIntegration.context.select.value='computer:local';await SerialIntegration.context.choose();return true;})()")


def run():
    from tests.test_serial_ui import run as run_ui
    return run_ui(check_only=check)


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
