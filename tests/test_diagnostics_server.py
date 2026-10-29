"""验证诊断 WebSocket 调度不会阻塞 handler，并会返回结构化结果。"""
import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def run() -> bool:
    import diagnostics
    import server

    old_runner = server.ADBDiagnosticRunner

    class FakeRunner:
        def __init__(self, _adb_path, _creationflags, _logger, progress):
            self.progress = progress

        def run(self, selected_device, _cancel_event):
            self.progress(1, 6, 'runtime', '应用运行信息')
            check = diagnostics.DiagnosticCheck('runtime', '应用运行信息', 'pass', '测试通过')
            return diagnostics.DiagnosticReport('test-report', 'start', 'end', selected_device, [check], {
                'priority': '无', 'title': 'ADB 基础链路正常', 'summary': '测试通过', 'advice': '',
            })

    async def exercise():
        manager = server.ManagerServer()
        manager.loop = asyncio.get_running_loop()
        ws = _FakeWS()
        manager.clients.add(ws)
        await manager._handle_message(ws, {
            'type': 'run-adb-diagnostic', 'id': 42,
            'selectedDevice': {'serial': 'TEST', 'transportId': 1},
        })
        for _ in range(20):
            if any('diagnostic-result' in message for message in ws.messages):
                break
            await asyncio.sleep(0.05)
        return ws.messages, manager._diagnostic_session

    try:
        server.ADBDiagnosticRunner = FakeRunner
        messages, active_session = asyncio.run(exercise())
        payloads = [__import__('json').loads(message) for message in messages]
        has_progress = any(payload.get('type') == 'diagnostic-progress' for payload in payloads)
        result = next((payload for payload in payloads if payload.get('type') == 'diagnostic-result'), None)
        passed = has_progress and result and result.get('success') and result.get('report', {}).get('id') == 'test-report' and active_session is None
        print(f'  [{"PASS" if passed else "FAIL"}] 诊断异步返回进度和结构化结果')
        return bool(passed)
    finally:
        server.ADBDiagnosticRunner = old_runner


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
