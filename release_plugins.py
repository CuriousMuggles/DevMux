"""Build all official plugin packages: python release_plugins.py [dist-directory]."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from plugin_catalog import OFFICIAL_PLUGINS
from plugin_pack import pack


def build_official_plugins(dist_directory):
    root = Path(__file__).resolve().parent
    output = Path(dist_directory) / 'plugin'
    packages = []
    for identifier in OFFICIAL_PLUGINS:
        source = root / 'plugins' / identifier
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        version = manifest['version']
        if not version or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_' for c in version):
            raise ValueError('Invalid plugin version: ' + version)
        packages.append(pack(source, output / f'{identifier}-{version}.dmp'))
    return packages


def catalog(dist_directory):
    """清单用的插件条目：摘要与大小取自实际产物，不另算一遍源目录。"""
    result = {}
    for package in sorted(Path(dist_directory).glob('*.dmp')):
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
        identifier = manifest.get('id')
        if identifier not in OFFICIAL_PLUGINS:
            continue
        result[identifier] = {
            'file': package.name,
            'version': manifest['version'],
            'size': package.stat().st_size,
            'sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
            'description': manifest.get('description', ''),
            # 只有插件真用到新宿主能力时才声明；未声明即不限制最低版本。
            'minAppVersion': manifest.get('minAppVersion'),
        }
    return result


if __name__ == '__main__':
    for package in build_official_plugins(sys.argv[1] if len(sys.argv) > 1 else 'dist'):
        print(package)
