"""池与配对的本地模拟验证：一个共享目录里跑**多套部署**，用准入门槛隔开。

验证的是模型 A（配对 = 准入过滤，见 ``docs/需求与设计/v6-配对池与双端派生路径设计.md`` §9）：

    ① 能不能说话 —— 未配对者发出的请求一律拒绝
    ② 能不能看见 —— 未配对者不进对端列表、不进角色仲裁
    ③ 数据是否同目录 —— **本版不做**（那要给每条配对开一条独立路径，是架构级改动）

脚本用**真节点、真线程、真共享目录、真请求/结果文件**跑完整链路，只在「设备枚举」
与「adb 执行」两处打桩（本机没有真机，也没必要有）：

    ① 池布局：数据根下沉到 ``<共享目录>/pool/<池>/``，老布局的槽目录不再掺在根上
    ② 池注册表：两个实例都在册、各占一个槽位
    ③ 门槛生效：两端互相看不见、主控连请求都发不出去（fail-closed，对方分区零残留）
    ④ 两端各自配对（配对是本机单方面的决定，所以要两端都放行）
    ⑤ 配对后连通：设备出现、命令真实往返（从机侧能证明自己真的执行了）
    ⑥ 解除配对立刻回到拦截；从机单方面解除时，主控仍会投递但**被从机拒收**
       —— 这条正说明「真正的裁决点在从机侧」，主控的预检只是体验优化
    ⑦ 跨部署发现：同一个共享目录里另一套部署可供配对，未配对时不参与通讯
    ⑧ 老部署零迁移：不填池标识的实例仍在老布局上工作，且不产生任何新目录

用法：

    python relay_pool_demo.py                 # 临时共享目录，跑完保留留痕
    python relay_pool_demo.py --root \\\\srv\\relay   # 用真实共享目录（会写 pool/<池>/）
    python relay_pool_demo.py --keep          # 保留临时目录，便于手工翻看现场
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import relay  # noqa: E402
import relay_node  # noqa: E402

POOL_ID = 'team-a'
OTHER_POOL_ID = 'team-b'
SLAVE_DEVICE = {'serial': 'de47c9b2', 'key': 'de47c9b2:tid=104', 'state': 'device'}

# 从机「真的执行过」的证据：打桩的 execute 每被调用一次就记一笔。
EXEC_LOG: list[dict] = []


def _fake_list_devices(adb_path, timeout=8.0):
    return [dict(SLAVE_DEVICE)], ''


def _fake_execute(adb_path, device, argv, *, timeout=30.0, max_bytes=None):
    tokens = [str(part) for part in (argv or [])]
    EXEC_LOG.append({'adb': str(adb_path), 'device': (device or {}).get('key', ''), 'argv': tokens})
    return relay_node.ExecOutcome(
        exitCode=0, stdout=f'executed-on-slave|{tokens[-1]}|{SLAVE_DEVICE["serial"]}',
        elapsedMs=3,
    )


class _patch:
    """把 relay_node 里的设备枚举与 adb 执行换成可控实现。"""

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


def rule(char='-', width=78):
    print(char * width)


def item(text=''):
    print(f'   {text}')


def stage(index, title):
    print()
    rule('=')
    print(f'  阶段 {index}｜{title}')
    rule('=')


_RESULTS: list[tuple] = []


def check(name, passed, detail=''):
    _RESULTS.append((name, bool(passed), detail))
    mark = 'PASS' if passed else 'FAIL'
    suffix = f' — {detail}' if detail and not passed else ''
    print(f'  [{mark}] {name}{suffix}')
    return bool(passed)


def wait_until(predicate, timeout=12.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def make_node(share, *, peer_id, role, node_id, pool_id, fingerprint):
    return relay_node.RelayNode(
        peer_id=peer_id,
        role=role,
        root=share,
        pool_id=pool_id,
        node_id=node_id,
        pairing_path=os.path.join(share, '..', f'{peer_id}-pairing.json'),
        adb_path=os.path.join(share, '..', 'fake-adb.exe'),
        trust_path=os.path.join(share, '..', f'{peer_id}-trust.json'),
        host_label=f'{peer_id}-host',
        host_fingerprint=fingerprint,
        heartbeat_interval_s=0.4,
        poll_interval_s=0.1,
    )


def unpaired_of(node) -> dict:
    return {str(item.get('peerId') or ''): item for item in node.health().unpairedPeers}


def request_files(node, peer_id) -> list:
    directory = relay.RelayQueue.for_peer(node.channel_root(peer_id), peer_id).requests_dir
    try:
        return os.listdir(directory)
    except OSError:
        return []


def run(args) -> int:
    base = args.root or tempfile.mkdtemp(prefix='relay-pool-demo-')
    share = base if args.root else os.path.join(base, 'share')
    os.makedirs(share, exist_ok=True)
    print(f'共享目录：{share}')
    print(f'池标识　：{POOL_ID}（另一个池：{OTHER_POOL_ID}）')

    with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        master = make_node(share, peer_id='pc-master', role=relay.ROLE_MASTER,
                           node_id='nd-master-01', pool_id=POOL_ID, fingerprint='fp-master')
        slave = make_node(share, peer_id='pc-slave', role=relay.ROLE_SLAVE,
                          node_id='nd-slave-01', pool_id=POOL_ID, fingerprint='fp-slave')
        other = make_node(share, peer_id='pc-other', role=relay.ROLE_SLAVE,
                          node_id='nd-other-01', pool_id=OTHER_POOL_ID, fingerprint='fp-other')
        legacy = make_node(share, peer_id='pc-legacy', role=relay.ROLE_SLAVE,
                           node_id='nd-legacy-01', pool_id='', fingerprint='fp-legacy')
        try:
            for node in (master, slave):
                node.start()
                node.refresh()

            # ------------------------------------------------------------------
            stage(1, '池布局：数据根下沉一层，老布局的槽目录不再掺在根上')
            pool_root = os.path.join(share, relay.POOL_DIR, POOL_ID)
            item(f'共享目录　　{share}')
            item(f'本池数据根　{pool_root}')
            check('数据根落在 <共享目录>/pool/<池>/',
                  os.path.normcase(master.root) == os.path.normcase(pool_root), master.root)
            for slot in (relay.SLOT_PEERS, relay.SLOT_REQUESTS, relay.SLOT_RESULTS,
                         relay.SLOT_STREAMS, relay.SLOT_ARCHIVE, relay.SLOT_REGISTRY):
                check(f'池内槽目录 {slot}/ 已建立', os.path.isdir(os.path.join(pool_root, slot)))
            check('老布局的 peers/ 没有被创建（两套部署不会混在根上）',
                  not os.path.exists(os.path.join(share, relay.SLOT_PEERS)))
            manifest = json.loads(open(os.path.join(pool_root, 'relay.json'), encoding='utf-8').read())
            check(f'清单声明了池特性：{relay.FEATURE_POOL}',
                  relay.FEATURE_POOL in (manifest.get('requiredFeatures') or []),
                  manifest.get('requiredFeatures'))

            # ------------------------------------------------------------------
            stage(2, '池注册表：两个实例都在册，各占一个槽位')
            wait_until(lambda: len(relay.read_registry(pool_root)) >= 2)
            snap = master.pool_info()
            entries = {entry['nodeId']: entry for entry in snap['entries']}
            item(f'池内实例：' + '、'.join(
                f'{e["peerId"]}({e["roleLabel"]}, 槽位 {e["slot"]}, '
                f'{"在线" if e["online"] else "离线"})' for e in snap['entries']))
            check('两个实例都注册进池', set(entries) == {master.node_id, slave.node_id},
                  sorted(entries))
            slots = sorted(entry['slot'] for entry in snap['entries'])
            check(f'槽位互不重复：{slots}', slots == [1, 2], slots)
            check('池快照能指出本机自己是哪一个实例',
                  [e['nodeId'] for e in snap['entries'] if e['isSelf']] == [master.node_id])
            check('本机实例键已写进心跳（对端据此判准入）',
                  entries[master.node_id]['peerId'] == 'pc-master')

            # ------------------------------------------------------------------
            stage(3, '门槛生效：未配对时互相看不见，请求也发不出去')
            online_both = wait_until(
                lambda: unpaired_of(master).get('pc-slave', {}).get('online')
                and unpaired_of(slave).get('pc-master', {}).get('online'))
            check('两端都被对方发现（心跳是通的，只是没被放行）', online_both)
            master.refresh()
            slave.refresh()
            m_health, s_health = master.health(), slave.health()
            item(f'主控视角：{m_health.stateText}｜{m_health.hint[:96]}')
            item(f'从机视角：{s_health.stateText}｜{s_health.hint[:96]}')
            check('主控的对端列表里没有未配对的从机', m_health.peers == [],
                  [v.peerId for v in m_health.peers])
            check('从机的对端列表里没有未配对的主控', s_health.peers == [],
                  [v.peerId for v in s_health.peers])
            check('未配对的实例单独列进 unpairedPeers（有可见的提示材料）',
                  set(unpaired_of(master)) == {'pc-slave'}, sorted(unpaired_of(master)))
            # masters 恒含本机自己（角色仲裁的既有语义），要验的是**未配对的对端**
            # 有没有混进仲裁：从机视角最关键 —— 若未配对的主控进了 onlineMasters，
            # 从机会误以为「已有主控」而把自己判成多余的一方。
            check('未配对的主控不进从机的角色仲裁',
                  'pc-master' not in s_health.masters
                  and 'pc-master' not in s_health.onlineMasters,
                  f'masters={s_health.masters} onlineMasters={s_health.onlineMasters}')
            check('未配对的从机不进主控的角色仲裁',
                  'pc-slave' not in m_health.masters
                  and 'pc-slave' not in m_health.onlineMasters,
                  f'masters={m_health.masters} onlineMasters={m_health.onlineMasters}')
            check('提示语点名「未配对」而不是「没发现心跳」'
                  if '未配对' in m_health.hint else '提示语点名「未配对」',
                  '未配对' in m_health.hint, m_health.hint)

            result = master.submit('pc-slave', relay.KIND_EXEC,
                                   device={'key': SLAVE_DEVICE['key']},
                                   payload={'argv': ['devices']}, timeout=1.0)
            check(f'主控发请求被本地拦下：{result.error[:44]}…', not result.ok and result.extra.get('unpaired'),
                  f'{result.status} / {result.error}')
            check('对方分区里零残留（fail-closed，不是发出去再拒）',
                  request_files(master, 'pc-slave') == [], request_files(master, 'pc-slave'))

            # ------------------------------------------------------------------
            stage(4, '两端各自配对（配对是本机单方面的决定，两端都要放行）')
            master_node_id = slave.node_id
            slave_peer_id = 'pc-slave'
            master_record = master.pair_peer(master_node_id, peer_id=slave_peer_id)
            slave_record = slave.pair_peer(master.node_id, peer_id='pc-master')
            item(f'主控侧：{slave_peer_id}（{master_node_id}）→ {master_record["stateLabel"]}')
            item(f'从机侧：pc-master（{master.node_id}）→ {slave_record["stateLabel"]}')
            check('两端配对状态都是「已配对」',
                  master_record['paired'] and slave_record['paired'],
                  f'{master_record["state"]} / {slave_record["state"]}')

            # ------------------------------------------------------------------
            stage(5, '配对后连通：设备出现，命令真实往返')
            ok = wait_until(lambda: master.health().state == relay.STATE_CONNECTED, timeout=8.0)
            master.refresh()
            health = master.health()
            item(f'主控视角：{health.stateText}｜{health.hint[:96]}')
            check('主控状态变为「已连接」', ok, f'{health.state} / {health.hint}')
            check('从机的设备出现在主控的远端设备列表里',
                  any('relay:' in str(d.get('key', '')) for d in master.remote_devices()),
                  [d.get('key') for d in master.remote_devices()])
            check('未配对列表已空', unpaired_of(master) == {}, unpaired_of(master))

            devices = [d for d in master.remote_devices() if d.get('key', '').startswith('relay:')]
            peer_id, local_key = relay_node.split_remote_key(devices[0]['key'])
            before = len(EXEC_LOG)
            exec_result = master.exec_remote(peer_id, local_key, ['devices'], timeout=6.0)
            check(f'跨机执行成功：{exec_result.stdout.strip()[:52]}', exec_result.ok,
                  f'{exec_result.status} / {exec_result.error}')
            check('从机侧确实执行了（不是主控本机跑的）', len(EXEC_LOG) == before + 1,
                  EXEC_LOG[before:])
            check('结果是命令的真实输出', SLAVE_DEVICE['serial'] in (exec_result.stdout or ''),
                  exec_result.stdout)

            # ------------------------------------------------------------------
            stage(6, '解除配对立刻回到拦截；裁决点在从机侧')
            slave.unpair_peer(master.node_id)
            time.sleep(0.3)
            accepted = master.exec_remote(peer_id, local_key, ['devices'], timeout=6.0)
            check(f'从机单方面解除后，主控照发但被拒收：{accepted.error[:40]}…',
                  accepted.status == relay.STATUS_REJECTED and '未配对' in (accepted.error or ''),
                  f'{accepted.status} / {accepted.error}')
            check('主控侧自己的预检此时仍放行（正说明真正的裁决点在从机）',
                  master.pairing.is_paired(slave.node_id))

            master.unpair_peer(slave.node_id)
            master.refresh()
            blocked = master.submit('pc-slave', relay.KIND_EXEC,
                                    device={'key': SLAVE_DEVICE['key']},
                                    payload={'argv': ['devices']}, timeout=1.0)
            master.refresh()
            check(f'主控也解除后本地就拦住了：{blocked.error[:40]}…',
                  not blocked.ok and blocked.extra.get('unpaired'),
                  f'{blocked.status} / {blocked.error}')
            check('主控不再把该从机列为对端', master.health().peers == [],
                  [v.peerId for v in master.health().peers])
            check('按实例键配对：解除后配对名单里仍是「已解除」而不是被抹掉',
                  (master.pairing.get(slave.node_id) or None) is not None
                  and master.pairing.get(slave.node_id).state == relay.PAIR_REVOKED)

            # ------------------------------------------------------------------
            stage(7, '跨部署发现：共享目录内的实例统一进入配对列表')
            other.start()
            other.refresh()
            other_pool_root = os.path.join(share, relay.POOL_DIR, OTHER_POOL_ID)
            item(f'另一套部署的数据根：{other_pool_root}')
            check('另一套部署落在自己的数据根上',
                  os.path.normcase(other.root) == os.path.normcase(other_pool_root), other.root)
            time.sleep(1.0)
            master.refresh()
            check('另一套部署可供配对，尚未配对时不进入通讯对端',
                  any(item['nodeId'] == other.node_id for item in master.pairing_records())
                  and all(v.peerId != 'pc-other' for v in master.health().peers),
                  master.pairing_records())
            other_snap = other.pool_info()
            check('另一套部署的池名册里只有它自己',
                  [e['nodeId'] for e in other_snap['entries']] == [other.node_id],
                  [e['nodeId'] for e in other_snap['entries']])
            other.stop()

            # ------------------------------------------------------------------
            stage(8, '老部署零迁移：不填池标识的实例仍在老布局上工作')
            legacy.start()
            legacy.refresh()
            item(f'老实例数据根：{legacy.root}')
            check('老实例的数据根就是配置里那个共享目录',
                  os.path.normcase(legacy.root) == os.path.normcase(share), legacy.root)
            check('老实例在老布局上建了 peers/（与今天的部署完全一致）',
                  os.path.isdir(os.path.join(share, relay.SLOT_PEERS)))
            check('老实例不产生 registry/（目录结构没有新增）',
                  not os.path.exists(relay.registry_dir(share)),
                  relay.registry_dir(share))
            # 兼容承诺的另一半：未启用池时清单必须与老版本**逐字节一致**，
            # 否则老端一读到不认识的特性就会拒绝启动 —— 那就不是「零迁移」了。
            legacy_manifest = json.loads(
                open(os.path.join(share, 'relay.json'), encoding='utf-8').read())
            check('老实例的清单里没有池特性（老端照常能读）',
                  relay.FEATURE_POOL not in (legacy_manifest.get('requiredFeatures') or []),
                  legacy_manifest.get('requiredFeatures'))
            check('老实例的 requiredFeatures 恰好等于底座特性、顺序也一致',
                  list(legacy_manifest.get('requiredFeatures') or []) == list(relay.BASE_FEATURES),
                  legacy_manifest.get('requiredFeatures'))
            legacy.stop()

            # ------------------------------------------------------------------
            stage(9, '收尾：退出即撤掉注册项（谁写谁清），槽位立刻可复用')
            master.stop()
            slave.stop()
            wait_until(lambda: master.node_id not in relay.read_registry(pool_root)
                       and slave.node_id not in relay.read_registry(pool_root), timeout=5.0)
            left = sorted(relay.read_registry(pool_root))
            check(f'两个实例的注册项都已撤掉：{left}', left == [], left)
            check('心跳文件仍在 peers/ 里（那是总线记录，不属于注册名册）',
                  os.path.isdir(os.path.join(pool_root, relay.SLOT_PEERS)))
        finally:
            for node in (master, slave, other, legacy):
                try:
                    node.stop()
                except Exception:
                    pass

    print()
    rule('=')
    passed = sum(1 for _name, ok, _detail in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    print(f'  结果：{passed} 项通过，{failed} 项失败')
    rule('=')
    if failed:
        for name, ok, detail in _RESULTS:
            if not ok:
                print(f'  [FAIL] {name} — {detail}')
    if args.keep or not args.root:
        print(f'  现场保留在：{base}')
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='池与配对的本地模拟验证')
    parser.add_argument('--root', default='', help='共享目录（留空则用临时目录模拟）')
    parser.add_argument('--keep', action='store_true', help='保留临时共享目录')
    args = parser.parse_args()
    if args.root and not os.path.isdir(args.root):
        print(f'共享目录不存在：{args.root}')
        return 2
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
