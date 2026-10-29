"""同机多开：同一台机器上两个**真实进程**（同一个共享目录）必须能互相发现。

用户现场：本机开两个窗口时，配对列表里看不到另一个实例。根因是「两个实例拿到了
同一个标识」，后果有两层：

  ① 心跳文件名就是 ``<peerId>.json`` —— 重名的两个实例写**同一个文件**，互相覆盖
     （破坏单写者原则）；
  ② ``evaluate_health`` 里 ``peer_id == self_id`` 直接跳过 —— 同名条目被当成「自己」。

而标识此前是**用户手填**的（多开时只给了一句 placeholder 建议、不自动应用），
所以默认两个窗口都叫主机名，必然撞车。修复后：标识自动生成，多开的实例**自动**
退让成 ``<主机名>-2``。

**为什么必须用真进程**：退让判据里有一条是 ``relay_app._pid_alive`` —— 用「同机 +
pid 仍活着」把「活着的兄弟」和「我上一轮留下的残留心跳」分开。同进程里的两个对象
``os.getpid()`` 永远相同，模拟不出这件事。子进程写完结果会停留不退出，好让两边都在
对方活着的时候各取一次状态（先退出的那个会被对方按「同机旧身份」正确过滤掉 ——
那样就测反了）。

两组场景都跑，因为「能不能看见」的落点不同：

  * 未启用池/配对 → 对端出现在 ``peers``（对端列表）；
  * 启用池 + 配对 → 未配对的对端**不进** ``peers``，而是出现在 ``unpairedPeers``，
    也就是界面上那张「可配对的目标」列表 —— 用户报的正是这一张。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import relay  # noqa: E402
import relay_app
from unittest.mock import patch
from pathlib import Path

CHILD = os.path.join(ROOT, 'tests', '_relay_mi_child.py')
# 与 relay_app.claim_config_path 里那句说明同形：非空即表示「本实例让出了默认配置」。
MULTI_NOTICE = ('本机已有一个实例在使用默认中转配置（pid 12345），本窗口改用独立临时'
                '配置（关闭本窗口后这些设置不保留）。')


def _spawn(tmp: str, share: str, *, name: str, role: str, notice: str, pool: str = ''):
    result_path = os.path.join(tmp, f'{name}.json')
    config_path = os.path.join(tmp, f'relay-{name}.json')
    log_path = os.path.join(tmp, f'{name}.log')
    handle = open(log_path, 'w', encoding='utf-8')
    argv = [sys.executable, '-B', CHILD,
            '--root', share, '--config', config_path, '--role', role,
            '--notice', notice, '--wait', '6', '--hold', '90', '--out', result_path]
    if pool:
        argv += ['--pool', pool]
    proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT)
    return proc, result_path, log_path, handle


def _read_json(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _wait_result(path: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _read_json(path)
        if data:
            return data
        time.sleep(0.25)
    return None


def _two_instances(base: str, *, pool: str = '') -> tuple:
    """起 A（先）与 B（后，模拟让出默认配置的多开实例），返回两边状态与日志路径。"""
    share = os.path.join(base, 'share')
    os.makedirs(share, exist_ok=True)
    procs: list = []
    handles: list = []
    logs: list = []
    first = second = None
    try:
        proc, first_out, log_path, handle = _spawn(
            base, share, name='a', role='slave', notice='', pool=pool)
        procs.append(proc)
        handles.append(handle)
        logs.append(log_path)
        # 先起的实例先落一拍心跳，再开第二个 —— 用户的真实顺序（先开一个窗口再开另一个）。
        time.sleep(3.0)
        proc, second_out, log_path, handle = _spawn(
            base, share, name='b', role='master', notice=MULTI_NOTICE, pool=pool)
        procs.append(proc)
        handles.append(handle)
        logs.append(log_path)

        first = _wait_result(first_out)
        second = _wait_result(second_out)
        if first is None or second is None:
            for path in logs:
                try:
                    with open(path, encoding='utf-8') as handle:
                        tail = handle.read()[-1500:]
                    print(f'  子进程日志 {os.path.basename(path)}：{tail}')
                except OSError:
                    pass
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass
    return first, second


def _same_name_is_invisible() -> bool:
    """两个实例**同名**时注定互相看不见 —— 这正是自动退让存在的理由。"""
    stamp = relay.now_iso()
    health = relay.evaluate_health(
        enabled=True, role=relay.ROLE_SLAVE, self_id='pc-same',
        peers={'pc-same': relay.PeerHeartbeat(
            peerId='pc-same', role=relay.ROLE_MASTER, fingerprint='fp-x',
            pid=999, startedAt=stamp, heartbeatAt=stamp)},
    )
    return health.peers == []


def _common_checks(checks: list, first, second, *, where: str) -> tuple:
    """两个实例都起来之后的公共断言（标识退让 + 进程身份）。返回 (a_id, b_id)。"""
    if first is None or second is None:
        checks.append((f'[{where}] 两个子实例都能在 60s 内给出状态', False, '结果文件没出现'))
        return '', ''
    a_id = str(first.get('peerId') or '')
    b_id = str(second.get('peerId') or '')
    checks.append((f'[{where}] 两个实例都启动成功',
                   bool(first.get('started')) and bool(second.get('started')),
                   f"{first.get('reason')} / {second.get('reason')}"))
    checks.append((f'[{where}] 两个实例是两个真实进程（pid 不同）',
                   bool(first.get('pid')) and first.get('pid') != second.get('pid'),
                   f"{first.get('pid')} / {second.get('pid')}"))
    checks.append((f'[{where}] 多开实例的标识自动退让：{a_id} → {b_id}',
                   bool(a_id) and b_id == f'{a_id}-2', f'{a_id} / {b_id}'))
    checks.append((f'[{where}] 先起的实例保持首选标识（不动用户的设定）',
                   a_id == first.get('configPeerId'),
                   f"{a_id} / {first.get('configPeerId')}"))
    checks.append((f'[{where}] 后起的实例把首选值留在配置里，只在生效值上退让',
                   second.get('configPeerId') != b_id,
                   f"{second.get('configPeerId')} / {b_id}"))
    checks.append((f'[{where}] 双方都没把对方误判成「本机旧身份」',
                   not (first.get('selfGhosts') or []) and not (second.get('selfGhosts') or []),
                   f"{first.get('selfGhosts')} / {second.get('selfGhosts')}"))
    return a_id, b_id


def run() -> bool:
    with tempfile.TemporaryDirectory() as duplicate_root:
        config_path = str(Path(duplicate_root) / 'config.json')
        config = relay_app.RelayConfig(enabled=True, role='slave', root=duplicate_root, nodeId='node-new')
        relay_app.save_config(config_path, config)
        controller = relay_app.RelayController(config_path=config_path, host_label='same-machine',
                                               host_fingerprint='same-fingerprint')
        peers_dir = str(Path(duplicate_root) / 'peers')
        existing = relay.PeerHeartbeat(peerId='existing', role='slave', host='same-machine',
            fingerprint='same-fingerprint', nodeId='node-existing', pid=os.getpid() + 100000,
            heartbeatAt=relay.now_iso(), startedAt=relay.now_iso())
        relay.write_heartbeat(peers_dir, existing)
        original = Path(config_path).read_bytes()
        with patch.object(relay_app, '_pid_alive', return_value=True), \
             patch.object(controller, 'stop', side_effect=AssertionError('不能停止原节点')):
            ok, reason = controller.apply_config({'allowShell': True})
            assert not ok and '无法保存并生效' in reason and Path(config_path).read_bytes() == original
            assert not controller.start()[0]
            assert controller.check_config({'poolId': 'another-deployment'})[0]
            assert controller.check_config({'role': 'master'})[0]
        with patch.object(relay_app, '_pid_alive', return_value=False):
            assert controller.check_config({})[0]
        existing.pid = os.getpid()
        existing.nodeId = config.nodeId
        relay.write_heartbeat(peers_dir, existing)
        assert controller.check_config({})[0]
        print('  [PASS] 同机同部署从机拒绝保存及启动，配置不变；不同部署、离线重启和自身保存允许')
    checks: list[tuple] = []
    checks.append(('反向：与自身同名的实例如同「自己」，互相看不见（所以必须自动退让）',
                   _same_name_is_invisible(), '同名条目进了对端列表'))

    # 标识选择本身（纯函数）：没冲突用首选；冲突依次 -2/-3；顶到长度上限也要能退让，
    # 而且退让结果必须仍能通过配置校验（PEER_ID_RE 上限 32 字符）。
    no_clash = relay.choose_peer_id('pc-a', [])
    checks.append(('无冲突时用首选标识', no_clash == 'pc-a', no_clash))
    clash = relay.choose_peer_id('pc-a', ['pc-a', 'pc-a-2'])
    checks.append(('冲突时依次退让 -2 → -3', clash == 'pc-a-3', clash))
    long_name = 'x' * 32
    long_clash = relay.choose_peer_id(long_name, [long_name])
    checks.append(('名字顶到 32 字符上限时也能退让（先给后缀腾位置，否则会原地打转）',
                   long_clash != long_name and len(long_clash) <= 32
                   and bool(relay.valid_peer_id(long_clash)),
                   long_clash))

    tmp = tempfile.mkdtemp(prefix='relay-mi-')
    try:
        # --- 第一组：默认部署也要求确认，发现落点在未配对列表 ---
        first, second = _two_instances(os.path.join(tmp, 'plain'))
        a_id, b_id = _common_checks(checks, first, second, where='无池')
        if first is not None and second is not None:
            checks.append(('默认部署实例 A 发现 B，但未确认不进入通讯对端',
                           not first.get('peers') and b_id in {item['peerId'] for item in first.get('unpairedPeers', [])},
                           str(first.get('unpairedPeers'))))
            checks.append(('默认部署实例 B 也能在未配对列表发现 A',
                           not second.get('peers') and a_id in {item['peerId'] for item in second.get('unpairedPeers', [])},
                           str(second.get('unpairedPeers'))))

        # --- 第二组：启用池 + 配对 → 未配对的对端应出现在「可配对目标」里 ---
        first, second = _two_instances(os.path.join(tmp, 'pooled'), pool='team-x')
        a_id, b_id = _common_checks(checks, first, second, where='启用池')
        if first is not None and second is not None:
            def _unpaired(result):
                return {str(item.get('peerId') or '') for item in (result.get('unpairedPeers') or [])}
            pair_a, pair_b = _unpaired(first), _unpaired(second)
            checks.append((f'启用配对后未配对的对端不进对端列表（A 的 peers 为空）',
                           (first.get('peers') or []) == [], str(first.get('peers'))))
            checks.append((f'实例 A 的配对列表里有实例 B（{sorted(pair_a)}）',
                           b_id in pair_a, str(sorted(pair_a))))
            checks.append((f'实例 B 的配对列表里有实例 A（{sorted(pair_b)}）',
                           a_id in pair_b, str(sorted(pair_b))))
            checks.append(('配对列表按**实例键**列出（每项都带 nodeId，才配得上对）',
                           all(item.get('nodeId') for item in (first.get('unpairedPeers') or [])),
                           str(first.get('unpairedPeers'))))
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
