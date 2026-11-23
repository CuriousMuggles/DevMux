"""串口持续流：字节与游标无损，逐帧授权，输入回执不走请求目录。"""
from tool_errors import RemoteToolError
import base64
import json
import os
import threading
import time

import relay
from serial_terminal import input_bytes

ACK_WAIT_SECONDS = 10


def push_enabled():
    """远程串口输出推送开关；关闭后退回轮询与同步回执。"""
    return str(os.environ.get('DEVMUX_SERIAL_PUSH', '1')).strip().lower() not in (
        '0', 'false', 'off', 'no')


class ByteBuffer:
    def __init__(self, first=0, limit=1024*1024):
        self.data=bytearray();self.total=first;self.limit=limit

    def append(self, cursor, data):
        if type(cursor) is not int or cursor<self.total:
            raise RuntimeError('串口输出游标回退，拒绝重放')
        if cursor>self.total:
            self.data.clear();self.total=cursor
        self.data.extend(data);self.total+=len(data)
        if len(self.data)>self.limit:del self.data[:-self.limit]

    def read(self, cursor=0, limit=32768):
        if type(cursor) is not int or not 0<=cursor<=self.total:
            raise ValueError('输出游标无效')
        if type(limit) is not int or not 1<=limit<=65536:
            raise ValueError('读取上限必须为1～65536')
        start=self.total-len(self.data);position=max(start,cursor)
        raw=bytes(self.data[position-start:position-start+limit])
        return dict(data=base64.b64encode(raw).decode('ascii'),nextCursor=position+len(raw),
            droppedBytes=max(0,start-cursor),hasMore=position+len(raw)<self.total)


class SlaveSerialStream:
    def __init__(self,node,request,opened):
        self.node,self.request=node,request
        self.identifier=opened['sessionId'];self.caller=(request.payload or {}).get('caller','agent')
        self.owner=node._extension_owner(request,check_expiry=False)
        beat=node._peers.get(request.peer);self.source_started=beat.startedAt if beat else ''
        self.channel=node.queue.stream('serial-'+self.identifier)
        self.stop_flag=threading.Event();self.meta=dict(opened,ackSeq=0,ackSuccess=True,ackError='')
        self.first=opened.get('firstCursor',0)
        self.channel.write_meta(self.meta,in_place=True)
        self.thread=threading.Thread(target=self.pump,daemon=True,name='serial-stream')
        self.thread.start()

    def authorize(self):
        beat=self.node._peers.get(self.request.peer)
        skew=relay.observer_clock_skew_ms(self.node._peers,self.node.peer_id)
        if (not beat or beat.startedAt!=self.source_started or beat.role!=relay.ROLE_MASTER
                or beat.is_stale(self.node.heartbeat_interval_s,skew_ms=skew)):
            raise PermissionError('主控启动身份已变化，请重新打开串口')
        return self.node._extension_owner(self.request,check_expiry=False)

    def call(self,action,args):
        if not isinstance(args,dict) or 'sessionId' in args:
            raise ValueError('串口流参数不能指定接入身份')
        return self.node.extension_tools.call('serial',action,{**args,'sessionId':self.identifier},
            remote=True,authorize=self.authorize,caller=self.caller)['data']

    def publish(self,data):
        updated={**self.meta,**{key:value for key,value in data.items() if key not in (
            'data','nextCursor','droppedBytes','hasMore','writtenBytes')}}
        if updated!=self.meta:
            self.meta=updated;self.channel.write_meta(self.meta,in_place=True)

    def pump(self):
        reader=self.channel.input_tail();writer=self.channel.output_writer()
        pending=b'';cursor=self.first;sequence=0;last_input=time.monotonic();error=''
        try:
            while not self.stop_flag.is_set():
                self.authorize()
                if time.monotonic()-last_input>1800:raise RuntimeError('串口流闲置超过30分钟')
                wrote=False
                pending+=reader.read_new(131072)
                if reader.rebuilt or len(pending)>512*1024:raise RuntimeError('串口输入流重建或帧过长，拒绝重放')
                while b'\n' in pending:
                    frame,pending=pending.split(b'\n',1);message=json.loads(frame)
                    self.authorize();last_input=time.monotonic()
                    operation=message.get('action')
                    if operation=='keepalive':continue
                    seq=message.get('seq')
                    if type(seq) is not int or seq!=sequence+1:raise RuntimeError('串口输入序号不连续，拒绝重放')
                    sequence=seq
                    if operation not in ('write','control','close'):raise ValueError('未知串口流操作')
                    try:
                        data=self.call(operation,message.get('args') or {})
                        self.meta.update(ackSeq=seq,ackSuccess=True,ackError='',ackBytes=data.get('writtenBytes',0))
                        self.publish(data)
                        # 输入回执必须发布，即使控制状态没有变化。
                        self.channel.write_meta(self.meta,in_place=True)
                    except Exception as exc:
                        # failedSeq 是粘滞字段：主控异步核对回执时不能因为后续帧成功而漏掉失败。
                        self.meta.update(ackSeq=seq,ackSuccess=False,ackError=str(exc),ackBytes=0,
                            failedSeq=seq,failedError=str(exc))
                        self.channel.write_meta(self.meta,in_place=True)
                    if operation=='write':wrote=True
                    if operation=='close':return
                # 设备通常微秒级回显；先让出一小段时间再读，省掉整轮 15ms 等待。
                # Windows 上 Event.wait 的小超时会被量化到 ~15.6ms，短等待必须用 sleep。
                if wrote:time.sleep(.002)
                output=self.call('read',{'cursor':cursor,'limit':65536})
                raw=base64.b64decode(output['data'])
                if raw:
                    payload={'cursor':output['nextCursor']-len(raw),'data':output['data']}
                    if not writer.append(json.dumps(payload).encode('ascii')+b'\n'):
                        raise OSError('串口输出流不可写')
                cursor=output['nextCursor'];self.publish(output)
                if output.get('closed') or output['state']!='running':break
                self.stop_flag.wait(0 if output['hasMore'] else .015)
        except Exception as exc:error=str(exc)
        finally:
            # 准入已撤销也必须释放本流接入，保留其他本机/Agent接入。
            try:
                service=self.node.extension_tools.services['serial']
                with service._lock:
                    service.call_for_context('close',{'sessionId':self.identifier},owner=self.owner,caller=self.caller)
            except Exception:pass
            try:
                self.meta.update(state='closed',closed=True,writable=False,error=error or self.meta.get('error',''))
                self.channel.write_meta(self.meta,in_place=True)
            except OSError:pass

    def stop(self):
        self.stop_flag.set()
        if threading.current_thread() is not self.thread:self.thread.join(timeout=3)


class MasterSerialStream:
    def __init__(self,node,peer,opened,caller,push=False,sink=None):
        self.node,self.peer,self.caller=node,peer,caller
        self.identifier=opened['sessionId'];self.binding=self.current_binding()
        self.channel=relay.RelayQueue.for_peer(node.channel_root(peer),peer).stream('serial-'+self.identifier)
        self.tail=self.channel.output_tail();self.buffer=ByteBuffer(opened.get('firstCursor',0))
        self.meta=dict(opened);self.sequence=0;self.pending=b'';self.write_lock=threading.Lock();self.read_lock=threading.Lock()
        self.next_meta=0;self.next_keepalive=0;self.uncertain=False;self.missing_since=None
        self.node_id=self.binding[1]
        # 推送只在主控 GUI 会话启用；Agent 会话保持原有请求-响应语义。
        self.push=bool(push and sink is not None and caller=='gui')
        self.sink=sink if self.push else None
        self.thread=None;self.stop_flag=threading.Event();self.wake_event=threading.Event()
        self.ack_lock=threading.Lock()
        self.pending_acks={};self.reported_failed=0;self.pushed_uncertain=False
        self.last_activity=time.monotonic();self.last_input=0.0

    @property
    def key(self):
        return f'serial:computer:{self.node_id}:{self.identifier}'

    def start(self):
        if not self.push or self.thread is not None or self.stop_flag.is_set():return
        self.thread=threading.Thread(target=self.pump,daemon=True,
            name='serial-push-'+self.identifier[:8])
        self.thread.start()

    def stop(self,wait=True):
        self.stop_flag.set();self.wake_event.set()
        thread=self.thread
        if thread is not None and wait and threading.current_thread() is not thread:
            thread.join(timeout=3)

    def current_binding(self):
        beat=self.node._peers.get(self.peer);record=self.node.pairing.get(beat.nodeId) if beat else None
        if (self.node._stop_flag.is_set() or self.node._pairing_blocks(self.peer)
                or self.node._peer_offline_reason(self.peer) or not record or not record.is_paired):
            raise PermissionError('串口电脑已离线或解除连接')
        return (self.node.channel_root(self.peer),beat.nodeId,beat.startedAt,record.connectionId)

    def check(self):
        if self.current_binding()!=self.binding:raise PermissionError('串口电脑启动/连接代次已变化，请重新打开')

    def update_meta(self):
        meta=self.channel.read_meta(report_missing=True)
        if meta and meta.get('status')=='missing':
            if self.missing_since is None:self.missing_since=time.monotonic()
            elif time.monotonic()-self.missing_since>3:raise RuntimeError('串口流状态文件丢失')
        if meta and 'state' in meta:
            self.missing_since=None
            if (meta.get('controlRevision',0)>=self.meta.get('controlRevision',0)
                    and meta.get('ackSeq',0)>=self.meta.get('ackSeq',0)):
                self.meta=meta
                # 明确的当前序号回执解除未知结果；不重放原输入。推送模式锁存失败，直到重开。
                if not self.push and self.uncertain and meta.get('ackSeq')==self.sequence:
                    self.uncertain=False
        self.next_meta=time.monotonic()+(.075 if not self.push else (.1 if self.pending_acks else .3))

    def transient_offline(self):
        """目标离线或连接尚未验证：保持线程等待心跳，不当作永久失败。"""
        try:
            return (bool(self.node._peer_offline_reason(self.peer))
                or not self.node.connection_ready(self.peer))
        except Exception:
            return False

    def push_payload(self,data=None,error='',transient=False):
        payload={**self.meta,'type':'serial-data','key':self.key,
            'identity':[False,self.node_id,self.identifier],
            'data':base64.b64encode(data).decode('ascii') if data else '',
            'nextCursor':self.buffer.total,'inputUncertain':bool(self.uncertain),
            'serialStreamProtocol':1,'serialPushProtocol':1}
        if error:payload['error']=error
        if transient:payload['transient']=True
        return payload

    def publish(self,data=None,error='',transient=False):
        if self.sink is None:return
        try:self.sink(self.push_payload(data,error,transient))
        except Exception:pass

    def check_acks(self):
        """异步写入的回执核对：失败或超时立即锁存并推给界面，不自动重发。"""
        if not self.push:return
        ack=self.meta.get('ackSeq',0)
        with self.ack_lock:
            failed=self.meta.get('failedSeq',0)
            if type(failed) is int and failed>self.reported_failed:
                self.reported_failed=failed
                self.pending_acks.clear();self.uncertain=True
            # 旧从机没有 failedSeq：只能在读到时按当前回执判断，作为降级路径。
            if (ack>=self.sequence and self.sequence>self.reported_failed
                    and not self.meta.get('ackSuccess',True)):
                self.reported_failed=self.sequence
                self.pending_acks.clear();self.uncertain=True
            for seq in [item for item in self.pending_acks if item<=ack]:
                self.pending_acks.pop(seq,None)
            if (self.pending_acks
                    and time.monotonic()-min(self.pending_acks.values())>ACK_WAIT_SECONDS):
                self.pending_acks.clear();self.uncertain=True

    def pump(self):
        """常驻读取 .out 并推给 GUI；本线程是该会话唯一的 .out 消费者。"""
        try:
            while not self.stop_flag.is_set():
                try:
                    self.check()
                except PermissionError as exc:
                    if not self.transient_offline():raise
                    self.publish(error=str(exc),transient=True)
                    self.stop_flag.wait(1)
                    continue
                now=time.monotonic()
                if now>=self.next_keepalive and self.write_lock.acquire(blocking=False):
                    try:self._append({'action':'keepalive'})
                    finally:self.write_lock.release()
                if now>=self.next_meta:self.update_meta()
                received=[]
                self.pending+=self.tail.read_new(262144)
                if self.tail.rebuilt or len(self.pending)>1024*1024:
                    raise RuntimeError('串口输出流重建或过长，拒绝重放')
                while b'\n' in self.pending:
                    frame,self.pending=self.pending.split(b'\n',1);message=json.loads(frame)
                    raw=base64.b64decode(message['data'],validate=True)
                    self.buffer.append(message['cursor'],raw);received.append(raw)
                self.check_acks()
                if received:
                    self.last_activity=now;self.publish(b''.join(received))
                elif self.uncertain!=self.pushed_uncertain:self.publish()
                self.pushed_uncertain=self.uncertain
                if self.meta.get('closed') or self.meta.get('state') not in (None,'running'):
                    self.publish();break
                # 有输入在途或刚输入按 10ms 追输出；刚有数据按 20ms；纯空闲等到写入唤醒或 1 秒。
                # Event.wait 的小超时在 Windows 会被量化到 ~15.6ms；短节拍用 sleep，长空闲才等唤醒。
                if received or self.pending_acks or time.monotonic()-self.last_input<.5:
                    time.sleep(.01)
                elif time.monotonic()-self.last_activity<1:
                    time.sleep(.02)
                else:
                    self.wake_event.wait(1)
                self.wake_event.clear()
        except Exception as exc:
            with self.ack_lock:
                if self.pending_acks:self.uncertain=True
                self.pending_acks.clear()
            self.meta.update(state='closed',closed=True,writable=False,error=str(exc))
            self.publish(error=str(exc))
        finally:
            self.thread=None

    def _append(self,message):
        if not self.channel.input_writer().append(json.dumps(message,ensure_ascii=False).encode('utf-8')+b'\n'):
            raise OSError('串口输入流不可写')
        self.next_keepalive=time.monotonic()+20

    def read(self,args):
        self.check()
        if self.push:
            # 推送模式下读取线程独占 .out；这里只读内存缓冲，供首次对齐与缺口补读。
            return {**self.meta,**self.buffer.read(args.get('cursor',0),args.get('limit',32768)),
                'serialStreamProtocol':1,'serialPushProtocol':1,'inputUncertain':self.uncertain}
        with self.read_lock:
            now=time.monotonic()
            if now>=self.next_keepalive and self.write_lock.acquire(blocking=False):
                try:self._append({'action':'keepalive'})
                finally:self.write_lock.release()
            if now>=self.next_meta:self.update_meta()
            self.pending+=self.tail.read_new(262144)
            if self.tail.rebuilt or len(self.pending)>1024*1024:raise RuntimeError('串口输出流重建或过长，拒绝重放')
            while b'\n' in self.pending:
                frame,self.pending=self.pending.split(b'\n',1);message=json.loads(frame)
                raw=base64.b64decode(message['data'],validate=True)
                self.buffer.append(message['cursor'],raw)
            return {**self.meta,**self.buffer.read(args.get('cursor',0),args.get('limit',32768)),
                'serialStreamProtocol':1,'inputUncertain':self.uncertain}

    def send(self,action,args):
        with self.write_lock:
            self.check()
            if self.meta.get('closed') or self.meta['state']!='running':
                if action=='close':return dict(self.meta,closed=True,state='closed')
                raise RuntimeError(self.meta.get('error') or '串口流已结束')
            if self.uncertain and action!='close':raise RemoteToolError('前一次输入结果未确认，请关闭后重新打开',uncertain=True)
            arguments={key:value for key,value in args.items() if key!='sessionId'}
            allowed={'write':{'generation','data','format','encoding','interCharDelayMs'},'control':{'generation','operation'},'close':set()}[action]
            if set(arguments)-allowed:raise ValueError('串口流参数无效')
            if action=='write':
                raw=input_bytes(arguments)
                delay_ms=arguments.get('interCharDelayMs', 0)
                if type(delay_ms) is not int or not 0 <= delay_ms <= 20:
                    raise ValueError('字符间隔必须为 0～20 ms 的整数')
                if delay_ms and len(raw)>2048:
                    raise ValueError('启用字符间隔时单次最多发送 2048 字节')
                if delay_ms:
                    # 新版从机需要原始文本来按字符分段；旧的无间隔输入仍走紧凑 base64 帧。
                    arguments={key:value for key,value in arguments.items() if key in allowed}
                else:
                    arguments={'generation':arguments.get('generation'),'data':base64.b64encode(raw).decode('ascii'),'format':'base64'}
            self.sequence+=1;sequence=self.sequence
            self._append({'seq':sequence,'action':action,'args':arguments})
            if self.push and action=='write':
                # GUI 输入投递即返回；回执由读取线程异步核对，失败通过推送通知界面。
                with self.ack_lock:
                    self.pending_acks[sequence]=time.monotonic()
                self.last_input=time.monotonic()
                self.wake_event.set()
                return dict(self.meta,queued=True,sequence=sequence)
            deadline=time.monotonic()+ACK_WAIT_SECONDS
            try:
                while time.monotonic()<deadline:
                    self.check();self.update_meta()
                    if self.meta.get('ackSeq')==sequence:
                        if not self.meta.get('ackSuccess'):raise RemoteToolError(self.meta.get('ackError') or '串口输入被拒绝')
                        return dict(self.meta,writtenBytes=self.meta.get('ackBytes',0))
                    if self.meta.get('closed'):break
                    time.sleep(.01)
            except RemoteToolError:
                raise
            except Exception as exc:
                self.uncertain=True
                raise RemoteToolError('串口输入回执未确认，禁止自动重发',uncertain=True) from exc
            self.uncertain=True
            raise RemoteToolError('串口输入回执未确认，禁止自动重发',uncertain=True)
