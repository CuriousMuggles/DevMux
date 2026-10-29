"""主应用隔离宿主：空设备、假执行器；交互预览保留原生文件窗口。"""
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import DBFlashService, SimulationExecutor
from tests.test_db_flash_server import EmptyWorker


async def main():
    import server
    import adb_worker
    runtime = Path(os.environ['ADB_AGENT_RUNTIME_DIR'])
    runtime.mkdir(parents=True, exist_ok=True)
    adb_worker.LOG_FILE = str(runtime / 'app-test.log')
    server.ADBWorker = lambda **_kwargs: EmptyWorker()
    server.ManagerServer._commands_path = staticmethod(lambda: str(runtime / 'commands.json'))

    async def fake_file(_self, multi=False):
        paths = json.loads(os.environ['DB_FLASH_TEST_PICK_FILES'])
        return {'success': True, 'path': paths[0], 'paths': paths}

    async def fake_directory(_self, title=''):
        return {'success': True, 'path': os.environ['DB_FLASH_TEST_TOOL_DIR']}

    preview = os.environ.get('DB_FLASH_INTERACTIVE_PREVIEW') == '1'
    if not preview:
        server.ManagerServer._pick_file = fake_file
        server.ManagerServer._pick_dir = fake_directory
    else:
        # 交互预览保留真实文件窗口，只开放固化模拟及页面必要的只读请求。
        original_handle = server.ManagerServer._handle_message
        original_ws = server.ManagerServer.ws_handler
        from urllib.parse import urlsplit

        async def guarded_handle(manager, ws, message):
            if message.get('type') == 'extension-tool' and message.get('tool') == 'db-flash' and message.get('target', 'computer:local') == 'computer:local':
                await original_handle(manager, ws, message)
                return
            allowed = {'db-flash', 'pick-file', 'pick-directory',
                       'relay-status', 'relay-config', 'get-commands', 'restart-server', 'ui-debug'}
            if message.get('type') not in allowed:
                await manager._send(ws, {'type': 'exec-result', 'id': message.get('id'),
                                        'success': False, 'output': '交互预览仅开放固化模拟与文件选择'})
                return
            await original_handle(manager, ws, message)

        async def guarded_ws(manager, ws):
            try:
                origin = urlsplit(ws.request.headers.get('Origin', ''))
                valid = origin.scheme == 'http' and origin.hostname in ('127.0.0.1', 'localhost') and origin.port == server._HTTP_PORT
            except ValueError:
                valid = False
            if not valid or ws.request.path == '/agent':
                await ws.close(code=1008, reason='仅允许本机固化模拟页面')
                return
            await original_ws(manager, ws)

        server.ManagerServer._handle_message = guarded_handle
        server.ManagerServer.ws_handler = guarded_ws
    # 包括后端重启的工厂也固定为假执行器，回归不能操作设备。
    server.RealExecutor = lambda: SimulationExecutor(delay=.12)
    manager = server.ManagerServer()
    manager.db_flash = DBFlashService(SimulationExecutor(delay=.12), manager._broadcast_async)
    http = server.start_http_server()

    def stop_on_input():
        for line in sys.stdin:
            if line.strip() == 'stop':
                manager.stop()
                return

    threading.Thread(target=stop_on_input, name='db-flash-test-stop', daemon=True).start()
    print('MAIN_APP_SIMULATION_READY', flush=True)
    try:
        # 回归模式禁止全部外部命令；交互模式只开放白名单及原生文件窗口。
        run_guard = nullcontext() if preview else patch('subprocess.run', side_effect=AssertionError('集成测试禁止执行外部命令'))
        popen_guard = nullcontext() if preview else patch('subprocess.Popen', side_effect=AssertionError('集成测试禁止执行外部命令'))
        with run_guard, popen_guard:
            await manager.run()
    finally:
        manager.db_flash.close()
        http.shutdown()
        http.server_close()


if __name__ == '__main__':
    asyncio.run(main())
