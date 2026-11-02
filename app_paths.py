"""Persistent application data beside the executable; bundled resources stay read-only."""
import hashlib
import os
from pathlib import Path
import sys
import shutil
import json
from contextlib import contextmanager
import threading

_MIGRATION_LOCK = threading.RLock()


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # Unknown/access denied is treated as active.
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _migration_lock(target):
    with _MIGRATION_LOCK, (target / '.migration.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def application_directory():
    return Path(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)).parent


def data_directory():
    base = application_directory()
    target = base / 'devmux-data'
    target.mkdir(parents=True, exist_ok=True)
    # Only known application data is migrated. Existing destinations always win.
    patterns = ('relay*.json', 'commands.json', 'serial-commands.json*',
                'plugins-*', 'windows-jobs-*', 'journal-state',
                'devmux.log', 'adb-manager.log', 'crash_dump.txt', 'diagnostics')
    with _migration_lock(target):
        for lock in base.glob('relay*.lock'):
            if (target / lock.with_suffix('.json').name).exists():
                continue
            try:
                pid = int(json.loads(lock.read_text(encoding='utf-8')).get('pid', 0))
            except (OSError, ValueError, TypeError, AttributeError):
                raise RuntimeError('无法确认旧版 DevMux 状态，请先退出旧版并检查锁文件: ' + str(lock))
            if _pid_alive(pid):
                raise RuntimeError('请先退出旧版 DevMux，再迁移运行数据')
        for pattern in patterns:
            for source in base.glob(pattern):
                destination = target / source.name
                if source.is_symlink() or destination.exists():
                    continue
                shutil.move(str(source), str(destination))
    return target


def storage_identity(config_path):
    path = Path(os.path.abspath(config_path))
    # Preserve the installed-plugin directory identity when default config moves.
    if not os.environ.get('RELAY_CONFIG') and path.parent == application_directory() / 'devmux-data':
        path = application_directory() / path.name
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]
