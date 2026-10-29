"""ADB 只读诊断：将本机 ADB 故障转换为可操作的检查报告。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable, Optional


MAX_OUTPUT_CHARS = 4096


@dataclass
class DiagnosticCheck:
    id: str
    title: str
    status: str
    summary: str
    advice: str = ''
    command: Optional[list[str]] = None
    stdout: str = ''
    stderr: str = ''
    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiagnosticReport:
    id: str
    started_at: str
    finished_at: str
    selected_device: Optional[dict]
    checks: list[DiagnosticCheck]
    primary_action: Optional[dict]

    def to_dict(self) -> dict:
        data = asdict(self)
        data['checks'] = [check.to_dict() for check in self.checks]
        return data


@dataclass
class _ProbeResult:
    returncode: Optional[int]
    stdout: str = ''
    stderr: str = ''
    error: str = ''
    cancelled: bool = False
    duration_ms: int = 0


def _truncate(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value, False
    return value[:MAX_OUTPUT_CHARS] + '\n…（输出已截断）', True


def _device_states(output: str) -> list[tuple[str, str]]:
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('List of devices'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


class ADBDiagnosticRunner:
    """运行固定的 ADB 探针；不启动、停止或修改 ADB server。"""

    def __init__(
        self,
        adb_path: str,
        creationflags: int = 0,
        logger: Optional[Callable[[str, str], None]] = None,
        progress: Optional[Callable[[int, int, str, str], None]] = None,
    ):
        self.adb_path = adb_path
        self.creationflags = creationflags
        self.logger = logger or (lambda _category, _message: None)
        self.progress = progress or (lambda _current, _total, _check_id, _title: None)

    def _run_probe(self, args: list[str], timeout: float, cancel_event: threading.Event) -> _ProbeResult:
        started = time.monotonic()
        if cancel_event.is_set():
            return _ProbeResult(None, error='诊断已取消', cancelled=True)
        try:
            proc = subprocess.Popen(
                [self.adb_path, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=self.creationflags,
            )
        except FileNotFoundError:
            return _ProbeResult(None, error=f'找不到 ADB 可执行文件：{self.adb_path}')
        except Exception as exc:
            return _ProbeResult(None, error=f'无法启动 ADB：{type(exc).__name__}: {exc}')

        deadline = started + timeout
        while True:
            if cancel_event.is_set():
                proc.kill()
                stdout, stderr = proc.communicate()
                return _ProbeResult(
                    None, stdout or '', stderr or '', '诊断已取消', True,
                    int((time.monotonic() - started) * 1000),
                )
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
                return _ProbeResult(
                    proc.returncode, stdout or '', stderr or '', '', False,
                    int((time.monotonic() - started) * 1000),
                )
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    return _ProbeResult(
                        None, stdout or '', stderr or '', f'命令超时（{timeout:g} 秒）', False,
                        int((time.monotonic() - started) * 1000),
                    )

    @staticmethod
    def _probe_check(
        check_id: str, title: str, probe: _ProbeResult, command: list[str],
        success_summary: str, failure_advice: str,
    ) -> DiagnosticCheck:
        stdout, stdout_truncated = _truncate(probe.stdout)
        stderr, stderr_truncated = _truncate(probe.stderr)
        if probe.cancelled:
            status, summary, advice = 'cancelled', '诊断已取消', ''
        elif probe.error:
            status, summary, advice = 'fail', probe.error, failure_advice
        elif probe.returncode == 0:
            status, summary, advice = 'pass', success_summary, ''
        else:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            hint = detail[-1] if detail else f'exit code {probe.returncode}'
            status, summary, advice = 'fail', f'命令失败：{hint[:240]}', failure_advice
        return DiagnosticCheck(
            check_id, title, status, summary, advice, command, stdout, stderr,
            probe.duration_ms, stdout_truncated or stderr_truncated,
        )

    @staticmethod
    def _skip(check_id: str, title: str, summary: str) -> DiagnosticCheck:
        return DiagnosticCheck(check_id, title, 'skip', summary)

    def _environment_check(self, command_output: str) -> DiagnosticCheck:
        user_home = os.environ.get('USERPROFILE') or os.path.expanduser('~')
        android_dir = os.path.join(user_home, '.android') if user_home else ''
        lowered = command_output.lower()
        cannot_mkdir = 'cannot mkdir' in lowered and '.android' in lowered
        if not user_home or not os.path.isdir(user_home):
            return DiagnosticCheck(
                'environment', '用户目录与 ADB key 环境', 'fail', '无法访问 Windows 用户目录',
                '检查 USERPROFILE 环境变量和用户目录权限。',
            )
        if cannot_mkdir:
            return DiagnosticCheck(
                'environment', '用户目录与 ADB key 环境', 'fail',
                'ADB 无法创建 .android 目录',
                '检查 USERPROFILE/.android 的权限和重定向设置。',
            )
        if not os.path.isdir(android_dir):
            return DiagnosticCheck(
                'environment', '用户目录与 ADB key 环境', 'warn',
                '.android 目录尚不存在',
                '首次授权时 ADB 会创建该目录；若同时出现权限错误，请检查用户目录权限。',
            )
        try:
            os.listdir(android_dir)
        except OSError as exc:
            return DiagnosticCheck(
                'environment', '用户目录与 ADB key 环境', 'fail',
                f'.android 目录不可访问：{exc}',
                '检查 USERPROFILE/.android 的读取权限和重定向设置。',
            )
        return DiagnosticCheck(
            'environment', '用户目录与 ADB key 环境', 'pass',
            '.android 目录可访问', '', None, android_dir,
        )

    @staticmethod
    def _port_check() -> DiagnosticCheck:
        started = time.monotonic()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                connected = sock.connect_ex(('127.0.0.1', 5037)) == 0
            if connected:
                return DiagnosticCheck('port-5037', 'ADB server 端口 5037', 'pass', '127.0.0.1:5037 可连接')
            return DiagnosticCheck(
                'port-5037', 'ADB server 端口 5037', 'warn', '127.0.0.1:5037 未监听',
                '若 ADB server 检查也失败，请在命令行重启 ADB server。',
            )
        except OSError as exc:
            return DiagnosticCheck(
                'port-5037', 'ADB server 端口 5037', 'warn', f'无法查询 5037：{exc}',
                '请结合 ADB server 检查结果处理。',
            )
        finally:
            # `finally` is intentionally retained for future probes; duration is assigned below.
            _ = int((time.monotonic() - started) * 1000)

    @staticmethod
    def _primary_action(checks: list[DiagnosticCheck]) -> Optional[dict]:
        by_id = {check.id: check for check in checks}
        for check_id in ('adb-binary', 'adb-server', 'environment'):
            check = by_id.get(check_id)
            if check and check.status == 'fail':
                return {'priority': 'P0', 'title': check.title, 'summary': check.summary, 'advice': check.advice}
        device_check = by_id.get('device-state')
        if device_check and device_check.status in ('fail', 'warn'):
            return {'priority': 'P1', 'title': device_check.title, 'summary': device_check.summary, 'advice': device_check.advice}
        list_check = by_id.get('device-list')
        if list_check and list_check.status == 'warn' and '异常设备' in list_check.summary:
            return {'priority': 'P1', 'title': list_check.title, 'summary': list_check.summary, 'advice': list_check.advice}
        if list_check and list_check.status == 'warn':
            return {'priority': 'P2', 'title': list_check.title, 'summary': list_check.summary, 'advice': list_check.advice}
        if any(check.status == 'fail' for check in checks):
            check = next(check for check in checks if check.status == 'fail')
            return {'priority': 'P0', 'title': check.title, 'summary': check.summary, 'advice': check.advice}
        return {'priority': '无', 'title': 'ADB 基础链路正常', 'summary': '未发现需要立即处理的 ADB 问题。', 'advice': ''}

    def run(self, selected_device: Optional[dict], cancel_event: threading.Event) -> DiagnosticReport:
        started_at = time.strftime('%Y-%m-%d %H:%M:%S')
        report_id = uuid.uuid4().hex[:12]
        total = 7 if selected_device else 6
        checks: list[DiagnosticCheck] = []

        def add(check: DiagnosticCheck):
            checks.append(check)
            self.logger('DIAG', f'[{report_id}] {check.id}: {check.status} - {check.summary}')

        def begin(check_id: str, title: str):
            self.progress(len(checks) + 1, total, check_id, title)

        begin('runtime', '应用运行信息')
        source = '环境变量 ADB_PATH' if os.environ.get('ADB_PATH') else ('包内 platform-tools' if getattr(sys, 'frozen', False) else 'PATH')
        add(DiagnosticCheck(
            'runtime', '应用运行信息', 'pass',
            f"运行模式：{'冻结 EXE' if getattr(sys, 'frozen', False) else '源码'}；ADB 来源：{source}",
            command=None, stdout=self.adb_path,
        ))

        if cancel_event.is_set():
            for check_id, title in [('adb-binary', 'ADB 可执行文件与版本'), ('adb-server', 'ADB server 可达性'), ('port-5037', 'ADB server 端口 5037'), ('device-list', '设备枚举'), ('environment', '用户目录与 ADB key 环境')]:
                add(self._skip(check_id, title, '诊断已取消'))
            if selected_device:
                add(self._skip('device-state', '目标设备状态', '诊断已取消'))
            return DiagnosticReport(report_id, started_at, time.strftime('%Y-%m-%d %H:%M:%S'), selected_device, checks, self._primary_action(checks))

        begin('adb-binary', 'ADB 可执行文件与版本')
        binary = self._run_probe(['version'], 5, cancel_event)
        binary_check = self._probe_check(
            'adb-binary', 'ADB 可执行文件与版本', binary, ['adb', 'version'], 'ADB 可执行文件可用',
            '配置正确的 ADB_PATH，或安装/修复 Android platform-tools。',
        )
        add(binary_check)

        command_output = binary.stdout + '\n' + binary.stderr
        if binary_check.status != 'pass':
            for check_id, title in [('adb-server', 'ADB server 可达性'), ('port-5037', 'ADB server 端口 5037'), ('device-list', '设备枚举')]:
                begin(check_id, title)
                add(self._skip(check_id, title, 'ADB 可执行文件不可用，跳过依赖检查'))
            begin('environment', '用户目录与 ADB key 环境')
            add(self._environment_check(command_output))
            if selected_device:
                begin('device-state', '目标设备状态')
                add(self._skip('device-state', '目标设备状态', 'ADB 可执行文件不可用，跳过设备检查'))
            report = DiagnosticReport(report_id, started_at, time.strftime('%Y-%m-%d %H:%M:%S'), selected_device, checks, self._primary_action(checks))
            return report

        begin('adb-server', 'ADB server 可达性')
        # `get-state` 会在多设备场景报 "more than one device/emulator"，
        # 因而不能用于 host 级 daemon 可达性检查。
        server_probe = self._run_probe(['host-features'], 5, cancel_event)
        server_check = self._probe_check(
            'adb-server', 'ADB server 可达性', server_probe, ['adb', 'host-features'], 'ADB server 可达',
            '检查 5037 端口，并在命令行执行 adb kill-server 后再执行 adb start-server。',
        )
        add(server_check)
        command_output += '\n' + server_probe.stdout + '\n' + server_probe.stderr

        begin('port-5037', 'ADB server 端口 5037')
        port_check = self._port_check()
        port_check.duration_ms = 0
        add(port_check)

        begin('device-list', '设备枚举')
        list_probe = self._run_probe(['devices', '-l'], 8, cancel_event)
        command_output += '\n' + list_probe.stdout + '\n' + list_probe.stderr
        list_stdout, list_truncated = _truncate(list_probe.stdout)
        list_stderr, list_err_truncated = _truncate(list_probe.stderr)
        device_states = _device_states(list_probe.stdout)
        if list_probe.cancelled:
            list_check = DiagnosticCheck('device-list', '设备枚举', 'cancelled', '诊断已取消', command=['adb', 'devices', '-l'])
        elif list_probe.error or list_probe.returncode != 0:
            fallback = self._run_probe(['devices'], 8, cancel_event) if not cancel_event.is_set() else _ProbeResult(None, error='诊断已取消', cancelled=True)
            command_output += '\n' + fallback.stdout + '\n' + fallback.stderr
            if fallback.returncode == 0:
                fallback_states = _device_states(fallback.stdout)
                list_check = DiagnosticCheck(
                    'device-list', '设备枚举', 'warn', '详细设备列表失败，已使用基础列表回退',
                    '检查 ADB server 状态；基础设备列表仍可用。', ['adb', 'devices', '-l'],
                    list_stdout, list_stderr, list_probe.duration_ms + fallback.duration_ms,
                    list_truncated or list_err_truncated,
                )
                device_states = fallback_states
            else:
                list_check = self._probe_check(
                    'device-list', '设备枚举', list_probe, ['adb', 'devices', '-l'], '',
                    '检查 USB/网络连接和 ADB server；修复 P0 问题后重新诊断。',
                )
        elif not device_states:
            list_check = DiagnosticCheck(
                'device-list', '设备枚举', 'warn', 'ADB 正常，但未发现已连接设备',
                '检查数据线、USB 调试授权、设备驱动或 TCP/IP 调试地址。', ['adb', 'devices', '-l'],
                list_stdout, list_stderr, list_probe.duration_ms, list_truncated or list_err_truncated,
            )
        else:
            abnormal = next(((serial, state) for serial, state in device_states if state != 'device'), None)
            if abnormal:
                serial, device_state = abnormal
                advice = '在设备上确认 RSA 调试授权后重新诊断。' if device_state == 'unauthorized' else '拔插 USB 或检查网络连接后重新诊断。'
                list_check = DiagnosticCheck(
                    'device-list', '设备枚举', 'warn', f'发现异常设备：{serial}（{device_state}）', advice,
                    ['adb', 'devices', '-l'], list_stdout, list_stderr, list_probe.duration_ms,
                    list_truncated or list_err_truncated,
                )
            else:
                list_check = DiagnosticCheck(
                    'device-list', '设备枚举', 'pass', f'发现 {len(device_states)} 台可用设备', '',
                    ['adb', 'devices', '-l'], list_stdout, list_stderr, list_probe.duration_ms,
                    list_truncated or list_err_truncated,
                )
        add(list_check)

        begin('environment', '用户目录与 ADB key 环境')
        add(self._environment_check(command_output))

        if selected_device:
            begin('device-state', '目标设备状态')
            transport_id = selected_device.get('transportId')
            serial = str(selected_device.get('serial', '')).strip()
            if transport_id not in (None, ''):
                args = ['-t', str(transport_id), 'get-state']
            elif serial:
                args = ['-s', serial, 'get-state']
            else:
                add(DiagnosticCheck('device-state', '目标设备状态', 'skip', '未提供有效设备标识'))
                args = None
            if args:
                device_probe = self._run_probe(args, 5, cancel_event)
                device_check = self._probe_check(
                    'device-state', '目标设备状态', device_probe, ['adb', *args], '目标设备状态正常',
                    '检查设备授权状态、USB/网络连接和 transport ID。',
                )
                state_text = (device_probe.stdout or device_probe.stderr).strip().lower()
                if device_check.status == 'fail' and 'unauthorized' in state_text:
                    device_check.summary = '设备未授权（unauthorized）'
                    device_check.advice = '在设备上确认 RSA 调试授权后重新诊断。'
                    device_check.status = 'warn'
                elif device_check.status == 'fail' and 'offline' in state_text:
                    device_check.summary = '设备离线（offline）'
                    device_check.advice = '拔插 USB 或检查网络连接后重新诊断。'
                    device_check.status = 'warn'
                add(device_check)

        report = DiagnosticReport(report_id, started_at, time.strftime('%Y-%m-%d %H:%M:%S'), selected_device, checks, self._primary_action(checks))
        self.logger('DIAG', f'[{report_id}] completed: {report.primary_action["priority"] if report.primary_action else "unknown"}')
        return report
