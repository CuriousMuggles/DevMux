"""旧接入生命周期回归：故障注入与临时目录，不接触现场设备。"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_app
import relay_session
from tests.test_relay_app import FakeBackend, _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until


def result(ok=True, error='模拟离线', **extra):
    return relay.Result(reqId='probe', status=relay.STATUS_OK if ok else relay.STATUS_ERROR,
                        error='' if ok else error, extra=extra)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='relay-lifecycle-')
        self.events, self.exits, self.output = [], [], []
        self.controller = relay_app.RelayController(
            config_path=os.path.join(self.tmp.name, 'config.json'),
            on_event=self.events.append,
            on_terminal_exit=lambda *a: self.exits.append(a),
            on_terminal_output=lambda *a: self.output.append(a))
        self.node = SimpleNamespace(detach_remote_session=Mock(),
                                    close_remote_session=Mock(return_value=result(False)),
                                    open_remote_session=Mock(return_value=result(False)))
        self.controller.node = self.node
        self.key = 'relay:slave:device'
        self.binding = dict(peerId='slave', localKey='device', streamId='old', opened=True)
        self.controller._sessions[self.key] = self.binding

    def tearDown(self):
        self.controller.node = None
        self.tmp.cleanup()

    def test_rejected_close_is_retained_until_ack(self):
        c = self.controller
        self.assertFalse(c.close_session(self.key))
        self.assertNotIn(self.key, c._sessions)
        self.assertEqual(len(c._retired_sessions), 1)
        token, item = next(iter(c._retired_sessions.items()))
        self.assertEqual(item['streamId'], 'old')
        item['nextAttempt'] = 0
        self.node.close_remote_session.return_value = result()
        c._drain_session_closes()
        self.assertFalse(c._retired_sessions)
        self.assertEqual(self.node.close_remote_session.call_count, 2)

    def test_pending_close_blocks_reopen_until_confirmed(self):
        c = self.controller
        endpoint = ('root', 'boot', 'node')
        device = dict(key=self.key, peerId='slave', localKey='device',
                      relay=dict(online=True, endpointRoot='root', startedAt='boot', nodeId='node'))
        c._is_confirmed = lambda d: True
        c.remote_devices = lambda: []
        c.online_remote_devices = lambda: []
        c._reconcile_sessions_once()
        self.assertEqual(len(c._retired_sessions), 1)
        c.online_remote_devices = lambda: [device]
        c._reconcile_sessions_once()
        self.node.open_remote_session.assert_not_called()
        self.node.close_remote_session.return_value = result()
        next(iter(c._retired_sessions.values()))['nextAttempt'] = 0
        c._emit_devices = c._emit_status = lambda: None
        c._reconcile_sessions_once()
        self.node.open_remote_session.assert_called_once()
        self.assertEqual(c._sessions[self.key]['endpoint'], endpoint)

    def test_open_in_flight_cannot_escape_pending_close(self):
        c = self.controller
        self.binding['opening'] = True
        self.assertFalse(c.close_session(self.key))
        c._drain_session_closes()
        self.node.close_remote_session.assert_not_called()
        self.binding['opening'] = False
        self.node.close_remote_session.return_value = result()
        c._drain_session_closes()
        self.assertFalse(c._retired_sessions)

    def test_old_exit_cannot_end_new_binding_or_node(self):
        c = self.controller
        c._on_session_exit(self.key, 'other', '迟到退出')
        c._on_session_exit(self.key, 'old', '旧节点退出', expected_node=object())
        c._on_session_exit(self.key, 'old', '旧绑定退出', expected_binding=dict(self.binding))
        self.assertTrue(self.binding['opened'])
        self.assertFalse(self.exits)
        self.assertFalse(self.events)
        self.assertFalse(c._retired_sessions)

    def test_old_output_and_exit_callbacks_are_isolated(self):
        c = self.controller
        callbacks = {}
        def opened(*args, **kwargs):
            callbacks.update(kwargs)
            return result()
        self.node.open_remote_session.side_effect = opened
        c._open_session(self.key, self.binding, {})
        callbacks['on_output'](b'current')
        self.assertEqual(len(self.output), 1)
        c._sessions[self.key] = dict(self.binding)
        callbacks['on_output'](b'old')
        callbacks['on_exit']('迟到')
        self.assertEqual(len(self.output), 1)
        self.assertFalse(self.exits)

    def test_reader_error_finishes_and_queues_cleanup(self):
        c = self.controller
        reader = relay_session.RemoteSessionReader(
            'slave', 'old', read_stream=Mock(side_effect=OSError('模拟共享目录断开')),
            read_meta=lambda: {'status': 'running'}, on_output=lambda b: None,
            on_exit=lambda reason: c._on_session_exit(self.key, 'old', reason),
            on_event=self.events.append)
        self.node.detach_remote_session.side_effect = lambda *a: reader.stop()
        reader.start()
        thread = reader._thread
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(reader.is_exited())
        self.assertEqual(len(self.exits), 1)
        self.assertFalse(self.binding['opened'])
        self.assertEqual(len(c._retired_sessions), 1)

    def test_repeat_failure_toast_dedup_and_sixty_second_retry(self):
        c = self.controller
        self.binding.update(opened=False, failures=4)
        for _ in range(3):
            before = time.time()
            c._open_session(self.key, self.binding, {})
            self.assertAlmostEqual(self.binding['nextAttempt'] - before, 60, delta=1)
        self.assertEqual(len([e for e in self.events if e['kind'] == 'reject']), 1)
        self.node.open_remote_session.return_value = result(False, '另一原因')
        c._open_session(self.key, self.binding, {})
        self.assertEqual(len([e for e in self.events if e['kind'] == 'reject']), 2)
        self.node.open_remote_session.return_value = result()
        c._open_session(self.key, self.binding, {})
        self.node.open_remote_session.return_value = result(False, '另一原因')
        c._open_session(self.key, self.binding, {})
        self.assertEqual(len([e for e in self.events if e['kind'] == 'reject']), 3)

    def test_exit_during_open_does_not_resurrect_binding(self):
        def opened(*a, **kwargs):
            kwargs['on_exit']('创建期间输出读取失败')
            return result()
        self.node.open_remote_session.side_effect = opened
        self.controller._open_session(self.key, self.binding, {})
        self.assertFalse(self.binding['opened'])
        self.assertEqual(len(self.controller._retired_sessions), 1)

    def test_windows_close_failure_and_compensation(self):
        c = self.controller
        identifier = 'a' * 32
        self.node._peers = {'slave': SimpleNamespace(nodeId='node')}
        self.node.submit = Mock(return_value=result(False))
        client = SimpleNamespace(node=self.node, meta={'state': 'running'})
        c._windows_clients[('node', identifier)] = client
        with self.assertRaises(RuntimeError):
            c.windows_call('node', 'session_close', {'sessionId': identifier})
        self.assertIs(c._windows_clients[('node', identifier)], client)
        self.assertEqual(len(c._retired_windows), 1)
        self.node.submit.return_value = result()
        next(iter(c._retired_windows.values()))['nextAttempt'] = 0
        c._drain_windows_closes()
        self.assertFalse(c._retired_windows)
        self.assertFalse(c._windows_clients)
        self.node.submit.side_effect = [result(data=dict(sessionId=identifier,
            streamProtocol=1, streamId='win-' + identifier)), result(False), result()]
        with patch('windows_stream.MasterStream', side_effect=OSError('模拟初始化失败')):
            with self.assertRaises(OSError):
                c.windows_call('node', 'session_open', {})
        self.assertEqual(len(c._retired_windows), 1)
        next(iter(c._retired_windows.values()))['nextAttempt'] = 0
        c._drain_windows_closes()
        self.assertFalse(c._retired_windows)

    def test_windows_close_is_idempotent_but_owner_is_enforced(self):
        from windows_executor import WindowsService
        service = WindowsService(Path(self.tmp.name) / 'windows')
        identifier = 'b' * 32
        proc = Mock()
        service.sessions[identifier] = dict(info=dict(sessionId=identifier, owner='owner', state='running'),
                                           proc=proc)
        try:
            with self.assertRaises(PermissionError):
                service.session_action('session_close', {'sessionId': identifier}, 'other')
            proc.close.assert_not_called()
            for _ in range(2):
                self.assertEqual(service.session_action('session_close',
                    {'sessionId': identifier}, 'owner')['state'], 'closed')
            proc.close.assert_called_once()
        finally:
            service.close()

    def test_slave_handover_preserves_ownership_and_fences_old_stream(self):
        backend = FakeBackend()
        manager = relay_session.SessionManager(relay.RelayQueue(self.tmp.name), backend)
        device = {'key': 'device', 'serial': 'device'}
        owner = ('master', 'generation')
        try:
            self.assertTrue(manager.open('first', device, owner=owner)[0])
            old_handle = backend.live_handles()[0]
            self.assertFalse(manager.open('first', device, owner=('other', 'generation'))[0])
            self.assertFalse(manager.open('foreign', device, owner=('other', 'generation'))[0])
            self.assertFalse(manager.close('first', owner=('other', 'generation'))[0])
            self.assertTrue(manager.open('second', device, owner=owner)[0])
            self.assertIn(old_handle, backend.closed)
            self.assertEqual(manager.active_ids(), ['second'])
            self.assertFalse(manager.open('first', device, owner=owner)[0])
            self.assertTrue(manager.close('first', owner=owner)[0])
            self.assertEqual(manager.active_ids(), ['second'])
            self.assertEqual(len(backend.live_handles()), 1)
            manager.stop_all()
            self.assertFalse(manager.open('second', device, owner=owner)[0])
        finally:
            manager.stop_all()

    def test_close_during_slave_open_cancels_late_completion(self):
        backend = FakeBackend()
        manager = relay_session.SessionManager(relay.RelayQueue(self.tmp.name), backend)
        entered, release = threading.Event(), threading.Event()
        original = backend.open
        def delayed(*a, **k):
            entered.set()
            release.wait(3)
            return original(*a, **k)
        backend.open = delayed
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(manager.open(
            'inflight', {'key': 'device'}, owner=('master', 'generation'))))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            manager.close('inflight', owner=('master', 'generation'))
            release.set()
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertFalse(outcomes[0][0])
            self.assertFalse(backend.live_handles())
            self.assertFalse(manager.active_ids())
        finally:
            release.set()
            thread.join(4)
            manager.stop_all()

    def test_real_nodes_recover_after_lost_close(self):
        harness = _Harness(os.path.join(self.tmp.name, 'bus'), heartbeat_s=.5)
        try:
            with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
                harness.start()
                harness.confirm_both()
                self.assertTrue(_wait_until(lambda: any(x.get('opened') for x in
                    harness.master.sessions_snapshot()), 10))
                key = next(x['key'] for x in harness.master.sessions_snapshot() if x.get('opened'))
                old_sid = harness.master._sessions[key]['streamId']
                with patch.object(harness.master.node, 'close_remote_session', return_value=result(False)):
                    self.assertFalse(harness.master.close_session(key))
                    harness.master._reconcile_sessions()
                    self.assertIn(old_sid, harness.slave.node._sessions.active_ids())
                    self.assertNotIn(key, harness.master._sessions)
                next(iter(harness.master._retired_sessions.values()))['nextAttempt'] = 0
                self.assertTrue(_wait_until(lambda: any(x['key'] == key and x.get('opened')
                    for x in harness.master.sessions_snapshot()), 10))
                self.assertNotIn(old_sid, harness.slave.node._sessions.active_ids())
                self.assertEqual(len(harness.backend.live_handles()), 1)
                self.assertNotEqual(harness.master._sessions[key]['streamId'], old_sid)
        finally:
            harness.stop()


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LifecycleTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
