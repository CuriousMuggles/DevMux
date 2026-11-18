"""替换进程：以 ``--apply-update`` 启动，等主程序退出后把暂存的新版搬到安装目录。

主程序无法替换自己——Windows 上运行中的 exe 被锁定，既不能覆盖也不能删除，只能
改名。所以退出前把自身复制到临时目录，由这个副本完成替换并启动新版。副本运行在
另一份文件上，不会锁住目标 exe。

本模块只依赖标准库：它在主后端初始化之前执行，不能牵连 pywebview / adb 等重量级导入。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

# PyInstaller onefile 通过环境变量把解压目录传给子进程。副本若带着它启动新 exe，
# 新实例会复用旧实例的临时目录（server.py 的 run() 注释里记着这个坑），旧进程
# 退出清理后新窗口直接 404。所有以 _PYI / _MEIPASS 开头的变量都必须剥掉。
INHERITED_MARKERS = ('_PYI', '_MEIPASS')

UNLOCK_TIMEOUT_SECONDS = 15
START_RETRIES = 3
START_RETRY_DELAY_SECONDS = 2
PLUGIN_SUFFIX = '.dmp'
METADATA_FILES = ('BUILD-INFO.json', 'SHA256SUMS.txt')


def clean_environment():
    """给新实例的不含 PyInstaller 痕迹的环境。"""
    return {key: value for key, value in os.environ.items()
            if not any(key.startswith(marker) for marker in INHERITED_MARKERS)}


def write_result(path, payload):
    """结果先写临时文件再替换，避免中断留下半截 JSON。"""
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    payload = dict(payload, finishedAt=time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        return
    return payload


def exclusive_ready(path):
    """能以独占方式打开就说明锁已释放。

    任务管理器里进程消失了并不等于文件解锁：单文件打包是引导进程 + Python 子进程
    两层，父进程退出后子进程可能还在收尾。以能否写打开作为判据才可靠。
    """
    try:
        with open(path, 'r+b'):
            return True
    except OSError:
        return False


def wait_unlocked(path, timeout=UNLOCK_TIMEOUT_SECONDS, interval=0.25):
    path = Path(path)
    deadline = time.monotonic() + timeout
    while True:
        if not path.exists():
            return True
        if exclusive_ready(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def install_plugins(staged, target):
    """只搬官方插件包；暂存目录里出现别的文件一律忽略。"""
    source = Path(staged) / 'plugin'
    if not source.is_dir():
        return []
    destination = Path(target) / 'plugin'
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    for package in sorted(source.glob('*' + PLUGIN_SUFFIX)):
        shutil.copy2(package, destination / package.name)
        installed.append(package.name)
    return installed


def install_metadata(staged, target):
    """把构建信息与校验清单换成新版；暂存目录缺这两份时保持原样。"""
    installed = []
    for name in METADATA_FILES:
        source = Path(staged) / name
        if source.is_file():
            shutil.copy2(source, Path(target) / name)
            installed.append(name)
    return installed


def swap(staged, target, result_path=None):
    """改名 → 落新文件 → 启动新版；任一步失败都把旧版改回来。

    返回结果字典，同时（若给了 result_path）落盘供下次启动读取。
    """
    staged, target = Path(staged), Path(target)
    executable = target / 'DevMux.exe'
    incoming = staged / 'DevMux.exe'
    if not incoming.is_file():
        raise ValueError('暂存目录缺少 DevMux.exe')
    backup = target / ('DevMux.exe.old-' + time.strftime('%Y%m%d%H%M%S'))
    renamed = False
    try:
        if executable.exists():
            if not wait_unlocked(executable):
                raise TimeoutError('等待主程序退出超时，已保留下载包')
            executable.rename(backup)
            renamed = True
        shutil.copy2(incoming, executable)
    except Exception as error:
        if renamed and backup.exists() and not executable.exists():
            try:
                backup.rename(executable)
            except OSError:
                pass
        if result_path:
            return write_result(result_path, {'status': 'failed', 'error': str(error)})
        raise
    plugins = install_plugins(staged, target)
    started = launch(executable, target)
    if not started:
        # 落盘成功但起不来同样算失败：把新版撤掉、旧版改回来，别留一个打不开的安装目录。
        try:
            if executable.exists():
                executable.unlink()
            if backup.exists():
                backup.rename(executable)
        except OSError:
            pass
        payload = {'status': 'rolled-back', 'error': '新版本无法启动，已恢复上一版本',
                   'plugins': plugins, 'target': str(executable)}
        if result_path:
            write_result(result_path, payload)
        return payload
    # 新版已经起来了再换元数据：安装目录的 BUILD-INFO/SHA256SUMS 要跟着版本走，
    # 否则目录里还写着旧版本，事后核对升级结果只能靠窗口标题和 exe 摘要。
    install_metadata(staged, target)
    payload = {'status': 'installed', 'started': True,
               'backup': backup.name if backup.exists() else '',
               'plugins': plugins, 'target': str(executable)}
    if result_path:
        write_result(result_path, payload)
    return payload


def launch(executable, working_directory):
    """启动新版；被杀软实时扫描短暂占用时重试，仍失败才算升级失败。"""
    for attempt in range(START_RETRIES):
        try:
            subprocess.Popen([str(executable)], cwd=str(working_directory),
                             env=clean_environment(), close_fds=True,
                             creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
            return True
        except OSError:
            if attempt == START_RETRIES - 1:
                return False
            time.sleep(START_RETRY_DELAY_SECONDS)
    return False


def run(argv):
    """``--apply-update <暂存目录> <安装目录> <结果文件>``；退出码供日志排查。"""
    if len(argv) < 3:
        return 2
    staged, target, result_path = argv[0], argv[1], argv[2]
    try:
        result = swap(staged, target, result_path)
    except Exception as error:
        write_result(result_path, {'status': 'failed', 'error': str(error)})
        return 1
    return 0 if result.get('status') == 'installed' else 1


if __name__ == '__main__':
    sys.exit(run(sys.argv[1:]))
