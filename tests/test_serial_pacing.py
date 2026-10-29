"""串口逐字符发送间隔与输入边界。"""
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tests.test_serial_terminal import service


class SerialPacingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='serial-pacing-')
        self.serial,self.created=service(Path(self.temp.name)/'serial.json')
        self.opened=self.serial.call_for_context('open',{'port':'COM3','requestId':uuid.uuid4().hex},caller='gui')['data']

    def tearDown(self):
        self.serial.close();self.temp.cleanup()

    def write(self,data,**extra):
        return self.serial.call_for_context('write',{'sessionId':self.opened['sessionId'],
            'generation':self.opened['generation'],'format':'text','data':data,**extra},caller='gui')['data']

    def test_paced_text_keeps_multibyte_characters_together(self):
        started=time.monotonic()
        result=self.write('A中B',interCharDelayMs=10)
        port=self.created[0][2]
        self.assertEqual(port.writes,[b'A','中'.encode('utf-8'),b'B'])
        self.assertEqual(result['writtenBytes'],5)
        self.assertGreaterEqual(port.write_times[1]-port.write_times[0],.008)
        self.assertGreaterEqual(port.write_times[2]-port.write_times[1],.008)
        self.assertGreaterEqual(time.monotonic()-started,.016)

    def test_zero_delay_preserves_single_write_and_invalid_pacing_is_rejected(self):
        self.write('A中B')
        port=self.created[0][2]
        self.assertEqual(port.writes,['A中B'.encode()])
        for value in (True,-1,21,1.5):
            with self.assertRaises(ValueError):self.write('XYZ',interCharDelayMs=value)
        with self.assertRaises(ValueError):self.write('X'*2049,interCharDelayMs=1)
        self.assertEqual(port.writes,['A中B'.encode()])

    def test_paced_hex_is_spaced_per_byte(self):
        self.serial.call_for_context('write',{'sessionId':self.opened['sessionId'],
            'generation':self.opened['generation'],'format':'hex','data':'41 42 43','interCharDelayMs':5},caller='gui')
        port=self.created[0][2]
        self.assertEqual(port.writes,[b'A',b'B',b'C'])
        self.assertGreaterEqual(port.write_times[1]-port.write_times[0],.003)
        self.assertGreaterEqual(port.write_times[2]-port.write_times[1],.003)


def run():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SerialPacingTests)).wasSuccessful()


if __name__=='__main__':sys.exit(0 if run() else 1)
