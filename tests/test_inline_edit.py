"""验证应用内远程文本编辑的 ADB 读写协议（不连接真实设备）。"""
import asyncio
import base64
import os
import sys
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run() -> bool:
    import server as srv

    calls = []
    old_run = srv.subprocess.run

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if 'exec-out' in args:
            return SimpleNamespace(returncode=0, stdout='原始内容\n'.encode('utf-8'), stderr=b'')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    srv.subprocess.run = fake_run
    try:
        manager = srv.ManagerServer()

        started = asyncio.run(manager._start_inline_edit(
            'SERIAL:tid=7', 'SERIAL', '7', '/data/config.txt'
        ))
        ok = started.get('success') is True
        ok = ok and base64.b64decode(started.get('content', '')).decode('utf-8') == '原始内容\n'
        ok = ok and calls[0][0][:5] == [srv.ADB_PATH, '-t', '7', 'exec-out', 'cat']
        print(f'  [{"PASS" if ok else "FAIL"}] 读取远程 UTF-8 文件')

        saved = asyncio.run(manager._save_inline_edit(
            'SERIAL:tid=7', base64.b64encode('修改后内容'.encode('utf-8')).decode('ascii')
        ))
        save_call = calls[-1]
        save_args, save_kwargs = save_call
        save_ok = saved.get('success') is True
        save_ok = save_ok and save_kwargs.get('input') == '修改后内容'.encode('utf-8')
        save_ok = save_ok and save_args[-2] == 'shell'
        save_ok = save_ok and 'cat > /data/config.txt' == save_args[-1]
        ok = ok and save_ok
        print(f'  [{"PASS" if save_ok else "FAIL"}] 写回远程文件且不生成本地文件')

        transfer_args = ['-t', '7', 'push', r'C:\Users\测试 用户\file.txt', '/data/目标 文件.txt']
        transfer_start = len(calls)
        transfer = asyncio.run(manager._exec_adb('', argv=transfer_args))
        transfer_calls = [args for args, _ in calls[transfer_start:]]
        transfer_ok = (
            transfer.get('success') is True
            and transfer_calls == [
                [srv.ADB_PATH] + transfer_args,
                [srv.ADB_PATH, '-t', '7', 'shell', 'sync'],
            ]
            and '设备文件已同步' in transfer.get('output', '')
        )
        ok = ok and transfer_ok
        print(f'  [{"PASS" if transfer_ok else "FAIL"}] 文件传输后同步设备文件系统')

        script_args = ['-t', '7', 'push', r'C:\Users\测试 用户\run.sh', '/data/']
        script_start = len(calls)
        script_transfer = asyncio.run(
            manager._exec_adb('', argv=script_args, make_executable=True)
        )
        script_calls = [args for args, _ in calls[script_start:]]
        chmod_ok = (
            script_transfer.get('success') is True
            and script_calls == [
                [srv.ADB_PATH] + script_args,
                [srv.ADB_PATH, '-t', '7', 'shell', 'test', '-d', '/data/'],
                [srv.ADB_PATH, '-t', '7', 'shell', 'chmod', '+x', '/data/run.sh'],
                [srv.ADB_PATH, '-t', '7', 'shell', 'sync'],
            ]
        )
        ok = ok and chmod_ok
        print(f'  [{"PASS" if chmod_ok else "FAIL"}] 上传 sh 脚本后设权限并同步')

        multi_start = len(calls)
        multi_script_args = [
            '-t', '7', 'push', r'C:\Users\测试 用户\first.sh',
            r'C:\Users\测试 用户\second.sh', r'C:\Users\测试 用户\note.txt', '/data/scripts',
        ]
        multi_transfer = asyncio.run(
            manager._exec_adb('', argv=multi_script_args, make_executable=True)
        )
        multi_calls = [args for args, _ in calls[multi_start:]]
        multi_chmods = [args for args in multi_calls if 'chmod' in args]
        multi_ok = (
            multi_transfer.get('success') is True
            and multi_calls[0] == [srv.ADB_PATH] + multi_script_args
            and multi_chmods == [
                [srv.ADB_PATH, '-t', '7', 'shell', 'chmod', '+x', '/data/scripts/first.sh'],
                [srv.ADB_PATH, '-t', '7', 'shell', 'chmod', '+x', '/data/scripts/second.sh'],
            ]
            and multi_calls[-1] == [srv.ADB_PATH, '-t', '7', 'shell', 'sync']
        )
        ok = ok and multi_ok
        print(f'  [{"PASS" if multi_ok else "FAIL"}] 多文件上传后设权限并同步')

        def fake_sync_fail(args, **kwargs):
            if args[-2:] == ['shell', 'sync']:
                return SimpleNamespace(returncode=1, stdout='', stderr='只读文件系统')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        srv.subprocess.run = fake_sync_fail
        sync_failure = asyncio.run(manager._exec_adb('', argv=transfer_args))
        sync_failure_ok = (
            sync_failure.get('success') is False
            and '上传成功，但设备文件同步失败：只读文件系统' == sync_failure.get('output')
        )
        ok = ok and sync_failure_ok
        print(f'  [{"PASS" if sync_failure_ok else "FAIL"}] 同步失败不误报上传完成')
        srv.subprocess.run = fake_run

        closed = manager._close_inline_edit('SERIAL:tid=7')
        close_ok = closed.get('success') is True and not manager._inline_sessions
        ok = ok and close_ok
        print(f'  [{"PASS" if close_ok else "FAIL"}] 关闭直接编辑会话')

        bad_path = asyncio.run(manager._start_inline_edit('k', 'SERIAL', '7', 'relative.txt'))
        path_ok = bad_path.get('success') is False and '绝对路径' in bad_path.get('output', '')
        ok = ok and path_ok
        print(f'  [{"PASS" if path_ok else "FAIL"}] 拒绝非绝对设备路径')

        def fake_binary(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b'\x00\xff\x01', stderr=b'')

        srv.subprocess.run = fake_binary
        binary = asyncio.run(manager._start_inline_edit('binary', 'SERIAL', '7', '/data/app.bin'))
        binary_ok = binary.get('success') is False and 'UTF-8' in binary.get('output', '')
        ok = ok and binary_ok
        print(f'  [{"PASS" if binary_ok else "FAIL"}] 拒绝二进制文件直接编辑')
        return ok
    finally:
        srv.subprocess.run = old_run


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
