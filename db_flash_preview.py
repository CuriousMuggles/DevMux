"""独立 DB 烧写模拟预览，不导入或启动 server.py、ADB 或中转节点。

运行：python -B db_flash_preview.py
仅允许本机页面连接；原生文件窗口不改变固化包内容。
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import websockets
from db_flash import DBFlashService, SimulationExecutor

ROOT = Path(__file__).resolve().parent
RENDERER = ROOT / 'renderer'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def pick_native(kind):
    # 固定脚本不拼接用户输入；只启动原生选择窗口，不调用任何设备工具。
    multi = '$true' if kind == 'file' else '$false'
    script = """Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = if (MULTI) { New-Object System.Windows.Forms.OpenFileDialog } else { New-Object System.Windows.Forms.FolderBrowserDialog }
try {
  if (MULTI) { $dialog.Multiselect = $true; $dialog.Title = '批量选择待烧写文件' }
  else { $dialog.Description = '选择包含 flashall.bat 的 DB 固化包根目录' }
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    if (MULTI) { @{success=$true;path=$dialog.FileName;paths=@($dialog.FileNames)} | ConvertTo-Json -Compress }
    else { @{success=$true;path=$dialog.SelectedPath} | ConvertTo-Json -Compress }
  } else { @{success=$false;path='';output=''} | ConvertTo-Json -Compress }
} finally { $dialog.Dispose() }
""".replace('MULTI', multi)
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    exe = windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(exe), '-NoProfile', '-STA', '-Command', script],
                            capture_output=True, encoding='utf-8', errors='replace',
                            timeout=120, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        return {'success': False, 'output': result.stderr.strip() or '无法打开原生文件选择窗口'}
    return json.loads(result.stdout.lstrip('\ufeff'))


class PreviewHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, ws_port=8865, **kwargs):
        self.ws_port = ws_port
        super().__init__(*args, directory=str(RENDERER), **kwargs)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            self.path = '/db-flash-preview.html'
        if path == '/db-flash-preview.js':
            body = (RENDERER / 'db-flash-preview.js').read_text(encoding='utf-8-sig')
            body = body.replace('__DB_FLASH_WS_PORT__', str(self.ws_port)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ('/', '/db-flash-preview.html', '/db-flash.js', '/db-flash.css', '/favicon.ico'):
            self.send_error(404)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()


async def serve(http_port=8180, ws_port=8865, stop_event=None, service=None):
    loop = asyncio.get_running_loop()
    clients = set()

    async def broadcast(payload):
        body = json.dumps(payload, ensure_ascii=False)
        for ws in tuple(clients):
            try:
                await ws.send(body)
            except websockets.exceptions.ConnectionClosed:
                clients.discard(ws)

    def progress(payload):
        if not loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

    service = service or DBFlashService(SimulationExecutor())
    service.progress = progress

    async def handler(ws):
        origin = urlsplit(ws.request.headers.get('Origin', ''))
        if origin.scheme != 'http' or origin.hostname not in ('127.0.0.1', 'localhost') or origin.port != http_port:
            await ws.close(code=1008, reason='仅允许本机模拟页面')
            return
        clients.add(ws)
        try:
            async for raw in ws:
                message = {}
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError('请求必须为对象')
                    type = message.get('type')
                    if type == 'db-flash':
                        result = await asyncio.to_thread(service.call, message.get('action'), message.get('arguments'))
                    elif type in ('pick-file', 'pick-directory'):
                        result = await asyncio.to_thread(pick_native, 'file' if type == 'pick-file' else 'directory')
                    else:
                        raise ValueError('预览服务不支持该操作')
                except Exception as exc:
                    result = {'success': False, 'output': str(exc)}
                await ws.send(json.dumps({'type': 'exec-result', 'id': message.get('id') if isinstance(message, dict) else None, **result}, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            clients.discard(ws)

    import functools
    http = None
    try:
        async with websockets.serve(handler, '127.0.0.1', ws_port, max_size=1024 * 1024):
            http = PreviewHTTPServer(('127.0.0.1', http_port),
                                     functools.partial(PreviewHandler, ws_port=ws_port))
            thread = threading.Thread(target=http.serve_forever, name='db-flash-preview-http', daemon=True)
            thread.start()
            print(f'DB 模拟预览：http://127.0.0.1:{http_port}/（不操作真实设备）', flush=True)
            if stop_event is None:
                await asyncio.Event().wait()
            else:
                await stop_event.wait()
    finally:
        await asyncio.to_thread(service.close)
        if http:
            await asyncio.to_thread(http.shutdown)
            http.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DB 烧写独立模拟预览；不启动 ADB 或继电器')
    parser.add_argument('--http-port', type=int, default=8180)
    parser.add_argument('--ws-port', type=int, default=8865)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.http_port, args.ws_port))
    except KeyboardInterrupt:
        pass
