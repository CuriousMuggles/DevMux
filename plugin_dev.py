"""DevMux plugin authoring CLI: create, check, pack and isolated live-source serve."""
import argparse
import ast
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from plugin_assets import build_frontend, local_path
from plugin_store import PluginStore, ID
from plugin_pack import pack
from plugin_process import PluginProcess
from plugin_manager import PluginService
from extension_tools import ExtensionTools


def create(path, identifier, name=None):
    if not isinstance(identifier,str) or not ID.fullmatch(identifier):
        raise ValueError('插件 ID 须以小写字母开头，只包含小写字母、数字和连字符，最长64字符')
    path=Path(path)
    if path.exists():raise ValueError('目标目录已存在，不会覆盖: '+str(path))
    manifest={'id':identifier,'name':name or identifier,'version':'1.0.0','apiVersion':1,
              'backend':'main.py','frontend':'index.html','permissions':[]}
    path.mkdir(parents=True)
    (path/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (path/'main.py').write_text('''from plugin_sdk import Plugin


class Tool(Plugin):
    def call(self, action, arguments):
        if action == 'echo':
            self.logger.info('Echo invoked')
            return {'text': arguments.get('text', '')}
        raise ValueError('Unknown action: ' + str(action))


def create_plugin(root):
    return Tool(root)
''',encoding='utf-8')
    (path/'index.html').write_text('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="devmux:ui.css"></head><body>
<script src="devmux:sdk.js"></script><script src="app.js"></script></body></html>
''',encoding='utf-8')
    (path/'app.js').write_text('''const {ui,call}=DevMuxPlugin;
const page=ui.mount({title:TITLE,description:'使用公共组件开发插件'});
const form=ui.form([{name:'text',label:'内容',type:'text',default:'Hello DevMux',required:true}]);
const output=ui.log({limit:100});
page.body.append(form,ui.button('调用后端',async()=>{
  if(!form.reportValidity())return;
  const result=await call('echo',form.values());
  output.appendLine(result.text);
},{variant:'primary'}),output);
'''.replace('TITLE',json.dumps(name or identifier,ensure_ascii=False)),encoding='utf-8')
    (path/'README.md').write_text('# '+(name or identifier)+'\n\n从 DevMux 仓库运行：\n\n```powershell\npython plugin_dev.py check "'+str(path)+'"\npython plugin_dev.py serve "'+str(path)+'"\npython plugin_dev.py pack "'+str(path)+'" dist/'+identifier+'.dmp\n```\n\n宿主API、资源、权限及生命周期参见 docs/plugin-development.md。\n',encoding='utf-8')
    (path/'AGENTS.md').write_text('''# 插件开发约定

- 后端通过 create_plugin(root) 和 call(action, arguments) 提供业务接口，可继承 plugin_sdk.Plugin。
- 配置、日志及凭据写入 self.data_dir；不要写入插件代码目录。
- 前端使用 DevMuxPlugin SDK 与公共 UI 组件，不直接调用宿主 WebSocket，不再创建浮动窗口外壳。
- call/hostCall 抛出 PluginError；超时不代表业务未执行，不自动重试硬件写操作。
- 宿主文件选择权限须在 manifest.permissions 声明；远程操作还需实现执行端授权及任务所有者校验。
- MCP/Agent公开动作写入localActions（仅本机）或actions（支持经授权远程）；启用注册，停用撤销，参数由后端校验。
- 监听、定时器及资源用 onDispose/on_dispose 注册清理；存在活动任务时实现 can_stop()。
- 页面资源使用插件内相对路径或 devmux:sdk.js、devmux:ui.css；打包自动内联，不使用外部CDN。
- 修改后 check，再在隔离 serve 环境验证，最后 pack；测试随开发分支获准后一起合入主线。
''',encoding='utf-8')
    return path


def check(source):
    source=Path(source).resolve()
    try:manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8-sig'))
    except (OSError,ValueError) as error:raise ValueError('无法读取 manifest.json: '+str(error)) from error
    PluginStore.validate(manifest)
    for path in source.rglob('*'):
        if path.is_symlink():raise ValueError('插件源不能包含符号链接: '+str(path))
    backend=local_path(source,manifest['backend'])
    try:tree=ast.parse(backend.read_text(encoding='utf-8-sig'),filename=str(backend))
    except SyntaxError as error:raise ValueError('后端语法错误: '+str(error)) from error
    if not any(isinstance(node,ast.FunctionDef) and node.name=='create_plugin' for node in tree.body):
        raise ValueError('后端须导出 create_plugin(root) 工厂函数')
    if manifest.get('frontend'):build_frontend(source,manifest['frontend'])
    return manifest


class DevServer(ThreadingHTTPServer):
    daemon_threads=True

    def __init__(self,source,port=0):
        self.source=Path(source).resolve();self.manifest=check(self.source)
        self.token=secrets.token_urlsafe(32);self.guard=threading.RLock()
        self.temporary=tempfile.TemporaryDirectory(prefix='devmux-plugin-dev-')
        self.root=Path(self.temporary.name);self.process=None
        self.registry=ExtensionTools({})
        try:
            self.reload()
            super().__init__(('127.0.0.1',port),DevHandler)
        except BaseException:
            if self.process:self.process.close()
            self.temporary.cleanup();raise

    @property
    def url(self):return 'http://127.0.0.1:'+str(self.server_port)+'/?token='+self.token

    def reload(self):
        with self.guard:
            manifest=check(self.source)
            package=pack(self.source,self.root/'development.dmp')
            if self.process:
                if self.process.process.poll() is None and not self.process.call('__can_stop__'):
                    raise ValueError('插件仍有活动任务，请先结束任务再重新加载')
                self.process.close();self.process=None
            # Rebuild to an isolated directory; even plugin writes cannot overwrite source data.
            PluginStore(self.root/'installed').install(package)
            process=PluginProcess(self.root/'installed'/manifest['id'])
            try:process.call('__ready__',timeout=10)
            except BaseException:process.close();raise
            self.process=process;self.manifest=manifest
            self.registry.services.clear()
            if manifest.get('actions') or manifest.get('localActions'):
                self.registry.services[manifest.get('tool',manifest['id'])]=PluginService(process,manifest,self.guard)

    def server_close(self):
        super().server_close()
        with self.guard:
            if self.process:self.process.close();self.process=None
        self.temporary.cleanup()


class DevHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,value,status=200,kind='application/json; charset=utf-8'):
        raw=value.encode('utf-8') if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)

    def authorized(self):
        token=parse_qs(urlsplit(self.path).query).get('token',[''])[0]
        token=self.headers.get('Authorization','').removeprefix('Bearer ') or token
        if not secrets.compare_digest(token,self.server.token):
            self.reply({'success':False,'output':'开发服务令牌无效'},403);return False
        return True

    def do_GET(self):
        if not self.authorized():return
        try:
            path=urlsplit(self.path).path
            if path=='/page':return self.reply(build_frontend(self.server.source,self.server.manifest['frontend']),kind='text/html; charset=utf-8')
            if path=='/logs':
                with self.server.guard:
                    log=self.server.root/'installed'/'.data'/self.server.manifest['id']/'runtime.log'
                    text=''
                    if log.is_file():
                        with log.open('rb') as stream:
                            stream.seek(max(0,log.stat().st_size-65536));text=stream.read().decode('utf-8',errors='replace')
                return self.reply(text,kind='text/plain; charset=utf-8')
            if path!='/':return self.reply({'success':False,'output':'未知开发接口'},404)
            token=json.dumps(self.server.token)
            html='''<!doctype html><meta charset="utf-8"><title>DevMux 插件开发</title>
<style>body{margin:0;background:#1e1e2e;color:#cdd6f4;font:13px sans-serif}header{padding:10px;display:flex;gap:12px;align-items:center}iframe{width:100%;height:calc(100vh - 48px);border:0}button{padding:6px 12px}span{white-space:pre-wrap}</style>
<header><strong>插件开发</strong><button id="page">刷新界面</button><button id="reload">重新加载后端</button><button id="logs">查看日志</button><span id="status">数据与运行环境已隔离</span></header><iframe sandbox="allow-scripts" title="插件预览"></iframe>
<script>const token=TOKEN,frame=document.querySelector('iframe'),status=document.querySelector('#status');
async function load(){const r=await fetch('/page?token='+encodeURIComponent(token));if(!r.ok)throw Error(await r.text());frame.srcdoc=await r.text();}
async function post(path,value){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(value)});return r.json();}
window.addEventListener('message',async e=>{if(e.source!==frame.contentWindow)return;const q=e.data;if(q?.type==='devmux-plugin-close'){status.textContent='插件界面已关闭，刷新界面可重新打开';return;}if(!['devmux-plugin-call','devmux-host-call'].includes(q?.type))return;try{const r=await post('/rpc',q);frame.contentWindow.postMessage({type:'devmux-plugin-result',id:q.id,result:r.result??r},'*');}catch(error){frame.contentWindow.postMessage({type:'devmux-plugin-result',id:q.id,error:error.message},'*');}});
document.querySelector('#page').onclick=()=>load().catch(e=>status.textContent=e.message);
document.querySelector('#reload').onclick=async()=>{const r=await post('/reload',{});status.textContent=r.success?'后端已重新加载':r.output;if(r.success)load();};
document.querySelector('#logs').onclick=async()=>{status.textContent=await(await fetch('/logs?token='+encodeURIComponent(token))).text()||'暂无日志';};
load().catch(e=>status.textContent=e.message);</script>'''.replace('TOKEN',token)
            self.reply(html,kind='text/html; charset=utf-8')
        except Exception as error:self.reply({'success':False,'output':str(error)},400)

    def do_POST(self):
        if not self.authorized():return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=262144:raise ValueError('开发请求大小无效')
            request=json.loads(self.rfile.read(size))
            with self.server.guard:
                path=urlsplit(self.path).path
                if path=='/reload':self.server.reload();return self.reply({'success':True})
                if path!='/rpc':return self.reply({'success':False,'output':'未知开发接口'},404)
                if request.get('type')=='devmux-host-call' and request.get('hostType')=='extension-tool':
                    payload=request.get('hostPayload',{})
                    tool=self.server.manifest.get('tool',self.server.manifest['id'])
                    if payload.get('tool')!=tool:raise PermissionError('只能调用当前插件工具')
                    if payload.get('action')=='targets':
                        return self.reply({'success':True,'data':{'targets':[{'target':'computer:local','local':True,'online':True,'label':'插件开发环境','role':'slave'}]}})
                    if payload.get('target')!='computer:local':raise PermissionError('开发预览仅支持隔离的本机目标')
                    return self.reply(self.server.registry.call(tool,payload.get('action'),payload.get('arguments',{}),caller='gui'))
                if request.get('type')!='devmux-plugin-call':
                    return self.reply({'success':False,'output':'开发预览不提供宿主文件选择和远程连接；请在实际应用中验证这些能力'})
                action=request.get('action')
                if not isinstance(action,str) or action.startswith('__'):raise ValueError('只允许调用公开业务动作')
                result=self.server.process.call(action,request.get('arguments',{}),timeout=min(180,self.server.manifest.get('timeout',30)))
                self.reply({'success':True,'result':result})
        except Exception as error:self.reply({'success':False,'output':str(error)},400)


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    new=commands.add_parser('create');new.add_argument('path');new.add_argument('--id',required=True);new.add_argument('--name')
    for name in ('check','pack','serve'):
        command=commands.add_parser(name);command.add_argument('source')
        if name=='pack':command.add_argument('output')
        if name=='serve':command.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    try:
        if args.command=='create':print(create(args.path,args.id,args.name))
        elif args.command=='check':print(json.dumps({'success':True,'manifest':check(args.source)},ensure_ascii=False))
        elif args.command=='pack':check(args.source);print(pack(args.source,args.output))
        else:
            server=DevServer(args.source,args.port)
            print(json.dumps({'url':server.url,'port':server.server_port}),flush=True)
            try:server.serve_forever()
            except KeyboardInterrupt:pass
            finally:server.server_close()
        return 0
    except Exception as error:parser.exit(1,'插件开发错误: '+str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())
