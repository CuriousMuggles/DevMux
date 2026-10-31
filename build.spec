# -*- mode: python ; coding: utf-8 -*-

import sys
import importlib.util
for _dependency in ('mijiaAPI', 'qrcode', 'Crypto', 'tzlocal', 'requests'):
    if importlib.util.find_spec(_dependency) is None:
        raise RuntimeError(f'缺少米家依赖 {_dependency}，请安装 requirements-mijia.txt')
import os as _os
import shutil
from pathlib import Path

# ---- Detect hidden imports needed by pywebview/pywinpty ----
# pywebview on Windows uses pythonnet (clr), bottle as internal WSGI server
# pywinpty has native .pyd binary; its ConPTY backend spawns OpenConsole.exe
# and the winpty backend spawns winpty-agent.exe — both MUST be bundled or
# every PTY session dies with EOF right after spawn.
import winpty
from app_version import windows_version_resource
_WINPTY_DIR = _os.path.dirname(winpty.__file__)

# 发布目录可能残留上次运行生成的日志；它们不属于发布包。
# commands.json 是用户的持久配置，不打入单文件包，也不在构建时删除。
# 打包前清除运行日志，无法删除时
# 直接中止打包，避免误将残留文件随目录分发。
_RELEASE_DIR = Path(DISTPATH)
for _runtime_name in ('devmux.log', 'adb-manager.log', 'crash_dump.txt'):
    _runtime_file = _RELEASE_DIR / _runtime_name
    try:
        _runtime_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as _cleanup_error:
        raise RuntimeError(f'无法清理发布残留文件: {_runtime_file}') from _cleanup_error

# 将 Android platform-tools 一并打入单文件程序，目标电脑无需另装 adb。
_ADB_DIR = _os.environ.get('ADB_BUNDLE_DIR') or _os.path.dirname(shutil.which('adb') or '')
if not _ADB_DIR or not _os.path.isfile(_os.path.join(_ADB_DIR, 'adb.exe')):
    raise RuntimeError('未找到 adb.exe；请设置 ADB_BUNDLE_DIR 指向 platform-tools 目录。')
if not _os.path.isfile(_os.path.join(_ADB_DIR, 'fastboot.exe')):
    raise RuntimeError('platform-tools 缺少 fastboot.exe，无法打包真实烧写工具。')
for _name in ('mke2fs.exe', 'mke2fs.conf'):
    if not _os.path.isfile(_os.path.join(_ADB_DIR, _name)):
        raise RuntimeError(f'platform-tools 缺少 {_name}，冻结版 fastboot 无法格式化 data。')
_ADB_BINARIES = [
    (_os.path.join(_ADB_DIR, name), 'adb')
    for name in ('adb.exe', 'fastboot.exe', 'mke2fs.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll')
    if _os.path.isfile(_os.path.join(_ADB_DIR, name))
]

block_cipher = None

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[
        (_os.path.join(_WINPTY_DIR, 'OpenConsole.exe'), 'winpty'),
        (_os.path.join(_WINPTY_DIR, 'winpty-agent.exe'), 'winpty'),
    ] + _ADB_BINARIES,
    datas=[
        # Include frontend renderer directory
        ('renderer', 'renderer'),
        ('plugin_sdk/sdk.js', 'plugin_sdk'),
        ('plugin_sdk/ui.css', 'plugin_sdk'),
        ('db_download_host.py', '.'),
        (_os.path.join(_ADB_DIR, 'mke2fs.conf'), 'adb'),
    ],
    hiddenimports=[
        'plugin_sdk',
        'mijiaAPI', 'qrcode.image.svg', 'Crypto.Cipher.ARC4', 'tzlocal',
        # pywebview internals
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'webview.platforms.cef',
        # pythonnet / CLR
        'clr_loader',
        'clr_loader.netfx',
        'pythonnet',
        # bottle (pywebview internal WSGI)
        'bottle',
        'proxy_tools',
        # websockets
        'websockets',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
        'websockets.extensions',
        'websockets.extensions.permessage_deflate',
        # adb_worker
        'adb_worker',
        'serial',
        'serial.serialwin32',
        'serial.tools.list_ports_windows',
        'diagnostics',
        'usb.core',
        'usb.backend.libusb1',
        'usb.backend.libusb0',
        'crcmod',
    ],
    hookspath=[],
    hooksconfig={},
    excludes=[
        'tkinter',       # not used
        'unittest',
        'pytest',
        'pip',
        'setuptools',
        'distutils',
        'idlelib',
        'matplotlib',
        'numpy',
        'PIL',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'scipy',
        'pandas',
        'curses',
        'test',
    ],
    runtime_hooks=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DevMux',
    version=windows_version_resource(),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # No console window for desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
