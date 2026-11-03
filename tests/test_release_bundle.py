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
            self.assertTrue({'README.md','使用说明.md','AGENT_USAGE.md','DevMux.exe'} <= actual)

    def test_source_hashes_exclude_runtime_and_build_output(self):
        hashes = release_bundle.source_hashes()
        self.assertIn('agent_api.py', hashes)
        self.assertFalse(any(name.startswith(('dist/', 'build/', 'devmux-data/')) for name in hashes))


def run():
    import sys
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])).wasSuccessful()
