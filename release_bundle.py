"""Build a bounded Windows delivery with source/build evidence; never copy runtime data."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

import doc_links
from doc_links import DOCUMENTS, STALE_RUNTIME_DOCS
from app_version import APP_VERSION
from release_plugins import OFFICIAL_PLUGINS, build_official_plugins, catalog

ROOT = Path(__file__).resolve().parent
REPOSITORY = 'CuriousMuggles/DevMux'
GITEE_REPOSITORY = 'liudaiyang/DevMux'


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT).decode('utf-8').strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files():
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT)
    return sorted(set(value.decode('utf-8') for value in raw.split(bytes([0])) if value))


def source_hashes():
    result = {}
    for name in source_files():
        path = ROOT / name
        if path.is_symlink():
            raise RuntimeError('Source symlink is unsupported: ' + name)
        if path.is_file():
            result[name] = digest(path)
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def exe_version(path):
    """读取 Windows 可执行文件的版本资源；非 Windows 或读不到时返回 None。"""
    path = Path(path)
    if sys.platform != 'win32' or not path.is_file():
        return None
    import ctypes
    from ctypes import wintypes

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [('signature', wintypes.DWORD), ('strucVersion', wintypes.DWORD),
                    ('fileVersionMS', wintypes.DWORD), ('fileVersionLS', wintypes.DWORD),
                    ('productVersionMS', wintypes.DWORD), ('productVersionLS', wintypes.DWORD)]

    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        return None
    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    return '%d.%d.%d' % (info.fileVersionMS >> 16, info.fileVersionMS & 0xFFFF,
                         info.fileVersionLS >> 16)


def verify_runtime(root=None):
    """检查 `dist/` 是否是一份自洽的交付集：EXE 版本、四个官方包、随包文档。

    返回问题描述列表；空列表表示双击这个 EXE 时，程序和身边的文档属于同一版本。
    """
    runtime = Path(root) if root else ROOT / 'dist'
    if not runtime.is_dir():
        return ['运行目录不存在：' + str(runtime)]
    problems = ['%s -> %s' % (path, target) for path, target in check_runtime_docs(runtime)]
    for name in DOCUMENTS:
        if not (runtime / name).is_file():
            problems.append('缺少随包文档：' + name)
    plugins = runtime / 'plugin'
    for identifier in OFFICIAL_PLUGINS:
        if not plugins.is_dir() or not list(plugins.glob(identifier + '-*.dmp')):
            problems.append('缺少官方插件包：' + identifier)
    version = exe_version(runtime / 'DevMux.exe')
    if version and version != APP_VERSION:
        problems.append('DevMux.exe 是 %s，文档是 %s，需重新构建' % (version, APP_VERSION))
    return problems


def sync_runtime_docs(root=None):
    """把随包文档整份同步到本地运行目录，让 ``dist/`` 成为一份自洽的交付集。

    与发布 ZIP 用同一份白名单，因此 ``dist/`` 里双击运行的 EXE、``plugin/`` 里的
    官方包和文档始终属于同一版本；同时清掉降为内部文档的旧文件，避免残留上一版
    说明。内容相同则不写（保留 mtime）；绝不触碰 devmux-data、plugin 及用户自己
    放进去的文件。返回 ``{'updated': [...], 'removed': [...], 'problems': [...]}``。
    """
    runtime = Path(root) if root else ROOT / 'dist'
    report = {'updated': [], 'removed': [], 'problems': []}
    if not runtime.is_dir():
        return report
    for name in DOCUMENTS:
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # 交付副本里的仓库根相对链接已改写为交付相对路径，不能直接拷源文件。
        data = doc_links.delivery_bytes(name, ROOT)
        if target.is_file() and target.read_bytes() == data:
            continue
        target.write_bytes(data)
        report['updated'].append(name)
    for name in STALE_RUNTIME_DOCS:
        target = runtime / name
        if target.is_file():
            target.unlink()
            report['removed'].append(name)
    report['problems'] = check_runtime_docs(runtime)
    return report


def check_runtime_docs(runtime=None):
    """校验运行目录里的随包文档链接闭合；返回问题列表。"""
    runtime = Path(runtime) if runtime else ROOT / 'dist'
    if not runtime.is_dir():
        return []
    paths = [runtime / name for name in DOCUMENTS if (runtime / name).is_file()]
    problems = doc_links.check_paths(paths, runtime)
    problems += doc_links.check_closed_set([p.relative_to(runtime).as_posix() for p in paths], runtime)
    return problems


def download_urls(tag, filename):
    """下载地址：GitHub 优先，Gitee 同 tag 的发行版附件兜底，再由环境变量追加镜像。

    GitHub 在国内经常连不上（尤其是 release 附件所在的 CDN），而 Gitee 上的同一份
    发行版对匿名下载可用，所以两条地址都写进清单，客户端按顺序回退。
    其它镜像的 URL 形态需要发布时确认，仍用模板注入，不在这里猜。
    """
    urls = [f'https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}',
            f'https://gitee.com/{GITEE_REPOSITORY}/releases/download/{tag}/{filename}']
    for template in os.environ.get('DEVMUX_DOWNLOAD_MIRRORS', '').split(','):
        template = template.strip()
        if template:
            urls.append(template.replace('{tag}', tag).replace('{name}', filename))
    return urls


def build_manifest(info, plugin_directory, asset=None):
    """客户端唯一要解析的清单；插件段让官方插件能脱离主程序单独升级。"""
    tag = info['tag']
    plugins = {identifier: {**entry, 'urls': download_urls(tag, entry['file'])}
               for identifier, entry in catalog(plugin_directory).items()}
    manifest = {'schema': 1, 'channel': 'stable', 'version': info['version'], 'tag': tag,
                'status': info['status'], 'commit': info['commit'], 'releasedAt': info['builtAt'],
                'plugins': plugins}
    if asset:
        manifest['assets'] = {'windows': asset}
    # 强制升级的下限由运维按需注入，默认不写：没设过就不该凭空出现一个下限版本。
    minimum = os.environ.get('DEVMUX_MIN_SUPPORTED_VERSION', '').strip()
    if minimum:
        manifest['minSupportedVersion'] = minimum
    return manifest


def stage_delivery(output, binary, info):
    """Copy only explicitly selected artifacts; user data never enters the ZIP."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(binary, output / 'DevMux.exe')
    build_official_plugins(output)
    for name in DOCUMENTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(doc_links.delivery_bytes(name, ROOT))
    write_json(output / 'BUILD-INFO.json', info)
    files = sorted(path for path in output.rglob('*') if path.is_file())
    (output / 'SHA256SUMS.txt').write_text(''.join(
        digest(path) + '  ' + path.relative_to(output).as_posix() + '\n' for path in files), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true', help='require clean source at the version tag')
    options = parser.parse_args()
    problems = doc_links.check_delivery(list(DOCUMENTS), ROOT)
    if problems:
        raise RuntimeError('随包文档链接不闭合，先修文档：' + '; '.join(
            '%s -> %s' % (path.name if path.is_absolute() else path, target) for path, target in problems))
    commit = git('rev-parse', 'HEAD')
    dirty = bool(git('status', '--porcelain', '--untracked-files=all'))
    tag = 'v' + APP_VERSION
    if options.release:
        if dirty:
            raise RuntimeError('Formal release requires a clean committed worktree')
        if git('rev-parse', tag + '^{commit}') != commit:
            raise RuntimeError('Version tag does not match HEAD')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'dist' / 'releases' / (APP_VERSION + '-' + stamp)
    output.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    environment = '\n'.join(sorted({dist.metadata['Name'] + '==' + dist.version
        for dist in metadata.distributions() if dist.metadata['Name']})) + '\n'
    adb_dir = Path(os.environ.get('ADB_BUNDLE_DIR') or Path(shutil.which('adb') or '').parent)
    platform_tools = {path.name: digest(path) for path in sorted(adb_dir.iterdir()) if path.is_file()}
    (output / 'build-environment.txt').write_text(environment, encoding='utf-8')
    command = [sys.executable, '-m', 'PyInstaller', 'build.spec', '--clean', '--noconfirm']
    with (output / 'build.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    if result.returncode:
        raise RuntimeError('Build failed; see ' + str(output / 'build.log'))
    if source_hashes() != before or git('rev-parse', 'HEAD') != commit:
        raise RuntimeError('Source changed during build; rebuild before delivery')
    info = dict(version=APP_VERSION, tag=tag, commit=commit, dirty=dirty,
                status='release' if options.release else 'candidate', builtAt=stamp,
                platform=platform.platform(), python=sys.version, command=command,
                platformToolsSha256=platform_tools,
                sourceSha256=before, environmentSha256=digest(output / 'build-environment.txt'),
                buildLogSha256=digest(output / 'build.log'),
                provenance='Locally recorded source and build evidence; not a signed reproducibility proof')
    stage = output / 'delivery'
    stage_delivery(stage, ROOT / 'dist' / 'DevMux.exe', info)
    binary_zip = output / ('DevMux-' + APP_VERSION + '-windows.zip')
    with zipfile.ZipFile(binary_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    # 清单是独立发布资产，不打进 ZIP：它是给已安装客户端读的，不是给解压的人看的。
    # 资产段要等 ZIP 生成后才能算大小与摘要，所以放在打包之后。
    asset = {'name': binary_zip.name, 'size': binary_zip.stat().st_size,
             'sha256': digest(binary_zip), 'urls': download_urls(tag, binary_zip.name)}
    manifest_path = output / 'latest.json'
    write_json(manifest_path, build_manifest(info, stage / 'plugin', asset))
    source_zip = output / ('DevMux-' + APP_VERSION + '-source.zip')
    with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in before:
            archive.write(ROOT / name, name)
    shutil.copy2(stage / 'BUILD-INFO.json', output / 'BUILD-INFO.json')
    artifacts = [binary_zip, source_zip, manifest_path, output / 'BUILD-INFO.json', output / 'build.log', output / 'build-environment.txt']
    (output / 'SHA256SUMS.txt').write_text(''.join(digest(path) + '  ' + path.name + '\n' for path in artifacts), encoding='utf-8')
    synced = sync_runtime_docs()
    for name in synced['updated']:
        print('synced runtime doc: ' + name)
    for name in synced['removed']:
        print('removed stale runtime doc: ' + name)
    runtime_problems = verify_runtime()
    for problem in runtime_problems:
        print('runtime problem: ' + problem)
    if runtime_problems:
        raise RuntimeError('dist/ 不是自洽的交付集，先修上面的问题再发布')
    print(output)


if __name__ == '__main__':
    main()
