"""验证非 GBK 字符不会让日志调用中断 PTY 输出线程。"""
import io
import sys
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run() -> bool:
    import adb_worker

    old_stdout = sys.stdout
    old_log_file = adb_worker.LOG_FILE
    raw = io.BytesIO()
    fake_stdout = io.TextIOWrapper(raw, encoding='gbk', errors='strict')
    with tempfile.TemporaryDirectory(prefix='devmux-log-test-') as temp_dir:
        adb_worker.LOG_FILE = str(Path(temp_dir) / 'devmux.log')
        sys.stdout = fake_stdout
        try:
            adb_worker.log('TEST', '✓ obff — 3/3')
            fake_stdout.flush()
        finally:
            sys.stdout = old_stdout
            adb_worker.LOG_FILE = old_log_file

        file_text = Path(temp_dir, 'devmux.log').read_text(encoding='utf-8')
        console_text = raw.getvalue().decode('gbk')
        ok = '✓ obff — 3/3' in file_text and r'\u2713' in console_text
        print(f'  [{"PASS" if ok else "FAIL"}] GBK 控制台不阻断 Unicode 日志')
        return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
