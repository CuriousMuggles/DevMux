"""Agent 接入：真实 PowerShell、进程树、PTY、协议、双节点准入和旧终端控制。"""
import asyncio
import base64
import codecs
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_api
import agent_client
import agent_mcp
import relay_app
import relay_node
from shared_terminal import TerminalHub
from windows_executor import FINAL, OutputBuffer, WindowsService
from tests.test_relay_app import _Harness, _wait_until, _fake_list_devices, _fake_execute


def completed(service, identifier, owner='tester'):
    assert _wait_until(lambda: service.status(identifier, owner)['state'] in FINAL, 15), 'PowerShell 未结束'
    return service.status(identifier, owner)


def test_windows(directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    service = WindowsService(directory)
    try:
        target = Path(directory) / '中文 空格.txt'
        script = "Set-Content -LiteralPath '"+str(target).replace("'", "''")+"' -Value '你好' -Encoding UTF8; [Console]::Write('中文'); [Console]::Error.Write('诊断')"
        job = service.start({'script': script, 'cwd': directory}, 'tester')
        info = completed(service, job['jobId'])
        assert info['state'] == 'succeeded', (info, service.output({'jobId': job['jobId'], 'stream': 'stderr'}, 'tester'))
        assert target.read_text(encoding='utf-8-sig').strip() == '你好'
        assert service.output({'jobId': job['jobId']}, 'tester')['text'] == '中文'
        assert service.output({'jobId': job['jobId'], 'stream': 'stderr'}, 'tester')['text'] == '诊断', service.output({'jobId': job['jobId'], 'stream': 'stderr'}, 'tester')
        repeated = service.start({'jobId': job['jobId'], 'script': "throw '重复执行'"}, 'tester')
        assert repeated['state'] == 'succeeded'
        try:
            service.status(job['jobId'], 'other')
            raise AssertionError('其他调用者读取了任务')
        except PermissionError:
            pass
        failure = service.start({'script': "throw '明确失败'"}, 'tester')
        assert completed(service, failure['jobId'])['state'] == 'failed'
        external = service.start({'script': 'cmd /c exit 7'}, 'tester')
        assert completed(service, external['jobId'])['exitCode'] == 7
        timeout = service.start({'script': 'Start-Sleep 60', 'timeout': .2}, 'tester')
        assert completed(service, timeout['jobId'])['state'] == 'timed_out'
        tree = service.start({'script': "$p=Start-Process powershell -ArgumentList '-NoProfile -Command Start-Sleep 60' -PassThru; [Console]::WriteLine($p.Id); Start-Sleep 60"}, 'tester')
        assert _wait_until(lambda: bool(service.output({'jobId': tree['jobId']}, 'tester')['text'].strip()), 8)
        child = int(service.output({'jobId': tree['jobId']}, 'tester')['text'].strip())
        assert relay_app._pid_alive(child)
        service.cancel(tree['jobId'], 'tester')
        assert completed(service, tree['jobId'])['state'] == 'cancelled'
        assert _wait_until(lambda: not relay_app._pid_alive(child), 5), '取消遗留子进程'
        session = service.session_open({}, 'tester:connection')
        sid = session['sessionId']
        service.session_action('session_write', {'sessionId': sid, 'data': '$agentTest=19\r'}, 'tester:connection')
        service.session_action('session_write', {'sessionId': sid, 'data': "[Console]::WriteLine('RESULT='+($agentTest+1))\r"}, 'tester:connection')
        assert _wait_until(lambda: 'RESULT=20' in service.session_action('session_read', {'sessionId': sid}, 'tester:connection')['text'], 12), service.session_action('session_read', {'sessionId': sid}, 'tester:connection')
        service.session_action('session_resize', {'sessionId': sid, 'cols': 90, 'rows': 30}, 'tester:connection')
        service.session_action('session_close', {'sessionId': sid}, 'tester:connection')
        restored = WindowsService(directory)
        assert restored.status(job['jobId'], 'tester')['state'] == 'succeeded'
        assert restored.output({'jobId': job['jobId']}, 'tester')['text'] == '中文'
        restored.close()
    finally:
        service.close()


def test_buffers_and_control():
    buffer = OutputBuffer(20)
    buffer.append('甲乙丙丁'.encode('utf-8'))
    first = buffer.read(0, 4)
    second = buffer.read(first['nextCursor'], 4)
    assert first['text'] == '甲' and second['text'] == '乙'
    assert base64.b64decode(first['data']) == '甲'.encode('utf-8')
    buffer.append(b'012345678901234567890123456789')
    assert buffer.read(0, 20)['droppedBytes'] > 0
    writes = []
    hub = TerminalHub()
    hub.register('device', SimpleNamespace(write=lambda data: writes.append(data), resize=lambda c, r: None))
    generation = hub.agent_claim('device')
    assert not hub.local_write('device', b'wrong')
    hub.agent_write('device', generation, b'correct')
    hub.take('device')
    try:
        hub.agent_write('device', generation, b'old')
        raise AssertionError('本地接管未阻止旧 Agent 输入')
    except PermissionError:
        pass
    assert writes == [b'correct']


def test_relay(directory):
    with patch.object(relay_node, 'list_devices', return_value=([], '')):
        harness = _Harness(directory, master_local_devices_enabled=False)
        harness.slave.config.allowWindows = False
        harness.start()
        try:
            harness.confirm_both()
            target = harness.slave.config.nodeId
            node = harness.slave.node
            assert harness.slave.set_windows_permission(True)
            assert harness.slave.node is node and node.allow_windows
            assert relay_app.load_config(harness.slave.config_path).allowWindows
            assert harness.slave.set_windows_permission(False) is False
            assert harness.slave.node is node and not node.allow_windows
            assert not relay_app.load_config(harness.slave.config_path).allowWindows
            with patch.object(relay_app, 'save_config', return_value=False):
                try:
                    harness.slave.set_windows_permission(True)
                    raise AssertionError('写入失败仍更新了权限')
                except OSError:
                    pass
            assert not node.allow_windows and not harness.slave.config.allowWindows
            caps = harness.master.windows_call(target, 'capabilities', {})
            assert caps['allowed'] is False
            try:
                harness.master.windows_call(target, 'start', {'script': "throw '不应执行'"})
                raise AssertionError('ADB 默认权限绕过电脑权限')
            except RuntimeError as exc:
                assert 'PowerShell' in str(exc)
            harness.slave.node.allow_windows = True
            session = harness.master.windows_call(target, 'session_open', {'cols': 100, 'rows': 30})
            import statistics
            timings = []
            for interval in (.25, .05):
                harness.slave.node.poll_interval_s = interval
                samples = []
                for _ in range(8):
                    began = time.monotonic()
                    if interval == .25:
                        reply = harness.master.node.submit('pc-b', relay_node.relay.KIND_WINDOWS,
                            payload={'action': 'session_read', 'args': {'sessionId': session['sessionId']}}, timeout=3)
                        assert reply.status == relay_node.relay.STATUS_OK
                    else:
                        harness.master.windows_call(target, 'session_read', {'sessionId': session['sessionId']})
                    samples.append((time.monotonic()-began)*1000)
                timings.append(statistics.median(samples))
            harness.master.windows_call(target, 'session_close', {'sessionId': session['sessionId']})
            assert timings[1] < timings[0]*.75 + 30, timings
            print(f'  [PASS] 真实双节点终端读取中位耗时 {timings[0]:.0f}ms → {timings[1]:.0f}ms')
            streamed = harness.master.windows_call(target, 'session_open', {})
            assert streamed.get('streamProtocol') == 1
            with patch.object(harness.master.node, 'submit', side_effect=AssertionError('交互退回逐次请求')):
                harness.master.windows_call(target, 'session_write', {'sessionId': streamed['sessionId'],
                    'data': "$streamValue=71\r[Console]::WriteLine('STREAM='+($streamValue+1))\r"})
                harness.master.windows_call(target, 'session_resize', {'sessionId': streamed['sessionId'], 'cols':100, 'rows':30})
                assert _wait_until(lambda: 'STREAM=72' in harness.master.windows_call(target, 'session_read',
                    {'sessionId': streamed['sessionId']})['text'], 10)
            harness.master.windows_call(target, 'session_close', {'sessionId': streamed['sessionId']})
            ended = harness.master.windows_call(target, 'session_open', {})
            harness.master.windows_call(target, 'session_write', {'sessionId': ended['sessionId'],
                'data': "[Console]::Write('Z'*90000); exit\r"})
            received, cursor, state = b'', 0, 'running'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                chunk = harness.master.windows_call(target, 'session_read',
                    {'sessionId': ended['sessionId'], 'cursor':cursor, 'limit':65536})
                received += base64.b64decode(chunk['data'])
                cursor, state = chunk['nextCursor'], chunk['state']
                if state == 'closed' and not chunk['hasMore']:
                    break
                time.sleep(.01)
            assert state == 'closed' and received.count(b'Z') >= 90000, len(received)
            harness.master.windows_call(target, 'session_close', {'sessionId': ended['sessionId']})
            disabled = harness.master.windows_call(target, 'session_open', {})
            harness.slave.set_windows_permission(False)
            assert _wait_until(lambda: not harness.slave.node.windows_service.sessions, 3)
            assert _wait_until(lambda: harness.master.windows_call(target, 'session_read',
                {'sessionId': disabled['sessionId']})['state'] == 'closed', 3)
            try:
                harness.master.windows_call(target, 'session_write', {'sessionId': disabled['sessionId'], 'data':'hostname\r'})
                raise AssertionError('权限关闭后仍允许写入旧流')
            except RuntimeError:
                pass
            harness.slave.set_windows_permission(True)
            # 没有协商持续流的旧端仍可按原控制协议操作。
            legacy = harness.master.node.submit('pc-b', relay_node.relay.KIND_WINDOWS,
                payload={'action': 'session_open', 'args': {}}, timeout=10).extra['data']
            assert 'streamProtocol' not in legacy
            harness.master.windows_call(target, 'session_close', {'sessionId': legacy['sessionId']})
            job = harness.master.windows_call(target, 'start', {'script': "[Console]::Write('远程')"})
            assert _wait_until(lambda: harness.master.windows_call(target, 'status', {'jobId': job['jobId']})['state'] in FINAL, 10)
            assert harness.master.windows_call(target, 'output', {'jobId': job['jobId']})['text'] == '远程'
            repeated = harness.master.windows_call(target, 'start', {'jobId': job['jobId'], 'script': "throw '重复'"})
            assert repeated['state'] == 'succeeded'
            source = Path(directory)/'中文 固件.bin'
            source.write_bytes(bytes(range(256))*16)
            uploaded = harness.master.windows_transfer(target, ['push', str(source), 'firmware'])
            assert uploaded['success'], uploaded
            staged = harness.slave.node.windows_service.file_path('firmware/'+source.name)
            assert staged.read_bytes() == source.read_bytes()
            downloaded = Path(directory)/'download.bin'
            pulled = harness.master.windows_transfer(target, ['pull', 'firmware/'+source.name, str(downloaded)])
            assert pulled['success'] and downloaded.read_bytes() == source.read_bytes(), pulled
            escaped = harness.master.windows_transfer(target, ['push', str(source), '../escape'])
            assert not escaped['success'], escaped
            assert not (Path(directory)/'escape').exists()
            revoked = harness.master.windows_call(target, 'session_open', {})
            harness.slave.node.unpair_peer(harness.master.config.nodeId)
            assert _wait_until(lambda: not harness.slave.node.windows_service.sessions, 3)
            assert _wait_until(lambda: harness.master.windows_call(target, 'session_read',
                {'sessionId': revoked['sessionId']})['state'] == 'closed', 3)
            try:
                harness.master.windows_call(target, 'status', {'jobId': job['jobId']})
                raise AssertionError('撤销未阻止电脑控制')
            except RuntimeError:
                pass
            stopped_node = harness.master.node
            harness.stop()
            with patch.object(stopped_node.queue, 'submit', side_effect=AssertionError('停止后仍投递请求')):
                cancelled = stopped_node.submit('pc-b', relay_node.relay.KIND_WINDOWS,
                    payload={'action': 'session_read', 'args': {}}, timeout=15)
            assert cancelled.extra.get('localCancelled')
        finally:
            harness.stop()


def test_protocol():
    response = asyncio.run(agent_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}))
    assert response['result']['protocolVersion'] == '2025-03-26'
    tools = asyncio.run(agent_mcp.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}))
    assert len(tools['result']['tools']) == len(agent_api.TOOLS)
    assert asyncio.run(agent_mcp.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})) is None
    bad = asyncio.run(agent_mcp.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'windows_exec', 'arguments': {'target': 'computer:local'}}}))
    assert bad['result']['isError']
    proc = subprocess.run([sys.executable, '-B', str(ROOT/'agent_mcp.py')], input=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n',
                          capture_output=True, text=True, encoding='utf-8', timeout=15,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) == 1 and json.loads(proc.stdout)['result']['tools']


def test_agent_adb(directory):
    import server
    events = []
    with patch.object(relay_node, 'list_devices', _fake_list_devices), patch.object(relay_node, 'execute', _fake_execute):
        harness = _Harness(directory, master_local_devices_enabled=False)
        harness.start()
        try:
            harness.confirm_both()
            assert _wait_until(lambda: harness.master.devices() and harness.backend.handles, 8)
            key = harness.master.devices()[0]['key']
            assert _wait_until(lambda: harness.master.session_input_ready(key), 8)
            manager = object.__new__(server.ManagerServer)
            manager.relay = harness.master
            manager.worker = SimpleNamespace(get_state=lambda: ([], []))
            manager._executor = None
            manager._broadcast_async = events.append
            manager.agent = agent_api.AgentAPI(manager)
            harness.master.on_terminal_output = manager.agent.publish
            async def exercise():
                result = await manager.agent.call('adb_exec', {'target':'device:'+key, 'argv':['shell','getprop ro.product.model']})
                assert result['success'] and 'node-b' in result['output'], result
                with patch.object(relay_node, 'execute', return_value=relay_node.ExecOutcome(exitCode=7, stdout='失败命令')):
                    failed = await manager.agent.call('adb_exec', {'target':'device:'+key, 'argv':['shell','false']})
                    assert not failed['success'] and failed['exitCode'] == 7, failed
                opened = await manager.agent.call('session_open', {'target':'device:'+key})
                sid = opened['sessionId']
                try:
                    await manager.agent.call('session_open', {'target':'device:'+key})
                    raise AssertionError('同一设备允许多个 Agent 写入者')
                except PermissionError:
                    pass
                await manager.agent.call('session_write', {'sessionId':sid,'data':'echo agent\r'})
                handle = next(iter(harness.backend.handles))
                assert _wait_until(lambda: b'echo agent\r' in harness.backend.written(handle), 5)
                harness.backend.emit(handle, '设备回显'.encode('utf-8'))
                for _ in range(100):
                    output = await manager.agent.call('session_read', {'sessionId':sid})
                    if '设备回显' in output['text']:
                        break
                    await asyncio.sleep(.05)
                assert '设备回显' in output['text'], output
                await manager.agent.call('session_close', {'sessionId':sid})
                assert not manager.agent.claimed(key)
                assert harness.backend.handles[handle]['alive'], '关闭 Agent 接入杀死已有设备 shell'
                again = await manager.agent.call('session_open', {'target':'device:'+key})
                manager.agent.take_local(key)
                try:
                    await manager.agent.call('session_write', {'sessionId':again['sessionId'],'data':'old'})
                    raise AssertionError('接管未阻止旧 Agent 输入')
                except RuntimeError:
                    pass
                manager.agent.close()
            asyncio.run(exercise())
        finally:
            harness.stop()


async def test_live_api(directory):
    import server
    import websockets
    Path(directory).mkdir(parents=True, exist_ok=True)
    runtime = Path(directory)/'runtime'
    runtime.mkdir()
    config = Path(directory)/'relay.json'
    relay_app.save_config(str(config), relay_app.RelayConfig(allowWindows=True))
    with patch.dict(os.environ, {'RELAY_CONFIG': str(config), 'ADB_AGENT_RUNTIME_DIR': str(runtime)}):
        manager = server.ManagerServer()
        manager.loop = asyncio.get_running_loop()
        try:
            async with websockets.serve(manager.ws_handler, '127.0.0.1', 0) as endpoint:
                port = endpoint.sockets[0].getsockname()[1]
                record = dict(instanceId=manager._agent_instance, pid=os.getpid(), wsPort=port, token=manager._agent_token)
                (runtime/'instance.json').write_text(json.dumps(record), encoding='utf-8')
                assert 'token' not in agent_client.public_instances()[0]
                async with websockets.connect(f'ws://127.0.0.1:{port}/agent') as ws:
                    await ws.send(json.dumps({'type':'agent-auth','token':'wrong'}))
                    try:
                        await ws.recv()
                        raise AssertionError('错误令牌获得了 Agent 访问')
                    except websockets.exceptions.ConnectionClosed as exc:
                        assert exc.code == 1008
                async with websockets.connect(f'ws://127.0.0.1:{port}/agent', origin='https://foreign.example') as ws:
                    try:
                        await ws.recv()
                        raise AssertionError('浏览器绕过 Agent Origin 检查')
                    except websockets.exceptions.ConnectionClosed:
                        pass
                result = await agent_client.call('targets_list', {})
                assert result['success'] and result['data']['targets'][0]['target'] == 'computer:local'
                result = await agent_client.call('windows_exec', {'target':'computer:local','script':"[Console]::Write('API成功')"})
                assert result['success'], result
                job = result['data']['jobId']
                for _ in range(100):
                    status = await agent_client.call('job_status', {'target':'computer:local','jobId':job})
                    if status['data']['state'] in FINAL:
                        break
                    await asyncio.sleep(.05)
                output = await agent_client.call('job_output', {'target':'computer:local','jobId':job})
                assert output['data']['text'] == 'API成功', output
                args_path = Path(directory)/'args.json'
                args_path.write_text(json.dumps({'target':'computer:local','jobId':job}), encoding='utf-8')
                cli = await asyncio.to_thread(subprocess.run, [sys.executable, '-B', str(ROOT/'agent_cli.py'),
                    'job_status','--args-file',str(args_path)], capture_output=True, text=True, encoding='utf-8',
                    timeout=20, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                assert cli.returncode == 0 and json.loads(cli.stdout)['data']['state'] == 'succeeded', (cli.stdout, cli.stderr)
                mcp = await agent_mcp.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'job_output','arguments':{'target':'computer:local','jobId':job}}})
                assert not mcp['result']['isError']
                assert json.loads(mcp['result']['content'][0]['text'])['data']['text'] == 'API成功'
                manager.relay.config.allowWindows = False
                denied = await agent_client.call('windows_exec', {'target':'computer:local','script':'hostname'})
                assert not denied['success']
                # 模拟从机输出读取很慢：同一 WS 的按键不能排在读取回执之后。
                read_started, written = asyncio.Event(), asyncio.Event()
                async def slow_terminal(name, args):
                    if name == 'session_read':
                        read_started.set()
                        await asyncio.wait_for(written.wait(), 2)
                    elif name == 'session_write':
                        written.set()
                    return {'state': 'running'}
                with patch.object(manager.agent, 'call', side_effect=slow_terminal):
                    async with websockets.connect(f'ws://127.0.0.1:{port}',
                            origin=f'http://127.0.0.1:{server._HTTP_PORT}') as ui:
                        await ui.send(json.dumps({'type':'computer-tool','id':'slow-read',
                            'name':'session_read','arguments':{'sessionId':'test'}}))
                        await asyncio.wait_for(read_started.wait(), 3)
                        await ui.send(json.dumps({'type':'computer-tool','id':'fast-write',
                            'name':'session_write','arguments':{'sessionId':'test','data':'x'}}))
                        responses = {}
                        while len(responses) < 2:
                            response = json.loads(await asyncio.wait_for(ui.recv(), 3))
                            if response.get('id') in ('slow-read', 'fast-write'):
                                responses[response['id']] = response
                        assert all(response['success'] for response in responses.values()), responses
        finally:
            manager.agent.close()
            manager.windows.close()
            manager._cancel_pending_broadcasts()
            manager._executor.shutdown(wait=True)


def test_plaintext_execution(directory):
    """独立任务不得再使用 -EncodedCommand：脚本以明文 .ps1（UTF-8 BOM）+ -File 执行。"""
    Path(directory).mkdir(parents=True, exist_ok=True)
    service = WindowsService(directory)
    captured = {}
    written = {}
    original_write_bytes = Path.write_bytes

    def record_write(self, data):
        written[str(self)] = data
        return original_write_bytes(self, data)

    class FakeStream:
        def read1(self, size):
            return b''

    class FakeProc:
        def __init__(self, argv, **kwargs):
            captured['argv'] = list(argv)
            captured['env'] = dict(kwargs.get('env') or {})
            captured['stdin'] = kwargs.get('stdin')
            self.pid = 424242
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    with patch('subprocess.Popen', side_effect=FakeProc), \
            patch('windows_executor.ProcessTree', return_value=SimpleNamespace(kill=lambda: None, close=lambda: None, proc=None)), \
            patch.object(Path, 'write_bytes', record_write):
        job = service.start({'script': "Write-Output '你好'"}, 'tester')
        info = completed(service, job['jobId'])
    argv = captured['argv']
    assert '-EncodedCommand' not in argv, argv
    assert '__ADM_START__' not in ' '.join(argv), argv
    assert '-File' in argv
    script_path = Path(argv[argv.index('-File') + 1])
    assert str(script_path).startswith(str(Path(directory).resolve())), script_path
    assert not script_path.exists(), '临时 .ps1 未清理'
    payload = written.get(str(script_path))
    assert payload is not None, '未写入临时 .ps1'
    assert payload.startswith(codecs.BOM_UTF8), '临时 .ps1 缺少 UTF-8 BOM'
    wrapper = payload.decode('utf-8-sig')
    assert "Write-Output '你好'" in wrapper
    assert '__ADM_START__' not in wrapper, '旧门闩哨兵仍存在'
    assert '[Console]::ReadLine' not in wrapper, '旧 stdin 门闩仍存在'
    assert '$__adm_gate' in wrapper, '门闩逻辑缺失'
    assert captured['env'].get('ADM_GATE', '').endswith('.gate')
    assert captured['stdin'] is subprocess.DEVNULL
    assert info['state'] == 'succeeded'


def run():
    try:
        assert relay_app.RelayConfig().allowWindows
        assert relay_app.RelayConfig.from_dict({}).allowWindows
        assert not relay_app.RelayConfig.from_dict({'allowWindows': False}).allowWindows
        test_buffers_and_control()
        test_protocol()
        with tempfile.TemporaryDirectory(prefix='adb-agent-test-', dir=ROOT) as directory:
            test_windows(str(Path(directory)/'windows'))
            test_plaintext_execution(str(Path(directory)/'plain'))
            test_relay(str(Path(directory)/'relay'))
            test_agent_adb(str(Path(directory)/'adb'))
            asyncio.run(test_live_api(str(Path(directory)/'api')))
        print('  [PASS] Agent：真实 PowerShell/子进程树/PTY/MCP/双节点权限与去重')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
