"""验证终端记录的可选逐行时间戳。"""
import os
import re
import sys
import tempfile
import threading


TIMESTAMP = r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] '


def _make_server(server):
    instance = server.ManagerServer.__new__(server.ManagerServer)
    instance._output_lock = threading.Lock()
    instance._output_activity = {}
    instance._recording_lock = threading.Lock()
    instance._recording_sessions = {}
    instance._broadcast_async = lambda _payload: None
    return instance


def run() -> bool:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    import server

    ok = True
    original_log = server.log
    server.log = lambda *_args, **_kwargs: None
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # 分块输出时，同一行只能追加一次时间戳；每个新行均使用统一格式。
            timestamped_path = os.path.join(temp_dir, 'timestamped.log')
            instance = _make_server(server)
            instance._start_recording('device', timestamped_path, timestamps=True)
            instance._on_terminal_output('device', b'first')
            instance._on_terminal_output('device', b' line\nsecond\nthird')
            instance._stop_recording('device')
            timestamped = open(timestamped_path, encoding='utf-8').read()
            expected = re.compile(
                rf'^{TIMESTAMP}first line\n{TIMESTAMP}second\n{TIMESTAMP}third$'
            )
            if expected.match(timestamped):
                print('  [PASS] 时间戳按逻辑行写入，分块输出不重复添加')
            else:
                print(f'  [FAIL] 逐行时间戳内容异常: {timestamped!r}')
                ok = False

            # 未选择时间戳时，保留旧版原始输出行为。
            plain_path = os.path.join(temp_dir, 'plain.log')
            instance = _make_server(server)
            instance._start_recording('device', plain_path, timestamps=False)
            instance._on_terminal_output('device', b'raw\noutput')
            plain = open(plain_path, encoding='utf-8').read()
            if plain == 'raw\noutput':
                print('  [PASS] 关闭时间戳后保留原始输出')
            else:
                print(f'  [FAIL] 关闭时间戳内容异常: {plain!r}')
                ok = False

            # 选择已有文件时必须保留原内容，并从末尾继续追加。
            appended_path = os.path.join(temp_dir, 'appended.log')
            with open(appended_path, 'w', encoding='utf-8') as f:
                f.write('已有记录\n')
            instance = _make_server(server)
            result = instance._start_recording('device', appended_path, timestamps=False)
            instance._on_terminal_output('device', '新增记录'.encode('utf-8'))
            appended = open(appended_path, encoding='utf-8').read()
            if result['success'] and appended == '已有记录\n新增记录':
                print('  [PASS] 已有记录文件从末尾追加，不覆盖原内容')
            else:
                print(f'  [FAIL] 已有记录文件追加异常: {result!r}, {appended!r}')
                ok = False
    finally:
        server.log = original_log

    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
