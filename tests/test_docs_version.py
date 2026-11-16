"""文档版本守卫：说明文件必须跟着实际版本走，不能只替换版本号字符串。

发版时最容易出的两类偏差是「忘了改版本号」和「只改了版本号、内容还是上一版」。
前者靠标题与源码包名的比对拦住，后者靠「发布说明里必须有一段实质小节」拦住。

使用说明是用户手册，只写怎么用；「本版修了什么」属于发布说明。
因此版本小节写在 发布说明.md 里校验，使用说明里出现版本小节反而视为越界。
"""
import re
import unittest
from pathlib import Path

import doc_links
import release_bundle
import app_version

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / '使用说明.md'
AGENT = ROOT / 'Agent接入说明.md'
NOTES = ROOT / '发布说明.md'
RELEASING = ROOT / 'docs' / 'internal' / '发布流程.md'
INDEX = ROOT / 'docs' / '文档索引.md'

SOURCE_ZIP = re.compile(r'DevMux-(\d+\.\d+\.\d+)-source\.zip')

# 一个版本小节至少要写这么多字、这么多条，空壳或只改标题会在这里失败。
MIN_SECTION_CHARS = 200
MIN_SECTION_BULLETS = 3

# 使用说明里不该出现的版本化标题/括注，例如「### 3.1.8 …」「（v2.3.1）」。
VERSION_HEADING = re.compile(r'^#{2,4} .*?\bv?\d+\.\d+\.\d+', re.M)
VERSION_PAREN = re.compile(r'（v\d+\.\d+(?:\.\d+)?')

# 文档命名统一用中文（「对象+文档类型」），只留三个惯例例外。
CONVENTIONAL_NAMES = frozenset({'README.md', 'LICENSE', 'AGENTS.md'})
CJK = re.compile(r'[\u4e00-\u9fff]')


def read(path):
    return path.read_text(encoding='utf-8')


def release_section(text, version):
    """取出 ``# DevMux <version>`` 到下一个一级标题之间的正文。"""
    heading = f'# DevMux {version}'
    start = text.index(heading) + len(heading)
    body = text[start:]
    cut = body.find('\r\n# DevMux ')
    if cut >= 0:
        body = body[:cut]
    return body.strip()


class DocsVersionTests(unittest.TestCase):
    def test_guide_title_matches_version(self):
        first = read(GUIDE).splitlines()[0]
        self.assertEqual(first, f'# DevMux v{app_version.APP_VERSION} 使用说明')

    def test_guide_stays_a_manual(self):
        """使用说明只写功能与操作，不承载版本变化叙述。"""
        text = read(GUIDE)
        found = VERSION_HEADING.findall(text) + VERSION_PAREN.findall(text)
        self.assertEqual(found, [],
                         f'使用说明不应出现版本化小节约 {len(found)} 处，'
                         f'版本变化请写进 发布说明.md：{found[:3]}')

    def test_release_notes_section_is_substantive(self):
        text = read(NOTES)
        heading = f'# DevMux {app_version.APP_VERSION}'
        self.assertIn(heading, text, '发布说明缺少本版本小节：发版必须写清本版实际变化')
        body = release_section(text, app_version.APP_VERSION)
        self.assertGreaterEqual(len(body), MIN_SECTION_CHARS,
                                f'本版本小节仅 {len(body)} 字，疑似只改版本号')
        bullets = [line for line in body.splitlines() if line.startswith('- ')]
        self.assertGreaterEqual(len(bullets), MIN_SECTION_BULLETS,
                                f'本版本小节仅 {len(bullets)} 条要点，应逐条写出实际变化')

    def test_source_zip_references_use_current_version(self):
        for path in (GUIDE, AGENT):
            found = set(SOURCE_ZIP.findall(read(path)))
            self.assertTrue(found, f'{path.name} 未提及源码包名')
            self.assertEqual(found, {app_version.APP_VERSION},
                             f'{path.name} 残留旧版本源码包名：{sorted(found)}')

    def test_release_notes_lead_with_current_version(self):
        first = read(NOTES).splitlines()[0]
        self.assertEqual(first, f'# DevMux {app_version.APP_VERSION}')

    def test_releasing_doc_references_current_tag(self):
        self.assertIn(f'v{app_version.APP_VERSION}', read(RELEASING))

    def test_shipped_documents_exist(self):
        missing = [name for name in release_bundle.DOCUMENTS if not (ROOT / name).is_file()]
        self.assertEqual(missing, [], f'发布清单里的文档不存在：{missing}')

    def test_shipped_documents_links_resolve_inside_the_set(self):
        """随包文档的相对链接必须落在发布清单内，否则解压后的 ZIP 会有死链。"""
        problems = doc_links.check_paths([ROOT / name for name in release_bundle.DOCUMENTS], ROOT)
        problems += doc_links.check_closed_set(list(release_bundle.DOCUMENTS), ROOT)
        readable = [f'{path.name} -> {target}' for path, target in problems]
        self.assertEqual(readable, [], '随包文档存在断链或指向发布集之外：' + '; '.join(readable))

    def test_every_shipped_document_is_in_the_index(self):
        """新增随包文档必须登记到 docs/文档索引.md，避免文档地图失真。"""
        index = read(INDEX)
        missing = [name for name in release_bundle.DOCUMENTS if name not in index]
        self.assertEqual(missing, [], f'文档索引未登记：{missing}')

    def test_document_filenames_are_chinese(self):
        """文档名统一用中文（对象+文档类型），避免中英混排的目录表。"""
        paths = list(ROOT.glob('*.md'))
        paths += list((ROOT / 'docs').rglob('*.md'))
        paths += list((ROOT / 'vendor' / 'usb-relay').glob('*.md'))
        bad = sorted(str(path.relative_to(ROOT)) for path in paths
                     if path.name not in CONVENTIONAL_NAMES and not CJK.search(path.name))
        self.assertEqual(bad, [], f'文档名应为中文（README/LICENSE/AGENTS 例外）：{bad}')

    def test_internal_docs_stay_out_of_the_package(self):
        internal = [name for name in release_bundle.DOCUMENTS if 'internal' in name]
        self.assertEqual(internal, [], f'内部文档不应随包分发：{internal}')
        problems = [name for name in ('docs/internal/发布流程.md', 'docs/internal/跨机中转设计.md')
                    if not (ROOT / name).is_file()]
        self.assertEqual(problems, [], f'内部文档不在 docs/internal/：{problems}')


def run():
    import sys
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])).wasSuccessful()
