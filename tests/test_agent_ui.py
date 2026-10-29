"""实际 Edge 回归：权限入口、电脑选择和 PowerShell 终端交互。"""
import sys
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_ui_e2e as base
from windows_executor import WindowsService
import relay_node
from tests.test_relay_app import _Harness


def check_computer(ws_url, url, manager):
    previous_windows = manager.windows
    previous_permission = manager.relay.config.allowWindows
    previous_relay = manager.relay
    previous_config_path = manager.relay.config_path
    # 受限宿主的ConPTY中断另有兼容限制；测试使用WinPTY，仍保留原Ctrl+C断言。
    with tempfile.TemporaryDirectory(prefix='DevMux-ui-computer-', dir=ROOT) as directory, \
            patch.dict(os.environ, {'PYWINPTY_BACKEND': '1'}):
        manager.relay.config_path = str(Path(directory)/'local-relay.json')
        manager.windows = WindowsService(directory)
        manager.relay.config.allowWindows = True
        harness = _Harness(str(Path(directory)/'nodes'), master_local_devices_enabled=False)
        remote_windows = WindowsService(Path(directory)/'remote-jobs')
        harness.master.config.allowWindows = True
        harness.slave.config.allowWindows = True
        harness.slave.local_devices_enabled = False
        harness.slave.windows_service = remote_windows
        opened_remotely = []
        original_open = remote_windows.session_open
        def remote_open(args, owner):
            opened_remotely.append(owner)
            return original_open(args, owner)
        remote_windows.session_open = remote_open
        try:
            result = base._cdp_eval(ws_url, url, r"""
            (async () => {
              const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
              let step=0;
              const wait = async predicate => {
                step++;
                for(let i=0;i<200;i++){if(predicate())return;await delay(50);}
                const t=Object.values(state.terminals).find(t=>t.computer&&!t.serialPort);
                const b=t?.term.buffer.active;
                const lines=b?Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)).join('\n'):'';
                throw new Error('电脑工具页面等待超时，步骤 '+step+'；'+JSON.stringify({cols:t?.term.cols,rows:t?.term.rows,width:t?.xtermDiv.clientWidth,height:t?.xtermDiv.clientHeight,tab:state.currentTab,layout:state.layout})+'；'+lines.slice(-6500));
              };
              await wait(() => document.getElementById('window-powershell') && ws && ws.readyState === 1);
              document.getElementById('window-powershell').click();
              await wait(() => document.getElementById('powershell-target').options.length > 0);
              const shortName = document.getElementById('window-powershell').textContent.trim() === 'PowerShell…';
              const noTasks = !document.getElementById('tools-computer') && !document.getElementById('computer-job');
              const permission = document.getElementById('powershell-allow-local');
              await wait(() => !permission.disabled && !document.getElementById('powershell-open').disabled);
              const permissionVisible = permission.checked && !!permission.closest('#powershell-select-overlay')
                && permission.getBoundingClientRect().width > 0 && !document.getElementById('relay-allow-windows');
              permission.click();
              await wait(() => !permission.disabled && !permission.checked && document.getElementById('powershell-open').disabled
                && document.getElementById('powershell-permission-status').textContent.includes('本机已关闭'));
              document.getElementById('powershell-select-close').click();
              document.getElementById('window-powershell').click();
              await wait(() => !permission.disabled && !permission.checked);
              permission.click();
              await wait(() => !document.getElementById('powershell-open').disabled);
              document.getElementById('powershell-open').click();
              await wait(() => document.querySelector('.powershell-terminal-host .xterm-helper-textarea'));
              const overlay = document.querySelector('.powershell-terminal-panel');
              const beforeLayout = state.layout;
              setLayout('single');
              const independentLayout = [...document.querySelectorAll('#terminal-container > .terminal-panel')].some(panel=>panel.style.display!=='none');
              // 长工作树路径的提示符在窄列下触发系统旧 PSReadLine 越界，交互先使用单屏。
              const terminal = overlay.querySelector('.powershell-terminal-host');
              const area = terminal.querySelector('.xterm-helper-textarea');
              // 命令结果可能已滚出可视区，检查真实终端历史而非DOM当前几行。
              const text = () => {
                const buffer=state.terminals[overlay.dataset.key].term.buffer.active;
                return Array.from({length:buffer.length},(_,i)=>buffer.getLine(i)?.translateToString(true)).join('\n');
              };
              const input = value => {
                area.focus();
                area.value = value;
                area.dispatchEvent(new InputEvent('input', {data:value, inputType:'insertText', bubbles:true}));
              };
              await wait(() => text().includes('PS '));
              input('$uiTerminal=19\r');
              input("[Console]::WriteLine('TERMINAL='+($uiTerminal+1))\r");
              await wait(() => text().includes('TERMINAL=20'));
              input('Start-Sleep 60\r');
              await delay(300);
              area.focus();
              area.dispatchEvent(new KeyboardEvent('keydown', {key:'c', code:'KeyC', keyCode:67,
                which:67, ctrlKey:true, bubbles:true, cancelable:true}));
              await delay(300);
              input("[Console]::WriteLine('AFTER_'+(10+1))\r");
              await wait(() => text().includes('AFTER_11'));
              setLayout(beforeLayout);
              const rect = overlay.getBoundingClientRect();
              const embedded = overlay.parentElement.id === 'terminal-container'
                && !!document.querySelector('#device-tabs .device-tab[data-key="'+overlay.dataset.key+'"]');
              handleWSMessage({type:'devices-updated', devices:state.devices, knownKeys:state.knownKeys});
              const survivedBroadcast = document.querySelector('.powershell-terminal-panel') === overlay;
              overlay.querySelector('.minimize').click();
              const minimized = overlay.classList.contains('minimized');
              overlay.querySelector('.minimize').click();
              overlay.querySelector('.maximize').click();
              const maximized = state.layout === 'single' && overlay.style.display !== 'none';
              overlay.querySelector('.maximize').click();
              const noInterruptButton = !overlay.querySelector('.powershell-interrupt');
              input('Start-Sleep 60\r');
              await delay(100);
              await overlay.querySelector('.powershell-terminal-close').onclick();
              await wait(() => !document.querySelector('.powershell-terminal-panel'));
              return JSON.stringify({ok: shortName && noTasks && permissionVisible && embedded && independentLayout && survivedBroadcast && minimized && maximized && noInterruptButton
                && rect.width <= innerWidth && rect.height <= innerHeight, width:rect.width, height:rect.height,
                shortName, noTasks, permissionVisible, embedded, survivedBroadcast, minimized, maximized, noInterruptButton, interactive:true, interrupted:true});
            })()
            """)
            if isinstance(result, str):
                result = json.loads(result)
            result['shellClosed'] = not manager.windows.sessions
            result['ok'] = result.get('ok') and result['shellClosed']
            print(f'  [{"PASS" if result.get("ok") else "FAIL"}] 命令行工具 UI：真实 PowerShell 交互、Ctrl+C、权限及主终端布局 ({result})')
            with patch.object(relay_node, 'list_devices', return_value=([], '')):
                harness.start()
                harness.confirm_both()
                manager.relay = harness.master
                expression = "(async () => { const chosen="+json.dumps('computer:'+harness.slave.config.nodeId)+";"+r"""
                  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
                  let step=0;
                  const wait = async predicate => {
                    step++;
                    for(let i=0;i<240;i++){if(predicate())return;await delay(50);}
                    const t=Object.values(state.terminals).find(t=>t.computer&&!t.serialPort);
                    const b=t?.term.buffer.active;
                    const lines=b?Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)).join('\n'):'';
                    throw new Error('从机终端等待超时，步骤 '+step+'；'+document.querySelector('.powershell-terminal-status')?.textContent+'；'+lines.slice(-3000));
                  };
                  await wait(() => document.getElementById('window-powershell') && ws && ws.readyState === 1);
                  document.getElementById('window-powershell').click();
                  const select = document.getElementById('powershell-target');
                  await wait(() => [...select.options].some(option => option.value === chosen));
                  select.value = chosen;
                  select.dispatchEvent(new Event('change', {bubbles:true}));
                  await wait(() => !document.getElementById('powershell-open').disabled);
                  document.getElementById('powershell-open').click();
                  await wait(() => document.querySelector('.powershell-terminal-host .xterm-helper-textarea'));
                  const overlay = document.querySelector('.powershell-terminal-panel');
                  const text = () => {
                    const buffer=state.terminals[overlay.dataset.key].term.buffer.active;
                    return Array.from({length:buffer.length},(_,i)=>buffer.getLine(i)?.translateToString(true)).join('\n');
                  };
                  const previousLayout=state.layout;
                  setLayout('single');
                  const area = overlay.querySelector('.xterm-helper-textarea');
                  const input = value => {area.focus(); area.value=value;
                    area.dispatchEvent(new InputEvent('input', {data:value,inputType:'insertText',bubbles:true}));};
                  await wait(() => text().includes('PS '));
                  input('$remoteUI=41\r');
                  input("[Console]::WriteLine('REMOTE_UI='+($remoteUI+1))\r");
                  await wait(() => text().includes('REMOTE_UI=42'));
                  const title = overlay.querySelector('.terminal-panel-header').textContent;
                  const latency = [];
                  for(let i=0;i<5;i++){
                    const started = performance.now();
                    input("[Console]::WriteLine('LATENCY='+"+(100+i)+")\r");
                    await wait(() => text().includes('LATENCY='+(100+i)));
                    latency.push(Math.round(performance.now()-started));
                  }
                  await overlay.querySelector('.powershell-terminal-close').onclick();
                  setLayout(previousLayout);
                  return {ok:title.includes('pc-b') && title.includes('远程从机'), title, latencyMs:latency};
                })()
                """
                remote = base._cdp_eval(ws_url, url, expression)
                remote_ok = remote.get('ok') and not remote_windows.sessions and opened_remotely == [harness.master.config.nodeId+':'+
                    harness.slave.node.pairing.get(harness.master.config.nodeId).connectionId]
                print(f'  [{"PASS" if remote_ok else "FAIL"}] 界面选择从机并经真实中转建立 PowerShell 交互，未回退本机 ({remote})')
                return result.get('ok', False) and remote_ok
        finally:
            harness.stop()
            remote_windows.close()
            manager.windows.close()
            manager.windows = previous_windows
            manager.relay = previous_relay
            manager.relay.config_path = previous_config_path
            manager.relay.config.allowWindows = previous_permission


def run():
    original = base._check_live_relay
    def augmented(ws_url, url, manager):
        existing_ok = original(ws_url, url, manager)
        computer_ok = check_computer(ws_url, url, manager)
        from tests.test_usb_relay import check_ui
        usb_ok = check_ui(ws_url, url, manager)
        from tests.test_extension_tools import check_ui as check_remote_ui
        remote_usb_ok = check_remote_ui(ws_url, url, manager)
        from tests.test_db_flash_remote import check_ui as check_db_ui
        db_ok = check_db_ui(ws_url, url, manager)
        return existing_ok and computer_ok and usb_ok and remote_usb_ok and db_ok
    base._check_live_relay = augmented
    try:
        return base.run()
    finally:
        base._check_live_relay = original


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
