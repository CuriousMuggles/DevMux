"""Build all official plugin packages: python release_plugins.py [dist-directory]."""
import json
from pathlib import Path
import sys
from plugin_pack import pack

OFFICIAL_PLUGINS = ('usb-relay', 'db-flash', 'mijia', 'ebpf')


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


if __name__ == '__main__':
    for package in build_official_plugins(sys.argv[1] if len(sys.argv) > 1 else 'dist'):
        print(package)
