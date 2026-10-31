import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from plugin_store import PluginStore

class PluginStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = PluginStore(self.root / 'installed')
        self.addCleanup(self.tmp.cleanup)

    def package(self, version='1', extra=None):
        path = self.root / 'package.dmp'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('manifest.json', json.dumps({'id':'relay', 'name':'Relay', 'version':version, 'apiVersion':1, 'backend':'backend/main.py'}))
            z.writestr('backend/main.py', 'pass')
            if extra:
                z.writestr(extra, 'bad')
        return path

    def test_install_update_remove(self):
        self.store.install(self.package())
        self.assertEqual(self.store.list()[0]['version'], '1')
        self.store.install(self.package('2'))
        self.assertEqual(self.store.list()[0]['version'], '2')
        self.store.uninstall('relay')
        self.assertEqual(self.store.list(), [])

    def test_invalid_update_keeps_previous_version(self):
        self.store.install(self.package())
        with self.assertRaises(ValueError):
            self.store.install(self.package('2', '../escape'))
        self.assertEqual(self.store.list()[0]['version'], '1')
        self.assertFalse((self.root / 'escape').exists())

    def test_manifest_contract_rejects_unsafe_paths_actions_and_permissions(self):
        valid={'id':'relay','name':'Relay','version':'1','apiVersion':1,'backend':'main.py'}
        changes=[{'backend':'../main.py'},{'backend':'main.js'},{'frontend':'/index.html'},{'frontend':'../index.html'},{'actions':['__ready__']},{'actions':['open','open']},{'localActions':['__ready__']},{'localActions':['ping','ping']},{'localActions':'ping'},{'permissions':['shell']},{'window':{'width':True}}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ValueError):
                PluginStore.validate({**valid,**change})

    def test_missing_entry_is_rejected_without_replacing_installed_plugin(self):
        self.store.install(self.package())
        path=self.root/'missing.dmp'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('manifest.json',json.dumps({'id':'relay','name':'Relay','version':'2','apiVersion':1,'backend':'missing.py'}))
        with self.assertRaises(ValueError):self.store.install(path)
        self.assertEqual(self.store.list()[0]['version'],'1')

    def test_invalid_id_cannot_remove_other_files(self):
        with self.assertRaises(ValueError):
            self.store.uninstall('../outside')

if __name__ == '__main__':
    unittest.main()


def run():
    import sys
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    return result.wasSuccessful()
