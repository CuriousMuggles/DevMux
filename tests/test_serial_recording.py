"""串口日志实际临时文件回归，不访问实体COM。"""
import base64
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from serial_recording import SerialRecordings
from server import strip_ansi, add_recording_timestamps


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'capture.log'
        self.rec = SerialRecordings(strip_ansi, add_recording_timestamps)
        self.owner = object()
        self.identity = (False, 'node-a', 'session-a')

    def feed(self, data, end, **extra):
        return self.rec.capture(self.owner, self.identity,
            dict(data=base64.b64encode(data).decode(), nextCursor=end, state='running', **extra))

    def start(self, **kw):
        return self.rec.start(self.owner, self.identity, str(self.path), **kw)

    def test_append_split_encoding_dedup_and_owner(self):
        self.path.write_text('已有\n', encoding='utf-8')
        self.start(timestamps=False)
        raw = '中文\n'.encode()
        self.feed(raw[:2], 2)
        self.feed(raw[:2], 2)
        self.rec.capture(object(), self.identity, dict(data=base64.b64encode(b'BAD').decode(), nextCursor=3))
        self.feed(raw, len(raw))
        self.rec.stop(self.owner, self.identity)
        self.assertEqual(self.path.read_text('utf-8'), '已有\n中文\n')

    def test_timestamps_ansi_and_stop(self):
        self.start()
        self.feed(b'\x1b[31mfirst', 10)
        self.feed(b' line\nsecond\n', 23)
        self.rec.stop(self.owner, self.identity)
        self.feed(b'NO', 25)
        self.assertRegex(self.path.read_text('utf-8'), r'^\[.+?\] first line\n\[.+?\] second\n$')

    def test_ansi_and_osc_split_across_reads(self):
        self.start(timestamps=False)
        chunks = [b'one\x1b[', b'31mred\x1b[0m\n\x1b]0;hidden', b' title\x1b', b'\\done\n']
        end = 0
        for raw in chunks:
            end += len(raw)
            self.feed(raw, end)
        self.rec.stop(self.owner, self.identity)
        self.assertEqual(self.path.read_text('utf-8'), 'onered\ndone\n')

    def test_hex_and_cursor_gap(self):
        self.start(mode='hex', timestamps=False)
        self.feed(bytes([0, 255]), 2)
        self.feed(b'A', 8)
        self.assertIn('00 FF\n', self.path.read_text('utf-8'))
        self.assertIn('裁剪 5 字节', self.path.read_text('utf-8'))
        self.assertTrue(self.path.read_text('utf-8').endswith('41\n'))

    def test_error_removes_record_without_throwing(self):
        self.start(timestamps=False)
        self.path.unlink(); self.path.mkdir()
        self.assertIn('写入失败', self.feed(b'abc', 3))
        self.assertFalse(self.rec._records)

    def test_close_owner_preserves_other_sessions(self):
        self.start()
        other = object()
        self.rec.start(other, self.identity, str(Path(self.temp.name)/'other.log'))
        self.rec.close_owner(self.owner)
        self.assertEqual(len(self.rec._records), 1)
        self.rec.close_owner()
        self.assertFalse(self.rec._records)

    def test_encoding_boundaries_and_reject_invalid(self):
        self.start(encoding='gbk', timestamps=False)
        raw = '测试'.encode('gbk')
        self.feed(raw[:1], 1); self.feed(raw[1:], 4)
        self.rec.stop(self.owner, self.identity)
        self.assertEqual(self.path.read_text('utf-8'), '测试')
        with self.assertRaises(ValueError): self.start(encoding='unsupported')
        with self.assertRaises(ValueError): self.start(cursor=-1)
        with self.assertRaises(ValueError): self.start(mode='binary')


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecordingTests)).wasSuccessful()

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
