import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mijia_service import MijiaService, _dpapi


class MijiaSecurityTests(unittest.TestCase):
    def test_dpapi_roundtrip_and_ciphertext(self):
        plain = b'mijia-test-sensitive-token'
        encrypted = _dpapi(plain, True)
        self.assertNotIn(plain, encrypted)
        self.assertEqual(_dpapi(encrypted, False), plain)

    def test_specification_request_has_timeout(self):
        import requests
        import mijiaAPI.devices as device_module
        with tempfile.TemporaryDirectory() as root:
            MijiaService(root)._api()
            with mock.patch.object(requests, 'get') as get:
                device_module.requests.get('https://example.invalid/spec')
                self.assertEqual(get.call_args.kwargs['timeout'], 20)

    def test_network_credentials_directory_rejected(self):
        service = MijiaService(r'\\server\share\mijia')
        with self.assertRaises(RuntimeError): service._verify_local_path()

    def test_save_and_reload_without_plaintext(self):
        with tempfile.TemporaryDirectory() as root:
            service = MijiaService(root)
            api = service._api()
            api.auth_data = {'serviceToken': 'sensitive-test-value', 'cUserId':'c', 'ssecurity':'x', 'ua':'a', 'userId':'u'}
            api._save_auth_data()
            self.assertNotIn(b'sensitive-test-value', service.path.read_bytes())
            self.assertFalse((Path(root) / 'auth.json').exists())
            restored = MijiaService(root)._api()
            self.assertEqual(restored.auth_data['serviceToken'], 'sensitive-test-value')
            service.call(object(), 'logout', {})
            self.assertFalse(service.path.exists())

    def test_owner_and_cancellation_reject_old_generation(self):
        service = MijiaService()
        owner, other = object(), object()
        service.owner = owner; service.state = 'waiting'; service.qr = 'secret-qr'
        self.assertEqual(service.call(other, 'login-status', {}), {'state':'idle'})
        service.cancel(other)
        self.assertEqual(service.qr, 'secret-qr')
        service.cancel(owner)
        self.assertIsNone(service.qr)
        self.assertEqual(service.generation, 1)

    def test_qr_login_flow_saves_only_after_confirmation(self):
        service = MijiaService()
        api = mock.Mock()
        api._get_location.return_value = {'sid':'mijia'}
        api.login_url = 'https://example.invalid/login'
        api.user_agent = 'test'
        api.auth_data = {}
        api._handle_ret.side_effect = [
            {'loginUrl':'https://example.invalid/scan','lp':'https://example.invalid/poll'},
            {'location':'https://example.invalid/callback', **{k:k for k in ('psecurity','nonce','ssecurity','passToken','userId','cUserId')}}]
        session = mock.Mock()
        session.cookies.get_dict.return_value = {'serviceToken':'secret'}
        service.generation = 1
        with mock.patch.object(service, '_api', return_value=api), mock.patch('requests.get'), mock.patch('requests.Session', return_value=session):
            service._login(1)
        self.assertEqual(service.state, 'success')
        self.assertIsNone(service.qr)
        api._save_auth_data.assert_called_once()
        self.assertEqual(api.auth_data['serviceToken'], 'secret')

    def test_switch_checks_capability_and_reads_back(self):
        service = MijiaService()
        service.devices = {'1': {'did':'1','model':'vendor.light.example','isOnline':True}}
        api=mock.Mock(); api.auth_data_path=Path('spec-cache/auth')
        api.set_devices_prop.return_value={'code':0}
        api.get_devices_prop.return_value={'code':0,'value':True}
        info={'properties':[{'name':'on','type':'bool','rw':'rw','method':{'siid':2,'piid':1}}]}
        with mock.patch.object(service, '_api', return_value=api), mock.patch('mijiaAPI.devices.get_device_info',return_value=info):
            result=service.call(object(),'set-switch',{'did':'1','value':True})
            self.assertEqual(result,{'on':True})
            api.set_devices_prop.assert_called_once_with({'did':'1','siid':2,'piid':1,'value':True})
            with self.assertRaises(ValueError): service.call(object(),'set-switch',{'did':'1','value':'true'})
            service.spec_cache.clear(); info['properties'][0]['rw']='r'
            with self.assertRaises(ValueError): service.call(object(),'set-switch',{'did':'1','value':False})

    def test_snapshot_batches_and_caches_catalog_and_specs(self):
        service=MijiaService(); api=mock.Mock(); api.available=True; api.auth_data_path=Path('spec-cache/auth')
        api.get_devices_list.return_value=[{'did':'1','name':'灯1','model':'vendor.light.example','isOnline':True},
            {'did':'2','name':'灯2','model':'vendor.light.example','isOnline':True},
            {'did':'3','name':'锁','model':'vendor.lock.example','isOnline':True},
            {'did':'4','name':'离线','model':'vendor.light.example','isOnline':False}]
        api.get_devices_prop.return_value=[{'did':'1','siid':2,'piid':1,'code':0,'value':True},
            {'did':'2','siid':2,'piid':1,'code':0,'value':False}]
        info={'properties':[{'name':'on','type':'bool','rw':'rw','method':{'siid':2,'piid':1}}]}
        with mock.patch.object(service,'_api',return_value=api),mock.patch('mijiaAPI.devices.get_device_info',return_value=info) as specs:
            first=service.call(object(),'snapshot',{})
            second=service.call(object(),'snapshot',{})
            self.assertEqual([(d['did'],d['on']) for d in first['devices']],[('1',True),('2',False)])
            self.assertEqual(second,first);self.assertEqual(api.get_devices_list.call_count,1)
            self.assertEqual(specs.call_count,1);self.assertEqual(api.get_devices_prop.call_count,2)
            self.assertEqual(len(api.get_devices_prop.call_args.args[0]),2)
            service.call(object(),'snapshot',{'refreshCatalog':True})
            self.assertEqual(api.get_devices_list.call_count,2)
            api.get_devices_prop.return_value=[{'did':'1','siid':2,'piid':99,'code':0,'value':True}]
            failed=service.call(object(),'snapshot',{})
            self.assertEqual(failed['devices'],[]);self.assertEqual(failed['unavailable'],2)

    def test_high_risk_and_arbitrary_device_denied(self):
        service = MijiaService()
        service.devices = {'1': {'did':'1','model':'vendor.lock.example','isOnline':True}}
        with self.assertRaises(PermissionError):
            service.call(object(), 'set-switch', {'did':'1','value':True})
        with self.assertRaises(ValueError):
            service.call(object(), 'set-switch', {'did':'missing','value':True})



class MijiaWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_and_page_token_required(self):
        import server
        from types import SimpleNamespace
        host = server.ManagerServer.__new__(server.ManagerServer)
        host.mijia = mock.Mock()
        async def execute(fn, *args): return fn(*args)
        sent = []
        async def send(ws, data): sent.append(data)
        host._run_blocking = execute
        host._send = send
        ws = SimpleNamespace(request=SimpleNamespace(headers={'Origin':'http://attacker.example'}))
        await host._handle_message(ws, {'type':'mijia','id':1,'action':'status','pageToken':server._MIJIA_PAGE_TOKEN})
        self.assertFalse(sent[-1]['success'])
        ws.request.headers['Origin'] = f'http://127.0.0.1:{server._HTTP_PORT}'
        await host._handle_message(ws, {'type':'mijia','id':2,'action':'status','pageToken':'wrong'})
        self.assertFalse(sent[-1]['success'])
        host.mijia.call.assert_not_called()
        host.mijia.call.return_value = {'loggedIn':False}
        await host._handle_message(ws, {'type':'mijia','id':3,'action':'status','pageToken':server._MIJIA_PAGE_TOKEN})
        self.assertTrue(sent[-1]['success'])
        self.assertEqual(sent[-1]['data'], {'loggedIn':False})

def run():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    return result.wasSuccessful()


if __name__ == '__main__': unittest.main()
