"""随包文档清单与链接校验：清单里的文档互相引用必须闭合。

发布包里最容易被忽略的失效方式是「文档搬了家、链接没跟着改」：
在仓库里点得开，解压后的 ZIP 里却是死链。本模块同时定义随包文档清单
（``DOCUMENTS``）——发布打包与链接校验共用同一份定义，不会各自漂移。

命令行用法（仓库根目录）：``py doc_links.py``。
"""
import re
import sys
from pathlib import Path

# 随包文档：用户能读到的全部内容。一个主题只有一份权威文档，其余链接过去。
DOCUMENTS = ('README.md', '使用说明.md', 'Agent接入说明.md', '发布说明.md',
             'LICENSE', '第三方许可.md', 'docs/插件开发说明.md',
             'docs/使用说明/串口使用说明.md', 'vendor/usb-relay/来源记录.md')

# 早先随包、现已降为内部文档或改了名的文件：同步本机运行目录时一并清掉，避免残留上一版说明。
STALE_RUNTIME_DOCS = ('docs/releasing.md', 'docs/新手使用与发布需求.md',
                      'docs/使用说明/双端交互流程.md', 'docs/REMOTE_TOOLS.md',
                      'docs/优化建议清单.md',
                      # v3.1.8 起的英文名文档，已统一改为中文名
                      'AGENT_USAGE.md', 'RELEASE-NOTES.md', 'THIRD_PARTY_LICENSES.md',
                      'docs/plugin-development.md', 'vendor/usb-relay/PROVENANCE.md')

# ``[文字](目标)``；目标里允许括号，但不允许空格（本项目约定路径不含空格）。
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
SKIP_PREFIX = ('http://', 'https://', 'mailto:', '#', 'file:')


def iter_links(path):
    """产出 ``path`` 里所有相对链接的原始目标（含锚点）。"""
    text = Path(path).read_text(encoding='utf-8', errors='ignore')
    for target in LINK.findall(text):
        if target.startswith(SKIP_PREFIX):
            continue
        yield target


def resolve(path, target):
    """把链接目标解析成绝对路径；纯锚点返回 None。"""
    local = target.split('#', 1)[0]
    if not local:
        return None
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
    problems = check_paths([root / name for name in documents], root)
    outside = check_closed_set(documents, root)
    for path, target in problems:
        print('BROKEN      %s -> %s' % (path.relative_to(root), target))
    for path, target in outside:
        print('OUTSIDE-SET %s -> %s' % (path, target))
    total = len(problems) + len(outside)
    print('文档 %d 份，问题 %d 处' % (len(documents), total))
    return 0 if not total else 1


if __name__ == '__main__':
    sys.exit(main())
