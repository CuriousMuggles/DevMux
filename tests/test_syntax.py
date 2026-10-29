"""验证所有 Python 源文件语法正确。

源文件清单**自动发现**根目录下的所有 ``*.py``，不再手工维护：旧的硬编码清单
里 ``conpty.py`` 早已删除却一直写着（靠 [SKIP] 掩盖），而新增的 ``relay.py``、
``relay_carrier.py`` 也没被补上，等于新模块完全没有语法校验。
"""
import sys
import ast
from pathlib import Path

# 确实不需要语法校验的例外（例如平台专用脚本）。当前为空。
SKIP_FILES: set = set()


def _source_files(root: Path) -> list:
    return [path for path in sorted(root.glob('*.py')) if path.name not in SKIP_FILES]


def run() -> bool:
    root = Path(__file__).resolve().parent.parent
    files = _source_files(root)
    if not files:
        print('  [FAIL] 未发现任何源文件')
        return False

    ok = True
    for fpath in files:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                ast.parse(f.read())
            print(f'  [PASS] {fpath.name}')
        except SyntaxError as e:
            print(f'  [FAIL] {fpath.name}: {e}')
            ok = False
    print(f'  [INFO] 共校验 {len(files)} 个源文件')
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
