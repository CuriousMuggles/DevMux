"""设备标识：同一台设备在 USB / 网络两种接入方式下的稳定键。

单列一个模块，是因为 adb_worker 与 shared_terminal 都需要它，而两者互相
还有别的依赖：谁把它定义在自己身上，谁就把对方拖进环里。
"""
from __future__ import annotations


def make_key(dev: dict) -> str:
    """设备唯一键：优先绑定传输标识，避免换线后 key 漂移。"""
    tid = dev.get('transportId')
    if tid is not None:
        return f"{dev['serial']}:tid={tid}"
    return dev['serial']


__all__ = ['make_key']
