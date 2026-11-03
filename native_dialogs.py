"""原生 Windows 文件/目录对话框（COM IFileOpenDialog，纯 ctypes）。

IFileDialog::Show 自带模态消息泵，可从任意工作线程调用，因此源码运行与冻结版
exe 都可用（冻结版无法再启动 python 子进程）。

从 server.py 摘出：它不依赖服务端任何状态。日志与窗口标题沿用主程序的
既有定义（adb_worker.log / app_version.APP_TITLE），便于在 devmux.log 里
按 PICK 分类追溯对话框行为。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

from adb_worker import log
from app_version import APP_TITLE

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


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
