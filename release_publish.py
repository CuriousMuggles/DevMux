"""一键发布：校验交付目录 → 双远端推送 master 与 tag → 创建 GitHub/Gitee 发行版并上传资产。

前置：``python release_bundle.py --release`` 已生成正式交付目录。
本脚本把「推送 + 上传 + 校验」固化下来：资产清单来自交付目录与 ``latest.json``，
上传后用 ``gh api`` 确认每个资产都是 ``uploaded``，不再需要人工逐个核对。

Gitee 是 OTA 的回退源：GitHub 在国内常常下不动附件，客户端会按清单里的地址顺序
回退到 Gitee。Gitee 发行版的上传走 API v5，需要 ``GITEE_TOKEN``（账号设置里的私人令牌，
勾选 projects 权限）；未设置时只推送代码并明确提示 Gitee 资产未更新。

用法：
  python release_publish.py                 # 取 dist/releases 下最新的正式目录
  python release_publish.py --dir <目录>    # 指定交付目录
  python release_publish.py --dry-run       # 只校验，不推送、不上传
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import doc_links

ROOT = Path(__file__).resolve().parent
REPOSITORY = 'CuriousMuggles/DevMux'
GITEE_REPOSITORY = 'liudaiyang/DevMux'
GITEE_API = 'https://gitee.com/api/v5/repos/' + GITEE_REPOSITORY
GITEE_TOKEN_ENV = 'GITEE_TOKEN'
GITEE_UPLOAD_LIMIT = 100 * 1024 * 1024
REMOTES = ('origin', 'gitee')
NOTES = ROOT / doc_links.source_of('发布说明.md')


def run(command, capture=False):
    result = subprocess.run(command, cwd=ROOT, text=True, encoding='utf-8',
                            capture_output=capture)
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(' '.join(command) + ' 失败: ' + detail)
    return result.stdout.strip() if capture else ''


def git(*args, capture=False):
    return run(['git', *args], capture=capture)


def gh(*args, capture=False):
    return run(['gh', *args], capture=capture)


def latest_release_dir():
    base = ROOT / 'dist' / 'releases'
    if not base.is_dir():
        raise RuntimeError('未找到 dist/releases，先执行 python release_bundle.py --release')
    for path in sorted((item for item in base.iterdir() if item.is_dir()), reverse=True):
        info = path / 'BUILD-INFO.json'
        try:
            if json.loads(info.read_text(encoding='utf-8')).get('status') == 'release':
                return path
        except (OSError, ValueError):
            continue
    raise RuntimeError('未找到正式（status=release）发布目录，先执行 python release_bundle.py --release')


def release_notes(version):
    text = NOTES.read_text(encoding='utf-8')
    heading = f'# DevMux {version}'
    if heading not in text:
        raise RuntimeError('发布说明缺少本版本小节: ' + heading)
    body = text[text.index(heading) + len(heading):]
    cut = body.find('\n# DevMux ')
    if cut >= 0:
        body = body[:cut]
    return body.strip() + '\n'


def asset_paths(directory, version, plugins):
    names = [f'DevMux-{version}-windows.zip', f'DevMux-{version}-source.zip',
             'latest.json', 'BUILD-INFO.json', 'build.log', 'build-environment.txt',
             'SHA256SUMS.txt']
    paths = [directory / name for name in names]
    paths += [directory / 'delivery' / 'plugin' / entry['file'] for entry in plugins.values()]
    return paths


def verify(directory):
    info = json.loads((directory / 'BUILD-INFO.json').read_text(encoding='utf-8'))
    version = str(info.get('version'))
    if info.get('status') != 'release':
        raise RuntimeError('交付目录不是正式构建: status=' + str(info.get('status')))
    if info.get('dirty'):
        raise RuntimeError('交付目录来自带未提交改动的构建')
    head = git('rev-parse', 'HEAD', capture=True)
    if info.get('commit') != head:
        raise RuntimeError(f"交付目录提交 {info.get('commit')} 与当前 HEAD {head} 不一致")
    tag = 'v' + version
    if git('rev-parse', tag + '^{commit}', capture=True) != head:
        raise RuntimeError(f'{tag} 未指向当前 HEAD {head}')
    manifest = json.loads((directory / 'latest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != version or manifest.get('status') != 'release':
        raise RuntimeError('latest.json 与 BUILD-INFO 的版本/状态不一致')
    paths = asset_paths(directory, version, manifest.get('plugins') or {})
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError('交付目录缺少资产: ' + ', '.join(missing))
    return version, tag, paths


def upload(tag, version, paths):
    if subprocess.run(['gh', 'release', 'view', tag], cwd=ROOT,
                      capture_output=True).returncode == 0:
        print(f'Release {tag} 已存在，补齐资产')
        gh('release', 'upload', tag, *[str(path) for path in paths], '--clobber')
        return
    with tempfile.TemporaryDirectory() as temp:
        notes = Path(temp) / 'notes.md'
        notes.write_text(release_notes(version), encoding='utf-8')
        gh('release', 'create', tag, *[str(path) for path in paths],
           '--title', 'DevMux ' + version, '--notes-file', str(notes))


def verify_uploaded(tag, paths):
    output = gh('api', f'repos/{REPOSITORY}/releases/tags/{tag}', '--jq',
                '.assets[] | "\\(.state) \\(.name)"', capture=True)
    states = dict(reversed(line.split(' ', 1)) for line in output.splitlines() if line)
    expected = {path.name for path in paths}
    missing = sorted(expected - set(states))
    bad = sorted(name for name in expected if states.get(name) not in (None, 'uploaded'))
    if missing or bad:
        raise RuntimeError('Release 资产未全部就绪: 缺失 ' + str(missing) + '，异常 ' + str(bad))
    print(f'全部 {len(expected)} 个资产 uploaded')


# ---------- Gitee 发行版：OTA 回退源 ----------

def gitee_api(method, path, token='', params=None, payload=None, timeout=60):
    """调用 Gitee API v5；token 放在查询串里，与官方示例一致。"""
    query = dict(params or {})
    if token:
        query['access_token'] = token
    url = GITEE_API + path
    if query:
        url += '?' + urllib.parse.urlencode(query)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    headers = {'Content-Type': 'application/json;charset=UTF-8'} if data else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body.strip() else {}


def gitee_attachment_names(release_id):
    """发行版现有附件名集合；Gitee 分页返回，一页取满即可覆盖本发布。"""
    payload = gitee_api('GET', f'/releases/{release_id}/attach_files', params={'per_page': 100})
    items = payload if isinstance(payload, list) else payload.get('attach_files') or []
    return {str(item.get('name')) for item in items if isinstance(item, dict)}


def gitee_upload(release_id, token, path):
    """以 multipart 上传单个资产；Gitee 附件上限 100MB，超限直接报错。"""
    if path.stat().st_size > GITEE_UPLOAD_LIMIT:
        raise RuntimeError(f'{path.name} 超过 Gitee 附件上限 100MB')
    boundary = '----DevMuxRelease' + str(int(time.time() * 1000))
    head = ('--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="file"; filename="' + path.name + '"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
    tail = ('\r\n--' + boundary + '--\r\n').encode('utf-8')
    body = head + path.read_bytes() + tail
    url = (GITEE_API + f'/releases/{release_id}/attach_files?'
           + urllib.parse.urlencode({'access_token': token}))
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(request, timeout=600) as response:
        response.read()


def publish_gitee(tag, version, paths):
    """创建/补齐 Gitee 发行版；没有令牌时明确提示，不假装已发布。"""
    token = os.environ.get(GITEE_TOKEN_ENV, '').strip()
    if not token:
        print(f'[SKIP] 未设置 {GITEE_TOKEN_ENV}，Gitee 发行版未更新；'
              '客户端在 GitHub 不通时将无法回退到 Gitee')
        return False
    try:
        release = gitee_api('GET', f'/releases/tags/{tag}', token=token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        release = None
    # Gitee 对不存在的发行版返回 200 + null，不能当成已存在。
    if not release:
        release = gitee_api('POST', '/releases', token=token, timeout=120, payload={
            'tag_name': tag, 'name': 'DevMux ' + version, 'body': release_notes(version),
            'target_commitish': 'master', 'prerelease': False})
    release_id = release.get('id')
    if not release_id:
        raise RuntimeError('Gitee 发行版缺少 id: ' + str(release))
    existing = gitee_attachment_names(release_id)
    for path in paths:
        if path.name in existing:
            continue
        gitee_upload(release_id, token, path)
        print('  gitee 上传 ' + path.name)
    missing = sorted({path.name for path in paths} - gitee_attachment_names(release_id))
    if missing:
        raise RuntimeError('Gitee 发行版附件未全部就绪: ' + ', '.join(missing))
    print(f'Gitee 发行版 {tag} 资产齐全 (https://gitee.com/{GITEE_REPOSITORY}/releases/tag/{tag})')
    return True


def main():
    parser = argparse.ArgumentParser(description='一键发布：推送双远端并上传 GitHub / Gitee 发行版资产')
    parser.add_argument('--dir', default=None, help='交付目录，默认取 dist/releases 最新的正式目录')
    parser.add_argument('--remotes', default=','.join(REMOTES), help='推送远端，逗号分隔')
    parser.add_argument('--dry-run', action='store_true', help='只校验，不推送、不上传')
    options = parser.parse_args()

    directory = Path(options.dir) if options.dir else latest_release_dir()
    version, tag, paths = verify(directory)
    print(f'交付目录: {directory}')
    print(f'版本: {version}  tag: {tag}  资产 {len(paths)} 个')
    for path in paths:
        print('  - ' + path.name)
    if options.dry_run:
        print('dry-run：校验通过，未推送、未上传')
        return 0

    for remote in (item.strip() for item in options.remotes.split(',') if item.strip()):
        print(f'推送 {remote}: master {tag}')
        git('push', remote, 'master', tag)
    print('创建/补齐 Release 并上传资产')
    upload(tag, version, paths)
    verify_uploaded(tag, paths)
    publish_gitee(tag, version, paths)
    print('Release:', f'https://github.com/{REPOSITORY}/releases/tag/{tag}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
