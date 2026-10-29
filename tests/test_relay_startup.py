"""首次连接终端并行初始化及准备期按键回归。"""
import asyncio
import concurrent.futures
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import adb_worker
import relay_app
import relay_session
import server

def run():
    checks=[]
    def check(name, value):
        checks.append(bool(value))
        print(('  [PASS] ' if value else '  [FAIL] ')+name,flush=True)
    prompt_release = threading.Event()
    first_read = threading.Event()
    constructed = threading.Event()
    output = []
    handles = []
    class WaitingPrompt:
        def read(self):
            first_read.set()
            prompt_release.wait(2)
            raise EOFError()
        def write(self, data):
            output.append(data)
    def create_waiting():
        handles.append(adb_worker.TerminalSession({'serial': 'slow-prompt'},
                                                  lambda data: None, lambda: None))
        constructed.set()
    with patch.object(adb_worker.PtyProcess, 'spawn', return_value=WaitingPrompt()), \
         patch.object(adb_worker, 'log'):
        constructor = threading.Thread(target=create_waiting)
        constructor.start()
        try:
            check('首包尚未到达时PTY构造与第一条输入均不等待提示符',
                  constructed.wait(.2) and first_read.wait(.2))
            if handles:
                handles[0].write(b'first\r')
            check('提示符未到时首条指令已送入PTY', output == ['first\r'])
        finally:
            prompt_release.set()
            constructor.join(3)
    class ImmediatePrompt:
        def open(self, device, *, on_output, **kwargs):
            on_output(b'initial-prompt')
            return object()
        def close(self, handle):
            pass
    with tempfile.TemporaryDirectory() as root:
        queue = relay.RelayQueue(root)
        session = relay_session.RemotePtySession(queue, 'first-prompt', {'key': 'dev'}, ImmediatePrompt())
        ok, reason = session.start()
        check('后端open期间首包回显不丢失',
              ok and queue.stream('first-prompt').output_tail(0).read_new() == b'initial-prompt')
        session.close()
    entered=threading.Barrier(3)
    release=threading.Event()
    closed=[]
    class SlowSession:
        def __init__(self, queue, stream_id, *args, **kwargs):
            self.stream_id=stream_id
        def start(self):
            entered.wait(2)
            release.wait(2)
            return True,'ok'
        def is_alive(self): return True
        def close(self, reason): closed.append(self.stream_id)
    with tempfile.TemporaryDirectory() as root, patch.object(relay_session,'RemotePtySession',SlowSession):
        manager=relay_session.SessionManager(relay.RelayQueue(root),object())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(manager.open,sid,{'key':sid}) for sid in ('one','two')]
            try:
                entered.wait(1)
                parallel=True
            except threading.BrokenBarrierError:
                parallel=False
            check('两个慢PTY初始化能同时进入启动阶段',parallel)
            started=time.monotonic()
            manager.active_ids()
            check('PTY启动期间会话表仍及时响应',time.monotonic()-started<0.1)
            manager.stop_all()
            release.set()
            results=[future.result(3) for future in futures]
            check('初始化期间撤销不会遗留迟到终端',all(not result[0] for result in results)
                  and not manager.active_ids() and set(closed)=={'one','two'})
    controller=relay_app.RelayController.__new__(relay_app.RelayController)
    controller.node=object()
    controller._sessions={'key':{'streamId':'pending','opening':True,'opened':False}}
    check('准备期已有稳定输入代次但不伪造终端就绪',
          controller.session_generation('key') is not None and not controller.session_input_ready('key'))
    controller._sessions['key']['opening']=False
    check('初始化失败后旧输入代次立即失效',controller.session_generation('key') is None)
    async def queued_input():
        m=server.ManagerServer.__new__(server.ManagerServer)
        m._executor=concurrent.futures.ThreadPoolExecutor(max_workers=2)
        m._remote_io_pending={'input':{},'resize':{}}
        m._remote_io_tasks={'input':{},'resize':{}}
        m._remote_last_size={}
        class Link:
            ready=False
            generation=(1,'first')
            def session_generation(self,key):return self.generation
            def session_input_ready(self,key):return self.ready
        m.relay=Link()
        writes=[]
        m._write_terminal=lambda key,data:writes.append(data)
        try:
            m._queue_remote_io('key','ls')
            await asyncio.sleep(.1)
            check('初始化未完成时不向PTY提前发按键',not writes)
            m._queue_remote_io('key',' /data')
            m.relay.ready=True
            await asyncio.gather(*list(m._remote_io_tasks['input'].values()))
            check('准备期连续按键就绪后不丢失且保持顺序',b''.join(writes)==b'ls /data')
            m.relay.ready=False
            m._queue_remote_io('key','stale')
            await asyncio.sleep(.06)
            m.relay.generation=(2,'second')
            m.relay.ready=True
            await asyncio.gather(*list(m._remote_io_tasks['input'].values()))
            check('准备期间重连不会将旧按键发往新终端',b'stale' not in writes)
        finally:
            await asyncio.gather(*m._cancel_remote_io(),return_exceptions=True)
            m._executor.shutdown(wait=True)
    asyncio.run(queued_input())
    return all(checks)

if __name__=='__main__':
    raise SystemExit(0 if run() else 1)
