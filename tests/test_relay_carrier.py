"""验证跨机中转的载体抽象层、按对端分区与主从角色仲裁。

全部在本地临时目录中运行，不依赖第二台机器、真实共享盘、网络或 ADB 设备，
也不启动线程或服务。以太网直连 / 串口直连在本阶段只验证「接口已就位、未实现
的载体被明确拒绝」，不验证链路本身。
"""
import os
import sys
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay          # noqa: E402
import relay_carrier  # noqa: E402


def _carrier(root: str, peer_id: str = 'pc-a') -> relay_carrier.SharedFolderCarrier:
    return relay_carrier.SharedFolderCarrier(peer_id, os.path.join(root, 'share'))


def _check_specs(root: str) -> list:
    specs = relay_carrier.carrier_specs()
    shared = specs.get(relay_carrier.CARRIER_SHARED_FOLDER)
    tcp = specs.get(relay_carrier.CARRIER_TCP)
    serial = specs.get(relay_carrier.CARRIER_SERIAL)
    listed = relay_carrier.describe_carriers()

    made_tcp, tcp_reason = None, ''
    try:
        relay_carrier.create_carrier(relay_carrier.CARRIER_TCP, 'pc-a', {'host': '10.0.0.2'})
    except relay_carrier.CarrierUnsupported as exc:
        made_tcp, tcp_reason = False, str(exc)

    unknown, unknown_reason = None, ''
    try:
        relay_carrier.create_carrier('carrier-pigeon', 'pc-a', {})
    except relay_carrier.CarrierUnsupported as exc:
        unknown, unknown_reason = False, str(exc)

    created = relay_carrier.create_carrier(
        relay_carrier.CARRIER_SHARED_FOLDER, 'pc-a', {'root': os.path.join(root, 'share')},
    )

    return [
        ('三种载体都在清单里',
         set(specs) == {relay_carrier.CARRIER_SHARED_FOLDER, relay_carrier.CARRIER_TCP,
                        relay_carrier.CARRIER_SERIAL}, str(sorted(specs))),
        ('共享文件夹标记为已实现',
         shared is not None and shared.implemented and shared.durable_messages is True, ''),
        ('以太网/串口标记为未实现且给出计划说明',
         tcp is not None and serial is not None
         and not tcp.implemented and not serial.implemented
         and bool(tcp.notes) and bool(serial.notes), ''),
        ('串口带宽标记为 low 并限制单条消息大小',
         serial is not None and serial.throughput == 'low'
         and serial.max_message_bytes < tcp.max_message_bytes, ''),
        ('总线型与点对点型拓扑区分清楚',
         shared.topology == relay_carrier.TOPO_BUS
         and tcp.topology == relay_carrier.TOPO_P2P, ''),
        ('创建未实现的载体被明确拒绝并说明原因',
         made_tcp is False and '尚未实现' in tcp_reason, tcp_reason),
        ('创建未知载体被明确拒绝',
         unknown is False and '未知的载体类型' in unknown_reason, unknown_reason),
        ('按配置创建共享文件夹载体成功',
         isinstance(created, relay_carrier.SharedFolderCarrier), type(created).__name__),
        ('设置页清单带配置项字段',
         all('optionFields' in item and item['optionFields'] for item in listed), str(len(listed))),
    ]


def _check_slot_validation(root: str) -> list:
    """槽名与键名最终会变成目录名/文件名，非法名必须是响亮的错误。

    这里刻意要求**抛异常而不是返回 False**：名字由本机代码构造，非法即是调用
    方 bug；如果退化成 False，就会被当成「一次普通的写失败」而被上层重试掩盖。
    """
    carrier = _carrier(root, 'pc-a')
    carrier.open()
    rejections = []
    for slot, key in (
        ('../escape', 'a.json'),
        ('requests/../../etc', 'a.json'),
        ('', 'a.json'),
        ('requests/pc-b', '../../evil.json'),
        ('requests/pc-b', ''),
        ('requests/pc-b', 'sub/dir.json'),
        ('requests/pc-b', '..'),
        ('requests/pc-b', '.hidden.json'),
    ):
        try:
            carrier.put_message(slot, key, b'{}')
        except relay_carrier.CarrierError:
            rejections.append(True)
        else:
            rejections.append(False)

    def raises(call):
        try:
            call()
        except relay_carrier.CarrierError:
            return True
        return False

    slot_path = os.path.join(root, 'share')
    return [
        ('非法槽名与键名一律拒绝（防目录穿越）', all(rejections), str(rejections)),
        ('读消息时非法槽名同样抛错',
         raises(lambda: carrier.read_message('~/root', 'a.json')), ''),
        ('枚举时非法槽名同样抛错',
         raises(lambda: carrier.list_keys('requests/pc-b/../..')), ''),
        ('删除时非法键名同样抛错',
         raises(lambda: carrier.drop_message('requests/pc-b', '..')), ''),
        ('流名非法同样抛错',
         raises(lambda: carrier.append_stream('../escape.out', b'x'))
         and raises(lambda: carrier.read_stream('a/b')), ''),
        ('合法槽名与键名正常通过',
         carrier.put_message('requests/pc-b', 'r-1.json', b'{}') is True, ''),
        ('拒绝后没有在共享目录外留下文件',
         not os.path.exists(os.path.join(root, 'escape'))
         and not os.path.exists(os.path.join(os.path.dirname(slot_path), 'escape')), ''),
    ]


def _check_peer_id_guard(root: str) -> list:
    """peerId 会拼成目录名，而它来自共享目录里的内容，必须在入口处校验。"""
    valid = ['pc-a', 'PC-B', 'pc_b', 'node1', 'a' * 41, 'a.b-c_d']
    invalid = ['', '..', '../etc', 'a/b', 'a\\b', '-lead', '.hidden', 'x' * 42, 'trail.', None,
               'with space', '中文']

    peers_dir = os.path.join(root, 'peer-guard', 'peers')
    os.makedirs(peers_dir, exist_ok=True)
    relay.write_heartbeat(peers_dir, relay.PeerHeartbeat(
        peerId='pc-a', role=relay.ROLE_MASTER, heartbeatAt=relay.now_iso(),
    ))
    # 伪造一个 peerId 里带目录穿越的心跳文件
    relay.atomic_write_json(os.path.join(peers_dir, 'evil.json'), {
        'peerId': '../../etc', 'role': relay.ROLE_MASTER, 'heartbeatAt': relay.now_iso(),
    })
    relay.atomic_write_json(os.path.join(peers_dir, 'evil2.json'), {
        'peerId': 'a/b', 'role': relay.ROLE_MASTER, 'heartbeatAt': relay.now_iso(),
    })
    seen = relay.read_peers(peers_dir)

    def raises(call):
        try:
            call()
        except ValueError:
            return True
        return False

    return [
        ('合法 peerId 全部通过', all(relay.valid_peer_id(item) for item in valid), str(valid)),
        ('非法 peerId 全部拒绝', not any(relay.valid_peer_id(item) for item in invalid), str(invalid)),
        ('心跳读取时过滤非法 peerId',
         list(seen) == ['pc-a'], str(list(seen))),
        ('写入心跳时非法 peerId 抛 ValueError',
         raises(lambda: relay.write_heartbeat(peers_dir, relay.PeerHeartbeat(peerId='../x', role='slave'))), ''),
        ('取对端分区时非法 peerId 抛 ValueError',
         raises(lambda: relay.RelayQueue.for_peer(root, '../x'))
         and raises(lambda: relay.RelayQueue.for_peer(root, 'a/b')), ''),
        ('合法 peerId 取分区正常',
         relay.RelayQueue.for_peer(root, 'pc-b').requests_dir.endswith(os.path.join('requests', 'pc-b')), ''),
        ('拒绝的 peerId 没有在磁盘上造出目录',
         not os.path.exists(os.path.join(root, 'etc'))
         and not os.path.exists(os.path.join(root, 'b')), ''),
    ]


def _check_shared_folder_messages(root: str) -> list:
    carrier = _carrier(root, 'pc-a')
    carrier.open()
    slot = 'requests/pc-b'
    checks = []

    checks.append(('写入消息成功', carrier.put_message(slot, 'r-1.json', b'{"i":1}') is True, ''))
    checks.append(('读回消息内容一致',
                   carrier.read_message(slot, 'r-1.json') == b'{"i":1}', ''))
    carrier.put_message(slot, 'r-2.json', b'{"i":2}')
    carrier.put_message(slot, 'other.txt', b'x')
    checks.append(('按前缀枚举且忽略其他后缀',
                   carrier.list_keys(slot, prefix='r-', suffix='.json') == ['r-1.json', 'r-2.json'],
                   str(carrier.list_keys(slot, prefix='r-', suffix='.json'))))

    # 临时文件与云盘冲突副本不是可消费消息
    for name in ('r-3.json.tmp-abcd', 'r-4.json (1)'):
        with open(os.path.join(carrier.slot_path(slot), name), 'w', encoding='utf-8') as handle:
            handle.write('{}')
    checks.append(('临时文件与冲突副本不计入枚举',
                   carrier.list_keys(slot, prefix='r-') == ['r-1.json', 'r-2.json'],
                   str(carrier.list_keys(slot, prefix='r-'))))

    moved = carrier.move_message(slot, 'r-1.json', 'archive/pc-b/2026-09')
    checks.append(('归档移动落地到目标槽',
                   bool(moved) and os.path.isfile(moved)
                   and carrier.read_message(slot, 'r-1.json') is None, str(moved)))
    carrier.put_message(slot, 'r-1.json', b'{"i":1}')
    twice = carrier.move_message(slot, 'r-1.json', 'archive/pc-b/2026-09')
    checks.append(('归档重名不覆盖而是改名',
                   bool(twice) and twice != moved and os.path.isfile(moved), str(twice)))
    checks.append(('删除消息成功', carrier.drop_message(slot, 'r-2.json') is True
                   and carrier.read_message(slot, 'r-2.json') is None, ''))
    checks.append(('删除不存在的消息返回 False',
                   carrier.drop_message(slot, 'nope.json') is False, ''))
    checks.append(('读取不存在的消息返回 None',
                   carrier.read_message(slot, 'nope.json') is None, ''))
    return checks


def _check_stream_carrier(root: str) -> list:
    carrier = _carrier(root, 'pc-a')
    carrier.open()
    checks = [('流尚未创建时读为空', carrier.read_stream('sess.out') == (b'', False), '')]

    carrier.append_stream('sess.out', b'hello ')
    carrier.append_stream('sess.out', '中文'.encode('utf-8'))
    expected = b'hello ' + '中文'.encode('utf-8')
    data, rebuilt = carrier.read_stream('sess.out')
    checks.append(('增量读拿到全部新增字节', data == expected and rebuilt is False, repr(data)))
    checks.append(('无新增时读为空', carrier.read_stream('sess.out') == (b'', False), ''))

    carrier.append_stream('sess.out', b'again')
    checks.append(('第二次增量读只取新字节', carrier.read_stream('sess.out')[0] == b'again', ''))

    with open(carrier.stream_path('sess.out'), 'wb') as handle:
        handle.write(b'new')
    data, rebuilt = carrier.read_stream('sess.out')
    checks.append(('文件重建后游标归零并重读', data == b'new' and rebuilt is True, repr(data)))

    carrier.reset_stream('sess.out')
    checks.append(('重置游标后从头读', carrier.read_stream('sess.out')[0] == b'new', ''))
    checks.append(('空数据不触发写入',
                   carrier.append_stream('sess.out', b'') is True, ''))

    carrier.open()
    checks.append(('同一实例上输入输出流互不干扰',
                   carrier.append_stream('sess.in', b'in') is True
                   and carrier.read_stream('sess.out')[0] == b'', ''))
    return checks


def _check_open_close(root: str) -> list:
    carrier = _carrier(root, 'pc-a')
    opened = carrier.open()
    status = carrier.status()
    layout_ok = all(os.path.isdir(carrier.slot_path(name)) for name in carrier.LAYOUT_DIRS)

    blocker = os.path.join(root, 'blocker')
    with open(blocker, 'w', encoding='utf-8') as handle:
        handle.write('x')
    broken = relay_carrier.SharedFolderCarrier('pc-a', os.path.join(blocker, 'nested'))
    broken_open = broken.open()
    broken_status = broken.status()

    bare = relay_carrier.SharedFolderCarrier('pc-a', '')
    bare_open = bare.open()

    # 空 root 绝不能退化成当前工作目录：否则「未配置」会变成「配置为程序目录」，
    # 并在里面建出整套中转布局。切到沙箱目录里验证，确保不污染真实工作区。
    sandbox = os.path.join(root, 'cwd-sandbox')
    os.makedirs(sandbox, exist_ok=True)
    previous = os.getcwd()
    os.chdir(sandbox)
    try:
        polluted_carrier = relay_carrier.SharedFolderCarrier('pc-a', '   ')
        polluted_open = polluted_carrier.open()
        polluted = sorted(os.listdir(sandbox))
    finally:
        os.chdir(previous)

    carrier.close()
    return [
        ('打开共享目录载体成功', opened is True, str(status)),
        ('打开时建立完整布局', layout_ok, str(carrier.LAYOUT_DIRS)),
        ('状态含中文状态文案与载体标签',
         status.get('stateText') == '已连接' and status.get('kindLabel') == '共享文件夹', str(status)),
        ('探针测出往返延迟', isinstance(status.get('latencyMs'), (int, float))
         and status['latencyMs'] >= 0, str(status.get('latencyMs'))),
        ('探针文件不残留', not os.path.exists(os.path.join(carrier.root, 'probe', 'pc-a.probe')), ''),
        ('不可用目录判为连接断开并带上原因',
         broken_open is False and broken_status.get('state') == 'disconnected'
         and bool(broken_status.get('lastError')), str(broken_status.get('lastError'))),
        ('未配置目录时判为未启用',
         bare_open is False and bare.status().get('state') == 'unconfigured', str(bare.status())),
        ('空 root 不落成当前工作目录',
         bare.root == '' and polluted_open is False and polluted == [],
         f'root={bare.root!r} polluted={polluted}'),
        ('关闭后可重复调用且状态转为断开',
         carrier.close() is None and carrier.status().get('state') == 'disconnected', ''),
    ]


def _check_manifest(root: str) -> list:
    queue = relay.RelayQueue(os.path.join(root, 'manifest'))
    queue.ensure_layout()
    manifest = relay_carrier.ensure_manifest(queue, relay_carrier.CARRIER_SHARED_FOLDER)
    ok, reason = relay_carrier.check_manifest(manifest)

    future = {**manifest, 'requiredFeatures': list(relay.BASE_FEATURES) + ['blobs']}
    future_ok, future_reason = relay_carrier.check_manifest(future)
    higher_layout = {**manifest, 'layoutVersion': relay.LAYOUT_VERSION + 1}
    layout_ok, layout_reason = relay_carrier.check_manifest(higher_layout)
    higher_schema = {**manifest, 'schema': relay.RELAY_SCHEMA + 1}
    schema_ok, schema_reason = relay_carrier.check_manifest(higher_schema)
    missing_ok, missing_reason = relay_carrier.check_manifest(None)

    again = relay_carrier.ensure_manifest(queue, relay_carrier.CARRIER_SHARED_FOLDER)
    return [
        ('清单写入布局版本、载体类型与基础特性',
         manifest.get('layoutVersion') == relay.LAYOUT_VERSION
         and manifest.get('carrierKind') == relay_carrier.CARRIER_SHARED_FOLDER
         and list(manifest.get('requiredFeatures')) == list(relay.BASE_FEATURES), str(manifest)),
        ('本版本自己的清单可处理', ok is True, reason),
        ('对端要求未实现的特性时拒绝启动',
         future_ok is False and 'blobs' in future_reason, future_reason),
        ('对端布局版本更高时拒绝启动',
         layout_ok is False and '布局版本' in layout_reason, layout_reason),
        ('对端协议版本更高时拒绝启动',
         schema_ok is False and '协议版本' in schema_reason, schema_reason),
        ('清单缺失时拒绝启动',
         missing_ok is False and '清单缺失' in missing_reason, missing_reason),
        ('重复写入清单保持 createdAt 不变',
         again.get('createdAt') == manifest.get('createdAt'), str(again.get('createdAt'))),
    ]


def _check_partition(root: str) -> list:
    """一主多从的核心约束：两个从机的请求/结果/流分区不能串。"""
    base = os.path.join(root, 'partition')
    master_b = relay.RelayQueue.for_peer(base, 'pc-b')
    master_c = relay.RelayQueue.for_peer(base, 'pc-c')
    master_b.ensure_layout()

    req_b = relay.new_request('exec', 'pc-b', {'key': 'b:tid=1'}, {'argv': ['version']})
    req_c = relay.new_request('exec', 'pc-c', {'key': 'c:tid=1'}, {'argv': ['version']})
    master_b.submit(req_b)
    master_c.submit(req_c)

    seen_b = [item.reqId for _p, item in master_b.pending_requests()]
    seen_c = [item.reqId for _p, item in master_c.pending_requests()]

    master_b.write_result(relay.Result(reqId=req_b.reqId, status=relay.STATUS_OK, stdout='b'))
    master_c.write_result(relay.Result(reqId=req_c.reqId, status=relay.STATUS_OK, stdout='c'))

    master_b.stream('sess-b').output_writer().append(b'from-b')
    master_c.stream('sess-c').output_writer().append(b'from-c')

    return [
        ('请求落到各自对端分区',
         master_b.requests_dir.endswith(os.path.join('requests', 'pc-b'))
         and master_c.requests_dir.endswith(os.path.join('requests', 'pc-c')),
         f'{master_b.requests_dir} | {master_c.requests_dir}'),
        ('从机只看得到发给自己的请求',
         seen_b == [req_b.reqId] and seen_c == [req_c.reqId], f'{seen_b} / {seen_c}'),
        ('结果分区互相隔离',
         master_b.read_result(req_b.reqId).stdout == 'b'
         and master_b.read_result(req_c.reqId) is None
         and master_c.read_result(req_c.reqId).stdout == 'c', ''),
        ('流分区互相隔离',
         master_b.stream('sess-b').output_tail().read_new() == b'from-b'
         and master_c.stream('sess-c').output_tail().read_new() == b'from-c', ''),
        ('心跳仍在共享总线目录',
         master_b.peers_dir == master_c.peers_dir
         and master_b.peers_dir.endswith('peers'), master_b.peers_dir),
        ('可枚举出现过的对端分区',
         set(master_b.peer_ids()) >= {'pc-b', 'pc-c'}, str(master_b.peer_ids())),
    ]


def _check_roles(root: str) -> list:
    now = time.time()
    beat = lambda role: relay.PeerHeartbeat(  # noqa: E731
        peerId='x', role=role, heartbeatAt=relay.now_iso(),
    )
    peers_single = {'pc-b': beat(relay.ROLE_SLAVE)}
    peers_multi = {
        'pc-y': beat(relay.ROLE_MASTER),
        'pc-x': beat(relay.ROLE_MASTER),
        'pc-b': beat(relay.ROLE_SLAVE),
    }
    offline = {
        'pc-z': relay.PeerHeartbeat(
            peerId='pc-z', role=relay.ROLE_MASTER, heartbeatAt='2020-01-01T00:00:00+00:00',
        ),
    }
    # 退役机器（曾作主控，现已关机）：它的心跳文件会永远留在共享目录里
    retired = {
        'old-pc': relay.PeerHeartbeat(
            peerId='old-pc', role=relay.ROLE_MASTER, heartbeatAt='2020-01-01T00:00:00+00:00',
        ),
    }
    retired_plus_live = {**retired, 'live-pc': beat(relay.ROLE_MASTER)}

    master_side = relay.arbitrate_roles('pc-a', relay.ROLE_MASTER, peers_single, now=now)
    slave_side = relay.arbitrate_roles('pc-b', relay.ROLE_SLAVE, {'pc-a': beat(relay.ROLE_MASTER)}, now=now)
    # 从机视角看到两个主控端；本机也是主控时，冲突名单里连自己一起算
    conflict = relay.arbitrate_roles('pc-b', relay.ROLE_SLAVE, peers_multi, now=now)
    conflict_self = relay.arbitrate_roles('pc-a', relay.ROLE_MASTER, peers_multi, now=now)
    accepted = relay.arbitrate_roles(
        'pc-b', relay.ROLE_SLAVE, {'pc-a': beat(relay.ROLE_MASTER), 'pc-d': beat(relay.ROLE_MASTER)},
        accepted_master_ids=['pc-d', 'pc-a'], now=now,
    )
    no_master = relay.arbitrate_roles('pc-b', relay.ROLE_SLAVE, {'pc-c': beat(relay.ROLE_SLAVE)}, now=now)
    stale_master = relay.arbitrate_roles('pc-b', relay.ROLE_SLAVE, offline, now=now)
    # 退役主控的残留心跳：不得让在跑的机器判成冲突（冲突是 fail-closed，从机会拒掉每个请求）
    retired_only = relay.arbitrate_roles('pc-b', relay.ROLE_MASTER, retired, now=now)
    # 已确认名单的顺序是「最早确认的在前」，旧主控不能因为排在前面就占住主控位
    retired_accepted_first = relay.arbitrate_roles(
        'pc-b', relay.ROLE_SLAVE, retired_plus_live,
        accepted_master_ids=['old-pc', 'live-pc'], now=now,
    )

    return [
        ('主控与从机角色被正确分类',
         master_side.masters == ['pc-a'] and master_side.slaves == ['pc-b'], str(master_side.to_dict())),
        ('从机选出声明的那个主控',
         slave_side.master == 'pc-a' and slave_side.conflict is False, slave_side.master),
        ('多个主控时判定冲突并按字典序给出建议主控',
         conflict.conflict is True and conflict.masters == ['pc-x', 'pc-y']
         and conflict.suggestedMaster == 'pc-x' and '建议保留 pc-x' in conflict.reason,
         conflict.reason),
        ('本机也是主控时冲突名单包含自己且不自动降级',
         conflict_self.conflict is True and conflict_self.masters == ['pc-a', 'pc-x', 'pc-y']
         and conflict_self.selfRole == relay.ROLE_MASTER, str(conflict_self.masters)),
        ('已确认名单优先于字典序',
         accepted.master == 'pc-d', accepted.master),
        ('无主控时 master 为空', no_master.master == '' and no_master.reason == '未发现任何主控端',
         no_master.reason),
        ('离线主控不计入在线主控',
         stale_master.onlineMasters == [] and stale_master.masters == ['pc-z'], str(stale_master.onlineMasters)),
        ('退役主控的残留心跳不导致冲突，在跑的机器照常当主控',
         retired_only.conflict is False and retired_only.staleMasters == ['old-pc']
         and retired_only.onlineMasters == ['pc-b'] and retired_only.master == 'pc-b'
         and retired_only.suggestedMaster == 'pc-b', retired_only.reason),
        ('建议主控绝不会指向一台已离线的机器',
         stale_master.suggestedMaster == '', repr(stale_master.suggestedMaster)),
        ('只剩离线主控时结论是「未发现在线的主控端」而不是「未发现任何主控端」',
         stale_master.reason == '未发现在线的主控端' and stale_master.master == '',
         stale_master.reason),
        ('已确认名单里的退役主控不占主控位（名单顺序是最早确认在前）',
         retired_accepted_first.master == 'live-pc' and retired_accepted_first.conflict is False
         and retired_accepted_first.staleMasters == ['old-pc'], retired_accepted_first.reason),
        ('角色归一化默认落到从机（fail-closed）',
         relay.normalize_role('') == relay.ROLE_SLAVE
         and relay.normalize_role('Master') == relay.ROLE_MASTER
         and relay.normalize_role('乱写') == relay.ROLE_SLAVE, ''),
        ('角色中文标签可读',
         relay.role_label('master') == '主控' and relay.role_label('slave') == '从机', ''),
    ]


def _check_self_ghosts(root: str) -> list:
    """本机自己换标识后留下的旧心跳：既不是对端，也不能参与仲裁。

    现场很常见：在设置里把「本机标识」从 ``host-example`` 改成 ``host-example-b``，
    旧标识的心跳文件按单写者原则谁也不会去删。不识别它，界面就会把「本机自己」
    显示成一台离线对端（角色还是改之前那个）——用户看到的就是「对端标识不对」。
    """
    def beat(pid, fp, started, role, peer_id, at=None):
        return relay.PeerHeartbeat(
            peerId=peer_id, role=role, fingerprint=fp, pid=pid,
            startedAt=started, heartbeatAt=at or relay.now_iso(),
        )

    me_fp, me_pid = 'fp-me', 4242
    old_start, new_start = '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:10+00:00'
    base = {
        # 本机自己上一次的身份：同指纹、同 pid、startedAt 不同
        'host-example': beat(me_pid, me_fp, old_start, relay.ROLE_MASTER, 'host-example'),
        # 本机当前身份
        'host-example-b': beat(me_pid, me_fp, new_start, relay.ROLE_SLAVE, 'host-example-b'),
        # 另一台机器上的从机
        'pc-remote': beat(77, 'fp-other', '2026-01-01T00:00:00+00:00', relay.ROLE_SLAVE, 'pc-remote'),
        # 同机**另一个实例**（同指纹、不同 pid）= 真对端，用于同机双实例验证
        'pc-sibling': beat(4243, me_fp, '2026-01-01T00:00:00+00:00', relay.ROLE_SLAVE, 'pc-sibling'),
    }
    kwargs = dict(self_fingerprint=me_fp, self_pid=me_pid, self_started_at=new_start)

    as_slave = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='host-example-b', peers=base, **kwargs)
    as_master = relay.evaluate_health(
        enabled=True, role=relay.ROLE_MASTER, self_id='host-example-b', peers=base, **kwargs)
    # 老调用方不传 self_* 时行为必须和以前一模一样
    legacy = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='host-example-b', peers=base)

    ghost_file_dir = os.path.join(root, 'ghosts', 'peers')
    os.makedirs(ghost_file_dir, exist_ok=True)
    for item in base.values():
        relay.write_heartbeat(ghost_file_dir, item)
    purged = relay.purge_self_heartbeats(
        ghost_file_dir, fingerprint=me_fp, pid=me_pid,
        started_at=new_start, keep_peer_id='host-example-b',
    )
    left = sorted(relay.read_peers(ghost_file_dir))

    # 「改完标识后整个应用重启过」：旧标识的心跳是**上一个进程**写的，pid 已经对不上，
    # 判据只能落在时间上 —— 它在本机这次启动之前就停了。真对端只要还在跑就会持续
    # 心跳，心跳时刻必然晚于本机启动，所以这条不会误伤在跑的同机兄弟。
    restart = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='host-example-b',
        peers={
            'prev-proc': beat(9999, me_fp, '2025-12-01T00:00:00+00:00', relay.ROLE_MASTER,
                              'prev-proc', at='2025-12-01T00:00:05+00:00'),
            'live-sibling': beat(4243, me_fp, '2025-12-01T00:00:00+00:00', relay.ROLE_SLAVE,
                                 'live-sibling', at='2026-06-01T00:00:00+00:00'),
        },
        **kwargs,
    )

    # 同机多开：**先起**的实例，心跳早于本机启动、但仍在跳 —— 它是活着的兄弟，不是
    # 「我放弃的旧身份」。只按时间判会把它吞掉（表现：后起的实例在头几秒看不见先起
    # 的那个）。用显式 now 把「早于启动」和「还在跳」两件事摆到同一时刻上 —— 不传
    # now 的话，构造出来的时间戳相对真实时钟全是陈旧的，测不出「新鲜」这一维。
    t0 = relay._epoch_ms('2026-03-01T00:00:00+00:00') / 1000.0
    early_alive = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='host-example-b',
        peers={
            # 先起的兄弟：比本机启动早 5 秒，刚跳过一拍（到现在只过了 2 秒 ⇒ 新鲜）
            'pc-early': beat(4243, me_fp, '2026-02-28T23:00:00+00:00', relay.ROLE_SLAVE,
                             'pc-early', at='2026-03-01T00:00:00+00:00'),
            # 已停止的旧身份：停在本机启动之前很久（已过期 ⇒ 仍旧是幽灵）
            'pc-dead': beat(4244, me_fp, '2026-02-28T23:00:00+00:00', relay.ROLE_SLAVE,
                            'pc-dead', at='2026-02-28T23:59:00+00:00'),
        },
        self_fingerprint=me_fp, self_pid=me_pid,
        self_started_at='2026-03-01T00:00:05+00:00',
        now=t0 + 2,
        interval_s=2.0,
    )

    # 清理的边界：上一个进程的残留**不**删（本机不是它的写者），只在显示层忽略。
    cross_dir = os.path.join(root, 'cross-purge', 'peers')
    os.makedirs(cross_dir, exist_ok=True)
    relay.write_heartbeat(cross_dir, beat(
        9999, me_fp, '2025-12-01T00:00:00+00:00', relay.ROLE_MASTER, 'prev-proc',
        at='2025-12-01T00:00:05+00:00'))
    cross_purged = relay.purge_self_heartbeats(
        cross_dir, fingerprint=me_fp, pid=me_pid,
        started_at=new_start, keep_peer_id='host-example-b',
    )
    cross_left = sorted(relay.read_peers(cross_dir))

    return [
        ('本机旧标识的心跳不算对端（自己绝不显示成一台对端）',
         [v.peerId for v in as_slave.peers] == ['pc-remote', 'pc-sibling'],
         str([v.peerId for v in as_slave.peers])),
        ('本机旧标识单独报出来，供界面解释「这条被忽略了」',
         as_slave.selfGhosts == ['host-example'] and '旧标识' in as_slave.hint,
         f'{as_slave.selfGhosts} / {as_slave.hint}'),
        ('同机另一个实例（同指纹不同 pid）是真对端，不能被吞掉',
         'pc-sibling' in [v.peerId for v in as_slave.peers], str([v.peerId for v in as_slave.peers])),
        ('旧身份不参与仲裁：不会被当成「待确认的主控端」',
         as_slave.state == relay.STATE_WAITING_PEER and as_slave.onlineMasters == []
         and as_slave.masters == [], f'{as_slave.state} {as_slave.masters}'),
        ('旧身份不参与仲裁：不会把在跑的主控判成角色冲突',
         as_master.conflict is False and as_master.masters == ['host-example-b']
         and as_master.state == relay.STATE_CONNECTED,
         f'conflict={as_master.conflict} masters={as_master.masters} state={as_master.state}'),
        ('不传 self_* 的旧调用方行为不变（默认不识别任何幽灵）',
         sorted(v.peerId for v in legacy.peers) == ['host-example', 'pc-remote', 'pc-sibling']
         and legacy.selfGhosts == [], str(legacy.selfGhosts)),
        ('启动时清掉自己的旧标识心跳，别人的一个都不动',
         purged == ['host-example'] and left == ['host-example-b', 'pc-remote', 'pc-sibling'],
         f'purged={purged} left={left}'),
        ('上一个进程留下的残留（pid 已不同、心跳早于本机启动）也算本机自己',
         restart.selfGhosts == ['prev-proc'], str(restart.selfGhosts)),
        ('还在跑的同机兄弟（心跳晚于本机启动）不受影响，仍算真对端',
         [v.peerId for v in restart.peers] == ['live-sibling'],
         str([v.peerId for v in restart.peers])),
        # 反向验证：真正的残留特征是「不再更新」，不是「早于本机启动」。同一时刻下
        # 两条心跳都早于本机启动，只有已停止的那条该被当幽灵。
        ('同机先起、还在跳的兄弟不算幽灵（否则后起实例会看不见它）',
         [v.peerId for v in early_alive.peers] == ['pc-early'],
         f'{early_alive.selfGhosts} / {[v.peerId for v in early_alive.peers]}'),
        ('反向：同机已停止的旧身份仍被认作幽灵、挡在对端列表外',
         early_alive.selfGhosts == ['pc-dead']
         and all(v.peerId != 'pc-dead' for v in early_alive.peers),
         str(early_alive.selfGhosts)),
        ('清理不越界：上一个进程的残留不删（本机不是它的写者，单写者原则）',
         cross_purged == [] and cross_left == ['prev-proc'],
         f'purged={cross_purged} left={cross_left}'),
    ]


def _check_trust(root: str) -> list:
    store = relay.TrustStore(os.path.join(root, 'trust', 'relay-trust.json'))
    fresh = store.check('pc-a', 'fp-a')
    store.confirm('pc-a', fingerprint='fp-a', role=relay.ROLE_SLAVE, note='局域网')
    after = store.check('pc-a', 'fp-a')
    other = store.check('pc-b', 'fp-b')
    drifted = store.check('pc-a', 'fp-evil')

    reloaded = relay.TrustStore(os.path.join(root, 'trust', 'relay-trust.json'))
    persisted = reloaded.get('pc-a')
    revoked = store.revoke('pc-a')
    after_revoke = store.check('pc-a', 'fp-a')
    missing_revoke = store.revoke('never-seen')

    records = reloaded.items()
    return [
        ('未确认的对端默认不可信', fresh[0] is False and '首次连接' in fresh[1], fresh[1]),
        ('确认后对端变为可信', after[0] is True, after[1]),
        ('未确认的其他对端仍不可信', other[0] is False, other[1]),
        ('指纹变化时要求重新确认', drifted[0] is False and '指纹' in drifted[1], drifted[1]),
        ('确认记录持久化并可读回',
         persisted is not None and persisted.fingerprint == 'fp-a'
         and persisted.role == relay.ROLE_SLAVE, str(persisted)),
        ('可列出全部确认记录', len(records) == 1, str(len(records))),
        ('撤销后重新变为不可信', revoked is True and after_revoke[0] is False, ''),
        ('撤销不存在的对端返回 False', missing_revoke is False, ''),
    ]


def _check_health(root: str) -> list:
    now = time.time()
    peers = {
        'pc-a': relay.PeerHeartbeat(peerId='pc-a', role=relay.ROLE_MASTER, heartbeatAt=relay.now_iso()),
        'pc-b': relay.PeerHeartbeat(
            peerId='pc-b', role=relay.ROLE_SLAVE, heartbeatAt=relay.now_iso(),
            devices=[{'key': 'b1:tid=1'}, {'key': 'b2:tid=2'}], carrier='shared-folder', host='lab-1',
        ),
        'pc-c': relay.PeerHeartbeat(peerId='pc-c', role=relay.ROLE_SLAVE, heartbeatAt=relay.now_iso()),
    }
    link_ok = relay_carrier.make_link_status(relay.STATE_CONNECTED, kind='shared-folder', latency_ms=3.5)
    link_bad = relay_carrier.make_link_status(
        relay.STATE_DISCONNECTED, kind='shared-folder', last_error='共享不可达',
    )

    disabled = relay.evaluate_health(enabled=False, role='master', self_id='pc-a', peers=peers, now=now)
    master_view = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=peers, link=link_ok, now=now,
        carrier_kind='shared-folder',
    )
    broken = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=peers, link=link_bad, now=now,
    )
    slave_wait = relay.evaluate_health(
        enabled=True, role='slave', self_id='pc-b', peers={'pc-c': peers['pc-c']},
        link=link_ok, now=now,
    )
    slave_pending = relay.evaluate_health(
        enabled=True, role='slave', self_id='pc-b', peers=peers, link=link_ok, now=now,
    )
    slave_ok = relay.evaluate_health(
        enabled=True, role='slave', self_id='pc-b', peers=peers, link=link_ok, now=now,
        accepted_master_ids=['pc-a'], local_device_count=2,
    )
    conflict_peers = {
        'pc-a': relay.PeerHeartbeat(peerId='pc-a', role=relay.ROLE_MASTER, heartbeatAt=relay.now_iso()),
        'pc-d': relay.PeerHeartbeat(peerId='pc-d', role=relay.ROLE_MASTER, heartbeatAt=relay.now_iso()),
    }
    conflict = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=conflict_peers, link=link_ok, now=now,
    )

    trust = relay.TrustStore(os.path.join(root, 'health-trust.json'))
    guarded = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=peers, link=link_ok,
        trust=trust, now=now,
    )
    trust.confirm('pc-b', fingerprint=peers['pc-b'].fingerprint)
    guarded_half = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=peers, link=link_ok,
        trust=trust, now=now,
    )
    trust.confirm('pc-c', fingerprint=peers['pc-c'].fingerprint)
    guarded_ok = relay.evaluate_health(
        enabled=True, role='master', self_id='pc-a', peers=peers, link=link_ok,
        trust=trust, now=now,
    )

    payload = master_view.to_dict()
    return [
        ('未启用时状态为未启用并给出启用提示',
         disabled.state == relay.STATE_UNCONFIGURED
         and disabled.stateText == '未启用' and '设置页' in disabled.hint, disabled.hint),
        ('主控端状态含中文角色标签与设备总数',
         master_view.to_dict()['roleLabel'] == '主控'
         and master_view.state == relay.STATE_CONNECTED
         and master_view.deviceCount == 2 and '2 个从机' in master_view.hint, master_view.hint),
        ('载体不可用时状态为断开并带上原因',
         broken.state == relay.STATE_DISCONNECTED and '共享不可达' in broken.hint, broken.hint),
        ('从机无主控时状态为等待对端',
         slave_wait.state == relay.STATE_WAITING_PEER and '未发现在线的主控端' in slave_wait.hint,
         slave_wait.hint),
        ('从机主控未确认时状态为等待确认',
         slave_pending.state == relay.STATE_PENDING_CONFIRM and '待确认' in slave_pending.hint,
         slave_pending.hint),
        ('从机确认后状态为已连接并写明受谁控制',
         slave_ok.state == relay.STATE_CONNECTED
         and 'pc-a' in slave_ok.hint and '2 台设备已对外共享' in slave_ok.hint, slave_ok.hint),
        ('多主控时状态为角色冲突并暂停透传',
         conflict.state == relay.STATE_ROLE_CONFLICT
         and conflict.stateText == '角色冲突' and '暂停' in conflict.hint, conflict.hint),
        ('未确认对端全部计入待确认并阻止主控进入已连接',
         guarded.pendingConfirmation == ['pc-b', 'pc-c']
         and guarded.state == relay.STATE_PENDING_CONFIRM, str(guarded.pendingConfirmation)),
        ('只确认一部分时仍为待确认',
         guarded_half.pendingConfirmation == ['pc-c']
         and guarded_half.state == relay.STATE_PENDING_CONFIRM, guarded_half.hint),
        ('全部确认后主控恢复已连接',
         guarded_ok.state == relay.STATE_CONNECTED
         and guarded_ok.pendingConfirmation == [], guarded_ok.hint),
        ('对端视图含在线状态、设备数与指纹',
         any(v['peerId'] == 'pc-b' and v['online'] and v['deviceCount'] == 2
             and v['roleLabel'] == '从机' and v['host'] == 'lab-1' for v in payload['peers']),
         str(payload['peers'])),
        ('状态快照可整体序列化给前端',
         isinstance(payload, dict) and {'state', 'hint', 'peers', 'link', 'master'} <= set(payload),
         str(sorted(payload))),
    ]


def _check_multipeer_end_to_end(root: str) -> list:
    """一主两从：一次完整往返，并确认从机之间互不可见。"""
    base = os.path.join(root, 'multi-e2e')
    master = relay.RelayQueue.for_peer(base, 'pc-b')
    master.ensure_layout()

    checks = []
    req_ids: dict[str, str] = {}
    for slave_id, argv, stdout in (
        ('pc-b', ['logcat', '-d', '-t', '20'], '34\n'),
        ('pc-c', ['version'], 'Android Debug Bridge version 1.0.41\n'),
    ):
        channel = relay.RelayQueue.for_peer(base, slave_id)
        request = relay.new_request(
            relay.KIND_EXEC, slave_id, {'key': f'{slave_id}:tid=1'}, {'argv': argv}, ttl_s=30,
        )
        req_ids[slave_id] = request.reqId
        channel.submit(request)

        pending = channel.pending_requests()
        checks.append((f'{slave_id} 只收到自己的 1 条请求', len(pending) == 1, str(len(pending))))
        if len(pending) != 1:
            continue
        _path, incoming = pending[0]
        checks.append((f'{slave_id} 收到的请求内容正确',
                       incoming.payload['argv'] == argv and incoming.peer == slave_id, str(incoming.to_dict())))
        checks.append((f'{slave_id} 按白名单放行',
                       relay.check_command(incoming.payload['argv'])[0] is True, ''))

        channel.write_result(relay.Result(
            reqId=incoming.reqId, status=relay.STATUS_OK, exitCode=0, stdout=stdout, agentId=slave_id,
        ))
        result = channel.read_result(request.reqId)
        checks.append((f'主控读到 {slave_id} 的结果并知道来自哪台从机',
                       result is not None and result.stdout == stdout and result.agentId == slave_id, ''))
        channel.archive_file(channel.result_path(request.reqId))
        checks.append((f'{slave_id} 归档后活跃目录清空', channel.read_result(request.reqId) is None, ''))

    checks.append(('跨分区读取别人的结果返回 None（不串台）',
                   master.scoped('pc-b').read_result(req_ids['pc-c']) is None
                   and master.scoped('pc-c').read_result(req_ids['pc-b']) is None, ''))

    carrier = relay_carrier.SharedFolderCarrier('pc-a', base)
    carrier.open()
    checks.append(('载体可直接读回分区里的请求文件',
                   carrier.list_keys('requests/pc-b', prefix='r-') == []
                   or carrier.list_keys('requests/pc-b', prefix='r-')[0].endswith('.json'), ''))
    return checks


PROBES = (
    _check_specs,
    _check_slot_validation,
    _check_peer_id_guard,
    _check_shared_folder_messages,
    _check_stream_carrier,
    _check_open_close,
    _check_manifest,
    _check_partition,
    _check_roles,
    _check_self_ghosts,
    _check_trust,
    _check_health,
    _check_multipeer_end_to_end,
)


def run() -> bool:
    checks = []
    tmp = tempfile.mkdtemp(prefix='relay-carrier-test-')
    try:
        for probe in PROBES:
            checks += probe(tmp)
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
