"""在独立辅助进程运行原 USB 下载脚本，不修改固化包。"""
import os
from pathlib import Path
import runpy
import sys


def run_host(arguments):
    # windowed EXE 的 sys.stdout 为 None，恢复继承的管道，保证原脚本连续打印。
    if os.name == 'nt':
        from pty_host import _stdio
        sys.stdout = _stdio('stdout', -11, 'wb')
        sys.stderr = _stdio('stderr', -12, 'wb')
    else:
        sys.stdout.reconfigure(encoding='utf-8', write_through=True)
        sys.stderr.reconfigure(encoding='utf-8', write_through=True)
    script = Path(arguments[0]).resolve()
    if script.name != 'download_image.py' or script.parent.name != 'scripts' or not script.is_file():
        raise ValueError('仅允许固化包 scripts/download_image.py 下载入口')
    import usb.core
    import usb.backend.libusb1 as libusb1
    import usb.backend.libusb0 as libusb0
    import crcmod  # noqa: F401，预检原脚本依赖
    # 原包可能仅在 udump 子目录提供 DLL；辅助进程只读加载，无需复制或改名。
    for filename, module in [('libusb-1.0.dll', libusb1), ('libusb0.dll', libusb0)]:
        candidates = [script.parent / filename, script.parent / 'udump' / filename]
        for dll in candidates:
            if dll.is_file():
                backend = module.get_backend(find_library=lambda _name, file=str(dll): file)
                if backend is not None:
                    original = module.get_backend
                    module.get_backend = lambda *a, _original=original, _backend=backend, **kw: _backend if not a and not kw else _original(*a, **kw)
                    break
    find = usb.core.find
    def unique_device(*args, **kwargs):
        # 原脚本不支持 USB 序列号，USB 下载阶段必须只有一块同型号板。
        # 0x3902 为 U-Boot 下载阶段，0x696d 为 p300-udp 的 SPL/BootROM 下载阶段。
        if not kwargs.get('find_all') and kwargs.get('idVendor') in (0x3902, 0x696D):
            devices = list(find(*args, **{**kwargs, 'find_all': True}))
            if len(devices) > 1:
                print('发现多块 USB 下载设备，请只连接一块测试板', file=sys.stderr)
                raise SystemExit(1)
            return devices[0] if devices else None
        return find(*args, **kwargs)
    usb.core.find = unique_device
    sys.argv = [str(script), *arguments[1:]]
    sys.dont_write_bytecode = True
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    run_host(sys.argv[1:])
