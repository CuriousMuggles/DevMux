"""米家本机适配：凭据仅由当前 Windows 用户解密，不接入跨机扩展。"""
import base64
import ctypes
import io
import json
import os
import secrets
import threading
import time
from pathlib import Path
from ctypes import wintypes


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, protect):
    if os.name != 'nt':
        raise RuntimeError('米家凭据保护仅支持 Windows')
    source = ctypes.create_string_buffer(data)
    inp = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    out = _Blob()
    crypt = ctypes.windll.crypt32
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    if protect:
        ok = crypt.CryptProtectData(ctypes.byref(inp), None, None, None, None, 1, ctypes.byref(out))
    else:
        ok = crypt.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 1, ctypes.byref(out))
    if not ok:
        raise RuntimeError('Windows 凭据保护失败')
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        ctypes.windll.kernel32.LocalFree(out.data)


class MijiaService:
    def __init__(self, root=None):
        self.root = Path(root or (Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ADBManager' / 'mijia'))
        self.path = self.root / 'credentials.dat'
        self.lock = threading.RLock()
        self.generation = 0
        self.owner = None
        self.qr = None
        self.state = 'idle'
        self.api = None
        self.devices = {}
        self.spec_cache = {}
        self.catalog_at = 0

    def _verify_local_path(self):
        if os.name != 'nt':
            raise RuntimeError('米家凭据保护仅支持 Windows')
        resolved = os.path.realpath(str(self.root))
        drive, _ = os.path.splitdrive(resolved)
        if not drive or drive.startswith('\\') or ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') not in (2, 3):
            raise RuntimeError('米家凭据目录必须位于本机磁盘')

    def _api(self, fresh=False):
        self._verify_local_path()
        if not fresh and self.api is not None:
            return self.api
        # 该库在调试级别输出认证数据，运行时关闭其日志。
        import logging
        import sys
        # windowed EXE 的 stdout 可能为空，而依赖导入时会调用 isatty。
        original = sys.stdout
        if original is None:
            sys.stdout = io.StringIO()
        try:
            from mijiaAPI import mijiaAPI
        finally:
            sys.stdout = original
        logging.getLogger('mijiaAPI').disabled = True
        # 上游设备规格查询未设置超时，仅替换其模块内的请求入口。
        import requests
        import mijiaAPI.devices as device_module
        class BoundedSpecRequests:
            @staticmethod
            def get(url, **kwargs):
                kwargs.setdefault('timeout', 20)
                return requests.get(url, **kwargs)
        device_module.requests = BoundedSpecRequests
        service = self
        class ProtectedAPI(mijiaAPI):
            def _init_session(self):
                super()._init_session()
                request = self.session.request
                def bounded(method, url, **kwargs):
                    kwargs.setdefault('timeout', 20)
                    return request(method, url, **kwargs)
                self.session.request = bounded

            def _get_location(self):
                import requests
                from urllib import parse
                headers = {'User-Agent': self.user_agent, 'Connection': 'keep-alive',
                    'Accept-Encoding': 'gzip', 'Content-Type': 'application/x-www-form-urlencoded',
                    'Cookie': f"deviceId={self.deviceId};pass_o={self.pass_o};passToken={self.auth_data.get('passToken', '')};userId={self.auth_data.get('userId', '')};cUserId={self.auth_data.get('cUserId', '')};uLocale={self.locale};"}
                data = self._handle_ret(requests.get(self.service_login_url, headers=headers, timeout=20), verify_code=False)
                location = data['location']
                if data['code'] == 0:
                    response = self.session.get(location, timeout=20)
                    if response.status_code == 200 and response.text == 'ok':
                        self.auth_data.update(self.session.cookies.get_dict())
                        self.auth_data['ssecurity'] = data['ssecurity']
                        return {'code':0, 'message':'刷新Token成功'}
                values = parse.parse_qs(parse.urlparse(location).query)
                return {k:v[0] for k,v in values.items()}

            def _save_auth_data(self):
                payload = json.dumps(self.auth_data, ensure_ascii=False).encode('utf-8')
                encrypted = _dpapi(payload, True)
                service._verify_local_path()
                service.root.mkdir(parents=True, exist_ok=True)
                service._verify_local_path()
                tmp = service.root / ('credentials-' + secrets.token_hex(8) + '.tmp')
                try:
                    tmp.write_bytes(encrypted)
                    os.replace(tmp, service.path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        # 不读取库默认的明文 auth.json。
        api = ProtectedAPI(str(self.root / ('unused-' + secrets.token_hex(16))))
        if self.path.exists():
            api.auth_data = json.loads(_dpapi(self.path.read_bytes(), False))
            api._init_session()
        if not fresh:
            self.api = api
        return api

    def start(self, owner):
        with self.lock:
            if self.state == 'waiting': raise RuntimeError('登录正在进行')
            self.generation += 1
            generation = self.generation
            self.owner, self.qr, self.state = owner, None, 'waiting'
        threading.Thread(target=self._login, args=(generation,), daemon=True, name='MijiaLogin').start()
        return {'state': 'waiting'}

    def _login(self, generation):
        try:
            import requests
            import qrcode
            import qrcode.image.svg
            from urllib import parse
            from datetime import datetime, timedelta
            api = self._api(fresh=True)
            location = api._get_location()
            if location.get('code') == 0:
                with self.lock:
                    if generation != self.generation: return
                    api._save_auth_data(); api._init_session(); self.api = api; self.state = 'success'
                return
            location.update({'theme':'', 'bizDeviceType':'', '_hasLogo':'false', '_qrsize':'240', '_dc':str(int(time.time()*1000))})
            headers = {'User-Agent':api.user_agent, 'Accept-Encoding':'gzip', 'Content-Type':'application/x-www-form-urlencoded', 'Connection':'keep-alive'}
            response = requests.get(api.login_url + '?' + parse.urlencode(location), headers=headers, timeout=15)
            data = api._handle_ret(response)
            qr = qrcode.make(data['loginUrl'], image_factory=qrcode.image.svg.SvgPathImage)
            output = io.BytesIO(); qr.save(output)
            with self.lock:
                if generation != self.generation: return
                self.qr = base64.b64encode(output.getvalue()).decode('ascii')
            session = requests.Session()
            reply = api._handle_ret(session.get(data['lp'], headers=headers, timeout=120))
            with self.lock:
                if generation != self.generation: return
            session.get(reply['location'], headers=headers, timeout=15)
            with self.lock:
                if generation != self.generation: return
                for key in ('psecurity','nonce','ssecurity','passToken','userId','cUserId'):
                    api.auth_data[key] = reply[key]
                api.auth_data.update(session.cookies.get_dict())
                api.auth_data['expireTime'] = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
                api._save_auth_data(); api._init_session()
                self.api = api; self.qr = None; self.state = 'success'
        except Exception:
            with self.lock:
                if generation == self.generation:
                    self.qr = None; self.state = 'error'

    def cancel(self, owner):
        with self.lock:
            if self.owner is owner:
                self.generation += 1; self.qr = None; self.owner = None; self.state = 'idle'

    def _switch_spec(self, api, item):
        import re
        if item.get('_shared') or not item.get('isOnline'):
            raise PermissionError('设备不可操作')
        model = str(item.get('model',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}', model):
            raise ValueError('设备型号无效')
        if model.split('.')[1] not in ('light','switch','plug','socket'):
            raise PermissionError('此设备类型不支持常用开关')
        cached = self.spec_cache.get(model)
        if cached and time.monotonic() - cached[0] < 3600:
            return dict(cached[1])
        from mijiaAPI.devices import get_device_info
        info = get_device_info(model, cache_path=api.auth_data_path.parent)
        prop = next((p for p in info.get('properties', []) if p.get('name') == 'on'), None)
        if not prop or prop.get('type') != 'bool' or 'r' not in prop.get('rw','') or 'w' not in prop.get('rw',''):
            raise ValueError('设备未提供可读写的电源开关')
        method = prop.get('method') or {}
        if type(method.get('siid')) is not int or type(method.get('piid')) is not int or method['siid'] <= 0 or method['piid'] <= 0:
            raise ValueError('设备开关规格无效')
        spec = {'siid':method['siid'], 'piid':method['piid']}
        self.spec_cache[model] = (time.monotonic(), spec)
        return dict(spec)

    def call(self, owner, action, args):
        if action == 'login': return self.start(owner)
        if action == 'cancel': self.cancel(owner); return {'state':'idle'}
        if action == 'login-status':
            with self.lock:
                if self.owner is not owner: return {'state':'idle'}
                return {'state':self.state, 'qr':self.qr}
        if action == 'logout':
            with self.lock:
                self.generation += 1; self.owner = None; self.qr = None; self.state = 'idle'
                self.api = None; self.devices = {}; self.spec_cache = {}; self.catalog_at = 0
                try: self.path.unlink()
                except FileNotFoundError: pass
            return {'state':'idle'}
        if action == 'status': return {'loggedIn':self.path.exists()}
        if action == 'snapshot':
            with self.lock:
                api = self._api()
                if not api.available: raise RuntimeError('登录已失效，请重新扫码')
                if args.get('refreshCatalog') is True or not self.catalog_at or time.monotonic() - self.catalog_at >= 60:
                    items = api.get_devices_list()
                    self.devices = {str(d['did']):dict(d, _shared=False) for d in items if d.get('did')}
                    self.catalog_at = time.monotonic()
                queries, entries, unavailable = [], {}, 0
                for did, item in self.devices.items():
                    if not item.get('isOnline'): continue
                    try:
                        spec = self._switch_spec(api, item)
                    except (PermissionError, ValueError):
                        continue
                    except Exception:
                        unavailable += 1
                        continue
                    query = {'did':did, **spec}
                    queries.append(query); entries[did] = (item, spec)
                values = {}
                for start in range(0, len(queries), 50):
                    batch = queries[start:start+50]
                    response = api.get_devices_prop(batch)
                    if not isinstance(response, list): raise RuntimeError('米家状态响应无效')
                    for result in response:
                        if not isinstance(result, dict): continue
                        did = str(result.get('did', ''))
                        entry = entries.get(did)
                        if not entry or result.get('siid') != entry[1]['siid'] or result.get('piid') != entry[1]['piid']:
                            continue
                        if result.get('code') == 0 and type(result.get('value')) is bool:
                            values[did] = result['value']
                unavailable += len(queries) - len(values)
                return {'devices':[{'did':did,'name':str(item.get('name','设备')),
                    'model':str(item.get('model','')),'room':str(item.get('roomName') or '未指定房间'),
                    'online':True,'on':values[did]} for did, (item, _) in entries.items() if did in values],
                    'unavailable':unavailable}
        if action in ('read-switch','set-switch'):
            with self.lock:
                item = self.devices.get(str(args.get('did','')))
                if not item or not item.get('isOnline'): raise ValueError('设备不存在或已离线，请刷新')
                api = self._api()
                spec = self._switch_spec(api, item)
                query = {'did':str(item['did']), **spec}
                if action == 'set-switch':
                    if type(args.get('value')) is not bool: raise ValueError('开关值无效')
                    response = api.set_devices_prop({**query, 'value':args['value']})
                    if not isinstance(response, dict) or response.get('code') not in (0, 1):
                        raise RuntimeError('设备操作结果未确认')
                response = api.get_devices_prop(query)
                if not isinstance(response, dict) or response.get('code') != 0 or type(response.get('value')) is not bool:
                    raise ValueError('设备状态无效')
                return {'on':response['value']}
        raise ValueError('米家操作无效')
