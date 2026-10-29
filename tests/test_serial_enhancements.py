"""串口优化：持久化及真实共享目录XMODEM回归，硬件为软件字节端口。"""
import asyncio
import base64
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server
import xmodem_transfer as xm
from tests.test_serial_remote import RemoteTests
from tests.test_serial_xmodem import Wire


class PersistenceTests(unittest.TestCase):
    def test_flat_legacy_adb_commands_survive_shared_store_upgrade(self):
        flat = [{'name':'旧版设备指令','cmd':'adb devices'}]
        grouped = [{'name':'默认','commands':flat}]
        serial = [{'name':'串口','commands':[{'name':'识别','cmd':'ATI'}]}]
        for original in (flat, {'adb':flat, 'serial':None}):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)/'commands.json'
                path.write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
                with patch.object(server.ManagerServer,'_commands_path',return_value=str(path)):
                    self.assertEqual(server.ManagerServer._load_commands(),grouped)
                    server.ManagerServer._serial_commands(serial)
                    self.assertEqual(server.ManagerServer._load_commands(),grouped)
                    self.assertEqual(json.loads(path.read_text('utf-8')),{'adb':grouped,'serial':serial})

    def test_command_scopes_share_store_and_migrate_serial_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'commands.json'
            adb = [{'name':'默认ADB', 'commands':[{'name':'设备','cmd':'adb devices'}]}]
            serial = [{'name':'默认串口', 'commands':[{'name':'识别','cmd':'ATI\r'}]}]
            path.write_text(json.dumps(adb, ensure_ascii=False), encoding='utf-8')
            legacy = Path(temporary)/'serial-commands.json'
            legacy.write_text(json.dumps(serial, ensure_ascii=False), encoding='utf-8')
            original = legacy.read_bytes()
            with patch.object(server.ManagerServer, '_commands_path', return_value=str(path)):
                self.assertEqual(server.ManagerServer._load_commands(), adb)
                self.assertEqual(server.ManagerServer._serial_commands(), serial)
                stored=json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(stored, {'adb':adb,'serial':serial})
                self.assertEqual(legacy.with_name('serial-commands.json.migrated').read_bytes(),original)
                next_serial=[{'name':'新版串口','commands':[{'name':'测试','cmd':'AT+TEST'}]}]
                server.ManagerServer._serial_commands(next_serial)
                self.assertEqual(server.ManagerServer._load_commands(), adb)
                self.assertEqual(server.ManagerServer._serial_commands(), next_serial)
                with self.assertRaises(ValueError): server.ManagerServer._serial_commands([])
                self.assertFalse(list(Path(temporary).glob('*.tmp')))
            from agent_extensions import describe
            from serial_terminal import SerialService
            schema = describe('serial', SerialService.ACTIONS)
            self.assertIn('requestId', schema['xmodem-start']['inputSchema']['required'])
            self.assertFalse(schema['xmodem-upload']['inputSchema']['additionalProperties'])


class RemoteXmodemTests(RemoteTests):
    async def test_xmodem_remote_both_directions_and_revocation(self):
        wire = Wire()
        self.slave.connector = lambda *_: wire
        self.registry.call('serial', 'permission', {'allowed':True})
        opened = await self.gui('open', {'port':'COM3','requestId':uuid.uuid4().hex})
        sid = opened['sessionId']
        peers = []
        async def task(action, tid, **args):
            return await self.gui('xmodem-'+action, {'sessionId':sid,'transferId':tid, **args})
        async def finished(tid):
            for _ in range(300):
                state = (await self.gui('read', {'sessionId':sid, 'cursor':0}))['xmodem']
                if state and state['transferId'] == tid and state['state'] not in xm.ACTIVE:
                    return state
                await asyncio.sleep(.02)
            raise AssertionError('远程传输等待超时')
        try:
            with patch.multiple(xm, TIMEOUT=.2, HANDSHAKE=1):
                for direction in ('send', 'receive'):
                    generation = (await self.gui('read', {'sessionId':sid,'cursor':0}))['generation']
                    raw = bytes(range(256))*7+b'\x1a'
                    args = dict(sessionId=sid, generation=generation, requestId=uuid.uuid4().hex,
                        direction=direction, size=len(raw) if direction=='send' else 0, blockSize=1024, checksum='crc')
                    started = await self.gui('xmodem-start', args); tid = started['xmodem']['transferId']
                    self.assertEqual((await self.gui('xmodem-start', args))['xmodem']['transferId'], tid)
                    if direction=='send':
                        await task('upload', tid, offset=0, data=base64.b64encode(raw).decode())
                    peer = xm.Transfer('receive' if direction=='send' else 'send', len(raw), 1024, 'crc', uuid.uuid4().hex)
                    peers.append(peer); wire.peer = peer
                    if direction=='receive': peer.upload(0,raw)
                    import threading
                    peer.thread = threading.Thread(target=lambda:peer.execute(lambda data,**_:wire.rx.put(data)), daemon=True)
                    await task('run', tid); peer.thread.start()
                    self.assertEqual((await finished(tid))['state'], 'succeeded')
                    peer.thread.join(2)
                    self.assertEqual(peer.state, 'succeeded', peer.error)
                    if direction=='send': peer.file.seek(0); received=peer.file.read()
                    else: received=base64.b64decode((await task('download', tid, offset=0))['data'])
                    self.assertEqual(received, raw.ljust(2048,b'\x1a'))
                    self.assertEqual((await self.gui('read', {'sessionId':sid,'cursor':0}))['data'], '')
                generation=(await self.gui('read', {'sessionId':sid,'cursor':0}))['generation']
                waiting=await self.gui('xmodem-start',dict(sessionId=sid,generation=generation,
                    requestId=uuid.uuid4().hex,direction='receive'))
                tid=waiting['xmodem']['transferId']; await task('run',tid)
                handle=self.slave.sessions[sid]; transfer=handle['slot']['transfer']
                self.registry.call('serial','permission',{'allowed':False})
                for _ in range(100):
                    if transfer.file.closed: break
                    await asyncio.sleep(.02)
                self.assertTrue(transfer.file.closed)
                self.assertTrue(handle['closed']);self.assertTrue(wire.closed)
        finally:
            for peer in peers:
                peer.cancel.set()
                if peer.thread: peer.thread.join(2)
                peer.dispose()


def run():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PersistenceTests))
    suite.addTest(RemoteXmodemTests('test_xmodem_remote_both_directions_and_revocation'))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
