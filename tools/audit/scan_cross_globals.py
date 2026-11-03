# 交叉扫描：13 个经典脚本按全局名消费的符号，是否都在 ESM 模块的导出集合里。
# 直接运行即可（路径相对本工具所在仓库根）。用于核对 app.js 末尾的过渡桥接层有没有漏项。
import os
import re
import esprima
from scan_implicit_globals import (Scope, walk_lets, visit,
                                  collect_top_level, preprocess)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
R = os.path.join(ROOT, 'renderer')
MODULES = ['core.js', 'net.js', 'notify.js', 'relay.js', 'terminal.js']
CONSUMERS = [
    'tool-floating-window', 'remote-tool-context', 'command-column-editor',
    'extension-tool-selection', 'terminal-window', 'tool-extra-windows',
    'powershell-terminal', 'serial-shortcuts', 'serial-xmodem',
    'serial-controls-window', 'serial-logging', 'serial-terminal', 'plugins',
]


def provider_names():
    """所有 ESM 模块导出的名字（app.js 靠 import 把它们带进桥接层）。"""
    names = set()
    for fname in MODULES:
        with open(os.path.join(R, fname), encoding='utf-8') as f:
            names |= set(re.findall(
                r'^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)',
                f.read(), re.M))
    return names


def consumer_free_names(path):
    """返回该脚本里所有「本文件从未声明」的标识符引用集合。"""
    free = set()
    orig_check = visit.__globals__['check_read']

    def check_read(node, scope):
        name = node.name
        if not scope.has(name):
            free.add(name)

    visit.__globals__['check_read'] = check_read
    try:
        with open(path, encoding='utf-8') as f:
            code = f.read()
        code = preprocess(code)
        tree = esprima.parseScript(code, tolerant=True)
        top = collect_top_level(tree)
        for st in tree.body:
            visit(st, top)
    finally:
        visit.__globals__['check_read'] = orig_check
    return free


def bridge_names():
    """app.js 末尾 Object.assign(window, {...}) 里的简写属性名。"""
    with open(os.path.join(R, 'app.js'), encoding='utf-8') as f:
        app = f.read()
    m = re.search(r'Object\.assign\(window,\s*\{(.*?)\}\);', app, re.S)
    assert m, '找不到桥接层 Object.assign(window, {...})'
    body = re.sub(r'//[^\n]*', '', m.group(1))
    return set(re.findall(r'[A-Za-z_$][\w$]*', body))


def main():
    os.chdir(ROOT)
    provider = provider_names()
    bridged = bridge_names()
    from scan_implicit_globals import BUILTINS
    needed_all = set()
    for name in CONSUMERS:
        path = os.path.join('renderer', f'{name}.js')
        try:
            free = consumer_free_names(path)
        except Exception as exc:
            print(f'{name}.js: 解析失败 {type(exc).__name__}: {exc}')
            continue
        needed = sorted(n for n in free if n in provider and n not in BUILTINS)
        needed_all.update(needed)
        if needed:
            print(f'{name}.js: ' + ', '.join(needed))
    # ws 用访问器桥接（可变绑定），不在 Object.assign 里。
    missing = sorted(needed_all - bridged - {'ws'})
    if missing:
        print('桥接层漏项: ' + ', '.join(missing))
        raise SystemExit(1)
    print(f'桥接层完整：{len(needed_all)} 个跨文件符号全部已导出')


if __name__ == '__main__':
    main()
