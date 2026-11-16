"""官方插件独立升级：按清单检测新版本，逐个停用、下载校验、替换、恢复启用。

与主程序升级解耦：插件有自己的版本号与发布节奏，宿主升级不牵连插件，插件升级也不
重启应用。只处理 plugin_catalog 里列出的官方插件——第三方插件不走这条通道，避免把
这里变成任意代码分发入口。

网络失败一律退回上次结果而不是报错：清单拉不到时页面停在「已知的最新状态」，比突然
宣称「全部是最新」更接近事实，也不会因为一次断网就丢掉用户已经看到过的更新提示。
"""
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.request
import zipfile

from app_version import APP_VERSION
from plugin_catalog import is_official
from update_feed import build_request, fetch_manifest, manifest_urls
import versions

LOGGER = logging.getLogger(__name__)

# 清单很小，缓存只是避免每次打开插件管理都发一次请求；手动「检查更新」总是绕过缓存。
CACHE_SECONDS = 6 * 3600
MAX_PACKAGE_BYTES = 8 * 1024 * 1024


def inspect_package(data):
    """只读检查包清单，确认这是官方插件，再交给安装流程。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if 'manifest.json' not in archive.namelist():
                raise ValueError('插件包缺少清单')
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
    except zipfile.BadZipFile as error:
        raise ValueError('插件包已损坏: ' + str(error))
    if not is_official(manifest.get('id')):
        raise ValueError('插件包不是官方插件')
    return manifest


class PluginUpdater:
    def __init__(self, manager, clock=time.time):
        self.manager = manager
        self.clock = clock
        self.state_path = Path(manager.store.root).parent / 'plugin-update-state.json'
        self._lock = threading.RLock()
        self._cache = None

    # ---------- 状态 ----------

    def _load_state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, value):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.state_path)
        except OSError:
            LOGGER.warning('插件更新状态写入失败', exc_info=True)

    def _cached_manifest(self):
        if self._cache is not None:
            return self._cache
        state = self._load_state()
        manifest = state.get('manifest')
        checked = state.get('checkedAt')
        if isinstance(manifest, dict) and isinstance(checked, (int, float)) and self.clock() - checked < CACHE_SECONDS:
            self._cache = (manifest, state.get('source'), checked)
            return self._cache
        return None

    def _refresh(self, force):
        cached = None if force else self._cached_manifest()
        if cached is not None:
            return cached
        manifest, source = fetch_manifest()
        checked = self.clock()
        self._cache = (manifest, source, checked)
        # 与其它状态文件一致：先写临时文件再替换，避免中断留下半截 JSON。
        self._save_state({'checkedAt': checked, 'source': source, 'manifest': manifest})
        return self._cache

    # ---------- 检测 ----------

    def check(self, force=False):
        """返回可升级与受限升级两组；未安装的官方插件不提示。"""
        with self._lock:
            error = None
            try:
                manifest, source, checked = self._refresh(force)
            except RuntimeError as failure:
                manifest, source, checked = self._fallback()
                if manifest is None:
                    return {'updates': [], 'blocked': [], 'checkedAt': None, 'source': None,
                            'error': str(failure)}
                error = str(failure)
            # 候选构建的清单不能驱动自动升级：它会随构建产物一起流出，包未必验证过。
            if manifest.get('status') not in (None, 'release'):
                return {'updates': [], 'blocked': [], 'checkedAt': checked, 'source': source,
                        'error': '清单来自候选构建（' + str(manifest.get('status')) + '），已忽略'}
            entries = manifest.get('plugins') if isinstance(manifest.get('plugins'), dict) else {}
            installed = {item['id']: item for item in self.manager.dispatch('list', {})}
            updates, blocked = [], []
            for identifier, entry in entries.items():
                current = installed.get(identifier)
                if not is_official(identifier) or current is None or not isinstance(entry, dict):
                    continue
                try:
                    if not versions.is_newer(entry.get('version'), current.get('version')):
                        continue
                except ValueError:
                    LOGGER.warning('无法比较插件版本: %s %r → %r', identifier,
                                   current.get('version'), entry.get('version'))
                    continue
                item = {
                    'id': identifier, 'name': current.get('name') or identifier,
                    'current': current.get('version'), 'version': entry.get('version'),
                    'size': entry.get('size'), 'sha256': entry.get('sha256'),
                    'urls': entry.get('urls') if isinstance(entry.get('urls'), list) else [],
                    'notes': entry.get('notes') or '',
                }
                minimum = entry.get('minAppVersion')
                try:
                    allowed = versions.satisfies_min(APP_VERSION, minimum)
                except ValueError:
                    # 清单声明写坏了就当作不允许：自动升级宁可停下，也不要在未知
                    # 兼容性上替用户做决定。问题会显示在「受限」组里。
                    LOGGER.warning('插件 %s 的 minAppVersion 无法解析: %r', identifier, minimum)
                    allowed = False
                if allowed:
                    updates.append(item)
                else:
                    item['blocked'] = '需要 DevMux ' + str(minimum) + ' 或更高版本'
                    blocked.append(item)
            result = {'updates': updates, 'blocked': blocked, 'checkedAt': checked, 'source': source}
            if error:
                result['error'] = error
            return result

    def _fallback(self):
        """网络失败时退回磁盘上最近一次成功的清单。"""
        state = self._load_state()
        manifest = state.get('manifest')
        if isinstance(manifest, dict):
            return manifest, state.get('source'), state.get('checkedAt')
        return None, None, None

    # ---------- 升级 ----------

    def _download(self, entry):
        urls = [url for url in entry.get('urls') or [] if isinstance(url, str) and url]
        if not urls:
            raise ValueError('清单没有提供下载地址')
        expected = str(entry.get('sha256') or '').lower()
        expected_size = entry.get('size')
        errors = []
        for url in urls:
            try:
                digest = hashlib.sha256()
                buffer = io.BytesIO()
                with urllib.request.urlopen(build_request(url), timeout=30) as response:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        digest.update(chunk)
                        buffer.write(chunk)
                        if buffer.tell() > MAX_PACKAGE_BYTES:
                            raise ValueError('插件包过大')
                data = buffer.getvalue()
                if expected and digest.hexdigest() != expected:
                    raise ValueError('SHA-256 校验失败')
                if isinstance(expected_size, int) and expected_size and len(data) != expected_size:
                    raise ValueError('插件包大小不符')
                return data, url
            except Exception as error:
                errors.append(url + ': ' + str(error))
        raise RuntimeError('插件包下载失败（' + '；'.join(errors) + '）')

    def _entry(self, identifier):
        report = self.check()
        for item in report['updates']:
            if item['id'] == identifier:
                return item
        for item in report['blocked']:
            if item['id'] == identifier:
                raise ValueError(item['blocked'])
        raise ValueError('该插件没有可用更新')

    def _apply_one(self, identifier):
        if not is_official(identifier):
            raise ValueError('只支持官方插件升级')
        entry = self._entry(identifier)
        # 先下载再停用：网络失败不该把一个正在工作的插件停在那儿。
        data, url = self._download(entry)
        manifest = inspect_package(data)
        if manifest.get('id') != identifier:
            raise ValueError('插件包与目标插件不一致')
        if manifest.get('version') != entry['version']:
            raise ValueError('插件包版本与清单不一致')
        was_enabled = identifier in self.manager.enabled
        if was_enabled:
            self.manager.dispatch('disable', {'id': identifier})
        try:
            self.manager.dispatch('install', {
                'package': base64.b64encode(data).decode('ascii'),
                'keepBackup': True, 'expectId': identifier, 'expectVersion': entry['version'],
            })
            if was_enabled:
                self.manager.dispatch('enable', {'id': identifier})
        except BaseException:
            # 装上却启用不起来同样是坏结果：退回上一版，别留一个打不开的插件。
            try:
                self.manager.dispatch('restore', {'id': identifier})
                if was_enabled:
                    self.manager.dispatch('enable', {'id': identifier})
            except Exception:
                LOGGER.exception('插件回滚失败: %s', identifier)
            raise
        LOGGER.info('插件升级完成: %s %s → %s (%s)', identifier, entry['current'], entry['version'], url)
        return {'id': identifier, 'name': entry.get('name'), 'success': True,
                'from': entry['current'], 'to': entry['version'], 'url': url}

    def apply(self, identifiers):
        """逐个升级；单个失败不影响其余，结果逐条返回给界面。"""
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError('没有要升级的插件')
        results = []
        for identifier in identifiers[:8]:
            if not isinstance(identifier, str):
                results.append({'id': str(identifier), 'success': False, 'error': '无效的插件 ID'})
                continue
            try:
                results.append(self._apply_one(identifier))
            except Exception as error:
                LOGGER.warning('插件升级失败: %s: %s', identifier, error)
                results.append({'id': identifier, 'success': False, 'error': str(error)})
        return {'results': results}


def seed_official_plugins(manager, plugin_dir):
    """首次启动把 exe 旁 plugin/ 目录的官方包装进本机插件存储。

    只装官方白名单内的 ID，且已安装的跳过；重复启动幂等，不覆盖用户已装或已改的
    插件。源码运行时没有 plugin/ 目录，自然无事可做。
    """
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return
    installed = {item['id'] for item in manager.dispatch('list', {})}
    for package in sorted(plugin_dir.glob('*.dmp')):
        try:
            with zipfile.ZipFile(package) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
            identifier = manifest.get('id')
            if not is_official(identifier) or identifier in installed:
                continue
            manager.dispatch('install', {'package': base64.b64encode(package.read_bytes()).decode()})
            LOGGER.info('已导入官方插件: %s %s', identifier, manifest.get('version'))
        except Exception:
            LOGGER.warning('官方插件导入失败: %s', package.name, exc_info=True)
