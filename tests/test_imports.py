"""验证核心模块可冷导入，并守住模块依赖方向。

依赖规则用静态分析（AST）检查，不执行被检查的代码：分层约定只有变成断言才守得住，
否则每次「先跑起来」的局部改动都会悄悄把环加回去。

规则依据见 docs/internal/流程/优化建议清单.md 的 B3 / B4 / B10。
"""
import ast
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 冷导入冒烟：包化后 relay 是包，门面 + 最重子模块各验一次，确认没有
# 只在「别的模块先加载过」时才成立的导入顺序依赖。
MODULES = [
    'server',
    'adb_worker',
    'diagnostics',
    'relay',
    'relay.relay_app',
]

# 底层模块：数据目录与版本号被所有层依赖，因此它们不能反向依赖任何人。
LEAF_MODULES = {'app_paths', 'app_version', 'device_key', 'tool_errors'}

# 入口模块只允许由解释器启动，不允许被其它模块 import。
ENTRY_MODULES = {'server'}

# relay 是扇入最高的内核（13 个模块 import 它）。它现在是一门面 + 若干子模块：
# 门面只允许再导出自己的子模块和纯 IO 工具，不允许引入任何业务模块。
RELAY_SUBMODULES = {
    'relay_constants', 'relay_store', 'relay_stream', 'relay_protocol', 'relay_pool',
    'relay_heartbeat', 'relay_maintenance', 'relay_trust', 'relay_health',
    'relay_policy', 'relay_pairing',
}
RELAY_ALLOWED_TOP_IMPORTS = {'relay_file_io', 'relay_cache'} | RELAY_SUBMODULES

# RelayNode 由这四个 mixin 拼成（B6）。它们共享同一个 self，因此不能反向
# import relay_node —— 门面在导入它们，反过来就是导入期环。
NODE_MIXINS = {
    'relay_node_heartbeat', 'relay_node_requests',
    'relay_node_remote', 'relay_node_pairing',
}

# RelayController 同理（B7）：核心与生命周期留在 relay_app，四类运行态成 mixin。
APP_MIXINS = {
    'relay_app_status', 'relay_app_session',
    'relay_app_windows', 'relay_app_pairing',
}

# 这些策略值只在调用时按模块属性读取（``relay_app_common.SESSION_MAX_FAILURES``），
# 是全层唯一的生效点。用 ``from ... import`` 会在导入期把值绑死：测试改了
# relay_app_common 里的值却看不到效果，表现为「调了参数但行为没变」。
APP_POLICY_CONSTANTS = {
    'APP_VERSION', 'DEFAULT_ALLOWED_SUBCOMMANDS', 'DEVICE_SELECTOR_TOKENS',
    'EVENT_LIMIT', 'PEER_ID_RE', 'PEER_UNCONFIRMED_MARKERS', 'SESSION_ID_PREFIX',
    'SESSION_MAX_FAILURES', 'SESSION_POLL_INTERVAL_S', 'SESSION_REOPEN_BACKOFF_S',
    'SESSION_RETRY_SLOW_S', 'STATUS_POLL_INTERVAL_S',
}

# 非顶层导入（函数内、条件分支内）是「已经存在环」的遮羞布：它把 import 推迟到
# 调用时才发生，绕开了循环。新增一条通常意味着又引入了一个环，所以这里登记当前
# 基线，新增即失败。
# 需要新增时改这张表，并在改动说明里写清为什么不拆成独立模块。
# 表里多出的（已删除的延迟导入）不影响通过，只是提示可以顺手清理。
DEFERRED_BASELINE = {
    # ADB 远端命令要拿 relay_app.strip_device_selector，顶层 import 会把
    # relay 全链路拖进每个只做本机 adb 的进程。
    'adb_service': {'relay_app'},
    'adb_worker': {'pty_host', 'shared_terminal', 'adb_runtime'},
    'agent_api': {'agent_extensions', 'relay_transfer'},
    'agent_client': {'relay_app'},
    'crash_diagnostics': {'adb_worker'},
    'db_download_host': {'pty_host'},
    'desktop_terminals': {'remote_plugins', 'extension_tools', 'plugin_devices'},
    'extension_tools': {'serial_stream'},
    'plugin_manager': {'remote_plugins'},
    'plugin_pack': {'relay_probe_build'},
    'pty_host': {'crash_diagnostics'},
    # relay_protocol 里的延迟导入是为了避免冷启动加载 zipfile，不是绕环。
    'relay_protocol': {'relay_archive'},
    # B7 把 RelayController 拆成四个 mixin，这几条延迟导入随方法搬到了真正
    # 读取它们的模块（原样保留，不是为了绕环）。
    'relay_app': {'relay_carrier', 'adb_worker'},
    'relay_app_status': {'adb_worker'},
    'relay_app_windows': {'windows_stream'},
    'relay_node': set(),
    # B6 把 RelayNode 的 102 个方法拆成四个 mixin，这几条延迟导入随方法
    # 一起搬到了真正读取它们的模块（原样保留，不是为了绕环）。
    'relay_node_requests': {'windows_executor', 'serial_stream', 'windows_stream', 'adb_pull'},
    'relay_transfer': {'relay'},
    'serial_terminal': {'xmodem_transfer'},
    'server': {'plugin_host', 'db_download_host', 'pty_host', 'app_paths',
               'crash_diagnostics', 'desktop_terminals', 'serial_recording',
               'extension_tools', 'serial_terminal', 'plugin_manager',
               'xmodem_transfer', 'relay_carrier', 'remote_plugins',
               'app_update', 'plugin_update', 'update_apply'},
    'windows_executor': {'powershell_pty', 'relay_transfer'},
}


def _project_modules() -> dict:
    """根目录下的扁平模块 + relay 包内的子模块（__init__.py 记作门面 relay）。"""
    modules = {path.stem: path for path in ROOT.glob('*.py')}
    pkg = ROOT / 'relay'
    for path in pkg.glob('*.py'):
        modules['relay' if path.name == '__init__.py' else path.stem] = path
    return modules


def _resolve_targets(node, names: set) -> set:
    """把一条 import 归一化成「项目模块名」。

    relay 包化后同一个模块有三种写法，规则要按同一份名字判定：
      import relay                    -> relay（门面）
      from relay import relay_node    -> relay_node
      from relay.relay_node import X  -> relay_node
      from .relay_node import X       -> relay_node（包内相对导入）
      from . import relay_node        -> relay_node
    """
    out = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split('.')
            if parts[0] == 'relay' and len(parts) > 1 and parts[1] in names:
                out.add(parts[1])
            else:
                out.add(parts[0])
        return {t for t in out if t in names}
    # ImportFrom
    if node.level:  # 包内相对导入：module 为 None 时取被导入的名字
        if node.module:
            out.add(node.module.split('.')[0])
        else:
            out.update(alias.name for alias in node.names)
        return {t for t in out if t in names}
    parts = (node.module or '').split('.')
    if parts[0] == 'relay':
        if len(parts) > 1 and parts[1] in names:
            out.add(parts[1])
        else:
            # 从门面上取的是符号而不是子模块（from relay import Result）
            out.update(alias.name for alias in node.names
                       if alias.name in names)
            if not out:
                out.add('relay')
    else:
        out.add(parts[0])
    return {t for t in out if t in names}


def _project_imports(path: Path, names: set) -> tuple:
    """返回 (顶层导入集合, 延迟导入集合)，只统计指向项目内模块的导入。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    top_level = {id(node) for node in tree.body
                 if isinstance(node, (ast.Import, ast.ImportFrom))}
    top, deferred = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _resolve_targets(node, names):
            (top if id(node) in top_level else deferred).add(target)
    return top, deferred


def architecture_checks():
    """返回 [(名称, 是否通过, 详情)]，不抛异常，便于一次报出全部越界。"""
    modules = _project_modules()
    names = set(modules)
    parsed, checks = {}, []

    for name, path in sorted(modules.items()):
        try:
            parsed[name] = _project_imports(path, names)
        except SyntaxError as exc:
            checks.append((f'{name} 可解析', False, str(exc)))
    if len(parsed) != len(modules):
        return checks

    for name in sorted(LEAF_MODULES & names):
        top, deferred = parsed[name]
        leaked = top | deferred
        checks.append((f'{name} 不依赖任何项目模块', not leaked,
                       f'出现了 {sorted(leaked)}' if leaked else ''))

    entry_hits = []
    for name, (top, deferred) in sorted(parsed.items()):
        if name in ENTRY_MODULES:
            continue
        hit = ENTRY_MODULES & (top | deferred)
        if hit:
            entry_hits.append(f'{name} -> {sorted(hit)}')
    checks.append(('入口模块未被其它模块 import', not entry_hits, '; '.join(entry_hits)))

    if 'relay' in parsed:
        top = parsed['relay'][0]
        extra = top - RELAY_ALLOWED_TOP_IMPORTS
        checks.append(('relay 门面只依赖自身子模块与文件 IO', not extra,
                       f'多出 {sorted(extra)}' if extra else ''))

    # 子模块反向 import 门面会立刻形成 relay ↔ relay_xxx 的环，而且让「只用一个
    # 子模块」的下游被迫加载整层。方向必须是：子模块 ← 门面。
    back_refs = []
    for name in sorted(RELAY_SUBMODULES & names):
        top, deferred = parsed[name]
        if 'relay' in (top | deferred):
            back_refs.append(name)
    checks.append(('relay 子模块不反向依赖门面', not back_refs, ', '.join(back_refs)))

    # RelayNode 的四个 mixin 同理：它们由 relay_node 导入，若反过来 import
    # relay_node 就是导入期环。共享状态靠同一个 self，不靠反向引用。
    node_back_refs = []
    for name in sorted(NODE_MIXINS & names):
        top, deferred = parsed[name]
        if 'relay_node' in (top | deferred):
            node_back_refs.append(name)
    checks.append(('RelayNode 的 mixin 不反向依赖门面', not node_back_refs,
                   ', '.join(node_back_refs)))

    # RelayController 的 mixin 同样不能反向 import relay_app（导入期环）。
    app_back_refs = []
    for name in sorted(APP_MIXINS & names):
        top, deferred = parsed[name]
        if 'relay_app' in (top | deferred):
            app_back_refs.append(name)
    checks.append(('RelayController 的 mixin 不反向依赖门面', not app_back_refs,
                   ', '.join(app_back_refs)))

    # 策略常量必须按模块属性读取。from-import 只在导入期取值一次，之后给
    # relay_app_common 的赋值就看不到效果 —— 这种「参数调了没用」的失效最难发现。
    # 兼容再导出是允许的（import 语句本身不算读取），但任何一处**裸读**都会
    # 把值绑死在别的模块的全局里。
    frozen = []
    for name in sorted((APP_MIXINS | {'relay_app'}) & names):
        tree = ast.parse(modules[name].read_text(encoding='utf-8'))
        illegal = {n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in APP_POLICY_CONSTANTS}
        if illegal:
            frozen.append(f'{name}: {sorted(illegal)}')
    checks.append(('策略常量按模块属性读取，未被裸名读死', not frozen,
                   '; '.join(frozen)))

    # 门面里一旦重新长出实现，拆分就会慢慢回退成单文件。这里把「纯再导出」变成断言。
    facade = ast.parse((ROOT / 'relay' / '__init__.py').read_text(encoding='utf-8'))
    impls = [node.name for node in facade.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    checks.append(('relay 门面只做再导出，不含实现', not impls, f'出现 {impls}' if impls else ''))

    added = {}
    for name, (_, deferred) in sorted(parsed.items()):
        baseline = DEFERRED_BASELINE.get(name, set())
        extra = deferred - baseline
        if extra:
            added[name] = sorted(extra)
        stale = baseline - deferred
        if stale:
            print(f'  [NOTE] {name} 的延迟导入基线条目已失效：{sorted(stale)}，可更新 DEFERRED_BASELINE')
    checks.append(('延迟导入未超出基线', not added,
                   '; '.join(f'{name} 新增 {mods}' for name, mods in added.items())))
    return checks


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

    print('\n[架构规则]')
    checks = architecture_checks()
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{f'：{detail}' if detail else ''}")
    if not all(passed for _, passed, _ in checks):
        ok = False
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
