"""真实页面验证菜单偏好，不连接设备或执行工具操作。"""
import json
from pathlib import Path
import sys
import tempfile
import time

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
        with tempfile.TemporaryDirectory(prefix='extension-menu-') as profile:
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

                wait_ready()
                assert evaluate("[...document.querySelectorAll('#extension-tool-options input')].every(x=>x.checked)")
                evaluate("document.querySelector('[data-tab=settings]').click()")
                assert evaluate("document.getElementById('extension-tool-options').getBoundingClientRect().height > 0")
                pairs = [('relay', 'tools-relay')]
                for name, menu in pairs:
                    evaluate(f"document.getElementById('extension-tool-{name}').click()")
                    assert evaluate(f"getComputedStyle(document.getElementById('{menu}')).display === 'none'")
                assert evaluate("getComputedStyle(document.getElementById('tools-tab')).display !== 'none'")
                evaluate('location.reload()')
                time.sleep(.6)
                wait_ready()
                assert evaluate("[...document.querySelectorAll('#extension-tool-options input')].every(x=>!x.checked)")
                evaluate("document.getElementById('extension-tool-relay').click()")
                assert evaluate("getComputedStyle(document.getElementById('tools-tab')).display !== 'none'")
                evaluate("localStorage.setItem('extension-tool-menu-v1', '{broken'); location.reload()")
                time.sleep(.6)
                wait_ready()
                assert evaluate("[...document.querySelectorAll('#extension-tool-options input')].every(x=>x.checked)")
                evaluate("Storage.prototype.setItem = () => {throw Error('不可用')}; document.getElementById('extension-tool-relay').click()")
                assert evaluate("document.getElementById('tools-relay').style.display === 'none' && document.getElementById('extension-tool-save-status').textContent.includes('无法保存')")
                print('[OK] Edge：四项默认显示、逐项隐藏、空菜单、恢复、刷新、损坏配置及写入失败')
            edge.close()
            edge = None
            # Job 结束后，Windows 释放浏览器缓存文件句柄需要短暂时间。
            time.sleep(.5)
        return True
    except Exception as exc:
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
