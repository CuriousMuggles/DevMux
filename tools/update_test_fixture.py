"""构造本地升级验证环境：复用现有产物「只改版本号」，起本地 HTTP 服务。

升级链路只校验 ZIP 里的 BUILD-INFO.json 和 dmp 里的 manifest.json 的 version/status，
不校验 exe 的真实内容，因此「新版」无需真实构建——复用现有 exe/dmp，只把版本号改高
即可驱动一次完整的「检测 → 下载 → 校验 → 替换 → 重启」验证，且不污染真实 Release。

用法：
  python tools/update_test_fixture.py --exe dist/DevMux.exe --plugin-dir dist/plugin

脚本会：
  1. 复用传入的 DevMux.exe，配一个 version 更高的 BUILD-INFO.json，打成测试 ZIP；
  2. 复用传入的官方插件 .dmp，把 manifest.json 的 version 递增，生成测试 dmp；
  3. 生成指向本地 HTTP 的 latest.json（version 更高、status=release）；
  4. 起一个本地静态文件服务托管以上产物，并打印验证启动命令。

验证时用 ``DEVMUX_UPDATE_MANIFEST`` 指向本地清单启动「旧版」，观察升级链路。
"""
import argparse
import functools
import hashlib
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_plugins import catalog  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bump_version(version):
    """把最后一段数字 +1；末段非数字时追加 1。"""
    parts = str(version).split('.')
    if parts and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
    else:
        parts.append('1')
    return '.'.join(parts)


def build_app_zip(exe, version, out, plugin_dir=None):
    """复用现有 exe，配一个 version 更高的 BUILD-INFO.json，打成测试 ZIP。

    给了 plugin_dir 时把其中的官方 .dmp 一并放进 ZIP 的 plugin/ 目录，与真实发布包
    一致——主程序升级会顺带覆盖安装目录的官方插件。
    """
    exe = Path(exe)
    if not exe.is_file():
        raise SystemExit(f'未找到 DevMux.exe：{exe}（请先构建，或用 --exe 指定）')
    name = f'DevMux-{version}-windows.zip'
    path = out / name
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(exe, 'DevMux.exe')
        archive.writestr('BUILD-INFO.json', json.dumps(
            {'version': version, 'status': 'release', 'commit': 'update-test-fixture'},
            ensure_ascii=False, indent=2))
        archive.writestr('SHA256SUMS.txt', '')
        if plugin_dir:
            for dmp in sorted(Path(plugin_dir).glob('*.dmp')):
                archive.write(dmp, 'plugin/' + dmp.name)
    return path


def build_plugin_dmps(plugin_dir, out):
    """复用现有官方 dmp，只把 manifest.json 的 version 递增，生成测试 dmp。"""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return
    for dmp in sorted(plugin_dir.glob('*.dmp')):
        with zipfile.ZipFile(dmp) as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
            items = {item.filename: archive.read(item.filename)
                     for item in archive.infolist() if not item.is_dir()}
        identifier = manifest.get('id')
        if identifier is None:
            continue
        manifest['version'] = bump_version(manifest.get('version', '0.0.0'))
        new_name = f"{identifier}-{manifest['version']}.dmp"
        new_path = out / new_name
        with zipfile.ZipFile(new_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            for name, data in items.items():
                if name != 'manifest.json':
                    archive.writestr(name, data)


def build_manifest(app_zip, version, plugin_dir, base_url):
    """生成指向本地 HTTP 的 latest.json；插件条目复用 release_plugins.catalog。"""
    manifest = {
        'schema': 1, 'channel': 'stable', 'version': version, 'tag': 'v' + version,
        'status': 'release',
        'assets': {'windows': {'name': app_zip.name, 'size': app_zip.stat().st_size,
                               'sha256': sha256(app_zip), 'urls': [base_url + '/' + app_zip.name]}},
        'plugins': {identifier: {**entry, 'urls': [base_url + '/' + entry['file']]}
                    for identifier, entry in catalog(plugin_dir).items()},
    }
    return manifest


def main():
    parser = argparse.ArgumentParser(description='构造本地升级验证环境并起 HTTP 服务')
    parser.add_argument('--exe', default='dist/DevMux.exe', help='当前（旧版）DevMux.exe 路径')
    parser.add_argument('--plugin-dir', default='dist/plugin', help='官方插件 dmp 目录')
    parser.add_argument('--version', default='99.0.0', help='测试主程序版本号，需高于当前版本')
    parser.add_argument('--port', type=int, default=8000, help='本地 HTTP 端口')
    parser.add_argument('--no-serve', action='store_true', help='只构造不启动服务')
    args = parser.parse_args()

    out = Path(tempfile.mkdtemp(prefix='devmux-update-test-'))
    app_zip = build_app_zip(args.exe, args.version, out)
    build_plugin_dmps(args.plugin_dir, out)
    base_url = f'http://127.0.0.1:{args.port}'
    manifest_path = out / 'latest.json'
    manifest_path.write_text(
        json.dumps(build_manifest(app_zip, args.version, out, base_url),
                   ensure_ascii=False, indent=2), encoding='utf-8')

    print('本地升级测试环境：')
    print(f'  目录: {out}')
    print(f'  清单: {base_url}/latest.json')
    plugins = catalog(out)
    if plugins:
        print('  插件: ' + ', '.join(entry['file'] for entry in plugins.values()))
    print()
    print('验证主程序升级（PowerShell，用旧版 DevMux 启动）：')
    print(f"  $env:DEVMUX_UPDATE_MANIFEST='{base_url}/latest.json'; & '{Path(args.exe).resolve()}'")
    print('验证插件升级：启动后打开「插件管理」，点「检查更新」即可看到插件有新版。')

    if args.no_serve:
        return
    print()
    print('启动本地 HTTP 服务（Ctrl+C 停止）...')
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out))
    try:
        HTTPServer(('127.0.0.1', args.port), handler).serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
