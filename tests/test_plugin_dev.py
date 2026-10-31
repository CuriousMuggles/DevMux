"""Plugin scaffold, resource bundling and isolated development server contracts."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PluginDevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def scaffold(self):
        from plugin_dev import create
        return create(self.root / 'source', 'sdk-test', name='SDK Test')

    def test_scaffold_check_pack_and_runtime(self):
        from plugin_dev import check
        from plugin_pack import pack
        from plugin_manager import PluginManager
        source = self.scaffold()
        manifest = check(source)
        self.assertEqual(manifest['id'], 'sdk-test')
        archive = pack(source, self.root / 'sdk-test.dmp')
        manager = PluginManager(self.root / 'installed')
        self.addCleanup(manager.close)
        manager.dispatch('install', {'package': base64.b64encode(archive.read_bytes()).decode()})
        manager.dispatch('enable', {'id': 'sdk-test'})
        html = manager.dispatch('page', {'id': 'sdk-test'})['html']
        self.assertIn('DevMuxPlugin', html)
        self.assertNotIn('src="devmux:', html)
        self.assertNotIn('href="devmux:', html)
        manager.dispatch('disable', {'id': 'sdk-test'})
        manager.dispatch('uninstall', {'id': 'sdk-test'})
        self.assertEqual(manager.store.list(), [])

    def test_create_refuses_overwrite_and_invalid_identifier(self):
        from plugin_dev import create
        source = self.scaffold()
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        with self.assertRaises((ValueError, FileExistsError)):
            create(source, 'replacement')
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError):
            create(self.root / 'bad', '../bad')

    def test_check_reports_missing_backend_and_unsupported_api(self):
        from plugin_dev import check
        source = self.scaffold()
        manifest_path = source / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        (source / manifest['backend']).unlink()
        with self.assertRaises(ValueError):
            check(source)
        manifest['apiVersion'] = 999
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ValueError):
            check(source)

    def test_cli_create_check_pack_and_invalid_manifest_exit_status(self):
        source=self.root/'cli-source'
        def cli(*arguments):
            return subprocess.run([sys.executable,'-X','utf8',str(ROOT/'plugin_dev.py'),*map(str,arguments)],capture_output=True,text=True,encoding='utf-8',timeout=20,cwd=self.root)
        created=cli('create',source,'--id','cli-test','--name','CLI Test')
        self.assertEqual(created.returncode,0,created.stderr)
        checked=cli('check',source)
        self.assertEqual(checked.returncode,0,checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['manifest']['id'],'cli-test')
        output=self.root/'cli-test.dmp'
        packed=cli('pack',source,output)
        self.assertEqual(packed.returncode,0,packed.stderr)
        self.assertTrue(zipfile.is_zipfile(output))
        (source/'manifest.json').write_text('{}',encoding='utf-8')
        self.assertNotEqual(cli('check',source).returncode,0)

    def test_bundle_local_resources_and_reject_external_or_traversal(self):
        from plugin_pack import pack
        source = self.scaffold()
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        page = source / manifest['frontend']
        (source / 'local.js').write_text('window.bundleMarker="bundled-script";', encoding='utf-8')
        (source / 'local.css').write_text('.bundled-style{color:red}', encoding='utf-8')
        page.write_text('<!doctype html><link rel="stylesheet" href="local.css"><script src="devmux:sdk.js"></script><script src="local.js"></script>', encoding='utf-8')
        output = self.root / 'bundle.dmp'
        with zipfile.ZipFile(pack(source, output)) as archive:
            html = archive.read(manifest['frontend']).decode()
        self.assertIn('bundled-script', html)
        self.assertIn('bundled-style', html)
        self.assertIn('DevMuxPlugin', html)
        for reference in ('../outside.js', 'https://example.invalid/code.js', '//example.invalid/code.js'):
            page.write_text('<script src="' + reference + '"></script>', encoding='utf-8')
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                pack(source, output)

    def test_backend_sdk_disposes_all_resources_once_in_reverse_order(self):
        from plugin_sdk import Plugin
        plugin=Plugin(self.root/'installed'/'demo')
        self.assertEqual(plugin.data_dir,(self.root/'installed'/'.data'/'demo').resolve())
        cleaned=[]
        plugin.on_dispose(lambda:cleaned.append('first'))
        def failing_cleanup():
            cleaned.append('second')
            raise RuntimeError('expected cleanup failure')
        plugin.on_dispose(failing_cleanup)
        plugin.on_dispose(lambda:cleaned.append('third'))
        with self.assertLogs(plugin.logger,level='ERROR'):
            plugin.dispose()
        plugin.dispose()
        self.assertEqual(cleaned,['third','second','first'])
        with self.assertRaises(RuntimeError):plugin.on_dispose(lambda:None)

    def test_dev_server_reads_source_and_restarts_backend_without_install(self):
        source = self.scaffold()
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        backend = source / manifest['backend']
        def backend_source(value):
            return 'class Demo:\n    def call(self, action, arguments): return {"success":True,"data":{"marker":' + repr(value) + '}}\n    def close(self): pass\ndef create_plugin(root): return Demo()\n'
        backend.write_text(backend_source('before'), encoding='utf-8')
        from plugin_dev import DevServer
        import threading
        from urllib.error import HTTPError
        server=DevServer(source,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():
            server.shutdown();server.server_close();thread.join(timeout=10)
        self.addCleanup(stop)
        url='http://127.0.0.1:'+str(server.server_port)
        with self.assertRaises(HTTPError) as rejected:
            urlopen(url+'/page',timeout=10)
        self.assertEqual(rejected.exception.code,403)
        def request(path, data=None):
            req = Request(url + path, data=None if data is None else json.dumps(data).encode(), headers={'Content-Type': 'application/json','Authorization':'Bearer '+server.token})
            with urlopen(req, timeout=15) as response: return response.read().decode()
        html = request('/page')
        self.assertIn('DevMuxPlugin', html)
        message = {'type': 'devmux-plugin-call', 'id': 'test', 'action': 'status', 'arguments': {}}
        self.assertIn('before', request('/rpc', message))
        page = source / manifest['frontend']
        page.write_text(page.read_text(encoding='utf-8') + '<div>fresh-source-page</div>', encoding='utf-8')
        self.assertIn('fresh-source-page', request('/page'))
        backend.write_text(backend_source('after-reload'), encoding='utf-8')
        request('/reload', {})
        self.assertIn('after-reload', request('/rpc', message))
        denied=json.loads(request('/rpc',{'type':'devmux-host-call','id':2,'hostType':'pick-file'}))
        self.assertFalse(denied['success'])
        with self.assertRaises(HTTPError):request('/rpc',{'type':'devmux-plugin-call','id':3,'action':'__shutdown__'})
        self.assertIn('after-reload',request('/rpc',message))
        backend.write_text(backend_source('busy').replace('    def close(self): pass','    def can_stop(self): return False\n    def close(self): pass'),encoding='utf-8')
        request('/reload',{})
        with self.assertRaises(HTTPError):request('/reload',{})
        self.assertIn('busy',request('/rpc',message))
        self.assertFalse(list(self.root.glob('plugins-*')))
        self.assertFalse((source / '.data').exists())


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PluginDevTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
