"""池与配对：一个共享目录服务多套部署时的**准入门槛**（模型 A）。

背景见 ``docs/需求与设计/v6-配对池与双端派生路径设计.md`` §9。隔离有三档，本版只做前两档：

    ① 能不能说话 —— 未配对的来源一律拒绝（``_authorize`` + 投递前预检）
    ② 能不能看见 —— 未配对的对端不进对端列表、不进角色仲裁
    ③ 数据是否同目录 —— **不做**（那要给每条配对开一条独立路径，是架构级改动）

每一档都配一条**反向验证**：把门槛关掉之后，同一组数据必须出现「未配对者也可见 /
可被投递」的现象。没有这条，测的就不是门槛 —— 它可能只是恰好没触发。

另外两条容易被漏掉的不变量也在这里钉住：

* ``poolId`` 为空时目录结构与老版本**逐字节一致**（连 ``registry/`` 都不建），
  这是「老部署零迁移」这句承诺的可执行版本；
* ``requiredFeatures`` **只增不减**：声明过的能力不能被另一个实例悄悄降级回去，
  否则「老端读到未知特性会拒绝启动」这条保护会在某次并发启动后消失。
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import relay  # noqa: E402 - 依赖上面的 sys.path
import relay_carrier  # noqa: E402
import relay_node  # noqa: E402

FAKE_DEVICES = [{'serial': 'de47c9b2', 'key': 'de47c9b2:tid=104', 'state': 'device'}]


def _fake_list_devices(adb_path, timeout=8.0):
    return [dict(device) for device in FAKE_DEVICES], ''


class _patch:
    """临时替换 ``relay_node`` 顶层可调用对象（照 test_relay_node 的写法）。"""

    def __init__(self, **targets):
        self._targets = targets
        self._saved: list = []

    def __enter__(self):
        for name, value in self._targets.items():
            self._saved.append((relay_node, name, getattr(relay_node, name)))
            setattr(relay_node, name, value)
        return self

    def __exit__(self, *_exc):
        for owner, attr, original in reversed(self._saved):
            setattr(owner, attr, original)
        self._saved.clear()
        return False


def _write_beat(peers_dir, *, peer_id, role, node_id='', slot=0, host=''):
    """写一份心跳（走真实的写盘路径，含 nodeId/nodeSlot 两个新字段）。"""
    os.makedirs(peers_dir, exist_ok=True)
    relay.write_heartbeat(peers_dir, relay.PeerHeartbeat(
        peerId=peer_id, role=role, appVersion='test', pid=os.getpid(),
        startedAt=relay.now_iso(), heartbeatAt=relay.now_iso(),
        devices=[dict(d) for d in FAKE_DEVICES],
        fingerprint=f'fp-{peer_id}', carrier='shared-folder', linkRttMs=1.0,
        host=host or peer_id, nodeId=node_id, nodeSlot=slot,
    ))
    return relay.peer_heartbeat_path(peers_dir, peer_id)


def _make_node(root, peer_id, role, **kwargs):
    return relay_node.RelayNode(
        peer_id=peer_id,
        role=role,
        root=os.path.join(root, 'share'),
        adb_path=os.path.join(root, 'fake-adb.exe'),
        trust_path=os.path.join(root, f'{peer_id}-trust.json'),
        pairing_path=os.path.join(root, f'{peer_id}-pairing.json'),
        host_label=f'{peer_id}-host',
        host_fingerprint=f'fp-{peer_id}',
        heartbeat_interval_s=0.6,
        poll_interval_s=0.1,
        **kwargs,
    )


# ----------------------------------------------------------------------
def _check_pool_root(base):
    checks: list = []
    share = os.path.join(base, 'share')

    if relay.resolve_pool_root(share, '') == share:
        checks.append(('未填池标识时数据根就是共享目录本身（老部署零迁移）', True, ''))
    else:
        checks.append(('未填池标识时数据根就是共享目录本身（老部署零迁移）', False,
                       relay.resolve_pool_root(share, '')))

    pooled = relay.resolve_pool_root(share, 'team-a')
    expect = os.path.join(share, relay.POOL_DIR, 'team-a')
    if os.path.normcase(pooled) == os.path.normcase(expect):
        checks.append((f'填了池标识后数据根下沉一层：{relay.POOL_DIR}/<池>', True, ''))
    else:
        checks.append(('填了池标识后数据根下沉一层', False, pooled))

    # 池标识是**用户可填**的字符串，会直接变成目录名，必须走和 peerId 同一套校验
    # （拒掉 `..`、绝对路径、盘符等）。否则「池标识」就成了一个路径穿越入口。
    bad_ids = ['..', '../x', 'a/b', 'a\\b', 'C:', '.hidden', 'a b']
    # 注意空串**不在**这张表里：空池标识是合法输入，含义是「不启用池」（见上一条）。
    rejected = []
    for bad in bad_ids:
        try:
            relay.resolve_pool_root(share, bad)
        except ValueError:
            rejected.append(bad)
    if len(rejected) == len(bad_ids):
        checks.append(('非法池标识一律拒绝（不会变成路径穿越）', True, ''))
    else:
        checks.append(('非法池标识一律拒绝（不会变成路径穿越）', False,
                       f'漏放：{[b for b in bad_ids if b not in rejected]}'))
    return checks


def _check_manifest_features(base):
    """清单按需声明池特性；且特性只增不减。"""
    checks: list = []
    root = os.path.join(base, 'share')
    os.makedirs(root, exist_ok=True)
    queue = relay.RelayQueue.for_peer(root, 'pc-a')

    plain = relay_carrier.ensure_manifest(queue, relay_carrier.CARRIER_SHARED_FOLDER)
    if list(plain.get('requiredFeatures') or []) == list(relay.BASE_FEATURES):
        checks.append(('未启用池时清单特性与老版本逐字节一致', True, ''))
    else:
        checks.append(('未启用池时清单特性与老版本逐字节一致', False, plain.get('requiredFeatures')))

    # 启用池：追加 pool-isolation
    pooled = relay_carrier.ensure_manifest(
        queue, relay_carrier.CARRIER_SHARED_FOLDER,
        required_features=relay.BASE_FEATURES + (relay.FEATURE_POOL,),
    )
    if relay.FEATURE_POOL in (pooled.get('requiredFeatures') or []):
        checks.append(('启用池后清单声明 pool-isolation', True, ''))
    else:
        checks.append(('启用池后清单声明 pool-isolation', False, pooled.get('requiredFeatures')))

    # 反向验证：老版本（不认识 pool-isolation）读到它必须拒绝启动，而不是半功能运行
    ok, reason = relay_carrier.check_manifest(pooled, supported=relay.BASE_FEATURES)
    if not ok and relay.FEATURE_POOL in reason:
        checks.append((f'反向验证：老版本读到池特性明确拒绝启动（{reason}）', True, ''))
    else:
        checks.append(('反向验证：老版本读到池特性明确拒绝启动', False, f'{ok} / {reason}'))

    # 只增不减：再以「不带池」的姿态写一次，声明不能被降级回去
    again = relay_carrier.ensure_manifest(queue, relay_carrier.CARRIER_SHARED_FOLDER)
    if relay.FEATURE_POOL in (again.get('requiredFeatures') or []):
        checks.append(('特性只增不减：后启动的实例不会把池声明降级掉', True, ''))
    else:
        checks.append(('特性只增不减：后启动的实例不会把池声明降级掉', False,
                       again.get('requiredFeatures')))
    return checks


def _check_registry_slots(base):
    """槽位分配：取最小空闲；已退场实例的槽位可复用；撞车要被报出来。"""
    checks: list = []
    root = os.path.join(base, 'share')
    os.makedirs(root, exist_ok=True)

    def write(node_id, slot, *, stale_s=0.0, peer_id='', role=relay.ROLE_SLAVE):
        relay.write_registry_entry(root, relay.RegistryEntry(
            nodeId=node_id, peerId=peer_id or node_id, role=role, host=node_id,
            fingerprint=f'fp-{node_id}', slot=slot, pid=1000 + slot,
            startedAt=relay.now_iso(), lastSeen=relay.now_iso(),
            appVersion='test', poolId='team-a',
        ))
        if stale_s:
            path = relay.registry_path(root, node_id)
            past = time.time() - stale_s
            os.utime(path, (past, past))

    write('nd-b', 1)
    write('nd-c', 2)
    write('nd-gone', 3, stale_s=relay.REGISTRY_LEASE_S * 4)

    entries = relay.read_registry(root)
    if len(entries) == 3:
        checks.append(('池注册表读回全部实例', True, ''))
    else:
        checks.append(('池注册表读回全部实例', False, sorted(entries)))

    # 本机第一次注册时槽位应当取 3（1、2 被在册实例占着，3 的原主人已退场）
    slot = relay.allocate_slot(entries, self_node_id='nd-me')
    if slot == 3:
        checks.append(('槽位取最小空闲且复用已退场实例的槽位', True, ''))
    else:
        checks.append(('槽位取最小空闲且复用已退场实例的槽位', False, f'得到 {slot}'))

    # 自己已经在册时不重新分配（见 relay.allocate_slot 的说明）：槽位在运行期往上
    # 跳，对别的实例来说就是「一个熟悉成员忽然换了槽位」，和重启/改名混在一起难分辨。
    again = relay.allocate_slot(entries, self_node_id='nd-b')
    if again == 1:
        checks.append(('已注册的实例重复分配得到原来的槽位（不会在运行期漂移）', True, ''))
    else:
        checks.append(('已注册的实例重复分配得到原来的槽位', False, f'得到 {again}'))

    # 租约：退场实例在租约内仍算在册（否则同机两个实例会互相抢槽位）
    fresh = relay.RegistryEntry(nodeId='nd-x', slot=9, lastSeen=relay.now_iso())
    relay.write_registry_entry(root, fresh)
    fresh_entries = relay.read_registry(root)
    if not fresh_entries['nd-x'].is_stale(lease_s=relay.REGISTRY_LEASE_S):
        checks.append(('租约内的心跳记录算在册', True, ''))
    else:
        checks.append(('租约内的心跳记录算在册', False, '被判退场'))

    # 撞车可见：同槽位 + 同 peerId 但不同实例，都要被 pool_snapshot 报出来。
    # 这两类过去只能靠肉眼对比心跳文件才发现（全中文主机名一起退化成 pc 就是这么来的）。
    relay.write_registry_entry(root, relay.RegistryEntry(
        nodeId='nd-clash', peerId='nd-b', role=relay.ROLE_SLAVE, host='nd-clash',
        slot=9, lastSeen=relay.now_iso(),
    ))
    snap = relay.pool_snapshot(root, pool_id='team-a', self_node_id='nd-b')
    slots = {item['slot'] for item in snap['slotConflicts']}
    peers = {item['peerId'] for item in snap['peerIdConflicts']}
    if 9 in slots:
        checks.append(('同槽位撞车被池快照报出', True, ''))
    else:
        checks.append(('同槽位撞车被池快照报出', False, snap['slotConflicts']))
    if 'nd-b' in peers:
        checks.append(('同标识多实例撞车被池快照报出（既有已知债变得可见）', True, ''))
    else:
        checks.append(('同标识多实例撞车被池快照报出', False, snap['peerIdConflicts']))

    self_rows = [item for item in snap['entries'] if item['isSelf']]
    if len(self_rows) == 1 and self_rows[0]['nodeId'] == 'nd-b':
        checks.append(('池快照标出本机自己是哪一个实例', True, ''))
    else:
        checks.append(('池快照标出本机自己是哪一个实例', False, self_rows))
    return checks


def _check_visible_gate(base):
    """第二档隔离：未配对的对端不进对端列表、不进角色仲裁。"""
    checks: list = []
    peers_dir = os.path.join(base, 'share', 'peers')
    os.makedirs(peers_dir, exist_ok=True)

    # 一个已配对的主控（nodeId=nd-paired）与一个未配对的主控（nodeId=nd-stranger），
    # 再加上一条**老版本**写的心跳（没有 nodeId，读出来是空串）。
    _write_beat(peers_dir, peer_id='pc-a', role=relay.ROLE_MASTER, node_id='nd-paired')
    _write_beat(peers_dir, peer_id='pc-b', role=relay.ROLE_MASTER, node_id='nd-stranger')
    _write_beat(peers_dir, peer_id='pc-old', role=relay.ROLE_MASTER, node_id='')
    peers = relay.read_peers(peers_dir)

    health = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='pc-me', peers=peers,
        carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED},
        pairing_enabled=True, paired_node_ids={'nd-paired'},
    )
    visible = sorted(view.peerId for view in health.peers)
    if visible == ['pc-a']:
        checks.append(('未配对的对端不进对端列表（含无实例键的老版本对端）', True, ''))
    else:
        checks.append(('未配对的对端不进对端列表（含无实例键的老版本对端）', False, visible))

    if sorted(health.masters) == ['pc-a']:
        checks.append(('未配对的实例不参与角色仲裁（不会冒充第二个主控）', True, ''))
    else:
        checks.append(('未配对的实例不参与角色仲裁（不会冒充第二个主控）', False, health.masters))

    # 这里只断言「不冲突」：从机没有信任名单时状态本来就会是 pending-confirm
    # （那是既有的首连确认语义），把状态一起写死会把两条不相干的判据耦在一起。
    if not health.conflict and health.state != relay.STATE_ROLE_CONFLICT:
        checks.append(('未配对者不制造角色冲突（冲突是 fail-closed 的）', True, ''))
    else:
        checks.append(('未配对者不制造角色冲突（冲突是 fail-closed 的）', False,
                       f'{health.state} / {health.hint}'))

    unpaired = {item['nodeId'] or item['peerId']: item for item in health.unpairedPeers}
    if set(unpaired) == {'nd-stranger', 'pc-old'}:
        checks.append(('未配对者单独进 unpairedPeers（有可见的提示材料）', True, ''))
    else:
        checks.append(('未配对者单独进 unpairedPeers（有可见的提示材料）', False, sorted(unpaired)))

    if unpaired.get('pc-old', {}).get('legacy'):
        checks.append(('无实例键的老版本对端被标成 legacy（提示要升级哪一端）', True, ''))
    else:
        checks.append(('无实例键的老版本对端被标成 legacy（提示要升级哪一端）', False,
                       unpaired.get('pc-old')))

    if '未配对' in health.hint and 'pc-b' in health.hint:
        checks.append((f'提示里点名未配对实例：{health.hint[:60]}…', True, ''))
    else:
        checks.append(('提示里点名未配对实例', False, health.hint))

    # 反向验证：关掉门槛，同样这份心跳必须三个都进来 —— 否则说明上面测的不是门槛
    legacy_health = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='pc-me', peers=peers,
        carrier_kind='shared-folder', link={'state': relay.STATE_CONNECTED},
    )
    legacy_visible = sorted(view.peerId for view in legacy_health.peers)
    if legacy_visible == ['pc-a', 'pc-b', 'pc-old']:
        checks.append((f'反向验证：门槛关闭时三条都在列表里（{legacy_visible}）', True, ''))
    else:
        checks.append(('反向验证：门槛关闭时三条都在列表里', False, legacy_visible))
    if legacy_health.conflict:
        checks.append(('反向验证：门槛关闭时两个在线主控直接判角色冲突（这正是要挡的情形）', True, ''))
    else:
        checks.append(('反向验证：门槛关闭时两个在线主控直接判角色冲突', False, legacy_health.masters))
    return checks


def _check_admission_points(base):
    """第一档隔离：未配对者既不能被受理，也不能被投递（两个生成点都要堵住）。"""
    checks: list = []
    with _patch(list_devices=_fake_list_devices):
        node = _make_node(base, 'pc-slave', relay.ROLE_SLAVE, pool_id='team-a',
                          pairing_required=True)
        try:
            peers_dir = node.queue.peers_dir
            _write_beat(peers_dir, peer_id='pc-master', role=relay.ROLE_MASTER,
                        node_id='nd-master')
            node._peers = relay.read_peers(peers_dir)

            request = relay.new_request('exec', 'pc-master', device={'key': 'de47c9b2'})
            reason = node._authorize(request)
            if '未配对' in reason:
                checks.append((f'生效点③ 从机拒收未配对主控的请求：{reason[:48]}…', True, ''))
            else:
                checks.append(('生效点③ 从机拒收未配对主控的请求', False, reason or '竟然放行了'))

            # 配对之后同一份请求必须放行 —— 这一步同时验证「配对会顺带确认信任」，
            # 否则用户还得再点一次首连确认，两次点击表示同一个决定，只是摩擦。
            node.pair_peer('nd-master', peer_id='pc-master')
            passed = node._authorize(relay.new_request('exec', 'pc-master',
                                                       device={'key': 'de47c9b2'}))
            if passed == '':
                checks.append(('配对后同一来源被放行（配对隐含信任确认）', True, ''))
            else:
                checks.append(('配对后同一来源被放行（配对隐含信任确认）', False, passed))

            if 'pc-master' in node.trust.items().__str__():
                checks.append(('配对写入的信任记录带上了对端标识', True, ''))
            else:
                checks.append(('配对写入的信任记录带上了对端标识', False,
                               [r.peerId for r in node.trust.items()]))

            # 解除配对后立刻回到拒绝（撤销是单方面立即生效的）
            node.unpair_peer('nd-master')
            after = node._authorize(relay.new_request('exec', 'pc-master',
                                                     device={'key': 'de47c9b2'}))
            if '未配对' in after or '未确认' in after:
                checks.append((f'解除配对后立刻拒收：{after[:44]}…', True, ''))
            else:
                checks.append(('解除配对后立刻拒收', False, after or '仍然放行'))
        finally:
            node.stop()
    return checks


def _check_submit_gate(base):
    """主控侧：未配对的对端不发请求（否则每次操作都在对方分区留垃圾）。"""
    checks: list = []
    with _patch(list_devices=_fake_list_devices):
        node = _make_node(base, 'pc-master', relay.ROLE_MASTER, pool_id='team-a',
                          pairing_required=True)
        try:
            peers_dir = node.queue.peers_dir
            _write_beat(peers_dir, peer_id='pc-slave', role=relay.ROLE_SLAVE,
                        node_id='nd-slave')
            node._peers = relay.read_peers(peers_dir)
            node._health = node._compute_health()

            requests_dir = relay.RelayQueue.for_peer(
                node.channel_root('pc-slave'), 'pc-slave').requests_dir

            result = node.submit('pc-slave', 'exec', device={'key': 'de47c9b2'},
                                 payload={'argv': ['devices']}, timeout=1.0)
            written = os.path.isdir(requests_dir) and os.listdir(requests_dir)
            if not result.ok and result.extra.get('unpaired'):
                checks.append((f'生效点② 未配对时不投递：{result.error[:48]}…', True, ''))
            else:
                checks.append(('生效点② 未配对时不投递', False,
                               f'{result.status} / {result.error}'))
            if not written:
                checks.append(('生效点② fail-closed：对方分区里没有留下任何请求文件', True, ''))
            else:
                checks.append(('生效点② fail-closed：对方分区里没有留下任何请求文件', False,
                               written))

            # 反向验证：配对之后请求真的写下去了（证明上一条不是「压根没实现投递」）
            node.pair_peer('nd-slave', peer_id='pc-slave')
            node._health = node._compute_health()
            node.submit('pc-slave', 'exec', device={'key': 'de47c9b2'},
                        payload={'argv': ['devices']}, timeout=0.4)
            written_after = os.path.isdir(requests_dir) and os.listdir(requests_dir)
            if written_after:
                checks.append((f'反向验证：配对后请求真的投出去了（{len(written_after)} 个文件）', True, ''))
            else:
                checks.append(('反向验证：配对后请求真的投出去了', False, '仍然没有请求文件'))
        finally:
            node.stop()
    return checks


def _check_pool_compat_and_seam(base):
    """两处结构不变量：未启用池时不落任何新目录；所有队列都走 channel_root 这道缝。"""
    checks: list = []
    with _patch(list_devices=_fake_list_devices):
        legacy = _make_node(base, 'pc-legacy', relay.ROLE_MASTER)
        pooled = _make_node(base, 'pc-pooled', relay.ROLE_MASTER, pool_id='team-a')
        try:
            # 未启用池：不建 registry/，数据根就是配置里那个目录
            legacy.queue.ensure_layout()
            legacy._refresh_presence()
            legacy_registry = relay.registry_dir(legacy.root)
            if not os.path.exists(legacy_registry):
                checks.append(('未启用池时不建 registry/（目录结构与老版本一致）', True, ''))
            else:
                checks.append(('未启用池时不建 registry/（目录结构与老版本一致）', False,
                               legacy_registry))

            # 启用池：数据根下沉、注册表真的写了、队列跟着数据根走
            pooled.queue.ensure_layout()
            pooled._refresh_presence()
            expect = os.path.join(pooled.base_root, relay.POOL_DIR, 'team-a')
            if os.path.normcase(pooled.root) == os.path.normcase(expect):
                checks.append(('启用池时数据根下沉一层', True, ''))
            else:
                checks.append(('启用池时数据根下沉一层', False, pooled.root))
            if os.path.normcase(pooled.queue.root) == os.path.normcase(pooled.root):
                checks.append(('队列跟着数据根走（不是仍指向配置里那个目录）', True, ''))
            else:
                checks.append(('队列跟着数据根走', False, pooled.queue.root))
            entries = relay.read_registry(pooled.root)
            if pooled.node_id in entries and entries[pooled.node_id].slot >= 1:
                checks.append((f'启动即注册进池并拿到槽位 {entries[pooled.node_id].slot}', True, ''))
            else:
                checks.append(('启动即注册进池并拿到槽位', False, sorted(entries)))

            # 缝：channel_root 是模型 A/B 的分界点，模型 A 下必须恒等于数据根。
            # 这条同时保证了 5 处 for_peer 调用点不会有人绕过去写死 self.root。
            if pooled.channel_root('pc-x') == pooled.root:
                checks.append(('channel_root 在模型 A 下恒等于数据根（B 模式的预留缝）', True, ''))
            else:
                checks.append(('channel_root 在模型 A 下恒等于数据根', False,
                               pooled.channel_root('pc-x')))

            source = open(os.path.join(ROOT, 'relay_node.py'), encoding='utf-8').read()
            # 只认真正的调用行：``channel_root`` 自己的文档里会提到这个旧写法
            # （它要解释自己为什么存在），把那行算成违例会得到一个永远修不掉的假失败。
            bypass = [line.strip() for line in source.splitlines()
                      if 'for_peer(self.root' in line and '``' not in line]
            if not bypass:
                checks.append(('没有调用点绕过 channel_root 直连 self.root', True, ''))
            else:
                checks.append(('没有调用点绕过 channel_root 直连 self.root', False, bypass))
        finally:
            legacy.stop()
            pooled.stop()
    return checks


def run() -> bool:
    tmp = tempfile.mkdtemp(prefix='relay-pairing-test-')
    checks: list = []
    probes = (
        _check_pool_root,
        _check_manifest_features,
        _check_registry_slots,
        _check_visible_gate,
        _check_admission_points,
        _check_submit_gate,
        _check_pool_compat_and_seam,
    )
    try:
        for index, probe in enumerate(probes):
            # 每个用例一个独立目录：信任/配对名单是**持久**的，共用目录会让上一个
            # 用例的配对结果串进下一个用例（那会掩盖掉「配对才放行」这条判据）。
            checks += probe(os.path.join(tmp, f'{index:02d}-{probe.__name__}'))

        # 配对的撤销是单方面生效的 —— 用真实的 PairingStore 走一遍状态机。
        store_path = os.path.join(tmp, 'pairing-store.json')
        store = relay.PairingStore(store_path)
        store.request('nd-1', peer_id='pc-a')
        checks.append(('配对状态机：discovered → requested',
                       store.get('nd-1').state == relay.PAIR_REQUESTED,
                       store.get('nd-1').state))
        store.confirm('nd-1')
        checks.append(('配对状态机：requested → confirmed 才放行',
                       store.is_paired('nd-1'), store.get('nd-1').state))
        store.revoke('nd-1')
        checks.append(('配对状态机：撤销立即生效（但记录保留）',
                       not store.is_paired('nd-1') and store.get('nd-1') is not None,
                       store.get('nd-1').state))
        reopened = relay.PairingStore(store_path)
        checks.append(('配对名单持久化在本机（重启后仍是「已解除」）',
                       reopened.get('nd-1').state == relay.PAIR_REVOKED,
                       reopened.get('nd-1').state))

        # 同一条记录对外**两种形状**，别混用：落盘（``to_dict``）不含派生字段，
        # 界面（``view``）带上 stateLabel/paired。``pair_peer`` 与 ``pairing_records``
        # 都返回 ``view`` 形状 —— 混用会让调用方在某一处读到 KeyError（本轮踩过）。
        record = store.get('nd-1')
        raw, shown = record.to_dict(), record.view()
        checks.append(('落盘形状不含派生字段（不污染长期格式）',
                       'stateLabel' not in raw and 'paired' not in raw, sorted(raw)))
        checks.append(('界面形状带 stateLabel/paired（两处调用点同形）',
                       shown.get('stateLabel') == relay.PAIR_STATE_LABELS[relay.PAIR_REVOKED]
                       and shown.get('paired') is False,
                       f"{shown.get('stateLabel')} / {shown.get('paired')}"))
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
