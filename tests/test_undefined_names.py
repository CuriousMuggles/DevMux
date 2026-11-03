"""验证没有"引用未定义全局名"的模块 —— 等价 ruff F821，防止拆模块漏带 import。

3.1.5 的真实事故：``native_dialogs.py`` 从 ``server.py`` 摘出时漏了 ``log`` /
``subprocess`` / ``APP_TITLE`` / ``CREATE_NO_WINDOW``，``adb_service.py`` 漏了
``tempfile`` / ``re``。导入期毫无征兆，只在用户点击按钮、执行到那一行时抛
NameError；异常又被前端 guard 吞掉，表现为"点了没反应"。

本组同时自检扫描器：故意构造漏 import 的源码，必须能被检出，否则工具失效。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / 'tools' / 'audit' / 'scan_undefined_names.py'


def _load_tool():
    spec = importlib.util.spec_from_file_location('scan_undefined_names', TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run() -> bool:
    tool = _load_tool()
    ok = True

    # 1) 自检：函数体引用未导入的名字必须被抓到（否则扫描器失去意义）
    sample = 'def handler():\n    return tempfile.mkdtemp()\n'
    hits = tool.scan_source(sample, 'sample.py')
    if (('handler', 'tempfile') in hits):
        print('  [PASS] 扫描器能检出漏 import')
    else:
        print(f'  [FAIL] 扫描器自检失败，未检出 sample.py 的 tempfile：{hits}')
        ok = False

    # 2) 合法写法不得误报：导入、模块级定义、内建、函数局部与内建类型
    clean = (
        'import tempfile\n'
        'APP_TITLE = "x"\n'
        'def handle(items, data):\n'
        '    seen = set()\n'
        '    for item in items:\n'
        '        seen.add(str(item))\n'
        '    return tempfile.mkdtemp(prefix=APP_TITLE) or len(seen) or data\n'
    )
    hits = tool.scan_source(clean, 'clean.py')
    if not hits:
        print('  [PASS] 合法代码无误报')
    else:
        print(f'  [FAIL] 合法代码被误报：{hits}')
        ok = False

    # 3) 全仓扫描必须为零
    failures = 0
    files = tool.source_files(ROOT)
    for path in files:
        try:
            problems = tool.scan_file(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f'  [SKIP] {path.relative_to(ROOT)}: {exc}')
            continue
        for scope, name in problems:
            print(f'  [FAIL] {path.relative_to(ROOT)}: {scope}() 引用未定义名 {name!r}')
            failures += 1
    if failures:
        ok = False
    else:
        print(f'  [PASS] {len(files)} 个文件均无未定义全局名')
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
