"""内置 ADB 的持久运行目录，避免常驻服务占用单文件解包目录。"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


ADB_RUNTIME_FILES = ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll')


def _digest(path):
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        return digest.hexdigest()


def _verify(directory, expected):
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file() or _digest(path) != digest:
            raise RuntimeError(f'ADB 运行缓存校验失败：{path}')


def persistent_adb_path(bundle_directory):
    """按内容分版、原子发布；复用时不改写可能正被其他实例使用的文件。"""
    source = Path(bundle_directory)
    expected = {name: _digest(source / name) for name in ADB_RUNTIME_FILES
                if (source / name).is_file()}
    if 'adb.exe' not in expected:
        raise RuntimeError(f'内置 ADB 文件缺失：{source / "adb.exe"}')
    signature = hashlib.sha256()
    for name, digest in expected.items():
        signature.update(f'{name}:{digest}\n'.encode('ascii'))
    override = (os.environ.get('DEVMUX_TOOL_CACHE') or
                os.environ.get('ADB_MANAGER_TOOL_CACHE'))
    root = (Path(override) if override else
            Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) /
            'ADBManager' / 'platform-tools').resolve()
    destination = root / signature.hexdigest()
    staging = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix='.prepare-', dir=root))
            for name in expected:
                shutil.copyfile(source / name, staging / name)
            _verify(staging, expected)
            try:
                staging.rename(destination)
                staging = None
            except OSError:
                # 另一实例可能已发布同一版本；必须核验，不能忽略真实写入失败。
                if not destination.is_dir():
                    raise
        _verify(destination, expected)
        return str(destination / 'adb.exe')
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f'无法准备内置 ADB 持久运行目录 {destination}：{exc}') from exc
    finally:
        # 只回收本次创建、位于缓存根下的暂存目录，不清理已发布或其他版本。
        if staging is not None and staging.parent == root:
            shutil.rmtree(staging)
