"""验证独立发布包的 ADB 内置与运行时隐私清理配置。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _contains(path: Path, text: str) -> bool:
    return text in path.read_text(encoding='utf-8')


def run() -> bool:
    ok = True
    worker = ROOT / 'adb_worker.py'
    checks = [
        ('冻结版优先使用内置 adb', _contains(worker, "sys._MEIPASS, 'adb', 'adb.exe'")),
    ]
    checks.append(('冻结版 ADB 从持久目录运行', _contains(worker, 'return persistent_adb_path(os.path.dirname(bundled))')))
    for spec_name in ('build.spec', 'build_debug.spec'):
        spec = ROOT / spec_name
        for required in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll',
                         'devmux.log', 'adb-manager.log', 'DevMux', 'commands.json', 'crash_dump.txt', 'diagnostics'):
            checks.append((f'{spec_name} 包含 {required}', _contains(spec, required)))

    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
