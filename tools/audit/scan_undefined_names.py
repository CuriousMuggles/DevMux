# 未定义全局名扫描：等价 ruff F821 / pyflakes 的 NameError 体检。
# 用途：模块被拆分、代码搬进新文件时漏带 import，导入期不会报错，只在调用到
# 那一行抛 NameError —— 典型症状是"点了按钮没反应"（异常被 UI 的 guard 吞掉）。
# 直接运行即可（路径相对本工具所在仓库根）：py tools/audit/scan_undefined_names.py
import builtins
import sys
import symtable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {'dist', 'build', 'node_modules', '.git', '.workbuddy',
             '__pycache__', '.venv', 'venv', '.idea'}
# 编译器为 `from __future__ import annotations` 生成的隐式符号，不是真实引用。
IGNORED = {'__conditional_annotations__'}
BUILTINS = set(dir(builtins)) | IGNORED | {
    '__file__', '__name__', '__doc__', '__package__', '__spec__',
    '__loader__', '__builtins__', '__annotations__', '__path__',
}


def module_bindings(table: symtable.SymbolTable) -> set:
    """模块作用域里被绑定（赋值 / 导入 / def / class / for / with …）的名字。"""
    bound = set()
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            bound.add(sym.get_name())
    return bound


def scan_table(table, bound, out, scope=''):
    for sym in table.get_symbols():
        name = sym.get_name()
        if sym.is_global() and sym.is_referenced() and name not in bound and name not in BUILTINS:
            out.append((scope or table.get_name(), name))
    child_scope = table.get_name() if table.get_name() != 'top' else scope
    for child in table.get_children():
        scan_table(child, bound, out, child_scope)


def scan_source(source: str, filename: str='<string>') -> list:
    """返回 [(作用域名, 未定义名字)]，同一作用域内同名去重。"""
    top = symtable.symtable(source, filename, 'exec')
    found = []
    scan_table(top, module_bindings(top), found)
    seen, result = set(), []
    for scope, name in found:
        if (scope, name) not in seen:
            seen.add((scope, name))
            result.append((scope, name))
    return result


def scan_file(path: Path) -> list:
    return scan_source(path.read_text(encoding='utf-8'), str(path))


def source_files(root: Path) -> list:
    files = []
    for path in root.rglob('*.py'):
        if not (set(path.relative_to(root).parts) & SKIP_DIRS):
            files.append(path)
    return sorted(files)


def main() -> int:
    total, checked = 0, 0
    for path in source_files(ROOT):
        checked += 1
        try:
            problems = scan_file(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f'[SKIP] {path.relative_to(ROOT)}: {exc}')
            continue
        for scope, name in problems:
            print(f"{path.relative_to(ROOT)}: {scope}() 引用未定义名 {name!r}")
            total += 1
    print(f'检查 {checked} 个文件，未定义名 {total} 处')
    return 1 if total else 0


if __name__ == '__main__':
    raise SystemExit(main())
