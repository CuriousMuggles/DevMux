"""验证 HTTP 服务器启动和基本功能。
WebSocket server 需要完整 asyncio 事件循环，端到端验证交由 pywinpty 测试覆盖。"""
import sys
import os
import socket

def _find_free_port(start=9000, max_attempts=20):
    for port in range(start, start + max_attempts):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('no free port found')

def run() -> bool:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)

    import server as srv
    old_http = srv.HTTP_PORT
    srv.HTTP_PORT = _find_free_port()

    ok = True

    # --- HTTP server starts ---
    try:
        httpd = srv.start_http_server()
        print(f'  [PASS] HTTP server on :{srv.HTTP_PORT}')
    except Exception as e:
        print(f'  [FAIL] HTTP server start: {e}')
        srv.HTTP_PORT = old_http
        return False

    # --- HTTP serves index.html ---
    import urllib.request
    try:
        resp = urllib.request.urlopen(f'http://127.0.0.1:{srv.HTTP_PORT}/', timeout=5)
        html = resp.read().decode('utf-8')
        if 'DevMux' in html:
            print(f'  [PASS] HTTP serves index.html ({len(html)} bytes)')
        else:
            print(f'  [FAIL] index.html missing expected content')
            ok = False
    except Exception as e:
        print(f'  [FAIL] HTTP request: {e}')
        ok = False

    httpd.shutdown()
    srv.HTTP_PORT = old_http
    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
