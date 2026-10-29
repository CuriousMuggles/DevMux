"""Windows 共享目录读取：绕过 SMB 陈旧数据缓存，保留本地文件普通读取。"""
from __future__ import annotations
import os


def is_remote(path: str) -> bool:
    """UNC 与映射网络盘都走同一读取路径。"""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    absolute = os.path.abspath(path)
    if absolute.startswith('\\\\'):
        return True
    drive = os.path.splitdrive(absolute)[0]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    return bool(drive and api.GetDriveTypeW(drive + '\\') == 4)


def stat(path: str):
    """网络文件重新打开原生句柄，避免目录枚举缓存把活心跳判离线。"""
    if is_remote(path):
        return _stat_uncached(path)
    return os.stat(path)


def _stat_uncached(path: str):
    import ctypes
    from ctypes import wintypes as w
    from types import SimpleNamespace
    class FileInfo(ctypes.Structure):
        _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME),
                    ('accessed', w.FILETIME), ('written', w.FILETIME),
                    ('volume', w.DWORD), ('sizeHigh', w.DWORD), ('sizeLow', w.DWORD),
                    ('links', w.DWORD), ('indexHigh', w.DWORD), ('indexLow', w.DWORD)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FileInfo)]
    api.GetFileInformationByHandle.restype = w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    handle = api.CreateFileW(os.path.abspath(path), 0x80, 7, None, 3, 0x20000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileInfo()
        if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        stamp = (info.written.dwHighDateTime << 32) | info.written.dwLowDateTime
        return SimpleNamespace(st_size=(info.sizeHigh << 32) | info.sizeLow,
                               st_ino=(info.indexHigh << 32) | info.indexLow,
                               st_mtime=stamp / 10000000 - 11644473600)
    finally:
        api.CloseHandle(handle)


def read_bytes(path: str, offset: int = 0, limit: int | None = None) -> bytes:
    """网络读使用对齐缓冲区；逻辑偏移与长度仍可为任意字节。失败不退回旧缓存。"""
    if not is_remote(path):
        with open(path, 'rb') as handle:
            handle.seek(offset)
            return handle.read() if limit is None else handle.read(limit)
    return _read_uncached(path, offset, limit)


def _read_uncached(path: str, offset: int = 0, limit: int | None = None) -> bytes:
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                               w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, w.DWORD, w.DWORD]
    api.VirtualAlloc.restype = ctypes.c_void_p
    api.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, w.DWORD]
    api.VirtualFree.restype = w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    api.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong, ctypes.c_void_p, w.DWORD]
    api.SetFilePointerEx.restype = w.BOOL
    api.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                            ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    api.ReadFile.restype = w.BOOL
    # 允许写入端原子替换文件；64 KiB 地址、长度和起始偏移对齐。
    block = 65536
    skip = offset % block
    handle = api.CreateFileW(os.path.abspath(path), 0x80000000, 7, None, 3, 0x20000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = None
    try:
        buffer = api.VirtualAlloc(None, block, 0x3000, 4)
        if not buffer:
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.SetFilePointerEx(handle, offset - skip, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        parts = []
        remaining = None if limit is None else max(0, limit)
        while remaining is None or remaining > 0:
            count = w.DWORD()
            if not api.ReadFile(handle, buffer, block, ctypes.byref(count), None):
                error = ctypes.get_last_error()
                if error == 38:  # ERROR_HANDLE_EOF
                    break
                raise ctypes.WinError(error)
            data = ctypes.string_at(buffer, count.value)[skip:]
            skip = 0
            if remaining is not None:
                data = data[:remaining]
                remaining -= len(data)
            parts.append(data)
            if count.value < block:
                break
        return b''.join(parts)
    finally:
        if buffer:
            api.VirtualFree(buffer, 0, 0x8000)
        api.CloseHandle(handle)
