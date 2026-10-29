"""共享shell的单方控制、代次隔离、断线保留及真实中转往返。"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared_terminal import TerminalHub, SharedTerminalBackend


class Shell:
    _alive = True
    def __init__(self):
        self.writes = []
        self.sizes = []
        self.closed = 0
        self.cwd = '/'
        self.output = lambda data: None
    def write(self, data):
        self.writes.append(data)
        if data.startswith(b'cd '):
            self.cwd = data[3:].strip().decode()
        self.output(data + b'\r\n' + self.cwd.encode() + b'> ')
    def resize(self, cols, rows):
        self.sizes.append((cols, rows))
    def close(self):
        self.closed += 1


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.shell = Shell()
        self.states = []
        self.hub = TerminalHub(self.states.append)
        self.key = 'dev:tid=1'
        self.hub.register(self.key, self.shell)
        self.shell.output = lambda data: self.hub.publish(self.key, data)
        self.backend = SharedTerminalBackend(lambda: SimpleNamespace(terminal_hub=self.hub))
        self.outputs = []
        self.exits = []
    def attach(self):
        return self.backend.open({'serial':'dev','transportId':1}, cols=90, rows=30,
                                 on_output=self.outputs.append, on_exit=lambda: self.exits.append(True))
    def test_single_shell_context_and_control(self):
        local_epoch = self.hub.state(self.key)['generation']
        self.assertTrue(self.hub.local_write(self.key, b'cd /data\r', local_epoch))
        lease = self.attach()
        self.assertFalse(self.hub.local_write(self.key, b'local forbidden', local_epoch))
        self.assertTrue(self.backend.write(lease, b'pwd\r'))
        self.assertEqual(self.shell.cwd, '/data')
        self.assertTrue(self.hub.take(self.key))
        self.assertFalse(self.backend.write(lease, b'old remote'))
        state = self.hub.state(self.key)
        self.assertFalse(self.hub.local_write(self.key, b'old local', local_epoch))
        self.assertFalse(self.hub.local_write(self.key, b'no epoch'))
        self.assertTrue(self.hub.local_write(self.key, b'cd /sdcard\r', state['generation']))
        self.assertTrue(self.hub.give(self.key))
        self.assertFalse(self.backend.write(lease, b'delayed old remote'))
        new = self.attach()
        self.assertTrue(self.backend.write(new, b'pwd after return\r'))
        self.assertEqual(self.shell.cwd, '/sdcard')
        self.assertEqual(self.shell.closed, 0)
        self.assertEqual(len(self.exits), 1)
        self.backend.close(new)
    def test_disconnect_keeps_shell_and_local_usable(self):
        lease = self.attach()
        self.backend.close(lease)
        self.assertEqual(self.shell.closed, 0)
        self.assertTrue(self.hub.local_write(self.key, b'local resumes'))
        self.assertFalse(self.backend.write(lease, b'late'))
    def test_history_and_slow_remote_do_not_block_local(self):
        self.hub.publish(self.key, b'prior output\r\n')
        entered, release = threading.Event(), threading.Event()
        lease = self.hub.attach(self.key, lambda data: (entered.set(), release.wait(2)),
                                lambda: self.exits.append(True))
        try:
            self.assertTrue(entered.wait(1))
            self.hub.take(self.key)
            token = self.hub.state(self.key)['generation']
            started = time.monotonic()
            self.assertTrue(self.hub.local_write(self.key, b'fast local', token))
            self.assertLess(time.monotonic() - started, .2)
            self.assertEqual(self.shell.writes, [b'fast local'])
        finally:
            release.set()
            self.hub.end(lease)
    def test_full_output_queue_never_runs_slow_exit_on_local_reader(self):
        blocked, release = threading.Event(), threading.Event()
        exiting, exit_release = threading.Event(), threading.Event()
        lease = self.hub.attach(self.key, lambda data: (blocked.set(), release.wait(3)),
                                lambda: (exiting.set(), exit_release.wait(3)))
        try:
            self.assertTrue(blocked.wait(1))
            done = threading.Event()
            def publish():
                for _ in range(260):
                    self.hub.publish(self.key, b'busy output')
                done.set()
            writer = threading.Thread(target=publish)
            writer.start()
            self.assertTrue(done.wait(.5), '队列满时退出IO阻塞了本地读取')
            self.assertFalse(lease.active)
            self.assertTrue(self.hub.local_write(self.key, b'local still works'))
            release.set()
            self.assertTrue(exiting.wait(1))
            writer.join(1)
        finally:
            release.set()
            exit_release.set()
            self.hub.end(lease)

    def test_resize_and_device_generation(self):
        lease = self.attach()
        self.hub.take(self.key)
        with self.assertRaises(PermissionError):
            self.backend.resize(lease, 120, 40)
        token = self.hub.state(self.key)['generation']
        self.assertTrue(self.hub.resize(self.key, 70, 20, generation=token))
        self.hub.remove(self.key)
        replacement = Shell()
        self.hub.register(self.key, replacement)
        self.hub.remove(self.key, self.shell)
        self.hub.publish(self.key, b'old output', self.shell)
        self.assertIs(self.hub.slots[self.key]['session'], replacement)
        self.assertEqual(list(self.hub.slots[self.key]['history']), [])
        self.assertFalse(self.backend.write(lease, b'old device input'))
        self.assertFalse(self.hub.local_write(self.key, b'old local device', token))
        self.assertEqual(replacement.writes, [])

    def test_readonly_cannot_close_shell_through_ws(self):
        import asyncio
        import server
        lease = self.attach()
        manager = server.ManagerServer.__new__(server.ManagerServer)
        closed = []
        manager.worker = SimpleNamespace(terminal_hub=self.hub, close_terminal=closed.append)
        manager._is_remote_key = lambda key: False
        manager._broadcast_async = lambda message: None
        manager._cleanup_device_edit = lambda key: None
        manager._close_inline_edit = lambda key: None
        asyncio.run(manager._handle_message(None, {'type':'close-terminal', 'key':self.key}))
        self.assertEqual(closed, [])
        self.hub.take(self.key)
        asyncio.run(manager._handle_message(None, {'type':'close-terminal', 'key':self.key,
                    'controlGeneration':'old'}))
        self.assertEqual(closed, [])
        self.backend.close(lease)

    def test_control_switch_does_not_report_device_exit_or_stop_recording(self):
        import server
        manager = server.ManagerServer.__new__(server.ManagerServer)
        sent = []
        manager._broadcast_async = sent.append
        manager._stop_recording = lambda key: self.fail('切换控制权不能停止录制')
        manager._on_relay_terminal_exit('relay:slave:dev', '终端控制权切换')
        self.assertEqual(sent[0]['type'], 'terminal-switch')

    def test_control_publications_cannot_finish_in_reverse_order(self):
        import relay_session
        session = relay_session.RemotePtySession.__new__(relay_session.RemotePtySession)
        session._meta_write_lock = threading.Lock()
        session._alive = True
        session._meta_error = ''
        session._published_control = None
        session.stream_id = 'race-control'
        session._emit = lambda *args: None
        state = {'owner': 'remote', 'generation': 'old'}
        session.meta = lambda *args, **kwargs: dict(state)
        entered, release = threading.Event(), threading.Event()
        written, active = [], [0]
        peak = [0]
        class Channel:
            def write_meta(self, payload, **kwargs):
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                if payload['generation'] == 'old':
                    entered.set()
                    release.wait(2)
                written.append(payload)
                active[0] -= 1
        session.queue = SimpleNamespace(stream=lambda stream: Channel())
        first = threading.Thread(target=session._write_meta, args=('running',))
        second = threading.Thread(target=session._write_meta, args=('running',))
        first.start()
        self.assertTrue(entered.wait(1))
        try:
            state.update(owner='local', generation='new')
            second.start()
            time.sleep(.05)
        finally:
            release.set()
            first.join(2)
            second.join(2)
        self.assertEqual(peak[0], 1)
        self.assertEqual(written[-1]['owner'], 'local')
        self.assertEqual(session._published_control[:2], ('local', 'new'))

    def test_real_relay_preserves_shell_across_handoff(self):
        from tests.test_relay_app import _Harness, _fake_list_devices, _fake_execute, _patch, _wait_until
        with tempfile.TemporaryDirectory(prefix='shared-relay-') as directory:
            with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
                harness = _Harness(directory, master_local_devices_enabled=False)
                hub = TerminalHub()
                shell = Shell()
                hub.register('devB:tid=1', shell)
                local = []
                def output(data):
                    local.append(data)
                    hub.publish('devB:tid=1', data)
                shell.output = output
                harness.slave.session_backend = SharedTerminalBackend(lambda: SimpleNamespace(terminal_hub=hub))
                harness.start()
                try:
                    exits = []
                    harness.master.on_terminal_exit = lambda key, reason: exits.append(reason)
                    harness.confirm_both()
                    self.assertTrue(_wait_until(lambda: any(s['opened'] for s in harness.master.sessions_snapshot()), 8))
                    key = harness.remote_key()
                    self.assertTrue(harness.master.session_input(key, b'cd /data\r'))
                    self.assertTrue(_wait_until(lambda: shell.cwd == '/data', 3))
                    self.assertTrue(_wait_until(lambda: b'/data> ' in harness.output_text(), 3))
                    old_lease = hub.slots['devB:tid=1']['lease']
                    old_stream = harness.master.sessions_snapshot()[0]['streamId']
                    # 不触发通用线程池刷新，且连续两次发布失败，独立检查仍必须恢复同步。
                    import relay
                    channel_before_take = harness.slave.node.queue.stream(old_stream)
                    original_inode = Path(channel_before_take.meta_path).stat().st_ino
                    failures = [2]
                    original_write_meta = relay.StreamChannel.write_meta
                    def failing_control_write(channel, meta, **kwargs):
                        if meta.get('owner') == 'local' and meta.get('status') == 'running' and failures[0]:
                            failures[0] -= 1
                            raise OSError('模拟共享目录暂时不可写')
                        return original_write_meta(channel, meta, **kwargs)
                    with patch.object(relay.StreamChannel, 'write_meta', failing_control_write):
                        hub.take('devB:tid=1')
                        self.assertTrue(_wait_until(lambda: harness.master.sessions_snapshot()[0]['owner'] == 'local', 5))
                    self.assertEqual(failures[0], 0)
                    self.assertEqual(Path(channel_before_take.meta_path).stat().st_ino, original_inode)
                    self.assertFalse(harness.master.session_input(key, b'blocked remote'))
                    token = hub.state('devB:tid=1')['generation']
                    self.assertTrue(hub.local_write('devB:tid=1', b'cd /sdcard\r', token))
                    self.assertTrue(_wait_until(lambda: b'/sdcard> ' in harness.output_text(), 3))
                    ok, message = harness.master.take_session_control(key, 'stale-generation')
                    self.assertFalse(ok)
                    self.assertEqual(hub.state('devB:tid=1')['owner'], 'local')
                    handoff_started = time.monotonic()
                    ok, message = harness.master.take_session_control(key, token)
                    self.assertTrue(ok, message)
                    self.assertFalse(hub.local_write('devB:tid=1', b'late local', token))
                    self.assertTrue(_wait_until(lambda: any(s['opened'] and s['streamId'] != old_stream
                                                          for s in harness.master.sessions_snapshot()), 10))
                    self.assertLess(time.monotonic() - handoff_started, 5, '主动接管仍进入5秒故障退避')
                    self.assertIn('终端控制权切换', exits)
                    self.assertFalse(hub.remote_write(old_lease, b'late old bytes'))
                    self.assertEqual(shell.cwd, '/sdcard')
                    self.assertEqual(shell.closed, 0)
                    # 旧接入元信息消失时，主机不能永远保留opened并向无人消费的输入流写入。
                    current = harness.master.sessions_snapshot()[0]
                    orphan_stream = current['streamId']
                    orphan_lease = hub.slots['devB:tid=1']['lease']
                    channel = harness.slave.node.queue.stream(orphan_stream)
                    import relay
                    stale_meta = channel.read_meta()
                    original_read_json = relay.read_json
                    def cached_read(path, *args, **kwargs):
                        return stale_meta if str(path) == channel.meta_path else original_read_json(path, *args, **kwargs)
                    with patch.object(relay, 'read_json', cached_read):
                        Path(channel.meta_path).unlink()
                        self.assertTrue(_wait_until(lambda: any(s['opened'] and s['streamId'] != orphan_stream
                                                              for s in harness.master.sessions_snapshot()), 16))
                    self.assertFalse(orphan_lease.active)
                    self.assertEqual(shell.closed, 0)
                    self.assertEqual(shell.cwd, '/sdcard')
                    self.assertTrue(harness.master.session_input(key, b'pwd\r'))
                    self.assertTrue(_wait_until(lambda: shell.writes[-1] == b'pwd\r', 3))
                    # 模拟主机进程不再发布心跳，没有发送关闭请求。
                    harness.master.node._stop_flag.set()
                    self.assertTrue(_wait_until(lambda: hub.slots['devB:tid=1']['lease'] is None, 8))
                    self.assertEqual(shell.closed, 0)
                finally:
                    # 本测试拥有临时目录，等待队列执行/归档任务退出后再删除，不能与落盘竞争。
                    nodes = [c.node for c in (harness.master, harness.slave) if c.node]
                    harness.stop()
                    for node in nodes:
                        node._pool.shutdown(wait=True)
                        node._terminal_pool.shutdown(wait=True)
                        node._archive_pool.shutdown(wait=True)


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SharedTests)).wasSuccessful()
if __name__ == '__main__':
    sys.exit(0 if run() else 1)
