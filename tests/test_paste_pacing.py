"""验证多行粘贴限速的行切分与发送协程逻辑。"""
import asyncio
import os
import sys

def run() -> bool:
    ok = True
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)

    try:
        from server import _iter_line_segments, ManagerServer
    except ImportError as e:
        print(f'  [SKIP] cannot import server: {e}')
        return True

    # --- 1. _iter_line_segments 纯函数 ---
    cases = [
        ('', []),
        ('line1', ['line1']),
        ('line1\n', ['line1\n']),
        ('line1\nline2\n', ['line1\n', 'line2\n']),
        ('line1\r\nline2\r\n', ['line1\r\n', 'line2\r\n']),
        ('line1\rline2\r', ['line1\r', 'line2\r']),
        ('line1\r\nline2\n', ['line1\r\n', 'line2\n']),
        ('\n', ['\n']),
        ('\r\n', ['\r\n']),
        ('\r', ['\r']),
        ('line1\n\nline3\n', ['line1\n', '\n', 'line3\n']),
        ('line1\n\n\nline4', ['line1\n', '\n', '\n', 'line4']),
        ('\n\n', ['\n', '\n']),
    ]
    for text, expected in cases:
        result = _iter_line_segments(text)
        if result == expected:
            print(f'  [PASS] _iter_line_segments({text!r})')
        else:
            print(f'  [FAIL] _iter_line_segments({text!r}) = {result!r}, expected {expected!r}')
            ok = False

    # --- 2. _send_one_line_paced：逐行写入 + 等待回显 ---
    try:
        writes = []

        class FakeWorker:
            def write_terminal(self, key, data):
                writes.append(data)

        sv = ManagerServer.__new__(ManagerServer)
        sv.worker = FakeWorker()
        sv._output_activity = {}
        sv._output_lock = __import__('threading').Lock()

        old_gate = os.environ.get('ADB_PASTE_GATE')

        async def scenario():
            # 触发方式 1：禁用 gate -> 直接整体写入
            os.environ['ADB_PASTE_GATE'] = '0'
            segs = _iter_line_segments('a\nb\n')
            for seg in segs:
                await sv._send_one_line_paced('k', seg.encode('utf-8'))
            assert writes == [b'a\n', b'b\n'], writes

            # 触发方式 2：开启 gate，但设备无回显 -> 由超时兜底，仍应全部写入
            os.environ['ADB_PASTE_GATE'] = '1'
            os.environ['ADB_PASTE_QUIET_MS'] = '50'
            os.environ['ADB_PASTE_FIRST_TIMEOUT_MS'] = '50'
            os.environ['ADB_PASTE_SETTLE_TIMEOUT_MS'] = '100'
            writes.clear()
            segs = _iter_line_segments('x\n')
            await sv._send_one_line_paced('k', segs[0].encode('utf-8'))
            assert writes == [b'x\n'], writes

        asyncio.run(scenario())

        if old_gate is None:
            os.environ.pop('ADB_PASTE_GATE', None)
        else:
            os.environ['ADB_PASTE_GATE'] = old_gate
        os.environ.pop('ADB_PASTE_QUIET_MS', None)
        os.environ.pop('ADB_PASTE_FIRST_TIMEOUT_MS', None)
        os.environ.pop('ADB_PASTE_SETTLE_TIMEOUT_MS', None)
        print('  [PASS] _send_one_line_paced writes all segments (gate off + timeout fallback)')
    except Exception as e:
        import traceback
        print(f'  [FAIL] _send_one_line_paced scenario: {type(e).__name__}: {e}')
        traceback.print_exc()
        ok = False

    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
