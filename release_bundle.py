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

from app_version import APP_VERSION
from release_plugins import build_official_plugins

ROOT = Path(__file__).resolve().parent
DOCUMENTS = ('README.md', '使用说明.md', 'AGENT_USAGE.md', 'RELEASE-NOTES.md',
             'LICENSE', 'THIRD_PARTY_LICENSES.md', 'docs/releasing.md',
             'docs/新手使用与发布需求.md', 'docs/plugin-development.md',
             'docs/使用说明/双端交互流程.md', 'docs/使用说明/串口使用说明.md')


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


def stage_delivery(output, binary, info):
    """Copy only explicitly selected artifacts; user data never enters the ZIP."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(binary, output / 'DevMux.exe')
    build_official_plugins(output)
    for name in DOCUMENTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    write_json(output / 'BUILD-INFO.json', info)
    files = sorted(path for path in output.rglob('*') if path.is_file())
    (output / 'SHA256SUMS.txt').write_text(''.join(
        digest(path) + '  ' + path.relative_to(output).as_posix() + '\n' for path in files), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true', help='require clean source at the version tag')
    options = parser.parse_args()
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
    source_zip = output / ('DevMux-' + APP_VERSION + '-source.zip')
    with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in before:
            archive.write(ROOT / name, name)
    shutil.copy2(stage / 'BUILD-INFO.json', output / 'BUILD-INFO.json')
    artifacts = [binary_zip, source_zip, output / 'BUILD-INFO.json', output / 'build.log', output / 'build-environment.txt']
    (output / 'SHA256SUMS.txt').write_text(''.join(digest(path) + '  ' + path.name + '\n' for path in artifacts), encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
