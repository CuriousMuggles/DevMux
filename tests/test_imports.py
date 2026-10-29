"""验证所有核心模块可以正常导入。"""
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULES = [
    'server',
    'adb_worker',
    'diagnostics',
]

def run() -> bool:
    ok = True
    for mod in MODULES:
        try:
            # 独立进程执行冷导入，避免重载模块使其他模块保留失效引用。
            result = subprocess.run(
                [sys.executable, '-B', '-c',
                 'import importlib,sys; importlib.import_module(sys.argv[1])', mod],
                cwd=ROOT, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
            print(f'  [PASS] {mod}')
        except Exception as e:
            print(f'  [FAIL] {mod}: {e}')
            ok = False
    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
