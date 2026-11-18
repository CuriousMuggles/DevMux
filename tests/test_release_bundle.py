"""Check actual staged packages and hashes, without building a second executable."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import release_bundle
from release_plugins import OFFICIAL_PLUGINS


class ReleaseBundleTests(unittest.TestCase):
    def test_delivery_allowlist_packages_and_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'DevMux.exe'
            binary.write_bytes(b'fixture-binary')
            output = root / 'delivery'
            info = {'commit':'a'*40, 'status':'candidate', 'dirty':True}
            release_bundle.stage_delivery(output, binary, info)
            self.assertEqual(json.loads((output / 'BUILD-INFO.json').read_text(encoding='utf-8')), info)
            identifiers = set()
            for path in (output / 'plugin').glob('*.dmp'):
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                identifiers.add(manifest['id'])
                self.assertEqual(path.name, manifest['id'] + '-' + manifest['version'] + '.dmp')
            self.assertEqual(identifiers, set(OFFICIAL_PLUGINS))
            listed = set()
            for line in (output / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
                expected, name = line.split('  ', 1)
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
                listed.add(name)
            actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(listed, actual - {'SHA256SUMS.txt'})
            self.assertFalse(any('devmux-data' in name or '.log' in name for name in actual))
            self.assertTrue({'README.md','使用说明.md','开发者说明.md','DevMux.exe'} <= actual)

    def test_source_hashes_exclude_runtime_and_build_output(self):
        hashes = release_bundle.source_hashes()
        self.assertIn('agent_api.py', hashes)
        self.assertFalse(any(name.startswith(('dist/', 'build/', 'devmux-data/')) for name in hashes))

    def test_sync_runtime_docs_mirrors_whole_set_and_prunes_stale(self):
        """dist/ 要与发布包同一份文档集：缺的补上、旧的清掉、用户文件不动。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'dist'
            (runtime / 'docs').mkdir(parents=True)
            (root / 'doc-a.md').write_text('new a', encoding='utf-8')
            (root / 'doc-b.md').write_text('new b', encoding='utf-8')
            (root / 'docs' / 'sub').mkdir(parents=True)
            (root / 'docs' / 'sub' / 'doc-c.md').write_text('new c', encoding='utf-8')
            (runtime / 'doc-a.md').write_text('stale a', encoding='utf-8')   # 过期 → 覆盖
            (runtime / 'docs' / 'releasing.md').write_text('old', encoding='utf-8')  # 降为内部 → 删除
            (runtime / 'user-note.txt').write_text('keep', encoding='utf-8')  # 用户文件 → 保留
            originals = (release_bundle.ROOT, release_bundle.DOCUMENTS, release_bundle.STALE_RUNTIME_DOCS)
            release_bundle.ROOT = root
            release_bundle.DOCUMENTS = ('doc-a.md', 'doc-b.md', 'docs/sub/doc-c.md')
            release_bundle.STALE_RUNTIME_DOCS = ('docs/releasing.md',)
            try:
                report = release_bundle.sync_runtime_docs()
                self.assertEqual(sorted(report['updated']),
                                 ['doc-a.md', 'doc-b.md', 'docs/sub/doc-c.md'])
                self.assertEqual(report['removed'], ['docs/releasing.md'])
                self.assertEqual(report['problems'], [])
                self.assertEqual((runtime / 'doc-a.md').read_text(encoding='utf-8'), 'new a')
                self.assertTrue((runtime / 'docs' / 'sub' / 'doc-c.md').is_file())
                self.assertFalse((runtime / 'docs' / 'releasing.md').exists())
                self.assertEqual((runtime / 'user-note.txt').read_text(encoding='utf-8'), 'keep')
                again = release_bundle.sync_runtime_docs()
                self.assertEqual((again['updated'], again['removed']), ([], []))  # 幂等
            finally:
                (release_bundle.ROOT, release_bundle.DOCUMENTS,
                 release_bundle.STALE_RUNTIME_DOCS) = originals

    def test_verify_runtime_flags_missing_docs_and_plugins(self):
        """dist/ 自洽性：缺文档、缺官方插件包都要报出来，齐全时为零问题。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'dist'
            runtime.mkdir()
            originals = (release_bundle.ROOT, release_bundle.DOCUMENTS)
            release_bundle.ROOT = root
            release_bundle.DOCUMENTS = ('doc-a.md',)
            try:
                problems = release_bundle.verify_runtime()
                self.assertTrue(any('doc-a.md' in item for item in problems))
                self.assertTrue(any('usb-relay' in item for item in problems))
                (root / 'doc-a.md').write_text('a', encoding='utf-8')
                (runtime / 'doc-a.md').write_text('a', encoding='utf-8')
                (runtime / 'plugin').mkdir()
                for identifier in release_bundle.OFFICIAL_PLUGINS:
                    (runtime / 'plugin' / (identifier + '-1.0.0.dmp')).write_bytes(b'x')
                self.assertEqual(release_bundle.verify_runtime(), [])
            finally:
                release_bundle.ROOT, release_bundle.DOCUMENTS = originals

    def test_sync_runtime_docs_skips_when_runtime_dir_missing(self):
        originals = release_bundle.ROOT
        with tempfile.TemporaryDirectory() as temp:
            release_bundle.ROOT = Path(temp)
            try:
                self.assertEqual(release_bundle.sync_runtime_docs()['updated'], [])
            finally:
                release_bundle.ROOT = originals


def run():
    import sys
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])).wasSuccessful()
