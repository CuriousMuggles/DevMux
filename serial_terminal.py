"""COM 交互服务：一个物理连接、字节游标、多接入和 GUI 优先控制。"""
import base64
import json
import re
import threading
import time
import uuid

from pathlib import Path

from xmodem_transfer import Transfer, MAX_FILE, CHUNK


DEFAULTS = dict(baudrate=921600, bytesize=8, parity='N', stopbits=1,
                flowcontrol='none', dtr=False, rts=False)
MAX_HISTORY = 1024 * 1024
MAX_PORTS = 8
MAX_SESSIONS = 64
IDLE_SECONDS = 1800


def enumerate_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError('缺少 pyserial，请安装 pyserial 或使用包含串口组件的新版程序') from exc
    return [dict(port=p.device, description=p.description or '', hwid=p.hwid or '',
                 manufacturer=p.manufacturer or '', serialNumber=p.serial_number or '')
            for p in sorted(list_ports.comports(), key=lambda p: (len(p.device), p.device))]


def connect_port(port, config):
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError('缺少 pyserial，请安装 pyserial') from exc
    # 设置线状态后再打开，避免枚举和配置阶段触发串口。
    transport = serial.Serial(port=None, baudrate=config['baudrate'], bytesize=config['bytesize'],
        parity=config['parity'], stopbits=config['stopbits'], timeout=.05, write_timeout=1,
        xonxoff=config['flowcontrol'] == 'xonxoff', rtscts=config['flowcontrol'] == 'rtscts',
        dsrdtr=config['flowcontrol'] == 'dsrdtr')
    transport.dtr, transport.rts = config['dtr'], config['rts']
    transport.port = port
    try:
        transport.open()
    except Exception:
        transport.close()
        raise
    return transport


def validate_config(args):
    config = {key: args.get(key, value) for key, value in DEFAULTS.items()}
    if type(config['baudrate']) is not int or not 1 <= config['baudrate'] <= 12000000:
        raise ValueError('波特率必须为 1～12000000 的整数')
    if type(config['bytesize']) is not int or config['bytesize'] not in (5, 6, 7, 8):
        raise ValueError('数据位必须为 5/6/7/8')
    if config['parity'] not in ('N', 'E', 'O', 'M', 'S'):
        raise ValueError('校验位无效')
    if type(config['stopbits']) not in (int, float) or config['stopbits'] not in (1, 1.5, 2):
        raise ValueError('停止位必须为 1/1.5/2')
    if config['flowcontrol'] not in ('none', 'xonxoff', 'rtscts', 'dsrdtr'):
        raise ValueError('流控无效')
    if any(type(config[key]) is not bool for key in ('dtr', 'rts')):
        raise ValueError('DTR/RTS 必须为布尔值')
    return config


def input_bytes(args):
    data, mode = args.get('data'), args.get('format', 'text')
    if not isinstance(data, str) or len(data) > 262144:
        raise ValueError('输入必须为有界字符串')
    if mode == 'hex':
        if not re.fullmatch(r'[0-9a-fA-F\s]*', data):
            raise ValueError('HEX 只能包含十六进制字节和空白')
        try:
            raw = bytes.fromhex(data)
        except ValueError as exc:
            raise ValueError('HEX 必须为完整的字节对') from exc
    elif mode == 'base64':
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('base64 输入无效') from exc
    elif mode == 'text':
        encoding = args.get('encoding', 'utf-8')
        if encoding not in ('utf-8', 'gbk', 'ascii', 'latin-1'):
            raise ValueError('编码必须为 utf-8/gbk/ascii/latin-1')
        raw = data.encode(encoding)
    else:
        raise ValueError('格式必须为 text/hex/base64')
    if len(raw) > 65536:
        raise ValueError('单次输入不能超过 65536 字节')
    return raw


class SerialService:
    NAME = '串口'
    ACTIONS = {'config', 'ports', 'open', 'read', 'write', 'control', 'configure', 'close', 'status', 'xmodem-start', 'xmodem-upload', 'xmodem-run',
               'xmodem-status', 'xmodem-download', 'xmodem-cancel'}
    FIELDS = {
        'config': set(), 'ports': set(),
        'open': {'port', 'requestId', *DEFAULTS},
        'read': {'sessionId', 'cursor', 'limit'},
        'write': {'sessionId', 'generation', 'data', 'format', 'encoding', 'interCharDelayMs'},
        'control': {'sessionId', 'operation', 'generation'},
        'configure': {'sessionId', 'generation', *DEFAULTS},
        'close': {'sessionId'}, 'status': {'sessionId'},
        'xmodem-start': {'sessionId', 'generation', 'requestId', 'direction', 'size', 'blockSize', 'checksum'},
        'xmodem-upload': {'sessionId', 'transferId', 'offset', 'data'},
        'xmodem-run': {'sessionId', 'transferId'},
        'xmodem-status': {'sessionId', 'transferId'},
        'xmodem-download': {'sessionId', 'transferId', 'offset', 'limit'},
        'xmodem-cancel': {'sessionId', 'transferId'},
    }

    def __init__(self, config_path, *, port_source=enumerate_ports, connector=connect_port, config_storage=None):
        self.config_path = Path(config_path)
        self.config_storage = config_storage
        self._lock = threading.RLock()
        self.port_source, self.connector = port_source, connector
        self.ports = {}
        self.sessions = {}
        self.requests = {}
        self._closed = False
        config = config_storage.read() if config_storage is not None else None
        try:
            if config is None:
                config = json.loads(self.config_path.read_text(encoding='utf-8'))
            self.remote_allowed = config.get('remoteAllowed') is True
        except (OSError, ValueError, AttributeError):
            self.remote_allowed = False

    def set_remote_permission(self, allowed):
        if type(allowed) is not bool:
            raise ValueError('权限值必须为布尔值')
        if self.config_storage is not None:
            self.config_storage.write({'remoteAllowed': allowed})
        else:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'remoteAllowed': allowed}), encoding='utf-8')
            temporary.replace(self.config_path)
        self.remote_allowed = allowed
        if not allowed:
            self.close_owner(remote_only=True)
        return self._result({'remoteAllowed': allowed})

    @staticmethod
    def _result(data):
        return dict(success=True, output='', data=data)

    def _view(self, handle):
        slot = handle['slot']
        controller = self.sessions.get(slot['controller'])
        return dict(sessionId=handle['id'], port=slot['port'], config=slot['config'],
            state=slot['state'], error=slot['error'], generation=slot['generation'],
            controlRevision=slot['controlRevision'], firstCursor=slot['total']-len(slot['history']),
            controller=controller['caller'] if controller else '',
            writable=slot['controller'] == handle['id'], nextCursor=slot['total'],
            shared=sum(h['slot'] is slot and not h['closed'] for h in self.sessions.values()) > 1,
            serialConfigProtocol=1, closed=handle['closed'], xmodem=slot['transfer'].view() if slot.get('transfer') else None)

    def _handle(self, args, owner, caller, *, allow_closed=False):
        identifier = args.get('sessionId')
        handle = self.sessions.get(identifier) if isinstance(identifier, str) else None
        if not handle or handle['owner'] != owner or handle['caller'] != caller:
            raise PermissionError('串口接入不存在或不属于当前调用者')
        if handle['closed'] and not allow_closed:
            raise RuntimeError('串口接入已关闭')
        handle['slot']['activity'] = time.monotonic()
        return handle

    def call_for_context(self, action, args, *, owner='local', caller='agent'):
        # 注册表锁覆盖本调用与撤销权限；读线程仅短时持锁更新字节历史。
        if action not in self.FIELDS or set(args) - self.FIELDS[action]:
            raise ValueError('串口动作或参数无效，禁止传入 caller/owner 等身份字段')
        if action == 'config':
            return self._result({'defaults': DEFAULTS, 'remoteAllowed': self.remote_allowed,
                'formats': ['text', 'hex', 'base64'], 'protocols': ['raw', 'xmodem'], 'xmodem': True,
                'xmodemMaxBytes': MAX_FILE, 'xmodemChunkBytes': CHUNK})
        if action == 'ports':
            ports = self.port_source()
            return self._result({'ports': [{**p, 'opened': p['port'].upper() in self.ports}
                                           for p in ports]})
        if action == 'open':
            return self._open(args, owner, caller)
        handle = self._handle(args, owner, caller, allow_closed=action in ('read', 'status', 'close'))
        slot = handle['slot']
        if action.startswith('xmodem-'):
            return self._xmodem(action, args, handle)
        if action == 'status':
            return self._result(self._view(handle))
        if action == 'read':
            cursor, limit = args.get('cursor', 0), args.get('limit', 32768)
            if type(cursor) is not int or cursor < 0 or cursor > slot['total']:
                raise ValueError('输出游标无效')
            if type(limit) is not int or not 1 <= limit <= 65536:
                raise ValueError('读取上限必须为 1～65536')
            start = slot['total'] - len(slot['history'])
            effective = max(cursor, start)
            raw = bytes(slot['history'][effective-start:effective-start+limit])
            return self._result({**self._view(handle), 'data': base64.b64encode(raw).decode('ascii'),
                'nextCursor': effective+len(raw), 'droppedBytes': max(0, start-cursor),
                'hasMore': effective+len(raw) < slot['total']})
        if action == 'close':
            self._detach(handle)
            return self._result(self._view(handle))
        if slot['state'] != 'running':
            raise RuntimeError(slot['error'] or '串口已断开，请重新打开')
        if slot.get('transfer') and slot['transfer'].active:
            raise RuntimeError('XMODEM 正在准备或传输，普通输入和控制暂不可用')
        if action == 'configure':
            return self._configure(args, handle)
        if action == 'control':
            operation = args.get('operation')
            if operation not in ('take', 'release'):
                raise ValueError('控制操作必须为 take/release')
            if args.get('generation') != slot['generation']:
                raise PermissionError('控制代次已变化，请先读取状态')
            if operation == 'take':
                self._claim(handle)
            elif slot['controller'] == handle['id']:
                self._controller(slot, '')
            else:
                raise PermissionError('当前接入没有输入权')
            return self._result(self._view(handle))
        raw = input_bytes(args)
        delay_ms = args.get('interCharDelayMs', 0)
        if type(delay_ms) is not int or not 0 <= delay_ms <= 20:
            raise ValueError('字符间隔必须为 0～20 ms 的整数')
        if delay_ms and len(raw) > 2048:
            raise ValueError('启用字符间隔时单次最多发送 2048 字节')
        # GUI 仍须携带当前代次，防止旧窗口的排队输入越过交还边界。
        if args.get('generation') != slot['generation']:
            raise PermissionError('控制代次已变化，请先读取状态，旧输入未发送')
        if caller == 'gui':
            self._claim(handle)
        if slot['controller'] != handle['id']:
            raise PermissionError('输入权由其他接入持有；GUI 输入优先，请申请控制权')
        try:
            if delay_ms and len(raw) > 1:
                if args.get('format', 'text') == 'text':
                    # 按字符分段，UTF-8/GBK 多字节序列仍在一次串口写入中发送。
                    chunks = [char.encode(args.get('encoding', 'utf-8')) for char in args['data']]
                else:
                    # HEX/base64 没有字符编码语义，按字节间隔发送。
                    chunks = [raw[index:index+1] for index in range(len(raw))]
                written = 0
                for index, chunk in enumerate(chunks):
                    if index:
                        time.sleep(delay_ms / 1000)
                    count = slot['transport'].write(chunk)
                    if count != len(chunk):
                        raise OSError(f'仅发送 {written+count}/{len(raw)} 字节，结果未确认，禁止自动重发')
                    written += count
            else:
                written = slot['transport'].write(raw)
                if written != len(raw):
                    raise OSError(f'仅发送 {written}/{len(raw)} 字节，结果未确认，禁止自动重发')
        except Exception as exc:
            self._stop_slot(slot, str(exc))
            raise RuntimeError(f'串口写入失败，可能已发送部分字节，不会自动重发：{exc}') from exc
        return self._result({**self._view(handle), 'writtenBytes': written})

    @staticmethod
    def _transport_settings(config):
        return {key: config[key] for key in ('baudrate', 'bytesize', 'parity', 'stopbits')} | {
            'xonxoff': config['flowcontrol'] == 'xonxoff',
            'rtscts': config['flowcontrol'] == 'rtscts',
            'dsrdtr': config['flowcontrol'] == 'dsrdtr'}

    def _configure(self, args, handle):
        slot = handle['slot']
        config = validate_config({**slot['config'], **args})
        if handle['caller'] != 'gui' or slot['controller'] != handle['id']:
            raise PermissionError('仅持有输入权的GUI接入可修改连接参数')
        if args.get('generation') != slot['generation']:
            raise PermissionError('控制代次已变化，连接参数未修改')
        if any(not h['closed'] and h['slot'] is slot and h is not handle for h in self.sessions.values()):
            raise PermissionError('COM仍有其他接入，请先关闭其他接入后修改连接参数')
        if config == slot['config']:
            return self._result(self._view(handle))
        transport, old = slot['transport'], slot['config']
        def apply(values):
            transport.apply_settings(self._transport_settings(values))
            transport.dtr, transport.rts = values['dtr'], values['rts']
        try:
            apply(config)
        except Exception as exc:
            try:
                apply(old)
            except Exception as rollback:
                self._stop_slot(slot, f'连接参数应用失败且无法恢复，串口已关闭：{exc}；{rollback}')
                raise RuntimeError(slot['error']) from exc
            raise RuntimeError(f'连接参数应用失败，已恢复原配置：{exc}') from exc
        slot['config'] = config
        slot['generation'] = uuid.uuid4().hex
        slot['controlRevision'] += 1
        return self._result(self._view(handle))

    def _xmodem(self, action, args, handle):
        slot = handle['slot']
        transfer = slot.get('transfer')
        if action == 'xmodem-start':
            request = args.get('requestId')
            if not isinstance(request, str) or not re.fullmatch('[a-f0-9]{32}', request):
                raise ValueError('传输必须提供32位requestId')
            signature = {key: args.get(key) for key in ('direction', 'size', 'blockSize', 'checksum')}
            previous = handle['transferRequests'].get(request)
            if previous:
                if previous != signature:
                    raise ValueError('requestId已用于其他传输参数')
                if not transfer or transfer.request_id != request or slot['transferOwner'] != handle['id']:
                    raise RuntimeError('原传输已结束且被替换，不会重复启动')
                return self._result({**transfer.view(), **self._view(handle)})
            if len(handle['transferRequests']) >= 64:
                raise RuntimeError('本接入传输次数已达上限，请关闭重开')
            if transfer and (transfer.active or (transfer.thread and transfer.thread.is_alive())):
                raise RuntimeError('该串口已有XMODEM任务')
            if slot['state'] != 'running' or args.get('generation') != slot['generation']:
                raise PermissionError('连接或控制代次已变化，请读取状态')
            direction, size = args.get('direction'), args.get('size', 0)
            block, checksum = args.get('blockSize', 128), args.get('checksum', 'crc')
            if direction not in ('send', 'receive') or type(size) is not int or not 0 <= size <= MAX_FILE:
                raise ValueError('方向或文件大小无效（至多64MiB）')
            if type(block) is not int or block not in (128, 1024) or checksum not in ('crc', 'checksum'):
                raise ValueError('分包或校验选项无效')
            if slot['config']['bytesize'] != 8 or slot['config']['flowcontrol'] == 'xonxoff':
                raise ValueError('XMODEM要求8数据位且关闭XON/XOFF软件流控')
            self._claim(handle)
            next_transfer = Transfer(direction, size, block, checksum, request)
            if transfer:
                transfer.dispose()
            transfer = slot['transfer'] = next_transfer
            slot['transferOwner'] = handle['id']
            handle['transferRequests'][request] = signature
            slot['generation'] = uuid.uuid4().hex
            slot['controlRevision'] += 1
        else:
            if not transfer or args.get('transferId') != transfer.id or slot.get('transferOwner') != handle['id']:
                raise PermissionError('传输不存在或不属于当前接入')
            if transfer.closed:
                raise RuntimeError('传输文件已释放')
            if action == 'xmodem-upload':
                value = args.get('data')
                if not isinstance(value, str) or len(value) > 4*((CHUNK+2)//3):
                    raise ValueError('上传块无效或过大')
                raw = base64.b64decode(value, validate=True)
                if len(raw) > CHUNK:
                    raise ValueError('上传块超过32KiB')
                transfer.upload(args.get('offset'), raw)
            elif action == 'xmodem-run':
                if transfer.state == 'preparing':
                    if transfer.direction == 'send' and transfer.uploaded != transfer.size:
                        raise ValueError('文件尚未上传完整')
                    transfer.state, transfer.phase = 'waiting', '等待协议握手'
                    transfer.thread = threading.Thread(target=self._run_transfer,
                        args=(slot, transfer), daemon=True, name='XMODEM-'+slot['port'])
                    transfer.thread.start()
                # 运行中和已结束均返回原任务，绝不重新启动。
            elif action == 'xmodem-cancel':
                if transfer.active:
                    transfer.cancel.set()
                    if transfer.thread is None:
                        transfer.state, transfer.phase = 'cancelled', '准备已取消'
                        transfer.file.close()
                        slot['generation'] = uuid.uuid4().hex
                        slot['controlRevision'] += 1
                    else:
                        transfer.state, transfer.phase = 'cancelling', '正在取消'
            elif action == 'xmodem-download':
                if transfer.direction != 'receive' or transfer.state != 'succeeded':
                    raise RuntimeError('接收尚未成功，不能下载')
                offset, limit = args.get('offset', 0), args.get('limit', CHUNK)
                if type(offset) is not int or not 0 <= offset <= transfer.transferred or type(limit) is not int or not 1 <= limit <= CHUNK:
                    raise ValueError('下载偏移或块大小无效')
                transfer.file.seek(offset)
                raw = transfer.file.read(limit)
                return self._result({**transfer.view(), 'data': base64.b64encode(raw).decode('ascii'),
                    'nextOffset': offset+len(raw), 'eof': offset+len(raw) == transfer.transferred})
        slot['activity'] = time.monotonic()
        return self._result({**transfer.view(), **self._view(handle)})

    def _run_transfer(self, slot, transfer):
        def write(raw, abort=False):
            with self._lock:
                if slot['stop'].is_set():
                    raise OSError('串口断开')
                if transfer.cancel.is_set() and not abort:
                    from xmodem_transfer import Cancelled
                    raise Cancelled(transfer.error or '已取消')
                try:
                    written = slot['transport'].write(raw)
                except Exception as exc:
                    self._stop_slot(slot, 'XMODEM写入失败：' + str(exc))
                    raise
                if written != len(raw):
                    self._stop_slot(slot, 'XMODEM写入不完整，连接已关闭')
                    raise OSError('XMODEM写入不完整')
        try:
            transfer.execute(write)
        finally:
            with self._lock:
                if slot.get('transfer') is transfer:
                    if transfer.trailing:
                        slot['history'].extend(transfer.trailing)
                        slot['total'] += len(transfer.trailing)
                        del slot['history'][:-MAX_HISTORY]
                        transfer.trailing.clear()
                    slot['generation'] = uuid.uuid4().hex
                    slot['controlRevision'] += 1
                if transfer.closed or transfer.state != 'succeeded' or transfer.direction == 'send':
                    transfer.file.close()

    @staticmethod
    def _controller(slot, identifier):
        if slot['controller'] != identifier:
            slot['controller'] = identifier
            slot['generation'] = uuid.uuid4().hex
            slot['controlRevision'] += 1

    def _claim(self, handle):
        slot = handle['slot']
        current = self.sessions.get(slot['controller'])
        if slot.get('transfer') and slot['transfer'].active and slot.get('transferOwner') != handle['id']:
            raise PermissionError('XMODEM 传输期间不能抢占输入权')
        if current and current['id'] != handle['id']:
            if handle['caller'] != 'gui' or current['caller'] == 'gui':
                raise PermissionError('串口已有输入控制者；GUI 优先于 Agent，其他 GUI 请先交还')
        self._controller(slot, handle['id'])

    def _open(self, args, owner, caller):
        if self._closed:
            raise RuntimeError('串口服务已停止，请刷新应用')
        port = args.get('port')
        if not isinstance(port, str) or not re.fullmatch(r'COM[1-9][0-9]*', port, re.I):
            raise ValueError('请选择有效 COM 端口')
        port = port.upper()
        config = validate_config(args)
        request = args.get('requestId')
        if not isinstance(request, str) or not re.fullmatch('[a-f0-9]{32}', request):
            raise ValueError('open 必须提供 32 位十六进制 requestId，超时后查询原请求，不重复打开')
        key = (owner, caller, request)
        if key in self.requests:
            handle = self.sessions[self.requests[key]]
            if handle['slot']['port'] != port or handle['slot']['config'] != config:
                raise ValueError('requestId 已用于不同串口配置')
            return self._result(self._view(handle))
        if sum(not h['closed'] for h in self.sessions.values()) >= MAX_SESSIONS:
            raise RuntimeError('串口接入已达上限')
        slot = self.ports.get(port)
        if slot and slot['config'] != config:
            raise RuntimeError('该 COM 已打开且参数不同，请先关闭已有连接')
        if not slot:
            if len(self.ports) >= MAX_PORTS:
                raise RuntimeError('串口连接已达上限')
            if port not in {p['port'].upper() for p in self.port_source()}:
                raise RuntimeError('所选 COM 不在当前端口列表，请刷新')
            try:
                transport = self.connector(port, config)
            except Exception as exc:
                raise RuntimeError(f'无法打开 {port}，请检查占用、驱动和参数：{exc}') from exc
            slot = dict(port=port, config=config, transport=transport, state='running', error='',
                history=bytearray(), total=0, activity=time.monotonic(), controller='',
                generation=uuid.uuid4().hex, controlRevision=0, stop=threading.Event(), thread=None,
                transfer=None, transferOwner='')
            self.ports[port] = slot
        handle = dict(id=uuid.uuid4().hex, slot=slot, owner=owner, caller=caller, closed=False, transferRequests={})
        self.sessions[handle['id']] = handle
        self.requests[key] = handle['id']
        current = self.sessions.get(slot['controller'])
        if (not slot.get('transfer') or not slot['transfer'].active) and (
                not current or (caller == 'gui' and current['caller'] == 'agent')):
            self._claim(handle)
        if slot['thread'] is None:
            slot['thread'] = threading.Thread(target=self._read_loop, args=(slot,), daemon=True,
                                             name='COM-reader-'+port)
            slot['thread'].start()
        self._prune()
        return self._result(self._view(handle))

    def _read_loop(self, slot):
        while not slot['stop'].is_set():
            try:
                transport = slot['transport']
                # 有积压就读取已有字节；空闲只等首字节，短回包不必凑满32KB或等超时。
                available = getattr(transport, 'in_waiting', 0)
                raw = transport.read(max(1, min(32768, available)))
                with self._lock:
                    if slot['stop'].is_set():
                        break
                    transfer = slot.get('transfer')
                    if raw and transfer and transfer.state in ('waiting', 'running', 'cancelling'):
                        transfer.feed(raw)
                        raw = b''
                    if transfer and not transfer.active and transfer.trailing:
                        slot['history'].extend(transfer.trailing)
                        slot['total'] += len(transfer.trailing)
                        transfer.trailing.clear()
                    if raw:
                        slot['history'].extend(raw)
                        slot['total'] += len(raw)
                    if len(slot['history']) > MAX_HISTORY:
                        del slot['history'][:-MAX_HISTORY]
                    # 输出不延长使用租约，无人读取的持续日志仍会被回收。
                    if time.monotonic() - slot['activity'] > IDLE_SECONDS:
                        self._stop_slot(slot, '串口接入闲置超过 30 分钟，已回收')
            except Exception as exc:
                with self._lock:
                    if not slot['stop'].is_set():
                        self._stop_slot(slot, f'串口断开：{exc}')
                break

    def _stop_slot(self, slot, reason=''):
        transfer = slot.get('transfer')
        if transfer:
            if transfer.active and reason:
                transfer.error = '串口断开：' + reason
            transfer.dispose()
        slot['stop'].set()
        slot.update(state='disconnected' if reason else 'closed', error=reason)
        self._controller(slot, '')
        if self.ports.get(slot['port']) is slot:
            self.ports.pop(slot['port'])
        try:
            slot['transport'].close()
        except Exception:
            pass

    def _detach(self, handle):
        handle['closed'] = True
        slot = handle['slot']
        if slot.get('transferOwner') == handle['id'] and slot.get('transfer'):
            transfer = slot['transfer']
            transfer.dispose()
            if transfer.thread is None:
                transfer.state, transfer.phase = 'cancelled', '接入已关闭'
        if slot['controller'] == handle['id']:
            self._controller(slot, '')
        if not any(not h['closed'] and h['slot'] is slot for h in self.sessions.values()):
            self._stop_slot(slot)

    def _prune(self):
        # 关闭和断开接入也有界，防止保留多个 1MiB 缓冲无限增长。
        inactive = [h for h in self.sessions.values() if h['closed'] or h['slot']['state'] != 'running']
        for handle in inactive[:-64]:
            self.sessions.pop(handle['id'], None)
        self.requests = {key: value for key, value in self.requests.items() if value in self.sessions}

    def close_owner(self, owner=None, *, remote_only=False):
        with self._lock:
            for handle in list(self.sessions.values()):
                matching = owner is None or handle['owner'] == owner or handle['owner'].startswith(owner+':')
                if matching and (not remote_only or handle['owner'] != 'local'):
                    self._detach(handle)
            self._prune()

    def close(self):
        with self._lock:
            self._closed = True
            threads = [slot['thread'] for slot in self.ports.values()]
            threads += [slot['transfer'].thread for slot in self.ports.values() if slot.get('transfer')]
            self.close_owner()
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
