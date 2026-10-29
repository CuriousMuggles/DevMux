# -*- mode: python ; coding: utf-8 -*-
# Debug build — shows console window for crash diagnostics

import sys
import importlib.util
for _dependency in ('mijiaAPI', 'qrcode', 'Crypto', 'tzlocal', 'requests'):
    if importlib.util.find_spec(_dependency) is None:
        raise RuntimeError(f'缺少米家依赖 {_dependency}，请安装 requirements-mijia.txt')
import os as _os
import shutil
from pathlib import Path

# pywinpty's ConPTY backend spawns OpenConsole.exe, winpty backend spawns
# winpty-agent.exe — both must be bundled or PTY sessions die with EOF.
import winpty
from app_version import windows_version_resource
from relay_probe_build import build_relay_probe
_RELAY_PROBE = build_relay_probe(SPECPATH)
_WINPTY_DIR = _os.path.dirname(winpty.__file__)

# 调试包同样不携带运行日志；commands.json 不打包，也不删除用户配置。
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
_ADB_BINARIES = [
    (_os.path.join(_ADB_DIR, name), 'adb')
    for name in ('adb.exe', 'fastboot.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll')
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
        ('renderer', 'renderer'),
        ('db_download_host.py', '.'),
        (str(_RELAY_PROBE), 'relay'),
        (str(Path(SPECPATH) / 'vendor' / 'usb-relay' / 'bin' / 'usb_relay_device.dll'), 'relay'),
    ],
    hiddenimports=[
        'mijiaAPI', 'qrcode.image.svg', 'Crypto.Cipher.ARC4', 'tzlocal',
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'webview.platforms.cef',
        'clr_loader',
        'clr_loader.netfx',
        'pythonnet',
        'bottle',
        'proxy_tools',
        'websockets',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
        'websockets.extensions',
        'websockets.extensions.permessage_deflate',
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
        'tkinter', 'unittest', 'pytest', 'pip', 'setuptools',
        'distutils', 'idlelib', 'matplotlib', 'numpy', 'PIL',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'scipy',
        'pandas', 'curses', 'test',
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
    name='DevMux_debug',
    version=windows_version_resource(),
    debug=True,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
