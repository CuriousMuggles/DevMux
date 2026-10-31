"""Authenticated plugin discovery and bounded remote package transfer."""
import base64
import hashlib
import json
from pathlib import Path
import re
from extension_tools import remote_call

class RemotePlugins:
    def __init__(self, manager):
        self.manager=manager
        self.root=manager.store.root / '.remote'
        self.settings=manager.store.root / '.remote-policy.json'
        try:self.allowed=json.loads(self.settings.read_text(encoding='utf-8')).get('allowed') is True
        except (OSError,ValueError,AttributeError):self.allowed=False

    def permission(self, allowed):
        if type(allowed) is not bool:raise ValueError('权限值必须为布尔值')
        self.settings.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.settings.with_suffix('.tmp')
        temporary.write_text(json.dumps({'allowed':allowed}),encoding='utf-8');temporary.replace(self.settings)
        self.allowed=allowed
        return {'allowed':allowed}

    def call(self, action, args, owner):
        with self.manager.lock:
            if action=='list':
                return {'plugins':self.manager.remote_catalog(),'installAllowed':self.allowed}
            if action in ('frontend-info','frontend-chunk'):
                identifier=args.get('id')
                item=next((p for p in self.manager.remote_catalog() if p['id']==identifier),None)
                if not item or not item['remoteCapable'] or not item['enabled'] or not item['remoteAllowed']:
                    raise PermissionError('请在从机开启插件的「允许主控使用」')
                raw=self.manager.dispatch('page',{'id':identifier})['html'].encode('utf-8')
                if not 0<len(raw)<=4*1024*1024:raise ValueError('插件界面过大')
                digest=hashlib.sha256(raw).hexdigest()
                if action=='frontend-info':return {**item,'sha256':digest,'size':len(raw)}
                offset=args.get('offset')
                if args.get('sha256')!=digest:raise ValueError('插件界面已变化，请重新打开')
                if type(offset) is not int or not 0<=offset<len(raw):raise ValueError('界面数据偏移无效')
                return {'data':base64.b64encode(raw[offset:offset+32768]).decode()}
            if not self.allowed:raise PermissionError('从机未允许主控安装插件，请在从机插件管理中开启')
            if not isinstance(args,dict):raise ValueError('安装参数无效')
            digest=args.get('sha256','')
            if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('插件摘要无效')
            folder=self.root / hashlib.sha256(owner.encode()).hexdigest()[:32]
            folder.mkdir(parents=True,exist_ok=True)
            package=folder/(digest+'.part');receipt=folder/(digest+'.json')
            if receipt.exists():
                saved=json.loads(receipt.read_text(encoding='utf-8'))
                marker=self.manager.store.root / saved['id'] / '.remote-package.sha256'
                if not marker.is_file() or marker.read_text(encoding='utf-8')!=digest:receipt.unlink()
            if action=='begin':
                size=args.get('size')
                if type(size) is not int or not 0<size<=4*1024*1024:raise ValueError('插件包大小无效')
                if receipt.exists():return {'installed':True,'result':json.loads(receipt.read_text(encoding='utf-8'))}
                if package.exists() and package.stat().st_size>size:package.unlink()
                return {'offset':package.stat().st_size if package.exists() else 0}
            if action=='chunk':
                data=base64.b64decode(args.get('data',''),validate=True);offset=args.get('offset')
                if type(offset) is not int or offset<0 or not data or len(data)>32768:raise ValueError('插件数据块无效')
                current=package.stat().st_size if package.exists() else 0
                if offset+len(data)>4*1024*1024:raise ValueError('插件包过大')
                if offset<current:
                    with package.open('rb') as stream:stream.seek(offset);existing=stream.read(len(data))
                    if existing!=data:raise ValueError('重传数据不一致')
                    return {'offset':current}
                if offset!=current:raise ValueError('数据块不连续')
                with package.open('ab') as stream:stream.write(data)
                return {'offset':offset+len(data)}
            if action=='finish':
                if receipt.exists():return json.loads(receipt.read_text(encoding='utf-8'))
                if not package.is_file() or hashlib.sha256(package.read_bytes()).hexdigest()!=digest:raise ValueError('插件包校验失败')
                result=self.manager.dispatch('install',{'package':base64.b64encode(package.read_bytes()).decode()})
                (self.manager.store.root / result['id'] / '.remote-package.sha256').write_text(digest,encoding='utf-8')
                receipt.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');package.unlink()
                return result
            raise ValueError('未知插件传输动作')

def invoke(controller,node_id,action,args):
    def call(operation,arguments):
        result=remote_call(controller,node_id,'__plugin_catalog__',operation,arguments,caller='gui')
        if not result.get('success'):raise RuntimeError(result.get('output','远程插件操作失败'))
        return result['data']
    if action=='list':return call('list',{})
    if action!='install':raise PermissionError('仅支持查询或安装远程插件')
    package=base64.b64decode(args.get('package',''),validate=True)
    if not 0<len(package)<=4*1024*1024:raise ValueError('插件包大小无效')
    digest=hashlib.sha256(package).hexdigest();state=call('begin',{'sha256':digest,'size':len(package)})
    if state.get('installed'):return state['result']
    offset=state['offset']
    while offset<len(package):
        offset=call('chunk',{'sha256':digest,'offset':offset,'data':base64.b64encode(package[offset:offset+32768]).decode()})['offset']
    return call('finish',{'sha256':digest})


def frontend(manager, controller, node_id, identifier):
    """Fetch only a sandboxed self-contained page. Never install or run remote code locally."""
    def call(action,args):
        result=remote_call(controller,node_id,'__plugin_catalog__',action,args,caller='gui')
        if not result.get('success'):raise RuntimeError(result.get('output','远程插件界面加载失败'))
        return result['data']
    info=call('frontend-info',{'id':identifier})
    if info.get('id')!=identifier or info.get('apiVersion')!=1:raise ValueError('不支持的插件界面协议')
    digest=info.get('sha256','');size=info.get('size')
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or type(size) is not int or not 0<size<=4*1024*1024:
        raise ValueError('插件界面元数据无效')
    folder=manager.store.root/'.frontend-cache'/hashlib.sha256((node_id+'\0'+identifier).encode()).hexdigest()
    cache=folder/(digest+'.html')
    raw=cache.read_bytes() if cache.is_file() else b''
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest:
        chunks=[];offset=0
        while offset<size:
            chunk=base64.b64decode(call('frontend-chunk',{'id':identifier,'sha256':digest,'offset':offset})['data'],validate=True)
            if not chunk or len(chunk)>32768 or offset+len(chunk)>size:raise ValueError('插件界面数据无效')
            chunks.append(chunk);offset+=len(chunk)
        raw=b''.join(chunks)
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('插件界面校验失败')
        folder.mkdir(parents=True,exist_ok=True)
        import tempfile,os
        with tempfile.NamedTemporaryFile(dir=folder,delete=False) as stream:
            stream.write(raw);temporary=stream.name
        os.replace(temporary,cache)
        for old in folder.glob('*.html'):
            if old!=cache:old.unlink(missing_ok=True)
    return {**info,'html':raw.decode('utf-8')}
