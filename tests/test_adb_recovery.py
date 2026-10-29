"""验证 ADB 设备探测失败时会限频恢复本机服务。"""
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run() -> bool:
    import adb_worker as worker

    old_run = worker._run
    old_last_recovery = worker._last_adb_recovery
    calls = []

    def fake_run(args, timeout=5):
        calls.append((args, timeout))
        # 两次设备列表命令均失败；恢复命令本身成功即可。
        return None if args in (['devices', '-l'], ['devices']) else ''

    try:
        worker._run = fake_run
        worker._last_adb_recovery = 0.0
        devices = worker.get_all_devices()
        expected = [
            (['devices', '-l'], 8),
            (['devices'], 8),
            (['kill-server'], 5),
            (['start-server'], 10),
        ]
        if devices == [] and calls == expected:
            print('  [PASS] ADB 探测失败后重建服务')
            return True
        print(f'  [FAIL] unexpected calls: {calls!r}')
        return False
    finally:
        worker._run = old_run
        worker._last_adb_recovery = old_last_recovery


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
