"""远程文件传输：真实队列往返，模拟 ADB 文件系统。"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import zipfile
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay_transfer as transfer
import relay_node
import server
from tests.test_relay_app import _Harness, _fake_list_devices, _fake_execute, _wait_until


def run(cross_deployment=False):
    try:
        with tempfile.TemporaryDirectory(prefix='relay-transfer-test-') as tmp:
            base = Path(tmp)
            src = base / '本机文件'
            src.mkdir()
            script = src / '空 格.sh'
            script.write_bytes(b'echo hello\n')
            folder = src / '目录'
            folder.mkdir()
            (folder / 'binary.bin').write_bytes(bytes(range(256)))
            (folder / 'empty').mkdir()
            device = base / 'fake-device'
            device.mkdir()
            calls = []

            def adb(argv, **kwargs):
                assert kwargs['creationflags'] == relay_node.CREATE_NO_WINDOW
                assert argv[:3] == ['node-b', '-t', '1'], argv
                calls.append(argv[3:])
                args = argv[3:]
                if args[0] == 'push':
                    for p in args[1:-1]:
                        p = Path(p)
                        target = device / p.name
                        if p.is_dir():
                            shutil.copytree(p, target, dirs_exist_ok=True)
                        else:
                            shutil.copy2(p, target)
                elif args[0] == 'pull':
                    p = device / args[1].split('/')[-1]
                    if not p.exists():
                        # 兼容下载逐文件传入设备绝对路径。
                        p = device.joinpath(*Path(args[1]).parts[2:])
                    target = Path(args[2]) / p.name if Path(args[2]).is_dir() else Path(args[2])
                    if p.is_dir():
                        if os.name == 'nt' and any(ord(c)>127 for c in p.name):
                            return SimpleNamespace(returncode=1,stdout='',stderr='adb: Illegal byte sequence')
                        shutil.copytree(p, target)
                    else:
                        shutil.copy2(p, target)
                elif args[0] == 'shell' and kwargs.get('text') is False:
                    import shlex
                    tokens=shlex.split(args[1]);remote=tokens[tokens.index('find')+1]
                    source=device/remote.split('/')[-1]
                    directories=[remote]+[remote+'/'+p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_dir()]
                    files=[remote+'/'+p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()]
                    raw=b'D\0'+b'\0'.join(p.encode() for p in directories)+b'\0F\0'+b'\0'.join(p.encode() for p in files)+b'\0L\0X\0E\0'
                    return SimpleNamespace(returncode=0,stdout=raw,stderr=b'')
                return SimpleNamespace(returncode=0, stdout='ok', stderr='')

            with patch.object(relay_node, 'list_devices', _fake_list_devices), patch.object(relay_node, 'execute', _fake_execute):
                harness = _Harness(str(base / 'instances'), master_local_devices_enabled=False)
                if cross_deployment:
                    raise ValueError('不同有效根之间不允许传输')
                harness.start()
                try:
                    harness.confirm_both()
                    assert _wait_until(lambda: bool(harness.master.devices()), 8)
                    key = harness.master.devices()[0]['key']
                    # 使用真正的服务端统一入口；主控不可用本地 ADB 回退。
                    manager = object.__new__(server.ManagerServer)
                    manager.relay = harness.master
                    manager._executor = None
                    with patch.object(relay_node.subprocess, 'run', adb):
                        rejected = asyncio.run(manager._exec_adb(argv=['push', str(script), '/sdcard/'], key=key))
                        assert not rejected['success'] and '白名单' in rejected['output'], rejected
                        assert not calls
                        harness.slave.node.allowed_subcommands = ('push', 'pull', 'shell')
                        uploaded = asyncio.run(manager._exec_adb(argv=['-t', '999', 'push', str(script), str(folder), '/sdcard/'], make_executable=True, key=key))
                        assert uploaded['success'], uploaded
                        assert (device / script.name).read_bytes() == script.read_bytes()
                        assert (device / folder.name / 'binary.bin').read_bytes() == bytes(range(256))
                        assert any(c[0] == 'shell' and c[1].startswith('chmod +x') for c in calls)
                        assert ['shell', 'sync'] in calls
                        # 发布完成时旧心跳观测暂时过期，等待新进展后只发送一次上传。
                        stale_until = [0.0]
                        original_publish = transfer.publish_upload
                        original_offline = harness.master.node._peer_offline_reason
                        def published_with_stale_presence(paths, package):
                            info = original_publish(paths, package)
                            stale_until[0] = time.monotonic() + .25
                            return info
                        def transient_offline(peer):
                            return '模拟旧心跳观测' if time.monotonic() < stale_until[0] else original_offline(peer)
                        push_count = sum(call[0] == 'push' for call in calls)
                        with patch.object(transfer, 'publish_upload', published_with_stale_presence), \
                             patch.object(harness.master.node, '_peer_offline_reason', transient_offline):
                            recovered_upload = harness.master.remote_transfer(key, ['push', str(script), '/sdcard/'])
                        assert recovered_upload['success'], recovered_upload
                        assert sum(call[0] == 'push' for call in calls) == push_count + 1, '恢复后上传重复执行'
                        # 真正没有新心跳时不能通过等候伪造在线或下发文件请求。
                        push_count = sum(call[0] == 'push' for call in calls)
                        with patch.object(harness.master.node, '_peer_offline_reason', return_value='模拟心跳冻结'):
                            offline_upload = harness.master.remote_transfer(key, ['push', str(script), '/sdcard/'])
                        assert not offline_upload['success'] and '离线' in offline_upload['output'], offline_upload
                        assert sum(call[0] == 'push' for call in calls) == push_count
                        out = base / '下载到本机'
                        out.mkdir()
                        downloaded = asyncio.run(manager._exec_adb(argv=['pull', '/sdcard/' + script.name, '/sdcard/' + folder.name, str(out)], key=key))
                        assert downloaded['success'], downloaded
                        assert (out / script.name).read_bytes() == script.read_bytes()
                        assert (out / folder.name / 'binary.bin').read_bytes() == bytes(range(256))
                        assert (out / folder.name / 'empty').is_dir()
                        assert _wait_until(lambda: not list(Path(harness.slave.node.root).rglob('*.zip')), 4)
                        if not cross_deployment:
                            # 真实双节点持续传输超过3秒，心跳必须继续推进，且共享包只读一遍。
                            large = device / 'large.bin'
                            large.write_bytes(bytes(range(256)) * (16 * 1024))
                            shared_reads = []
                            heartbeats = []
                            class SlowSharedReader:
                                def __init__(self, stream):
                                    self.stream = stream
                                def __enter__(self):
                                    return self
                                def __exit__(self, *args):
                                    return self.stream.__exit__(*args)
                                def read(self, size):
                                    chunk = self.stream.read(size)
                                    if chunk:
                                        time.sleep(.9)
                                        view = harness.master.node.connection_info('pc-b')
                                        heartbeats.append((view['heartbeatSeq'], view['connectionState']))
                                    return chunk
                            def slow_shared_open(path, mode='r', *args, **kwargs):
                                stream = builtins.open(path, mode, *args, **kwargs)
                                if mode == 'rb' and str(path).endswith('download.zip'):
                                    shared_reads.append(str(path))
                                    return SlowSharedReader(stream)
                                return stream
                            with patch.object(transfer, 'open', slow_shared_open, create=True):
                                r = harness.master.remote_transfer(key, ['pull', '/sdcard/large.bin', str(out)])
                            assert r['success'], r
                            assert (out / 'large.bin').read_bytes() == large.read_bytes()
                            assert len(shared_reads) == 1, '下载对共享目录重复校验或解压'
                            assert len(heartbeats) == 5 and len({s for s, _ in heartbeats}) >= 3, heartbeats
                            assert all(state == 'connected' for _, state in heartbeats), heartbeats
                            print('  [PASS] 慢大文件下载只读取共享包一次，超过3秒仍持续观察心跳并保持连接')
                        # 单文件重命名保存。
                        renamed = base / 'renamed.sh'
                        r = harness.master.remote_transfer(key, ['pull', '/sdcard/' + script.name, str(renamed)])
                        assert r['success'] and renamed.read_bytes() == script.read_bytes(), r
                        harness.master.node.unpair_peer(harness.slave.config.nodeId)
                        calls.clear()
                        r = harness.master.remote_transfer(key, ['push', str(script), '/sdcard/'])
                        assert not r['success'] and not calls
                finally:
                    harness.stop()
            package = base / 'tampered.zip'
            local_published = base / 'shared-sim' / 'upload.zip'
            with patch.object(transfer, 'digest', wraps=transfer.digest) as hash_calls:
                transfer.publish_upload([script], local_published)
                assert hash_calls.call_count == 1
                assert Path(hash_calls.call_args.args[0]) != local_published, '校验仍重读共享目录的包'
            with patch.object(transfer, 'digest', wraps=transfer.digest) as hash_calls:
                paths = transfer.receive_package(local_published, base / 'received', {
                    'size': local_published.stat().st_size, 'sha256': transfer.digest(local_published)})
                assert paths[0].read_bytes() == script.read_bytes()
                assert Path(hash_calls.call_args.args[0]) != local_published, '接收校验仍读取共享包'
            info = transfer.pack([script], package)
            package.write_bytes(package.read_bytes() + b'x')
            try:
                transfer.unpack(package, base / 'invalid', info)
                raise AssertionError('损坏包未拒绝')
            except ValueError:
                pass
            with zipfile.ZipFile(package, 'w') as z:
                z.writestr('0/../../escaped.txt', b'bad')
            info = {'sha256': transfer.digest(package), 'size': package.stat().st_size}
            try:
                transfer.unpack(package, base / 'invalid', info)
                raise AssertionError('路径穿越未拒绝')
            except ValueError:
                pass
            assert not (base / 'escaped.txt').exists()
            print('  [PASS] 远程上传、目录、多文件、下载、权限、清理及恶意包校验')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
