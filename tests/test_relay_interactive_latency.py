"""慢回显不阻塞按键、普通回车不限速以及输出突发排空回归。"""
import asyncio
import concurrent.futures
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import adb_worker
import relay_session
import server

def run():
    checks=[]
    def check(name,value):
        checks.append(bool(value))
        print(('  [PASS] ' if value else '  [FAIL] ')+name,flush=True)
    entered=threading.Event()
    release=threading.Event()
    written=threading.Event()
    exited=threading.Event()
    logs=[]
    class Proc:
        reads=0
        def read(self):
            self.reads+=1
            if self.reads==1:return ''
            if self.reads==2:return 'echo'
            raise EOFError()
        def write(self,data):written.set()
    def slow_output(data):
        entered.set()
        release.wait(2)
    with patch.object(adb_worker.PtyProcess,'spawn',return_value=Proc()), \
         patch.object(adb_worker,'log',side_effect=lambda cat,msg:logs.append(msg)), \
         patch.dict(os.environ,{'ADB_PTY_TRACE':'0'}):
        session=adb_worker.TerminalSession({'serial':'fake','transportId':1},slow_output,exited.set)
        entered.wait(1)
        writer=threading.Thread(target=session.write,args=(b'next',))
        writer.start()
        independent=written.wait(.15)
        release.set()
        writer.join(2)
        exited.wait(2)
        check('回显回调阻塞时后续按键仍立即写入PTY',independent)
        check('普通字符回显不逐包同步写日志',not any(' output (' in item for item in logs))
        check('读取线程结束与退出回调保持正常',exited.is_set())
    async def input_checks():
        m=server.ManagerServer.__new__(server.ManagerServer)
        m._executor=concurrent.futures.ThreadPoolExecutor(max_workers=2)
        m._remote_io_pending={'input':{},'resize':{}}
        m._remote_io_tasks={'input':{},'resize':{}}
        m._remote_last_size={}
        class Link:
            def session_generation(self,key):return (1,'stream')
            def session_input_ready(self,key):return True
            def is_remote_key(self,key):return True
        m.relay=Link()
        writes=[]
        paced=[]
        m._write_terminal=lambda key,data:writes.append(data)
        async def pace(key,data):
            paced.append(data)
            writes.append(data)
        m._send_one_line_paced=pace
        try:
            for data in ('ls','\r','pwd'):
                await m._handle_message(None,{'type':'write-terminal','key':'relay:slave:dev','data':data})
            await asyncio.gather(*list(m._remote_io_tasks['input'].values()))
            check('普通回车与后续字符合包不触发粘贴等待',not paced and b''.join(writes)==b'ls\rpwd')
            writes.clear()
            await m._handle_message(None,{'type':'write-terminal','key':'relay:slave:dev','data':'a\rb\r'})
            await asyncio.gather(*list(m._remote_io_tasks['input'].values()))
            check('真实多行粘贴仍逐行限速且顺序不变',paced==[b'a\r'] and writes==[b'a\r',b'b\r'])
        finally:
            await asyncio.gather(*m._cancel_remote_io(),return_exceptions=True)
            m._executor.shutdown(wait=True)
    asyncio.run(input_checks())
    chunks=[]
    all_output=threading.Event()
    finished=threading.Event()
    counters={'index':0,'meta':0,'status':'running'}
    def read_stream():
        index=counters['index']
        if index<20:
            counters['index']+=1
            return b'x',index+1,False
        return b'',20,False
    def read_meta():
        counters['meta']+=1
        time.sleep(.06)
        return {'status':counters['status']}
    def output(data):
        chunks.append(data)
        if len(chunks)==20:all_output.set()
    reader=relay_session.RemoteSessionReader('slave','burst',read_stream=read_stream,
        read_meta=read_meta,on_output=output,on_exit=lambda reason:finished.set())
    started=time.monotonic()
    reader.start()
    try:
        complete=all_output.wait(.3)
        elapsed=time.monotonic()-started
        check('20个回显包遇到60ms状态读取仍在300ms内排空',complete and counters['meta']==1)
        print('  输出突发排空耗时：'+str(round(elapsed*1000,1))+' ms',flush=True)
        counters['status']='ended'
        check('降低状态读取频率后仍及时检测会话结束',finished.wait(.9))
    finally:
        reader.stop()
    return all(checks)

if __name__=='__main__':
    raise SystemExit(0 if run() else 1)
