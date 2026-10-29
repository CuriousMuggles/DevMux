"""自动合并已下线：只验证历史包去重兼容，不生成生产合并任务。"""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_archive as archive
from relay_node import RelayNode


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-legacy-archive-')
        self.addCleanup(self.temp.cleanup)
        self.queue = relay.RelayQueue.for_peer(self.temp.name, 'slave')
        self.queue.ensure_layout()
        self.month = Path(self.queue.archive_dir) / '2026-10'
        self.month.mkdir()
        archive._CACHE.clear()
        archive._BUNDLE_CACHE.clear()

    def bundle(self, records):
        path = self.month / ('compact-' + 'a' * 32 + '.zip')
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('__index__.json', json.dumps({'schema': 1, 'records': records}))
            package.writestr('records/unused.json', b'x' * 100000)
        return path

    def test_legacy_result_survives_restart_and_no_message_decompression(self):
        req_id = 'a' * 16
        package = self.bundle([{'reqId': req_id, 'result': True}])
        before = package.read_bytes()
        original = zipfile.ZipFile.read
        reads = []
        def read_only_index(instance, name, *args, **kwargs):
            reads.append(name)
            if name != '__index__.json':
                raise AssertionError('去重不应解压消息正文')
            return original(instance, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'read', read_only_index):
            restarted = relay.RelayQueue.for_peer(self.temp.name, 'slave')
            self.assertTrue(restarted.has_result(req_id))
            self.assertTrue(restarted.has_result(req_id))
        self.assertEqual(reads, ['__index__.json'])
        self.assertEqual(package.read_bytes(), before)
        self.assertFalse(restarted.has_result('b' * 16))

    def test_request_only_record_is_not_a_completed_result(self):
        self.bundle([{'reqId': 'c' * 16, 'result': False}])
        self.assertFalse(self.queue.has_result('c' * 16))

    def test_corrupt_index_fails_closed(self):
        package = self.month / ('compact-' + 'a' * 32 + '.zip')
        package.write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, '归档去重索引不可用'):
            self.queue.has_result('d' * 16)

    def test_normal_message_archive_still_prevents_replay(self):
        result = relay.Result(reqId='e' * 16, status=relay.STATUS_OK)
        path = self.queue.write_result(result)
        self.assertTrue(self.queue.archive_file(path))
        self.assertFalse(os.path.exists(path))
        restarted = relay.RelayQueue.for_peer(self.temp.name, 'slave')
        self.assertTrue(restarted.has_result(result.reqId))

    def test_node_has_no_compaction_executor(self):
        node = RelayNode(peer_id='slave', role=relay.ROLE_SLAVE,
                         root=self.temp.name, local_devices_enabled=False)
        try:
            self.assertFalse(hasattr(node, '_maintenance_pool'))
            self.assertFalse(hasattr(node, '_schedule_archive_maintenance'))
            self.assertFalse(hasattr(archive, 'compact_archive'))
        finally:
            node.stop()


def run():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ArchiveTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
