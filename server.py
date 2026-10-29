"""
DevMux Server
------------------
HTTP  static files  -> http://localhost:8080  (renderer/)
WebSocket control   -> ws://localhost:8765

All ConPTY / adb polling happens in adb_worker background threads.
Server marshals events to the asyncio loop and pushes to connected browsers.
"""
import asyncio
import functools
import atexit
import base64
import uuid
import concurrent.futures
import ctypes
from collections import deque
import json
import os
import re
import shlex
import sys

# 下载辅助入口在主后端及窗口初始化之前执行，隔离 USB 原生调用。
if len(sys.argv) > 1 and sys.argv[1] == '--db-download-host':
    from db_download_host import run_host as run_download_host
    try:
        run_download_host(sys.argv[2:])
    except Exception:
        import traceback as _download_traceback
        _download_traceback.print_exc()
        raise SystemExit(1)
    raise SystemExit(0)

# windowed 冻结版复用自身作为 PTY 辅助进程；须在后端/窗口初始化前分流。
if '--adb-pty-host' in sys.argv:
    from pty_host import run_host
    run_host(sys.argv[-1])
    raise SystemExit(0)

import threading
_COMMANDS_LOCK = threading.RLock()
import time
import traceback
import subprocess
import shutil
import tempfile
from app_version import APP_VERSION, APP_TITLE
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Set

import websockets
from websockets import ServerConnection as WebSocketServerProtocol

import adb_worker
from adb_worker import ADBWorker, ADB_PATH, get_all_devices, log, make_key
from diagnostics import ADBDiagnosticRunner
from db_flash import DBFlashService, RealExecutor
from db_flash_extension import DBFlashExtension
from relay_app import RelayController, default_session_id
import relay_app
from shared_terminal import SharedTerminalBackend
from agent_api import AgentAPI
from windows_executor import WindowsService
from usb_relay import USBRelayService
from agent_client import runtime_directory
import secrets
from pathlib import Path

# Suppress console windows for child processes when running as a GUI (windowed) app
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
BROADCAST_QUEUE_LIMIT = 256
EXECUTOR_MAX_WORKERS = 4

# ---------------------------------------------------------------------------
# Resource path helpers (must be before RENDERER_DIR)
# ---------------------------------------------------------------------------
def _exe_dir():
    """Return the directory containing bundled resources (PyInstaller _MEIPASS) or script dir."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

# ANSI escape code pattern — strips color codes, cursor moves, screen clears, etc.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][0-9;]*[^\x1b]*\x1b\\|\x1b[\[\]()][0-9;]*[~!@#$%^&*()=+<>?/\\|]|\x1b[PX^_]|[\x08\r]')
def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a string, keeping plain text."""
    return _ANSI_RE.sub('', text).replace('\x00', '')


def add_recording_timestamps(text: str, at_line_start: bool = True) -> tuple[str, bool]:
    """为终端记录的每个逻辑行添加与程序日志一致的时间戳。"""
    if not text:
        return '', at_line_start

    output = []
    for segment in _iter_line_segments(text):
        if at_line_start:
            output.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] ')
        output.append(segment)
        at_line_start = segment.endswith(('\r', '\n'))
    return ''.join(output), at_line_start


def _iter_line_segments(text: str):
    """把文本按行拆成若干段，每段保留自己原来的行结束符。

    处理 \\r\\n / \\n / \\r 三种行结束符；空文本返回空列表；
    末尾不带结束符的一段作为最后一行返回（如只有一行的快路径）。
    """
    if not text:
        return []
    segments = []
    i = 0
    n = len(text)
    while i < n:
        j = i
        while j < n and text[j] not in ('\r', '\n'):
            j += 1
        if j < n:
            if text[j] == '\r' and j + 1 < n and text[j + 1] == '\n':
                segments.append(text[i:j + 2])
                j += 2
            else:
                segments.append(text[i:j + 1])
                j += 1
        else:
            if i < n:
                segments.append(text[i:])
        i = j
    return segments

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env_port(name: str, default: int) -> int:
    """读取端口类环境变量，非法值一律回落到默认端口。

    同机起多个实例（跨机中转的双实例验证、UI E2E）时必须能各自指定端口，
    否则第二个实例只能靠 +1 重试撞到哪算哪。这里只做一次启动期解析，
    之后 `HTTP_PORT` / `WS_PORT` 仍是普通模块属性，测试可以直接改写。
    """
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        log('MAIN', f'{name}={raw!r} 不是合法端口，回落 {default}')
        return default
    if not (1024 <= port <= 65535):
        log('MAIN', f'{name}={port} 超出 1024-65535，回落 {default}')
        return default
    return port


HTTP_PORT = _env_port('HTTP_PORT', 8080)
_HTTP_PORT = HTTP_PORT
WS_PORT = _env_port('WS_PORT', 8765)
RENDERER_DIR = os.path.join(_exe_dir(), 'renderer')


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔类环境变量，只在明确写了真值时才为真。"""
    raw = (os.environ.get(name) or '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')


# ``DEVMUX_NO_LOCAL_DEVICES=1``：本实例完全不识别本机 adb 设备，窗口里只剩
# 远端（中转）设备。两种用途：① 主控机不接设备、只去管别的机器上的设备；
# ② 同机双实例验证时屏蔽掉「两台设备其实都插在同一台 PC 上」这个假象 —— 屏蔽后
# 演示脚本里主控的每一次操作都只能经从机完成，跨机链路才算真的被验证过。
# 这里读一次、往各模块显式下发（而非让各模块自己读环境变量）：同机双实例共享
# 同一个进程环境，只有调用方知道该屏蔽哪一个实例。
LOCAL_DEVICES_ENABLED = not _env_flag('DEVMUX_NO_LOCAL_DEVICES',
                                      _env_flag('ADB_MANAGER_NO_LOCAL_DEVICES'))

# Set at startup so the frontend can read the actual WebSocket port
_WS_PORT = WS_PORT
_MIJIA_PAGE_TOKEN = secrets.token_urlsafe(32)

# ---------------------------------------------------------------------------
# HTTP static file server (runs in a background thread)
# ---------------------------------------------------------------------------
class CustomHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=RENDERER_DIR, **kwargs)

    def end_headers(self):
        # Prevent caching
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        import random
        cb = str(random.randint(100000, 999999))
        if self.path.startswith('/?_t=') or self.path == '/' or self.path == '/index.html' or self.path.split('?',1)[0] in ('/desktop-terminal.html', '/desktop-mijia.html'):
            # Serve index.html with actual WebSocket port injected
            page = self.path.split('?', 1)[0].lstrip('/')
            idx_path = os.path.join(RENDERER_DIR, page if page in ('desktop-terminal.html', 'desktop-mijia.html') else 'index.html')
            if os.path.exists(idx_path):
                with open(idx_path, 'r', encoding='utf-8') as f:
                    html = f.read()
                html = html.replace('__WS_PORT__', str(_WS_PORT))
                html = html.replace('__APP_VERSION__', APP_VERSION)
                html = html.replace('__MIJIA_PAGE_TOKEN__', _MIJIA_PAGE_TOKEN)
                html = html.replace('v=52', f'v={cb}')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))
                return
        # Default: serve static files
        return super().do_GET()

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        # 浏览器测试或窗口关闭时可能提前断开静态资源连接，不应打印线程堆栈。
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass


class StaticHTTPServer(ThreadingHTTPServer):
    """静态资源 HTTP 服务端（只用它的端口语义，别的地方别直接实例化 ThreadingHTTPServer）。

    Windows 的 ``SO_REUSEADDR`` 和 POSIX 语义不同：开着它时**第二个进程也能绑成功**
    同一个 ``127.0.0.1:8080``，两边都处于 LISTEN，连接落到哪个套接字由系统决定。而
    stdlib 的 ``ThreadingHTTPServer`` 默认 ``allow_reuse_address = True``，正好踩这个坑 ——
    第二个应用实例于是可能被第一个实例的 HTTP 服务端服务，``index.html`` 里注入的是
    **第一个实例的 WS_PORT**，它的界面就去连第一个实例的后端：两个窗口看/改同一份
    状态（用户报的「开两个应用，配置有干扰」）。关掉它，端口冲突就以 ``OSError``
    暴露出来，走 ``start_http_server`` 里的 +1 重试。

    POSIX 下 ``SO_REUSEADDR`` 不允许两个活监听（那是 ``SO_REUSEPORT``），保留它以免
    重启时被 TIME_WAIT 挡住，所以只在 Windows 关。
    """

    allow_reuse_address = os.name != 'nt'
    daemon_threads = True
    # 页面冷启动并发加载脚本/样式；默认5条队列在Windows会拒绝资源连接。
    request_queue_size = 128


def start_http_server():
    global _HTTP_PORT
    port = HTTP_PORT
    while True:
        try:
            server = StaticHTTPServer(('127.0.0.1', port), CustomHandler)
            break
        except OSError as e:
            if port < HTTP_PORT + 10:
                port += 1
                log('HTTP', f'Port {port-1} busy, trying {port}')
            else:
                log('HTTP', f'Failed to bind HTTP: {e}')
                raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    _HTTP_PORT = server.server_address[1]
    thread.start()
    log('HTTP', f'Static file server on http://127.0.0.1:{port}')
    return server


# ---------------------------------------------------------------------------
# Native Windows file/directory dialogs via COM IFileOpenDialog (ctypes)
# ---------------------------------------------------------------------------
class _FileDialog:
    """原生 Windows 文件对话框。

    通过 ctypes 直接调用 COM IFileOpenDialog，不依赖 tkinter 子进程。
    IFileDialog::Show 自带模态消息泵，可从任意工作线程调用 —— 因此
    源码运行与冻结版 exe 均可用（冻结版无法再启动 python 子进程）。
    """

    _SIGDN_FILESYSPATH = 0x80058000
    _FOS_PICKFOLDERS = 0x00000020
    _FOS_FORCEFILESYSTEM = 0x00000040
    _FOS_ALLOWMULTISELECT = 0x00000200
    _CLSCTX_INPROC_SERVER = 0x1
    _COINIT_APARTMENTTHREADED = 0x2
    _CANCELED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)

    class _GUID(ctypes.Structure):
        _fields_ = [
            ('Data1', wintypes.DWORD),
            ('Data2', wintypes.WORD),
            ('Data3', wintypes.WORD),
            ('Data4', ctypes.c_ubyte * 8),
        ]

    class _I(ctypes.Structure):
        _fields_ = [('lpVtbl', ctypes.POINTER(ctypes.c_void_p))]

    CLSID_FileOpenDialog = _GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, (0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
    CLSID_FileSaveDialog = _GUID(0x84BCCD23, 0x5FDE, 0x4CDB, (0xAE, 0xA4, 0xAF, 0x64, 0xB8, 0x3D, 0x78, 0xAB))
    # 注意：FileOpenDialog 类对象对 IFileOpenDialog 的 QueryInterface 会失败，
    # 需用父接口 IFileDialog（含 Show/SetOptions/SetFolder/SetTitle/GetResult）。
    IID_IFileDialog = _GUID(0x42F85136, 0xDB7E, 0x439C, (0x85, 0xF1, 0xE4, 0x07, 0x5D, 0x13, 0x5F, 0xC8))
    IID_IFileOpenDialog = _GUID(0xD57C7288, 0xD4AD, 0x4768, (0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
    IID_IShellItem = _GUID(0x43826D1E, 0xE718, 0x42EE, (0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE))

    # 记住上次打开的目录，作为下次 initialdir
    _last_dir: str = ''

    @staticmethod
    def _fn(iface, idx, restype, *argtypes):
        """取 vtable 中第 idx 个方法（首参固定为 this 指针）。"""
        return ctypes.cast(iface.lpVtbl[idx], ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes))

    @staticmethod
    def _release(iface):
        try:
            _FileDialog._fn(iface, 2, ctypes.c_ulong)(ctypes.addressof(iface))
        except Exception:
            pass

    @staticmethod
    def _free_str(p):
        try:
            ctypes.OleDLL('ole32').CoTaskMemFree(ctypes.cast(p, ctypes.c_void_p))
        except Exception:
            pass

    @staticmethod
    def pick(op: str, multi: bool = False, title: str | None = None, default_name: str = '') -> dict:
        """op: 'file'、'directory' 或 'save'。文件选择可返回多条路径。"""
        # 环境变量可强制走 PowerShell 兜底（部分机器 COM 对话框渲染异常时用）
        if os.environ.get('ADB_DIALOG_PS') == '1':
            log('PICK', 'ADB_DIALOG_PS=1, using PowerShell dialog')
            return _FileDialog._ps_pick(op, multi, title, default_name)
        result = _FileDialog._com_pick(op, multi, title, default_name)
        if result.get('success') or result.get('path'):
            return result
        # COM 失败（返回带 'output' 键的错误结果）→ PowerShell 兜底；
        # 用户取消返回 {success:False, path:''} 无 'output'，直接返回。
        if 'output' in result:
            log('PICK', 'COM dialog failed, falling back to PowerShell')
            return _FileDialog._ps_pick(op, multi, title, default_name)
        return result

    @staticmethod
    def _ps_pick(op: str, multi: bool = False, title: str | None = None, default_name: str = '') -> dict:
        """PowerShell WinForms 对话框兜底（跨 Windows 版本最稳）。"""
        init = (_FileDialog._last_dir or '').replace("'", "''")
        enc = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        if op == 'directory':
            dialog_title = (title or '选择本地保存目录').replace("'", "''")
            script = (
                enc +
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$d.Description = '{dialog_title}'; "
                "$d.ShowNewFolderButton = $true; "
                f"$d.SelectedPath = '{init}'; "
                "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }"
            )
        elif op == 'save':
            dialog_title = (title or '选择或新建终端记录文件').replace("'", "''")
            name = default_name.replace("'", "''")
            script = (
                enc +
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.SaveFileDialog; "
                f"$d.Title = '{dialog_title}'; "
                f"$d.FileName = '{name}'; "
                "$d.Filter = '日志文件 (*.log)|*.log|所有文件 (*.*)|*.*'; "
                "$d.OverwritePrompt = $false; "
                f"$d.InitialDirectory = '{init}'; "
                "$r = $d.ShowDialog(); "
                "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileName }"
            )
        else:
            script = (
                enc +
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$d.Title = '{(title or ('选择要上传的文件（可多选）' if multi else '选择要上传的文件')).replace(chr(39), chr(39) * 2)}'; "
                f"$d.Multiselect = ${str(multi).lower()}; "
                f"$d.InitialDirectory = '{init}'; "
                "$r = $d.ShowDialog(); "
                "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileNames }"
            )
        try:
            proc = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', script],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
                creationflags=CREATE_NO_WINDOW,
            )
            paths = [line.strip() for line in (proc.stdout or '').splitlines() if line.strip()]
            if paths:
                _FileDialog._last_dir = os.path.dirname(paths[0])
                return {'success': True, 'path': paths[0], 'paths': paths}
            if proc.returncode != 0 and proc.stderr:
                return {'success': False, 'path': '', 'output': proc.stderr.strip()}
            return {'success': False, 'path': ''}
        except Exception as e:
            log('PICK', f'PowerShell dialog error: {type(e).__name__}: {e}')
            return {'success': False, 'path': '', 'output': str(e)}

    @staticmethod
    def _com_pick(op: str, multi: bool = False, title: str | None = None, default_name: str = '') -> dict:
        """op: 'file'、'directory' 或 'save'。阻塞直到对话框关闭（COM IFileDialog）。"""
        ole32 = ctypes.OleDLL('ole32')
        shell32 = ctypes.OleDLL('shell32')
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

        self = _FileDialog
        coinit = ole32.CoInitializeEx(None, self._COINIT_APARTMENTTHREADED)
        if coinit not in (0, 1):
            return {'success': False, 'path': '', 'output': f'COM 初始化失败: {coinit:#x}'}

        try:
            # --- 创建 IFileOpenDialog 或 IFileSaveDialog（均用父接口 IFileDialog 查询） ---
            pdlg = ctypes.POINTER(self._I)()
            clsid = self.CLSID_FileSaveDialog if op == 'save' else self.CLSID_FileOpenDialog
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, self._CLSCTX_INPROC_SERVER,
                ctypes.byref(self.IID_IFileDialog), ctypes.byref(pdlg),
            )
            if hr < 0 or not pdlg:
                return {'success': False, 'path': '', 'output': f'创建对话框失败: {ctypes.c_long(hr).value:#x}'}
            dlg = pdlg.contents

            # --- 选项 ---
            opts = self._FOS_FORCEFILESYSTEM
            if op == 'directory':
                opts |= self._FOS_PICKFOLDERS
            elif multi:
                opts |= self._FOS_ALLOWMULTISELECT
            self._fn(dlg, 9, ctypes.c_long, ctypes.c_ulong)(ctypes.addressof(dlg), opts)

            if op == 'save' and default_name:
                self._fn(dlg, 15, ctypes.c_long, ctypes.c_wchar_p)(
                    ctypes.addressof(dlg), default_name
                )

            # --- 标题 ---
            dialog_title = title or ('选择本地保存目录' if op == 'directory' else ('选择要上传的文件（可多选）' if multi else '选择要上传的文件'))
            self._fn(dlg, 17, ctypes.c_long, ctypes.c_wchar_p)(ctypes.addressof(dlg), dialog_title)

            # --- 初始目录（记住上次位置，需存在才设置） ---
            if self._last_dir and os.path.isdir(self._last_dir):
                pitem = ctypes.POINTER(self._I)()
                h = shell32.SHCreateItemFromParsingName(
                    self._last_dir, None, ctypes.byref(self.IID_IShellItem), ctypes.byref(pitem)
                )
                if h >= 0 and pitem:
                    self._fn(dlg, 12, ctypes.c_long, ctypes.POINTER(self._I))(
                        ctypes.addressof(dlg), pitem
                    )
                    self._release(pitem.contents)

            # --- 显示（模态） ---
            # 对话框在 asyncio 的工作线程中创建。未指定 owner 时，Windows 会把
            # 它作为无主窗口放到后台，用户看起来就像点击无反应。绑定当前前台
            # 的 pywebview 窗口，保证对话框置顶并正确保持模态关系。
            owner = None
            try:
                user32 = ctypes.WinDLL('user32', use_last_error=True)
                user32.FindWindowW.restype = wintypes.HWND
                user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
                user32.GetForegroundWindow.restype = wintypes.HWND
                # Show 在 executor 线程中运行时，GetForegroundWindow 有时会返回
                # 空句柄；pywebview 的顶层窗口标题固定为 DevMux，所以先按
                # 标题查找，找不到再退回当前前台窗口。
                owner = user32.FindWindowW(None, APP_TITLE) or user32.GetForegroundWindow()
            except Exception as e:
                log('PICK', f'cannot get dialog owner: {e}')
            log('PICK', f'opening {op} dialog (owner={owner!r})')
            hr = self._fn(dlg, 3, ctypes.c_long, wintypes.HWND)(ctypes.addressof(dlg), owner)
            if ctypes.c_ulong(hr & 0xFFFFFFFF).value == self._CANCELED or hr < 0:
                return {'success': False, 'path': ''}

            # --- 取结果 ---
            paths = []
            if multi and op == 'file':
                # GetResults 是 IFileOpenDialog 的扩展方法，不能通过父接口
                # IFileDialog 的 vtable 调用；先显式 QueryInterface 再读取数组。
                popen = ctypes.POINTER(self._I)()
                hr = self._fn(
                    dlg, 0, ctypes.c_long, ctypes.POINTER(self._GUID),
                    ctypes.POINTER(ctypes.POINTER(self._I)),
                )(ctypes.addressof(dlg), ctypes.byref(self.IID_IFileOpenDialog), ctypes.byref(popen))
                if hr < 0 or not popen:
                    return {'success': False, 'path': '', 'output': f'获取多选对话框接口失败: {ctypes.c_long(hr).value:#x}'}
                open_dlg = popen.contents
                try:
                    pitems = ctypes.POINTER(self._I)()
                    hr = self._fn(open_dlg, 27, ctypes.c_long, ctypes.POINTER(ctypes.POINTER(self._I)))(
                        ctypes.addressof(open_dlg), ctypes.byref(pitems)
                    )
                    if hr < 0 or not pitems:
                        return {'success': False, 'path': '', 'output': f'读取多选结果失败: {ctypes.c_long(hr).value:#x}'}
                    items = pitems.contents
                    try:
                        count = ctypes.c_uint()
                        hr = self._fn(items, 7, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(
                            ctypes.addressof(items), ctypes.byref(count)
                        )
                        if hr < 0:
                            return {'success': False, 'path': '', 'output': f'读取多选数量失败: {ctypes.c_long(hr).value:#x}'}
                        for index in range(count.value):
                            pitem = ctypes.POINTER(self._I)()
                            hr = self._fn(items, 8, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.POINTER(self._I)))(
                                ctypes.addressof(items), index, ctypes.byref(pitem)
                            )
                            if hr < 0 or not pitem:
                                continue
                            item = pitem.contents
                            pname = ctypes.c_wchar_p()
                            hr2 = self._fn(item, 5, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_wchar_p))(
                                ctypes.addressof(item), self._SIGDN_FILESYSPATH, ctypes.byref(pname)
                            )
                            if hr2 >= 0 and pname.value:
                                paths.append(pname.value)
                            self._free_str(pname)
                            self._release(item)
                    finally:
                        self._release(items)
                finally:
                    self._release(open_dlg)
            else:
                pres = ctypes.POINTER(self._I)()
                hr = self._fn(dlg, 20, ctypes.c_long, ctypes.POINTER(ctypes.POINTER(self._I)))(
                    ctypes.addressof(dlg), ctypes.byref(pres)
                )
                if hr >= 0 and pres:
                    item = pres.contents
                    pname = ctypes.c_wchar_p()
                    hr2 = self._fn(item, 5, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_wchar_p))(
                        ctypes.addressof(item), self._SIGDN_FILESYSPATH, ctypes.byref(pname)
                    )
                    if hr2 >= 0 and pname.value:
                        paths.append(pname.value)
                    self._free_str(pname)
                    self._release(item)
            if paths:
                import os as _os
                self._last_dir = _os.path.dirname(paths[0])
                log('PICK', f'{op} selected: {len(paths)} item(s)')
                return {'success': True, 'path': paths[0], 'paths': paths}
            if multi and op == 'file':
                return {'success': False, 'path': '', 'output': '未读取到已选择的文件路径'}
            log('PICK', f'{op} dialog closed without selection')
            return {'success': False, 'path': ''}
        except Exception as e:
            log('PICK', f'dialog error: {type(e).__name__}: {e}')
            return {'success': False, 'path': '', 'output': str(e)}
        finally:
            try:
                self._release(dlg)
            except Exception:
                pass
            if coinit == 0:
                ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# WebSocket + ADB Worker coordinator
# ---------------------------------------------------------------------------
class ManagerServer:
    def __init__(self):
        self.clients: Set[WebSocketServerProtocol] = set()
        from mijia_service import MijiaService
        self.mijia = MijiaService()
        from desktop_terminals import DesktopTerminals
        self.desktop_terminals = DesktopTerminals(self,lambda:_HTTP_PORT)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self.worker = self._new_worker()
        # 所有阻塞 ADB/文件操作共用固定大小线程池。默认 executor 会按负载扩张并
        # 常驻到进程退出，长期使用时容易被误认为线程泄漏。
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=EXECUTOR_MAX_WORKERS, thread_name_prefix='DevMuxIO'
        )
        # 远端输入与尺寸调整各自串行，消息处理线程仅入队；不等待 SMB。
        self._remote_io_pending = {'input': {}, 'resize': {}}
        self._remote_io_tasks = {'input': {}, 'resize': {}}
        self._remote_last_size = {}
        self._broadcast_lock = threading.Lock()
        self._broadcast_queue = deque()
        self._pending_devices_payload: Optional[dict] = None
        self._broadcast_scheduled = False
        self._broadcast_future = None
        self._dropped_broadcasts = 0
        # key -> {path, timestamps, at_line_start}
        self._recording_sessions: dict[str, dict] = {}
        self._recording_lock = threading.Lock()
        from serial_recording import SerialRecordings
        self.serial_recordings = SerialRecordings(strip_ansi, add_recording_timestamps)
        self._edit_sessions: dict[str, dict] = {}
        self._edit_lock = threading.Lock()
        self._inline_sessions: dict[str, dict] = {}
        self._inline_lock = threading.Lock()
        self._output_activity: dict[str, float] = {}  # key -> last output monotonic time
        self._output_lock = threading.Lock()
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_session: Optional[dict] = None
        # 跨机中转：控制器自己起线程管理节点与远端会话，回调都在它的线程上。
        # 多实例隔离：本实例若是第二个窗口，_relay_config_path 会让它用独立持久
        # 配置，并把说明带到界面上（status.configNotice）。
        self.relay = RelayController(
            config_path=self._relay_config_path(),
            config_notice=self._relay_config_notice,
            adb_path=ADB_PATH,
            session_backend=SharedTerminalBackend(lambda: self.worker),
            on_devices_changed=self._on_relay_devices_changed,
            on_status=self._on_relay_status,
            on_terminal_output=self._on_terminal_output,
            on_terminal_exit=self._on_relay_terminal_exit,
            on_event=self._on_relay_event,
            on_peer_request=self._on_relay_peer_request,
            log=log,
            local_devices_enabled=LOCAL_DEVICES_ENABLED,
        )
        # 干净退出时归还默认配置的占用锁（只删自己写的那把，见 release_config_claim）。
        if not os.environ.get('RELAY_CONFIG'):
            atexit.register(relay_app.release_config_claim, self.relay.config_path)
        self._relay_lock = threading.Lock()
        self._agent_token = secrets.token_urlsafe(32)
        self._agent_instance = secrets.token_hex(16)
        self._agent_runtime = None
        import hashlib
        storage_id = hashlib.sha256(os.path.abspath(self.relay.config_path).encode('utf-8')).hexdigest()[:16]
        self.windows = WindowsService(Path(self.relay.config_path).parent / ('windows-jobs-'+storage_id))
        self.usb_relay = USBRelayService(Path(self.relay.config_path).with_name(
            Path(self.relay.config_path).stem + '-usb-relay.json'), log=log,
            config_storage=self._tool_config_storage('usb-relay'))
        from extension_tools import ExtensionTools
        self.db_tool = DBFlashExtension(lambda: self.db_flash, Path(self.relay.config_path).with_name(
            Path(self.relay.config_path).stem + '-db-flash.json'),
            config_storage=self._tool_config_storage('db-flash'))
        from serial_terminal import SerialService
        self.serial = SerialService(Path(self.relay.config_path).with_name(
            Path(self.relay.config_path).stem + '-serial.json'),
            config_storage=self._tool_config_storage('serial'))
        self.extension_tools = ExtensionTools({'relay': self.usb_relay, 'db-flash': self.db_tool,
                                               'serial': self.serial})
        self.relay.extension_tools = self.extension_tools
        self.relay.windows_service = self.windows
        self.agent = AgentAPI(self)
        # 固化工具真实执行；原 DB 包保持只读，测试宿主显式注入假执行器。
        self.db_flash = DBFlashService(RealExecutor(), progress=self._broadcast_async)
        # 待确认对端的最近一次首连确认负载。中转在界面连上来之前就可能已经发现
        # 对端，那次推送没有人听；新客户端接入时按这里补推一次，否则用户只能靠
        # 状态里兜底提示，看不到「同意/拒绝」的说明与指纹。
        self._relay_pending_requests: dict = {}

    def _remember_relay_request(self, peer_id: str, request: Optional[dict]):
        """记住/清除某个对端的首连确认负载（线程安全）。"""
        if not peer_id:
            return
        with self._relay_lock:
            if request is None:
                self._relay_pending_requests.pop(peer_id, None)
            else:
                self._relay_pending_requests[peer_id] = request

    def _pending_relay_requests(self, pending_ids) -> list:
        """按当前仍在等的对端过滤出可补推的确认负载（已确认的不再推）。"""
        allowed = set(pending_ids or ())
        with self._relay_lock:
            return [dict(v) for k, v in self._relay_pending_requests.items() if k in allowed]

    def _new_worker(self):
        return ADBWorker(
            on_devices_changed=self._on_devices_changed,
            on_terminal_output=self._on_terminal_output,
            on_terminal_exit=self._on_terminal_exit,
            on_terminal_control=self._on_terminal_control,
            local_devices_enabled=LOCAL_DEVICES_ENABLED,
        )

    # --- ADBWorker callbacks (called from worker thread) ---
    def _on_devices_changed(self, devices, keys):
        self._broadcast_devices()

    # --- 跨机中转回调（在控制器线程上）---
    def _on_relay_devices_changed(self, devices, keys):
        """远端设备集合变化 → 重新合并并广播设备列表。"""
        self._broadcast_devices()

    def _on_relay_status(self, status: dict):
        payload = dict(status)
        payload['type'] = 'relay-status'
        self._broadcast_async(payload)

    def _on_relay_event(self, event: dict):
        # 中转异常必须落入日志，便于没有诊断界面的远端机器排障。
        log('RELAY', str(event.get('text') or ''))
        self._broadcast_async({'type': 'relay-event', **event})

    def _on_relay_peer_request(self, request: dict):
        """新对端待确认：推给界面弹首连确认框（含主/从身份提示）。"""
        self._remember_relay_request(str(request.get('peerId') or ''), request)
        self._broadcast_async({'type': 'relay-peer-request', **request})

    def _on_relay_terminal_exit(self, key: str, reason: str):
        agent = getattr(self, 'agent', None)
        if agent:
            agent.ended(key)
        if reason == '终端控制权切换':
            self._broadcast_async({'type': 'terminal-switch', 'key': key, 'reason': reason})
            return
        # 远端会话结束时与本地终端保持同样的收尾：停录制、清编辑会话、通知前端。
        with self._output_lock:
            self._output_activity.pop(key, None)
        self._stop_recording(key)
        self._cleanup_device_edit(key)
        self._close_inline_edit(key)
        self._broadcast_async({'type': 'terminal-exit', 'key': key, 'reason': reason})

    # --- 设备列表合并（本机 + 远端）---
    def _merged_devices(self):
        """本机设备与远端设备合成一份列表。

        远端设备的 key 已带 ``relay:<peerId>:`` 前缀，且携带 ``relay`` 元信息
        （在线、掉线时长），前端据此灰显并门禁操作。本机设备也补上 ``key`` 字段，
        让两侧用同一套取值规则，避免前端再自己拼 key 拼错。
        """
        devices, _keys = self.worker.get_state()
        merged: list[dict] = []
        seen: set = set()
        for device in devices:
            item = dict(device)
            item['key'] = str(item.get('key') or make_key(item))
            item['origin'] = 'local'
            merged.append(item)
            seen.add(item['key'])
        try:
            remote = self.relay.devices()
        except Exception as e:
            log('RELAY', f'读取远端设备失败：{type(e).__name__}: {e}')
            remote = []
        for device in remote:
            key = str(device.get('key') or '')
            if not key or key in seen:
                continue
            merged.append(device)
            seen.add(key)
        return merged

    def _broadcast_devices(self):
        merged = self._merged_devices()
        self._broadcast_async({
            'type': 'devices-updated',
            'devices': merged,
            'knownKeys': [device['key'] for device in merged],
        })

    def _on_terminal_control(self, state):
        self._broadcast_async({'type': 'terminal-control-status', **state})
        executor = getattr(self, '_executor', None)
        if executor:
            executor.submit(self._refresh_control_meta, state['key'])

    def _refresh_control_meta(self, key):
        node = getattr(getattr(self, 'relay', None), 'node', None)
        sessions = getattr(node, '_sessions', None)
        if sessions:
            sessions.refresh_control_meta(key)

    def _on_terminal_output(self, key: str, data: bytes):
        agent = getattr(self, 'agent', None)
        if agent:
            agent.publish(key, data)
        # Record output activity timestamp (used by paced multi-line writer)
        with self._output_lock:
            self._output_activity[key] = time.monotonic()
        # Encode binary terminal data as base64 to keep JSON safe
        import base64
        payload = {
            'type': 'terminal-data',
            'key': key,
            'data': base64.b64encode(data).decode('ascii'),
        }
        self._broadcast_async(payload)
        # If recording is active for this session, append to file (plain text)
        with self._recording_lock:
            recording = self._recording_sessions.get(key)
        if recording:
            try:
                text = data.decode('utf-8', errors='replace')
                plain = strip_ansi(text)
                if plain:
                    if recording['timestamps']:
                        plain, at_line_start = add_recording_timestamps(
                            plain, recording['at_line_start']
                        )
                        with self._recording_lock:
                            # 记录可能已停止或被新会话替换，不能更新过期状态。
                            if self._recording_sessions.get(key) is recording:
                                recording['at_line_start'] = at_line_start
                    with open(recording['path'], 'a', encoding='utf-8') as f:
                        f.write(plain)
            except Exception:
                pass

    def _on_terminal_exit(self, key: str):
        agent = getattr(self, 'agent', None)
        if agent:
            agent.ended(key)
        with self._output_lock:
            self._output_activity.pop(key, None)
        self._stop_recording(key)
        self._cleanup_device_edit(key)
        self._close_inline_edit(key)
        payload = {
            'type': 'terminal-exit',
            'key': key,
        }
        self._broadcast_async(payload)

    def _broadcast_async(self, payload: dict):
        """线程安全地投入有界广播队列，避免慢客户端堆积协程。"""
        if self.loop is None or self.loop.is_closed():
            return
        should_schedule = False
        with self._broadcast_lock:
            # 设备刷新只需要最新快照；每两秒一次的旧快照没有保留价值。
            if payload.get('type') == 'devices-updated':
                self._pending_devices_payload = payload
            elif len(self._broadcast_queue) < BROADCAST_QUEUE_LIMIT:
                self._broadcast_queue.append(payload)
            else:
                self._dropped_broadcasts += 1
            if not self._broadcast_scheduled:
                self._broadcast_scheduled = True
                should_schedule = True
        if not should_schedule:
            return
        try:
            self._broadcast_future = asyncio.run_coroutine_threadsafe(
                self._drain_broadcasts(), self.loop
            )
        except Exception as e:
            with self._broadcast_lock:
                self._broadcast_scheduled = False
            log('WS', f'broadcast schedule failed: {e}')

    async def _drain_broadcasts(self):
        """每次最多一个广播协程；慢客户端不会导致待发送任务无限增长。"""
        try:
            while True:
                with self._broadcast_lock:
                    if self._broadcast_queue:
                        payload = self._broadcast_queue.popleft()
                    elif self._pending_devices_payload is not None:
                        payload = self._pending_devices_payload
                        self._pending_devices_payload = None
                    else:
                        dropped = self._dropped_broadcasts
                        self._dropped_broadcasts = 0
                        self._broadcast_scheduled = False
                        self._broadcast_future = None
                        break
                await self._broadcast(payload)
            if dropped:
                log('WS', f'慢客户端期间丢弃 {dropped} 条待广播消息（队列上限 {BROADCAST_QUEUE_LIMIT}）')
        except asyncio.CancelledError:
            raise
        finally:
            with self._broadcast_lock:
                self._broadcast_scheduled = False
                self._broadcast_future = None

    def _cancel_pending_broadcasts(self):
        with self._broadcast_lock:
            self._broadcast_queue.clear()
            self._pending_devices_payload = None
            future = self._broadcast_future
            self._broadcast_future = None
            self._broadcast_scheduled = False
            self._dropped_broadcasts = 0
        if future:
            future.cancel()

    async def _broadcast(self, payload: dict):
        if not self.clients:
            return
        msg = json.dumps(payload)
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    # --- 终端输入/尺寸选路（本机 PTY 或远端会话）---
    def _is_remote_key(self, key: str) -> bool:
        try:
            return bool(key) and self.relay.is_remote_key(key)
        except Exception:
            return False

    def _write_terminal(self, key: str, data: bytes, *, generation=None):
        """按设备 key 选路写入。

        远端设备的按键走数据面流文件（``streams/<从机>/<会话>.in``），不走控制面：
        高频输入若每条一个请求文件，会在共享目录上产生海量小文件。
        """
        if self._is_remote_key(key):
            self.relay.session_input(key, data)
        else:
            if generation is None:
                self.worker.write_terminal(key, data)
            else:
                self.worker.write_terminal(key, data, generation=generation)

    def _resize_terminal(self, key: str, cols: int, rows: int, *, generation=None):
        if self._is_remote_key(key):
            self.relay.session_resize(key, cols, rows)
        else:
            if generation is None:
                self.worker.resize_terminal(key, cols, rows)
            else:
                self.worker.resize_terminal(key, cols, rows, generation=generation)

    async def _write_terminal_async(self, key: str, data: bytes, *, generation=None):
        if self._is_remote_key(key):
            await self._run_blocking(self._write_terminal, key, data)
        else:
            if generation is None:
                self._write_terminal(key, data)
            else:
                self._write_terminal(key, data, generation=generation)

    def _queue_remote_io(self, key: str, payload, *, resize: bool = False):
        """按会话代次入队；输入合并小包，尺寸只保留最新值。"""
        kind = 'resize' if resize else 'input'
        if not resize and not getattr(self.relay, 'session_input_allowed', lambda _key: True)(key):
            return
        generation = self.relay.session_generation(key)
        if generation is None:
            return
        pending = self._remote_io_pending[kind]
        if resize:
            pending[key] = (generation, payload)
        else:
            pending.setdefault(key, []).append((generation, payload))
        tasks = self._remote_io_tasks[kind]
        if key not in tasks or tasks[key].done():
            tasks[key] = asyncio.create_task(self._drain_remote_io(kind, key))

    async def _wait_remote_ready(self, key, generation):
        # 终端初始化期间先保留按键，失败或换代立即丢弃，不能发到另一条会话。
        waiting_started = time.monotonic()
        deadline = waiting_started + 30.0
        ready = getattr(self.relay, 'session_input_ready', lambda _key: True)
        while generation == self.relay.session_generation(key):
            if ready(key):
                waited = time.monotonic() - waiting_started
                if waited >= 0.5:
                    log('RELAY', f'远端输入等待终端就绪 {waited * 1000:.0f}ms（{key}）')
                return True
            if time.monotonic() >= deadline:
                log('RELAY', f'等待远端终端就绪超时（{key}）')
                return False
            await asyncio.sleep(0.05)
        return False

    async def _drain_remote_io(self, kind: str, key: str):
        pending = self._remote_io_pending[kind]
        try:
            await asyncio.sleep(0.06 if kind == 'resize' else 0.015)
            while key in pending:
                batch = pending.pop(key)
                if kind == 'resize':
                    generation, size = batch
                    token = (generation, size)
                    if (await self._wait_remote_ready(key, generation)
                            and self._remote_last_size.get(key) != token):
                        await self._run_blocking(self._resize_terminal, key, *size)
                        self._remote_last_size[key] = token
                    continue
                # 同一批按会话代次分组，不能把旧会话排队的按键送入新终端。
                groups = []
                for generation, data in batch:
                    if groups and groups[-1][0] == generation:
                        groups[-1][1].append(data)
                    else:
                        groups.append((generation, [data]))
                for generation, parts in groups:
                    if not await self._wait_remote_ready(key, generation):
                        continue
                    # 是否粘贴按原始消息判定，不能把回车和后续按键合并后误限速。
                    if all(len(_iter_line_segments(part)) <= 1 for part in parts):
                        await self._write_terminal_async(key, ''.join(parts).encode('utf-8', errors='replace'))
                        continue
                    for part in parts:
                        segs = _iter_line_segments(part)
                        for index, seg in enumerate(segs):
                            if generation != self.relay.session_generation(key):
                                break
                            data = seg.encode('utf-8', errors='replace')
                            if index < len(segs) - 1:
                                await self._send_one_line_paced(key, data)
                            else:
                                await self._write_terminal_async(key, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log('RELAY', f'远端{kind}处理失败（{key}）：{type(exc).__name__}: {exc}')
        finally:
            self._remote_io_tasks[kind].pop(key, None)

    def _cancel_remote_io(self):
        tasks = [task for mapping in getattr(self, '_remote_io_tasks', {}).values()
                 for task in mapping.values()]
        for task in tasks:
            task.cancel()
        for mapping in getattr(self, '_remote_io_pending', {}).values():
            mapping.clear()
        for mapping in getattr(self, '_remote_io_tasks', {}).values():
            mapping.clear()
        getattr(self, '_remote_last_size', {}).clear()
        return tasks

    async def _send_one_line_paced(self, key: str, seg_bytes: bytes, *, generation=None):
        """发送一行并等待设备回显稳定后再返回（多行粘贴限速）。

        高延迟设备（如 argos 板）一次性灌入多行时，tty 逐字回显会与 shell
        提示符交错导致画面重叠乱码。逐行发送并等待“有新输出 → 输出静默”
        可保持每行回显干净。仅多行 payload 走此路径；单行/普通打字不受影响。
        """
        if os.environ.get('ADB_PASTE_GATE') == '0':
            await self._write_terminal_async(key, seg_bytes, generation=generation)
            return
        settle_s = int(os.environ.get('ADB_PASTE_QUIET_MS', '200')) / 1000.0
        first_timeout_s = int(os.environ.get('ADB_PASTE_FIRST_TIMEOUT_MS', '3000')) / 1000.0
        settle_timeout_s = int(os.environ.get('ADB_PASTE_SETTLE_TIMEOUT_MS', '5000')) / 1000.0

        # 记录本次写入前的活动基线
        with self._output_lock:
            before = self._output_activity.get(key, 0)

        await self._write_terminal_async(key, seg_bytes, generation=generation)

        # 阶段 1：等待本行产生新输出（回显到达），超时兜底
        deadline = time.monotonic() + first_timeout_s
        while time.monotonic() < deadline:
            with self._output_lock:
                last = self._output_activity.get(key, 0)
            if last > before:
                break
            await asyncio.sleep(0.02)

        # 阶段 2：等待输出静默（回显与提示符都落定）
        deadline = time.monotonic() + settle_timeout_s
        while time.monotonic() < deadline:
            with self._output_lock:
                last = self._output_activity.get(key, 0)
            if time.monotonic() - last >= settle_s:
                return
            await asyncio.sleep(0.02)

    async def _send(self, ws: WebSocketServerProtocol, payload: dict):
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            pass

    async def _run_blocking(self, func, *args, **kwargs):
        """在固定大小的应用线程池中运行阻塞操作。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(func, *args, **kwargs))

    def _start_adb_diagnostic(self, ws: WebSocketServerProtocol, request_id, selected_device: Optional[dict]):
        """启动单个后台诊断任务；不能在 handler 中等待，以便同客户端可以取消。"""
        with self._diagnostic_lock:
            if self._diagnostic_session is not None:
                return False
            cancel_event = threading.Event()
            self._diagnostic_session = {'id': request_id, 'cancelEvent': cancel_event}
        asyncio.create_task(self._execute_adb_diagnostic(ws, request_id, selected_device, cancel_event))
        return True

    def _cancel_adb_diagnostic(self, request_id) -> bool:
        with self._diagnostic_lock:
            session = self._diagnostic_session
            if not session or session.get('id') != request_id:
                return False
            session['cancelEvent'].set()
            return True

    async def _execute_adb_diagnostic(self, ws: WebSocketServerProtocol, request_id, selected_device: Optional[dict], cancel_event: threading.Event):
        """在线程池执行只读探针，并把进度安全地转回 WebSocket loop。"""
        def progress(current: int, total: int, check_id: str, title: str):
            self._broadcast_async({
                'type': 'diagnostic-progress', 'id': request_id,
                'current': current, 'total': total, 'checkId': check_id, 'title': title,
            })

        runner = ADBDiagnosticRunner(ADB_PATH, CREATE_NO_WINDOW, log, progress)
        try:
            report = await self._run_blocking(runner.run, selected_device, cancel_event)
            await self._send(ws, {
                'type': 'diagnostic-result', 'id': request_id,
                'success': True, 'report': report.to_dict(),
            })
        except Exception as e:
            log('DIAG', f'诊断执行失败: {type(e).__name__}: {e}')
            await self._send(ws, {
                'type': 'diagnostic-result', 'id': request_id,
                'success': False, 'output': f'诊断执行失败：{type(e).__name__}: {e}',
            })
        finally:
            with self._diagnostic_lock:
                if self._diagnostic_session and self._diagnostic_session.get('id') == request_id:
                    self._diagnostic_session = None

    # --- WebSocket handler ---
    async def ws_handler(self, ws: WebSocketServerProtocol):
        if getattr(getattr(ws, 'request', None), 'path', '') == '/desktop-terminal':
            await self.desktop_terminals.child_handler(ws)
            return
        if getattr(getattr(ws, 'request', None), 'path', '') == '/agent':
            await self._agent_handler(ws)
            return
        self.clients.add(ws)
        log('WS', f'client connected {ws.remote_address}')

        # Send current state immediately
        merged = self._merged_devices()
        await self._send(ws, {
            'type': 'devices-updated',
            'devices': merged,
            'knownKeys': [device['key'] for device in merged],
        })
        hub = getattr(self.worker, 'terminal_hub', None)
        if hub:
            for state in hub.states():
                await self._send(ws, {'type': 'terminal-control-status', **state})
        agent = getattr(self, 'agent', None)
        if agent:
            for record in list(agent.sessions.values()):
                if record.get('key') and record['state'] == 'running':
                    await self._send(ws, {'type': 'agent-control', 'key': record['key'], 'active': True})
        # 中转状态（角色、连接状态、待确认对端）随首帧一起给，避免界面先空一下。
        try:
            status = await self._run_blocking(self.relay.status)
            await self._send(ws, {'type': 'relay-status', **status})
            # 补推仍在等待确认的对端。中转先于界面启动是常态，首次推送会落空。
            for request in self._pending_relay_requests(status.get('pendingConfirmation')):
                await self._send(ws, {'type': 'relay-peer-request', **request})
        except Exception as e:
            log('RELAY', f'发送初始中转状态失败：{type(e).__name__}: {e}')

        computer_reads = set()
        cache_tasks = set()
        def cache_finished(task):
            cache_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error:
                    log('RELAY', f'历史缓存清理后台请求失败：{type(error).__name__}: {error}')
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # 远端读取等待共享目录回执时，键盘输入必须继续处理。
                # 写入/关闭/权限变更仍按接收顺序执行，不并发打乱输入。
                if msg.get('type') == 'relay-clear-cache':
                    # 清理等待SMB时继续接收同窗口终端输入，每个连接只允许运行一个。
                    if cache_tasks:
                        await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'),
                                              'success': False, 'output': '历史缓存清理仍在进行，请勿重复执行'})
                    else:
                        task = asyncio.create_task(self._handle_message(ws, msg))
                        cache_tasks.add(task)
                        task.add_done_callback(cache_finished)
                elif (msg.get('type') == 'computer-tool' and msg.get('name') == 'session_read'
                        and len(computer_reads) < 16):
                    task = asyncio.create_task(self._handle_message(ws, msg))
                    computer_reads.add(task)
                    task.add_done_callback(computer_reads.discard)
                else:
                    await self._handle_message(ws, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            pending = computer_reads | cache_tasks
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self.clients.discard(ws)
            # 所属界面结束时回收普通串口及传输接入，不能只清理启动过XMODEM的窗口。
            sessions = getattr(ws, '_serial_gui_sessions', set()) | getattr(ws, '_serial_transfer_sessions', set())
            for local, node_id, session_id in sessions:
                try:
                    if local:
                        await self._run_blocking(self.extension_tools.call, 'serial', 'close',
                            {'sessionId': session_id}, caller='gui')
                    else:
                        from extension_tools import remote_call
                        await self._run_blocking(remote_call, self.relay, node_id, 'serial', 'close',
                            {'sessionId': session_id}, caller='gui')
                except Exception as exc:
                    log('SERIAL', f'界面结束时关闭串口接入失败：{type(exc).__name__}: {exc}')
            await self._run_blocking(self.serial_recordings.close_owner, ws)
            mijia = getattr(self, 'mijia', None)
            if mijia is not None:
                mijia.cancel(ws)
            await self._run_blocking(self.desktop_terminals.close_all,ws)
            log('WS', f'client disconnected {ws.remote_address}')

    async def _agent_handler(self, ws):
        try:
            # 浏览器不能使用 stdio 客户端的令牌接口，避免跨站读取本地凭据。
            if ws.request.headers.get('Origin'):
                await ws.close(code=1008, reason='Agent 接口仅供本机客户端')
                return
            message = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if (message.get('type') != 'agent-auth' or not isinstance(message.get('token'), str)
                    or not secrets.compare_digest(message['token'], self._agent_token)):
                await ws.close(code=1008, reason='Agent 身份校验失败')
                return
            await self._send(ws, dict(type='agent-auth', success=True, instanceId=self._agent_instance))
            async for raw in ws:
                request = {}
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict) or request.get('type') != 'agent-call':
                        raise ValueError('Agent 请求格式无效')
                    data = await self.agent.call(request.get('name'), request.get('arguments', {}))
                    response = dict(type='agent-result', id=request.get('id'), success=True, data=data)
                except Exception as exc:
                    response = dict(type='agent-result', id=request.get('id') if isinstance(request, dict) else None,
                                    success=False, error=f'{type(exc).__name__}: {exc}')
                await self._send(ws, response)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed, ValueError, AttributeError):
            return

    async def _handle_message(self, ws, msg: dict):
        typ = msg.get('type')
        if isinstance(typ,str) and typ.startswith('desktop-terminal-'):
            try:
                data=await self.desktop_terminals.handle(ws,msg)
                result=dict(success=True,output='',data=data)
            except Exception as exc:
                result=dict(success=False,output=str(exc))
            if msg.get('id') is not None:
                await self._send(ws,dict(type='exec-result',id=msg['id'],**result))
            return
        if typ == 'mijia':
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(getattr(getattr(ws, 'request', None), 'headers', {}).get('Origin', ''))
                allowed = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port == _HTTP_PORT
                token = msg.get('pageToken')
                allowed = allowed and isinstance(token, str) and secrets.compare_digest(token, _MIJIA_PAGE_TOKEN)
                if not allowed: raise PermissionError('米家功能仅允许本机应用页面')
                args = msg.get('arguments', {})
                if not isinstance(args, dict): raise ValueError('请求参数无效')
                data = await self._run_blocking(self.mijia.call, ws, msg.get('action'), args)
                result = dict(success=True, data=data)
            except Exception:
                # 云端异常可能包含凭据或登录 URL，不向页面和日志输出。
                result = dict(success=False, output='米家操作失败，请检查登录、网络和设备状态')
            await self._send(ws, dict(type='exec-result', id=msg.get('id'), **result))
            return
        if typ == 'db-flash':
            # 本机烧写接口不接入扩展工具的跨机选路，也不允许远端目标参数。
            from urllib.parse import urlsplit
            origin = getattr(getattr(ws, 'request', None), 'headers', {}).get('Origin', '')
            try:
                parsed = urlsplit(origin)
                local_origin = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port == _HTTP_PORT
            except ValueError:
                local_origin = False
            if not local_origin:
                result = {'success': False, 'output': '固化烧写仅允许本机应用页面'}
            elif msg.get('target', 'computer:local') != 'computer:local':
                result = {'success': False, 'output': '当前固化烧写仅支持本机'}
            else:
                result = await self._run_blocking(
                    self.db_tool.call, msg.get('action', ''), msg.get('arguments', {}))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})
            return
        if typ in ('computer-tool', 'extension-tool', 'usb-relay', 'relay-clear-cache', 'serial-xmodem-save', 'get-serial-commands', 'save-serial-commands', 'serial-recording'):
            # 电脑及扩展工具仅接受本机页面，防止外部网站触发硬件动作。
            origin = getattr(getattr(ws, 'request', None), 'headers', {}).get('Origin', '')
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(origin)
                local_page = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port == _HTTP_PORT
            except ValueError:
                local_page = False
            if not local_page:
                await self._send(ws, dict(type='exec-result', id=msg.get('id'), success=False, output='电脑操作仅允许本机应用页面'))
                return
        if typ == 'serial-recording':
            try:
                target = self.agent.resolve(msg.get('target'), 'computer')
                identity = (target['local'], target.get('nodeId', ''), msg.get('sessionId', ''))
                if identity not in getattr(ws, '_serial_gui_sessions', set()):
                    raise PermissionError('串口日志接入不属于当前页面')
                if msg.get('action') == 'start':
                    result = await self._run_blocking(self.serial_recordings.start, ws, identity,
                        msg.get('path'), msg.get('cursor', 0), msg.get('format', 'text'),
                        msg.get('encoding', 'utf-8'), msg.get('timestamps', True))
                elif msg.get('action') == 'stop':
                    error = await self._run_blocking(self.serial_recordings.stop, ws, identity)
                    result = dict(success=not error, output=error or '串口日志已停止')
                else:
                    raise ValueError('串口日志动作无效')
            except Exception as exc:
                result = dict(success=False, output=str(exc))
            await self._send(ws, dict(type='exec-result', id=msg.get('id'), **result))
            return
        if typ == 'serial-xmodem-save':
            try:
                target = self.agent.resolve(msg.get('target'), 'computer')
                result = await self._run_blocking(self._save_serial_xmodem, target,
                    msg.get('sessionId'), msg.get('transferId'))
            except Exception as exc:
                result = dict(success=False, output=str(exc))
            await self._send(ws, dict(type='exec-result', id=msg.get('id'), **result))
            return
        if typ in ('computer-tool', 'extension-tool'):
            try:
                if typ == 'extension-tool':
                    tool, action = msg.get('tool'), msg.get('action')
                    if action == 'targets':
                        targets = [item for item in self.agent.targets() if item['kind'] == 'computer'
                                   and (item['local'] or item['role'] == 'slave')]
                        result = dict(success=True, data={'targets': targets})
                    else:
                        target = self.agent.resolve(msg.get('target'), 'computer')
                        arguments = msg.get('arguments', {})
                        if tool == 'serial' and action == 'xmodem-start':
                            transfers = getattr(ws, '_serial_transfer_sessions', set())
                            transfers.add((target['local'], target.get('nodeId', ''), arguments.get('sessionId', '')))
                            ws._serial_transfer_sessions = transfers
                        if target['local']:
                            result = await self._run_blocking(self.extension_tools.call, tool, action, arguments,
                                **({'caller': 'gui'} if tool == 'serial' else {}))
                        else:
                            from extension_tools import remote_call
                            result = await self._run_blocking(remote_call, self.relay, target['nodeId'], tool, action, arguments,
                                **({'caller': 'gui'} if tool == 'serial' else {}))
                        if tool == 'serial' and result.get('success'):
                            sessions = getattr(ws, '_serial_gui_sessions', set())
                            data = result.get('data') or {}
                            if action == 'open' and data.get('sessionId') and not data.get('closed'):
                                sessions.add((target['local'], target.get('nodeId', ''), data['sessionId']))
                            elif action == 'close':
                                identity = (target['local'], target.get('nodeId', ''), arguments.get('sessionId', ''))
                                sessions.discard(identity)
                                getattr(ws, '_serial_transfer_sessions', set()).discard(identity)
                            identity = (target['local'], target.get('nodeId', ''), arguments.get('sessionId', ''))
                            if action == 'read':
                                error = await self._run_blocking(self.serial_recordings.capture, ws, identity, data)
                                if error:
                                    data['recordingError'] = error
                            elif action == 'close':
                                await self._run_blocking(self.serial_recordings.stop, ws, identity)
                            ws._serial_gui_sessions = sessions
                    await self._send(ws, dict(type='exec-result', id=msg.get('id'), **result))
                    return
                name, arguments = msg.get('name'), msg.get('arguments', {})
                if name == 'local_windows_permission':
                    if 'allowed' in arguments:
                        if not isinstance(arguments['allowed'], bool):
                            raise ValueError('权限值必须为布尔值')
                        await self._run_blocking(self.relay.set_windows_permission, arguments['allowed'])
                    data = {'allowed': self.relay.config.allowWindows}
                elif name not in {'targets_list', 'target_status', 'session_open', 'session_read',
                                'session_write', 'session_resize', 'session_close'}:
                    raise ValueError('命令行界面仅提供电脑选择和交互会话')
                else:
                    data = await self.agent.call(name, arguments)
                await self._send(ws, dict(type='exec-result', id=msg.get('id'), success=True, data=data))
            except Exception as exc:
                await self._send(ws, dict(type='exec-result', id=msg.get('id'), success=False, output=str(exc),
                                         targetUnavailable=typ == 'extension-tool',
                                         operationUncertain=bool(getattr(exc, 'operation_uncertain', False))))
            return
        agent = getattr(self, 'agent', None)
        if agent and typ == 'terminal-control' and msg.get('action') == 'take':
            agent.take_local(msg.get('key', ''))
        if agent and typ in ('write-terminal', 'resize-terminal', 'close-terminal') and agent.claimed(msg.get('key', '')):
            return

        if typ == 'terminal-control':
            if self._is_remote_key(msg.get('key', '')):
                if msg.get('action') == 'take':
                    ok, message = await self._run_blocking(self.relay.take_session_control,
                        msg.get('key', ''), msg.get('controlGeneration', ''))
                    self._broadcast_async({'type': 'relay-event', 'kind': 'info' if ok else 'error',
                                           'text': message})
                return
            hub = getattr(self.worker, 'terminal_hub', None)
            key = msg.get('key', '')
            if hub and not self._is_remote_key(key):
                state = hub.state(key)
                if state and msg.get('controlGeneration') == state['generation']:
                    if msg.get('action') == 'take':
                        hub.take(key)
                    elif msg.get('action') == 'give':
                        hub.give(key)
            return

        if typ == 'write-terminal':
            key = msg.get('key', '')
            data = msg.get('data', '')
            generation = msg.get('controlGeneration')
            # data 以字符串到达；逐段编码发送。多行内容逐行限速，避免高延迟
            # 设备上一次性灌入整段导致回显与提示符交错乱码。单行/普通打字走快路径。
            if self._is_remote_key(key):
                self._queue_remote_io(key, data)
                return
            segs = _iter_line_segments(data)
            if len(segs) <= 1:
                self._write_terminal(key, data.encode('utf-8', errors='replace'), generation=generation)
            else:
                for i, seg in enumerate(segs):
                    seg_bytes = seg.encode('utf-8', errors='replace')
                    if i < len(segs) - 1:
                        await self._send_one_line_paced(key, seg_bytes, generation=generation)
                    else:
                        self._write_terminal(key, seg_bytes, generation=generation)

        elif typ == 'resize-terminal':
            key = msg.get('key', '')
            cols = int(msg.get('cols', 120))
            rows = int(msg.get('rows', 40))
            if self._is_remote_key(key):
                self._queue_remote_io(key, (cols, rows), resize=True)
            else:
                self._resize_terminal(key, cols, rows, generation=msg.get('controlGeneration'))

        elif typ == 'close-terminal':
            key = msg.get('key', '')
            hub = getattr(self.worker, 'terminal_hub', None)
            control = hub.state(key) if hub and not self._is_remote_key(key) else None
            if control and (control['owner'] != 'local' or
                            ((control['shared'] or control['held']) and
                             msg.get('controlGeneration') != control['generation'])):
                self._broadcast_async({'type': 'terminal-control-status', **control})
                return
            self._cleanup_device_edit(key)
            self._close_inline_edit(key)
            with self._output_lock:
                self._output_activity.pop(key, None)
            # 远端设备的「关闭」只由前端隐藏标签：与本机设备一致（本机 worker 下一轮
            # 轮询也会把设备重新加回来），会话本身保留，重新显示时不丢上下文。
            if not self._is_remote_key(key):
                self.worker.close_terminal(key)

        elif typ == 'exec-adb':
            args = msg.get('args', '')
            argv = msg.get('argv')
            result = await self._exec_adb(
                args, argv=argv, make_executable=bool(msg.get('makeExecutable')),
                key=msg.get('key', ''),
            )
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'exec-logcat':
            serial = msg.get('serial', '')
            result = await self._exec_logcat(serial, key=msg.get('key', ''))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'start-recording':
            key = msg.get('key', '')
            path = msg.get('path', '')
            result = self._start_recording(key, path, bool(msg.get('timestamps', True)))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'stop-recording':
            key = msg.get('key', '')
            result = self._stop_recording(key)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'get-all-devices':
            devices = get_all_devices() if LOCAL_DEVICES_ENABLED else []
            await self._send(ws, {'type': 'all-devices', 'devices': devices})

        elif typ == 'run-adb-diagnostic':
            selected = msg.get('selectedDevice')
            if selected is not None and not isinstance(selected, dict):
                await self._send(ws, {
                    'type': 'diagnostic-result', 'id': msg.get('id'), 'success': False,
                    'output': '目标设备参数无效',
                })
            elif not self._start_adb_diagnostic(ws, msg.get('id'), selected):
                await self._send(ws, {
                    'type': 'diagnostic-result', 'id': msg.get('id'), 'success': False,
                    'output': '已有 ADB 诊断正在运行，请等待完成或取消。',
                })

        elif typ == 'cancel-adb-diagnostic':
            cancelled = self._cancel_adb_diagnostic(msg.get('id'))
            await self._send(ws, {
                'type': 'diagnostic-cancelled', 'diagnosticId': msg.get('id'), 'success': cancelled,
                'output': '正在取消诊断…' if cancelled else '没有匹配的运行中诊断',
            })

        elif typ == 'usb-relay':
            arguments = msg.get('arguments') or {}
            if not isinstance(arguments, dict):
                result = {'success': False, 'output': '继电器参数无效'}
            else:
                result = await self._run_blocking(self.usb_relay.call, msg.get('action', ''), arguments)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'get-log':
            result = await self._get_log()
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'clear-log':
            result = await self._clear_log()
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'pick-file':
            path = await self._pick_file(bool(msg.get('multi')))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **path})

        elif typ == 'pick-recording-file':
            path = await self._pick_recording_file(msg.get('suggestedName', ''))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **path})

        elif typ == 'pick-directory':
            path = await self._pick_dir(msg.get('title', ''))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **path})

        elif typ == 'ui-debug':
            log('UI', f"{msg.get('action', '')}: {msg.get('detail', '')}")

        elif typ == 'list-device-dir':
            result = await self._list_device_dir(
                msg.get('serial', ''),
                msg.get('transportId', ''),
                msg.get('path', '/'),
                key=msg.get('key', ''),
            )
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'start-device-edit':
            if self._is_remote_key(msg.get('key', '')):
                await self._send(ws, {
                    'type': 'exec-result', 'id': msg.get('id'), 'success': False,
                    'output': '跨机中转暂不支持「用本机程序打开设备文件」：编辑器会在从机弹出，'
                              '本机看不到窗口。请改用应用内直接编辑。',
                })
            else:
                result = await self._start_device_edit(
                    msg.get('key', ''),
                msg.get('serial', ''),
                msg.get('transportId', ''),
                msg.get('path', ''),
            )
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'save-device-edit':
            result = await self._finish_device_edit(msg.get('key', ''), save=True)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'discard-device-edit':
            result = await self._finish_device_edit(msg.get('key', ''), save=False)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'start-inline-edit':
            # 应用内直接编辑需要「读文件 + 原样写回设备」，跨机路径缺少 stdin 通道，
            # 明确拒绝而不是让用户编辑完才发现写不回去。
            if self._is_remote_key(msg.get('key', '')):
                await self._send(ws, {
                    'type': 'exec-result', 'id': msg.get('id'), 'success': False,
                    'output': '跨机中转暂不支持应用内直接编辑设备文件（写回需要 stdin 通道）。'
                              '可以使用终端里的 shell 命令读写远端文件。',
                })
            else:
                result = await self._start_inline_edit(
                    msg.get('key', ''),
                    msg.get('serial', ''),
                    msg.get('transportId', ''),
                    msg.get('path', ''),
                )
                await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'save-inline-edit':
            result = await self._save_inline_edit(msg.get('key', ''), msg.get('content', ''))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ == 'close-inline-edit':
            result = self._close_inline_edit(msg.get('key', ''))
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})

        elif typ in ('get-serial-commands', 'save-serial-commands'):
            try:
                result = await self._run_blocking(self._serial_commands,
                    msg.get('commands') if typ == 'save-serial-commands' else None)
                await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'),
                    'success': True, 'data': result})
            except (OSError, ValueError, TypeError) as exc:
                await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'),
                    'success': False, 'output': str(exc)})

        elif typ == 'get-commands':
            cmds = self._load_commands()
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), 'success': True, 'output': cmds})

        elif typ == 'save-commands':
            cmds = msg.get('commands', [])
            self._save_commands(cmds)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), 'success': True, 'output': 'ok'})

        # --- 跨机中转（需求①的实时状态、②③的设备混入、④的载体选择）---
        elif typ == 'relay-config':
            await self._send(ws, {
                'type': 'relay-config',
                'id': msg.get('id'),
                'config': self.relay.config.to_dict(),
                'carriers': self._relay_carrier_options(),
                'configPath': self.relay.config_path,
            })

        elif typ == 'relay-status':
            status = await self._run_blocking(self.relay.status)
            await self._send(ws, {'type': 'relay-status', 'id': msg.get('id'), **status})

        elif typ == 'relay-discover-deployments':
            roots = await self._run_blocking(
                relay_app.relay.deployment_roots, str(msg.get('root') or ''))
            await self._send(ws, {
                'type': 'relay-deployments', 'id': msg.get('id'),
                'deployments': list(roots),
            })

        elif typ == 'relay-check-config':
            payload = msg.get('config') if isinstance(msg.get('config'), dict) else {}
            ok, reason = await self._run_blocking(self.relay.check_config, payload)
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'),
                                 'success': ok, 'output': '' if ok else reason})

        elif typ == 'relay-save-config':
            payload = msg.get('config') if isinstance(msg.get('config'), dict) else {
                key: msg[key] for key in (
                    'enabled', 'role', 'peerId', 'carrierKind', 'root',
                    'poolId', 'nodeId', 'pairingRequired',
                    'allowedSubcommands', 'allowShell', 'managedSerials',
                ) if key in msg
            }
            ok, reason = await self._run_blocking(self.relay.apply_config, payload)
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok,
                'output': '跨机中转配置已保存并生效' if ok else reason,
            })
            self._broadcast_devices()
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})

        elif typ == 'relay-reload-config':
            # 界面的「刷新状态」：配置可能被另一个实例或手工编辑改过，先按磁盘上的
            # 内容重新生效，再把状态广播出去 —— 只拉状态看不到角色变化。
            changed, ok, reason = await self._run_blocking(self.relay.reload_from_disk)
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok,
                'output': reason, 'changed': bool(changed),
            })
            if changed:
                self._broadcast_devices()
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})

        elif typ == 'relay-inspect-root':
            # 换共享目录前先看看里面有没有上一轮的中转数据：有的话前端要问
            # 「沿用还是清空重建」，而不是默默把两轮数据混在一个目录里。
            info = await self._run_blocking(self.relay.inspect_root, str(msg.get('root') or ''))
            await self._send(ws, {
                'type': 'relay-root-info', 'id': msg.get('id'), **info,
            })

        elif typ == 'relay-clear-cache':
            # 全量删除仅接受本机配置的根和显式确认；耗时清理不能阻塞事件循环。
            try:
                result = await self._run_blocking(
                    self.relay.clear_cache, str(msg.get('root') or ''), msg.get('confirmed') is True)
            except Exception as exc:
                result = {'success': False, 'output': str(exc)}
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), **result})
            self._broadcast_devices()
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})

        elif typ == 'relay-clean-root':
            # 「清空后重建」：只删中转自己的文件（布局清单 + 五个槽目录），
            # 目录里其它东西一律不碰；删了什么会进事件流水。
            removed = await self._run_blocking(self.relay.clean_root, str(msg.get('root') or ''))
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'),
                'success': True,
                'output': ('已清空：' + '、'.join(removed)) if removed else '目录里没有可清的中转数据',
                'removed': removed,
            })
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})

        elif typ == 'relay-confirm-peer':
            peer_id = msg.get('peerId', '')
            ok, reason = await self._run_blocking(
                self.relay.confirm_peer, peer_id, bool(msg.get('accept', True)),
            )
            self._remember_relay_request(peer_id, None)
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok, 'output': reason,
            })
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})
            self._broadcast_devices()

        elif typ == 'relay-revoke-peer':
            peer_id = msg.get('peerId', '')
            ok, reason = await self._run_blocking(self.relay.revoke_peer, peer_id)
            self._remember_relay_request(peer_id, None)
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok, 'output': reason,
            })
            self._broadcast_devices()

        # --- 池与配对：准入门槛的界面入口 ---
        # 配对按**实例键**（nodeId）操作，不按 peerId：peerId 是可变的显示名，
        # 拿它当隔离键的话改一次名就等于换了个空间（详见 docs/v6 §9）。
        elif typ == 'relay-pair-peer':
            node_id = str(msg.get('nodeId') or '')
            peer_id = str(msg.get('peerId') or '')
            ok, reason = await self._run_blocking(
                self.relay.pair_peer, node_id, peer_id=peer_id,
            )
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok, 'output': reason,
            })
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})
            # 配对状态一变，「哪些设备可用」就变了 —— 设备列表要跟着重算，
            # 否则界面会继续显示一台刚被解除配对的远端设备。
            self._broadcast_devices()

        elif typ == 'relay-unpair-peer':
            node_id = str(msg.get('nodeId') or '')
            ok, reason = await self._run_blocking(self.relay.unpair_peer, node_id)
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': ok, 'output': reason,
            })
            status = await self._run_blocking(self.relay.status)
            self._broadcast_async({'type': 'relay-status', **status})
            self._broadcast_devices()

        elif typ == 'relay-close-session':
            ok = await self._run_blocking(self.relay.close_session, msg.get('deviceKey', ''))
            await self._send(ws, {
                'type': 'exec-result', 'id': msg.get('id'), 'success': True,
                'output': '远端终端会话已关闭，窗口保持显示时会自动重开' if ok else '该设备没有远端会话',
            })
            self._broadcast_devices()

        elif typ == 'restart-server':
            await self._send(ws, {'type': 'exec-result', 'id': msg.get('id'), 'success': True, 'output': 'restarting'})
            # 稍作延迟确保响应送达，再触发进程内后端重启（不重生进程）
            await asyncio.sleep(0.5)
            self.restart()

    # --- Command file storage (shared across all clients) ---
    def _tool_config_storage(self, tool):
        path = Path(self.relay.config_path)
        legacy = path.with_name(path.stem + '-' + tool + '.json')
        return self.relay.config_store.section('tools', tool, legacy)

    @staticmethod
    def _default_relay_config_path():
        # 与 commands.json 同规则：冻结版写到 exe 同目录，源码运行写到项目目录。
        if getattr(sys, 'frozen', False):
            return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'relay.json')
        return os.path.join(_exe_dir(), 'relay.json')

    @classmethod
    def _relay_config_path(cls):
        """本实例该用哪份中转配置。

        - 显式 ``RELAY_CONFIG``：用户明确指定（同机起多实例做跨机验证就是走这个），
          原样使用，不做占用检测 —— 那是调用方自己选择共享与否。
        - 默认路径：relay.json 语义是「一台机器一份」，但同时开两个窗口时两个窗口
          会读写同一个文件（改了这边的角色，那边一刷新/重启就跟着变）。所以第二
          个实例通过 ``claim_config_path`` 让出去，用独立的持久配置并在界面说明。
        """
        override = os.environ.get('RELAY_CONFIG')
        if override:
            return override
        default_path = cls._default_relay_config_path()
        chosen, notice = relay_app.claim_config_path(default_path)
        cls._relay_config_notice = notice
        return chosen

    _relay_config_notice = ''

    @staticmethod
    def _commands_path():
        # 冻结版 exe 中 _exe_dir() 指向 _MEIPASS 临时目录（重启即清空），
        # 快捷指令必须持久化到 exe 所在目录（与 adb_worker.LOG_FILE 一致）。
        if getattr(sys, 'frozen', False):
            return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'commands.json')
        return os.path.join(_exe_dir(), 'commands.json')

    @staticmethod
    def _command_store_path():
        return Path(ManagerServer._commands_path())

    @staticmethod
    def _load_command_store():
        # 两种窗口共用一个文件，各自维护独立命名空间与指令集。
        path = ManagerServer._command_store_path()
        with _COMMANDS_LOCK:
            rewrite = False
            if path.exists():
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, list):  # 原版commands.json中的ADB指令集
                    store = {'adb': data, 'serial': None}
                    rewrite = True
                elif isinstance(data, dict) and set(data) == {'adb', 'serial'}:
                    store = data
                else:
                    raise ValueError('快捷指令文件格式无效')
            else:
                store = {'adb': None, 'serial': None}
            # 保留早期单层ADB指令数组的升级能力，也修复已装入命名空间的旧形状。
            adb = store['adb']
            if isinstance(adb, list) and adb and isinstance(adb[0], dict) and 'cmd' in adb[0]:
                store['adb'] = [{'name': '默认', 'commands': adb}]
                rewrite = True
            legacy = path.with_name('serial-commands.json')
            if legacy.exists():
                serial = json.loads(legacy.read_text(encoding='utf-8'))
                if not isinstance(serial, list):
                    raise ValueError('旧串口快捷指令格式无效，已保留原文件')
                if store['serial'] is None:
                    store['serial'] = serial
                elif store['serial'] != serial:
                    known = {json.dumps(group, ensure_ascii=False, sort_keys=True) for group in store['serial']}
                    for group in serial:
                        identity = json.dumps(group, ensure_ascii=False, sort_keys=True)
                        if identity in known:
                            continue
                        candidate = group
                        names = {entry.get('name') for entry in store['serial'] if isinstance(entry, dict)}
                        if isinstance(group, dict) and group.get('name') in names:
                            candidate = {**group, 'name': ('迁移串口 · ' + group['name'])[:80]}
                        store['serial'].append(candidate)
                        known.add(identity)
                ManagerServer._validate_command_groups(store['serial'])
                rewrite = True
                backup = legacy.with_name('serial-commands.json.migrated')
                if not backup.exists():
                    # 先写入共用文件，再保留旧串口文件副本。
                    ManagerServer._write_command_store(path, store)
                    legacy.replace(backup)
                    rewrite = False
            if rewrite:
                ManagerServer._write_command_store(path, store)
            return store

    @staticmethod
    def _validate_command_groups(groups):
        if not isinstance(groups, list) or not 1 <= len(groups) <= 32:
            raise ValueError('指令集数量必须为1～32')
        for group in groups:
            if not isinstance(group, dict) or set(group) != {'name', 'commands'}:
                raise ValueError('指令集格式无效')
            if not isinstance(group['name'], str) or not 1 <= len(group['name']) <= 80:
                raise ValueError('指令集名称长度必须为1～80')
            if not isinstance(group['commands'], list) or len(group['commands']) > 256:
                raise ValueError('每个指令集最多256条指令')
            for command in group['commands']:
                if (not isinstance(command, dict) or set(command) != {'name', 'cmd'}
                        or not isinstance(command['name'], str) or not 1 <= len(command['name']) <= 80
                        or not isinstance(command['cmd'], str) or not 1 <= len(command['cmd']) <= 65536):
                    raise ValueError('指令名称或内容无效')

    @staticmethod
    def _write_command_store(path, store):
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _save_command_scope(scope, groups):
        ManagerServer._validate_command_groups(groups)
        path = ManagerServer._command_store_path()
        with _COMMANDS_LOCK:
            store = ManagerServer._load_command_store()
            store[scope] = groups
            ManagerServer._write_command_store(path, store)
        return groups

    @staticmethod
    def _load_commands():
        # ADB兼容入口仅返回ADB自己的指令集。
        return ManagerServer._load_command_store()['adb']

    @staticmethod
    def _save_commands(cmds):
        ManagerServer._save_command_scope('adb', cmds)

    def _save_serial_xmodem(self, target, session_id, transfer_id):
        # 只从已成功且归属GUI的任务读取；目标路径由本机原生另存为选择。
        from xmodem_transfer import CHUNK, MAX_FILE
        arguments = {'sessionId': session_id, 'transferId': transfer_id}
        def read(action, **extra):
            if target['local']:
                result = self.extension_tools.call('serial', action, {**arguments, **extra}, caller='gui')
            else:
                from extension_tools import remote_call
                result = remote_call(self.relay, target['nodeId'], 'serial', action,
                    {**arguments, **extra}, caller='gui')
            if not result.get('success'):
                raise RuntimeError(result.get('output') or '接收文件读取失败')
            return result['data']
        status = read('xmodem-status')['xmodem']
        if status['direction'] != 'receive' or status['state'] != 'succeeded':
            raise RuntimeError('接收文件尚未成功')
        chosen = _FileDialog.pick('save', title='保存XMODEM接收文件（请选择新文件名）',
            default_name='serial-' + time.strftime('%Y%m%d-%H%M%S') + '.bin')
        if not chosen.get('success'):
            return dict(success=False, output=chosen.get('output') or '已取消保存；接收文件仍保留')
        destination = Path(chosen['path'])
        offset = 0
        # 独占新文件，拒绝覆盖已有数据；异常仅移除本调用创建的未完成文件。
        with destination.open('xb') as output:
            try:
                while True:
                    data = read('xmodem-download', offset=offset, limit=CHUNK)
                    raw = base64.b64decode(data['data'], validate=True)
                    if data['nextOffset'] != offset + len(raw) or data['nextOffset'] > MAX_FILE or (not raw and not data['eof']):
                        raise ValueError('接收文件块偏移无效')
                    output.write(raw)
                    offset = data['nextOffset']
                    if data['eof']:
                        if offset != status['transferredBytes']:
                            raise ValueError('接收文件长度不一致')
                        break
            except Exception:
                output.close()
                destination.unlink(missing_ok=True)
                raise
        return dict(success=True, output=f'已保存 {offset} 字节（含协议填充）：{destination}',
            data={'path': str(destination), 'bytes': offset})

    @staticmethod
    def _serial_commands(groups=None):
        # 与ADB共用commands.json持久化，但窗口各自读取独立指令集。
        if groups is None:
            stored = ManagerServer._load_command_store()['serial']
            return stored if stored is not None else [{'name': '默认', 'commands': []}]
        return ManagerServer._save_command_scope('serial', groups)

    # --- ADB command helpers (run in executor to avoid blocking loop) ---
    @staticmethod
    def _relay_carrier_options() -> list:
        """设置页的连接方式下拉：已实现的可选，未实现的显式标注原因。"""
        try:
            from relay_carrier import carrier_specs  # noqa: PLC0415 - 仅此处需要
            specs = carrier_specs()
        except Exception:
            specs = {}
        options = []
        for kind, spec in specs.items():
            options.append({
                'kind': kind,
                'label': spec.label,
                'implemented': bool(spec.implemented),
                'options': list(spec.option_fields),
                'note': spec.notes,
                'topology': spec.topology,
            })
        options.sort(key=lambda item: (not item['implemented'], item['kind']))
        return options

    async def _exec_adb(self, args: str = '', argv=None, make_executable=False, key: str = ''):
        """执行 adb 命令。

        普通命令仍接收字符串；文件传输使用 argv，避免 Windows 本地路径
        中的反斜杠被 shlex.split 当作转义字符吞掉。

        带 ``key`` 且该 key 属于远端设备时改走中转：操作端自带的设备选择符
        （``-s``/``-t``）会在这里被剥掉 —— 那两个值在从机上没有意义，甚至可能
        指向从机上的**另一台**设备。
        """
        command_args = list(argv) if isinstance(argv, list) else shlex.split(args or '')
        if key and self._is_remote_key(key):
            return await self._exec_remote_adb(key, command_args, make_executable)
        timeout = 60 if isinstance(argv, list) else 30
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    [ADB_PATH] + command_args,
                    capture_output=True, text=True, timeout=timeout,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = (proc.stdout or '') + (proc.stderr or '')
            # 移除最后的换行
            output = output.rstrip('\n')
            if proc.returncode == 0:
                if make_executable:
                    permission = await self._make_uploaded_script_executable(command_args)
                    if not permission['success']:
                        detail = permission.get('output') or '未知错误'
                        return {
                            'success': False,
                            'output': f'上传成功，但自动添加执行权限失败：{detail}',
                        }
                    targets = permission.get('targets') or ([permission['target']] if permission.get('target') else [])
                    suffix = f'；已添加可执行权限 → {"、".join(targets)}' if targets else '；已添加可执行权限'
                    output = f'{output}{suffix}' if output else f'上传成功{suffix}'
                if 'push' in command_args:
                    sync_result = await self._sync_uploaded_files(command_args)
                    if not sync_result['success']:
                        detail = sync_result.get('output') or '未知错误'
                        return {
                            'success': False,
                            'output': f'上传成功，但设备文件同步失败：{detail}',
                        }
                    output = f'{output}；设备文件已同步' if output else '上传成功；设备文件已同步'
                return {'success': True, 'output': output}
            return {'success': False, 'output': output or f'exit code {proc.returncode}'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': f'执行超时 ({timeout}s)'}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    async def _exec_remote_adb(self, key: str, command_args, make_executable=False) -> dict:
        """远端文件经共享目录搬运，普通命令经请求队列执行。"""
        from relay_app import strip_device_selector
        tokens = strip_device_selector(command_args)
        if tokens and tokens[0] in ('push', 'pull'):
            result = await self._run_blocking(self.relay.remote_transfer, key, command_args, make_executable)
        else:
            result = await self._run_blocking(self.relay.remote_exec, key, command_args, 60.0)
        result['remote'] = True
        return result

    async def _sync_uploaded_files(self, command_args):
        """在文件上传及可执行权限设置完成后，刷新目标设备的文件系统缓存。"""
        try:
            push_index = command_args.index('push')
            transport = command_args[:push_index]
            sync_args = [ADB_PATH, *transport, 'shell', 'sync']
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    sync_args,
                    capture_output=True, text=True, timeout=15,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).rstrip('\n')
            if proc.returncode == 0:
                return {'success': True, 'output': output}
            return {'success': False, 'output': output or f'exit code {proc.returncode}'}
        except ValueError:
            return {'success': False, 'output': '上传参数不完整'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': 'sync 超时 (15s)'}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    async def _make_uploaded_script_executable(self, command_args):
        """上传 shell 脚本后，在设备端补上可执行权限。"""
        try:
            push_index = command_args.index('push')
            if push_index + 2 >= len(command_args):
                return {'success': False, 'output': '上传参数不完整'}
            local_paths = [str(path) for path in command_args[push_index + 1:-1]]
            remote_path = str(command_args[-1])
            script_paths = [path for path in local_paths if path.lower().endswith('.sh')]
            if not script_paths:
                return {'success': True, 'targets': []}
            transport = command_args[:push_index]

            check_args = [ADB_PATH, *transport, 'shell', 'test', '-d', remote_path]
            check = await self._run_blocking(
                lambda: subprocess.run(
                    check_args,
                    capture_output=True, text=False, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            targets = []
            for local_path in script_paths:
                filename = os.path.basename(local_path.replace('\\', '/').rstrip('/'))
                if not filename:
                    return {'success': False, 'output': '无法确定脚本文件名'}
                target = remote_path.rstrip('/') + '/' + filename if check.returncode == 0 else remote_path
                chmod_args = [ADB_PATH, *transport, 'shell', 'chmod', '+x', target]
                log('ADB', f'add executable permission: {chmod_args!r}')
                chmod = await self._run_blocking(
                    lambda: subprocess.run(
                        chmod_args,
                        capture_output=True, text=False, timeout=10,
                        creationflags=CREATE_NO_WINDOW,
                    )
                )
                if chmod.returncode != 0:
                    error = ((chmod.stdout or b'') + (chmod.stderr or b'')).decode(
                        'utf-8', errors='replace'
                    ).strip()
                    return {'success': False, 'output': error or f'exit code {chmod.returncode}'}
                targets.append(target)
            return {'success': True, 'target': targets[0], 'targets': targets}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '设置执行权限超时 (10s)'}
        except Exception as e:
            log('ADB', f'add executable permission failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _exec_logcat(self, serial: str, key: str = ''):
        try:
            if key and self._is_remote_key(key):
                # 远端设备：命令里不能带 -s，由从机自己选定设备。
                result = await self._run_blocking(
                    self.relay.remote_exec, key, ['logcat', '-d', '-v', 'brief'], 30.0,
                )
                if not result.get('success'):
                    return result
                lines = (result.get('output') or '').splitlines()
                return {'success': True, 'output': '\n'.join(lines[-2000:]), 'remote': True}
            args = ['-s', serial, 'logcat', '-d', '-v', 'brief'] if serial else ['logcat', '-d', '-v', 'brief']
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    [ADB_PATH] + args,
                    capture_output=True, text=True, timeout=15,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            lines = (proc.stdout or '').splitlines()
            trimmed = '\n'.join(lines[-2000:])
            return {'success': True, 'output': trimmed}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    # --- Terminal session recording (captures xterm output to file) ---
    def _start_recording(self, key: str, path: str, timestamps: bool = True):
        if not key:
            return {'success': False, 'output': '缺少终端标识'}
        if not path:
            return {'success': False, 'output': '缺少保存路径'}
        # Ensure parent directory exists
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception as e:
                return {'success': False, 'output': f'创建目录失败: {e}'}
        # 记录采用追加模式。启动时先打开一次，尽早反馈无写入权限或误选目录等错误。
        try:
            with open(path, 'ab+') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                at_line_start = size == 0
                if not at_line_start:
                    f.seek(-1, os.SEEK_END)
                    at_line_start = f.read(1) in (b'\n', b'\r')
        except Exception as e:
            return {'success': False, 'output': f'无法追加写入记录文件: {e}'}
        with self._recording_lock:
            self._recording_sessions[key] = {
                'path': path,
                'timestamps': timestamps,
                'at_line_start': at_line_start,
            }
        log('REC', f'start recording {key} (timestamps={timestamps}) → {path}')
        suffix = '（每行带时间戳）' if timestamps else '（原始输出）'
        return {'success': True, 'output': f'终端记录已开始{suffix}，将从末尾追加 → {path}'}

    def _stop_recording(self, key: str):
        with self._recording_lock:
            old = self._recording_sessions.pop(key, None)
        if old:
            log('REC', f'stop recording {key}')
            return {'success': True, 'output': '终端记录已停止'}
        return {'success': True, 'output': '当前未在记录'}

    def _stop_all_recording(self):
        with self._recording_lock:
            self._recording_sessions.clear()

    async def _get_log(self):
        try:
            content = await self._run_blocking(adb_worker.read_log_tail)
            return {'success': True, 'output': content}
        except OSError as exc:
            return {'success': False, 'output': f'读取应用日志失败：{exc}'}

    async def _clear_log(self):
        try:
            await self._run_blocking(adb_worker.clear_log_file)
            return {'success': True, 'output': ''}
        except OSError as exc:
            return {'success': False, 'output': f'清空应用日志失败：{exc}'}

    async def _pick_file(self, multi: bool = False):
        """Open native Windows file dialog (COM IFileDialog) from an executor thread."""
        try:
            log('PICK', f'pick-file request received (multi={multi})')
            return await self._run_blocking(
                lambda: _FileDialog.pick('file', multi)
            )
        except Exception as e:
            log('PICK', f'error: {e}')
            return {'success': False, 'path': '', 'output': str(e)}

    async def _pick_recording_file(self, suggested_name: str = ''):
        """一个另存为窗口：可选择已有记录文件或在目录中输入新文件名。"""
        try:
            log('PICK', 'pick-recording-file request received')
            return await self._run_blocking(
                lambda: _FileDialog.pick(
                    'save', title='选择或新建终端记录文件（已有文件将追加）',
                    default_name=suggested_name,
                )
            )
        except Exception as e:
            log('PICK', f'recording file error: {e}')
            return {'success': False, 'path': '', 'output': str(e)}

    async def _pick_dir(self, title: str = ''):
        """Open native Windows directory picker (COM IFileDialog) from an executor thread."""
        try:
            log('PICK', f'pick-directory request received ({title or "默认标题"})')
            return await self._run_blocking(
                lambda: _FileDialog.pick('directory', title=title or None)
            )
        except Exception as e:
            log('PICK', f'dir error: {e}')
            return {'success': False, 'path': '', 'output': str(e)}

    async def _list_device_dir(self, serial: str, transport_id: str, path: str, key: str = ''):
        """List contents of a directory on the Android device via adb shell."""
        log('LDDIR', f'list_device_dir called: serial={serial!r} tid={transport_id!r} path={path!r} key={key!r}')
        try:
            path = (path or '/').strip()
            if not path.startswith('/'):
                return {'success': False, 'output': '设备目录路径必须是绝对路径', 'entries': []}
            if key and self._is_remote_key(key):
                return await self._list_remote_device_dir(key, path)
            transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
            list_args = [ADB_PATH, *transport, 'shell', 'ls', '-1F', path]
            log('LDDIR', f'Step 1 - args: {list_args!r}')
            log('LDDIR', f'Step 1 - path({type(path).__name__})={path!r}')

            proc = await self._run_blocking(
                lambda: subprocess.run(
                    list_args,
                    capture_output=True, text=True, timeout=10,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            log('LDDIR', f'Step 1 - returncode={proc.returncode}')
            log('LDDIR', f'Step 1 - stdout ({len(proc.stdout or "")} chars): {proc.stdout!r}')
            log('LDDIR', f'Step 1 - stderr ({len(proc.stderr or "")} chars): {proc.stderr!r}')

            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            log('LDDIR', f'Step 1 - trimmed output ({len(output)} chars): {output[:500]!r}')

            # Step 2: Detect errors
            if proc.returncode != 0:
                log('LDDIR', f'Step 2 - command failed: {proc.returncode}')
                return {'success': False, 'output': output or f'exit code {proc.returncode}', 'entries': []}
            if not output:
                log('LDDIR', 'Step 2 - output empty, returning []')
                return {'success': True, 'entries': [], 'currentPath': path}
            err_lower = output.lower()
            if any(kw in err_lower for kw in ('permission denied', 'cannot access', 'no such file', 'not found', 'opendir failed')):
                log('LDDIR', f'Step 2 - error detected in output: {output[:200]}')
                return {'success': False, 'output': output, 'entries': []}
            log('LDDIR', 'Step 2 - no error detected')

            # Step 3: Parse lines（与远端目录列举共用同一套解析规则）
            lines = [l for l in output.splitlines() if l.strip()]
            log('LDDIR', f'Step 3 - {len(lines)} lines: {lines[:20]}')
            entries = self._parse_device_dir(lines, path)
            log('LDDIR', 'SUCCESS - returning entries')
            return {'success': True, 'entries': entries, 'currentPath': path}
        except Exception as e:
            log('LDDIR', f'ERROR: {type(e).__name__}: {e}')
            import traceback
            log('LDDIR', f'ERROR traceback: {traceback.format_exc()}')
            return {'success': False, 'output': str(e), 'entries': []}

    @staticmethod
    def _parse_device_dir(lines, path: str) -> list[dict]:
        """把 ``ls -1F`` 的输出解析成目录项列表。

        ``-F`` 会在目录后加 ``/``、可执行文件后加 ``*`` 等标记，这里只保留
        目录判定需要的信息。远端列举与本机列举共用它，避免两条路径的解析分叉。
        """
        entries = []
        for line in lines:
            if line in ('.', '..'):
                continue
            if line.endswith('/'):
                name, is_dir = line[:-1], True
            else:
                name, is_dir = line.rstrip('@*|='), False
            full_path = path.rstrip('/') + '/' + name if name else ''
            entries.append({'name': name, 'isDir': is_dir, 'path': full_path})
        entries.sort(key=lambda e: (not e['isDir'], e['name'].lower()))
        return entries

    async def _list_remote_device_dir(self, key: str, path: str):
        """远端设备的目录列举：同一条 ``ls -1F``，只是经从机执行。"""
        result = await self._run_blocking(
            self.relay.remote_list_dir, key, path,
        )
        if not result.get('success'):
            return {'success': False, 'output': result.get('output', ''), 'entries': []}
        output = (result.get('output') or '').strip()
        if not output:
            return {'success': True, 'entries': [], 'currentPath': path}
        lines = [line for line in output.splitlines() if line.strip()]
        return {
            'success': True,
            'entries': self._parse_device_dir(lines, path),
            'currentPath': path,
            'remote': True,
        }

    async def _start_device_edit(self, key: str, serial: str, transport_id: str, remote_path: str):
        """拉取到临时目录，并通过 Windows“打开方式”选择编辑程序。"""
        if not key:
            return {'success': False, 'output': '缺少编辑会话标识'}
        if not remote_path or not remote_path.startswith('/'):
            return {'success': False, 'output': '设备文件路径必须是绝对路径'}
        if not serial and not transport_id:
            return {'success': False, 'output': '缺少设备标识'}
        with self._edit_lock:
            if key in self._edit_sessions:
                return {'success': False, 'output': '该设备已有未结束的编辑会话'}

        temp_dir = tempfile.mkdtemp(prefix='devmux-edit-')
        filename = remote_path.rstrip('/').rsplit('/', 1)[-1] or 'device-file'
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        local_path = os.path.join(temp_dir, filename)
        transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
        pull_args = [ADB_PATH, *transport, 'pull', remote_path, local_path]
        log('EDIT', f'pull device file: {pull_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    pull_args,
                    capture_output=True, text=True, timeout=60,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0 or not os.path.isfile(local_path):
                shutil.rmtree(temp_dir, ignore_errors=True)
                return {'success': False, 'output': output or f'exit code {proc.returncode}'}

            with self._edit_lock:
                self._edit_sessions[key] = {
                    'tempDir': temp_dir,
                    'localPath': local_path,
                    'remotePath': remote_path,
                    'transport': transport,
                }
            try:
                subprocess.Popen(
                    ['rundll32.exe', 'shell32.dll,OpenAs_RunDLL', local_path],
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                self._cleanup_device_edit(key)
                return {'success': False, 'output': '无法打开 Windows“打开方式”窗口'}
            return {'success': True, 'output': '文件已打开；编辑并保存后，点击“保存覆盖”写回设备'}
        except subprocess.TimeoutExpired:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {'success': False, 'output': '拉取文件超时 (60s)'}
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            log('EDIT', f'start edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _start_inline_edit(self, key: str, serial: str, transport_id: str, remote_path: str):
        """直接读取设备上的 UTF-8 文本，不创建本地临时文件。"""
        if not key:
            return {'success': False, 'output': '缺少编辑会话标识'}
        if not remote_path or not remote_path.startswith('/'):
            return {'success': False, 'output': '设备文件路径必须是绝对路径'}
        if not serial and not transport_id:
            return {'success': False, 'output': '缺少设备标识'}
        with self._edit_lock:
            if key in self._edit_sessions:
                return {'success': False, 'output': '该设备正在使用外部编辑会话，请先结束后再直接编辑'}
        with self._inline_lock:
            if key in self._inline_sessions:
                return {'success': False, 'output': '该设备已有直接编辑会话'}

        transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
        read_args = [ADB_PATH, *transport, 'exec-out', 'cat', remote_path]
        log('INLINE', f'read remote text: {read_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    read_args,
                    capture_output=True, text=False, timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            if proc.returncode != 0:
                error = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
                return {'success': False, 'output': error or f'exit code {proc.returncode}'}
            content = proc.stdout or b''
            if len(content) > 4 * 1024 * 1024:
                return {'success': False, 'output': '文件超过 4MB，无法直接编辑，请使用打开方式'}
            try:
                content.decode('utf-8')
            except UnicodeDecodeError:
                return {'success': False, 'output': '仅支持 UTF-8 文本文件；二进制文件请使用打开方式'}
            with self._inline_lock:
                self._inline_sessions[key] = {
                    'remotePath': remote_path,
                    'transport': transport,
                    'content': content,
                }
            return {
                'success': True,
                'content': base64.b64encode(content).decode('ascii'),
                'encoding': 'utf-8',
                'output': '文件已读取，可直接编辑；修改后点击“立即保存”写回设备',
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '读取文件超时 (60s)'}
        except Exception as e:
            log('INLINE', f'start inline edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _save_inline_edit(self, key: str, content_b64: str):
        """把应用内编辑器的 UTF-8 内容通过 adb shell 标准输入写回设备。"""
        with self._inline_lock:
            session = self._inline_sessions.get(key)
        if not session:
            return {'success': False, 'output': '没有可保存的直接编辑会话'}
        try:
            content = base64.b64decode(content_b64 or '', validate=True)
            content.decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return {'success': False, 'output': '编辑内容不是有效的 UTF-8 文本'}
        if len(content) > 4 * 1024 * 1024:
            return {'success': False, 'output': '文件超过 4MB，无法直接保存'}

        remote_path = session['remotePath']
        shell_command = f'cat > {shlex.quote(remote_path)}'
        write_args = [ADB_PATH, *session['transport'], 'shell', shell_command]
        log('INLINE', f'write remote text: {write_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    write_args,
                    input=content, capture_output=True, text=False, timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            if proc.returncode != 0:
                error = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
                return {'success': False, 'output': error or f'exit code {proc.returncode}'}
            with self._inline_lock:
                current = self._inline_sessions.get(key)
                if current:
                    current['content'] = content
            return {'success': True, 'output': '已保存到设备'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '保存文件超时 (60s)'}
        except Exception as e:
            log('INLINE', f'save inline edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    def _close_inline_edit(self, key: str):
        with self._inline_lock:
            session = self._inline_sessions.pop(key, None)
        return {'success': True, 'output': '直接编辑会话已关闭'} if session else {'success': True, 'output': ''}

    def _cleanup_all_inline_edits(self):
        with self._inline_lock:
            self._inline_sessions.clear()

    async def _finish_device_edit(self, key: str, save: bool):
        """保存或放弃编辑会话，并清理临时文件。"""
        with self._edit_lock:
            session = self._edit_sessions.get(key)
        if not session:
            return {'success': False, 'output': '没有可结束的编辑会话'}
        if not save:
            self._cleanup_device_edit(key)
            return {'success': True, 'output': '已放弃编辑，临时文件已删除'}

        push_args = [
            ADB_PATH, *session['transport'], 'push',
            session['localPath'], session['remotePath'],
        ]
        log('EDIT', f'push edited file: {push_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    push_args,
                    capture_output=True, text=True, timeout=60,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0:
                return {'success': False, 'output': output or f'exit code {proc.returncode}'}
            self._cleanup_device_edit(key)
            return {'success': True, 'output': '已覆盖设备端文件，临时文件已删除'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '保存文件超时 (60s)'}
        except Exception as e:
            log('EDIT', f'save edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    def _cleanup_device_edit(self, key: str):
        with self._edit_lock:
            session = self._edit_sessions.pop(key, None)
        if session:
            shutil.rmtree(session.get('tempDir', ''), ignore_errors=True)

    def _cleanup_all_device_edits(self):
        with self._edit_lock:
            keys = list(self._edit_sessions.keys())
        for key in keys:
            self._cleanup_device_edit(key)

    # --- Lifecycle ---
    async def run(self):
        """后台服务主循环：初始化 → 等待停止/重启 → 清理，循环往复。

        重启走进程内重置（复用同一 WS 端口、重建 worker），避免 PyInstaller
        onefile 模式下"新实例复用旧实例 _MEIPASS、旧实例退出时清理导致 404"
        的脆弱性。
        """
        global _WS_PORT
        self.loop = asyncio.get_running_loop()
        while True:
            self._stop = asyncio.Event()
            self._restart = asyncio.Event()

            self.worker.start()
            log('MAIN', 'ADB Worker started')

            # 跨机中转按配置自启。载体打开、心跳初始化都有磁盘 IO，放进线程池。
            if self.relay.config.enabled:
                started, reason = await self._run_blocking(self.relay.start)
                if started:
                    log('RELAY', f'跨机中转已启动：{self.relay.config.role} '
                                 f'{self.relay.config.peerId} → {self.relay.config.root}')
                else:
                    log('RELAY', f'跨机中转未启动：{reason}')
                self._broadcast_devices()

            # Bind WebSocket with retry on port conflict
            port = WS_PORT
            while True:
                try:
                    ws_server = await websockets.serve(
                        self.ws_handler, '127.0.0.1', port,
                        ping_interval=20, ping_timeout=10,
                        max_size=8 * 1024 * 1024,
                    )
                    break
                except OSError as e:
                    if port < WS_PORT + 10:
                        port += 1
                        log('WS', f'Port {port-1} busy, trying {port}')
                    else:
                        log('WS', f'Failed to bind WebSocket: {e}')
                        raise
            self._ws_port = port
            _WS_PORT = port
            directory = runtime_directory()
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / (self._agent_instance+'.json')
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(instanceId=self._agent_instance,
                    pid=os.getpid(), wsPort=port, token=self._agent_token,
                    configPath=self.relay.config_path), ensure_ascii=False), encoding='utf-8')
                temporary.replace(path)
                self._agent_runtime = path
            except OSError as exc:
                log('AGENT', f'无法写入实例发现目录 {directory}：{exc}；请设置 ADB_AGENT_RUNTIME_DIR 为可写目录')
            log('WS', f'WebSocket server on ws://127.0.0.1:{self._ws_port}')

            # Wait until stop or restart is requested
            tasks = [asyncio.create_task(self._stop.wait()), asyncio.create_task(self._restart.wait())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()

            # Tear down current backend
            # 先停止烧写工具并回收线程，再关闭 WS，保留最终状态推送。
            await self._run_blocking(self.db_flash.close)
            if self._agent_runtime:
                self._agent_runtime.unlink(missing_ok=True)
            self.agent.close()
            self.windows.close_sessions()
            await self._run_blocking(self.serial.close)
            ws_server.close()
            await ws_server.wait_closed()
            self._cancel_pending_broadcasts()
            remote_tasks = self._cancel_remote_io()
            if remote_tasks:
                await asyncio.gather(*remote_tasks, return_exceptions=True)
            self._stop_all_recording()
            self.serial_recordings.close_owner()
            self._cleanup_all_device_edits()
            self._cleanup_all_inline_edits()
            # 中转先取消远端接入并写meta，再由worker关闭本地PTY；共享会话不能被
            # 远端接入的关闭操作提前终止。
            await self._run_blocking(self.relay.stop)
            worker_stopped = self.worker.stop()

            if self._stop.is_set():
                self.windows.close()
                break  # 真正退出
            if not worker_stopped:
                # ADB 调用仍在超时窗口内时，旧 worker 保留其已设置的停止令牌并自行
                # 退出；重启使用新实例，绝不重新激活旧线程。
                log('MAIN', '旧 ADB Worker 尚未退出，创建新实例继续重启')
                self.worker = self._new_worker()
            self.db_flash = DBFlashService(RealExecutor(), progress=self._broadcast_async)
            self.db_tool = DBFlashExtension(lambda: self.db_flash, self.db_tool.config_path,
                config_storage=self._tool_config_storage('db-flash'))
            self.extension_tools.services['db-flash'] = self.db_tool
            from serial_terminal import SerialService
            self.serial = SerialService(self.serial.config_path,
                config_storage=self._tool_config_storage('serial'))
            self.extension_tools.services['serial'] = self.serial
            log('MAIN', 'Backend services restarting...')

        self._executor.shutdown(wait=False, cancel_futures=True)

    def stop(self):
        """Request graceful shutdown from another thread."""
        if self.loop and not self.loop.is_closed() and self._stop is not None and not self._stop.is_set():
            self.loop.call_soon_threadsafe(self._stop.set)

    def restart(self):
        """Request an in-process backend restart from another thread."""
        if self.loop and not self.loop.is_closed() and self._restart is not None and not self._restart.is_set():
            self.loop.call_soon_threadsafe(self._restart.set)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _crash_dump_path():
    """冻结版崩溃记录持久保存在 exe 旁，不能写入退出后消失的 _MEIPASS。"""
    return os.path.join(os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__)), 'crash_dump.txt')


_native_crash_file = None


def _enable_native_crash_log():
    """捕获原生 PTY/WebView 崩溃，保留所有线程的现场。"""
    global _native_crash_file
    try:
        import faulthandler
        _native_crash_file = open(_crash_dump_path(), 'a', encoding='utf-8')
        faulthandler.enable(file=_native_crash_file, all_threads=True)
    except Exception as exc:
        log('MAIN', f'无法启用原生崩溃记录：{exc}')


def crash_handler(exctype, value, tb):
    """Global exception hook — writes to crash_dump.txt before the process dies."""
    dump_path = _crash_dump_path()
    msg = f'Unhandled exception: {exctype.__name__}: {value}\n'
    import traceback as _tb
    msg += ''.join(_tb.format_tb(tb))
    try:
        with open(dump_path, 'a', encoding='utf-8') as f:
            f.write(msg)
    except Exception:
        pass
    # Also write to our structured log if possible
    try:
        log('FATAL', msg)
    except Exception:
        pass
    # Call original hook
    sys.__excepthook__(exctype, value, tb)

sys.excepthook = crash_handler
# Also catch threading exceptions
threading.excepthook = lambda args: crash_handler(args.exc_type, args.exc_value, args.exc_traceback)

def main():
    """Start backend services, then open native desktop window if webview is available."""
    import crash_diagnostics
    crash_diagnostics.initialize(log, Path(_crash_dump_path()).parent)
    _enable_native_crash_log()
    http_srv = start_http_server()
    http_port = http_srv.server_address[1]
    mgr = ManagerServer()

    try:
        # Run asyncio event loop (WebSocket + ADB worker) in a background thread
        stop_event = threading.Event()

        def _run_async():
            try:
                asyncio.run(mgr.run())
            except Exception:
                log('MAIN', f'Async thread crashed: {traceback.format_exc()}')
            finally:
                stop_event.set()

        threading.Thread(target=_run_async, daemon=True).start()

        # Wait for asyncio loop + WebSocket to be ready (with timeout)
        ready = False
        t0 = time.time()
        while time.time() - t0 < 30:
            if mgr.loop is not None and hasattr(mgr, '_ws_port') and mgr._ws_port:
                ready = True
                break
            if stop_event.is_set():
                log('MAIN', 'Async thread stopped before becoming ready')
                break
            time.sleep(0.1)

        if not ready:
            log('MAIN', 'Failed to start backend services, exiting')
            http_srv.shutdown()
            return

        ws_port = mgr._ws_port
        log('MAIN', f'Backend ready (HTTP {http_port}, WS {ws_port})')

        # Open native desktop window
        if (os.environ.get('DEVMUX_HEADLESS') or
                os.environ.get('ADB_MANAGER_HEADLESS')) == '1':
            # 自动化/多实例验证用：只跑后端（HTTP + WS），不开窗口。
            log('MAIN', f'headless 模式：后端已就绪（HTTP {http_port}, WS {ws_port}）')
            try:
                while not stop_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            http_srv.shutdown()
            return
        try:
            import webview
            import random as _r
            _cache_bust_url = f'http://127.0.0.1:{http_port}/?_t={_r.randint(0,999999999)}'
            log('MAIN', f'Opening desktop window ({_cache_bust_url})...')
            window = webview.create_window(
                APP_TITLE,
                _cache_bust_url,
                width=1200, height=800,
                min_size=(800, 600),
                resizable=True,
                text_select=True,
                confirm_close=True,
            )
            mgr.desktop_terminals.bind_native(webview)
            window.events.closed += lambda:mgr.desktop_terminals.close_all()
            window.events.closing += lambda: log('WINDOW', '收到窗口 closing 事件')
            window.events.closed += lambda: log('WINDOW', '收到窗口 closed 事件')
            window.events.loaded += lambda: log('WINDOW', '页面 loaded 事件')
            crash_diagnostics.mark('webview-event-loop-enter')
            log('WINDOW', '进入 webview.start')
            webview.start(private_mode=True)
            log('WINDOW', 'webview.start 已返回')
            crash_diagnostics.mark('webview-event-loop-returned')
        except ImportError:
            log('MAIN', f'webview not installed — server running in headless mode (open http://127.0.0.1:{http_port} in browser)')
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        # Cleanup after window closes
        log('MAIN', 'Shutting down...')
        crash_diagnostics.mark('application-shutdown')
        mgr.stop()
        http_srv.shutdown()
        stop_event.wait(timeout=5)
        log('MAIN', 'Shutdown complete')
    except Exception:
        msg = f'FATAL: {traceback.format_exc()}'
        log('MAIN', msg)
        try:
            with open(_crash_dump_path(), 'a', encoding='utf-8') as f:
                f.write(msg)
        except Exception:
            pass
        raise

def main_old():
    http_srv = start_http_server()
    mgr = ManagerServer()
    try:
        asyncio.run(mgr.run())
    except KeyboardInterrupt:
        log('MAIN', 'Shutting down...')
    finally:
        http_srv.shutdown()


if __name__ == '__main__':
    main()
