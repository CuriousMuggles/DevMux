"""随包文档清单与链接校验：清单里的文档互相引用必须闭合。

发布包里最容易被忽略的失效方式是「文档搬了家、链接没跟着改」：
在仓库里点得开，解压后的 ZIP 里却是死链。本模块同时定义随包文档清单
（``DOCUMENTS``）——发布打包与链接校验共用同一份定义，不会各自漂移。

命令行用法（仓库根目录）：``py doc_links.py``。
"""
import os
import posixpath
import re
import sys
from pathlib import Path

# 随包文档：用户能读到的全部内容。一个主题只有一份权威文档，其余链接过去。
# 这里的路径是**交付/解压后的位置**（ZIP 内的相对路径），与仓库内源文件位置解耦，
# 由 ``DOC_SOURCES`` 映射；仓库按功能分目录，交付仍保持用户熟悉的扁平结构。
DOCUMENTS = ('README.md', '使用说明.md', '开发者说明.md', '发布说明.md',
             'LICENSE', '第三方许可.md', 'vendor/usb-relay/来源记录.md')

# 交付位置 -> 仓库源文件位置。未列出的表示两者相同（README、LICENSE、来源记录）。
DOC_SOURCES = {
    '使用说明.md': 'docs/用户/使用说明.md',
    '开发者说明.md': 'docs/用户/开发者说明.md',
    '发布说明.md': 'docs/用户/发布说明.md',
    '第三方许可.md': 'docs/用户/第三方许可.md',
}


def source_of(name):
    """交付位置对应的仓库源文件路径（相对仓库根）。"""
    return DOC_SOURCES.get(name, name)


# 源文件 -> 交付位置：显式映射 + 原样交付（README/LICENSE/来源记录）。
DELIVERY_BY_SOURCE = {source_of(name): name for name in DOCUMENTS}

# 仓库根相对链接（``](/docs/...)``），发布时改写成交付相对路径。
ROOT_LINK = re.compile(r'\]\((/[^)\s]+)(#[^)]*)?\)')


def delivery_bytes(name, root=None):
    """返回某份随包文档在**交付位置**应写出的字节。

    Markdown 里的仓库根相对链接（如 ``/docs/用户/使用说明.md``）会按
    ``DOC_SOURCES`` 反查成交付路径，并改写为从该文档交付位置出发的相对链接；
    LICENSE 等非 Markdown 文件原样返回。``root`` 供测试或调用方指定另一套
    源文件根目录，默认用仓库根。
    """
    source = (Path(root) if root else REPO_ROOT) / source_of(name)
    raw = source.read_bytes()
    if not str(name).lower().endswith('.md'):
        return raw
    text = raw.decode('utf-8')

    def replace(match):
        target, anchor = match.group(1), match.group(2) or ''
        delivery = DELIVERY_BY_SOURCE.get(target.lstrip('/'))
        if delivery is None:
            return match.group(0)
        relative = posixpath.relpath(delivery, posixpath.dirname(name) or '.')
        return '](%s%s)' % (relative, anchor)

    return ROOT_LINK.sub(replace, text).encode('utf-8')

# 早先随包、现已降为内部文档或改了名的文件：同步本机运行目录时一并清掉，避免残留上一版说明。
STALE_RUNTIME_DOCS = ('docs/releasing.md', 'docs/新手使用与发布需求.md',
                      'docs/使用说明/双端交互流程.md', 'docs/REMOTE_TOOLS.md',
                      'docs/优化建议清单.md',
                      # v3.1.8 起的英文名文档，已统一改为中文名
                      'AGENT_USAGE.md', 'RELEASE-NOTES.md', 'THIRD_PARTY_LICENSES.md',
                      'docs/plugin-development.md', 'vendor/usb-relay/PROVENANCE.md',
                      # 使用说明合并为「用户手册 + 开发者手册」后的旧交付名
                      'Agent接入说明.md', 'docs/插件开发说明.md',
                      'docs/使用说明/串口使用说明.md')

# ``[文字](目标)``；目标里允许括号，但不允许空格（本项目约定路径不含空格）。
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
SKIP_PREFIX = ('http://', 'https://', 'mailto:', '#', 'file:')
REPO_ROOT = Path(__file__).resolve().parent


def iter_links(path):
    """产出 ``path`` 里所有相对链接的原始目标（含锚点）。"""
    text = Path(path).read_text(encoding='utf-8', errors='ignore')
    for target in LINK.findall(text):
        if target.startswith(SKIP_PREFIX):
            continue
        yield target


def resolve(path, target):
    """把链接目标解析成绝对路径；纯锚点返回 None。

    以 ``/`` 开头的目标按**仓库根**解析（内部文档统一用这种写法，避免
    ``../../`` 链随目录移动而失效）；其余仍按文档所在目录解析，保证发布包
    解压后不依赖仓库结构。
    """
    local = target.split('#', 1)[0]
    if not local:
        return None
    if local.startswith('/'):
        return (REPO_ROOT / local.lstrip('/')).resolve()
    return (Path(path).parent / local).resolve()


def check_paths(paths, root=None):
    """检查给定文档的相对链接是否都存在；返回 ``[(文档, 目标), ...]``。"""
    root = Path(root).resolve() if root else None
    problems = []
    for path in paths:
        path = Path(path)
        for target in iter_links(path):
            resolved = resolve(path, target)
            if resolved is None:
                continue
            if not resolved.exists():
                problems.append((path, target))
            elif root is not None and root not in resolved.parents and resolved != root:
                problems.append((path, target + '（指向发布集之外）'))
    return problems


def check_delivery(documents=None, root=None):
    """按交付布局校验随包文档链接。

    源文件可以放在仓库任意目录（见 ``DOC_SOURCES``），但文档里的相对链接按
    **解压后的位置**解析 —— 用户在 ZIP 里点开时才是对的。返回 ``[(交付名, 目标)]``。
    """
    root = Path(root).resolve() if root else REPO_ROOT
    names = list(documents or DOCUMENTS)
    allowed = set(names)
    problems = []
    for name in names:
        source = root / source_of(name)
        if not source.is_file():
            problems.append((Path(name), '（源文件缺失）'))
            continue
        for target in iter_links(source):
            local = target.split('#', 1)[0]
            if not local:
                continue
            if local.startswith('/'):
                repo_path = local.lstrip('/')
                delivery = DELIVERY_BY_SOURCE.get(repo_path, posixpath.normpath(repo_path))
                resolved = root / repo_path
            else:
                delivery = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), local))
                resolved = root / source_of(delivery)
            if not resolved.exists():
                problems.append((Path(name), target))
            elif delivery not in allowed:
                problems.append((Path(name), target + '（指向发布集之外）'))
    return problems


def check_closed_set(documents, root):
    """随包文档互相引用时必须闭合：目标文件也得在发布清单里。

    ``documents`` 是相对仓库根的路径列表（发布清单），返回指向清单外文件的链接。
    """
    root = Path(root).resolve()
    allowed = {(root / name).resolve() for name in documents}
    outside = []
    for name in documents:
        path = root / name
        for target in iter_links(path):
            resolved = resolve(path, target)
            if resolved is None or not resolved.exists():
                continue
            if resolved not in allowed and root in resolved.parents and resolved != root:
                outside.append((Path(name), target))
    return outside


def main():
    root = Path(__file__).resolve().parent
    documents = list(DOCUMENTS)
    problems = check_delivery(documents, root)
    for path, target in problems:
        print('BROKEN      %s -> %s' % (path, target))
    total = len(problems)
    print('文档 %d 份，问题 %d 处' % (len(documents), total))
    return 0 if not total else 1


if __name__ == '__main__':
    sys.exit(main())
