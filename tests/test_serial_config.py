"""串口连接参数在原transport应用及权限/回滚回归，不操作实体COM。"""
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from serial_terminal import SerialService
from extension_tools import ExtensionTools
from tests.test_serial_terminal import FakeSerial

class ConfigWire(FakeSerial):
    def __init__(self):
        super().__init__(); self.reject = set(); self.calls = []
    def apply_settings(self, settings):
        self.settings.update(settings); self.calls.append(dict(settings))
        if settings['baudrate'] in self.reject: raise OSError('模拟驱动拒绝参数')

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.wire=ConfigWire();self.connects=[]
        def connector(port,config):self.connects.append((port,config));return self.wire
        self.service=SerialService(Path(self.temp.name)/'serial.json',port_source=lambda:[{'port':'COM3'}],connector=connector)
        self.addCleanup(self.service.close);self.tools=ExtensionTools({'serial':self.service})
        self.opened=self.call('open',port='COM3',requestId=uuid.uuid4().hex)
        self.sid=self.opened['sessionId'];self.gen=self.opened['generation']
    def call(self,action,caller='gui',**args):
        return self.tools.call('serial',action,args,caller=caller)['data']
    def configure(self,**args):return self.call('configure',sessionId=self.sid,generation=self.gen,**args)
    def test_same_transport_session_and_generation(self):
        updated=self.configure(baudrate=115200,bytesize=7,parity='E',stopbits=2,flowcontrol='rtscts',dtr=True,rts=True)
        self.assertEqual(updated['sessionId'],self.sid);self.assertEqual(len(self.connects),1)
        self.assertIs(self.service.ports['COM3']['transport'],self.wire);self.assertFalse(self.wire.closed)
        self.assertEqual(updated['config']['baudrate'],115200);self.assertTrue(self.wire.settings['rtscts']);self.assertTrue(self.wire.dtr)
        self.assertNotEqual(updated['generation'],self.gen)
        self.assertEqual(updated['serialConfigProtocol'],1)
        with self.assertRaises(PermissionError):self.call('write',sessionId=self.sid,generation=self.gen,data='OLD')
        self.call('write',sessionId=self.sid,generation=updated['generation'],data='NEW')
        self.assertEqual(self.wire.writes,[b'NEW'])
    def test_invalid_values_do_not_touch_driver(self):
        for params in [dict(baudrate=0),dict(baudrate=True),dict(bytesize=9),dict(parity='BAD'),dict(stopbits=3),dict(flowcontrol='BAD'),dict(dtr=1)]:
            with self.subTest(params=params),self.assertRaises(ValueError):self.configure(**params)
        self.assertFalse(self.wire.calls)
    def test_shared_and_old_generation_denied(self):
        other=self.call('open',caller='agent',port='COM3',requestId=uuid.uuid4().hex)
        with self.assertRaises(PermissionError):self.configure(baudrate=115200)
        self.call('close',caller='agent',sessionId=other['sessionId'])
        with self.assertRaises(PermissionError):self.call('configure',sessionId=self.sid,generation='old',baudrate=115200)
        self.assertFalse(self.wire.calls)
    def test_released_input_and_agent_denied(self):
        self.call('control',sessionId=self.sid,generation=self.gen,operation='release')
        with self.assertRaises(PermissionError):self.configure(baudrate=115200)
        with self.assertRaises(PermissionError):self.call('configure',caller='agent',sessionId=self.sid,generation=self.gen,baudrate=115200)
    def test_active_transfer_denied(self):
        self.call('xmodem-start',sessionId=self.sid,generation=self.gen,requestId=uuid.uuid4().hex,direction='receive')
        with self.assertRaises(RuntimeError):self.configure(baudrate=115200)
        self.assertFalse(self.wire.calls)
    def test_failed_apply_rolls_back(self):
        self.wire.reject={115200}
        with self.assertRaisesRegex(RuntimeError,'已恢复'):self.configure(baudrate=115200)
        state=self.call('status',sessionId=self.sid)
        self.assertEqual(state['config']['baudrate'],921600);self.assertEqual(state['generation'],self.gen)
        self.assertEqual(self.wire.settings['baudrate'],921600);self.assertFalse(self.wire.closed)
    def test_failed_rollback_closes_port(self):
        self.wire.reject={115200,921600}
        with self.assertRaisesRegex(RuntimeError,'无法恢复'):self.configure(baudrate=115200)
        self.assertTrue(self.wire.closed);self.assertNotIn('COM3',self.service.ports)

def run():return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConfigTests)).wasSuccessful()
if __name__=='__main__':sys.exit(0 if run() else 1)
