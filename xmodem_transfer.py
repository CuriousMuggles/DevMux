"""XMODEM 协议：目标端分包、有限重试与可取消字节通道。"""
import binascii
import queue
import tempfile
import threading
import time
import uuid

SOH, STX, EOT, ACK, NAK, CAN, CRC = 1, 2, 4, 6, 21, 24, 67
MAX_FILE = 64 * 1024 * 1024
CHUNK = 32768
TIMEOUT = 10
HANDSHAKE = 60
RETRIES = 10
ACTIVE = ('preparing', 'waiting', 'running', 'cancelling')


class Cancelled(RuntimeError):
    pass


def trailer(data, crc):
    return binascii.crc_hqx(data, 0).to_bytes(2, 'big') if crc else bytes([sum(data) & 255])


class Transfer:
    def __init__(self, direction, size, block_size, checksum, request_id):
        self.id = uuid.uuid4().hex
        self.request_id = request_id
        self.direction, self.size = direction, size
        self.block_size, self.checksum = block_size, checksum
        self.file = tempfile.TemporaryFile()
        self.state, self.error = 'preparing', ''
        self.uploaded = self.transferred = self.retries = 0
        self.phase = '准备文件'
        self.cancel = threading.Event()
        self.rx = queue.Queue(maxsize=256)
        self.pending = bytearray()
        self.trailing = bytearray()
        self.thread = None
        self.closed = False

    @property
    def active(self):
        return self.state in ACTIVE

    def view(self):
        return dict(transferId=self.id, requestId=self.request_id, direction=self.direction,
                    state=self.state, error=self.error, phase=self.phase, size=self.size,
                    uploadedBytes=self.uploaded, transferredBytes=self.transferred,
                    retries=self.retries, blockSize=self.block_size, checksum=self.checksum)

    def upload(self, offset, data):
        if self.direction != 'send' or self.state != 'preparing':
            raise RuntimeError('当前传输不接受上传')
        if type(offset) is not int or offset < 0 or offset + len(data) > self.size:
            raise ValueError('上传偏移或文件大小无效')
        if offset < self.uploaded:
            self.file.seek(offset)
            if offset + len(data) > self.uploaded or self.file.read(len(data)) != data:
                raise ValueError('重复上传内容不一致')
        elif offset == self.uploaded:
            self.file.seek(offset)
            self.file.write(data)
            self.uploaded += len(data)
        else:
            raise ValueError('上传必须连续，不能跳过偏移')

    def feed(self, raw):
        try:
            self.rx.put_nowait(raw)
        except queue.Full:
            self.error = '协议输入缓冲已满'
            self.cancel.set()

    def read(self, count, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while len(self.pending) < count:
            if self.cancel.is_set():
                raise Cancelled(self.error or '已取消')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b''
            try:
                self.pending.extend(self.rx.get(timeout=min(.05, remaining)))
            except queue.Empty:
                pass
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def control(self, timeout=TIMEOUT):
        value = self.read(1, timeout)
        if value == bytes([CAN]):
            following = self.read(1, min(timeout, 1))
            if following == bytes([CAN]):
                raise Cancelled('对端取消传输')
            self.pending[:0] = following
            return b''
        return value

    def retry(self):
        self.retries += 1

    def send(self, write):
        self.phase = '等待对端接收（C / NAK）'
        deadline = time.monotonic() + HANDSHAKE
        crc = None
        while time.monotonic() < deadline:
            value = self.control(min(1, max(.01, deadline-time.monotonic())))
            if value in (bytes([CRC]), bytes([NAK])):
                crc = value == bytes([CRC])
                break
        if crc is None:
            raise TimeoutError('等待接收方握手超时')
        if self.block_size == 1024 and not crc:
            raise RuntimeError('XMODEM-1K 需要对端以 C 请求 CRC；可改用128字节')
        self.checksum = 'crc' if crc else 'checksum'
        self.state, self.phase = 'running', '发送数据'
        self.file.seek(0)
        number = 1
        while self.transferred < self.size:
            raw = self.file.read(self.block_size)
            payload = raw.ljust(self.block_size, b'\x1a')
            packet = bytes([STX if self.block_size == 1024 else SOH, number, number ^ 255]) + payload + trailer(payload, crc)
            for _ in range(RETRIES):
                write(packet)
                if self.control() == bytes([ACK]):
                    break
                self.retry()
            else:
                raise TimeoutError('数据包未获确认，重试次数已达上限')
            self.transferred += len(raw)
            number = (number + 1) & 255
        self.phase = '等待结束确认'
        for _ in range(RETRIES):
            write(bytes([EOT]))
            if self.control() == bytes([ACK]):
                return
            self.retry()
        raise TimeoutError('EOT 未获确认，传输结果未确认')

    def drain(self):
        # 错误帧后等待短暂静默，清掉其余碎片再请求重传。
        self.pending.clear()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not self.cancel.is_set():
            try:
                self.rx.get(timeout=.1)
            except queue.Empty:
                return

    def receive(self, write):
        crc = self.checksum == 'crc'
        signal = CRC if crc else NAK
        self.phase = '等待对端发送'
        number, blocks, errors = 1, 0, 0
        write(bytes([signal]))
        deadline = time.monotonic() + HANDSHAKE
        while True:
            value = self.control(TIMEOUT)
            if value == bytes([EOT]):
                write(bytes([ACK]))
                # 结束ACK丢失时重复确认；不会重新提交文件。
                self.phase = '确认传输结束'
                until = time.monotonic() + TIMEOUT + .25
                while time.monotonic() < until:
                    extra = self.control(min(.1, until-time.monotonic()))
                    if extra == bytes([EOT]):
                        write(bytes([ACK]))
                    elif extra:
                        # 对端已回到终端，保留提示符/日志，不能吞掉传输后的输出。
                        self.trailing.extend(extra + self.pending)
                        self.pending.clear()
                        for _ in range(256):
                            try:
                                self.trailing.extend(self.rx.get_nowait())
                            except queue.Empty:
                                break
                        return
                return
            if value not in (bytes([SOH]), bytes([STX])):
                if blocks == 0 and time.monotonic() >= deadline:
                    raise TimeoutError('等待发送方握手超时')
                errors += 1
                if errors >= RETRIES:
                    raise TimeoutError('等待数据包超时或无效帧过多')
                self.retry()
                write(bytes([signal if blocks == 0 else NAK]))
                continue
            length = 128 if value[0] == SOH else 1024
            packet = self.read(2 + length + (2 if crc else 1))
            valid = (len(packet) == 2 + length + (2 if crc else 1)
                     and packet[0] ^ packet[1] == 255
                     and trailer(packet[2:2+length], crc) == packet[2+length:])
            if not valid:
                errors += 1
                self.retry()
                if errors >= RETRIES:
                    raise RuntimeError('数据校验失败次数已达上限')
                self.drain()
                write(bytes([NAK]))
                continue
            if packet[0] == number:
                if self.transferred + length > MAX_FILE:
                    raise ValueError('接收文件超过64MiB上限')
                self.file.write(packet[2:2+length])
                self.transferred += length
                number = (number + 1) & 255
                blocks += 1
                self.state, self.phase = 'running', '接收数据（保留尾部填充）'
            elif not blocks or packet[0] != ((number - 1) & 255):
                raise RuntimeError('数据包序号不连续')
            else:
                self.retry()
            errors = 0
            write(bytes([ACK]))

    def execute(self, write):
        try:
            (self.send if self.direction == 'send' else self.receive)(write)
            if self.cancel.is_set():
                raise Cancelled(self.error or '已取消')
            self.state, self.phase = 'succeeded', '传输完成'
        except Exception as exc:
            self.error = str(exc)
            self.state = 'cancelled' if isinstance(exc, Cancelled) and not self.error.startswith('串口断开') else 'failed'
            self.phase = '传输已取消' if self.state == 'cancelled' else '传输失败'
            try:
                write(bytes([CAN, CAN]), abort=True)
            except Exception:
                pass

    def dispose(self):
        self.cancel.set()
        self.closed = True
        # 文件由调用方在后台线程结束后关闭，不能与读写并发关闭。
        if self.thread is None or not self.thread.is_alive():
            if self.active:
                self.state = 'failed' if self.error else 'cancelled'
                self.phase = '传输失败' if self.error else '传输已取消'
            self.file.close()
