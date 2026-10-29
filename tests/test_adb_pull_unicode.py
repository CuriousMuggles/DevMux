"""模拟Windows递归目录创建失败，验证真实兼容分支而非shutil假成功。"""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import adb_pull
import relay_transfer

class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.destination=Path(self.tmp.name)/'0';self.destination.mkdir()
        self.remote='/tmp/含 空格'
        self.dirs=[self.remote,self.remote+'/子目录',self.remote+'/empty']
        self.files={self.remote+'/子目录/二进制.dat':bytes(range(256))*30,self.remote+'/中文 文件.txt':'中文'.encode()}
        self.calls=[]
    def raw(self):
        return b'D\0'+b'\0'.join(p.encode() for p in self.dirs)+b'\0F\0'+b'\0'.join(p.encode() for p in self.files)+b'\0L\0X\0E\0'
    def run_adb(self,args,*,raw=False):
        self.calls.append(args)
        if args[0]=='shell':return SimpleNamespace(stdout=self.raw())
        if Path(args[-1])==self.destination:raise RuntimeError('adb: Illegal byte sequence')
        Path(args[-1]).write_bytes(self.files[args[1]])
        return SimpleNamespace(stdout='ok')
    def test_unicode_tree_empty_directories_and_bytes(self):
        result=adb_pull.pull_to_temp(self.run_adb,self.remote,self.destination,windows=True)
        self.assertEqual(result.name,'含 空格');self.assertTrue((result/'empty').is_dir())
        self.assertEqual((result/'子目录/二进制.dat').read_bytes(),bytes(range(256))*30)
        self.assertEqual((result/'中文 文件.txt').read_bytes(),'中文'.encode())
        self.assertTrue(all(Path(a[-1]).name.isascii() for a in self.calls if a[0]=='pull'))
    def test_unknown_adb_failure_not_retried(self):
        def fail(args,**kw):raise RuntimeError('device offline')
        with self.assertRaisesRegex(RuntimeError,'offline'):adb_pull.pull_to_temp(fail,self.remote,self.destination,windows=True)
    def test_link_outside_and_incomplete_inventory_rejected(self):
        samples=[self.raw().replace(b'L\0X\0',b'L\0/tmp/link\0X\0'),self.raw().replace(self.remote.encode()+b'/empty',b'/etc/escape'),self.raw()[:-2]]
        for data in samples:
            with self.subTest(data=data),patch.object(self,'raw',return_value=data):
                with self.assertRaises((ValueError,UnicodeError)):adb_pull.pull_to_temp(self.run_adb,self.remote,self.destination,windows=True)
    def test_reserved_case_collision_and_limits(self):
        original=self.raw()
        samples=[original.replace('子目录'.encode(),b'CON'),original.replace(b'\0F\0',b'\0'+self.remote.encode()+b'/EMPTY\0F\0')]
        for data in samples:
            with self.subTest(data=data),patch.object(self,'raw',return_value=data):
                with self.assertRaises(ValueError):adb_pull.pull_to_temp(self.run_adb,self.remote,self.destination,windows=True)
        with patch.object(relay_transfer,'MAX_ENTRIES',2):
            with self.assertRaises(ValueError):adb_pull.pull_to_temp(self.run_adb,self.remote,self.destination,windows=True)
        with patch.object(relay_transfer,'MAX_BYTES',1):
            with self.assertRaises(ValueError):adb_pull.pull_to_temp(self.run_adb,self.remote,self.destination,windows=True)

def run():return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PullTests)).wasSuccessful()
if __name__=='__main__':sys.exit(0 if run() else 1)
