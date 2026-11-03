"""验证所有 Python 源文件语法正确。

源文件清单**自动发现**，不再手工维护：旧的硬编码清单里 ``conpty.py`` 早已删除
却一直写着（靠 [SKIP] 掩盖），而新增模块也没被补上，等于新模块完全没有语法校验。
递归扫根目录、``relay/`` 包与 ``tools/``（含 tools 下的子目录）。
"""
import sys
import ast
from pathlib import Path

# 确实不需要语法校验的例外（例如平台专用脚本）。当前为空。
SKIP_FILES: set = set()


def _source_files(root: Path) -> list:
    """根目录、relay 包与 tools/ 下的脚本都要检查：包内模块与开发脚本同样是源码。"""
    files = list(root.glob('*.py'))
    for package in ('relay', 'tools'):
        files += list((root / package).rglob('*.py'))
    return [path for path in sorted(files) if path.name not in SKIP_FILES]


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
