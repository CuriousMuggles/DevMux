"""GUI串口接收日志：按页面、接入和字节游标隔离，不增加COM读者。"""
import base64
import codecs
from pathlib import Path
import threading


class SerialRecordings:
    def __init__(self, strip_ansi, timestamps):
        self._strip = strip_ansi
        self._timestamps = timestamps
        self._records = {}
        self._lock = threading.RLock()

    def start(self, owner, identity, path, cursor=0, mode='text', encoding='utf-8', timestamps=True):
        if mode not in ('text', 'hex') or encoding not in ('utf-8', 'gbk', 'ascii', 'latin-1'):
            raise ValueError('日志格式或编码无效')
        if type(cursor) is not int or cursor < 0 or not isinstance(path, str) or not path.strip():
            raise ValueError('日志路径或游标无效')
        with self._lock:
            if (owner, identity) in self._records:
                raise ValueError('当前串口已在记录，请先停止')
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('ab+') as stream:
                stream.seek(0, 2)
                at_start = stream.tell() == 0
                if not at_start:
                    stream.seek(-1, 2)
                    at_start = stream.read(1) in (b'\n', b'\r')
            self._records[owner, identity] = dict(path=target, cursor=cursor, mode=mode,
                decoder=codecs.getincrementaldecoder(encoding)(errors='replace'),
                timestamps=bool(timestamps), at_start=at_start, ansi='text')
        return {'success': True, 'output': '串口日志已开始，将从文件末尾追加'}

    def _write(self, record, text):
        # 控制序列可能横跨串口读取边界；只保存状态，不缓存无界转义正文。
        plain = []
        state = record['ansi']
        for char in text:
            if state == 'text':
                if char == '\x1b':
                    state = 'escape'
                else:
                    plain.append(char)
            elif state == 'escape':
                state = {'[': 'csi', ']': 'osc', 'P': 'string', '^': 'string', '_': 'string'}.get(char, 'text')
            elif state == 'csi':
                if '@' <= char <= '~':
                    state = 'text'
            elif state in ('osc', 'string'):
                if char == '\a':
                    state = 'text'
                elif char == '\x1b':
                    state = 'string-escape'
            elif state == 'string-escape':
                state = 'text' if char == '\\' else 'string'
        record['ansi'] = state
        text = self._strip(''.join(plain))
        if not text:
            return
        if record['timestamps']:
            text, record['at_start'] = self._timestamps(text, record['at_start'])
        with record['path'].open('a', encoding='utf-8', newline='') as stream:
            stream.write(text)

    def capture(self, owner, identity, data):
        with self._lock:
            record = self._records.get((owner, identity))
            if not record:
                return ''
            try:
                raw = base64.b64decode(data.get('data', ''), validate=True)
                end = data.get('nextCursor', record['cursor'])
                if type(end) is not int or end < len(raw):
                    raise ValueError('日志接收游标无效')
                start = end - len(raw)
                if end <= record['cursor']:
                    return ''
                if start > record['cursor']:
                    self._write(record, record['decoder'].decode(b'', final=True))
                    record['decoder'].reset()
                    record['ansi'] = 'text'
                    self._write(record, f"\n[日志输出已裁剪 {start-record['cursor']} 字节]\n")
                raw = raw[max(0, record['cursor'] - start):]
                if record['mode'] == 'hex':
                    text = ''.join(' '.join(f'{b:02X}' for b in raw[i:i+16]) + '\n'
                        for i in range(0, len(raw), 16))
                else:
                    text = record['decoder'].decode(raw)
                self._write(record, text)
                record['cursor'] = end
                if data.get('closed') or data.get('state') not in (None, 'running'):
                    return self.stop(owner, identity)
                return ''
            except Exception as exc:
                self._records.pop((owner, identity), None)
                return f'串口日志写入失败，已停止记录：{exc}'

    def stop(self, owner, identity):
        with self._lock:
            record = self._records.pop((owner, identity), None)
            if record:
                try:
                    if record['mode'] == 'text':
                        self._write(record, record['decoder'].decode(b'', final=True))
                except Exception as exc:
                    return f'串口日志结束写入失败：{exc}'
        return ''

    def close_owner(self, owner=None):
        with self._lock:
            for current, identity in list(self._records):
                if owner is None or current is owner:
                    self.stop(current, identity)
