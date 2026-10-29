"""隔离中转+真实pyserial回环，记录同配置前后字节回显延迟；不操作物理COM。"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tests.test_serial_remote import RemoteTests


class EchoCase(RemoteTests):
    async def test_measure(self):
        if self.mode=='loop':
            import serial
            self.slave.connector=lambda port,config:serial.serial_for_url('loop://',baudrate=config['baudrate'],timeout=.05,write_timeout=1)
        self.registry.call('serial','permission',{'allowed':True})
        opened=await self.gui('open',{'port':'COM3','requestId':uuid.uuid4().hex})
        cursor=0;samples=[]
        for index in range(22):
            raw=bytes([index,0,255,0xe4,0xb8,0xad]);start=time.perf_counter()
            await self.gui('write',dict(sessionId=opened['sessionId'],generation=opened['generation'],format='base64',data=base64.b64encode(raw).decode()))
            received=bytearray()
            while len(received)<len(raw) and time.perf_counter()-start<3:
                output=await self.gui('read',dict(sessionId=opened['sessionId'],cursor=cursor))
                received.extend(base64.b64decode(output['data']));cursor=output['nextCursor']
                if len(received)<len(raw):await asyncio.sleep(.002)
            self.assertEqual(bytes(received),raw)
            samples.append((time.perf_counter()-start)*1000)
        await self.gui('close',dict(sessionId=opened['sessionId']))
        self.samples=samples[2:]


def run(label):
    results={}
    for mode in ('fake','loop'):
        case=EchoCase('test_measure');case.mode=mode
        result=unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite([case]));assert result.wasSuccessful()
        samples=case.samples
        results[mode]=dict(samples=20,p50Ms=round(statistics.median(samples),2),p95Ms=round(sorted(samples)[18],2),maxMs=round(max(samples),2))
    results['serialSourceSha256']=hashlib.sha256((ROOT/'serial_terminal.py').read_bytes()).hexdigest()
    results['environment']='同机临时共享目录、GUI调用中转、921600/50ms、无物理COM/非双机SLA'
    output=ROOT/'.agent-runtime/serial-latency';output.mkdir(parents=True,exist_ok=True)
    (output/(label+'.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':run(sys.argv[1])
