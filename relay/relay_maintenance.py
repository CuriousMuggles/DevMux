"""共享目录的盘点与清空（换目录时的「沿用 / 清空后重建」）。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import os
import shutil
import stat

from .relay_constants import (
    SLOT_ARCHIVE,
    SLOT_PEERS,
    SLOT_REGISTRY,
    SLOT_REQUESTS,
    SLOT_RESULTS,
    SLOT_STREAMS,
)


# ----------------------------------------------------------------------
# 共享目录的盘点与清空（换目录时的「沿用 / 清空后重建」）
# ----------------------------------------------------------------------
# 用户把共享目录指到一个用过（或别人用过）的文件夹时，里面可能躺着上一轮的
# 心跳、消息队列甚至别的机器的分区。沿用通常没问题（身份靠指纹与确认名单），
# 但用户也可能想从零开始。这里提供**只认中转自有文件**的盘点与清理 —— 绝不
# 碰目录里的其他文件，也绝不删目录本身。


def _slot_dir(root: str, slot: str) -> str:
    return os.path.join(root, slot)


def inspect_relay_root(root: str) -> dict:
    """盘点一个目录里有多少中转自有数据，供前端决定要不要问「沿用还是清空」。"""
    root = str(root or '')
    info = {
        'root': root,
        'exists': bool(root) and os.path.isdir(root),
        'hasData': False,
        'manifest': False,
        'peers': 0,
        'requests': 0,
        'results': 0,
        'streams': 0,
        'archive': 0,
        'registry': 0,
    }
    if not info['exists']:
        return info
    info['manifest'] = os.path.isfile(os.path.join(root, 'relay.json'))
    for slot in (SLOT_PEERS, SLOT_REQUESTS, SLOT_RESULTS, SLOT_STREAMS, SLOT_ARCHIVE, SLOT_REGISTRY):
        try:
            count = len(os.listdir(_slot_dir(root, slot)))
        except OSError:
            count = 0
        info[slot] = count
    info['hasData'] = bool(
        info['manifest'] or info['peers'] or info['requests']
        or info['results'] or info['streams'] or info['archive'] or info['registry']
    )
    return info


def _cache_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _cache_link(path: str) -> bool:
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def validate_cache_root(root: str, protected_paths=()) -> str:
    """校验全量清理的根，拒绝危险祖先和根路径上的目录链接。"""
    value = str(root or '').strip()
    if not value or not os.path.isabs(value):
        raise ValueError('清除缓存必须使用已保存的绝对共享目录')
    if value[:4] in ("\\" * 2 + "?" + "\\", "\\" * 2 + "." + "\\"):
        raise ValueError('共享目录不能使用设备命名空间路径')
    spelled = _cache_path(value)
    target = _cache_path(os.path.realpath(value))
    if os.path.dirname(spelled) == spelled:
        raise ValueError('不能清空盘根或共享根目录')
    protected = [os.path.expanduser('~'), os.environ.get('SystemRoot', '')]
    protected.extend(protected_paths)
    for item in protected:
        if not item:
            continue
        for path in (_cache_path(str(item)), _cache_path(os.path.realpath(str(item)))):
            try:
                contains = os.path.commonpath((target, path)) == target
            except ValueError:
                contains = False  # 不同盘符没有交集。
            if contains:
                raise ValueError('共享目录包含本机程序、配置或系统目录，不能全量清除')
    current = spelled
    while os.path.dirname(current) != current:
        if os.path.lexists(current) and _cache_link(current):
            raise ValueError('共享目录根路径不能经过符号链接或目录联接点')
        current = os.path.dirname(current)
    if os.path.lexists(target) and not os.path.isdir(target):
        raise ValueError('共享目录不是文件夹')
    return target


def clean_relay_root(root: str) -> list[str]:
    """清空目录里的中转自有数据，返回删除的相对路径（供界面流水展示）。

    白名单制：只删 ``relay.json``（布局清单）和六个槽目录；目录本身和其它任何
    文件一概不碰。目录不存在或没有任何中转数据时返回空列表（不报错 —— 对调用
    方来说「本来就干净」和「没删成」都应该继续走保存流程，由前端提示差异）。

    注意 ``registry/`` 也在白名单里：清了共享目录就等于把池成员名册一起作废，这是
    「清空后重建」的应有语义（两端都停时才会有人这么做）。槽位由租约回收，不依赖
    这份名册被保留。
    """
    root = str(root or '')
    if not root or not os.path.isdir(root):
        return []
    removed: list[str] = []
    manifest = os.path.join(root, 'relay.json')
    if os.path.isfile(manifest):
        try:
            os.remove(manifest)
            removed.append('relay.json')
        except OSError:
            pass
    for slot in (SLOT_PEERS, SLOT_REQUESTS, SLOT_RESULTS, SLOT_STREAMS, SLOT_ARCHIVE, SLOT_REGISTRY):
        path = _slot_dir(root, slot)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
            removed.append(slot + '/')
        except OSError:
            continue  # 被占用（有实例正在用它）就留着 —— 清不干净不算致命
    return removed
