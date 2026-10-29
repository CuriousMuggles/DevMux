"""从机终端复用：保留一个PTY，远端订阅输出，控制代次隔离输入。"""
import collections
import queue
import threading
import uuid


class Lease:
    def __init__(self, hub, slot, on_output, on_exit):
        self.hub, self.slot = hub, slot
        self.on_output, self.on_exit = on_output, on_exit
        self.token = uuid.uuid4().hex
        self.active = True
        self.exit_reason = ''
        self.overflow = False
        self.pending = queue.Queue(maxsize=256)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True, name='shared-terminal-output')

    def _pump(self):
        try:
            while not self.stop.is_set():
                try:
                    data = self.pending.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    self.on_output(data)
                except Exception:
                    self.hub.end(self, notify=True)
                    return
        finally:
            # 满队列撤销由本地读线程发起，但写meta的退出通知留在订阅线程。
            if self.overflow:
                self.on_exit()

    def feed(self, data):
        if not self.active:
            return
        try:
            self.pending.put_nowait(data)
        except queue.Full:
            # 共享目录阻塞不能拖住本地终端读取；关闭远端订阅，等待重新接入。
            self.overflow = True
            self.hub.end(self, notify=False)

    def state(self):
        return self.hub.state(self.slot['key']) or {'owner':'local', 'shared':False, 'generation':self.token}


class TerminalHub:
    def __init__(self, on_control=None):
        self.lock = threading.RLock()
        self.slots = {}
        self.on_control = on_control

    def register(self, key, session):
        with self.lock:
            old = self.slots.get(key)
            self.slots[key] = dict(key=key, session=session, owner='local',
                                   generation=uuid.uuid4().hex, lease=None, hold=False,
                                   history=collections.deque(), history_size=0)
        if old and old['lease']:
            self.end(old['lease'], notify=True)
        self.emit(key)

    def state(self, key):
        with self.lock:
            slot = self.slots.get(key)
            if not slot:
                return None
            return dict(key=key, owner=slot['owner'], generation=slot['generation'],
                        shared=bool(slot['lease']), held=slot['hold'])

    def states(self):
        with self.lock:
            return [self.state(key) for key in self.slots]

    def emit(self, key):
        state = self.state(key)
        if state and self.on_control:
            self.on_control(state)

    def publish(self, key, data, session=None):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or (session is not None and slot['session'] is not session):
                return
            # 回放只用于新接入的显示，不向PTY发送换行或重复指令。
            history_data = bytes(data)[-128 * 1024:]
            slot['history'].append(history_data)
            slot['history_size'] += len(history_data)
            while slot['history_size'] > 128 * 1024 and len(slot['history']) > 1:
                slot['history_size'] -= len(slot['history'].popleft())
            lease = slot['lease']
        if lease:
            lease.feed(bytes(data))

    def attach(self, key, on_output, on_exit):
        with self.lock:
            slot = self.slots.get(key)
            if (not slot or not getattr(slot['session'], '_alive', True)
                    or (hasattr(slot['session'], 'proc') and slot['session'].proc is None)):
                raise RuntimeError('从机本地终端尚未就绪')
            if slot['hold']:
                raise RuntimeError('从机正在本地控制，请等待交还主机')
            if slot['lease'] and slot['lease'].active:
                raise RuntimeError('该设备已有主机接入')
            lease = Lease(self, slot, on_output, on_exit)
            slot.update(owner='remote', generation=uuid.uuid4().hex, lease=lease)
            history = b''.join(slot['history'])
            lease.feed(b'\x1bc' + history)
            lease.thread.start()
        self.emit(key)
        return lease

    def end(self, lease, notify=False, reason=''):
        with self.lock:
            if not lease.active:
                return
            lease.exit_reason = reason
            lease.active = False
            lease.stop.set()
            slot = lease.slot
            if self.slots.get(slot['key']) is slot and slot['lease'] is lease:
                slot.update(lease=None, owner='local', generation=uuid.uuid4().hex)
        self.emit(slot['key'])
        if notify:
            lease.on_exit()

    def take(self, key):
        with self.lock:
            slot = self.slots.get(key)
            if not slot:
                return False
            slot.update(owner='local', hold=True, generation=uuid.uuid4().hex)
        self.emit(key)
        return True

    def give(self, key):
        with self.lock:
            slot = self.slots.get(key)
            if not slot:
                return False
            # 旧接入永远不能恢复写权限：重开新stream后才接受主机输入。
            lease = slot['lease']
            slot.update(hold=False, generation=uuid.uuid4().hex)
        if lease:
            self.end(lease, notify=True, reason='终端控制权切换')
        else:
            self.emit(key)
        return True

    def request_remote_control(self, lease, generation):
        with self.lock:
            slot = lease.slot
            if (not lease.active or self.slots.get(slot['key']) is not slot
                    or slot['lease'] is not lease or not generation or generation != slot['generation']):
                return False, '控制代次已变化，请刷新后重试'
            if slot['owner'] == 'remote':
                return True, '主机已拥有控制权'
            slot.update(hold=False, generation=uuid.uuid4().hex)
        # 旧stream永久退出，重新接入同一个shell，旧排队按键不能在接管后执行。
        self.end(lease, notify=True, reason='终端控制权切换')
        return True, '已允许主机重新接管'

    def remove(self, key, session=None):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or (session is not None and slot['session'] is not session):
                return
            self.slots.pop(key)
        if slot['lease']:
            self.end(slot['lease'], notify=True)
        # shell结束也要释放界面的只读状态，允许操作者关闭并重新打开终端。
        state = self.state(key) or dict(key=key, owner='local', generation=uuid.uuid4().hex,
                                       shared=False, held=False)
        if self.on_control:
            self.on_control(state)

    def agent_claim(self, key):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or slot['owner'] != 'local' or slot['hold'] or slot['lease']:
                raise PermissionError('终端未就绪或已有控制者')
            slot.update(owner='agent', generation=uuid.uuid4().hex)
            generation = slot['generation']
        self.emit(key)
        return generation

    def agent_write(self, key, generation, data):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or slot['owner'] != 'agent' or slot['generation'] != generation:
                raise PermissionError('终端控制权已变化')
            slot['session'].write(data)

    def agent_resize(self, key, generation, cols, rows):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or slot['owner'] != 'agent' or slot['generation'] != generation:
                raise PermissionError('终端控制权已变化')
            slot['session'].resize(cols, rows)

    def agent_release(self, key, generation):
        with self.lock:
            slot = self.slots.get(key)
            if slot and slot['owner'] == 'agent' and slot['generation'] == generation:
                slot.update(owner='local', generation=uuid.uuid4().hex)
        self.emit(key)

    def local_write(self, key, data, generation=None):
        with self.lock:
            slot = self.slots.get(key)
            if not slot or slot['owner'] != 'local':
                return False
            if generation is not None and generation != slot['generation']:
                return False
            # 有共享接入或主动接管时，禁止没有代次的旧本地请求绕过控制权。
            if generation is None and (slot['lease'] or slot['hold']):
                return False
            slot['session'].write(data)
            return True

    def remote_write(self, lease, data):
        with self.lock:
            slot = lease.slot
            if (not lease.active or self.slots.get(slot['key']) is not slot
                    or slot['lease'] is not lease or slot['owner'] != 'remote'):
                return False
            slot['session'].write(data)
            return True

    def resize(self, key, cols, rows, lease=None, generation=None):
        with self.lock:
            slot = self.slots.get(key)
            if not slot:
                return False
            if lease is None:
                if slot['owner'] != 'local' or (generation is not None and generation != slot['generation']):
                    return False
            elif not lease.active or slot['lease'] is not lease or slot['owner'] != 'remote':
                return False
            slot['session'].resize(cols, rows)
            return True


class SharedTerminalBackend:
    """真实应用复用worker终端；关闭接入只取消订阅，绝不关闭shell。"""
    shared = True
    def __init__(self, worker):
        self.worker = worker

    def open(self, device, *, cols, rows, on_output, on_exit):
        import adb_worker
        hub = self.worker().terminal_hub
        lease = hub.attach(adb_worker.make_key(device), on_output, on_exit)
        hub.resize(lease.slot['key'], cols, rows, lease)
        return lease

    def write(self, handle, data):
        return handle.hub.remote_write(handle, bytes(data))

    def resize(self, handle, cols, rows):
        if not handle.hub.resize(handle.slot['key'], cols, rows, handle):
            raise PermissionError('终端已由从机接管，主机不能调整尺寸')

    def take_control(self, handle, generation):
        return handle.hub.request_remote_control(handle, generation)

    def close(self, handle):
        handle.hub.end(handle)
