import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from extension_tools import ExtensionTools
from plugin_manager import PluginManager
from plugin_pack import pack
from remote_plugins import invoke

class RemotePluginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.registry=ExtensionTools({});self.manager=PluginManager(self.root/'installed',self.registry);self.registry.plugin_manager=self.manager
        self.addCleanup(self.manager.close)
        self.package=pack(Path(__file__).resolve().parents[1]/'plugins/example',self.root/'example.dmp').read_bytes()
    def remote(self,action,args):
        return self.registry.call('__plugin_catalog__',action,args,remote=True,authorize=lambda:'verified-master')['data']
    def test_transfer_installs_only_with_local_permission_and_is_idempotent(self):
        def transport(controller,node,tool,action,args,**kw):
            return self.registry.call(tool,action,args,remote=True,authorize=lambda:'verified-master')
        args={'package':base64.b64encode(self.package).decode()}
        with patch('remote_plugins.remote_call',side_effect=transport):
            with self.assertRaises(PermissionError):invoke(None,'slave','install',args)
            self.manager.dispatch('set-install-policy',{'allowed':True})
            installed=invoke(None,'slave','install',args)
            self.assertEqual(installed['id'],'example')
            self.assertEqual(invoke(None,'slave','install',args),installed)
            self.assertNotIn('example',self.manager.running)
    def test_remote_uninstall_requires_permission_and_allows_reinstall(self):
        def transport(controller,node,tool,action,args,**kw):
            return self.registry.call(tool,action,args,remote=True,authorize=lambda:'verified-master')
        args={'package':base64.b64encode(self.package).decode()}
        with patch('remote_plugins.remote_call',side_effect=transport):
            self.manager.remote.permission(True)
            invoke(None,'slave','install',args)
            self.manager.dispatch('enable',{'id':'example'})
            self.manager.remote.permission(False)
            with self.assertRaises(PermissionError):invoke(None,'slave','uninstall',{'id':'example'})
            self.assertEqual(len(self.manager.store.list()),1)
            self.manager.remote.permission(True)
            invoke(None,'slave','uninstall',{'id':'example'})
            self.assertEqual(self.manager.store.list(),[])
            self.assertNotIn('example',self.manager.running)
            self.assertEqual(invoke(None,'slave','install',args)['id'],'example')

    def test_revoke_during_upload_blocks_finish_and_wrong_hash_is_rejected(self):
        self.manager.remote.permission(True)
        digest=hashlib.sha256(self.package).hexdigest();args={'sha256':digest,'size':len(self.package)}
        self.remote('begin',args)
        block={'sha256':digest,'offset':0,'data':base64.b64encode(self.package).decode()}
        self.assertEqual(self.remote('chunk',block),self.remote('chunk',block))
        self.manager.remote.permission(False)
        with self.assertRaises(PermissionError):self.remote('finish',args)
        self.assertEqual(self.manager.store.list(),[])
        self.manager.remote.permission(True)
        with self.assertRaises(ValueError):self.remote('finish',{'sha256':'0'*64})
        self.assertEqual(self.remote('finish',args)['id'],'example')
    def test_unverified_connection_cannot_discover_or_install(self):
        for action in ('list','begin','uninstall'):
            with self.assertRaises(PermissionError):self.registry.call('__plugin_catalog__',action,{},remote=True,authorize=lambda:'')
    def test_combined_switch_enables_and_revokes_even_when_stopped(self):
        package=pack(Path(__file__).resolve().parents[1]/'plugins/usb-relay',self.root/'relay.dmp')
        self.manager.dispatch('install',{'package':base64.b64encode(package.read_bytes()).decode()})
        self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':True})
        self.assertTrue(self.registry.call('relay','capabilities',remote=True)['data']['allowed'])
        self.manager.dispatch('disable',{'id':'usb-relay'})
        self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':False})
        self.manager.dispatch('enable',{'id':'usb-relay'})
        self.assertFalse(self.registry.call('relay','capabilities',remote=True)['data']['allowed'])
    def test_frontend_cache_never_installs_backend_and_rechecks_permission(self):
        from remote_plugins import frontend
        package=pack(Path(__file__).resolve().parents[1]/'plugins/usb-relay',self.root/'relay.dmp')
        self.manager.dispatch('install',{'package':base64.b64encode(package.read_bytes()).decode()})
        self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':True})
        local=PluginManager(self.root/'host');self.addCleanup(local.close)
        calls=[]
        def transport(controller,node,tool,action,args,**kw):
            calls.append(action)
            return self.registry.call(tool,action,args,remote=True,authorize=lambda:'verified-master')
        with patch('remote_plugins.remote_call',side_effect=transport):
            page=frontend(local,None,'slave','usb-relay')
            self.assertIn('<!doctype html>',page['html'])
            self.assertGreater(calls.count('frontend-chunk'),1)
            self.assertEqual(local.dispatch('list',{}),[])
            self.assertEqual(local.running,{})
            calls.clear()
            self.assertEqual(frontend(local,None,'slave','usb-relay')['html'],page['html'])
            self.assertEqual(calls,['frontend-info'])
            self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':False})
            with self.assertRaises(PermissionError):frontend(local,None,'slave','usb-relay')
        self.assertEqual(len(list((local.store.root/'.frontend-cache').rglob('*.html'))),1)

    def test_frontend_digest_and_connection_are_validated(self):
        from remote_plugins import frontend
        info={'id':'relay','apiVersion':1,'size':3,'sha256':'0'*64}
        def transport(controller,node,tool,action,args,**kw):
            return {'success':True,'data':info if action=='frontend-info' else {'data':base64.b64encode(b'bad').decode()}}
        with patch('remote_plugins.remote_call',side_effect=transport):
            with self.assertRaisesRegex(ValueError,'校验失败'):frontend(self.manager,None,'slave','relay')
        for action in ('frontend-info','frontend-chunk'):
            with self.assertRaises(PermissionError):self.registry.call('__plugin_catalog__',action,{'id':'relay'},remote=True,authorize=lambda:'')

    def test_real_verified_relay_transfers_multiple_chunks(self):
        from tests.test_relay_app import _Harness,_patch,_fake_list_devices,_fake_execute,_wait_until
        import os,shutil
        source=self.root/'source';shutil.copytree(Path(__file__).resolve().parents[1]/'plugins/example',source)
        payload=os.urandom(70000);(source/'payload.bin').write_bytes(payload)
        package=pack(source,self.root/'large.dmp').read_bytes()
        with _patch(list_devices=_fake_list_devices,execute=_fake_execute):
            harness=_Harness(str(self.root/'bus'),master_local_devices_enabled=False)
            harness.slave.extension_tools=self.registry
            try:
                harness.start()
                with self.assertRaises((RuntimeError,ValueError)):invoke(harness.master,harness.slave.config.nodeId,'list',{})
                harness.confirm_both()
                self.assertTrue(_wait_until(lambda:harness.master.node.connection_ready('pc-b')))
                node=harness.slave.config.nodeId
                self.assertFalse(invoke(harness.master,node,'list',{})['installAllowed'])
                self.manager.remote.permission(True)
                result=invoke(harness.master,node,'install',{'package':base64.b64encode(package).decode()})
                self.assertEqual(result['id'],'example')
                self.assertEqual((self.manager.store.root/'example/payload.bin').read_bytes(),payload)
                self.assertFalse(invoke(harness.master,node,'list',{})['plugins'][0]['enabled'])
                relay=pack(Path(__file__).resolve().parents[1]/'plugins/usb-relay',self.root/'real-relay.dmp')
                invoke(harness.master,node,'install',{'package':base64.b64encode(relay.read_bytes()).decode()})
                self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':True})
                from remote_plugins import frontend
                host=PluginManager(self.root/'real-host');self.addCleanup(host.close)
                page=frontend(host,harness.master,node,'usb-relay')
                self.assertIn('<!doctype html>',page['html'])
                self.assertEqual(host.store.list(),[])
                self.assertEqual(frontend(host,harness.master,node,'usb-relay')['sha256'],page['sha256'])
                self.manager.dispatch('allow-remote',{'id':'usb-relay','allowed':False})
                with self.assertRaises(RuntimeError):frontend(host,harness.master,node,'usb-relay')
            finally:harness.stop()

def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RemotePluginTests)).wasSuccessful()
if __name__=='__main__':
    import sys
    sys.exit(0 if run() else 1)
