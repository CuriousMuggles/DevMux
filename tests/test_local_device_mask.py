"""验证「本机设备识别开关」（``ADB_MANAGER_NO_LOCAL_DEVICES=1``）真的生效。

开关的用途：这台机器不接设备，只做主控去管**别的机器**上的设备。开启后本实例
既不识别本机设备（窗口里只剩远端设备），也不把本机设备共享给对端。

它同时是同机双实例验证的分界线：两个实例其实跑在同一台 PC 上，本机 adb 能看到
全部设备；不屏蔽的话，主控「自己直连也能做同一件事」，跨机链路就没被真正验证过。
所以这里逐层钉住四个点：

1. ``ADBWorker``：屏蔽后不枚举、不建本机 PTY 会话（列表恒空）。对照组必须照常
   看到设备 —— 否则「恒空」可能只是因为测试数据本来就空。
2. ``RelayNode``：屏蔽后不枚举、不共享任何设备（心跳里设备数为 0）。
3. ``server.ManagerServer``：开关要**真的下发**到 worker 与中转节点，合并后的
   设备列表里没有本机设备。只定义常量而下发漏了，是这类开关最典型的坏法。
4. 环境变量解析：只有明确写了真值才算开启，其余（含空值）一律保持旧行为。

全部用替身提供设备，**不需要真机、真 adb 或第二台电脑**。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay          # noqa: E402
import relay_node     # noqa: E402

FAKE_DEVICES = [
    {'serial': 'devA', 'state': 'device', 'transportId': 2, 'key': 'devA:tid=2'},
    {'serial': 'devB', 'state': 'device', 'transportId': 1, 'key': 'devB:tid=1'},
]


def _fake_devices():
    return [dict(device) for device in FAKE_DEVICES]


# ----------------------------------------------------------------------
# 1. ADBWorker：不枚举、不起本机终端
# ----------------------------------------------------------------------
def _check_worker_mask() -> list:
    import adb_worker

    saved_devices = adb_worker.get_all_devices
    saved_spawn = adb_worker.ADBWorker._spawn_terminal
    spawned: list = []
    checks: list = []
    workers: list = []
    try:
        adb_worker.get_all_devices = _fake_devices
        # 别真去 spawn winpty：本用例只关心「屏蔽后不该走到 spawn」。
        adb_worker.ADBWorker._spawn_terminal = lambda self, key, device: spawned.append(key)

        masked = adb_worker.ADBWorker(
            lambda *_: None, lambda *_: None, lambda *_: None, local_devices_enabled=False,
        )
        workers.append(masked)
        masked._tick()
        masked_devices, masked_keys = masked.get_state()
        checks.append((
            '屏蔽后本机设备列表恒空',
            masked_devices == [] and masked_keys == [], str(masked_keys),
        ))
        checks.append((
            '屏蔽后不为本机设备建终端会话',
            not any(key.startswith('dev') for key in spawned), str(spawned),
        ))

        # 对照组：同样两份设备数据，不屏蔽时必须照常出现（证明上面的空不是数据本身为空）。
        spawned.clear()
        normal = adb_worker.ADBWorker(lambda *_: None, lambda *_: None, lambda *_: None)
        workers.append(normal)
        normal._tick()
        normal_devices, normal_keys = normal.get_state()
        checks.append((
            '对照组（不屏蔽）照常看到两台设备',
            sorted(normal_keys) == ['devA:tid=2', 'devB:tid=1'], str(normal_keys),
        ))
        checks.append((
            '对照组为每台设备各起一个终端会话',
            sorted(spawned) == ['devA:tid=2', 'devB:tid=1'], str(spawned),
        ))
    finally:
        for worker in workers:
            worker.stop()
        adb_worker.get_all_devices = saved_devices
        adb_worker.ADBWorker._spawn_terminal = saved_spawn
    return checks


# ----------------------------------------------------------------------
# 2. RelayNode：不枚举、不共享
# ----------------------------------------------------------------------
def _check_node_mask(root) -> list:
    saved_list = relay_node.list_devices
    calls: list = []
    nodes: list = []
    checks: list = []

    def counting_list(adb_path, timeout=8.0):
        calls.append(str(adb_path))
        return _fake_devices(), ''

    try:
        relay_node.list_devices = counting_list

        masked = relay_node.RelayNode(
            peer_id='pc-a', role=relay.ROLE_MASTER, root=os.path.join(root, 'a'),
            adb_path='masked-node', local_devices_enabled=False,
        )
        nodes.append(masked)
        masked._refresh_devices()
        checks.append((
            '屏蔽后不枚举本机设备（一次 adb 都不调）',
            'masked-node' not in calls, str(calls),
        ))
        checks.append((
            '屏蔽后不共享任何设备',
            masked._managed_devices() == [] and masked.local_devices_enabled is False,
            str(masked._managed_devices()),
        ))
        beat = relay.PeerHeartbeat(peerId='pc-a', role=relay.ROLE_MASTER,
                                   devices=[dict(d) for d in masked._managed_devices()])
        checks.append((
            '屏蔽后心跳里设备数为 0（对端看不到它的设备）',
            beat.devices == [], str(beat.devices),
        ))

        # 对照组
        normal = relay_node.RelayNode(
            peer_id='pc-b', role=relay.ROLE_SLAVE, root=os.path.join(root, 'b'),
            adb_path='normal-node',
        )
        nodes.append(normal)
        normal._refresh_devices()
        checks.append((
            '对照组（不屏蔽）照常枚举并共享设备',
            [d['key'] for d in normal._managed_devices()] == ['devA:tid=2', 'devB:tid=1'],
            str(normal._managed_devices()),
        ))
        checks.append((
            '对照组会去调 adb 枚举',
            'normal-node' in calls, str(calls),
        ))
    finally:
        for node in nodes:
            try:
                node.stop()
            except Exception:
                pass
        relay_node.list_devices = saved_list
    return checks


# ----------------------------------------------------------------------
# 3. server.ManagerServer：开关真的下发到了 worker 与中转节点
# ----------------------------------------------------------------------
def _check_server_dispatch(root) -> list:
    """合并后的设备列表里不能有本机设备 —— 这条要跨过整条接线才成立。

    只改 ``_merged_devices`` 或只改 worker 都不够：开关必须从 server.py 一路传到
    ``ADBWorker`` 与 ``RelayController``。这里用真实的 ``ManagerServer``（真 worker、
    真 relay 控制器）跑两遍，只看合并结果。

    替身要挂在 **server 实际使用的那份 adb_worker 命名空间** 上。``tests/test_imports.py``
    会把 ``server`` / ``adb_worker`` 从 ``sys.modules`` 删掉再重新导入，于是进程里同时
    活着两份 ``adb_worker`` 模块对象：``server`` 绑的是其中一份的 ``ADBWorker``，而
    ``import adb_worker`` 拿到的是另一份。把替身挂在后者上，前者一个字都读不到 ——
    本用例单独跑通过、在全量里失败，就是这个原因。``srv.ADBWorker._tick.__globals__``
    就是这个类真正会去查的那份命名空间。
    """
    import server as srv

    worker_cls = srv.ADBWorker
    owner_globals = worker_cls._tick.__globals__
    saved_devices = owner_globals.get('get_all_devices')
    saved_spawn = worker_cls._spawn_terminal
    saved_flag = srv.LOCAL_DEVICES_ENABLED
    saved_env = os.environ.get('RELAY_CONFIG')
    servers: list = []
    checks: list = []
    try:
        # 别真去 spawn winpty：本用例只关心「屏蔽后列表里没有本机设备」。
        owner_globals['get_all_devices'] = _fake_devices
        worker_cls._spawn_terminal = lambda self, key, device: None
        os.environ['RELAY_CONFIG'] = os.path.join(root, 'relay.json')

        for enabled in (True, False):
            srv.LOCAL_DEVICES_ENABLED = enabled
            manager = srv.ManagerServer()
            servers.append(manager)
            manager.worker._tick()
            merged = manager._merged_devices()
            if enabled:
                checks.append((
                    '默认（开关关闭）时窗口里照常有本机设备',
                    sorted(d['key'] for d in merged) == ['devA:tid=2', 'devB:tid=1'],
                    str([d.get('key') for d in merged]),
                ))
                checks.append((
                    '默认时中转节点没有走屏蔽路径',
                    manager.relay.local_devices_enabled is True
                    and manager.worker.local_devices_enabled is True,
                    f'relay={manager.relay.local_devices_enabled} '
                    f'worker={manager.worker.local_devices_enabled}',
                ))
            else:
                checks.append((
                    '开关打开后合并列表里没有本机设备',
                    merged == [], str([d.get('key') for d in merged]),
                ))
                checks.append((
                    '开关被下发到 worker 与中转节点（不是只定义了常量）',
                    manager.worker.local_devices_enabled is False
                    and manager.relay.local_devices_enabled is False,
                    f'relay={manager.relay.local_devices_enabled} '
                    f'worker={manager.worker.local_devices_enabled}',
                ))
                checks.append((
                    '开关打开后中转节点也不共享本机设备',
                    manager.relay.local_devices_enabled is False,
                    str(manager.relay.local_devices_enabled),
                ))
    finally:
        for manager in servers:
            try:
                manager.worker.stop()
            except Exception:
                pass
            try:
                manager._executor.shutdown(wait=False)
            except Exception:
                pass
        owner_globals['get_all_devices'] = saved_devices
        worker_cls._spawn_terminal = saved_spawn
        srv.LOCAL_DEVICES_ENABLED = saved_flag
        if saved_env is None:
            os.environ.pop('RELAY_CONFIG', None)
        else:
            os.environ['RELAY_CONFIG'] = saved_env
    return checks


# ----------------------------------------------------------------------
# 4. 环境变量解析
# ----------------------------------------------------------------------
def _check_env_flag() -> list:
    import server as srv

    checks = []
    for raw, expected in (
        ('', False), (None, False), ('0', False), ('false', False), ('no', False),
        ('1', True), ('true', True), ('TRUE', True), ('yes', True), ('on', True), (' 1 ', True),
    ):
        saved = os.environ.get('ADB_MANAGER_NO_LOCAL_DEVICES')
        try:
            if raw is None:
                os.environ.pop('ADB_MANAGER_NO_LOCAL_DEVICES', None)
            else:
                os.environ['ADB_MANAGER_NO_LOCAL_DEVICES'] = raw
            got = srv._env_flag('ADB_MANAGER_NO_LOCAL_DEVICES')
            checks.append((
                f'环境变量 {raw!r} → 屏蔽={got}',
                got is expected, f'期望 {expected}，实际 {got}',
            ))
        finally:
            if saved is None:
                os.environ.pop('ADB_MANAGER_NO_LOCAL_DEVICES', None)
            else:
                os.environ['ADB_MANAGER_NO_LOCAL_DEVICES'] = saved

    checks.append((
        '本测试进程没设该变量时，默认照常识别本机设备',
        os.environ.get('ADB_MANAGER_NO_LOCAL_DEVICES') is None
        and srv.LOCAL_DEVICES_ENABLED is True,
        f'env={os.environ.get("ADB_MANAGER_NO_LOCAL_DEVICES")!r} '
        f'LOCAL_DEVICES_ENABLED={srv.LOCAL_DEVICES_ENABLED}',
    ))
    return checks


def run() -> bool:
    checks: list = []
    tmp = tempfile.mkdtemp(prefix='local-device-mask-')
    try:
        checks += _check_worker_mask()
        checks += _check_node_mask(os.path.join(tmp, 'node'))
        checks += _check_server_dispatch(os.path.join(tmp, 'server'))
        checks += _check_env_flag()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for name, passed, detail in checks:
        suffix = '' if passed else f' — {detail}'
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{suffix}")
        ok = ok and passed
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
