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

    # db_download_host 有两份：根目录版打进冻结包，插件目录版供 db-flash 运行时调用。
    # 两份必须逐字节一致，否则同一次下载在两个入口会走出不同行为。
    bundled = ROOT / 'db_download_host.py'
    plugin_copy = ROOT / 'plugins' / 'db-flash' / 'db_download_host.py'
    checks.append(('db_download_host 两份副本均存在',
                   bundled.is_file() and plugin_copy.is_file()))
    checks.append(('db_download_host 两份副本内容一致',
                   bundled.is_file() and plugin_copy.is_file()
                   and bundled.read_bytes() == plugin_copy.read_bytes()))

    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
