"""Windows ADB中文目录下载兼容；设备只读，失败不发布下载包。"""
import os
import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath
import relay_transfer

LIST_LIMIT = 16 * 1024 * 1024


def _parts(value):
    parts = PurePosixPath(value).parts
    if not parts or any(not p or p in ('.', '..') or re.search(r'[\\:<>"|?*\x00-\x1f]', p)
            or p.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', p)
            for p in parts):
        raise ValueError('设备目录包含Windows不支持的文件名')
    return parts


def _inventory(run, remote):
    quoted = shlex.quote(remote)
    commands = []
    for tag, selector in [('D', '-type d'), ('F', '-type f'), ('L', '-type l'),
                          ('X', '! -type d ! -type f ! -type l')]:
        commands.extend(["printf '" + tag + "\\000'", f'find {quoted} {selector} -print0 || exit 23'])
    # NUL分隔支持空格/中文；缺结束标记、命令失败或达到截断上限均拒绝。
    script = '{ ' + '; '.join(commands) + "; printf 'E\\000'; } | head -c " + str(LIST_LIMIT + 1)
    proc = run(['shell', script], raw=True)
    raw = proc.stdout
    if not isinstance(raw, bytes) or len(raw) > LIST_LIMIT or not raw.endswith(b'E\0'):
        raise ValueError('设备目录枚举失败、超过限制或结果不完整')
    tokens = raw[:-1].split(b'\0')
    groups = {tag: [] for tag in ('D', 'F', 'L', 'X')}
    current = None
    expected_tags = iter(('D', 'F', 'L', 'X', 'E'))
    for token in tokens:
        if token in (b'D', b'F', b'L', b'X'):
            tag = token.decode('ascii')
            if tag != next(expected_tags, None):
                raise ValueError('设备目录枚举标记非法')
            current = tag
        elif token == b'E':
            if current != 'X' or next(expected_tags, None) != 'E':
                raise ValueError('设备目录枚举未完成')
            current = 'E'
        else:
            if current not in groups:
                raise ValueError('设备目录枚举结构非法')
            value = token.decode('utf-8', errors='strict')
            if not value.startswith('/') or '\x00' in value or posixpath.normpath(value) != value:
                raise ValueError('设备目录包含非法路径')
            groups[current].append(value)
    if current != 'E' or groups['L'] or groups['X']:
        raise ValueError('不支持下载链接、特殊文件或不完整目录')
    if sum(map(len, groups.values())) > relay_transfer.MAX_ENTRIES:
        raise ValueError('传输超过100000项限制')
    return groups


def _flat_pull(run, remote, destination):
    remote = posixpath.normpath(remote)
    if remote == '/':
        raise ValueError('请指定设备根目录内需要下载的文件夹')
    entries = _inventory(run, remote)
    if not entries['D'] or entries['D'][0] != remote:
        raise ValueError('设备目录身份无法确认')
    base = PurePosixPath(remote)
    name = base.name
    _parts(name)
    mappings, seen = [], set()
    for kind in ('D', 'F'):
        for value in entries[kind]:
            try:
                relative = PurePosixPath(value).relative_to(base)
            except ValueError as exc:
                raise ValueError('设备枚举路径越界') from exc
            parts = () if value == remote else _parts(relative.as_posix())
            key = '/'.join(parts).casefold()
            if key in seen:
                raise ValueError('设备文件名在Windows上冲突')
            seen.add(key)
            mappings.append((kind, value, parts))
    directories = set(entries['D'])
    if any(posixpath.dirname(value) not in directories for kind, value, parts in mappings if parts):
        raise ValueError('设备目录枚举缺少父目录')
    destination.mkdir(exist_ok=False)
    result = destination / name
    for kind, value, parts in sorted(mappings, key=lambda item: len(item[2])):
        local = result.joinpath(*parts)
        if not local.resolve().is_relative_to(destination.resolve()):
            raise ValueError('本机下载路径越界')
        if kind == 'D':
            local.mkdir(exist_ok=False)
    total = 0
    for index, (kind, value, parts) in enumerate(mappings):
        if kind != 'F':
            continue
        # ADB只创建ASCII文件名，Unicode目录和最终文件名由Python创建。
        staging = destination / f'file-{index:08d}.bin'
        run(['pull', value, str(staging)])
        if not staging.is_file() or staging.is_symlink():
            raise ValueError('ADB未生成预期的下载文件')
        total += staging.stat().st_size
        if total > relay_transfer.MAX_BYTES:
            raise ValueError('传输超过10GiB限制')
        staging.replace(result.joinpath(*parts))
    return result


def pull_to_temp(run, remote, destination, *, windows=None):
    """保留原快速路径；仅确定的Windows递归编码失败启用兼容下载。"""
    destination = Path(destination)
    try:
        run(['pull', remote, str(destination)])
    except RuntimeError as exc:
        if not (os.name == 'nt' if windows is None else windows) or 'illegal byte sequence' not in str(exc).lower():
            raise
        # 原失败产物留在独立临时树，由调用方统一回收，不覆盖或发布半成品。
        return _flat_pull(run, remote, destination.with_name(destination.name + '-unicode'))
    children = list(destination.iterdir())
    if len(children) != 1:
        raise ValueError('下载产物结构异常')
    return children[0]
