# 一次性工具：扫描 ESM 模块里的「隐式全局」——未声明就读/写自由变量。
# 模块代码是严格模式，未声明赋值会抛 ReferenceError（经典脚本只是悄悄建全局）。
import sys
import esprima

MODULE_SCOPE = set()   # 本文件顶层声明的名字
IMPORTED = set()       # import 进来的绑定
BUILTINS = {
    'window', 'document', 'navigator', 'console', 'setTimeout', 'setInterval',
    'clearTimeout', 'clearInterval', 'requestAnimationFrame', 'WebSocket',
    'localStorage', 'sessionStorage', 'fetch', 'URL', 'URLSearchParams',
    'CustomEvent', 'KeyboardEvent', 'MouseEvent', 'Event', 'Promise', 'Map',
    'Set', 'WeakMap', 'WeakSet', 'JSON', 'Math', 'Date', 'Object', 'Array',
    'String', 'Number', 'Boolean', 'RegExp', 'Error', 'TypeError',
    'ReferenceError', 'RangeError', 'SyntaxError', 'Uint8Array', 'ArrayBuffer',
    'TextEncoder', 'TextDecoder', 'atob', 'btoa', 'alert', 'confirm', 'prompt',
    'undefined', 'NaN', 'Infinity', 'globalThis', 'Reflect', 'Proxy', 'Symbol',
    'Intl', 'Function', 'eval', 'isFinite', 'isNaN', 'parseFloat', 'parseInt',
    'AbortController', 'FileReader', 'Blob', 'File', 'FormData', 'Image',
    'Audio', 'Notification', 'structuredClone', 'queueMicrotask', 'crypto',
    'history', 'location', 'screen', 'getComputedStyle', 'matchMedia',
    # DOM 全局构造器
    'Option', 'InputEvent', 'Image', 'MutationObserver', 'ResizeObserver',
    # 经典脚本注入的页面级全局（index.html 内联脚本 / vendor 脚本）
    'WS_PORT', 'APP_VERSION', 'Terminal', 'FitAddon',
    'Text', 'Node', 'Element',
}

class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.names = set()

    def declare(self, name):
        self.names.add(name)

    def has(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return True
            s = s.parent
        return False


def walk_lets(node, scope):
    """把当前语句里的声明登记进 scope（不进入函数体）。"""
    if node is None:
        return
    t = node.type
    if t in ('VariableDeclaration',):
        for d in node.declarations:
            for n in pattern_names(d.id):
                scope.declare(n)
    elif t in ('FunctionDeclaration', 'ClassDeclaration'):
        if node.id:
            scope.declare(node.id.name)
    elif t == 'ImportDeclaration':
        for spec in node.specifiers:
            IMPORTED.add(spec.local.name)
    elif t == 'ExportNamedDeclaration' and getattr(node, 'declaration', None):
        walk_lets(node.declaration, scope)
    elif t == 'CatchClause' and node.param:
        for n in pattern_names(node.param):
            scope.declare(n)


def pattern_names(node):
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        if n.type == 'Identifier':
            out.append(n.name)
        elif n.type == 'ObjectPattern':
            for p in n.properties:
                stack.append(p.value if p.type == 'Property' else p.argument)
        elif n.type == 'ArrayPattern':
            stack.extend(n.elements)
        elif n.type == 'AssignmentPattern':
            stack.append(n.left)
        elif n.type == 'RestElement':
            stack.append(n.argument)
    return out


issues = []

def visit(node, scope):
    if node is None:
        return
    t = node.type

    if t in ('FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'):
        # 新作用域；先登记参数
        inner = Scope(scope)
        params = node.params or []
        for p in params:
            for n in pattern_names(p):
                inner.declare(n)
        if t == 'FunctionDeclaration' and node.id:
            scope.declare(node.id.name)
        if node.body:
            if node.body.type == 'BlockStatement':
                for st in node.body.body:
                    walk_lets(st, inner)
                for st in node.body.body:
                    visit(st, inner)
            else:
                visit(node.body, inner)
        return

    if t == 'BlockStatement':
        inner = Scope(scope)
        for st in node.body:
            walk_lets(st, inner)
        for st in node.body:
            visit(st, inner)
        return

    if t == 'ForStatement' or t == 'ForOfStatement' or t == 'ForInStatement':
        inner = Scope(scope)
        walk_lets(getattr(node, 'left', None), inner) if (getattr(node, 'left', None) and node.left.type == 'VariableDeclaration') else None
        if getattr(node, 'left', None) is not None:
            if node.left.type == 'VariableDeclaration':
                for d in node.left.declarations:
                    for n in pattern_names(d.id):
                        inner.declare(n)
            elif node.left.type == 'Identifier':
                check_read(node.left, scope)
        for key in ('init', 'test', 'update', 'right'):
            visit(getattr(node, key, None), inner)
        if node.body:
            if node.body.type == 'BlockStatement':
                for st in node.body.body:
                    walk_lets(st, inner)
                for st in node.body.body:
                    visit(st, inner)
            else:
                visit(node.body, inner)
        return

    if t == 'VariableDeclaration':
        for d in node.declarations:
            for n in pattern_names(d.id):
                scope.declare(n)
        for d in node.declarations:
            visit(d.init, scope)
        return

    if t == 'CatchClause':
        inner = Scope(scope)
        if node.param:
            for n in pattern_names(node.param):
                inner.declare(n)
        if node.body:
            for st in node.body.body:
                walk_lets(st, inner)
            for st in node.body.body:
                visit(st, inner)
        return

    if t == 'Identifier':
        check_read(node, scope)
        return

    if t == 'ExportNamedDeclaration':
        if getattr(node, 'declaration', None):
            visit(node.declaration, scope)
        return

    if t == 'MemberExpression':
        # 非计算属性名不是变量引用
        visit(node.object, scope)
        if node.computed:
            visit(node.property, scope)
        return

    if t in ('Property', 'MethodDefinition', 'PropertyDefinition'):
        # 对象字面量 / 类成员的 key（非计算）不是变量引用
        if not getattr(node, 'computed', False):
            k = getattr(node, 'key', None)
            if k is not None and k.type == 'Identifier':
                v = getattr(node, 'value', None)
                if v is not None and hasattr(v, 'type'):
                    visit(v, scope)
                return
        attrs = getattr(node, '__dict__', None)
        if not attrs:
            return
        for key, value in attrs.items():
            if key in ('loc', 'range', 'key'):
                continue
            if isinstance(value, list):
                for item in value:
                    if hasattr(item, 'type'):
                        visit(item, scope)
            elif hasattr(value, 'type'):
                visit(value, scope)
        return

    attrs = getattr(node, '__dict__', None)
    if not attrs:
        return
    for key, value in attrs.items():
        if key in ('loc', 'range'):
            continue
        if isinstance(value, list):
            for item in value:
                if hasattr(item, 'type'):
                    visit(item, scope)
        elif hasattr(value, 'type'):
            visit(value, scope)


def check_read(node, scope):
    name = node.name
    if name in MODULE_SCOPE or name in IMPORTED or name in BUILTINS:
        return
    if scope.has(name):
        return
    issues.append(name)


def collect_top_level(tree):
    top = Scope(None)
    for st in tree.body:
        walk_lets(st, top)
    return top


def preprocess(code):
    # esprima 4.0 不支持 ES2020 语法：可选链/空值合并/逻辑赋值。扫描作用域时
    # 做等价文本替换即可（不改变标识符的绑定关系）。
    # 顺序很重要：先处理可选调用/可选索引，再处理可选属性访问。
    code = code.replace('?.(', '(').replace('?.[', '[')
    code = code.replace('?.', '.').replace('??=', '||=').replace('??', '||')
    code = code.replace('catch{', 'catch (_implicitCatch){')
    code = code.replace('catch {', 'catch (_implicitCatch) {')  # ES2019 可选 catch 绑定
    return code


def scan(path):
    MODULE_SCOPE.clear(); IMPORTED.clear(); issues.clear()
    with open(path, encoding='utf-8') as f:
        code = f.read()
    code = preprocess(code)
    tree = esprima.parseModule(code, tolerant=True)
    top = collect_top_level(tree)
    MODULE_SCOPE.update(top.names)
    for st in tree.body:
        visit(st, top)
    # 顶层作用域里声明过的（hoisted 函数、var）已在 top.names
    from collections import Counter
    for name, cnt in Counter(issues).most_common():
        print(f'  {name}: {cnt} 处自由引用（本文件未声明）')


if __name__ == '__main__':
    scan(sys.argv[1] if len(sys.argv) > 1 else 'renderer/app.js')
