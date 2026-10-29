"""历史合并归档的只读去重兼容；不再定时扫描、压缩或删除文件。"""
import json
import os
import re
import stat
import threading
import time
import zipfile
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 10000
_MONTH = re.compile(r'^\d{4}-\d{2}$')
_BUNDLE = re.compile(r'^compact-[0-9a-f]{32}\.zip$')
_INDEX = '__index__.json'
_CACHE = {}
_LOCK = threading.RLock()
_BUNDLE_CACHE = {}


def _plain(path, directory=False):
    info = os.stat(path, follow_symlinks=False)
    return (not stat.S_ISLNK(info.st_mode)
            and not getattr(info, 'st_file_attributes', 0) & 1024
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))


def has_archived_result(archive_dir, req_id):
    """压缩后及重启后仍识别已消费回执，避免追加通知重放。"""
    root = Path(os.path.abspath(archive_dir))
    with _LOCK:
        cached = _CACHE.get(str(root))
        if cached and time.monotonic() - cached[0] < 2:
            return req_id in cached[1]
    # 目录和压缩包读取不持缓存锁，避免与后台合并互相阻塞。
    result_ids = set()
    if root.exists() and _plain(root, directory=True):
        for directory in root.iterdir():
            if not _MONTH.fullmatch(directory.name) or not _plain(directory, directory=True):
                continue
            for path in directory.iterdir():
                if not _BUNDLE.fullmatch(path.name) or not _plain(path):
                    continue
                try:
                    with _LOCK:
                        ids = _BUNDLE_CACHE.get(str(path))
                    if ids is None:
                        # 包名不可变；只读索引，不在指令路径解压全部消息。
                        # 唯一包名发布后不再改写，普通随机读取不会遇到同名替换缓存。
                        # ZipFile 只读取目录和索引，避免首次指令下载整个归档包。
                        with zipfile.ZipFile(path) as package:
                            info = package.getinfo(_INDEX)
                            if info.file_size > MAX_BYTES:
                                raise ValueError('归档索引超过读取限制')
                            manifest = json.loads(package.read(_INDEX))
                            if manifest.get('schema') != 1 or len(manifest['records']) > MAX_RECORDS:
                                raise ValueError('归档索引非法')
                            ids = {item['reqId'] for item in manifest['records'] if item.get('result')}
                        with _LOCK:
                            if len(_BUNDLE_CACHE) >= 256:
                                _BUNDLE_CACHE.clear()
                            _BUNDLE_CACHE[str(path)] = ids
                    result_ids.update(ids)
                except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
                    # 索引无法读取时暂停处理，不能把已执行请求误当成新请求重放。
                    raise ValueError('归档去重索引不可用') from exc
    with _LOCK:
        if len(_CACHE) >= 64:
            _CACHE.clear()
        _CACHE[str(root)] = (time.monotonic(), result_ids)
    return req_id in result_ids
