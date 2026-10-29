"""验证慢 ADB 和慢 WebSocket 客户端不会累积后台线程或广播协程。"""
import asyncio
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _test_worker_stop_token() -> bool:
    import adb_worker

    original_devices = adb_worker.get_all_devices
    original_timeout = adb_worker.POLL_STOP_JOIN_TIMEOUT
    entered = threading.Event()
    release = threading.Event()
    spawned = []

    def slow_devices():
        entered.set()
        release.wait(2)
        return [{'serial': 'TEST', 'state': 'device', 'transportId': 1}]

    worker = adb_worker.ADBWorker(lambda *_: None, lambda *_: None, lambda *_: None)
    worker._spawn_terminal = lambda key, _device: spawned.append(key)
    try:
        adb_worker.get_all_devices = slow_devices
        adb_worker.POLL_STOP_JOIN_TIMEOUT = 0.05
        first_started = worker.start()
        entered_ok = entered.wait(1)
        stopped = worker.stop()
        duplicate_start = worker.start()
        release.set()
        worker._poll_thread.join(timeout=1)
        old_exited = not worker._poll_thread.is_alive()
        stale_spawned = list(spawned)

        # 旧线程退出后才能创建新的轮询线程。
        restarted = worker.start()
        time.sleep(0.03)
        worker.stop()
        passed = (
            first_started and entered_ok and not stopped and not duplicate_start
            and old_exited and restarted and not stale_spawned
        )
        print(f'  [{"PASS" if passed else "FAIL"}] 慢 ADB 停止后不复活旧轮询线程')
        return passed
    finally:
        release.set()
        worker.stop()
        adb_worker.get_all_devices = original_devices
        adb_worker.POLL_STOP_JOIN_TIMEOUT = original_timeout


def _test_bounded_broadcast_queue() -> bool:
    import server

    class SlowClient:
        async def send(self, _message):
            await asyncio.sleep(5)

    async def exercise():
        manager = server.ManagerServer()
        manager.loop = asyncio.get_running_loop()
        manager.clients.add(SlowClient())
        try:
            for index in range(server.BROADCAST_QUEUE_LIMIT + 100):
                manager._broadcast_async({
                    'type': 'terminal-data', 'key': 'stress', 'data': str(index),
                })
            await asyncio.sleep(0.05)
            with manager._broadcast_lock:
                queued = len(manager._broadcast_queue)
                dropped = manager._dropped_broadcasts
                scheduled = manager._broadcast_scheduled
            drain_tasks = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and getattr(task.get_coro(), '__name__', '') == '_drain_broadcasts'
            ]
            return queued, dropped, scheduled, len(drain_tasks)
        finally:
            manager._cancel_pending_broadcasts()
            await asyncio.sleep(0)
            manager._executor.shutdown(wait=False, cancel_futures=True)

    queued, dropped, scheduled, drain_count = asyncio.run(exercise())
    passed = (
        queued <= server.BROADCAST_QUEUE_LIMIT
        and dropped >= 99
        and scheduled
        and drain_count == 1
    )
    print(f'  [{"PASS" if passed else "FAIL"}] 慢客户端广播队列有界且只保留一个调度协程')
    return passed


def run() -> bool:
    return _test_worker_stop_token() and _test_bounded_broadcast_queue()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
