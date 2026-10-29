"""日志持久路径、错误反馈和写清互斥；不触碰正式日志。"""
import asyncio
import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import adb_worker
import server

class LoggingPaths(unittest.TestCase):
    def test_persistent_path_and_tail_and_clear(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);real=root/'exe-log.txt';wrong=root/'_MEI';wrong.mkdir()
            (wrong/'devmux.log').write_text('临时目录错误日志',encoding='utf-8')
            with patch.object(adb_worker,'LOG_FILE',str(real)),patch.object(server,'__file__',str(wrong/'server.py')):
                with contextlib.redirect_stdout(io.StringIO()):adb_worker.log('TEST','持久中文✓')
                self.assertIn('持久中文✓',adb_worker.read_log_tail())
                real.write_text(''.join(f'行{i}\n' for i in range(600)),encoding='utf-8')
                manager=object.__new__(server.ManagerServer)
                async def blocking(fn,*args,**kw):return fn(*args,**kw)
                manager._run_blocking=blocking
                result=asyncio.run(manager._get_log())
                self.assertTrue(result['success']);self.assertEqual(result['output'].splitlines(),[f'行{i}' for i in range(100,600)])
                self.assertTrue(asyncio.run(manager._clear_log())['success']);self.assertEqual(real.read_bytes(),b'')
                self.assertEqual((wrong/'devmux.log').read_text('utf-8'),'临时目录错误日志')

    def test_ws_reports_read_and_clear_failures(self):
        manager=object.__new__(server.ManagerServer);messages=[]
        async def blocking(fn,*args,**kw):raise PermissionError('测试拒绝')
        async def send(ws,value):messages.append(value)
        manager._run_blocking=blocking;manager._send=send
        for operation in ('get-log','clear-log'):
            asyncio.run(manager._handle_message(None,{'type':operation,'id':operation}))
            self.assertFalse(messages[-1]['success']);self.assertIn('失败',messages[-1]['output'])
            self.assertEqual(messages[-1]['id'],operation)

    def test_clear_waits_for_writer_lock(self):
        with tempfile.TemporaryDirectory() as work,patch.object(adb_worker,'LOG_FILE',str(Path(work)/'log')):
            Path(adb_worker.LOG_FILE).write_text('before',encoding='utf-8');started=threading.Event();done=threading.Event()
            def clear():started.set();adb_worker.clear_log_file();done.set()
            with adb_worker.LOG_FILE_LOCK:
                thread=threading.Thread(target=clear);thread.start();self.assertTrue(started.wait(2));self.assertFalse(done.wait(.05))
            thread.join(2);self.assertTrue(done.is_set())

def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LoggingPaths)).wasSuccessful()
if __name__=='__main__':sys.exit(0 if run() else 1)
