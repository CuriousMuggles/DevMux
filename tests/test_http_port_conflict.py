"""HTTP 静态服务的端口必须独占：两个应用实例不能共用一个端口。

为什么必须有这条回归：Windows 的 ``SO_REUSEADDR`` 语义允许**两个进程同时 LISTEN
同一个** ``127.0.0.1:8080``，而 stdlib 的 ``ThreadingHTTPServer`` 默认
``allow_reuse_address = True``。后果不是「端口占用报错」而是「静默共用」：第二个实例
的窗口有可能被第一个实例的 HTTP 服务端服务，``index.html`` 里注入的是第一个实例的
``WS_PORT``，于是它的界面去连第一个实例的后端 —— 两个窗口看/改同一份状态（用户报的
「打开两个应用，感觉配置有干扰」）。所以这里同时钉两件事：

1. Windows 下 ``StaticHTTPServer.allow_reuse_address`` 必须是 ``False``（重复绑定必须报错）；
2. 真的连着起两次 ``start_http_server()``，第二次必须让到下一个端口，且两个端口各自
   都能正常响应。
"""
import socket
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _find_free_port(start=18870, span=50):
    for port in range(start, start + span):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('no free port found')


def run() -> bool:
    import server as srv

    ok = True

    # 暂不启动accept循环，模拟冷启动瞬间页面脚本连接先集中到达。
    # 默认5条队列会在第6条失败；实际HTTP服务必须容纳整批资源连接。
    pending = srv.StaticHTTPServer(('127.0.0.1', 0), srv.CustomHandler)
    clients = []
    try:
        for _ in range(20):
            clients.append(socket.create_connection(pending.server_address, timeout=1))
        print('  [PASS] 冷启动短暂停止accept时20条资源连接全部入队')
    except OSError as exc:
        print(f'  [FAIL] 冷启动资源连接队列不足：仅{len(clients)}条成功，{exc}')
        ok = False
    finally:
        for client in clients:
            client.close()
        pending.server_close()

    # --- 1. 端口语义：Windows 下必须关掉 SO_REUSEADDR ---
    if os.name == 'nt':
        if srv.StaticHTTPServer.allow_reuse_address is False:
            print('  [PASS] Windows 下已关闭 SO_REUSEADDR（重复绑定会报错而不是静默共用）')
        else:
            print('  [FAIL] allow_reuse_address 仍是 True：第二个实例会与第一个共用端口')
            ok = False
    else:
        print('  [SKIP] 非 Windows：SO_REUSEADDR 本来就不允许两个活监听')

    # --- 2. 连着起两个静态服务，端口必须错开 ---
    old_http = srv.HTTP_PORT
    srv.HTTP_PORT = _find_free_port()
    first = second = None
    try:
        first = srv.start_http_server()
        port1 = first.server_address[1]
        second = srv.start_http_server()
        port2 = second.server_address[1]
        if port1 == srv.HTTP_PORT and port2 == port1 + 1:
            print(f'  [PASS] 第二个实例让到下一个端口（{port1} → {port2}）')
        else:
            print(f'  [FAIL] 端口分配不对：期望 {srv.HTTP_PORT}/{srv.HTTP_PORT + 1}，实得 {port1}/{port2}')
            ok = False
    except Exception as exc:
        print(f'  [FAIL] 起第二个静态服务出错：{type(exc).__name__}: {exc}')
        ok = False
    finally:
        srv.HTTP_PORT = old_http

    # --- 3. 两个端口都要能真的服务页面 ---
    import urllib.request
    for name, srv_obj in (('第一个', first), ('第二个', second)):
        if srv_obj is None:
            continue
        port = srv_obj.server_address[1]
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as resp:
                html = resp.read().decode('utf-8')
            if resp.status == 200 and 'WS_PORT' in html:
                print(f'  [PASS] {name}实例端口 :{port} 正常返回 index.html')
            else:
                print(f'  [FAIL] {name}实例端口 :{port} 返回内容异常')
                ok = False
        except Exception as exc:
            print(f'  [FAIL] {name}实例端口 :{port} 打不开：{type(exc).__name__}: {exc}')
            ok = False

    for srv_obj in (first, second):
        if srv_obj is not None:
            try:
                srv_obj.shutdown()
                srv_obj.server_close()
            except Exception:
                pass

    return ok
