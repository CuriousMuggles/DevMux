import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1] / 'plugins' / 'ebpf'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('ebpf_test_backend', ROOT / 'main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EBPFTests(unittest.TestCase):
    def test_device_symbol_plan_and_result_validate_identity(self):
        import json
        args=dict(pid=10,addresses=['0x00400010'],sha256='a'*64)
        plan=self.plugin.call('symbol-plan',args)
        self.assertIn('timeout 5 ',plan['command'])
        kernel=self.plugin.call('symbol-plan',{**args,'kind':'kernel'})
        self.assertIn(' --kernel 0x00400010',kernel['command'])
        with self.assertRaises(ValueError):self.plugin.call('symbol-plan',{**args,'kind':'kernel;bad'})
        report=dict(schemaVersion=1,pid=10,frames=[dict(address='0x400010',symbol='main',offset=16,status='resolved')])
        self.assertEqual(self.plugin.call('symbol-result',{**args,'text':json.dumps(report)})['resolvedFrames'],1)
        for key,value in [('pid',11),('addresses',['0x400011']),('addresses',['0x4;echo bad'])]:
            with self.assertRaises(ValueError):self.plugin.call('symbol-result',{**args,key:value,'text':json.dumps(report)})
        report['frames'][0]['status']='no-function-symbol'
        with self.assertRaises(ValueError):self.plugin.call('symbol-result',{**args,'text':json.dumps(report)})
        report['frames'][0]['symbol']=None
        self.assertEqual(self.plugin.call('symbol-result',{**args,'text':json.dumps(report)})['resolvedFrames'],0)

    def test_live_session_plan_parse_histogram_and_quality(self):
        import json
        plan=self.plugin.call('plan',dict(mode='session',pid=10,duration=1,sha256='a'*64))
        self.assertIn('2000000 '+plan['sessionId'],plan['command'])
        report=dict(schemaVersion=1,sessionId=plan['sessionId'],pid=10,startNs=1,endNs=3000000,
            slowThresholdNs=2000000,quality=dict(completedCalls=2,unmatchedExits=1,
            mapUpdateFailures=0,stackFailures=0,droppedSlowCalls=0),slowCalls=[],
            latencyBuckets=[2]+[0]*31,bucketWidthNs=65536)
        result=self.plugin.call('parse',dict(mode='session',text=json.dumps(report)))
        self.assertEqual(result['samples'],2)
        self.assertEqual(self.plugin.call('analyze',{'result':result})['quality']['unmatchedExits'],1)
        report['latencyBuckets'][0]=3
        with self.assertRaises(ValueError):self.plugin.call('parse',dict(mode='session',text=json.dumps(report)))

    def test_symbol_chunks_validate_size_and_digest(self):
        import base64
        upload=self.plugin.call('symbol-chunk',{'data':base64.b64encode(b'not an ELF').decode()})
        self.assertEqual(upload['bytes'],10)
        with self.assertRaises(ValueError):self.plugin.call('resolve-user',dict(token=upload['token'],sha256='0'*64))
        with self.assertRaises(ValueError):self.plugin.call('symbol-chunk',dict(token='missing',data=''))
        with self.assertRaises(ValueError):self.plugin.call('symbol-chunk',dict(data='A'*65537))

    def test_correlated_calls_reject_cross_session_and_preserve_missing_stacks(self):
        import copy
        report=dict(schemaVersion=1,sessionId='a'*32,pid=10,startNs=100,endNs=1000,slowThresholdNs=100,
          quality=dict(completedCalls=1,unmatchedExits=0,mapUpdateFailures=0,stackFailures=1,droppedSlowCalls=0),
          slowCalls=[dict(sessionId='a'*32,pid=10,tid=11,syscallId=63,enterNs=200,exitNs=500,kernelStack=['0xffff800000001000'],userStack=None)])
        result=self.plugin.call('parse-session',{'report':report})
        self.assertEqual(result['slowCalls'][0]['durationNs'],300)
        self.assertIsNone(result['slowCalls'][0]['userStack'])
        report['kernelProbeRanges']=[dict(name='devmux_sys_enter',start='0xffff800080001000',size=512)]
        self.assertEqual(self.plugin.call('parse-session',{'report':report})['kernelProbeRanges'],report['kernelProbeRanges'])
        for probe in [dict(name='invented_function',start='0xffff800080001000',size=512),
                      dict(name='devmux_sys_enter',start='0x1000',size=512),
                      dict(name='devmux_sys_enter',start='0xffff800080001000',size=1048577)]:
            with self.assertRaises(ValueError):self.plugin.call('parse-session',{'report':{**report,'kernelProbeRanges':[probe]}})
        for mutation in ('session','duplicate','time'):
            bad=copy.deepcopy(report)
            if mutation=='session':bad['slowCalls'][0]['sessionId']='b'*32
            if mutation=='duplicate':bad['slowCalls'].append(copy.deepcopy(bad['slowCalls'][0]))
            if mutation=='time':bad['slowCalls'][0]['exitNs']=1001
            with self.assertRaises(ValueError):self.plugin.call('parse-session',{'report':bad})

    def test_kernel_symbol_output_preserves_unknown_frames(self):
        result=self.plugin.call('parse',dict(mode='kernel',text='frame=ffff800000001010\nframe=ffff800000002010\nsymbol=0 first 0x10\n'))
        self.assertEqual(result['resolvedFrames'],1)
        self.assertEqual(result['symbols'][0],{'name':'first','offset':16})
        self.assertIsNone(result['symbols'][1])
        with self.assertRaises(ValueError):
            self.plugin.call('parse',dict(mode='kernel',text='frame=ffff800000001010\nsymbol=5 invalid 0x0\n'))
    def test_user_symbols_require_matching_build_id_and_function_bounds(self):
        import struct
        from elf_symbols import ELF
        strings=b'\0test_function\0'
        symbol=struct.pack('<IBBHQQ',1,2,0,1,0x1000,32)
        note=struct.pack('<III',4,4,3)+b'GNU\0'+b'\x12\x34\x56\x78'
        shoff=64;data_start=64+4*64
        header=struct.pack('<16sHHIQQQIHHHHHH',b'\x7fELF\x02\x01'+bytes(10),3,183,1,0,0,shoff,0,64,0,0,64,4,0)
        section=lambda kind,offset,size,link=0,entry=0:struct.pack('<IIQQQQIIQQ',0,kind,0,0,offset,size,link,0,1,entry)
        data=header+bytes(64)+section(2,data_start,len(symbol),2,24)+section(3,data_start+len(symbol),len(strings))+section(7,data_start+len(symbol)+len(strings),len(note))+symbol+strings+note
        elf=ELF(data)
        resolved=elf.resolve(0x701008,0x700000,'12345678')
        self.assertEqual((resolved['symbol'],resolved['offset']),('test_function',8))
        self.assertEqual(elf.resolve(0x701008,0x700000,'bad')['status'],'build-id-mismatch')
        import base64
        uploaded=self.plugin.call('symbol-chunk',{'data':base64.b64encode(data).decode()})
        # Even without caller-supplied build ID, only an identity-checked ELF
        # may provide its own build ID; mapped-address handling remains bounded.
        with self.assertRaises(ValueError):
            self.plugin.call('resolve-user',dict(token=uploaded['token'],sha256='0'*64))
        self.plugin.call('symbol-release',{'token':uploaded['token']})
        with self.assertRaises(ValueError):
            self.plugin.call('resolve-user',dict(token=uploaded['token']))
        self.assertIsNone(elf.resolve(0x701020,0x700000,'12345678')['symbol'])
        with self.assertRaises(ValueError):ELF(data[:80])
        # A real program-header entry exercises PIE/shared-library mappings
        # whose executable mapping starts at a non-zero file offset.
        program=struct.pack('<IIQQQQQQ',1,5,0,0,0,0x3000,0x3000,4096)
        mapped_header=struct.pack('<16sHHIQQQIHHHHHH',b'\x7fELF\x02\x01'+bytes(10),3,183,1,0,len(data),shoff,0,64,56,1,64,4,0)
        mapped_elf=ELF(mapped_header+data[64:]+program)
        mapping={'start':0x701000,'end':0x702000,'offset':0x1000}
        mapped=mapped_elf.resolve_mapping(0x701008,mapping,'12345678')
        self.assertEqual((mapped['symbol'],mapped['offset']),('test_function',8))
        self.assertEqual(mapped_elf.resolve_mapping(0x701008,mapping,'wrong')['status'],'build-id-mismatch')
        self.assertIsNone(mapped_elf.resolve_mapping(0x703000,mapping,'12345678')['symbol'])

    def test_analysis_does_not_claim_unmeasured_quality_or_exact_tail(self):
        report = module.analyze_latency({'samples':12,'buckets':[10,0,2],'bucketWidthNs':65536})
        self.assertIsNone(report['quality']['lostEvents'])
        self.assertEqual(report['facts'][1]['lowerBoundNs'],131072)
        self.assertNotIn('maxLatencyNs',report)
        self.assertIsNone(report['quality']['stackFailures'])
        empty=module.analyze_latency({'samples':0,'buckets':[0,0],'bucketWidthNs':65536})
        self.assertIsNone(empty['overflowRatio'])
        self.assertEqual(empty['recommendations'],[])
        with self.assertRaises(ValueError):
            module.analyze_latency({'samples':13,'buckets':[10,0,2],'bucketWidthNs':65536})

    def test_kernel_symbols_are_bounded_and_hidden_symbols_stay_unknown(self):
        symbols='ffff800000001000 T first\nffff800000002000 T second\nffff800000003000 T end\n'
        result=module.resolve_kernel(['0xffff800000001010','0xffff800000004000'],symbols)
        self.assertEqual((result[0]['symbol'],result[0]['offset']),('first',16))
        self.assertIsNone(result[1]['symbol'])
        self.assertIsNone(module.resolve_kernel(['0xffff800000001010'],'0000000000000000 T hidden')[0]['symbol'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = module.create_plugin(Path(self.temp.name) / 'ebpf')
        self.addCleanup(self.plugin.close)

    def test_reject_command_injection_and_invalid_duration(self):
        for pid, duration in [('1; reboot', 5), (True, 5), (0, 5), (1, 31), (1, None)]:
            with self.assertRaises(ValueError):
                self.plugin.call('plan', dict(pid=pid, duration=duration, sha256='a'*64))

    def test_plan_has_finite_timeout_and_unique_cancel_ticket(self):
        args = dict(pid=1, duration=5, sha256='a'*64)
        first, second = [self.plugin.call('plan', args) for _ in range(2)]
        self.assertNotEqual(first['ticket'], second['ticket'])
        self.assertIn('timeout 10 ', first['command'])
        self.assertIn(first['ticket'], first['cancel'])
        self.assertTrue(first['command'].startswith('('))
        self.assertTrue(first['command'].endswith(')'))

    def test_missing_and_inconsistent_histograms_are_rejected(self):
        for text in ['', 'samples=2', 'bucket=40 x: 1\nsamples=1', 'bucket=0 x: 1\nsamples=2']:
            with self.assertRaises(ValueError):
                self.plugin.call('parse', dict(text=text))

    def test_histogram_preserves_overflow(self):
        result = self.plugin.call('parse', dict(text='bucket=0 x: 10\nbucket=31 x: 2\nsamples=12'))
        self.assertEqual(result['buckets'][31], 2)
        self.assertEqual(result['samples'], 12)

    def test_stack_addresses_are_not_fabricated_symbols(self):
        result = self.plugin.call('parse', dict(mode='user', text='frame=0000000000400590\nframes=1'))
        self.assertEqual(result['frames'], ['0x0000000000400590'])
        with self.assertRaises(ValueError):
            self.plugin.call('parse', dict(mode='user', text='stack_id=-99'))


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EBPFTests)).wasSuccessful()
