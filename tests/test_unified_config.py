"""统一配置迁移、隔离、故障及并发验证；全部使用临时配置与假工具。"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
import relay_app
from relay_config_store import ConfigStore
from serial_terminal import SerialService
from usb_relay import USBRelayService
from db_flash_extension import DBFlashExtension


class UnifiedConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adb-unified-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'relay.json'
        self.store = ConfigStore(self.path)
        relay_app.save_config(str(self.path), relay_app.RelayConfig(
            peerId='slave', nodeId='node-slave', role='slave'))

    def legacy(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def section(self, group, name, legacy=None):
        return self.store.section(group, name, legacy or self.root/'missing.json')

    def test_unscoped_legacy_pairing_is_not_imported_into_root_scope(self):
        pairs = {'schema': 1, 'pairs': {'master-node': {
            'nodeId': 'master-node', 'peerId': 'master', 'state': 'confirmed',
            'connectionId': 'current-generation', 'retiredConnectionIds': ['old-generation']}}}
        trust = {'schema': 1, 'peers': {'master': {'peerId': 'master', 'fingerprint': 'fp', 'role': 'master'}}}
        source = self.legacy('relay-pairing-node-slave.json', pairs)
        self.legacy('relay-trust-slave.json', trust)
        ctrl = relay_app.RelayController(config_path=str(self.path), local_devices_enabled=False)
        self.assertIsNone(ctrl._pairing_store().get('master-node'))
        self.assertEqual(ctrl._trust_storage_for('slave').read(), trust)
        self.assertNotIn('pairings', self.store.read().get('private', {}))
        self.assertEqual(ctrl.config.nodeId, 'node-slave')
        self.assertEqual(source.read_text(encoding='utf-8'), json.dumps(pairs, ensure_ascii=False))

    def test_revocation_and_empty_records_never_reimport_legacy(self):
        source = self.legacy('pairs.json', {'schema': 1, 'pairs': {'master-node': {'state': 'confirmed'}}})
        storage = self.section('pairings', 'node-slave', source)
        store = relay.PairingStore(str(source), storage=storage)
        self.assertTrue(store.revoke('master-node'))
        restarted = relay.PairingStore(str(source), storage=self.section('pairings', 'node-slave', source))
        self.assertFalse(restarted.is_paired('master-node'))
        storage.read()
        storage.write({'schema': 1, 'pairs': {}})
        self.assertEqual(self.section('pairings', 'node-slave', source).read()['pairs'], {})

    def test_parallel_updates_preserve_sections_and_unknown_fields(self):
        self.store.update_config({'futureField': {'keep': 7}})
        def write_tool(tool):
            section = self.section('tools', tool)
            for counter in range(25):
                section.write({'remoteAllowed': bool(counter % 2), 'counter': counter})
        def write_config():
            for counter in range(25):
                cfg = relay_app.load_config(str(self.path))
                cfg.poolId = 'pool-' + str(counter)
                self.assertTrue(relay_app.save_config(str(self.path), cfg))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write_tool, tool) for tool in ('serial', 'usb-relay', 'db-flash')]
            futures.append(pool.submit(write_config))
            for future in futures:
                future.result()
        data = self.store.read()
        self.assertEqual(data['futureField'], {'keep': 7})
        self.assertEqual(data['poolId'], 'pool-24')
        self.assertTrue(all(v['counter'] == 24 for v in data['private']['tools'].values()))
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_corrupt_config_and_legacy_are_preserved(self):
        for content in ('{broken', '[]', '{"private": []}', '{"storageVersion": 99}'):
            self.path.write_text(content, encoding='utf-8')
            with self.assertRaises(OSError):
                self.store.update_config({'peerId': 'replacement'})
            self.assertEqual(self.path.read_text(encoding='utf-8'), content)
        self.path.unlink()
        source = self.root/'old.json'
        source.write_text('{broken', encoding='utf-8')
        with self.assertRaises(OSError):
            self.section('tools', 'serial', source).read()
        self.assertFalse(self.path.exists())

    def test_write_failure_does_not_confirm_in_memory_or_touch_source(self):
        storage = self.section('pairings', 'node-slave')
        pairs = relay.PairingStore(str(self.root/'missing.json'), storage=storage)
        pairs.items()
        before = self.path.read_bytes()
        with patch('relay_config_store.os.replace', side_effect=OSError('拒绝写入')):
            with self.assertRaises(OSError):
                pairs.confirm('master-node', peer_id='master')
        self.assertFalse(pairs.is_paired('master-node'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_stale_same_section_cannot_undo_revoke(self):
        source = self.root/'missing.json'
        a = relay.PairingStore(str(source), storage=self.section('pairings', 'node-slave'))
        a.confirm('master-node')
        b = relay.PairingStore(str(source), storage=self.section('pairings', 'node-slave'))
        b.items()
        a.revoke('master-node')
        with self.assertRaises(OSError):
            b.confirm('other-node')
        self.assertFalse(b.is_paired('master-node'))

    def test_tools_migrate_and_jobs_remain_separate(self):
        serial_path = self.legacy('relay-serial.json', {'remoteAllowed': True})
        usb_path = self.legacy('relay-usb-relay.json', {'toolDir': 'C:/继电器', 'remoteAllowed': True})
        db_path = self.legacy('relay-db-flash.json', {'toolDir': 'C:/烧写', 'remoteAllowed': True})
        journal = self.legacy('relay-db-flash-jobs.json', {'a'*32: {'owner': 'master-node', 'jobId': 'existing-job'}})
        before = journal.read_bytes()
        serial = SerialService(serial_path, config_storage=self.section('tools', 'serial', serial_path))
        self.addCleanup(serial.close)
        usb = USBRelayService(usb_path, config_storage=self.section('tools', 'usb-relay', usb_path))
        db = DBFlashExtension(None, db_path, config_storage=self.section('tools', 'db-flash', db_path))
        self.assertTrue(serial.remote_allowed and usb.remote_allowed and db.remote_allowed)
        self.assertEqual(db.starts['a'*32]['jobId'], 'existing-job')
        serial.set_remote_permission(False)
        usb.set_remote_permission(False)
        db.set_remote_permission(False)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(db.journal_path, journal)
        self.assertNotIn('existing-job', self.path.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(serial_path.read_text())['remoteAllowed'], True)

    def test_instance_and_identity_sections_are_isolated(self):
        for peer in ('slave', 'slave-2'):
            self.section('trust', peer).write({'schema': 1, 'peers': {peer: {}}})
        second = ConfigStore(self.root/'relay-instance-2.json')
        second.section('tools', 'serial', self.root/'missing2.json').write({'remoteAllowed': True})
        self.assertNotIn('tools', self.store.read()['private'])
        self.assertEqual(set(self.store.read()['private']['trust']), {'slave', 'slave-2'})

    def test_migrated_pair_reconnects_without_confirmation(self):
        from tests.test_relay_app import (_Harness, _patch, _fake_list_devices,
                                         _fake_execute, _wait_until)
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness = _Harness(str(self.root/'link'/'share'), master_local_devices_enabled=False)
            sources = []
            root = relay.resolve_pool_root(harness.master.config.root, harness.master.config.poolId)
            canonical_root = os.path.normcase(os.path.realpath(os.path.abspath(root)))
            generation = hashlib.sha256(canonical_root.encode('utf-8')).hexdigest()[:16] + '.preserved-generation'
            for controller, node_id, other_node, other_peer in (
                    (harness.master, 'node-master', 'node-slave', 'pc-b'),
                    (harness.slave, 'node-slave', 'node-master', 'pc-a')):
                controller.config.nodeId = node_id
                self.assertTrue(relay_app.save_config(controller.config_path, controller.config))
                pair_path = controller._pairing_path_for(node_id)
                pairs = relay.PairingStore(pair_path)
                pairs.confirm(other_node, peer_id=other_peer, fingerprint='fp-' + other_peer,
                              host=other_peer, connection_id='retired-generation')
                pairs.confirm(other_node, peer_id=other_peer, fingerprint='fp-' + other_peer,
                              host=other_peer, connection_id=generation)
                trust_path = controller._trust_path_for(controller.config.peerId)
                relay.TrustStore(trust_path).confirm(other_peer, fingerprint='fp-' + other_peer,
                    role='slave' if other_peer == 'pc-b' else 'master')
                sources.extend((Path(pair_path), Path(trust_path)))
            try:
                harness.start()
                self.assertTrue(_wait_until(lambda: bool(harness.master.devices()) and any(
                    session['opened'] for session in harness.master.sessions_snapshot()), 12))
                for controller, other_node in ((harness.master, 'node-slave'), (harness.slave, 'node-master')):
                    record = controller.node.pairing.get(other_node)
                    self.assertEqual(record.connectionId, generation)
                    self.assertIn('retired-generation', record.retiredConnectionIds)
                    self.assertIn('private', ConfigStore(controller.config_path).read())
                harness.stop()
                # 已迁移小节不再读取旧来源，即便旧来源后来损坏也不会覆盖新记录。
                for source in sources:
                    source.write_text('{broken', encoding='utf-8')
                harness.start()
                self.assertTrue(_wait_until(lambda: any(item.get('connectionState') == 'connected'
                    for item in harness.master.status()['pairing']), 12))
                self.assertEqual(harness.slave.node.pairing.get('node-master').connectionId, generation)
                self.assertTrue(harness.master.unpair_peer('node-slave')[0])
                self.assertFalse(harness.master.node.pairing.is_paired('node-slave'))
            finally:
                harness.stop()

    def test_actual_manager_injects_storage_and_reloads_permissions(self):
        import server
        from tests.test_db_flash_server import EmptyWorker
        serial_path = self.legacy('relay-serial.json', {'remoteAllowed': True})
        self.legacy('relay-usb-relay.json', {'toolDir': 'C:/继电器', 'remoteAllowed': True})
        self.legacy('relay-db-flash.json', {'toolDir': 'C:/烧写', 'remoteAllowed': True})
        with patch.dict(os.environ, {'RELAY_CONFIG': str(self.path)}), patch.object(
                server.ManagerServer, '_new_worker', return_value=EmptyWorker()):
            first = server.ManagerServer()
            try:
                self.assertTrue(first.serial.remote_allowed)
                self.assertFalse(hasattr(first, 'usb_relay'))
                self.assertFalse(hasattr(first, 'db_tool'))
                self.assertNotIn('relay', first.extension_tools.services)
                self.assertNotIn('db-flash', first.extension_tools.services)
                first.serial.set_remote_permission(False)
            finally:
                first.serial.close()
                first.windows.close()
                first._executor.shutdown(wait=True)
            second = server.ManagerServer()
            try:
                self.assertFalse(second.serial.remote_allowed)
                self.assertEqual(second.plugins.store.list(), [])
                self.assertTrue(json.loads((self.root/'relay-db-flash.json').read_text(encoding='utf-8'))['remoteAllowed'])
                self.assertEqual(json.loads(serial_path.read_text())['remoteAllowed'], True)
            finally:
                second.serial.close()
                second.windows.close()
                second._executor.shutdown(wait=True)

    def test_invalid_section_and_failed_migration_keep_legacy(self):
        source = self.legacy('pairs.json', {'schema': 1, 'pairs': []})
        before = self.path.read_bytes()
        with self.assertRaises(OSError):
            self.section('pairings', 'node-slave', source).read()
        self.assertEqual(before, self.path.read_bytes())
        source.write_text('{"schema": 1, "pairs": {}}', encoding='utf-8')
        with patch('relay_config_store.os.replace', side_effect=OSError('拒绝写入')):
            with self.assertRaises(OSError):
                self.section('pairings', 'node-slave', source).read()
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(source.exists())

    def test_corrupt_unscoped_pairing_does_not_affect_root_start(self):
        config = relay_app.load_config(str(self.path))
        config.enabled = True
        config.root = str(self.root/'never-created-share')
        relay_app.save_config(str(self.path), config)
        source = self.root/'relay-pairing-node-slave.json'
        source.write_text('{broken', encoding='utf-8')
        controller = relay_app.RelayController(config_path=str(self.path), local_devices_enabled=False)
        ok, reason = controller.start()
        self.assertTrue(ok, reason)
        self.assertIsNotNone(controller.node)
        self.assertTrue(Path(config.root).exists())
        self.assertIsNone(controller.node.pairing.get('master-node'))
        self.assertEqual(source.read_text(), '{broken')
        controller.stop()

    def test_explicit_paths_remain_independent(self):
        source = self.legacy('external-pairs.json', {'schema': 1, 'pairs': {}})
        ctrl = relay_app.RelayController(config_path=str(self.path), pairing_path=str(source), trust_path=str(self.root/'external-trust.json'))
        self.assertIsNone(ctrl._trust_storage_for('slave'))
        ctrl._pairing_store().confirm('master-node')
        self.assertIn('master-node', json.loads(source.read_text())['pairs'])
        self.assertNotIn('private', self.store.read())


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(UnifiedConfigTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
