"""应用版本与桌面标题；不用于中转或 MCP 协议协商。"""

APP_VERSION = '3.0.1'
FILE_VERSION = (3, 0, 1, 0)
APP_TITLE = f'DevMux v{APP_VERSION}'


def windows_version_resource():
    """为正式/调试构建提供相同的 Windows 文件版本资源。"""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct, VSVersionInfo,
    )
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=FILE_VERSION, prodvers=FILE_VERSION,
                          mask=0x3f, flags=0, OS=0x40004, fileType=1,
                          subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('FileDescription', APP_TITLE),
            StringStruct('FileVersion', '.'.join(map(str, FILE_VERSION))),
            StringStruct('ProductName', 'DevMux'),
            StringStruct('ProductVersion', APP_VERSION),
        ])]), VarFileInfo([VarStruct('Translation', [0x0409, 1200])])],
    )
