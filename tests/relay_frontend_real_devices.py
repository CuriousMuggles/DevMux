"""两台真实 ADB 设备的双实例前端验收；--frozen 验证发布版，不进入无设备 CI。"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_ui_e2e import _cdp_eval, _edge_executable
from tests.edge_test_support import EdgeSession


def free_port(used):
    while True:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if port not in used:
            used.add(port)
            return port


def wait_for(predicate, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, TimeoutError, ValueError):
            pass
        time.sleep(0.2)
    raise TimeoutError('等待服务或页面超时')


def evaluation(target, expression):
    value = _cdp_eval(target['webSocketDebuggerUrl'], target['url'], expression)
    print(json.dumps(value, ensure_ascii=False), flush=True)
    if not isinstance(value, dict) or not value.get('ok'):
        raise AssertionError(value)
    return value


def main():
    edge = _edge_executable()
    if not edge:
        raise RuntimeError('未找到 Edge')
    output = subprocess.check_output(['adb', 'devices', '-l'], text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    serials = [parts[0] for line in output.splitlines() if len(parts := line.split()) >= 2 and parts[1] == 'device']
    if len(serials) != 2:
        raise RuntimeError(f'需要恰好两台在线设备，当前 {serials!r}')
    print('在线设备：' + ', '.join(serials), flush=True)
    frozen = '--frozen' in sys.argv
    test_dir = Path(tempfile.mkdtemp(prefix='adb-relay-ui-real-')).resolve()
    share = test_dir / 'share'
    share.mkdir()
    used = set()
    processes = []
    edge_sessions = []
    handles = []
    try:
        urls = []
        for name, mask in [('master', True), ('slave', False)]:
            http_port, ws_port = free_port(used), free_port(used)
            env = os.environ.copy()
            env.update({'HTTP_PORT': str(http_port), 'WS_PORT': str(ws_port),
                        'RELAY_CONFIG': str(test_dir / f'{name}-relay.json'),
                        'ADB_MANAGER_HEADLESS': '1', 'ADB_MANAGER_NO_LOCAL_DEVICES': '1' if mask else '0',
                        'PYTHONIOENCODING': 'utf-8'})
            handle = open(test_dir / f'{name}.log', 'w', encoding='utf-8')
            handles.append(handle)
            if frozen:
                source_exe = ROOT / 'dist' / 'DevMux.exe'
                app_exe = test_dir / f'DevMux-{name}.exe'
                shutil.copyfile(source_exe, app_exe)
                command = [str(app_exe)]
            else:
                command = [sys.executable, '-B', 'server.py']
            proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                    stdout=handle, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            processes.append(proc)
            url = f'http://127.0.0.1:{http_port}/?_t={name}-real'
            urls.append(url)
            wait_for(lambda: urllib.request.urlopen(url, timeout=1).status == 200)
        targets_found = []
        for index, url in enumerate(urls):
            edge_proc = EdgeSession.start(edge, [
                '--headless=old', '--disable-background-timer-throttling',
                '--no-first-run', '--no-default-browser-check', '--remote-allow-origins=*',
                '--window-size=1280,900', url,
            ], test_dir / f'edge-profile-{index}')
            edge_sessions.append(edge_proc)
            targets_found.append({'webSocketDebuggerUrl': edge_proc.wait_ready(20, expected_url=url), 'url': url})
        master, slave = targets_found
        for name, target, role, deployment in [
            ('主控', master, 'master', ''), ('从机', slave, 'slave', 'test')]:
            print('页面配置：' + name, flush=True)
            expression = """(async () => {
              const sleep = ms => new Promise(r => setTimeout(r, ms));
              const until = async (fn, ms=30000) => {const end=Date.now()+ms; while(Date.now()<end){if(fn()) return; await sleep(100)} throw new Error('等待页面配置超时')};
              document.getElementById('tools-tab').click();
              document.getElementById('tools-relay').click();
              await until(() => state.relay.loaded && document.getElementById('relay-config-overlay').style.display === 'flex');
              const root=document.getElementById('relay-root'); root.value=SHARE; root.dispatchEvent(new Event('change',{bubbles:true}));
              document.getElementById('relay-enabled').checked=true;
              document.getElementById('relay-role').value=ROLE;
              if(DEPLOYMENT){
                document.getElementById('relay-add-deployment').click();
                document.getElementById('relay-deployment-name').value=DEPLOYMENT;
                document.getElementById('relay-deployment-add').click();
              }
              document.getElementById('relay-save').click();
              await until(() => document.getElementById('relay-root-choice-overlay').style.display === 'flex' ||
                (state.relay.status && state.relay.status.running && state.relay.status.role === ROLE));
              if(document.getElementById('relay-root-choice-overlay').style.display === 'flex')
                document.getElementById('relay-root-keep').click();
              await until(() => state.relay.status && state.relay.status.running && state.relay.status.role === ROLE);
              return {ok:true, role:state.relay.status.role, nodeId:state.relay.status.nodeId,
                deployment:state.relay.status.poolId, root:state.relay.status.root};
            })()""".replace('SHARE', json.dumps(str(share))).replace('ROLE', json.dumps(role)).replace('DEPLOYMENT', json.dumps(deployment))
            result = evaluation(target, expression)
            if result['deployment'] != deployment:
                raise AssertionError('部署未按页面选择生效')
        observe = """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); const end=Date.now()+30000;
          while(Date.now()<end){const p=(state.relay.status||{}).pairing||[];if(p.some(x=>x.online))
            return {ok:true, items:p.map(x=>({nodeId:x.nodeId,online:x.online,phase:x.connectionState})),
              tabs:document.querySelectorAll('#device-tabs .device-tab').length}; await sleep(100)}
          return {ok:false,reason:'未发现对端'};
        })()"""
        before_master = evaluation(master, observe)
        before_slave = evaluation(slave, observe)
        if before_master['tabs'] != 0:
            raise AssertionError('确认前主控端出现设备')
        if before_slave['tabs'] != 2:
            raise AssertionError('从机未展示本机两台设备')
        evaluation(slave, """({ok:document.getElementById('relay-master-connection').hidden
          && !document.getElementById('relay-slave-connection').hidden
          && document.getElementById('relay-pair').disabled
          && document.getElementById('relay-confirm-overlay').style.display !== 'flex',
          phase:relayVisibleState(state.relay.status)})""")
        print('页面主控确认，从机无需操作', flush=True)
        first = evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms));
          const select=document.getElementById('relay-pairing'); select.value=state.relay.status.pairing.find(x=>x.online).nodeId;
          select.dispatchEvent(new Event('change',{bubbles:true}));
          const button=document.getElementById('relay-pair'); button.scrollIntoView({block:'center'});
          const rect=button.getBoundingClientRect();
          if(document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2)!==button)
            return {ok:false,reason:'主控确认按钮被遮挡，不能按用户流程点击'};
          button.click();
          const end=Date.now()+30000;
          while(Date.now()<end){const p=state.relay.status.pairing.find(x=>x.nodeId===select.value);
            if(p&&p.localConfirmed&&p.connectionState==='connected')
              return {ok:document.getElementById('relay-step-use').classList.contains('active'),
                phase:p.connectionState, reason:p.connectionReason}; await sleep(100)}
          return {ok:false,reason:'主控确认后没有自动连接',pairing:state.relay.status.pairing};
        })()""")
        ready_master = evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); const end=Date.now()+40000;
          while(Date.now()<end){const s=state.relay.status||{};const p=(s.pairing||[]).find(x=>x.online);
            const keys=Object.keys(state.terminals).filter(k=>k.startsWith('relay:'));
            if(p&&p.connectionState==='connected'&&document.getElementById('relay-step-use').classList.contains('active')&&keys.length===2)
              return {ok:true,phase:p.connectionState,keys,serials:keys.map(k=>k.split(':')[2])};
            await sleep(100)}
          return {ok:false,reason:'主控未出现两台远端设备和终端',pairing:s.pairing,terminals:Object.keys(state.terminals)};
        })()""")
        ready_slave = evaluation(slave, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); const end=Date.now()+20000;
          while(Date.now()<end){const p=(state.relay.status||{}).pairing||[];
            if(p.some(x=>x.connectionState==='connected')) return {ok:true,phase:p[0].connectionState};
            await sleep(100)} return {ok:false,pairing:state.relay.status.pairing};
        })()""")
        if set(ready_master['serials']) != set(serials):
            raise AssertionError(f'远端设备与 ADB 在线设备不一致: {ready_master["serials"]} != {serials}')
        print('页面发送两台设备终端命令', flush=True)
        command = evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms));
          const keys=Object.keys(state.terminals).filter(k=>k.startsWith('relay:'));
          const expected=['333','777'];
          for(let i=0;i<keys.length;i++){
            const textarea=state.terminals[keys[i]].panel.querySelector('.terminal-cmd-textarea');
            textarea.value=i===0?'echo $((111+222))':'echo $((333+444))';
            textarea.dispatchEvent(new Event('input',{bubbles:true}));
            textarea.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
          }
          const end=Date.now()+30000;
          while(Date.now()<end){
            const seen=keys.map((k,i)=>{
              const b=state.terminals[k].term.buffer.active; const lines=[];
              for(let j=0;j<b.length;j++) lines.push(b.getLine(j)?.translateToString(true).trim());
              return lines.some(line=>line===expected[i]);
            });
            if(seen.every(Boolean)) return {ok:true,keys,seen};
            await sleep(200);
          }
          return {ok:false,reason:'未在两个前端终端看到实际回显',keys};
        })()""")
        print('页面主控解除连接', flush=True)
        evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms));
          document.getElementById('relay-unpair').click();
          const end=Date.now()+20000;
          while(Date.now()<end){const p=(state.relay.status||{}).pairing||[];
            if(p.length && !p[0].localConfirmed) return {ok:true,phase:p[0].connectionState};
            await sleep(100)}
          return {ok:false,reason:'主控解除未生效'};
        })()""")
        evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); const end=Date.now()+30000;
          while(Date.now()<end){const p=(state.relay.status||{}).pairing||[];
            if(p.length && p[0].connectionState!=='connected' &&
              Object.keys(state.terminals).filter(k=>k.startsWith('relay:')).length===0)
              return {ok:true,phase:p[0].connectionState,remoteTerminals:0};
            await sleep(100)}
          return {ok:false,reason:'主控解除后未撤下远端终端',
            pairing:state.relay.status.pairing,terminals:Object.keys(state.terminals)};
        })()""")
        evaluation(slave, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); const end=Date.now()+20000;
          while(Date.now()<end){const s=state.relay.status||{};
            if(!s.pairing.some(x=>x.paired)) return {ok:document.getElementById('relay-master-connection').hidden,
              phase:relayVisibleState(s)}; await sleep(100)} return {ok:false,reason:'从机未恢复等待状态'};
        })()""")
        print('主控重新确认连接', flush=True)
        evaluation(master, """(async () => {
          const sleep=ms=>new Promise(r=>setTimeout(r,ms)); document.getElementById('relay-pair').click();
          const end=Date.now()+30000;
          while(Date.now()<end){const s=state.relay.status||{};
            if(s.pairing.some(x=>x.connectionState==='connected') && Object.keys(state.terminals).filter(k=>k.startsWith('relay:')).length===2)
              return {ok:true,phase:'connected',remoteTerminals:2}; await sleep(100)}
          return {ok:false,reason:'重新确认未自动恢复两台远端设备'};
        })()""")
        print('双实例前端真实设备验收通过', flush=True)
        return True
    finally:
        for edge_proc in reversed(edge_sessions):
            edge_proc.close()
        for proc in reversed(processes):
            if proc.poll() is None:
                # PyInstaller 单文件与 Edge 会创建子进程，只清理本测试创建的进程树。
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try: proc.wait(timeout=8)
                except subprocess.TimeoutExpired: proc.kill()
        for handle in handles:
            handle.close()
        # 仅删除本次脚本创建的临时目录。
        temp_root = Path(tempfile.gettempdir()).resolve()
        if test_dir.parent == temp_root and test_dir.name.startswith('adb-relay-ui-real-'):
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    try: sys.exit(0 if main() else 1)
    except Exception as exc:
        print(f'[FAIL] {type(exc).__name__}: {exc}', flush=True)
        raise
