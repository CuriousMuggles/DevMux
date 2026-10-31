"""Plugin package validation and atomic installation."""
import json
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

ID = re.compile(r'^[a-z][a-z0-9-]{0,63}$')

class PluginStore:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            if not self.root.exists():
                return []
            result = []
            for path in sorted(self.root.iterdir()):
                if path.is_dir() and ID.fullmatch(path.name):
                    try:
                        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                        self.validate(manifest)
                        if manifest['id'] == path.name:
                            result.append(manifest)
                    except (OSError, ValueError, TypeError):
                        continue
            return result

    @staticmethod
    def validate(manifest):
        if not isinstance(manifest, dict) or not isinstance(manifest.get('id'), str) or not ID.fullmatch(manifest['id']):
            raise ValueError('无效的插件 ID')
        if type(manifest.get('apiVersion')) is not int or manifest.get('apiVersion') != 1:
            raise ValueError('不支持的插件接口版本')
        for key in ('name', 'version'):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise ValueError('插件清单缺少 ' + key)
        for key in ('backend', 'frontend'):
            value=manifest.get(key)
            if key=='frontend' and value is None:continue
            if not isinstance(value,str) or not value or '\\' in value or ':' in value or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/')):
                raise ValueError('插件入口路径无效: ' + key)
            if not value.endswith('.py' if key=='backend' else '.html'):
                raise ValueError('插件入口类型无效: ' + key)
        for field in ('actions','localActions'):
            actions=manifest.get(field,[])
            if not isinstance(actions,list) or any(not isinstance(action,str) or not action or action.startswith('__') for action in actions) or len(set(actions))!=len(actions):
                raise ValueError('插件 '+field+' 必须是无重复的公开动作名称列表')
        if 'tool' in manifest and (not isinstance(manifest['tool'],str) or not ID.fullmatch(manifest['tool'])):
            raise ValueError('插件工具标识无效')
        permissions=manifest.get('permissions',[])
        if not isinstance(permissions,list) or any(permission not in ('pick-file','pick-directory') for permission in permissions):
            raise ValueError('插件 permissions 包含不支持的宿主权限')
        if 'window' in manifest:
            value=manifest['window']
            if not isinstance(value,dict) or any(key not in ('width','height') or type(number) is not int or not 240<=number<=1600 for key,number in value.items()):
                raise ValueError('插件窗口尺寸无效，范围为 240–1600')
        if 'timeout' in manifest:
            value=manifest['timeout']
            if type(value) not in (int,float) or not 1<=value<=180:
                raise ValueError('插件 timeout 须在 1–180 秒范围内')

    def install(self, package):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.stage-', dir=self.root) as temp:
                stage = Path(temp) / 'plugin'
                stage.mkdir()
                with zipfile.ZipFile(package) as archive:
                    names = set()
                    if len(archive.infolist()) > 10000:
                        raise ValueError("插件包文件过多")
                    size = 0
                    for item in archive.infolist():
                        path = PurePosixPath(item.filename)
                        if (not item.filename or '\\' in item.filename or path.is_absolute()
                                or any(p in ('.', '..') or ':' in p or p.endswith((' ', '.')) or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))} for p in path.parts)
                                or item.filename.casefold() in names or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                            raise ValueError('插件包包含非法路径')
                        names.add(item.filename.casefold())
                        size += item.file_size
                        if size > 100 * 1024 * 1024:
                            raise ValueError('插件包过大')
                    if 'manifest.json' not in names:
                        raise ValueError('插件包缺少清单')
                    manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
                    self.validate(manifest)
                    for key in ('backend','frontend'):
                        if manifest.get(key) and manifest[key] not in archive.namelist():
                            raise ValueError('插件包缺少入口: '+manifest[key])
                    archive.extractall(stage)
                target = self.root / manifest['id']
                backup = Path(temp) / 'backup'
                if target.exists():
                    target.rename(backup)
                try:
                    stage.rename(target)
                except BaseException:
                    if backup.exists():
                        backup.rename(target)
                    raise
                return manifest

    def uninstall(self, plugin_id):
        if not isinstance(plugin_id, str) or not ID.fullmatch(plugin_id):
            raise ValueError('无效的插件 ID')
        with self.lock:
            target = self.root / plugin_id
            if not target.is_dir():
                raise ValueError('插件未安装')
            shutil.rmtree(target)
