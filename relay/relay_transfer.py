"""共享目录文件中转：单写者包、完整性校验和安全解包。"""
import hashlib
import os
import re
import shutil
import stat
import zipfile
import tempfile
import time
from pathlib import Path

MAX_BYTES = 10 * 1024 ** 3
MAX_ENTRIES = 100000
TIMEOUT = 300.0
KIND_TRANSFER = 'file-transfer'
KIND_CLEANUP = 'file-transfer-cleanup'


def transfer_dir(root, target, source, transfer_id):
    import relay
    relay.check_peer_id(target)
    relay.check_peer_id(source)
    if not re.fullmatch(r'[0-9a-f]{32}', str(transfer_id)):
        raise ValueError('文件传输标识非法')
    base = Path(root).resolve()
    path = base / 'archive' / target / 'transfers' / source / transfer_id
    if path.resolve() != path:
        raise ValueError('中转目录包含链接，已拒绝操作')
    return path


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def linked(path):
    # Python 3.10 尚无 Path.is_junction，直接识别 Windows 重解析点。
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 1024)


def tree(path):
    if linked(path):
        raise ValueError(f'不支持传输链接：{path}')
    yield path
    if path.is_dir():
        for child in path.iterdir():
            yield from tree(child)


def pack(paths, package):
    """来源独立编号，保留文件名和目录结构；完成后才发布包名。"""
    package = Path(package)
    package.parent.mkdir(parents=True, exist_ok=True)
    temp = package.with_suffix('.partial')
    total = count = 0
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED) as out:
            for index, source in enumerate(paths):
                source = Path(source)
                if not source.exists() or linked(source):
                    raise ValueError(f'来源不存在或包含链接：{source}')
                entries = tree(source)
                for entry in entries:
                    if linked(entry):
                        raise ValueError(f'不支持传输链接：{entry}')
                    relative = entry.relative_to(source.parent).as_posix()
                    name = f'{index}/{relative}'
                    count += 1
                    total += entry.stat().st_size if entry.is_file() else 0
                    if total > MAX_BYTES or count > MAX_ENTRIES:
                        raise ValueError('传输超过限制（10 GiB / 100000 项）')
                    out.write(entry, name + '/' if entry.is_dir() else name)
        os.replace(temp, package)
        return {'sha256': digest(package), 'size': package.stat().st_size}
    finally:
        temp.unlink(missing_ok=True)


def publish_upload(paths, package):
    """本机打包校验，共享目录只写一次；完成前只暴露partial文件。"""
    package = Path(package)
    package.parent.mkdir(parents=True, exist_ok=True)
    partial = package.with_suffix('.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='DevMux-upload-') as work:
            staged = Path(work) / 'upload.zip'
            info = pack(paths, staged)
            with open(staged, 'rb') as source, open(partial, 'wb') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(chunk)
                    time.sleep(0)  # 大文件复制期间让心跳读写线程获得执行机会。
                target.flush()
                os.fsync(target.fileno())
            os.replace(partial, package)
            return info
    finally:
        partial.unlink(missing_ok=True)


def receive_package(package, destination, info):
    """共享包顺序读取一次；校验与解压均在本机完成。"""
    package, destination = Path(package), Path(destination)
    expected = info.get('size')
    if not isinstance(expected, int) or expected < 0 or expected > MAX_BYTES + MAX_ENTRIES * 1024:
        raise ValueError('中转包大小非法')
    staged = destination / 'received.zip'
    destination.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(package, 'rb') as source, open(staged, 'wb') as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            size += len(chunk)
            if size > expected:
                raise ValueError('中转文件不完整或校验失败')
            target.write(chunk)
            time.sleep(0)
    return unpack(staged, destination / 'contents', info)


def unpack(package, destination, info):
    """先检查整个包；拒绝路径穿越、Windows 特殊文件名和链接。"""
    package = Path(package)
    if package.stat().st_size != info.get('size') or digest(package) != info.get('sha256'):
        raise ValueError('中转文件不完整或校验失败')
    destination = Path(destination)
    with zipfile.ZipFile(package) as source:
        entries = source.infolist()
        if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError('传输包超过限制')
        seen = set()
        for entry in entries:
            parts = entry.filename.rstrip('/').split('/')
            key = entry.filename.rstrip('/').casefold()
            if (len(parts) < 2 or not parts[0].isdigit() or key in seen
                    or any(not p or p in ('.', '..') or re.search(r'[\\:<>"|?*\x00-\x1f]', p)
                           or p.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', p)
                           for p in parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('中转包包含非法路径或链接')
            seen.add(key)
        source.extractall(destination)
    paths = []
    for directory in sorted(destination.iterdir(), key=lambda p: int(p.name)):
        children = list(directory.iterdir())
        if len(children) != 1:
            raise ValueError('中转包来源结构非法')
        paths.append(children[0])
    if not paths:
        raise ValueError('中转包为空')
    return paths


def save_download(paths, destination):
    destination = Path(destination)
    if len(paths) > 1 and not destination.is_dir():
        raise ValueError('下载多个文件时请选择已有的本机目录')
    for source in paths:
        target = destination / source.name if destination.is_dir() else destination
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
