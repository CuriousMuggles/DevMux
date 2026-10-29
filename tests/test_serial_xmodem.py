"""XMODEM软件协议与服务回归：无实体COM。"""
import base64
import io
import queue
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import xmodem_transfer as xm
from extension_tools import ExtensionTools
from serial_terminal import SerialService
from tests.test_serial_terminal import wait


class Wire:
    def __init__(self):
        self.rx = queue.Queue()
        self.closed = False
        self.peer = None
        self.writes = []

    def read(self, size):
        if self.closed:
            raise OSError('拔出')
        try:
            return self.rx.get(timeout=.01)
        except queue.Empty:
            return b''

    def write(self, data):
        if self.closed:
            raise OSError('已关闭')
        self.writes.append(data)
        if self.peer:
            self.peer.feed(data)
        return len(data)

    def close(self):
        self.closed = True


class XmodemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wire = Wire()
        self.service = SerialService(Path(self.temp.name)/'serial.json',
            port_source=lambda: [{'port': 'COM3'}], connector=lambda *_: self.wire)
        self.registry = ExtensionTools({'serial': self.service})
        self.opened = self.call('open', port='COM3', requestId=uuid.uuid4().hex)
        self.sid = self.opened['sessionId']
        self.peers = []
        self.timeout = patch.multiple(xm, TIMEOUT=.15, HANDSHAKE=.5)
        self.timeout.start()

    def tearDown(self):
        self.service.close()
        for peer in self.peers:
            peer.cancel.set()
            peer.thread.join(2)
            peer.dispose()
        for transfer in [h['slot'].get('transfer') for h in self.service.sessions.values()]:
            if transfer and transfer.thread:
                transfer.thread.join(2)
                transfer.dispose()
        self.timeout.stop()
        self.temp.cleanup()

    def call(self, action, **args):
        if hasattr(self, 'sid') and action != 'open':
            args.setdefault('sessionId', self.sid)
        return self.registry.call('serial', action, args, caller='gui')['data']

    def start(self, direction='send', data=b'abc', block=128, checksum='crc'):
        self.arguments = dict(generation=self.call('status')['generation'], requestId=uuid.uuid4().hex,
            direction=direction, size=len(data) if direction == 'send' else 0,
            blockSize=block, checksum=checksum)
        result = self.call('xmodem-start', **self.arguments)
        self.tid = result['xmodem']['transferId']
        if direction == 'send':
            for offset in range(0, len(data), xm.CHUNK):
                self.call('xmodem-upload', transferId=self.tid, offset=offset,
                    data=base64.b64encode(data[offset:offset+xm.CHUNK]).decode())
        return result

    def task(self, action, **args):
        return self.call('xmodem-'+action, transferId=self.tid, **args)

    def finished(self):
        wait(lambda: self.task('status')['xmodem']['state'] not in xm.ACTIVE, 5)
        return self.task('status')['xmodem']

    def peer(self, direction, data=b'', block=128, checksum='crc'):
        peer = xm.Transfer(direction, len(data), block, checksum, uuid.uuid4().hex)
        if data:
            peer.upload(0, data)
        self.wire.peer = peer
        def run():
            peer.state = 'waiting'
            peer.execute(lambda raw, **_: self.wire.rx.put(raw))
        peer.thread = threading.Thread(target=run, daemon=True)
        self.peers.append(peer)
        return peer

    def test_crc_vector_and_all_modes_roundtrip(self):
        self.assertEqual(xm.trailer(b'123456789', True), b'\x31\xc3')
        self.assertEqual(xm.trailer(bytes([255, 2]), False), b'\x01')
        for block, checksum in [(128, 'crc'), (128, 'checksum'), (1024, 'crc')]:
            with self.subTest(block=block, checksum=checksum):
                raw = bytes(range(256))*140 + b'\x00\x1a'
                self.start(data=raw, block=block)
                peer = self.peer('receive', checksum=checksum)
                self.task('run'); peer.thread.start()
                self.assertEqual(self.finished()['state'], 'succeeded')
                peer.thread.join(2)
                self.assertEqual(peer.state, 'succeeded', peer.error)
                peer.file.seek(0)
                self.assertEqual(peer.file.read(), raw.ljust((len(raw)+block-1)//block*block, b'\x1a'))
                self.assertEqual(self.call('status')['nextCursor'], 0, '协议字节不得混入终端')
                self.assertEqual(self.task('run')['xmodem']['state'], 'succeeded')

    def test_receive_download_padding_and_duplicate_upload(self):
        raw = bytes(range(256))*4 + b'\x1a'
        self.start('receive')
        peer = self.peer('send', raw, block=1024)
        peer.thread.start(); self.task('run')
        self.assertEqual(self.finished()['state'], 'succeeded')
        result = self.task('download', offset=0)
        self.assertTrue(result['eof'])
        self.assertEqual(base64.b64decode(result['data']), raw.ljust(2048, b'\x1a'))
        self.assertEqual(self.call('status')['nextCursor'], 0)
        self.start(data=b'abc')
        self.task('upload', offset=0, data='YWJj')
        with self.assertRaises(ValueError): self.task('upload', offset=0, data='QUJD')
        with self.assertRaises(ValueError): self.task('upload', offset=9, data='YQ==')

    def test_scripted_retransmit_and_two_eot(self):
        self.start(data=b'ABC')
        self.task('run'); self.wire.rx.put(b'C')
        wait(lambda: any(len(raw) == 133 for raw in self.wire.writes))
        packet = next(raw for raw in self.wire.writes if len(raw) == 133)
        self.assertEqual(packet[:6], b'\x01\x01\xfeABC')
        self.wire.rx.put(bytes([xm.NAK]))
        wait(lambda: self.wire.writes.count(packet) >= 2)
        self.wire.rx.put(bytes([xm.ACK]))
        wait(lambda: bytes([xm.EOT]) in self.wire.writes)
        self.wire.rx.put(bytes([xm.NAK]))
        wait(lambda: self.wire.writes.count(bytes([xm.EOT])) >= 2)
        self.wire.rx.put(bytes([xm.ACK]))
        self.assertEqual(self.finished()['state'], 'succeeded')
        self.assertGreaterEqual(self.finished()['retries'], 2)

    def test_receive_crc_error_duplicate_and_fragment(self):
        self.start('receive'); self.task('run')
        wait(lambda: b'C' in self.wire.writes)
        data = b'Z'*128
        packet = b'\x01\x01\xfe' + data + xm.trailer(data, True)
        self.wire.rx.put(packet[:-1] + bytes([packet[-1] ^ 1]))
        wait(lambda: bytes([xm.NAK]) in self.wire.writes)
        self.wire.rx.put(packet[:20]); self.wire.rx.put(packet[20:])
        wait(lambda: bytes([xm.ACK]) in self.wire.writes)
        self.wire.rx.put(packet)
        wait(lambda: self.wire.writes.count(bytes([xm.ACK])) >= 2)
        self.wire.rx.put(bytes([xm.EOT]))
        self.assertEqual(self.finished()['state'], 'succeeded')
        self.assertEqual(base64.b64decode(self.task('download')['data']), data)

    def test_receive_keeps_prompt_after_eot(self):
        self.start('receive'); self.task('run')
        wait(lambda: b'C' in self.wire.writes)
        payload = b'P'*128
        self.wire.rx.put(b'\x01\x01\xfe'+payload+xm.trailer(payload,True))
        wait(lambda: bytes([xm.ACK]) in self.wire.writes)
        self.wire.rx.put(bytes([xm.EOT])+b'\r\nDEVICE# ')
        self.assertEqual(self.finished()['state'], 'succeeded')
        wait(lambda: self.call('status')['nextCursor'] == 10)
        self.assertEqual(base64.b64decode(self.call('read')['data']), b'\r\nDEVICE# ')

    def test_exclusive_control_request_identity_and_cancel(self):
        self.start()
        result = self.call('xmodem-start', **self.arguments)
        self.assertEqual(result['xmodem']['transferId'], self.tid)
        with self.assertRaises(ValueError): self.call('xmodem-start', **{**self.arguments, 'size': 9})
        with self.assertRaises(RuntimeError): self.call('write', generation=self.call('status')['generation'], data='BAD')
        with self.assertRaises(RuntimeError): self.call('control', generation=self.call('status')['generation'], operation='release')
        other = self.call('open', port='COM3', requestId=uuid.uuid4().hex)
        with self.assertRaises(PermissionError): self.call('xmodem-status', sessionId=other['sessionId'], transferId=self.tid)
        self.task('run'); self.task('cancel')
        self.assertEqual(self.finished()['state'], 'cancelled')
        self.assertIn(bytes([xm.CAN, xm.CAN]), self.wire.writes)
        current = self.call('status')
        self.call('write', generation=current['generation'], data='OK')
        self.assertEqual(self.wire.writes[-1], b'OK')
        self.start()
        with self.assertRaises(RuntimeError): self.call('xmodem-start', **(self.arguments | {'requestId': uuid.uuid4().hex}))

    def test_agent_transfer_blocks_gui_takeover_and_closes_only_owner(self):
        released = self.call('control', generation=self.call('status')['generation'], operation='release')
        agent = self.registry.call('serial','open',{'port':'COM3','requestId':uuid.uuid4().hex},caller='agent')['data']
        task = self.registry.call('serial','xmodem-start',{'sessionId':agent['sessionId'],
            'generation':agent['generation'],'requestId':uuid.uuid4().hex,'direction':'receive'},caller='agent')['data']
        self.registry.call('serial','xmodem-run',{'sessionId':agent['sessionId'],
            'transferId':task['xmodem']['transferId']},caller='agent')
        with self.assertRaises(RuntimeError):
            self.call('write',generation=self.call('status')['generation'],data='禁止抢占')
        transfer=self.service.ports['COM3']['transfer']
        self.registry.call('serial','close',{'sessionId':agent['sessionId']},caller='agent')
        wait(lambda:transfer.file.closed)
        self.assertFalse(self.wire.closed)
        self.call('write',generation=self.call('status')['generation'],data='GUI恢复')
        self.assertEqual(self.wire.writes[-1], 'GUI恢复'.encode())

    def test_timeout_remote_cancel_and_disconnect_cleanup(self):
        self.start(); self.task('run')
        self.assertEqual(self.finished()['state'], 'failed')
        self.start(); self.task('run'); self.wire.rx.put(bytes([xm.CAN, xm.CAN]))
        self.assertEqual(self.finished()['state'], 'cancelled')
        self.start(); self.task('run')
        transfer = self.service.ports['COM3']['transfer']
        self.call('close')
        wait(lambda: not transfer.thread.is_alive())
        self.assertTrue(transfer.file.closed)
        self.assertFalse(self.service.ports)

    def test_upload_limits_and_configuration(self):
        with self.assertRaises(ValueError): self.start(data=b'', block=256)
        self.start(data=b'abc')
        with self.assertRaises(ValueError): self.task('upload', offset=0, data=base64.b64encode(b'x'*32769).decode())
        self.task('cancel')
        with self.assertRaises(RuntimeError): self.task('download')
        self.arguments['requestId'] = uuid.uuid4().hex
        self.arguments['size'] = 6
        self.arguments['generation'] = self.call('status')['generation']
        self.tid = self.call('xmodem-start', **self.arguments)['xmodem']['transferId']
        with self.assertRaises(ValueError): self.task('run')


def run():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(XmodemTests))
    return result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
