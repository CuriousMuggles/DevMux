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

# 运行数据（devmux.log、adb-manager.log、crash_dump.txt、commands.json、diagnostics）
# 统一位于 devmux-data，不属于发布包；构建不得清理用户已有运行数据。

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
        ('assets/DevMux.ico', 'assets'),   # 运行时窗口图标（webview.start(icon=...)）
        ('plugin_sdk/sdk.js', 'plugin_sdk'),
        ('plugin_sdk/ui.css', 'plugin_sdk'),
        ('db_download_host.py', '.'),
        (_os.path.join(_ADB_DIR, 'mke2fs.conf'), 'adb'),
    ],
    hiddenimports=[
        'plugin_devices',
        'plugin_sdk',
        # 只在打 usb-relay 插件时延迟导入，静态分析看不到；缺了会在打包插件时 ImportError。
        'relay.relay_probe_build',
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
    icon='assets/DevMux.ico',
)

# Official packages are released separately beside the executable.
from release_plugins import build_official_plugins
build_official_plugins(DISTPATH)
