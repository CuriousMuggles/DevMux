"""日志控制通道的生产数量、持久恢复、故障边界及真实节点往返（假设备）。"""
import concurrent.futures
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
import relay_journal as journal
import relay_node
from tests.test_relay_node import _make_node, _wait_until, _fake_list_devices, _fake_execute


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adb-journal-')
        self.root = Path(self.temp.name)
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.temp.cleanup()

    def runtime(self, name):
        runtime = journal.Runtime(self.root / 'local' / (name + '.sqlite3'), name)
        self.runtimes.append(runtime)
        return runtime

    def queues(self):
        sender, receiver = self.runtime('source'), self.runtime('target')
        root = str(self.root / 'share')
        return (journal.JournalQueue(root, 'slave', sender, sending=True),
                journal.JournalQueue(root, 'slave', receiver))

    def request(self):
        return relay.new_request(relay.KIND_CHECK_CONNECTION, 'master',
                                 payload={'sourceNodeId': 'source', 'targetNodeId': 'target'})

    def test_production_file_count_and_concurrent_roundtrip(self):
        outgoing, incoming = self.queues()
        requests = [self.request() for _ in range(1000)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(outgoing.submit, requests))
        pending = incoming.pending_requests()
        self.assertEqual({r.reqId for _, r in pending}, {r.reqId for r in requests})
        for path, request in pending:
            self.assertTrue(incoming.claim_request(path, request))
            incoming.write_result(relay.Result(reqId=request.reqId, status='ok', stdout='中文回执'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda r: outgoing.read_result(r.reqId), requests))
        self.assertTrue(all(r and r.stdout == '中文回执' for r in results))
        files = list((self.root / 'share').rglob('*'))
        files = [p for p in files if p.is_file()]
        self.assertLessEqual(len(files), 8, [str(p) for p in files])
        self.assertFalse(any(p.name.startswith('r-') or p.name.startswith('.from-')
                             or p.name == '.replies.jsonl' for p in files))
        self.assertFalse(incoming.pending_requests())
        print(f'  [INFO] 1000次并发请求/回执，共享文件{len(files)}个')

    def test_partial_record_checksum_and_restart_cursor(self):
        outgoing, incoming = self.queues()
        request = self.request()
        directory = Path(outgoing.requests_dir) / 'journal-v1' / 'source'
        writer = outgoing.runtime.writer(directory)
        path = writer._path(0)
        line = journal._encode(request.to_dict())
        path.write_bytes(line[:len(line)//2])
        self.assertFalse(incoming.pending_requests())
        with path.open('ab') as stream:
            stream.write(line[len(line)//2:])
        self.assertEqual(incoming.pending_requests()[0][1].reqId, request.reqId)
        incoming.runtime.close()
        restored = self.runtime('target')
        incoming = journal.JournalQueue(str(self.root / 'share'), 'slave', restored)
        pending = incoming.pending_requests()
        self.assertEqual(len(pending), 1)
        self.assertTrue(incoming.claim_request(*pending[0]))
        self.assertFalse(incoming.claim_request(*pending[0]))
        with path.open('ab') as stream:
            stream.write(b'{"body":"{}","sha256":"wrong"}\n')
        incoming.pending_requests()
        self.assertTrue(any('摘要' in item for item in incoming.runtime.notices.values()))

    def test_crash_running_does_not_reexecute_and_completed_reply_survives(self):
        outgoing, incoming = self.queues()
        request = self.request()
        outgoing.submit(request)
        path, body = incoming.pending_requests()[0]
        self.assertTrue(incoming.claim_request(path, body))
        incoming.runtime.close()
        runtime = self.runtime('target')
        restored = journal.JournalQueue(str(self.root / 'share'), 'slave', runtime)
        self.assertFalse(restored.pending_requests())
        reply = outgoing.read_result(request.reqId)
        self.assertTrue(reply.extra['outcomeUnknown'])
        outgoing.runtime.close()
        sender = self.runtime('source')
        outgoing = journal.JournalQueue(str(self.root / 'share'), 'slave', sender, sending=True)
        self.assertTrue(outgoing.read_result(request.reqId).extra['outcomeUnknown'])

    def test_completed_result_publish_failure_retries_original_result(self):
        outgoing, incoming = self.queues()
        request = self.request()
        outgoing.submit(request)
        path, body = incoming.pending_requests()[0]
        incoming.claim_request(path, body)
        with patch.object(journal.Writer, 'append', side_effect=OSError('共享暂不可写')):
            incoming.write_result(relay.Result(reqId=request.reqId, status='ok', stdout='只执行一次'))
        self.assertIsNone(outgoing.read_result(request.reqId))
        incoming.runtime.close()
        restored = journal.JournalQueue(str(self.root / 'share'), 'slave', self.runtime('target'))
        self.assertFalse(restored.pending_requests())
        self.assertEqual(outgoing.read_result(request.reqId).stdout, '只执行一次')

    def test_rotation_ack_collection_and_backpressure(self):
        directory = self.root / 'segments'
        writer = journal.Writer(directory, 'a' * 32, segment_bytes=1, max_segments=2)
        writer.append({'value': 1})
        writer.append({'value': 2})
        before = {p.name: p.read_bytes() for p in directory.glob('*.jsonl')}
        with self.assertRaises(OSError):
            writer.append({'value': 3})
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.glob('*.jsonl')})
        runtime = self.runtime('reader')
        received = []
        runtime.consume(directory, received.append)
        self.assertEqual(received, [{'value': 1}, {'value': 2}])
        writer.append({'value': 3})
        runtime.consume(directory, received.append)
        self.assertEqual(received[-1], {'value': 3})
        self.assertLessEqual(len(list(directory.glob('*.jsonl'))), 2)

    def test_directory_cache_missing_middle_segment_never_acknowledges_past_gap(self):
        directory = self.root / 'gap'
        writer = journal.Writer(directory, 'b' * 32, segment_bytes=1)
        writer.append({'n': 0})
        writer.append({'n': 1})
        first = writer._path(0)
        backup = first.read_bytes()
        first.unlink()
        runtime = self.runtime('reader')
        received = []
        with self.assertRaises(OSError):
            runtime.consume(directory, received.append)
        self.assertFalse(received)
        self.assertFalse(list(directory.glob('*.ack')))
        first.write_bytes(backup)
        runtime.consume(directory, received.append)
        self.assertEqual(received, [{'n': 0}, {'n': 1}])

    def test_ack_write_failure_retries_without_reinserting_request(self):
        outgoing, incoming = self.queues()
        request = self.request()
        outgoing.submit(request)
        with patch.object(relay, 'atomic_write_json', side_effect=OSError('确认不可写')):
            incoming.pending_requests()
            self.assertTrue(incoming.runtime.notices)
        self.assertEqual(len(incoming.pending_requests()), 1)
        count = incoming.runtime.db.execute('SELECT count(*) FROM inbox').fetchone()[0]
        self.assertEqual(count, 1)

    def test_write_failure_disables_writer_without_overwriting_partial(self):
        writer = journal.Writer(self.root / 'failed', 'f' * 32)
        with patch.object(os, 'fsync', side_effect=OSError('写失败')):
            with self.assertRaises(OSError):
                writer.append({'value': 1})
        before = writer._path(0).read_bytes()
        with self.assertRaises(OSError):
            writer.append({'value': 2})
        self.assertEqual(writer._path(0).read_bytes(), before)

    def test_same_instance_exclusive_and_distinct_instances_independent(self):
        self.runtime('same')
        with self.assertRaises(OSError):
            self.runtime('same')
        self.runtime('other')

    def test_abrupt_process_exit_after_side_effect_never_reexecutes(self):
        sender = self.runtime('source')
        outgoing = journal.JournalQueue(str(self.root / 'share'), 'slave', sender, sending=True)
        request = self.request()
        outgoing.submit(request)
        script = '''
import os,sys
from pathlib import Path
import relay_journal as journal
root=Path(sys.argv[1])
runtime=journal.Runtime(root/'local'/'child.sqlite3','target')
queue=journal.JournalQueue(str(root/'share'),'slave',runtime)
path,request=queue.pending_requests()[0]
assert queue.claim_request(path,request)
(root/'side-effect.txt').write_text('once',encoding='utf-8')
os._exit(17)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                cwd=ROOT, capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 17, result.stderr.decode('utf-8', errors='replace'))
        restored = journal.Runtime(self.root / 'local' / 'child.sqlite3', 'target')
        self.runtimes.append(restored)
        incoming = journal.JournalQueue(str(self.root / 'share'), 'slave', restored)
        self.assertFalse(incoming.pending_requests())
        self.assertEqual((self.root / 'side-effect.txt').read_text(encoding='utf-8'), 'once')
        self.assertTrue(outgoing.read_result(request.reqId).extra['outcomeUnknown'])

    def test_real_nodes_negotiate_and_legacy_fallback(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                root = self.root / ('legacy' if legacy else 'new')
                root.mkdir()
                master = _make_node(str(root), 'master', relay.ROLE_MASTER, 'node-a')
                slave = _make_node(str(root), 'slave', relay.ROLE_SLAVE, 'node-b', control_journal=not legacy)
                with patch.object(relay_node, 'list_devices', _fake_list_devices), patch.object(relay_node, 'execute', _fake_execute):
                    try:
                        self.assertTrue(master.start()[0])
                        self.assertTrue(slave.start()[0])
                        self.assertTrue(_wait_until(lambda: 'slave' in master._peers and 'master' in slave._peers))
                        master.confirm_peer('slave')
                        slave.confirm_peer('master')
                        self.assertTrue(_wait_until(lambda: not master._peer_offline_reason('slave')
                                                    and slave.refresh().state == relay.STATE_CONNECTED))
                        result = master.submit('slave', relay.KIND_EXEC, payload={'argv': ['shell', 'echo', 'hello']},
                                               device={'key': 'devB:tid=1'}, timeout=8)
                        self.assertTrue(result.ok, result.to_dict())
                        files = list((root / 'share').rglob('*'))
                        self.assertEqual(any(p.name.endswith('.jsonl') and 'journal-v1' in p.parts for p in files), not legacy)
                        if not legacy:
                            # 发送可能已完整写入后才报错，主控必须给出不确定结果且不降级重发。
                            before = list(Path(master.queue.archive_dir).rglob('r-*.json'))
                            with patch.object(journal.Writer, 'append', side_effect=OSError('写入结果未知')):
                                failed = master.submit('slave', relay.KIND_CHECK_CONNECTION, timeout=1)
                            self.assertTrue(failed.extra.get('outcomeUnknown'))
                            self.assertEqual(list(Path(master.queue.archive_dir).rglob('r-*.json')), before)
                    finally:
                        master.stop()
                        slave.stop()


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(JournalTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
