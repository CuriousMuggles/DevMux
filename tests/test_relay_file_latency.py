"""慢设备枚举、重复目录浏览和下载拒绝的响应回归。"""
import concurrent.futures
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay
import relay_node
import relay_transfer
from tests.test_relay_app import _Harness, _fake_list_devices, _fake_execute, _wait_until


def run():
    release = threading.Event()
    try:
        with tempfile.TemporaryDirectory() as root, patch.object(relay_node, 'list_devices', _fake_list_devices), patch.object(relay_node, 'execute', _fake_execute):
            harness = _Harness(root, master_local_devices_enabled=False)
            harness.start()
            try:
                # 未确认连接且控制循环锁被慢操作占用时，仍必须持续写心跳。
                slave_node = harness.slave.node
                assert _wait_until(lambda: 'pc-b' in relay.read_peers(slave_node.queue.peers_dir), 8), \
                    '从机启动后未写入首个心跳'
                with slave_node._io_lock:
                    before = relay.read_peers(slave_node.queue.peers_dir)['pc-b'].fileMtimeMs
                    assert _wait_until(lambda: relay.read_peers(slave_node.queue.peers_dir)['pc-b'].fileMtimeMs > before,
                                       slave_node.heartbeat_interval_s + 3), '未配对或控制循环阻塞时心跳停止'
                print('  [PASS] 未配对且控制循环阻塞时从机心跳仍持续更新')
                master_node = harness.master.node
                assert _wait_until(lambda: 'pc-a' in relay.read_peers(master_node.queue.peers_dir), 8), \
                    '主控启动后未写入首个心跳'
                with master_node._io_lock:
                    before = relay.read_peers(master_node.queue.peers_dir)['pc-a'].fileMtimeMs
                    assert _wait_until(lambda: relay.read_peers(master_node.queue.peers_dir)['pc-a'].fileMtimeMs > before,
                                       master_node.heartbeat_interval_s + 3), '主控阻塞时心跳停止'
                print('  [PASS] 主控也独立持续写心跳，从机可检测主控在线')
                harness.confirm_both()
                assert _wait_until(lambda: bool(harness.master.devices()) and harness.master.node.connection_ready('pc-b'), 8)
                key = harness.master.devices()[0]['key']
                # 连接验证节奏：成功后退避到 30s 档复查，失败保持 5s 档密集重试。
                verify_node = harness.master.node
                with verify_node._io_lock:
                    with patch.object(verify_node, 'submit', return_value=relay.Result(
                            reqId='probe-ok', status=relay.STATUS_OK,
                            extra={'nodeId': harness.slave.config.nodeId})):
                        verify_node.verify_connection('pc-b')
                ok_gap = verify_node._connection_check_after['pc-b'] - time.monotonic()
                assert 25 <= ok_gap <= 35, ok_gap
                with patch.object(verify_node, 'submit', return_value=relay.Result(
                        reqId='probe-fail', status=relay.STATUS_ERROR, error='x')):
                    verify_node.verify_connection('pc-b')
                fail_gap = verify_node._connection_check_after['pc-b'] - time.monotonic()
                assert 0 <= fail_gap <= 6, fail_gap
                verify_node._mark_connection_verified('pc-b')  # 恢复已验证状态供后续断言
                print('  [PASS] 验证成功退避到 30s、失败仍 5s 密集重试')
                # 重新确认（新代次）会作废验证节奏：即使挂着 60s 的未来复查时间
                # 和已验证标记，pair_peer 之后也必须立即到期重验，不能等旧节奏。
                with verify_node._io_lock:
                    verify_node._connection_check_after['pc-b'] = time.monotonic() + 60
                    verify_node._connection_verified['pc-b'] = (
                        verify_node._connection_endpoint('pc-b'), time.monotonic())
                    not_due_before = not verify_node.connection_check_due('pc-b')
                    original_cid = verify_node.pairing.get(harness.slave.config.nodeId).connectionId
                verify_node.pair_peer(harness.slave.config.nodeId, peer_id='pc-b')
                with verify_node._io_lock:
                    due_after_pair = verify_node.connection_check_due('pc-b')
                    mark_cleared = verify_node._connection_verified.get('pc-b') is None
                assert not_due_before and due_after_pair and mark_cleared
                # 恢复原确认代次，避免后续真实从机操作被当作退役代次拒绝。
                verify_node.pair_peer(harness.slave.config.nodeId, peer_id='pc-b',
                                      connection_id=original_cid)
                print('  [PASS] 重新确认立即重验，不等 30s 成功档')
                # 对端端点变化（模拟重启/换部署）后，验证节奏同样立即到期：
                # 挂着旧端点的已验证标记 + 未来复查时间，一轮刷新就强制重验。
                with verify_node._io_lock:
                    verify_node._connection_check_after['pc-b'] = time.monotonic() + 60
                    verify_node._connection_verified['pc-b'] = (
                        ('旧根目录', '旧实例', '旧启动代次'), time.monotonic())
                    stale_not_due = not verify_node.connection_check_due('pc-b')
                verify_calls = []
                with patch.object(verify_node, 'verify_connection',
                                  side_effect=lambda peer: verify_calls.append(peer) or None):
                    harness.master._refresh_connections()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and verify_calls != ['pc-b']:
                        time.sleep(0.02)
                assert stale_not_due and verify_calls == ['pc-b']
                verify_node._mark_connection_verified('pc-b')  # 恢复已验证状态供后续断言
                print('  [PASS] 端点变化后验证节奏立即到期')
                controller = harness.master
                with patch.object(relay, 'deployment_roots', side_effect=AssertionError('状态读取共享目录')), \
                        patch.object(relay, 'read_registry', side_effect=AssertionError('状态读取注册表')):
                    started = time.monotonic()
                    state = controller.status()
                    assert state['pairing'] and time.monotonic() - started < 0.5
                print('  [PASS] 状态快照不重复读取共享目录或注册表')
                node = controller.node
                with node._io_lock:
                    verified = dict(node._connection_verified)
                    checks = dict(node._connection_checks)
                    after = dict(node._connection_check_after)
                    with patch.object(node, '_connection_endpoint', side_effect=[('old',), ('new',)]), \
                            patch.object(node, 'submit', return_value=relay.Result(
                                reqId='old-probe', status=relay.STATUS_REJECTED, error='旧实例拒绝')):
                        node.verify_connection('pc-b')
                    assert node._connection_verified == verified and node._connection_checks == checks
                    assert node._connection_check_after == after
                    record = node.pairing.get(harness.slave.config.nodeId)
                    original_generation = record.connectionId
                    def old_generation_result(*args, **kwargs):
                        record.connectionId = 'new-generation'
                        return relay.Result(reqId='old-generation', status=relay.STATUS_REJECTED, error='旧代次拒绝')
                    try:
                        with patch.object(node, 'submit', old_generation_result):
                            node.verify_connection('pc-b')
                        assert node._connection_verified == verified and node._connection_checks == checks
                        assert node._connection_check_after == after
                    finally:
                        record.connectionId = original_generation
                print('  [PASS] 旧实例和旧确认代次的失败回执不能覆盖当前连接')
                # 已验证连接不再重复发起检查；拒绝下载不产生清理请求。
                original_submit = controller.node.submit
                kinds = []
                def submit(peer, kind, **kwargs):
                    kinds.append(kind)
                    return original_submit(peer, kind, **kwargs)
                with patch.object(controller.node, 'submit', submit), patch.object(controller.node, 'verify_connection', side_effect=AssertionError('重复连接验证')):
                    r = controller.remote_transfer(key, ['pull', '/sdcard/file.txt', root])
                    assert not r['success'] and 'pull' in r['output'] and '白名单' in r['output'], r
                assert relay_transfer.KIND_TRANSFER in kinds and relay_transfer.KIND_CLEANUP not in kinds
                print('  [PASS] 已验证连接不重复探测，白名单拒绝直接返回而不等待清理')
                # 从机只修改白名单并保存，原配对和确认代次应自动恢复。
                master_record = controller.node.pairing.get(harness.slave.config.nodeId)
                connection_id = master_record.connectionId
                old_slave = harness.slave.node
                saved, reason = harness.slave.apply_config({'allowedSubcommands': ['shell', 'push', 'pull']})
                assert saved, reason
                assert harness.slave.node is not old_slave
                assert _wait_until(lambda: controller.node._connection_endpoint('pc-b')
                                   and controller.node._connection_endpoint('pc-b')[-1] == harness.slave.node._started_at
                                   and controller.node.connection_ready('pc-b'), 12), '保存从机配置后未自动恢复连接'
                assert controller.node.pairing.get(harness.slave.config.nodeId).connectionId == connection_id
                print('  [PASS] 从机保存白名单后保留原确认代次，主控自动重新验证连接')
                presence_entered, presence_release = threading.Event(), threading.Event()
                def slow_presence():
                    presence_entered.set()
                    presence_release.wait(5)
                try:
                    with patch.object(controller.node, '_refresh_presence', slow_presence):
                        assert presence_entered.wait(3), '未进入慢注册表刷新'
                        previous_beat = controller.node._peers['pc-b'].heartbeatAt
                        assert _wait_until(lambda: controller.node._peers['pc-b'].heartbeatAt != previous_beat, 3), \
                            '注册表刷新阻塞导致在线心跳快照停滞'
                        assert not presence_release.is_set()
                finally:
                    presence_release.set()
                print('  [PASS] 注册表刷新挂起时心跳发现快照仍继续更新')
                compat_entered, compat_release = threading.Event(), threading.Event()
                def slow_compat(beat):
                    compat_entered.set()
                    compat_release.wait(5)
                try:
                    with patch.object(controller.node, '_publish_heartbeat_compat', slow_compat):
                        assert compat_entered.wait(3), '未进入慢兼容心跳发布'
                        before = relay.read_peers(controller.node.queue.peers_dir)['pc-a'].heartbeatAt
                        assert _wait_until(lambda: relay.read_peers(controller.node.queue.peers_dir)['pc-a'].heartbeatAt != before, 3), \
                            '兼容JSON或快照清理阻塞导致追加心跳停止'
                        assert not compat_release.is_set()
                finally:
                    compat_release.set()
                print('  [PASS] 兼容JSON发布及快照清理挂起时追加心跳仍更新')
                # 共享目录发现被挂起时，首次终端请求仍须走独立控制循环。
                discovery_entered = threading.Event()
                discovery_release = threading.Event()
                slave_node = harness.slave.node
                original_discovery = slave_node._read_shared_peers
                def slow_discovery():
                    discovery_entered.set()
                    discovery_release.wait(5)
                    return original_discovery()
                try:
                    with patch.object(slave_node, '_read_shared_peers', slow_discovery):
                        assert discovery_entered.wait(3), '未进入慢对端扫描'
                        started = time.monotonic()
                        r = controller.node.open_remote_session('pc-b', harness.slave.node.local_devices()[0]['key'],
                                                                'latency-first-session', timeout=2)
                        assert r.ok, r.error
                        assert time.monotonic() - started < 1.5, '首次终端开启被对端扫描阻塞'
                        assert not discovery_release.is_set()
                finally:
                    discovery_release.set()
                print('  [PASS] 慢共享目录发现期间首个终端请求仍能及时开启')
                # 复现上传被旧发现快照误判离线：慢扫描超过3秒离线阈值，独立读端仍观察序号。
                master_scan_entered = threading.Event()
                master_scan_release = threading.Event()
                original_master_scan = controller.node._read_shared_peers
                def stalled_master_scan():
                    master_scan_entered.set()
                    master_scan_release.wait(15)
                    return original_master_scan()
                try:
                    with patch.object(controller.node, '_read_shared_peers', stalled_master_scan):
                        assert master_scan_entered.wait(3)
                        sequence_before = controller.node._peers['pc-b'].heartbeatSeq
                        time.sleep(9)
                        assert not master_scan_release.is_set()
                        assert controller.node._peers['pc-b'].heartbeatSeq > sequence_before
                        assert controller.node._peer_offline_reason('pc-b') == '', '上传前检查仍使用冻结的发现快照'
                        assert controller.node.connection_info('pc-b')['connectionState'] == 'connected'
                finally:
                    master_scan_release.set()
                print('  [PASS] 主机发现扫描冻结9秒时，独立心跳仍推进，界面与上传前检查均保持在线')
                # 重放SMB目录与新文件负缓存：JSON不可见时追加通知仍能往返。
                original_read_json = relay.read_json
                def hidden_control_json(path, *args, **kwargs):
                    text = str(path)
                    if ('requests' in text or 'results' in text) and text.endswith('.json'):
                        return None
                    return original_read_json(path, *args, **kwargs)
                with patch.object(relay, 'list_messages', return_value=[]), \
                     patch.object(relay, 'read_json', side_effect=hidden_control_json):
                    started = time.monotonic()
                    r = controller.remote_list_dir(key, '/data/fast-notification')
                    assert r['success'], r
                    assert time.monotonic() - started < 1.5, '新文件缓存仍阻塞目录列表'
                print('  [PASS] 请求目录及独立JSON全部不可见时，追加通知仍能及时返回目录列表')
                # 通用执行线程全部繁忙时，终端仍有独立的准入和开启通道。
                busy_release = threading.Event()
                busy_entered = [threading.Event() for _ in range(slave_node._pool._max_workers)]
                def occupy_worker(event):
                    event.set()
                    busy_release.wait(5)
                try:
                    for event in busy_entered:
                        slave_node._pool.submit(occupy_worker, event)
                    assert all(event.wait(2) for event in busy_entered)
                    started = time.monotonic()
                    r = controller.node.open_remote_session('pc-b', slave_node.local_devices()[0]['key'],
                                                            'latency-busy-session', timeout=2)
                    assert r.ok, r.error
                    assert time.monotonic() - started < 1.5, '终端开启被通用执行线程阻塞'
                    assert 'prepareMs' in r.extra
                finally:
                    busy_release.set()
                print('  [PASS] 通用执行线程全部繁忙时终端仍能及时开启，并返回准备耗时')
                archive_entered = threading.Event()
                archive_release = threading.Event()
                original_archive = relay.RelayQueue.archive_file
                def slow_archive(queue, path):
                    if str(path).startswith(queue.results_dir):
                        archive_entered.set()
                        archive_release.wait(5)
                    return original_archive(queue, path)
                try:
                    with patch.object(relay.RelayQueue, 'archive_file', slow_archive):
                        started = time.monotonic()
                        r = controller.node.open_remote_session('pc-b', slave_node.local_devices()[0]['key'],
                                                                'latency-archive-session', timeout=2)
                        assert r.ok, r.error
                        assert time.monotonic() - started < 1.5, '结果归档阻塞终端就绪'
                        assert not archive_entered.wait(1), '新日志通道不应逐回执归档'
                finally:
                    archive_release.set()
                print('  [PASS] 新日志通道不逐回执归档，终端开启立即返回')
                entered = threading.Event()
                def slow_devices(adb_path, **kwargs):
                    if adb_path == 'node-b':
                        entered.set()
                        release.wait(5)
                    return _fake_list_devices(adb_path, **kwargs)
                with patch.object(relay_node, 'list_devices', slow_devices):
                    assert entered.wait(3), '未进入慢枚举'
                    started = time.monotonic()
                    r = controller.remote_list_dir(key, '/data/空 格')
                    assert r['success'], r
                    assert time.monotonic() - started < 1.5, '目录请求被设备枚举阻塞'
                    assert "ls -1F '/data/空 格'" in r['output'], r
                    assert not release.is_set()
                    release.set()
                print('  [PASS] 慢 adb devices 挂起时目录请求仍能完成，空格路径正确引用')
                # 同时浏览只请求一次；再次浏览走短缓存；重启和撤销不复用旧结果。
                calls = []
                entered.clear()
                release.clear()
                def slow_exec(*args, **kwargs):
                    calls.append(args)
                    entered.set()
                    release.wait(3)
                    return {'success': True, 'output': 'cached.txt'}
                with patch.object(controller, 'remote_exec', slow_exec), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(controller.remote_list_dir, key, '/sdcard')
                    assert entered.wait(1)
                    b = pool.submit(controller.remote_list_dir, key, '/sdcard')
                    release.set()
                    assert a.result(2)['success'] and b.result(2)['success']
                    assert controller.remote_list_dir(key, '/sdcard')['success']
                    assert len(calls) == 1, calls
                    controller.node._peers['pc-b'].startedAt = '2027-01-01T00:00:00+00:00'
                    controller.remote_list_dir(key, '/sdcard')
                    assert len(calls) == 2
                    controller.node.unpair_peer(harness.slave.config.nodeId)
                    controller.remote_list_dir(key, '/sdcard')
                    assert len(calls) == 3
                print('  [PASS] 重复浏览只读一次；对端重启和撤销后旧缓存失效')
            finally:
                release.set()
                # stop 不等待归档线程；临时共享目录必须等本测试的写入者退出后才能回收。
                nodes = [getattr(harness.master, 'node', None), getattr(harness.slave, 'node', None)]
                nodes.extend([locals().get('master_node'), locals().get('slave_node')])
                archive_pools = [getattr(node, '_archive_pool', None) for node in nodes if node is not None]
                harness.stop()
                archive_threads = {thread for pool in archive_pools if pool is not None
                                   for thread in getattr(pool, '_threads', ())}
                for thread in archive_threads:
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise RuntimeError('测试归档线程未停止，不能清理临时共享目录')
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False
    finally:
        release.set()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
