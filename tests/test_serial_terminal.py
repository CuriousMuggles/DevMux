"""串口字节/控制权/生命周期验证，所有端口均为注入假设备。"""
import base64
import queue
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from extension_tools import ExtensionTools
from serial_terminal import SerialService, MAX_HISTORY


class FakeSerial:
    def __init__(self):
        self.rx = queue.Queue()
        self.writes = []
        self.write_times = []
        self.closed = False
        self.partial = False
        self.settings = {}
        self.dtr = self.rts = False

    def read(self, size):
        if self.closed:
            raise OSError('拔出设备')
        try:
            value = self.rx.get(timeout=.01)
        except queue.Empty:
            return b''
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, data):
        if self.closed:
            raise OSError('已关闭')
        self.writes.append(data)
        self.write_times.append(time.monotonic())
        self.rx.put(data)
        return len(data)-1 if self.partial else len(data)

    def apply_settings(self, settings):
        self.settings.update(settings)

    def close(self):
        self.closed = True


def wait(check, seconds=3):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('等待串口假设备超时')


def service(path, *, config_storage=None):
    created = []
    def connector(port, config):
        serial = FakeSerial(); created.append((port, config, serial)); return serial
    instance = SerialService(path, port_source=lambda: [dict(port='COM3', description='模拟 USB 串口'),
        dict(port='COM12', description='第二个模拟串口')], connector=connector, config_storage=config_storage)
    return instance, created


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.serial, self.created = service(Path(self.temp.name)/'serial.json')
        self.registry = ExtensionTools({'serial': self.serial})

    def tearDown(self):
        self.serial.close(); self.temp.cleanup()

    def call(self, action, args=None, caller='agent', owner=None):
        return self.registry.call('serial', action, args or {}, caller=caller,
            remote=owner is not None, authorize=(lambda:owner) if owner else None)['data']

    def open(self, caller='agent', owner=None, **kwargs):
        return self.call('open', {'port':'COM3','requestId':uuid.uuid4().hex, **kwargs},caller,owner)

    def test_bytes_config_and_idempotency(self):
        request = uuid.uuid4().hex
        opened = self.open(requestId=request)
        self.assertEqual(opened['config']['baudrate'],921600)
        self.assertEqual(self.open(requestId=request)['sessionId'],opened['sessionId'])
        self.assertEqual(len(self.created),1)
        raw = bytes(range(256))
        written = self.call('write', {'sessionId':opened['sessionId'],'generation':opened['generation'],
            'format':'hex','data':raw.hex()})
        self.assertEqual(written['writtenBytes'],256)
        wait(lambda:self.call('status', {'sessionId':opened['sessionId']})['nextCursor']==256)
        output = self.call('read', {'sessionId':opened['sessionId'],'limit':17})
        self.assertEqual(base64.b64decode(output['data']),raw[:17]); self.assertTrue(output['hasMore'])
        rest = self.call('read', {'sessionId':opened['sessionId'],'cursor':17})
        self.assertEqual(base64.b64decode(rest['data']),raw[17:])
        self.assertFalse(rest['hasMore'])
        for bad in ({'baudrate':True},{'baudrate':0},{'bytesize':9},{'stopbits':True},
                    {'parity':'X'},{'flowcontrol':'X'},{'dtr':1},{'port':'COM999'}):
            with self.assertRaises((ValueError,RuntimeError)): self.open(**bad)
        for mode,data in [('hex','0'),('hex','GG'),('base64','@'),('text','测试')]:
            with self.assertRaises((ValueError,UnicodeError)):
                self.call('write', {'sessionId':opened['sessionId'],'generation':opened['generation'],
                    'format':mode,'data':data,'encoding':'ascii'})
        with self.assertRaises(RuntimeError): self.open(baudrate=115200)
        with self.assertRaises(ValueError): self.open(requestId=request,baudrate=115200)
        with self.assertRaises(ValueError): self.call('open',{'port':'COM3','requestId':uuid.uuid4().hex,'caller':'gui'})

    def test_gui_priority_shared_handles(self):
        agent = self.open(); another = self.open(); gui = self.open('gui')
        self.assertEqual(len(self.created),1)
        self.assertTrue(gui['writable']); self.assertTrue(gui['shared']); self.assertFalse(another['writable'])
        self.assertNotEqual(gui['generation'],agent['generation'])
        with self.assertRaises(PermissionError):
            self.call('write', {'sessionId':agent['sessionId'],'generation':agent['generation'],'data':'OLD'})
        with self.assertRaises(PermissionError):
            self.call('control',{'sessionId':agent['sessionId'],'generation':gui['generation'],'operation':'take'})
        with self.assertRaises(PermissionError): self.call('read',{'sessionId':gui['sessionId']})
        released = self.call('control',{'sessionId':gui['sessionId'],'generation':gui['generation'],'operation':'release'},'gui')
        taken = self.call('control',{'sessionId':agent['sessionId'],'generation':released['generation'],'operation':'take'})
        wait_gui = self.call('status',{'sessionId':gui['sessionId']},'gui')
        self.call('write',{'sessionId':gui['sessionId'],'generation':wait_gui['generation'],'data':'GUI'},'gui')
        with self.assertRaises(PermissionError):
            self.call('write',{'sessionId':agent['sessionId'],'generation':taken['generation'],'data':'OLD2'})
        self.assertEqual(self.created[0][2].writes,[b'GUI'])
        self.call('close',{'sessionId':gui['sessionId']},'gui'); self.assertFalse(self.created[0][2].closed)
        self.call('close',{'sessionId':agent['sessionId']}); self.assertFalse(self.created[0][2].closed)
        self.call('close',{'sessionId':another['sessionId']}); self.assertTrue(self.created[0][2].closed)
        self.assertFalse(self.serial.ports)

    def test_remote_permission_ownership_and_revocation(self):
        with self.assertRaises(PermissionError): self.open(owner='peer:g1')
        self.registry.call('serial','permission',{'allowed':True})
        remote = self.open(owner='peer:g1'); local = self.open('gui')
        with self.assertRaises(PermissionError): self.call('read',{'sessionId':remote['sessionId']},owner='peer:g2')
        self.registry.close_owner('peer')
        self.assertTrue(self.call('status',{'sessionId':remote['sessionId']},owner='peer:g1')['closed'])
        self.assertFalse(self.created[0][2].closed)
        self.call('close',{'sessionId':local['sessionId']},'gui'); self.assertTrue(self.created[0][2].closed)
        remote = self.open(owner='other:g1')
        self.registry.call('serial','permission',{'allowed':False})
        self.assertTrue(self.created[-1][2].closed)
        self.assertFalse(SerialService(self.serial.config_path).remote_allowed)
        with self.assertRaises(PermissionError): self.registry.call('serial','permission',{'allowed':True},remote=True)

    def test_ring_disconnect_partial_write_and_idle(self):
        opened = self.open(); handle = {'sessionId':opened['sessionId']}
        raw = b'X'*(MAX_HISTORY+70000)
        self.created[0][2].rx.put(raw)
        wait(lambda:self.call('status',handle)['nextCursor']==len(raw))
        output = self.call('read',handle)
        self.assertEqual(output['droppedBytes'],70000)
        self.assertEqual(output['nextCursor'],70000+32768)
        self.created[0][2].rx.put(OSError('USB 拔出'))
        wait(lambda:self.call('status',handle)['state']=='disconnected')
        self.assertTrue(self.created[0][2].closed)
        with self.assertRaises(RuntimeError): self.call('write',{**handle,'generation':opened['generation'],'data':'x'})
        next_open = self.open(); self.assertEqual(len(self.created),2)
        self.created[-1][2].partial = True
        with self.assertRaises(RuntimeError): self.call('write',{'sessionId':next_open['sessionId'],
            'generation':next_open['generation'],'data':'ABC'})
        self.assertTrue(self.created[-1][2].closed)
        idle = self.open()
        with self.serial._lock: self.serial.ports['COM3']['activity'] -= 1801
        wait(lambda:self.created[-1][2].closed)
        self.assertIn('闲置',self.call('status',{'sessionId':idle['sessionId']})['error'])
        self.serial.close()
        with self.assertRaises(RuntimeError): self.open()


def run():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SerialTests))
    return result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
