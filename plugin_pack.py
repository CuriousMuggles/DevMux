"""Build one .dmp file: python plugin_pack.py plugins/example dist/example.dmp"""
import argparse
import json
from pathlib import Path
import zipfile
from plugin_store import PluginStore
from plugin_assets import build_frontend

def pack(source, output):
    source = Path(source).resolve()
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8-sig'))
    PluginStore.validate(manifest)
    frontend = build_frontend(source, manifest['frontend']) if manifest.get('frontend') else None
    backend = source / manifest['backend']
    if not backend.is_file():raise ValueError('插件后端入口不存在: ' + manifest['backend'])
    compile(backend.read_text(encoding='utf-8-sig'),str(backend),'exec')
    output = Path(output).resolve()
    if output.is_relative_to(source):
        raise ValueError('输出文件不能位于插件源目录内')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        if manifest['id'] == 'usb-relay':
            from relay_probe_build import build_relay_probe
            workspace = Path(__file__).resolve().parent
            archive.write(build_relay_probe(str(workspace)), 'native/RelayProbe.exe')
            archive.write(workspace / 'vendor/usb-relay/bin/usb_relay_device.dll', 'native/usb_relay_device.dll')
        for path in sorted(source.rglob('*')):
            if path.is_symlink():
                raise ValueError('插件源不能包含符号链接')
            if path.is_file() and not any(part.startswith('.') or part=='__pycache__' for part in path.relative_to(source).parts):
                if path.name == 'manifest.json' and path.parent == source:
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                elif path.relative_to(source).as_posix() == manifest.get('frontend'):
                    archive.writestr(manifest['frontend'], frontend)
                else:
                    archive.write(path, path.relative_to(source).as_posix())
    return output

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source'); parser.add_argument('output')
    args = parser.parse_args()
    print(pack(args.source, args.output))
