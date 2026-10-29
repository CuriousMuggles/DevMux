"""实际Edge通过共享目录关闭远程串口，端口使用可核对的假设备。"""
import json
import tempfile
from pathlib import Path
from tests.test_serial_terminal import service,wait
from tests.test_ui_e2e import _cdp_eval
from tests.test_relay_app import _Harness,_patch,_fake_list_devices,_fake_execute,_wait_until
from extension_tools import ExtensionTools


def check(endpoint,manager):
    original=manager.relay
    with tempfile.TemporaryDirectory(prefix='serial-close-ui-') as directory, \
            _patch(list_devices=_fake_list_devices,execute=_fake_execute):
        root=Path(directory);slave,created=service(root/'serial.json')
        registry=ExtensionTools({'serial':slave});registry.call('serial','permission',{'allowed':True})
        harness=_Harness(str(root/'bus'),master_local_devices_enabled=False)
        harness.slave.extension_tools=registry
        try:
            harness.start();harness.confirm_both()
            assert _wait_until(lambda:harness.master.node.connection_ready('pc-b'))
            manager.relay=harness.master
            target='computer:'+harness.slave.config.nodeId
            opened=_cdp_eval(endpoint,'',r'''(async()=>{
              window.closeWait=async fn=>{for(let i=0;i<400;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('远程串口关闭等待超时');};
              document.getElementById('window-serial').click();await SerialIntegration.context.refresh();
              SerialIntegration.context.select.value='''+json.dumps(target)+r''';await SerialIntegration.context.choose();
              await closeWait(()=>!document.getElementById('serial-open').disabled);
              document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();
              await closeWait(()=>SerialIntegration.windows.size===1);
              window.remoteCloseWindow=[...SerialIntegration.windows.values()][0];
              // 真实设备关闭不能依赖xterm等本地显示资源清理成功。
              remoteCloseWindow.term.dispose=()=>{throw Error('注入显示销毁异常');};
              remoteCloseWindow.panel.querySelector('.terminal-panel-header .close').click();
              await closeWait(()=>SerialIntegration.windows.size===0);return {ok:true};
            })()''')
            assert opened['ok'];wait(lambda:created[0][2].closed)
            assert not slave.ports
            # 新接入可立即打开；随后模拟未结束的输入回执，关闭不应再等其60s超时。
            result=_cdp_eval(endpoint,'',r'''(async()=>{
              document.getElementById('window-serial').click();await SerialIntegration.context.refresh();
              await closeWait(()=>!document.getElementById('serial-open').disabled);
              document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();
              await closeWait(()=>SerialIntegration.windows.size===1);
              const w=[...SerialIntegration.windows.values()][0],original=window.callWS;
              window.closeWritePending=false;
              window.callWS=(type,payload,...rest)=>{
                if(type==='extension-tool'&&payload.action==='write'){closeWritePending=true;return new Promise(()=>{});}
                return original(type,payload,...rest);
              };
              try {
                w.panel.querySelector('.terminal-cmd-textarea').value='UNSENT';w.panel.querySelector('.serial-send').click();
                await closeWait(()=>closeWritePending);
                await Promise.race([w.close(),new Promise((_,reject)=>setTimeout(()=>reject(Error('关闭仍等待输入链')),3000))]);
                return {ok:SerialIntegration.windows.size===0};
              } finally {window.callWS=original;}
            })()''')
            assert result['ok'];wait(lambda:created[1][2].closed)
            assert created[1][2].writes==[],'关闭不得重放未发送的排队输入'
            print('  [PASS] 真实共享目录远程GUI关闭：显示清理异常/输入待确认不阻止释放，从机端可立即重开且无输入重放')
        finally:
            manager.relay=original
            harness.stop();slave.close()
            _cdp_eval(endpoint,'',"(async()=>{SerialIntegration.context.select.value='computer:local';await SerialIntegration.context.choose();return true;})()")
