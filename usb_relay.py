"""本机 USB 继电器：隔离的 32 位辅助进程直接调用兼容 DLL。

DLL ABI 参考 https://github.com/pavel-a/usb-relay-hid/blob/master/lib/usb_relay_device.h
仅使用原版导出；读取进程每次释放句柄，不与厂商 GUI 长期争用设备。
"""
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
import tempfile
import crash_diagnostics

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

class USBRelayService:
    NAME = '继电器'
    ACTIONS = frozenset({'config', 'save', 'scan', 'open', 'close', 'status', 'set', 'diagnose'})
    def __init__(self, config_path, log=None, *, config_storage=None):
        self.log = log or (lambda *_: None)
        self.config_path = Path(config_path)
        self.config_storage = config_storage
        self.tool_dir = ''
        self.remote_allowed = False
        self.devices = []
        self.serial = ''
        self.channels = 0
        self.states = []
        self.opened = False
        self._lock = threading.RLock()
        config = config_storage.read() if config_storage is not None else None
        try:
            if config is None:
                config = json.loads(self.config_path.read_text(encoding='utf-8'))
            self.tool_dir = str(config.get('toolDir') or '')
            self.remote_allowed = config.get('remoteAllowed') is True
        except (OSError, ValueError, AttributeError):
            pass

    def _view(self):
        return dict(toolDir=self.tool_dir, devices=list(self.devices), serial=self.serial,
                    channels=self.channels, states=list(self.states), opened=self.opened,
                    remoteAllowed=self.remote_allowed)

    def _save_config(self, directory, allowed):
        if self.config_storage is not None:
            self.config_storage.write({'toolDir': directory, 'remoteAllowed': allowed})
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'toolDir': directory, 'remoteAllowed': allowed}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.config_path)

    def set_remote_permission(self, allowed):
        if type(allowed) is not bool:
            raise ValueError('远程权限值必须为布尔值')
        with self._lock:
            self._save_config(self.tool_dir, allowed)
            self.remote_allowed = allowed
            self.log('USB', f'本机远程控制权限：{allowed}')
            return dict(success=True, output='远程控制权限已保存', data=self._view())

    def _reset(self):
        self.serial = ''
        self.channels = 0
        self.states = []
        self.opened = False

    def _validate_dir(self, value):
        directory = Path(str(value).strip()).resolve()
        for name in ('usb_relay_device.dll',):
            file = directory / name
            if not file.is_file():
                raise ValueError(f'工具目录缺少 {name}')
            data = file.read_bytes()
            try:
                offset = struct.unpack_from('<I', data, 60)[0]
                if data[:2] != b'MZ' or data[offset:offset+4] != b'PE\0\0' or struct.unpack_from('<H', data, offset+4)[0] != 0x14c:
                    raise ValueError('需要 32 位兼容继电器 DLL')
            except struct.error:
                raise ValueError(f'{name} 不是有效的 Windows 程序')
        return str(directory)

    def _probe(self, action, serial=''):
        self.log('USB', f'开始读取：{action}，设备 {serial or "枚举"}')
        if getattr(sys, 'frozen', False):
            helper = Path(sys._MEIPASS) / 'relay' / 'RelayProbe.exe'
            library_dir = Path(sys._MEIPASS) / 'relay'
        else:
            helper = Path(__file__).resolve().parent / 'build' / 'relay-probe' / 'RelayProbe.exe'
            library_dir = Path(__file__).resolve().parent / 'vendor' / 'usb-relay' / 'bin'
        if not helper.is_file():
            raise RuntimeError('缺少继电器辅助程序；源码运行请先执行 python -B relay_probe_build.py')
        argv = [str(helper), action, str(library_dir)]
        if action == 'status':
            argv.append(serial)
        elif action == 'set':
            argv.extend([serial, str(self._probe_channel), self._probe_operation])
        # 辅助进程每个调用边界落盘，主程序被防护终止时也能查看最后阶段。
        log_dir = Path(os.environ['ADB_DIAG_DIR']) if os.environ.get('ADB_DIAG_DIR') else (
            Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'ADBManager' / 'diagnostics')
        log_dir.mkdir(parents=True, exist_ok=True)
        stage_log = log_dir / f'relay-probe-{os.getpid()}.log'
        child_env = dict(os.environ, ADB_RELAY_STAGE_LOG=str(stage_log))
        self.log('USB', f'阶段日志：{stage_log}')
        proc = self._run_child(argv, cwd=str(library_dir), capture_output=True, timeout=10 if action == 'set' else 20,
                              creationflags=CREATE_NO_WINDOW, env=child_env)
        try:
            result = json.loads(proc.stdout.decode('utf-8-sig').strip())
        except (ValueError, UnicodeError):
            raise RuntimeError('继电器检测进程未返回有效结果：' + proc.stderr.decode('utf-8', errors='replace')[-600:])
        if proc.returncode or not result.get('success'):
            raise RuntimeError(result.get('output') or '继电器检测失败')
        self.log('USB', f'读取完成：{action}，设备 {serial or "枚举"}')
        return result

    def _run_child(self, argv, *, timeout, capture_output=True, **kwargs):
        """保留实际子进程 PID、耗时、退出码和 stderr，不改变调用超时语义。"""
        started = time.monotonic()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        self.log('USB', f'子进程启动 PID {proc.pid}，程序 {Path(argv[0]).name}，超时 {timeout}s')
        crash_diagnostics.mark('usb-child-running', childPid=proc.pid, program=Path(argv[0]).name, serial=self.serial)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log('USB', f'子进程超时 PID {proc.pid}，开始终止')
            proc.kill()
            proc.communicate()
            crash_diagnostics.mark('usb-child-timeout', childPid=proc.pid, exitCode=proc.returncode)
            raise
        elapsed = (time.monotonic()-started)*1000
        self.log('USB', f'子进程结束 PID {proc.pid}，退出码 {proc.returncode} / 0x{proc.returncode & 0xffffffff:08X}，耗时 {elapsed:.0f}ms，stdout {len(stdout)}B，stderr {len(stderr)}B')
        if stderr:
            self.log('USB', f'子进程 stderr PID {proc.pid}：{stderr.decode("utf-8", errors="replace")[-2000:]}')
        crash_diagnostics.mark('usb-child-returned', childPid=proc.pid, exitCode=proc.returncode, elapsedMs=elapsed)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    def _read(self):
        mask = self._probe('status', self.serial)['mask']
        self.states = [bool(int(mask) & (1 << i)) for i in range(self.channels)]
        self.log('USB', f'状态读取结果：设备 {self.serial}，mask=0x{int(mask):02X}，通道 {self.states}')

    def call(self, action, args=None):
        args = args or {}
        with self._lock:
            self.log('USB', f'请求 {action}，设备 {args.get("serial") or self.serial or "未选择"}')
            try:
                message = ''
                if action == 'config':
                    pass
                elif action == 'save':
                    directory = self._validate_dir(args.get('toolDir', ''))
                    self._save_config(directory, self.remote_allowed)
                    self.tool_dir = directory
                    self.devices = []
                    self._reset()
                    message = '工具目录已保存，请查找设备'
                elif action == 'scan':
                    self.states = [None] * self.channels
                    devices = self._probe('scan').get('devices') or []
                    if not isinstance(devices, list):
                        devices = [devices]
                    for device in devices:
                        if not re.fullmatch(r'[A-Za-z0-9]{1,64}', str(device.get('serial', ''))) or not 1 <= int(device['channels']) <= 8:
                            raise ValueError('设备信息异常')
                    self.devices = devices
                    if self.serial and not any(d['serial'] == self.serial for d in devices):
                        self._reset()
                    message = f'找到 {len(devices)} 个继电器设备'
                elif action == 'diagnose':
                    stage = args.get('stage')
                    if stage not in ('check', 'load', 'init', 'scan', 'status'):
                        raise ValueError('无效的诊断阶段')
                    serial = str(args.get('serial') or self.serial or '')
                    if stage == 'status' and not re.fullmatch(r'[A-Za-z0-9]{1,64}', serial):
                        raise ValueError('读取状态前请输入有效设备序列号')
                    probe = self._probe(stage, serial if stage == 'status' else '')
                    return dict(success=True, output=f'诊断 {stage} 完成：' + json.dumps(probe, ensure_ascii=False),
                                data=self._view(), diagnostic=probe)
                elif action == 'open':
                    serial = str(args.get('serial') or '')
                    device = next((d for d in self.devices if d['serial'] == serial), None)
                    if not device:
                        raise ValueError('请先查找并选择设备')
                    self._reset()
                    self.serial, self.channels = serial, int(device['channels'])
                    self.states = [None] * self.channels
                    self._read()
                    self.opened = True
                    message = '设备已打开'
                elif action == 'close':
                    self._reset()
                    message = '设备已关闭，继电器通断保持不变'
                elif action in ('status', 'set'):
                    if not self.opened:
                        raise ValueError('请先打开设备')
                    self.states = [None] * self.channels
                    if action == 'set':
                        channel, operation = args.get('channel'), args.get('operation')
                        if type(channel) is not int or channel not in [*range(1, self.channels+1), 255] or operation not in ('open', 'close'):
                            raise ValueError('通道或操作参数无效')
                        self.log('USB', f'开始控制：设备 {self.serial}，通道 {channel}，操作 {operation}')
                        self._probe_channel, self._probe_operation = channel, operation
                        try:
                            result = self._probe('set', self.serial)
                        finally:
                            self._probe_channel, self._probe_operation = 0, ''
                        message = ('全部通道' if channel == 255 else f'继电器 {channel}') + ('已打开' if operation == 'open' else '已关闭')
                        if 'mask' in result:
                            mask = int(result['mask'])
                            self.states = [bool(mask & (1 << i)) for i in range(self.channels)]
                            self.log('USB', f'控制后状态：设备 {self.serial}，mask=0x{mask:02X}，通道 {self.states}')
                        else:
                            return dict(success=True, output=message + '；状态读取失败：' + str(result.get('readError') or '未返回状态'), data=self._view())
                    else:
                        self._read()
                else:
                    raise ValueError('不支持的继电器操作')
                return dict(success=True, output=message, data=self._view())
            except subprocess.TimeoutExpired:
                self.log('USB', f'{action} 超时，结果未确认')
                self.states = [None] * self.channels
                return dict(success=False, output='操作超时，结果未确认，请刷新状态；不会自动重试', data=self._view())
            except Exception as exc:
                self.log('USB', f'{action} 失败：{type(exc).__name__}: {exc}')
                return dict(success=False, output=str(exc), data=self._view())
