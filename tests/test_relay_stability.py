"""持续连接与慢共享目录 IO 的回归验证。"""
import asyncio
import concurrent.futures
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_node
import relay_app
import server


def run():
    checks = []
    def check(name, value):
        checks.append(bool(value))
        print(('  [PASS] ' if value else '  [FAIL] ') + name, flush=True)
    # 主循环发现还没结束时，界面先离线再恢复，日志也必须完整记录。
    controller = relay_app.RelayController.__new__(relay_app.RelayController)
    controller._lock = threading.RLock()
    controller._last_status_sig = ''
    peer = {'nodeId': 'node-slave', 'peerId': 'slave', 'online': True,
            'heartbeatSeq': 21, 'lastHeartbeatAgeMs': 100, 'heartbeatReadMs': 15}
    other = {'nodeId': 'node-other', 'peerId': 'other', 'online': True}
    snapshot = {'pairing': [peer, other], 'state': 'connected'}
    controller.status = lambda: snapshot
    pushed, logged = [], []
    controller.on_status = lambda status: pushed.append(status)
    controller.log = lambda category, message: logged.append(message)
    controller._emit_status()
    peer.update(online=False, lastHeartbeatAgeMs=3201, heartbeatReadMs=3100)
    controller._emit_status()
    controller._emit_status()
    peer.update(online=True, heartbeatSeq=25, lastHeartbeatAgeMs=0)
    controller._emit_status()
    check('发现状态未变化时逐从机界面离线和恢复仍各记录一次',
          len(logged) == 2 and '判定离线' in logged[0] and '恢复在线' in logged[1]
          and '3201ms' in logged[0] and '3100ms' in logged[0]
          and not any('other' in line for line in logged) and len(pushed) == 3)
    with tempfile.TemporaryDirectory() as root:
        node = relay_node.RelayNode(peer_id='master', role='master', root=root,
            node_id='node-master', local_devices_enabled=False, pairing_required=False,
            trust_path=os.path.join(root, 'trust.json'))
        try:
            beat = relay.PeerHeartbeat(peerId='slave', role='slave', nodeId='node-slave',
                fingerprint='slave', host='slave', startedAt=relay.now_iso(),
                heartbeatAt=relay.now_iso(),
                devices=[{'key': 'dev:tid=1', 'serial': 'dev', 'state': 'device'}])
            node._peers = {'slave': beat}
            node.confirm_peer('slave')
            node._health = node._compute_health()
            node._mark_connection_verified('slave')
            endpoint = node._connection_endpoint('slave')
            node._connection_verified['slave'] = (endpoint, time.monotonic() - 100)
            check('在线且实例未变时不会仅因验证计时拆掉连接', node.connection_ready('slave'))
            with patch.object(beat, 'is_stale', return_value=True):
                info = node.connection_info('slave')
                check('越过心跳阈值时在线字段和连接结论使用同一次判定',
                      info['online'] is False and info['connectionState'] == 'offline')
            before = node.remote_devices()
            timeout = relay.Result(reqId='', status=relay.STATUS_ERROR,
                error='共享目录探测超时', extra={'localTimeout': True})
            with patch.object(node, 'submit', return_value=timeout):
                for _ in range(3):
                    node.verify_connection('slave')
                    check('瞬时超时保持已验证设备与连接', node.connection_ready('slave')
                          and node.remote_devices() == before)
            rejected = relay.Result(reqId='', status=relay.STATUS_REJECTED, error='已解除配对')
            with patch.object(node, 'submit', return_value=rejected):
                node.verify_connection('slave')
            check('从机明确拒绝仍立即停止连接', not node.connection_ready('slave'))
            node._mark_connection_verified('slave')
            beat.startedAt = '2027-01-01T00:00:00+00:00'
            check('对端重启不能继承旧验证', not node.connection_ready('slave'))
            stream = relay.RelayQueue.for_peer(root, 'slave').stream('sess-independent')
            stream.output_writer().append(b'output')
            completed = threading.Event()
            outcome = []
            def stream_io():
                outcome.append(node.send_remote_input('slave', 'sess-independent', b'input'))
                outcome.append(node.read_remote_stream('slave', 'sess-independent')[0] == b'output')
                completed.set()
            with node._io_lock:
                thread = threading.Thread(target=stream_io)
                thread.start()
                independent = completed.wait(0.3)
            thread.join(2)
            check('心跳/控制锁被占用时输入和输出仍可流动', independent and all(outcome))
        finally:
            node.stop()

    controller = relay_app.RelayController.__new__(relay_app.RelayController)
    controller._reconcile_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def slow_reconcile():
        calls.append(1)
        entered.set()
        release.wait(2)
    controller._reconcile_sessions_once = slow_reconcile
    thread = threading.Thread(target=controller._reconcile_sessions)
    thread.start()
    entered.wait(1)
    controller._reconcile_sessions()
    release.set()
    thread.join()
    check('确认与后台轮询并发时只执行一次会话对齐', len(calls) == 1)

    # 网络等待跨过重载时，旧轮询必须使用自己的停止令牌。
    old_stop = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    controller = relay_app.RelayController.__new__(relay_app.RelayController)
    controller._stop_flag = old_stop
    controller.node = type('Node', (), {'refresh': lambda self: None})()
    controller._refresh_connections = lambda: (entered.set(), release.wait(2))
    controller._notice_peer_requests = lambda: None
    controller._emit_status = lambda: None
    reconciles = []
    controller._reconcile_sessions = lambda: reconciles.append(1)
    controller._emit_devices = lambda: None
    controller._push_event = lambda event: None
    thread = threading.Thread(target=controller._loop, args=(old_stop,))
    thread.start()
    entered.wait(1)
    old_stop.set()
    controller._stop_flag = threading.Event()
    release.set()
    thread.join(1)
    check('重载不会复活在等待网络的旧轮询线程', not thread.is_alive() and not reconciles)
    controller._stop_flag.set()

    # 第一台从机验证挂起时，第二台验证与界面轮询仍独立前进。
    from types import SimpleNamespace
    slow_entered, slow_release, second_checked = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def verify(peer_id):
        calls.append(peer_id)
        if peer_id == 'same-pc':
            slow_entered.set()
            slow_release.wait(3)
        else:
            second_checked.set()
    controller = relay_app.RelayController.__new__(relay_app.RelayController)
    controller._lock = threading.RLock()
    controller.config = SimpleNamespace(role=relay.ROLE_MASTER)
    controller._connection_futures = {}
    controller.node = SimpleNamespace(health=lambda: SimpleNamespace(conflict=False),
        peers=lambda: [SimpleNamespace(peerId=peer, role=relay.ROLE_SLAVE, online=True,
                                      trusted=True, needsConfirmation=False)
                       for peer in ('same-pc', 'same-pc-2')],
        connection_check_due=lambda peer: True, verify_connection=verify,
        _io_lock=threading.RLock(), _connection_verified={}, _connection_endpoint=lambda peer: ('r', 'n', 't'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        controller._connection_executor = pool
        started = time.monotonic()
        controller._refresh_connections()
        check('多个从机的验证不阻塞状态轮询', time.monotonic() - started < .2)
        try:
            check('同机第二实例不必等待第一实例验证完成',
                  slow_entered.wait(1) and second_checked.wait(1) and not slow_release.is_set())
            controller._refresh_connections()
            check('慢从机只保留一个验证任务，避免重复请求堆积', calls.count('same-pc') == 1)
        finally:
            slow_release.set()

    # 使用正常构造，避免夹具漏掉新增状态；关闭以实际确认回执为准。
    with tempfile.TemporaryDirectory(prefix='relay-stability-controller-') as directory:
        # 短暂心跳离线不能删除当前流；设备彻底消失时仍回收。
        controller = relay_app.RelayController(config_path=os.path.join(directory, 'relay.json'))
        closed = []
        def close_remote(*args, **kwargs):
            closed.append(args)
            return relay.Result(reqId='close', status=relay.STATUS_OK)
        controller.node = SimpleNamespace(close_remote_session=close_remote,
                                          detach_remote_session=lambda *args: None)
        controller._lock = threading.RLock()
        controller._notified_peers = set()
        controller._push_event = lambda event: None
        controller._is_confirmed = lambda device: True
        controller.online_remote_devices = lambda: []
        controller.remote_devices = lambda: [{'key': 'remote', 'relay': {'online': False}}]
        binding = {'streamId': 'original-stream', 'peerId': 'same-pc-2', 'opened': True}
        controller._sessions = {'remote': binding}
        controller._reconcile_sessions_once()
        check('短暂离线保留原终端流与输入代次，不重复重建',
              controller._sessions.get('remote') is binding and not closed)
        controller.remote_devices = lambda: []
        controller._reconcile_sessions_once()
        check('设备退出离线保留期后保留待确认回收记录',
              not controller._sessions and len(controller._retired_sessions) == 1 and not closed)
        controller._drain_session_closes()
        check('从机确认关闭后才完成旧接入回收',
              len(closed) == 1 and not controller._retired_sessions)

        # 开启请求返回前绑定已被取消，不能再把旧终端标成已就绪。
        binding = {'streamId': 'obsolete-stream', 'peerId': 'same-pc-2', 'localKey': 'dev'}
        detached = []
        controller._sessions = {'remote': binding}
        def late_open(*args, **kwargs):
            controller._sessions.pop('remote')
            return relay.Result(reqId='late', status=relay.STATUS_OK)
        controller.node.open_remote_session = late_open
        controller.node.detach_remote_session = lambda *args: detached.append(args)
        controller._open_session('remote', binding, {})
        check('取消后迟到的开启回执进入回收队列，不冒充当前终端',
              not binding.get('opened') and len(detached) == 1
              and len(controller._retired_sessions) == 1 and len(closed) == 1)
        controller._drain_session_closes()
        check('迟到开启确认关闭后回收记录才清除',
              len(closed) == 2 and not controller._retired_sessions)

    async def ui_checks():
        manager = server.ManagerServer.__new__(server.ManagerServer)
        manager._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        manager._remote_io_pending = {'input': {}, 'resize': {}}
        manager._remote_io_tasks = {'input': {}, 'resize': {}}
        manager._remote_last_size = {}
        class FakeRelay:
            generation = (1, 'stream-a')
            def is_remote_key(self, key): return True
            def session_generation(self, key): return self.generation
        manager.relay = FakeRelay()
        writes, sizes, completions = [], [], []
        def write(key, data):
            time.sleep(0.12)
            writes.append(data)
            completions.append('input')
        def resize(key, cols, rows):
            time.sleep(0.3)
            sizes.append((cols, rows))
            completions.append('resize')
        manager._write_terminal = write
        manager._resize_terminal = resize
        key = 'relay:slave:dev'
        try:
            started = time.monotonic()
            for cols in (80, 81, 82):
                await manager._handle_message(None, {'type':'resize-terminal', 'key':key,
                                                     'cols':cols, 'rows':24})
            for char in 'abcdefghijklmnopqrst':
                await manager._handle_message(None, {'type':'write-terminal','key':key,'data':char})
            check('慢输入与尺寸回执不阻塞消息处理', time.monotonic() - started < 0.05)
            ticks = 0
            for _ in range(12):
                await asyncio.sleep(0.01)
                ticks += 1
            check('网络 IO 期间事件循环持续响应', ticks == 12)
            tasks = [t for mapping in manager._remote_io_tasks.values() for t in mapping.values()]
            await asyncio.gather(*tasks)
            check('连续按键合并为一包且字节顺序不变', writes == [b'abcdefghijklmnopqrst'])
            check('连续尺寸变化只发送最后一次', sizes == [(82, 24)])
            check('慢尺寸调整不拖住输入发送', completions == ['input', 'resize'])
            manager._queue_remote_io(key, 'old')
            manager.relay.generation = (2, 'stream-b')
            manager._queue_remote_io(key, 'new')
            await asyncio.gather(*[t for m in manager._remote_io_tasks.values() for t in m.values()])
            check('旧会话排队输入不进入新终端', writes[-1] == b'new' and b'old' not in writes)
        finally:
            tasks = manager._cancel_remote_io()
            await asyncio.gather(*tasks, return_exceptions=True)
            manager._executor.shutdown(wait=True)
            check('退出清理全部远端 IO 队列', not any(manager._remote_io_pending.values())
                  and not any(manager._remote_io_tasks.values()))
    asyncio.run(ui_checks())
    return all(checks)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
