"""真实页面验证菜单偏好，不连接设备或执行工具操作。"""
import json
from pathlib import Path
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_relay_row_layout import _edge_executable, _free_port, _start_edge


def run():
    import server
    from websockets.sync.client import connect
    old_port = server.HTTP_PORT
    server.HTTP_PORT = _free_port()
    httpd = None
    edge = None
    try:
        httpd = server.start_http_server()
        # Edge 被终止后句柄释放有延迟，Cache 文件可能仍被占用：清理失败不应判失败。
        with tempfile.TemporaryDirectory(prefix='extension-menu-', ignore_cleanup_errors=True) as profile:
            edge = _start_edge(_edge_executable(), profile, 0,
                               f'http://127.0.0.1:{httpd.server_address[1]}/')
            with connect(edge.wait_ready(25), proxy=None) as ws:
                counter = 0

                def evaluate(expression):
                    nonlocal counter
                    counter += 1
                    ws.send(json.dumps({'id': counter, 'method': 'Runtime.evaluate',
                                        'params': {'expression': expression, 'returnByValue': True}}))
                    while True:
                        response = json.loads(ws.recv(timeout=20))
                        if response.get('id') == counter:
                            result = response['result']
                            if result.get('exceptionDetails'):
                                raise AssertionError(result['exceptionDetails'])
                            return result['result'].get('value')

                def wait_ready():
                    for _ in range(60):
                        if evaluate("document.querySelectorAll('#extension-tool-options input').length === 1"):
                            return
                        time.sleep(.1)
                    raise AssertionError('设置复选框未加载')

                def wait_until(expression, message):
                    """页面更新是异步的：点击后立即读布局会偶发读到旧状态。"""
                    for _ in range(120):
                        if evaluate(expression):
                            return
                        time.sleep(.05)
                    raise AssertionError(message)

                wait_ready()
                wait_until("[...document.querySelectorAll('#extension-tool-options input')].every(x=>x.checked)", '默认未全选')

                def open_settings():
                    """tab 点击绑定在 terminal.js，晚于本面板渲染；绑定前点击会落空。"""
                    for _ in range(100):
                        evaluate("document.querySelector('[data-tab=settings]').click()")
                        if evaluate("document.getElementById('extension-tool-options').getBoundingClientRect().height > 0"):
                            return
                        time.sleep(.1)
                    raise AssertionError('设置面板未展开')

                open_settings()
                pairs = [('relay', 'tools-relay')]
                for name, menu in pairs:
                    evaluate(f"document.getElementById('extension-tool-{name}').click()")
                    wait_until(f"getComputedStyle(document.getElementById('{menu}')).display === 'none'", f'{name} 菜单未隐藏')
                wait_until("getComputedStyle(document.getElementById('tools-tab')).display !== 'none'", '全部隐藏后应回退显示工具页')
                evaluate('location.reload()')
                time.sleep(.6)
                wait_ready()
                wait_until("[...document.querySelectorAll('#extension-tool-options input')].every(x=>!x.checked)", '刷新后未恢复隐藏状态')
                evaluate("document.getElementById('extension-tool-relay').click()")
                wait_until("getComputedStyle(document.getElementById('tools-tab')).display !== 'none'", '恢复后工具页应重新显示')
                evaluate("localStorage.setItem('extension-tool-menu-v1', '{broken'); location.reload()")
                time.sleep(.6)
                wait_ready()
                wait_until("[...document.querySelectorAll('#extension-tool-options input')].every(x=>x.checked)", '损坏配置应回退默认全选')
                evaluate("Storage.prototype.setItem = () => {throw Error('不可用')}; document.getElementById('extension-tool-relay').click()")
                wait_until("document.getElementById('tools-relay').style.display === 'none' && document.getElementById('extension-tool-save-status').textContent.includes('无法保存')", '保存失败时应仍隐藏菜单并提示')
                print('[OK] Edge：四项默认显示、逐项隐藏、空菜单、恢复、刷新、损坏配置及写入失败')
            edge.close()
            edge = None
            # Job 结束后，Windows 释放浏览器缓存文件句柄需要短暂时间。
            time.sleep(.5)
        return True
    except Exception as exc:
        # 裸 assert 没有消息，只有堆栈能指明失败的是哪一条页面断言。
        traceback.print_exc()
        print('[FAIL]', exc)
        return False
    finally:
        if edge:
            edge.close()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        server.HTTP_PORT = old_port


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
