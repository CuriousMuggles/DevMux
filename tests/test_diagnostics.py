"""验证 ADB 诊断的核心状态、优先级与只读命令约束。"""
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _runner(responses):
    from diagnostics import ADBDiagnosticRunner, DiagnosticCheck, _ProbeResult

    runner = ADBDiagnosticRunner('fake-adb.exe')
    calls = []

    def fake_probe(args, _timeout, _cancel_event):
        calls.append(args)
        value = responses.get(tuple(args), _ProbeResult(0, ''))
        if isinstance(value, Exception):
            raise value
        return value

    runner._run_probe = fake_probe
    runner._port_check = lambda: DiagnosticCheck(
        'port-5037', 'ADB server 端口 5037', 'pass', '127.0.0.1:5037 可连接'
    )
    return runner, calls, _ProbeResult


def _by_id(report):
    return {check.id: check for check in report.checks}


def run() -> bool:
    ok = True
    from diagnostics import _ProbeResult

    # P0：找不到 ADB 后，其余 ADB 检查必须跳过，不能执行任何修复命令。
    runner, calls, _ = _runner({
        ('version',): _ProbeResult(None, error='找不到 ADB 可执行文件：fake-adb.exe'),
    })
    report = runner.run(None, threading.Event())
    passed = report.primary_action['priority'] == 'P0' and calls == [['version']]
    print(f'  [{"PASS" if passed else "FAIL"}] 缺少 ADB 显示 P0 且不触发修复')
    ok &= passed

    # P2：daemon 正常、没有设备是警告，不应诊断成 ADB server 故障。
    runner, calls, _ = _runner({
        ('version',): _ProbeResult(0, 'Android Debug Bridge version 1.0.41'),
        ('host-features',): _ProbeResult(0, 'shell_v2,stat_v2\n'),
        ('devices', '-l'): _ProbeResult(0, 'List of devices attached\n'),
    })
    report = runner.run(None, threading.Event())
    checks = _by_id(report)
    passed = report.primary_action['priority'] == 'P2' and checks['device-list'].status == 'warn'
    print(f'  [{"PASS" if passed else "FAIL"}] 无设备显示 P2 警告')
    ok &= passed

    # P1：未授权设备在设备列表阶段即可优先提示授权。
    runner, calls, _ = _runner({
        ('version',): _ProbeResult(0, 'Android Debug Bridge version 1.0.41'),
        ('host-features',): _ProbeResult(0, 'shell_v2,stat_v2\n'),
        ('devices', '-l'): _ProbeResult(0, 'List of devices attached\nABC\tunauthorized transport_id:1\n'),
    })
    report = runner.run(None, threading.Event())
    passed = report.primary_action['priority'] == 'P1' and '授权' in report.primary_action['advice']
    print(f'  [{"PASS" if passed else "FAIL"}] 未授权设备显示 P1 建议')
    ok &= passed

    # 选中离线设备时使用 transport ID，结果保留为 P1。
    runner, calls, _ = _runner({
        ('version',): _ProbeResult(0, 'Android Debug Bridge version 1.0.41'),
        ('host-features',): _ProbeResult(0, 'shell_v2,stat_v2\n'),
        ('devices', '-l'): _ProbeResult(0, 'List of devices attached\nABC\tdevice transport_id:7\n'),
        ('-t', '7', 'get-state'): _ProbeResult(1, '', 'error: device offline\n'),
    })
    report = runner.run({'serial': 'ABC', 'transportId': 7}, threading.Event())
    checks = _by_id(report)
    passed = report.primary_action['priority'] == 'P1' and checks['device-state'].status == 'warn' and ['-t', '7', 'get-state'] in calls
    print(f'  [{"PASS" if passed else "FAIL"}] 离线设备使用 transport ID 并显示 P1')
    ok &= passed

    # 已取消的诊断不得启动 ADB 命令。
    runner, calls, _ = _runner({})
    cancelled = threading.Event()
    cancelled.set()
    report = runner.run(None, cancelled)
    passed = calls == [] and all(check.status == 'skip' or check.id == 'runtime' for check in report.checks)
    print(f'  [{"PASS" if passed else "FAIL"}] 取消诊断不启动 ADB 子进程')
    ok &= passed

    return bool(ok)


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
