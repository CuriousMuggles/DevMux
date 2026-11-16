"""主程序在线升级：检测清单 → 下载校验 → 解压暂存 → 退出时替换并重启。

与插件升级的分工：插件是子进程，停掉即可原地替换，全程不重启应用；主程序是正在
运行的 exe，只能退出后换文件。所以这里只负责「把包准备好」，真正的替换交给
update_apply（以 --apply-update 启动的自身副本）。

检测失败一律静默：断网、代理、公司网络的机器每天都会遇到一次拉不到清单，弹窗报错
只会让用户学会无视这个功能。失败时退回上次成功的结果，界面停在「已知的最新状态」。
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from app_paths import application_directory
from app_version import APP_VERSION
from update_feed import build_request, fetch_manifest
import update_apply
import versions

LOGGER = logging.getLogger(__name__)

# 启动后先等一会儿再查：把网络请求错开启动时的 ADB 枚举和窗口初始化。
STARTUP_DELAY_SECONDS = 30
CHECK_INTERVAL_SECONDS = 12 * 3600
CACHE_SECONDS = 6 * 3600
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4000
STAGED_FILES = ('DevMux.exe', 'BUILD-INFO.json', 'SHA256SUMS.txt')


def _newer(candidate, current):
    try:
        return versions.is_newer(candidate, current)
    except ValueError:
        return False


class AppUpdater:
    """检测、下载与暂存；替换动作由 update_apply 在进程退出后执行。"""

    def __init__(self, data_dir, install_dir=None, clock=time.time):
        self.data_dir = Path(data_dir)
        self.install_dir = Path(install_dir) if install_dir else application_directory()
        self.state_path = self.data_dir / 'update-state.json'
        self.updates_dir = self.data_dir / 'updates'
        self.result_path = self.updates_dir / 'update-result.json'
        self.restart_flag = self.data_dir / 'update-restart.json'
        self.clock = clock
        self._lock = threading.RLock()

    # ---------- 状态 ----------

    def _load(self):
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, value):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.state_path)
        except OSError:
            LOGGER.warning('更新状态写入失败', exc_info=True)

    def status(self):
        """不发网络的完整状态：界面打开时先渲染它，再后台补一次检测。"""
        with self._lock:
            state = self._load()
            report = state.get('report') if isinstance(state.get('report'), dict) else {}
            result = state.get('result') if isinstance(state.get('result'), dict) else None
            if result is not None:
                result = dict(result, acknowledged=bool(result.get('acknowledged')))
            data = dict(report)
            data.update({
                'current': APP_VERSION,
                'available': False,
                'pending': self._pending(state),
                'result': result,
                'beta': bool(state.get('beta')),
            })
            latest = report.get('latest')
            if latest and _newer(latest, APP_VERSION):
                data['available'] = not self._is_skipped(state, latest)
                data['skipped'] = self._is_skipped(state, latest)
            return data

    @staticmethod
    def _is_skipped(state, version):
        skipped = state.get('skipped')
        return isinstance(skipped, list) and version in skipped

    def _pending(self, state):
        pending = state.get('pending')
        if not isinstance(pending, dict):
            return None
        staged = Path(str(pending.get('staged') or ''))
        if not (staged / 'DevMux.exe').is_file():
            return None
        return pending

    # ---------- 检测 ----------

    def check(self, force=False):
        """拉清单并给出结论；网络失败时退回上次结果并标记 error，不抛异常。"""
        with self._lock:
            state = self._load()
            checked = state.get('checkedAt')
            cached = state.get('manifest')
            stale = not isinstance(checked, (int, float)) or self.clock() - checked >= CACHE_SECONDS
            error = None
            if force or stale or not isinstance(cached, dict):
                try:
                    manifest, source = fetch_manifest()
                    state['manifest'] = manifest
                    state['source'] = source
                    state['checkedAt'] = self.clock()
                except RuntimeError as failure:
                    error = str(failure)
                    LOGGER.info('更新检测失败: %s', failure)
            manifest = state.get('manifest') if isinstance(state.get('manifest'), dict) else None
            report = self._evaluate(manifest, state)
            if error:
                report['error'] = error
            state['report'] = report
            self._save(state)
            data = self.status()
            data.update(report)
            data['pending'] = self._pending(state)
            data['available'] = report.get('available') and not self._is_skipped(state, report.get('latest'))
            return data

    def _evaluate(self, manifest, state):
        if manifest is None:
            return {'available': False, 'latest': None, 'notes': '', 'checkedAt': None}
        # 候选构建的清单会随产物一起流出，其中的包未必走过完整验证，不能驱动升级。
        if manifest.get('status') not in (None, 'release'):
            return {'available': False, 'latest': None, 'checkedAt': state.get('checkedAt'),
                    'error': '清单来自候选构建（' + str(manifest.get('status')) + '），已忽略'}
        channel = manifest.get('channel') or 'stable'
        if channel != 'stable' and not state.get('beta'):
            return {'available': False, 'latest': manifest.get('version'), 'channel': channel,
                    'checkedAt': state.get('checkedAt'),
                    'error': '该清单属于测试通道，未开启接收测试版'}
        latest = manifest.get('version')
        assets = manifest.get('assets') if isinstance(manifest.get('assets'), dict) else {}
        asset = assets.get('windows') if isinstance(assets.get('windows'), dict) else {}
        minimum = manifest.get('minSupportedVersion')
        required = bool(minimum) and not _newer(APP_VERSION, minimum) and APP_VERSION != minimum
        return {
            'available': bool(latest) and _newer(latest, APP_VERSION),
            'latest': latest, 'channel': channel,
            'notes': self._notes(manifest), 'size': asset.get('size'),
            'sha256': asset.get('sha256'), 'urls': asset.get('urls') or [],
            'fileName': asset.get('name') or ('DevMux-' + str(latest) + '-windows.zip'),
            'releasedAt': manifest.get('releasedAt'), 'required': required,
            'minSupportedVersion': minimum, 'checkedAt': state.get('checkedAt'),
        }

    @staticmethod
    def _notes(manifest):
        value = manifest.get('notes')
        if isinstance(value, str):
            return value[:2000]
        if isinstance(value, list):
            return '\n'.join(str(item) for item in value[:40])[:2000]
        return ''

    def skip(self, version):
        with self._lock:
            if not version:
                raise ValueError('缺少版本号')
            state = self._load()
            skipped = [item for item in state.get('skipped') or [] if item != version]
            skipped.append(version)
            state['skipped'] = skipped[-20:]
            self._save(state)
            return {'skipped': state['skipped']}

    def set_beta(self, enabled):
        with self._lock:
            state = self._load()
            state['beta'] = bool(enabled)
            self._save(state)
            return {'beta': state['beta']}

    def ack_result(self):
        """界面上已经提示过替换结果，标记后不再重复弹。"""
        with self._lock:
            state = self._load()
            result = state.get('result')
            if isinstance(result, dict):
                result = dict(result, acknowledged=True)
                state['result'] = result
                self._save(state)
            return {'acknowledged': True}

    # ---------- 下载与暂存 ----------

    def download(self, progress=None):
        """下载 → SHA-256 校验 → 白名单解压到 updates/<ver>/staged。

        网络与解压放在锁外：33MB 的下载不该把 status / 关闭窗口这类请求一起卡住。
        """
        with self._lock:
            state = self._load()
            report = state.get('report') if isinstance(state.get('report'), dict) else {}
            if not report.get('available'):
                raise ValueError('当前没有可用的新版本')
            version = report.get('latest')
            urls = [url for url in report.get('urls') or [] if isinstance(url, str) and url]
            if not urls:
                raise ValueError('清单没有提供下载地址')
            target = self.updates_dir / str(version)
            staged = target / 'staged'
            archive = target / 'package.zip'
            self._clean_dir(target, keep=())
            target.mkdir(parents=True, exist_ok=True)
        error = None
        for url in urls:
            try:
                self._fetch(url, archive, report, progress)
                error = None
                break
            except Exception as failure:
                error = str(failure)
                LOGGER.warning('更新包下载失败 %s: %s', url, failure)
        if error:
            self._clean_dir(target, keep=())
            raise RuntimeError('更新包下载失败（' + error + '）')
        try:
            self._extract(archive, staged, version)
        except Exception:
            self._clean_dir(target, keep=())
            raise
        archive.unlink(missing_ok=True)
        pending = {'version': version, 'staged': str(staged), 'size': report.get('size'),
                   'sha256': report.get('sha256'), 'stagedAt': self.clock(),
                   'notes': report.get('notes') or ''}
        with self._lock:
            state = self._load()
            state['pending'] = pending
            self._save(state)
            self._prune_versions(keep=version)
        return pending

    def _fetch(self, url, archive, report, progress):
        expected = str(report.get('sha256') or '').lower()
        expected_size = report.get('size')
        digest = hashlib.sha256()
        received = 0
        temporary = archive.with_suffix('.part')
        try:
            with urllib.request.urlopen(build_request(url), timeout=30) as response, \
                    open(temporary, 'wb') as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_DOWNLOAD_BYTES:
                        raise ValueError('更新包过大')
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress:
                        progress(received, expected_size)
            if expected and digest.hexdigest() != expected:
                raise ValueError('SHA-256 校验失败')
            if isinstance(expected_size, int) and expected_size and received != expected_size:
                raise ValueError('更新包大小不符')
            temporary.replace(archive)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _extract(self, archive, staged, version):
        """只解压白名单条目；包里的文档按设计忽略，非法路径与软链接一律拒绝。

        正式包里有 README、使用说明等文档，它们不需要进入安装目录，但也不能因此
        判定整包不可用。真正要拒绝的是「路径本身有问题」——那说明包不可信。
        """
        self._clean_dir(staged, keep=())
        staged.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('更新包文件过多')
            total = 0
            selected = []
            for item in infos:
                name = item.filename
                if item.is_dir():
                    continue
                if not self._safe_path(name):
                    raise ValueError('更新包包含非法路径: ' + name)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('更新包包含符号链接')
                if not self._allowed(name):
                    continue
                total += item.file_size
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError('更新包解压后过大')
                selected.append(item)
            bundle.extractall(staged, members=selected)
        if not (staged / 'DevMux.exe').is_file():
            raise ValueError('更新包缺少 DevMux.exe')
        try:
            info = json.loads((staged / 'BUILD-INFO.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('更新包缺少构建信息')
        if str(info.get('version')) != str(version):
            raise ValueError('更新包版本与清单不一致')
        if info.get('status') not in (None, 'release'):
            raise ValueError('更新包来自候选构建')

    @staticmethod
    def _safe_path(name):
        if '\\' in name or name.startswith('/') or ':' in name:
            return False
        parts = name.split('/')
        return all(part not in ('', '.', '..') and not part.endswith((' ', '.')) for part in parts)

    @staticmethod
    def _allowed(name):
        if name in STAGED_FILES:
            return True
        return name.startswith('plugin/') and name.endswith('.dmp') and '/' not in name[len('plugin/'):]

    @staticmethod
    def _clean_dir(directory, keep=()):
        directory = Path(directory)
        if not directory.exists():
            return
        for entry in directory.iterdir():
            if entry.name in keep:
                continue
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            except OSError:
                LOGGER.warning('清理更新缓存失败: %s', entry, exc_info=True)

    def _prune_versions(self, keep=None):
        if not self.updates_dir.exists():
            return
        cutoff = time.time() - 1800
        for entry in self.updates_dir.iterdir():
            if not entry.is_dir() or entry.name == str(keep):
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            self._clean_dir(entry)
            try:
                entry.rmdir()
            except OSError:
                pass

    def cleanup(self):
        """启动时清理残缺文件；半小时内动过的目录留给可能正在下载的另一个实例。

        本项目支持多开且共用 devmux-data：后启动的实例不能把先启动实例下到一半的包删掉。
        """
        cutoff = time.time() - 1800
        with self._lock:
            state = self._load()
            pending = self._pending(state)
            if pending is None:
                state.pop('pending', None)
                self._save(state)
            if not self.updates_dir.exists():
                return
            for entry in self.updates_dir.iterdir():
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                if entry.is_dir() and pending and entry.name == str(pending['version']):
                    for leftover in entry.rglob('*.part'):
                        self._discard(leftover)
                    continue
                self._clean_dir(entry)
                if entry.is_dir():
                    try:
                        entry.rmdir()
                    except OSError:
                        pass
            for leftover in self.updates_dir.rglob('*.part'):
                try:
                    if leftover.stat().st_mtime < cutoff:
                        self._discard(leftover)
                except OSError:
                    pass

    @staticmethod
    def _discard(path):
        try:
            Path(path).unlink()
        except OSError:
            pass

    # ---------- 退出与替换 ----------

    def arm_restart(self):
        """记下「退出后要换文件」；真正的替换发生在主进程收尾之后。"""
        with self._lock:
            state = self._load()
            pending = self._pending(state)
            if pending is None:
                raise ValueError('没有已下载完成的更新包')
            # 目录不可写（装在 Program Files、只读共享、U 盘写保护）时应在退出前就告知，
            # 而不是让用户点了重启、程序关了、然后什么都没发生。
            probe = self.install_dir / '.devmux-update-probe'
            try:
                probe.write_text('probe', encoding='utf-8')
                probe.unlink()
            except OSError as error:
                raise ValueError('安装目录不可写（' + str(error) + '），请手动下载后解压覆盖')
            payload = {'staged': pending['staged'], 'version': pending['version'],
                       'target': str(self.install_dir),
                       'result': str(self.result_path), 'armedAt': self.clock()}
            self.restart_flag.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            return payload

    def cancel_restart(self):
        self.restart_flag.unlink(missing_ok=True)

    def launch_updater(self):
        """主进程收尾时调用：复制自身到临时目录，带 --apply-update 启动并退出。

        复制到临时目录是关键：目标 exe 正被自己占着，直接以自身启动仍会锁住它。
        """
        if not self.restart_flag.exists():
            return False
        try:
            payload = json.loads(self.restart_flag.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.restart_flag.unlink(missing_ok=True)
            return False
        if not getattr(sys, 'frozen', False):
            # 源码运行时没有可替换的 exe；保留暂存包，便于开发期手工验证替换流程。
            LOGGER.info('开发态不执行替换，已跳过（暂存包保留在 %s）', payload.get('staged'))
            self.restart_flag.unlink(missing_ok=True)
            return False
        try:
            workspace = Path(tempfile.mkdtemp(prefix='devmux-update-'))
            updater = workspace / 'DevMux-updater.exe'
            shutil.copy2(sys.executable, updater)
            subprocess.Popen(
                [str(updater), '--apply-update', str(payload['staged']), str(payload['target']),
                 str(payload['result'])],
                cwd=str(workspace), env=update_apply.clean_environment(), close_fds=True,
                creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0)
                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        except Exception:
            LOGGER.exception('无法启动替换进程，已保留下载包')
            return False
        self.restart_flag.unlink(missing_ok=True)
        return True

    def consume_result(self):
        """新版启动后读一次替换结果：成功则清掉旧 exe 备份，失败则保留给界面提示。"""
        try:
            payload = json.loads(self.result_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        try:
            self.result_path.unlink()
        except OSError:
            pass
        with self._lock:
            state = self._load()
            state['result'] = payload
            if payload.get('status') == 'installed':
                state.pop('pending', None)
                state.pop('skipped', None)
            self._save(state)
        if payload.get('status') == 'installed':
            self._prune_backups()
        return payload

    def _prune_backups(self, keep_seconds=7 * 24 * 3600):
        """保留最近一份旧 exe 供回退；更老的按时间清掉，避免每次升级多占 32MB。"""
        now = time.time()
        for backup in self.install_dir.glob('DevMux.exe.old-*'):
            try:
                if keep_seconds and now - backup.stat().st_mtime < keep_seconds:
                    continue
                backup.unlink()
            except OSError:
                LOGGER.warning('旧版本备份未能删除: %s', backup, exc_info=True)
