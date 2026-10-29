"""共享文件缓存回归：对齐读取、偏移、重建及陈旧缓存旁路。"""
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_file_io


def run():
    ok = True
    def check(name, value):
        nonlocal ok
        print(('  [PASS] ' if value else '  [FAIL] ') + name)
        ok = ok and bool(value)
    with tempfile.TemporaryDirectory() as root:
        p = Path(root) / 'data.bin'
        data = bytes(range(256)) * 700
        p.write_bytes(data)
        check('本地路径保持普通读取', not relay_file_io.is_remote(str(p))
              and relay_file_io.read_bytes(str(p), 13, 87) == data[13:100])
        if os.name == 'nt':
            for start, length in [(0, None), (13, 87), (65530, 40),
                                  (70000, 80000), (len(data), 3), (0, 0)]:
                expected = data[start:] if length is None else data[start:start + length]
                check(f'原生对齐读取 offset={start} limit={length}',
                      relay_file_io._read_uncached(str(p), start, length) == expected)
            p.write_bytes(b'{"peerId":"fresh"}')
            with patch.object(relay_file_io, 'is_remote', return_value=True), \
                 patch('builtins.open', side_effect=AssertionError('不能回退到旧缓存')):
                check('网络 JSON 不调用返回旧缓存的普通 open',
                      relay.read_json(str(p)) == {'peerId': 'fresh'})
                p.unlink()
                check('网络文件消失时不采信旧缓存', relay.read_json(str(p)) is None)
            p.write_bytes(data)
            with patch.object(relay_file_io, 'is_remote', return_value=True):
                tail = relay.StreamTail(str(p), 65530)
                check('网络流跨对齐边界且只推进实际字节', tail.read_new(40) == data[65530:65570]
                      and tail.offset == 65570)
                p.write_bytes(b'new')
                check('网络流重建后归零', tail.read_new() == b'new' and tail.rebuilt)
            peers_dir = Path(root) / 'peers'
            peers_dir.mkdir()
            old = relay.PeerHeartbeat(peerId='slave', role='slave', pid=111, devices=[{'key': 'a'}])
            relay.write_heartbeat(str(peers_dir), old)
            cached = old.to_dict()
            fresh = relay.PeerHeartbeat(peerId='slave', role='slave', pid=111, devices=[{'key': 'a'}, {'key': 'b'}])
            native_read = relay.read_json
            def stale_legacy(path):
                return cached if str(path).endswith('slave.json') else native_read(path)
            with patch.object(relay, 'read_json', side_effect=stale_legacy):
                relay.write_heartbeat(str(peers_dir), fresh, snapshot=True)
                check('稳定路径返回旧缓存时仍通过唯一快照发现第二台设备',
                      len(relay.read_peers(str(peers_dir))['slave'].devices) == 2)
                for number in range(4):
                    fresh.devices = [{'key': str(number)}]
                    relay.write_heartbeat(str(peers_dir), fresh, snapshot=True)
                check('连续发布不重复文件名且只保留本进程三份快照',
                      len(list(peers_dir.glob('*.beat'))) == 3
                      and relay.read_peers(str(peers_dir))['slave'].devices == [{'key': '3'}])
            from contextlib import contextmanager
            from types import SimpleNamespace
            native_scan = os.scandir
            @contextmanager
            def stale_scan(directory):
                with native_scan(directory) as entries:
                    records = list(entries)
                if str(directory) == str(peers_dir):
                    def missing_stat():
                        raise FileNotFoundError('快照已被上一轮删除')
                    records.append(SimpleNamespace(name='slave.beat-111-missing.beat',
                                                   path=str(peers_dir / 'missing.beat'), stat=missing_stat))
                yield iter(records)
            with patch.object(relay.os, 'scandir', side_effect=stale_scan):
                relay.write_heartbeat(str(peers_dir), fresh, snapshot=True)
                check('SMB目录缓存含已删除快照时心跳发布与刷新不中断',
                      relay.read_peers(str(peers_dir))['slave'].devices == fresh.devices)
            with patch.object(relay, 'read_json', wraps=relay.read_json) as reads:
                relay.read_peers(str(peers_dir))
                check('同一节点只读最新心跳，不逐一打开历史快照', reads.call_count == 1)
            @contextmanager
            def old_attributes(directory):
                with native_scan(directory) as entries:
                    records = list(entries)
                yield iter([SimpleNamespace(name=e.name, path=e.path,
                    stat=lambda e=e: SimpleNamespace(st_mtime=e.stat().st_mtime - 20)) for e in records])
            with patch.object(relay.os, 'scandir', side_effect=old_attributes), \
                 patch.object(relay_file_io, 'is_remote', return_value=True):
                native_beat = relay.read_peers(str(peers_dir))['slave']
                check('目录元数据滞后20秒时仍从原生句柄取得最新心跳',
                      abs(native_beat.fileMtimeMs - Path(relay.heartbeat_pulse_path(str(peers_dir), fresh)).stat().st_mtime * 1000) < 1)
            other = relay.PeerHeartbeat(peerId='slave', role='slave', pid=222, devices=[{'key': 'other'}])
            relay.write_heartbeat(str(peers_dir), other, snapshot=True)
            relay.write_heartbeat(str(peers_dir), fresh, snapshot=True)
            check('同一 peerId 跨进程快照只保留最新三份（重启延续同身份）',
                  len(list(peers_dir.glob('slave.beat-*.beat'))) == 3
                  and len(list(peers_dir.glob('slave.beat-222-*.beat'))) == 1)
            pulse_dir = Path(root) / 'pulse-peers'
            pulse_dir.mkdir()
            first = relay.PeerHeartbeat(peerId='pulse-slave', role='slave', pid=333,
                nodeId='pulse-node', startedAt='2026-10-10T00:00:00+08:00',
                heartbeatAt='2026-10-10T00:00:01+08:00', devices=[{'key': 'first'}])
            relay.write_heartbeat(str(pulse_dir), first, snapshot=True)
            stable_data = first.to_dict()
            second = relay.PeerHeartbeat.from_dict(stable_data)
            second.heartbeatAt = '2026-10-10T00:00:03+08:00'
            second.devices = [{'key': 'second'}]
            relay.write_heartbeat(str(pulse_dir), second, snapshot=True, pulse_only=True)
            stable_path = pulse_dir / 'pulse-slave.json'
            pulse_path = Path(relay.heartbeat_pulse_path(str(pulse_dir), first))
            original_stat = relay_file_io.stat
            def stale_pulse_size(path):
                info = original_stat(path)
                if str(path) == str(pulse_path):
                    return SimpleNamespace(st_mtime=info.st_mtime, st_size=1)
                return info
            with patch.object(relay, 'read_json', return_value=stable_data), \
                    patch.object(relay_file_io, 'stat', side_effect=stale_pulse_size):
                actual = relay.read_peers(str(pulse_dir))['pulse-slave']
            check('稳定JSON和目录没有更新且大小属性陈旧时，追加心跳仍读取最新完整记录',
                  actual.heartbeatAt == second.heartbeatAt and actual.devices == second.devices
                  and relay.read_json(str(stable_path)) == stable_data)
            with pulse_path.open('ab') as f:
                f.write(b'{"unfinished":')
            actual = relay.read_peers(str(pulse_dir))['pulse-slave']
            check('并发追加半条记录不会丢失最近完整心跳', actual.heartbeatAt == second.heartbeatAt)
            with pulse_path.open('ab') as f:
                f.write(b'x' * (2 * 1024 * 1024))
            third = relay.PeerHeartbeat.from_dict(second.to_dict())
            third.heartbeatAt = '2026-10-10T00:00:05+08:00'
            relay.write_heartbeat(str(pulse_dir), third, snapshot=True, pulse_only=True)
            def pre_roll_size(path):
                info = original_stat(path)
                if str(path) == str(pulse_path):
                    return SimpleNamespace(st_mtime=info.st_mtime, st_size=2 * 1024 * 1024)
                return info
            with patch.object(relay_file_io, 'stat', side_effect=pre_roll_size):
                actual = relay.read_peers(str(pulse_dir))['pulse-slave']
            check('心跳回卷保持文件身份且旧大小属性不能遮住新记录',
                  pulse_path.stat().st_size < 65536 and actual.heartbeatAt == third.heartbeatAt)
            old_mtime = time.time() - 60
            os.utime(pulse_path, (old_mtime, old_mtime))
            os.utime(stable_path, (old_mtime, old_mtime))
            for path in pulse_dir.glob('*.beat'):
                os.utime(path, (old_mtime, old_mtime))
            actual = relay.read_peers(str(pulse_dir))['pulse-slave']
            check('追加心跳停止更新后仍按原阈值判离线，不因重新读取变在线', actual.is_stale(2))
            # --- pulse 生命周期清理：正常退出删本机 pulse；异常退出遗留按 pid 补收 ---
            clean = relay.PeerHeartbeat(peerId='pulse-clean', role='slave', pid=os.getpid(),
                nodeId='pulse-node', startedAt='2026-10-11T00:00:00+08:00')
            relay.write_heartbeat(str(pulse_dir), clean, snapshot=True, pulse_only=True)
            clean_path = Path(relay.heartbeat_pulse_path(str(pulse_dir), clean))
            check('drop_own_pulse 删除正常退出的本机 pulse',
                  relay.drop_own_pulse(str(pulse_dir), clean) and not clean_path.exists())
            check('drop_own_pulse 幂等：已删不再删',
                  not relay.drop_own_pulse(str(pulse_dir), clean))
            dead1 = relay.PeerHeartbeat(peerId='pulse-clean', role='slave', pid=1111,
                nodeId='pulse-node', startedAt='2026-10-11T00:00:00+08:00')
            dead2 = relay.PeerHeartbeat(peerId='pulse-clean', role='slave', pid=2222,
                nodeId='pulse-node', startedAt='2026-10-11T00:00:00+08:00')
            relay.write_heartbeat(str(pulse_dir), dead1, snapshot=True, pulse_only=True)
            relay.write_heartbeat(str(pulse_dir), dead2, snapshot=True, pulse_only=True)
            dead1_path = Path(relay.heartbeat_pulse_path(str(pulse_dir), dead1))
            dead2_path = Path(relay.heartbeat_pulse_path(str(pulse_dir), dead2))
            with patch.object(relay, '_pid_alive', return_value=False):
                removed = relay.purge_dead_pulses(str(pulse_dir), 'pulse-clean',
                                                  self_pid=os.getpid())
            check('purge_dead_pulses 回收异常退出遗留的 pulse',
                  len(removed) == 2 and not dead1_path.exists() and not dead2_path.exists())
            alive = relay.PeerHeartbeat(peerId='pulse-clean', role='slave', pid=3333,
                nodeId='pulse-node', startedAt='2026-10-11T00:00:00+08:00')
            relay.write_heartbeat(str(pulse_dir), alive, snapshot=True, pulse_only=True)
            with patch.object(relay, '_pid_alive', return_value=True):
                removed = relay.purge_dead_pulses(str(pulse_dir), 'pulse-clean',
                                                  self_pid=os.getpid())
            check('purge_dead_pulses 不删仍在运行的进程 pulse', len(removed) == 0)
        else:
            print('  [SKIP] 原生 Windows 检查')
    return ok


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
