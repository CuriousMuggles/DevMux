"""独立模拟服务的真实 WebSocket + Edge 界面回归，不连接开发板。"""
import base64
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import SPECS
from tests.edge_test_support import EdgeSession


def wait_until(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, StopIteration):
            pass
        time.sleep(.1)
    raise RuntimeError('等待模拟测试环境超时')


def get_json(url):
    with urllib.request.urlopen(url, timeout=1) as response:
        return json.loads(response.read().decode())


def run():
    from websockets.sync.client import connect
    edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.is_file():
        print('  [SKIP] 未找到 Edge，未验证模拟界面')
        return None
    runtime = ROOT / '.agent-runtime/db-flash'
    runtime.mkdir(parents=True, exist_ok=True)
    app_mode = os.environ.get('DB_FLASH_TEST_MAIN_APP') == '1'
    http_port = int(os.environ.get('DB_FLASH_TEST_HTTP_PORT', '8182' if app_mode else '8180'))
    ws_port = int(os.environ.get('DB_FLASH_TEST_WS_PORT', '8867' if app_mode else '8865'))
    for port in (http_port, ws_port):
        with socket.socket() as test_socket:
            try:
                test_socket.bind(('127.0.0.1', port))
            except OSError:
                print(f'  [FAIL] 模拟测试端口 {port} 已占用；未停止任何现有程序')
                return False
    backend = browser = None
    log = None
    profile = None
    try:
        with tempfile.TemporaryDirectory(prefix='ui-fixture-', dir=runtime) as temp, contextlib.ExitStack() as cleanup:
            fixture = Path(temp) / 'DB 工具 中文'
            for name in ['flashall.bat', 'flash_all.sh', 'scripts/download_image.py',
                         'images/u-boot/u-boot.bin'] + [s.default for s in SPECS]:
                file = fixture / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'simulation only')
            outside = Path(temp) / 'selected'
            outside.mkdir()
            first = outside / 'p500_SYSTEM_2026.img'
            second = outside / 'system_new.img'
            first.write_bytes(b'first simulation image')
            second.write_bytes(b'second simulation image')
            boot_old = outside / 'boot-ori.itb'
            boot_new = outside / 'boot.itb'
            preboot = outside / 'preboot.itb'
            for file in (boot_old, boot_new, preboot):
                file.write_bytes(b'simulation image')
            snapshot = {p.relative_to(fixture).as_posix(): p.read_bytes() for p in fixture.rglob('*') if p.is_file()}
            p300 = Path(temp) / 'p300 udp 工具'
            p300_files = {
                'flashall.bat': (b'python scripts/download_image.py -i images/spl/spl_im_burn_signed.img '
                                 b'-m bootrom_download -d 0 -p 2 -v 0x696d -u 0x7633\r\n'
                                 b'fastboot getvar is-secure-chip\r\n'),
                'flash_cfg.ini': b'[UsbInfo]\r\nUSBID_VID=0x1234,0x696D,0x3902\r\n',
                'scripts/download_image.py': b'simulation only',
                'images/spl/spl_im_burn_signed.img': b'simulation only',
                'images/spl/spl_im_signed.img': b'simulation only',
                'images/spl/spl_im_signed.img.pro.enc': b'simulation only',
                'images/gpt/gpt.bin': b'simulation only',
                'images/u-boot/u-boot.bin': b'simulation only',
                'images/u-boot/uboot_env.img': b'simulation only',
                'images/lpddr_fw/lpddr5_fw.bin': b'simulation only',
                'images/preboot.itb': b'simulation only',
                'images/preboot_sign.itb': b'simulation only',
                'images/boot.itb': b'simulation only',
                'images/rootfs.img': b'simulation only',
                'images/userdata.img': b'simulation only',
            }
            for name, content in p300_files.items():
                file = p300 / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
            snapshot_p300 = {p.relative_to(p300).as_posix(): p.read_bytes() for p in p300.rglob('*') if p.is_file()}
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
                   'HTTP_PORT': str(http_port), 'WS_PORT': str(ws_port), 'ADB_MANAGER_NO_LOCAL_DEVICES': '1',
                   'RELAY_CONFIG': str(runtime / 'relay.json'), 'ADB_AGENT_RUNTIME_DIR': str(runtime)}
            log = (runtime / 'preview-test.log').open('w', encoding='utf-8')
            if app_mode:
                env['DB_FLASH_TEST_TOOL_DIR'] = str(fixture.resolve())
                env['DB_FLASH_TEST_PICK_FILES'] = json.dumps([str(first.resolve()), str(second.resolve())])
                config_path = Path(env['RELAY_CONFIG'])
                config_path.write_text('{"enabled": false}', encoding='utf-8')
                command_line = [sys.executable, '-B', str(ROOT / 'tests/db_flash_app_harness.py')]
            else:
                command_line = [sys.executable, '-B', str(ROOT / 'db_flash_preview.py'),
                                '--http-port', str(http_port), '--ws-port', str(ws_port)]
            backend = subprocess.Popen(command_line,
                                       cwd=str(ROOT), env=env, stdout=log, stderr=log, stdin=subprocess.PIPE,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            wait_until(lambda: urllib.request.urlopen(f'http://127.0.0.1:{http_port}/', timeout=1).close() or True)
            # 无 Origin 的客户端必须被拒绝，不能成为任意网站的文件操作通道。
            with connect(f'ws://127.0.0.1:{ws_port}', proxy=None) as denied:
                if app_mode:
                    denied.send(json.dumps({'type': 'db-flash', 'id': 90001, 'action': 'status'}))
                    while True:
                        denial = json.loads(denied.recv(timeout=2))
                        if denial.get('id') == 90001:
                            assert not denial['success']
                            break
                else:
                    try:
                        denied.recv(timeout=2)
                        raise AssertionError('缺少 Origin 的连接未被拒绝')
                    except __import__('websockets').exceptions.ConnectionClosed as exc:
                        assert exc.rcvd and exc.rcvd.code == 1008
            profile = runtime / ('edge-db-flash-' + str(os.getpid()))
            browser = EdgeSession.start(edge, ['--headless=new', '--no-first-run',
                    '--no-default-browser-check', '--hide-scrollbars', '--remote-allow-origins=*',
                    '--window-size=1180,1100', f'http://127.0.0.1:{http_port}/'], profile)
            cleanup.callback(browser.close)
            ws_url = browser.wait_ready(20)
            with connect(ws_url, proxy=None, max_size=12 * 1024 * 1024) as cdp:
                counter = 0

                def command(method, params=None):
                    nonlocal counter
                    counter += 1
                    cdp.send(json.dumps({'id': counter, 'method': method, 'params': params or {}}))
                    while True:
                        message = json.loads(cdp.recv(timeout=25))
                        if message.get('id') == counter:
                            if message.get('error'):
                                raise RuntimeError(message['error'])
                            return message['result']

                def evaluate(code):
                    result = command('Runtime.evaluate', {'expression': code, 'returnByValue': True, 'awaitPromise': True})
                    if result.get('exceptionDetails'):
                        raise RuntimeError(result['exceptionDetails'])
                    return result['result'].get('value')

                if app_mode:
                    wait_until(lambda: evaluate("Boolean(window.DBFlashIntegration)"))
                    wait_until(lambda: evaluate("typeof ws !== 'undefined' && ws.readyState === WebSocket.OPEN"))
                    evaluate("window.dbFlashPreview=window.DBFlashIntegration.panel; document.getElementById('tools-tab').click(); document.getElementById('tools-db-flash').click()")
                    assert evaluate("!document.getElementById('db-flash-overlay').hidden"), '主菜单未打开烧写面板'
                    assert evaluate("document.body.style.overflow !== 'hidden'"), '浮动工具不应额外锁定背景'
                    wait_until(lambda: evaluate("window.DBFlashIntegration.panel.context.ready"))
                    print('  [PASS] 主应用工具菜单打开独立文件表格弹窗')
                wait_until(lambda: evaluate("Boolean(window.dbFlashPreview)"))
                # 等待独立传输就绪。
                wait_until(lambda: evaluate("window.dbFlashPreview.request('db-flash',{action:'status',arguments:{}}).then(x=>x.success).catch(()=>false)"))
                code = r"""
(async () => {
 const p = window.dbFlashPreview;
 const wait = async check => {
   const end = Date.now()+12000;
   while (Date.now()<end) { if (check()) return; await new Promise(r=>setTimeout(r,30)); }
   throw new Error('界面状态等待超时');
 };
 const assert = (value,text) => { if(!value) throw new Error(text); };
 p.el('directory').value = FIXTURE;
 p.el('confirm').click();
 await wait(()=>!p.busy && p.model?.toolValid);
 assert(p.el('message').textContent.includes('已自动识别：p500'),'未提示自动识别平台: '+p.el('message').textContent);
 assert(p.model.items.filter(x=>x.checked).length===7,'默认 ALL 未全选');
 assert(p.el('data-mode').value==='erase','默认 ALL 必须擦除而非烧写 userdata');
 const standardNames=['spl_im_signed.img','gpt.bin','preboot.itb','boot.itb','rootfs.img','system.img','userdata.img'];
 const rows=[...p.host.querySelectorAll('.db-flash-item')];
 assert(rows.length===7,'缺少烧写文件说明');
 assert(p.el('items').tagName==='TBODY','文件清单不是表格');
 assert([...p.host.querySelectorAll('.db-flash-table thead th')].map(x=>x.textContent).join('|')==='勾选|标准文件名|功能说明|实际烧写文件','表格列顺序不正确');
 rows.forEach((row,index)=>{
   assert(row.querySelector('.db-flash-standard').textContent===standardNames[index],'标准文件名不正确');
   const description=row.querySelector('.db-flash-description');
   assert(description.textContent.length>8 && description.getBoundingClientRect().height>0,'功能介绍不可见');
   const nameRect=row.querySelector('.db-flash-standard').getBoundingClientRect();
   assert(description.getBoundingClientRect().left>nameRect.right,'功能说明不在文件名后面的独立列');
 });
 assert(p.el('prepare').closest('.db-flash-option').textContent.includes('u-boot.bin'),'临时准备缺少配套文件名');
 assert(p.el('erase-env').closest('.db-flash-option').textContent.includes('env 分区'),'擦除启动设置缺少操作说明');
 assert(p.el('data-mode-help').textContent.includes('不使用 userdata.img'),'默认数据擦除说明不明确');
 p.el('plan').click(); await wait(()=>!p.busy);
 assert(p.el('steps').textContent.includes('gpt_resize'),'ALL 缺少 GPT 调整');
 assert(!p.el('steps').textContent.includes('flash-data'),'ALL 意外烧写 data');
 // 两次批量选择同一项时，第二批替换旧候选，保留其他项。
 const originalPick=p.pick;
 let picked=[BOOT_OLD,PREBOOT];
 p.pick=async()=>({success:true,paths:picked});
 p.el('pick').click(); await wait(()=>!p.busy);
 assert(p.model.items.find(x=>x.id==='boot').candidates.length===1,'首次 boot 候选异常');
 picked=[BOOT_NEW];
 p.el('pick').click(); await wait(()=>!p.busy);
 assert(p.files.includes(BOOT_NEW) && p.files.includes(PREBOOT) && !p.files.includes(BOOT_OLD),'新 boot 未替换旧候选或丢失 preboot');
 assert(p.model.items.find(x=>x.id==='boot').candidates.length===1,'分批选择仍提示多个 boot 候选');
 p.el('directory').dispatchEvent(new Event('input'));
 assert(p.files.length===0,'目录变更后仍保留旧文件选择');
 p.el('confirm').click(); await wait(()=>!p.busy && p.model?.toolValid);
 picked=[BOOT_NEW,PREBOOT];
 p.el('pick').click(); await wait(()=>!p.busy);
 assert(p.model.items.find(x=>x.id==='boot').candidates.length===1 &&
   p.model.items.find(x=>x.id==='preboot').candidates.length===1,'boot/preboot 同批次误判重复候选');
 assert([...p.host.querySelector('[data-file="boot"]').options].map(x=>x.value).join(',')===BOOT_NEW,
   'boot 下拉框仍显示未选择的默认文件');
 assert([...p.host.querySelector('[data-file="preboot"]').options].map(x=>x.value).join(',')===PREBOOT,
   'preboot 下拉框仍显示未选择的默认文件');
 p.el('defaults').click(); await wait(()=>!p.busy);
 p.pick=originalPick;
 // 修改默认 ALL；必须保留手动勾选。
 const boot = p.host.querySelector('[data-item="boot"]');
 boot.click();
 assert(p.el('all').indeterminate && !p.model.items.find(x=>x.id==='boot').checked,'ALL 不能手动取消单项');
 const nativeRequest = p.request;
 p.request = async (type,args) => type==='pick-file'
   ? {success:true,paths:FILES,path:FILES[0]} : nativeRequest(type,args);
 p.el('pick').click(); await wait(()=>!p.busy);
 assert(p.model.items.filter(x=>x.checked).map(x=>x.id).join(',')==='system','批量文件未正确自动勾选');
 assert(!p.el('erase-env').checked && !p.el('reboot').checked,'局部烧写意外启用擦除或重启');
 p.el('plan').click(); await wait(()=>!p.busy);
 assert(p.el('message').textContent.includes('多个候选'),'重复候选未阻止计划');
 const select=p.host.querySelector('[data-file="system"]');
 select.value=FILES[1]; select.dispatchEvent(new Event('change'));
 // 手动补选默认 rootfs，同时验证计划自动遵循原脚本顺序。
 p.host.querySelector('[data-item="rootfs"]').click();
 p.el('plan').click(); await wait(()=>!p.busy);
 assert(p.el('steps').textContent.includes(FILES[1].replaceAll('\\','\\\\')),'计划没有使用选定的外部路径');
 assert(p.el('steps').children.length===5,'单独镜像计划包含额外烧写项');
 p.el('start').click(); await wait(()=>!p.busy && !p.running);
 assert(p.el('message').textContent.includes('未进行硬件验证'),'模拟成功结果不明确');
 assert(p.el('progress').value===p.el('progress').max,'成功后进度不完整');
 const originalLog=p.el('log').textContent;
 p.acceptProgress({jobId:p.jobId,sequence:p.sequence+1,kind:'output',text:'连续'});
 p.acceptProgress({jobId:p.jobId,sequence:p.sequence+1,kind:'output',text:'打印\n'});
 assert(p.el('log').textContent.endsWith('连续打印\n'),'输出片段被额外换行拆开');
 p.el('log').textContent=originalLog;
 // ALL 可全取消、再全选。
 p.el('all').checked=false; p.el('all').dispatchEvent(new Event('change'));
 assert(p.model.items.every(x=>!x.checked),'取消 ALL 未取消所有项');
 p.el('all').checked=true; p.el('all').dispatchEvent(new Event('change'));
 assert(p.model.items.every(x=>x.checked),'勾选 ALL 未补选所有项');
 p.el('defaults').click(); await wait(()=>!p.busy);
 assert(p.files.length===0 && p.model.items.every(x=>x.checked),'恢复默认失败');
 p.el('start').click(); await wait(()=>!p.busy && p.running);
 p.el('cancel').click(); await wait(()=>!p.busy && !p.running);
 assert(p.el('message').textContent.includes('取消'),'取消模拟未结束作业');
 p.close(); assert(p.host.hidden,'关闭面板失败'); p.open(); assert(!p.host.hidden,'重新打开面板失败');
 const overflow=()=>document.documentElement.scrollWidth>window.innerWidth;
 assert(!overflow(),'桌面宽度溢出');
 return {ok:true,checked:p.model.items.filter(x=>x.checked).length,log:p.el('message').textContent};
})()
""".replace('FIXTURE', json.dumps(str(fixture.resolve()), ensure_ascii=False)).replace('FILES', json.dumps([str(first.resolve()), str(second.resolve())], ensure_ascii=False)).replace('BOOT_OLD', json.dumps(str(boot_old.resolve()), ensure_ascii=False)).replace('BOOT_NEW', json.dumps(str(boot_new.resolve()), ensure_ascii=False)).replace('PREBOOT', json.dumps(str(preboot.resolve()), ensure_ascii=False))
                if app_mode:
                    code = code.replace(" const nativeRequest = p.request;\n p.request = async (type,args) => type==='pick-file'\n   ? {success:true,paths:FILES,path:FILES[0]} : nativeRequest(type,args);"
                        .replace('FILES', json.dumps([str(first.resolve()), str(second.resolve())], ensure_ascii=False)), '')
                result = evaluate(code)
                assert result['ok']
                print('  [PASS] 原生选择结果注入 → 后端识别 → 手动修改 → 真实 WS 模拟、取消、关闭重开')
                p300_code = r"""
(async () => {
 const p=window.dbFlashPreview;
 const wait=async check=>{const end=Date.now()+12000;while(Date.now()<end){if(check())return;await new Promise(r=>setTimeout(r,30));}throw new Error('p300-udp 界面等待超时');};
 const assert=(value,text)=>{if(!value)throw new Error(text);};
 await wait(()=>!p.busy && !p.el('confirm').disabled);
 p.el('directory').value=P300;
 p.el('confirm').click();
 await wait(()=>!p.busy && p.model?.platform==='p300-udp' && p.model?.toolValid);
 assert(p.model.platform==='p300-udp','未识别 p300-udp 平台');
 assert(p.el('platform').textContent.includes('p300-udp'),'界面未显示识别平台');
 assert(p.el('message').textContent.includes('已自动识别：p300-udp'),'未提示自动识别平台: '+p.el('message').textContent);
 assert(p.model.items.length===8,'p300-udp 镜像项数量不正确');
 assert(p.model.items.filter(x=>x.checked).length===8,'p300-udp 默认 ALL 应仅包含支持的镜像项');
 assert(!p.model.items.some(x=>x.id==='system'),'p300-udp 不应显示 system.img');
 assert(p.el('data-mode').value==='format','p300-udp 默认应格式化 data');
 assert(p.el('erase-env-row').hidden,'p300-udp 不应显示旧平台擦除 env');
 assert(!p.el('secure-chip-row').hidden && p.el('secure-chip').value==='auto','安全芯片选项未显示');
 assert(p.el('prepare-files').textContent.includes('spl_im_burn_signed.img'),'准备文件未按 p300-udp 显示');
 const names=[...p.host.querySelectorAll('.db-flash-standard')].map(x=>x.textContent);
 assert(names.join(',')==='spl_im_signed.img,gpt.bin,uboot_env.img,lpddr5_fw.bin,preboot.itb,boot.itb,rootfs.img,userdata.img','p300-udp 标准文件名不正确');
 p.el('plan').click(); await wait(()=>!p.busy);
 const text=p.el('steps').textContent;
 assert(text.includes('0x696d') && text.includes('is-secure-chip') && text.includes('gpt_resize'),'p300-udp 计划缺少下载/安全芯片/分区调整步骤');
 assert(text.includes('格式化用户数据'),'p300-udp 计划缺少 data 格式化步骤');
 p.el('start').click(); await wait(()=>!p.busy && !p.running);
 assert(p.el('message').textContent.includes('未进行硬件验证'),'p300-udp 模拟未完成');
 p.el('defaults').click(); await wait(()=>!p.busy);
 p.el('all').checked=true; p.el('all').dispatchEvent(new Event('change'));
 assert(p.model.items.filter(x=>x.checked).length===8,'ALL 未包含全部 8 项');
 p.el('all').checked=false; p.el('all').dispatchEvent(new Event('change'));
 assert(p.model.items.every(x=>!x.checked),'取消 ALL 未取消所有项');
 await wait(()=>!p.busy && !p.el('confirm').disabled);
 p.el('directory').value=FIXTURE;
 p.el('confirm').click();
 await wait(()=>!p.busy && p.model?.platform==='p500' && p.model?.toolValid);
 return {ok:true,platform:p.model.platform};
})()
""".replace('P300', json.dumps(str(p300.resolve()), ensure_ascii=False)).replace('FIXTURE', json.dumps(str(fixture.resolve()), ensure_ascii=False))
                result = evaluate(p300_code)
                assert result['ok']
                print('  [PASS] p300-udp 平台识别、安全芯片/数据格式选项、默认计划与 ALL')
                if app_mode:
                    result = evaluate(r'''
(async () => {
 const p=window.dbFlashPreview;
 const wait=async check=>{const end=Date.now()+15000;while(Date.now()<end){if(check())return;await new Promise(r=>setTimeout(r,30));}throw new Error('主应用等待超时');};
 const assert=(value,text)=>{if(!value)throw new Error(text);};
 // 浮动窗口允许焦点离开；Escape 只关闭当前工具并返回菜单。
 assert(document.getElementById('db-flash-overlay').getAttribute('aria-modal')==='false','烧写窗口仍为模态');
 const tabEvent=new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true});
 p.el('close').dispatchEvent(tabEvent);
 assert(!tabEvent.defaultPrevented,'浮动窗口不应锁定 Tab 焦点');
 p.el('directory').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert(document.getElementById('db-flash-overlay').hidden,'Escape 未关闭弹窗');
 assert(document.activeElement.id==='tools-tab','关闭后未恢复菜单焦点');
 document.getElementById('tools-db-flash').click();
 await wait(()=>p.context.ready && !p.context.loading);
 // 作业运行期间重启后端，旧作业取消，新服务仍能启动模拟。
 p.el('start').click();await wait(()=>!p.busy && p.running);
 const oldId=p.jobId;
 await p.request('restart-server',{});
 await wait(()=>!p.running);
 await wait(()=>typeof ws!=='undefined' && ws.readyState===WebSocket.OPEN);
 const status=await p.request('db-flash',{action:'status',arguments:{}});
 assert(status.success && status.data===null,'重启后模拟服务未重新初始化');
 p.el('start').click();await wait(()=>!p.busy && !p.running);
 assert(p.jobId!==oldId && p.el('message').textContent.includes('未进行硬件验证'),'重启后新作业不能完成');
 return true;
})()
''')
                    assert result
                    print('  [PASS] 浮动窗口焦点/Escape、作业中后端重启取消、重连后新模拟作业完成')
                    # 继电器和电脑请求全部在浏览器替换为假结果，不进入硬件接口。
                    evaluate(r'''
window.floatingTest = {original:callWS, operations:[], model:{toolDir:'',
  devices:[{serial:'TEST',channels:8}], states:Array(8).fill(false), opened:false,channels:8,serial:''}};
callWS = async (type,payload,...rest) => {
 const t=window.floatingTest;
 const target={target:'computer:local',local:true,online:true,label:'测试电脑',role:'master'};
 if(type==='extension-tool' && payload.tool==='relay') {
  if(payload.action==='targets')return {success:true,data:{targets:[target]}};
  if(payload.action==='capabilities')return {success:true,data:{allowed:true,remoteAllowed:false}};
  if(payload.action==='open'){t.model.opened=true;t.model.serial='TEST';}
  if(payload.action==='set'){t.operations.push(payload.arguments);t.model.states[payload.arguments.channel-1]=payload.arguments.operation==='open';}
  return {success:true,data:structuredClone(t.model),output:'测试操作完成'};
 }
 if(type==='computer-tool') {
  if(payload.name==='local_windows_permission')return {success:true,data:{allowed:true}};
  if(payload.name==='targets_list')return {success:true,data:{targets:[target]}};
  if(payload.name==='target_status')return {success:true,data:{allowed:true}};
  throw new Error('测试不允许创建电脑会话');
 }
 return t.original(type,payload,...rest);
};
''')
                    def rect(selector):
                        return evaluate("(()=>{const r=document.querySelector(" + json.dumps(selector) + ").getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()")

                    def mouse_drag(x, y, dx, dy):
                        command('Input.dispatchMouseEvent', {'type':'mousePressed','x':x,'y':y,'button':'left','buttons':1,'clickCount':1})
                        command('Input.dispatchMouseEvent', {'type':'mouseMoved','x':x+dx,'y':y+dy,'button':'left','buttons':1})
                        command('Input.dispatchMouseEvent', {'type':'mouseReleased','x':x+dx,'y':y+dy,'button':'left','buttons':0,'clickCount':1})

                    def mouse_click(selector):
                        bounds = rect(selector)
                        x, y = bounds['x']+bounds['width']/2, bounds['y']+bounds['height']/2
                        command('Input.dispatchMouseEvent', {'type':'mousePressed','x':x,'y':y,'button':'left','clickCount':1})
                        command('Input.dispatchMouseEvent', {'type':'mouseReleased','x':x,'y':y,'button':'left','clickCount':1})

                    # 真鼠标拖动标题栏和右下角，检查实际位置与尺寸，不只检查 CSS 字符串。
                    original = rect('#db-flash-overlay .tool-floating-window')
                    header = rect('#db-flash-overlay .tool-floating-header')
                    mouse_drag(header['x']+100, header['y']+15, 35, 20)
                    moved = rect('#db-flash-overlay .tool-floating-window')
                    assert moved['x'] >= original['x']+25 and moved['y'] >= original['y']+15, '烧写窗口无法拖动'
                    mouse_drag(moved['x']+moved['width']-4, moved['y']+moved['height']-4, -380, -80)
                    smaller = rect('#db-flash-overlay .tool-floating-window')
                    assert smaller['width'] < moved['width']-200 and smaller['height'] < moved['height']-40, '烧写窗口无法缩放'
                    mouse_click('#tools-tab'); mouse_click('#tools-usb-relay')
                    wait_until(lambda: evaluate("document.getElementById('usb-relay-overlay').getAttribute('aria-busy')==='false'"))
                    assert evaluate("!document.getElementById('db-flash-overlay').hidden && !document.getElementById('usb-relay-overlay').hidden"), '两个工具未同时保持打开'
                    relay = rect('#usb-relay-overlay .tool-floating-window')
                    mouse_drag(relay['x']+relay['width']-4, relay['y']+relay['height']-4, -100, -60)
                    relay_small = rect('#usb-relay-overlay .tool-floating-window')
                    assert relay_small['width'] < relay['width']-50, '继电器窗口无法缩放'
                    relay_header = rect('#usb-relay-overlay .tool-floating-header')
                    mouse_drag(relay_header['x']+100, relay_header['y']+15, 90, 15)
                    assert rect('#usb-relay-overlay .tool-floating-window')['x'] > relay_small['x']+40, '继电器窗口无法拖动'
                    mouse_click('#usb-relay-open')
                    wait_until(lambda: evaluate("document.querySelector('#usb-relay-connection-state').textContent.includes('已打开')"))
                    evaluate("window.dbFlashPreview.el('start').click()")
                    wait_until(lambda: evaluate("window.dbFlashPreview.running && !window.dbFlashPreview.busy"))
                    evaluate("document.querySelector('#usb-relay-channels [data-channel=\"1\"][data-operation=\"open\"]').scrollIntoView({block:'nearest'})")
                    mouse_click('#usb-relay-channels [data-channel="1"][data-operation="open"]')
                    wait_until(lambda: evaluate("window.floatingTest.operations.length===1"))
                    assert evaluate("window.dbFlashPreview.running && document.querySelector('#usb-relay-channels').firstElementChild.textContent.includes('已接通')"), '烧写运行时未能独立操作继电器'
                    mouse_click('#usb-relay-dismiss')
                    assert evaluate("window.dbFlashPreview.running && !document.getElementById('db-flash-overlay').hidden"), '关闭继电器影响了烧写作业'
                    mouse_click('#window-tab'); mouse_click('#window-powershell')
                    assert evaluate("!document.getElementById('powershell-select-overlay').hidden && !document.getElementById('db-flash-overlay').hidden"), '命令行选窗仍阻断烧写窗口'
                    evaluate("document.getElementById('powershell-select-close').click()")
                    wait_until(lambda: evaluate("!window.dbFlashPreview.running"))
                    # 点击窗口外不关闭，关闭后重新打开保留位置和尺寸。
                    remembered = rect('#db-flash-overlay .tool-floating-window')
                    evaluate("window.dbFlashPreview.close(); document.getElementById('tools-db-flash').click()")
                    reopened = rect('#db-flash-overlay .tool-floating-window')
                    assert remembered == reopened, '重新打开未保留位置尺寸'
                    evaluate("callWS=window.floatingTest.original; delete window.floatingTest")
                    print('  [PASS] 真鼠标拖动/缩放、主菜单穿透、烧写时独立操作假继电器及关闭重开')
                command('Emulation.setDeviceMetricsOverride', {'width': 480, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                assert evaluate('document.documentElement.scrollWidth <= window.innerWidth'), '窄窗口溢出'
                print('  [PASS] 桌面与窄窗口布局无水平溢出')
                command('Emulation.setDeviceMetricsOverride', {'width': 1180, 'height': 1100, 'deviceScaleFactor': 1, 'mobile': False})
                evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                assert evaluate('document.documentElement.scrollWidth <= window.innerWidth'), '截图前桌面布局溢出'
                size = command('Page.getLayoutMetrics')['cssContentSize']
                png = command('Page.captureScreenshot', {'captureBeyondViewport': True, 'fromSurface': True,
                              'clip': {'x': 0, 'y': 0, 'width': size['width'], 'height': size['height'], 'scale': 1}})['data']
                (runtime / 'db-flash-preview.png').write_bytes(base64.b64decode(png))
            after = {p.relative_to(fixture).as_posix(): p.read_bytes() for p in fixture.rglob('*') if p.is_file()}
            assert snapshot == after, '模拟过程中修改了 DB 工具目录'
            after_p300 = {p.relative_to(p300).as_posix(): p.read_bytes() for p in p300.rglob('*') if p.is_file()}
            assert snapshot_p300 == after_p300, 'p300-udp 模拟过程中修改了固化包目录'
            assert not (fixture / second.name).exists(), '外部镜像被复制到 DB'
            print('  [PASS] 模拟后 DB 文件保持逐字节一致，外部镜像未复制到 DB')
        return True
    except Exception as exc:
        print(f'  [FAIL] DB 模拟界面：{type(exc).__name__}: {exc}')
        return False
    finally:
        if browser:
            browser.close()
        if backend:
            if app_mode and backend.poll() is None:
                backend.stdin.write(b'stop\n')
                backend.stdin.flush()
                try:
                    backend.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    backend.terminate()
                    backend.wait(timeout=10)
            elif backend.poll() is None:
                backend.terminate()
                backend.wait(timeout=10)
        if log:
            log.close()


if __name__ == '__main__':
    result = run()
    sys.exit(0 if result is not False else 1)
