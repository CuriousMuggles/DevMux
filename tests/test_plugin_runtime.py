import base64
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from plugin_manager import PluginManager
from plugin_pack import pack

class RuntimeTest(unittest.TestCase):
    def test_lifecycle_and_window_resource(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            package = pack(Path(__file__).resolve().parents[1] / 'plugins/example', Path(temp) / 'example.dmp')
            manager = PluginManager(Path(temp) / 'installed')
            resources.callback(manager.close)
            manager.dispatch('install', {'package':base64.b64encode(package.read_bytes()).decode()})
            manager.dispatch('enable', {'id':'example'})
            self.assertTrue(manager.dispatch('list', {})[0]['enabled'])
            self.assertEqual(manager.dispatch('call', {'id':'example','action':'echo','arguments':{'text':'你好'}}), {'text':'你好'})
            self.assertIn('devmux-plugin-call', manager.dispatch('page', {'id':'example'})['html'])
            with self.assertRaises(ValueError):
                manager.dispatch('uninstall', {'id':'example'})
            manager.dispatch('disable', {'id':'example'})
            manager.dispatch('enable', {'id':'example'})
            manager.dispatch('disable', {'id':'example'})
            manager.dispatch('uninstall', {'id':'example'})
            self.assertEqual(manager.dispatch('list', {}), [])

    def test_relay_package_config_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            package = pack(Path(__file__).resolve().parents[1] / 'plugins/usb-relay', Path(temp) / 'relay.dmp')
            manager = PluginManager(Path(temp) / 'installed')
            resources.callback(manager.close)
            manager.dispatch('install', {'package':base64.b64encode(package.read_bytes()).decode()})
            manager.dispatch('enable', {'id':'usb-relay'})
            result = manager.dispatch('call', {'id':'usb-relay','action':'config'})
            self.assertTrue(result['success'])
            self.assertFalse(result['data']['opened'])
            manager.dispatch('disable', {'id':'usb-relay'})

    def test_timeout_stops_process_and_does_not_retry(self):
        import json
        from plugin_process import PluginProcess
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            root = Path(temp)
            (root / 'manifest.json').write_text(json.dumps({'backend':'main.py'}))
            (root / 'main.py').write_text("import time\nclass Plugin:\n def call(self, action, args):\n  time.sleep(60)\ndef create_plugin(root): return Plugin()\n")
            process = PluginProcess(root)
            resources.callback(process.close)
            process.call('__ready__')
            with self.assertRaises(TimeoutError):
                process.call('hang', timeout=.05)
            self.assertIsNotNone(process.process.poll())

    def test_db_and_mijia_packages_start_without_builtin_services(self):
        for identifier, action in [('db-flash', 'config'), ('mijia', 'status')]:
            with self.subTest(plugin=identifier), tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
                package = pack(Path(__file__).resolve().parents[1] / 'plugins' / identifier, Path(temp) / 'plugin.dmp')
                manager = PluginManager(Path(temp) / 'installed')
                resources.callback(manager.close)
                manager.dispatch('install', {'package':base64.b64encode(package.read_bytes()).decode()})
                manager.dispatch('enable', {'id':identifier})
                self.assertTrue(manager.dispatch('call', {'id':identifier,'action':action})['success'])
                self.assertIn('<script>', manager.dispatch('page', {'id':identifier})['html'])
                manager.dispatch('disable', {'id':identifier})
                manager.dispatch('uninstall', {'id':identifier})

    def test_remote_registration_permissions_and_removal(self):
        from extension_tools import ExtensionTools
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            registry=ExtensionTools({})
            manager=PluginManager(Path(temp)/'installed',registry)
            resources.callback(manager.close)
            for identifier,tool in [('usb-relay','relay'),('db-flash','db-flash')]:
                package=pack(Path(__file__).resolve().parents[1]/'plugins'/identifier,Path(temp)/(identifier+'.dmp'))
                manager.dispatch('install',{'package':base64.b64encode(package.read_bytes()).decode()})
                self.assertNotIn(tool,registry.services)
                manager.dispatch('enable',{'id':identifier})
                self.assertFalse(registry.call(tool,'capabilities',remote=True)['data']['allowed'])
                registry.call(tool,'permission',{'allowed':True})
                self.assertTrue(registry.call(tool,'capabilities',remote=True)['data']['allowed'])
                if tool=='db-flash':
                    result=registry.call(tool,'plan',{'pythonPath':'evil.exe'},remote=True,authorize=lambda:'master')
                    self.assertFalse(result['success'])
                manager.dispatch('disable',{'id':identifier})
                self.assertNotIn(tool,registry.services)

    def test_remote_catalog_requires_verified_owner_and_excludes_private_metadata(self):
        from extension_tools import ExtensionTools
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            registry = ExtensionTools({})
            manager = PluginManager(Path(temp) / 'installed', registry)
            registry.plugin_manager = manager
            resources.callback(manager.close)
            package = pack(Path(__file__).resolve().parents[1] / 'plugins/usb-relay', Path(temp) / 'relay.dmp')
            manager.dispatch('install', {'package':base64.b64encode(package.read_bytes()).decode()})
            with self.assertRaises(PermissionError):
                registry.call('__plugin_catalog__', 'list', remote=True, authorize=lambda: '')
            result=registry.call('__plugin_catalog__', 'list', remote=True, authorize=lambda: 'verified-node')
            self.assertEqual(result['data']['plugins'][0]['id'], 'usb-relay')
            self.assertFalse(result['data']['plugins'][0]['enabled'])
            self.assertNotIn('backend', result['data']['plugins'][0])
            manager.dispatch('enable', {'id':'usb-relay'})
            self.assertTrue(registry.call('__plugin_catalog__','list',remote=True,authorize=lambda:'verified-node')['data']['plugins'][0]['enabled'])

if __name__ == '__main__':
    unittest.main()


def run():
    import sys
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    return result.wasSuccessful()
