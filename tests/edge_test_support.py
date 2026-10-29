"""Edge 启动、CDP 就绪检测和进程树回收的共用测试工具。"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def webview_test_environment():
    """只为测试子进程补充受限 Windows 环境的 WebView2 启动参数。"""
    arguments = os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '')
    return {'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS':
            f'{arguments} --no-sandbox --disable-gpu'.strip()}


if os.name == 'nt':
    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_longlong),
            ('PerJobUserTimeLimit', ctypes.c_longlong),
            ('LimitFlags', ctypes.c_uint32),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', ctypes.c_uint32),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', ctypes.c_uint32),
            ('SchedulingClass', ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', _BasicLimitInformation),
            ('IoInfo', _IoCounters),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]


class EdgeSession:
    """将 Edge 及其全部子进程放入 kill-on-close Job，并通过系统分配端口连接 CDP。"""

    def __init__(self, process, job, profile: Path, stderr_path: Path):
        self.process = process
        self._job = job
        self.profile = Path(profile)
        self.stderr_path = Path(stderr_path)
        self.port = None
        self.endpoint = None

    @classmethod
    def start(cls, executable, arguments, profile, *, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, creationflags=0):
        if os.name != 'nt':
            raise RuntimeError('Edge 测试进程树隔离仅支持 Windows')
        profile = Path(profile).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        flags = creationflags | getattr(subprocess, 'CREATE_NO_WINDOW', 0) | 0x00000004  # CREATE_SUSPENDED
        # 当前 Edge 的 GPU 子进程在受限 Windows 测试环境会以
        # STATUS_ACCESS_DENIED 退出，随后浏览器主动 fatal。强制软件
        # 渲染，避免每个 UI 用例各自遗漏一部分兼容开关。
        software_rendering = [
            '--no-sandbox',
            '--disable-gpu', '--disable-gpu-compositing', '--disable-gpu-rasterization',
            '--disable-accelerated-2d-canvas', '--disable-accelerated-video-decode',
            '--disable-accelerated-video-encode', '--disable-zero-copy',
            '--disable-gpu-sandbox', '--in-process-gpu', '--use-angle=swiftshader',
        ]
        command = [str(executable), *software_rendering, *arguments,
                   '--remote-debugging-port=0', f'--user-data-dir={profile}']
        stderr_path = profile / 'edge-stderr.log'
        if stderr is subprocess.DEVNULL:
            with stderr_path.open('wb') as error_log:
                process = subprocess.Popen(command, stdout=stdout, stderr=error_log, creationflags=flags)
        else:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, creationflags=flags)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                       ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
            resume_process = ctypes.WinDLL('ntdll').NtResumeProcess
            resume_process.argtypes = [ctypes.c_void_p]
            resume_process.restype = ctypes.c_long
            status = resume_process(ctypes.c_void_p(int(process._handle)))
            if status != 0:
                raise OSError(f'NtResumeProcess 失败: 0x{status & 0xffffffff:08x}')
        except Exception:
            kernel32.TerminateJobObject(job, 1)
            kernel32.TerminateProcess(ctypes.c_void_p(int(process._handle)), 1)
            kernel32.CloseHandle(job)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        return cls(process, job, profile, stderr_path)

    def wait_ready(self, timeout=20, expected_url=None):
        active = self.profile / 'DevToolsActivePort'
        deadline = time.monotonic() + timeout
        last_error = 'DevToolsActivePort 尚未生成'
        while time.monotonic() < deadline:
            # Edge 155 的 msedge.exe 启动器会先正常退出，真正的浏览器子进程随后
            # 才写 DevToolsActivePort；不能把父进程退出当成浏览器启动失败。
            try:
                lines = active.read_text(encoding='utf-8').splitlines()
                self.port = int(lines[0])
                base = f'http://127.0.0.1:{self.port}'
                with urllib.request.urlopen(f'{base}/json/version', timeout=1) as response:
                    version = json.load(response)
                if not version.get('webSocketDebuggerUrl'):
                    raise RuntimeError('CDP /json/version 未返回 websocket 地址')
                with urllib.request.urlopen(f'{base}/json/list', timeout=1) as response:
                    targets = json.load(response)
                page = next((item for item in targets if item.get('type') == 'page'
                             and (expected_url is None or item.get('url') == expected_url)), None)
                if page and page.get('webSocketDebuggerUrl'):
                    self.endpoint = page['webSocketDebuggerUrl']
                    return self.endpoint
                last_error = 'CDP 已启动但尚无页面目标'
            except (OSError, ValueError, IndexError, json.JSONDecodeError) as exc:
                last_error = f'{type(exc).__name__}: {exc}'
            time.sleep(.1)
        raise TimeoutError(f'Edge CDP 在 {timeout}s 内未就绪: {last_error}')

    def close(self):
        if self._job:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            # 显式终止本次测试的进程树，再关闭 Job；不依赖最后一个句柄的释放时机。
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.TerminateJobObject(self._job, 0)
            kernel32.CloseHandle(self._job)
            self._job = None
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        try:
            log = self.stderr_path.read_text(encoding='utf-8', errors='replace')
            errors = [line for line in log.splitlines()
                      if 'FATAL:' in line or 'GPU process exited unexpectedly' in line]
            if errors:
                print('  [Edge diagnostics] ' + '\n  '.join(errors[-8:]), file=sys.stderr)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
