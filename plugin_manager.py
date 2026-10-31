"""Application-facing plugin lifecycle and package operations."""
import base64
import io
import json
import threading
from plugin_store import PluginStore, ID
from plugin_process import PluginProcess

class PluginManager:
    def __init__(self, root, extension_tools=None):
        self.extension_tools = extension_tools
        self.store = PluginStore(root)
        self.running = {}
        self.lock = threading.RLock()
        from remote_plugins import RemotePlugins
        self.remote = RemotePlugins(self)

    def remote_catalog(self):
        """Only expose identity and running state, never local paths or credentials."""
        with self.lock:
            return [{'id': item['id'], 'name': item['name'],
                     'version': item['version'], 'apiVersion': item['apiVersion'], 'frontend': bool(item.get('frontend')), 'permissions': item.get('permissions',['pick-file','pick-directory']), 'tool': item.get('tool', item['id']), 'actions':item.get('actions',[]), 'remoteCapable': bool(item.get('actions')),
                     'enabled': item['id'] in self.running and self.running[item['id']].process.poll() is None,
                     'remoteAllowed': self.running[item['id']].call('config').get('data',{}).get('remoteAllowed') is True if item.get('actions') and item['id'] in self.running and self.running[item['id']].process.poll() is None else False}
                    for item in self.store.list()]

    def close(self):
        with self.lock:
            for process in self.running.values():
                process.close()
            self.running.clear()
            if self.extension_tools:
                for key, service in self.extension_tools.services.copy().items():
                    if isinstance(service, PluginService):
                        self.extension_tools.services.pop(key, None)

    def dispatch(self, action, arguments):
        with self.lock:
            if action == 'install-policy':return {'allowed':self.remote.allowed}
            if action == 'set-install-policy':return self.remote.permission(arguments.get('allowed'))
            if action == 'allow-remote':
                identifier=arguments.get('id');allowed=arguments.get('allowed')
                if type(allowed) is not bool:raise ValueError('权限值必须为布尔值')
                was_running=identifier in self.running and self.running[identifier].process.poll() is None
                self.dispatch('enable',{'id':identifier})
                result=self.running[identifier].call('permission',{'allowed':allowed})
                if not allowed and not was_running:self.dispatch('disable',{'id':identifier})
                return result
            if action == 'list':
                return [{**m, 'enabled': m['id'] in self.running and self.running[m['id']].process.poll() is None} for m in self.store.list()]
            if action == 'install':
                raw = arguments.get('package', '')
                if not isinstance(raw, str) or len(raw) > 6000000:
                    raise ValueError('插件包过大')
                package = io.BytesIO(base64.b64decode(raw, validate=True))
                # Running versions must be explicitly stopped before replacement.
                import zipfile
                with zipfile.ZipFile(package) as z:
                    manifest = json.loads(z.read('manifest.json').decode('utf-8'))
                self.store.validate(manifest)
                if manifest['id'] in self.running:
                    raise ValueError('请先停用插件再更新')
                package.seek(0)
                return self.store.install(package)
            identifier = arguments.get('id')
            if not isinstance(identifier, str) or not ID.fullmatch(identifier):
                raise ValueError('无效的插件 ID')
            directory = self.store.root / identifier
            if not directory.is_dir():
                raise ValueError('插件未安装')
            if action == 'enable':
                if identifier in self.running and self.running[identifier].process.poll() is not None:
                    self.running.pop(identifier).close()
                if identifier not in self.running:
                    process = PluginProcess(directory)
                    try:
                        process.call('__ready__', timeout=10)
                    except BaseException:
                        process.close()
                        raise
                    self.running[identifier] = process
                    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                    if self.extension_tools and (manifest.get("actions") or manifest.get('localActions')):
                        tool = manifest.get("tool", identifier)
                        previous = self.extension_tools.services.get(tool)
                        if previous is not None and not (isinstance(previous, PluginService) and previous.plugin_id == identifier):
                            self.running.pop(identifier).close()
                            raise ValueError('工具标识与现有服务冲突')
                        self.extension_tools.services[tool] = PluginService(process, manifest, self.lock)
                return {}
            if action == 'disable':
                process = self.running.get(identifier)
                if process and process.process.poll() is None and not process.call('__can_stop__'):
                    raise ValueError('插件仍有活动任务，请先结束任务')
                self.running.pop(identifier, None)
                if self.extension_tools:
                    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                    tool = manifest.get('tool',identifier)
                    registered = self.extension_tools.services.get(tool)
                    if isinstance(registered, PluginService) and registered.process is process:
                        self.extension_tools.services.pop(tool, None)
                if process:
                    process.close()
                return {}
            if action == 'uninstall':
                if identifier in self.running:
                    raise ValueError('请先停用插件再卸载')
                self.store.uninstall(identifier)
                return {}
            if action == 'call':
                if identifier not in self.running:
                    raise ValueError('插件未启用')
                manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                timeout = min(180, max(1, float(manifest.get('timeout', 30))))
                return self.running[identifier].call(arguments.get('action'), arguments.get('arguments', {}), timeout=timeout)
            if action == 'page':
                if identifier not in self.running:
                    raise ValueError('插件未启用')
                manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                entry = manifest.get('frontend')
                if not isinstance(entry, str):
                    raise ValueError('插件没有界面')
                page = (directory / entry).resolve()
                if not page.is_relative_to(directory.resolve()) or not page.is_file():
                    raise ValueError('非法界面入口')
                return {'html': page.read_text(encoding='utf-8')}
            if action == 'logs':
                path=directory.parent/'.data'/identifier/'runtime.log'
                if not path.is_file():return {'text':''}
                with path.open('rb') as stream:
                    stream.seek(max(0,path.stat().st_size-65536))
                    return {'text':stream.read().decode('utf-8',errors='replace')}
            raise ValueError('未知插件管理动作')


class PluginService:
    def __init__(self, process, manifest, lock):
        self.process = process
        self.plugin_id = manifest['id']
        self.NAME = manifest['name']
        self.ACTIONS = frozenset(manifest.get('actions',[])+manifest.get('localActions',[]))
        self.remote_supported = bool(manifest.get('actions'))
        self.remote_actions = frozenset(manifest.get('actions',[]))
        self._lock = lock
    @property
    def remote_allowed(self):
        return self.remote_supported and self.process.call('config').get('data', {}).get('remoteAllowed') is True
    def set_remote_permission(self, allowed):
        return self.process.call('permission', {'allowed': allowed})
    def call(self, action, arguments):
        return self.process.call(action, arguments)
    def call_for_owner(self, action, arguments, *, owner):
        if not self.remote_supported:return self.process.call(action,arguments)
        return self.process.call('__owned__', {'action': action, 'arguments': arguments, 'owner': owner})
