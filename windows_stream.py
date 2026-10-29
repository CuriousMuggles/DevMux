"""PowerShell 持续字节流：开关会话走控制队列，交互不逐键往返。"""
import base64
import json
import threading
import time

import relay
from windows_executor import OutputBuffer


class SlaveStream:
    def __init__(self, node, request, identifier, owner):
        self.node, self.request, self.identifier, self.owner = node, request, identifier, owner
        self.channel = node.queue.stream('win-' + identifier)
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self.pump, daemon=True, name='powershell-stream')
        self.channel.write_meta({'state': 'running', 'sessionId': identifier})
        self.thread.start()

    def authorize(self):
        reason = self.node._authorize(self.request)
        if reason:
            raise PermissionError(reason)
        self.node._windows_owner(self.request, 'session_write')

    def pump(self):
        tail = self.channel.input_tail()
        writer = self.channel.output_writer()
        pending = b''
        cursor = 0
        last_input = time.monotonic()
        error = ''
        try:
            while not self.stop_flag.is_set():
                self.authorize()
                if time.monotonic() - last_input > 1800:
                    raise RuntimeError('PowerShell 接入长时间无读取或输入，已关闭')
                pending += tail.read_new(65536)
                if tail.rebuilt or len(pending) > 512 * 1024:
                    raise RuntimeError('输入流被重建或帧长度超过限制，拒绝重放')
                while b'\n' in pending:
                    frame, pending = pending.split(b'\n', 1)
                    message = json.loads(frame.decode('utf-8'))
                    self.authorize()
                    if message.get('action') not in ('session_write', 'session_resize', 'keepalive'):
                        raise ValueError('未知终端流操作')
                    last_input = time.monotonic()
                    if message['action'] != 'keepalive':
                        self.node.windows_service.session_action(message['action'],
                            {**message.get('args', {}), 'sessionId': self.identifier}, self.owner)
                output = self.node.windows_service.session_action('session_read',
                    {'sessionId': self.identifier, 'cursor': cursor, 'limit': 65536}, self.owner)
                if output['droppedBytes'] and not writer.append(
                        f"\r\n[较早输出已裁剪 {output['droppedBytes']} 字节]\r\n".encode('utf-8')):
                    raise OSError('PowerShell 输出流不可写')
                if output['data'] and not writer.append(base64.b64decode(output['data'])):
                    raise OSError('PowerShell 输出流不可写')
                cursor = output['nextCursor']
                if output['state'] != 'running' and not output['hasMore']:
                    break
                self.stop_flag.wait(0 if output['hasMore'] else .015)
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                self.node.windows_service.session_action('session_close',
                    {'sessionId': self.identifier}, self.owner)
            except Exception:
                pass
            try:
                self.channel.write_meta({'state': 'closed', 'sessionId': self.identifier, 'error': error})
            except OSError:
                pass

    def stop(self):
        self.stop_flag.set()
        self.thread.join(timeout=3)


class MasterStream:
    def __init__(self, node, peer, identifier, stream_id):
        self.node, self.peer, self.identifier = node, peer, identifier
        self.binding = self.current_binding()
        self.channel = relay.RelayQueue.for_peer(node.channel_root(peer), peer).stream(stream_id)
        self.tail = self.channel.output_tail()
        self.buffer = OutputBuffer()
        self.read_lock, self.write_lock = threading.Lock(), threading.Lock()
        self.meta = {'state': 'running'}
        self.next_meta = 0
        self.next_keepalive = 0
        self.missing_since = None

    def current_binding(self):
        beat = self.node._peers.get(self.peer)
        record = self.node.pairing.get(beat.nodeId) if beat else None
        if (self.node._stop_flag.is_set() or self.node._pairing_blocks(self.peer)
                or self.node._peer_offline_reason(self.peer) or not record or not record.is_paired):
            raise PermissionError('电脑连接已离线或解除')
        return (self.node.channel_root(self.peer), beat.nodeId, beat.startedAt, record.connectionId)

    def check(self):
        if self.current_binding() != self.binding:
            raise PermissionError('电脑连接代次或部署已变化，请重新打开终端')

    def send(self, action, args):
        with self.write_lock:
            self.check()
            if self.meta['state'] != 'running':
                raise RuntimeError(self.meta.get('error') or 'PowerShell 已结束')
            if action == 'session_write':
                if not isinstance(args.get('data'), str) or len(args['data']) > 65536:
                    raise ValueError('终端输入无效')
            elif action == 'session_resize':
                if not all(1 <= int(args.get(name, 0)) <= 1000 for name in ('cols', 'rows')):
                    raise ValueError('终端尺寸无效')
            data = json.dumps({'action': action, 'args': args}, ensure_ascii=False).encode('utf-8') + b'\n'
            if not self.channel.input_writer().append(data):
                raise OSError('PowerShell 输入流不可写')
            self.next_keepalive = time.monotonic() + 20
            return {'sessionId': self.identifier, 'state': 'running', 'queued': True}

    def read(self, args):
        with self.read_lock:
            self.check()
            now = time.monotonic()
            if now >= self.next_keepalive and self.meta['state'] == 'running':
                self.send('keepalive', {})
            data = self.tail.read_new(65536)
            if self.tail.rebuilt:
                raise RuntimeError('输出流被重建，请重新打开终端')
            if data:
                self.buffer.append(data)
            if now >= self.next_meta:
                meta = self.channel.read_meta(report_missing=True)
                if meta and meta.get('status') == 'missing':
                    if self.missing_since is None:
                        self.missing_since = now
                    elif now - self.missing_since > 3:
                        raise RuntimeError('PowerShell 会话状态文件已丢失')
                if meta and 'state' in meta:
                    self.missing_since = None
                    self.meta = meta
                    if meta['state'] != 'running':
                        # 先读字节再读元信息时，最后一段输出可能刚写入。
                        final_data = self.tail.read_new(65536)
                        if final_data:
                            self.buffer.append(final_data)
                self.next_meta = now + .5
            return {**self.buffer.read(args.get('cursor', 0), args.get('limit', 32768),
                    final=self.meta['state'] != 'running'), **self.meta, 'sessionId': self.identifier}
