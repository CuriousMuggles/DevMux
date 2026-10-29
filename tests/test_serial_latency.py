"""使用真实pyserial内存回环验证短包读取，不用立即返回的假read隐藏超时。"""
import base64
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from serial_terminal import SerialService


class LoopPort:
    def __init__(self,timeout=.15):
        import serial
        self.serial=serial.serial_for_url('loop://',baudrate=921600,timeout=timeout,write_timeout=1)
        self.reading=threading.Event();self.sizes=[]
    @property
    def in_waiting(self):return self.serial.in_waiting
    def read(self,size):self.sizes.append(size);self.reading.set();return self.serial.read(size)
    def write(self,data):return self.serial.write(data)
    def close(self):self.serial.close()


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='serial-latency-');self.ports=[]
        def connect(port,config):
            transport=LoopPort();self.ports.append(transport);return transport
        self.service=SerialService(Path(self.temp.name)/'serial.json',port_source=lambda:[dict(port='COM3')],connector=connect)
        self.opened=self.call('open',dict(port='COM3',requestId=uuid.uuid4().hex))
        self.args=dict(sessionId=self.opened['sessionId']);self.cursor=0
        self.assertTrue(self.ports[0].reading.wait(2))
    def call(self,action,args):return self.service.call_for_context(action,args,caller='gui')['data']
    def tearDown(self):self.service.close();self.temp.cleanup()
    def send(self,data):self.call('write',{**self.args,'generation':self.opened['generation'],'format':'base64','data':base64.b64encode(data).decode()})
    def receive(self,size):
        result=bytearray();deadline=time.monotonic()+3
        while len(result)<size and time.monotonic()<deadline:
            output=self.call('read',{**self.args,'cursor':self.cursor})
            result.extend(base64.b64decode(output['data']));self.cursor=output['nextCursor']
            if len(result)<size:time.sleep(.001)
        self.assertEqual(len(result),size);return bytes(result)
    def test_short_reply_does_not_wait_for_total_timeout(self):
        samples=[]
        for index in range(8):
            data=bytes([index,0,255,0xe4,0xb8,0xad]);start=time.perf_counter()
            self.send(data);self.assertEqual(self.receive(len(data)),data)
            samples.append((time.perf_counter()-start)*1000)
        self.assertLess(statistics.median(samples),100,'短回显等待了150ms读取超时')
        print('  [MEASURE] pyserial loop短回包P50 %.2fms / 最大 %.2fms'%(statistics.median(samples),max(samples)),flush=True)
    def test_binary_bursts_remain_exact_and_bounded(self):
        data=bytes(range(256))*512
        for offset in range(0,len(data),32768):self.send(data[offset:offset+32768])
        self.assertEqual(self.receive(len(data)),data)
        self.assertTrue(all(1<=size<=32768 for size in self.ports[0].sizes))
    def test_idle_read_blocks_and_close_releases(self):
        time.sleep(.04)
        self.assertLessEqual(len(self.ports[0].sizes),2,'空闲读取发生忙轮询')
        start=time.monotonic();self.call('close',self.args)
        self.service.sessions[self.args['sessionId']]['slot']['thread'].join(timeout=.5)
        self.assertLess(time.monotonic()-start,1)
        self.assertFalse(self.service.sessions[self.args['sessionId']]['slot']['thread'].is_alive())


def run():return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LatencyTests)).wasSuccessful()
if __name__=='__main__':sys.exit(0 if run() else 1)
