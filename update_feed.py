"""更新清单的来源与读取：主程序与官方插件共用同一份 latest.json。

客户端不解析 api.github.com 的发行版对象，只读发布时生成的自建清单：换托管方式
（GitHub / Gitee / CDN / 内网）不需要改客户端，也不会被 API 的每小时限额卡住。
"""
import json
import os
import urllib.request

from app_version import APP_VERSION

# releases/latest/download 是稳定地址，不经过 API 限流。
MANIFEST_URLS = (
    'https://github.com/CuriousMuggles/DevMux/releases/latest/download/latest.json',
)
# GitHub 不可达时回退到 Gitee：同一个 Release 里也上传 latest.json，靠 API 找到最新发行版。
GITEE_REPOSITORY = 'liudaiyang/DevMux'
GITEE_LATEST_RELEASE = 'https://gitee.com/api/v5/repos/' + GITEE_REPOSITORY + '/releases/latest'
MANIFEST_LIMIT = 512 * 1024


def manifest_urls():
    """测试与内网镜像的注入点：单个地址直接替代，多个地址用逗号分隔追加。"""
    override = os.environ.get('DEVMUX_UPDATE_MANIFEST', '').strip()
    if override:
        return (override,)
    extra = [item.strip() for item in os.environ.get('DEVMUX_UPDATE_MIRRORS', '').split(',') if item.strip()]
    return MANIFEST_URLS + tuple(extra)


def build_request(url):
    return urllib.request.Request(url, headers={
        'User-Agent': 'DevMux/' + APP_VERSION,
        'Accept': 'application/json, application/octet-stream;q=0.9,*/*;q=0.8',
    })


def _read_manifest(url, timeout):
    with urllib.request.urlopen(build_request(url), timeout=timeout) as response:
        status = getattr(response, 'status', 200)
        if status and status != 200:
            raise ValueError('HTTP ' + str(status))
        payload = response.read(MANIFEST_LIMIT + 1)
    if len(payload) > MANIFEST_LIMIT:
        raise ValueError('清单过大')
    manifest = json.loads(payload.decode('utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ValueError('清单格式不支持')
    return manifest


def gitee_manifest_url(timeout=8):
    """从 Gitee 最新发行版里取 latest.json 附件的下载地址。"""
    with urllib.request.urlopen(build_request(GITEE_LATEST_RELEASE), timeout=timeout) as response:
        payload = response.read(MANIFEST_LIMIT + 1)
    release = json.loads(payload.decode('utf-8'))
    for asset in release.get('assets') or []:
        if asset.get('name') == 'latest.json':
            url = str(asset.get('browser_download_url') or '').strip()
            if url:
                return url
    raise ValueError('Gitee 最新发行版缺 latest.json 附件')


def fetch_manifest(timeout=8):
    """返回 (清单, 来源地址)；全部来源失败时抛 RuntimeError 并带上每个来源的原因。"""
    errors = []
    for url in manifest_urls():
        try:
            return _read_manifest(url, timeout), url
        except Exception as error:
            errors.append(url + ': ' + str(error))
    # 显式注入的清单（测试/内网）是唯一来源，不再去 Gitee 猜。
    if not os.environ.get('DEVMUX_UPDATE_MANIFEST', '').strip():
        try:
            url = gitee_manifest_url(timeout)
            return _read_manifest(url, timeout), url
        except Exception as error:
            errors.append('Gitee: ' + str(error))
    raise RuntimeError('更新清单不可用（' + '；'.join(errors) + '）')
