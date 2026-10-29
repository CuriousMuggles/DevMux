"""验证跨机中转的节点运行时（``relay_node.py``）。

覆盖：设备行解析、退出码标记剥离、输出截断、远端 key 编解码、执行守卫、
从机准入裁决、白名单拒绝、过期作废、幂等、一主多从分区、日志流增量读、
连接状态机（未确认 → 已确认）。

全部在本地临时目录里跑，**不依赖真实设备、真实 adb 输出或网络**：
``relay_node.list_devices`` 与 ``relay_node.execute`` 在测试内被替换为可控实现，
因此断言的是「命令到了哪个节点的哪台设备」，而不是设备本身的行为。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay          # noqa: E402
import relay_node     # noqa: E402


# ----------------------------------------------------------------------
# 可控替身：用 adb_path 当节点标识，测试里每个节点的 adb_path 唯一
# ----------------------------------------------------------------------
FAKE_DEVICES = {
    'node-a': [{'serial': 'devA', 'state': 'device', 'transportId': 2, 'model': 'PixelA', 'key': 'devA:tid=2'}],
    'node-b': [{'serial': 'devB', 'state': 'device', 'transportId': 1, 'model': 'PixelB', 'key': 'devB:tid=1'}],
    'node-c': [{'serial': 'devC', 'state': 'device', 'transportId': 0, 'model': 'PixelC', 'key': 'devC:tid=0'}],
}

_EXEC_LOG: list = []
LOG_CHUNKS = [f'logcat-line-{index:03d}\n'.encode('utf-8') for index in range(200)]


def _fake_list_devices(adb_path, timeout=8.0):
    return [dict(item) for item in FAKE_DEVICES.get(str(adb_path), [])], ''


def _fake_execute(adb_path, device, argv, *, timeout=30.0, max_bytes=None):
    tokens = [str(part) for part in (argv or [])]
    device_key = (device or {}).get('key', '')
    _EXEC_LOG.append({'node': str(adb_path), 'device': device_key, 'argv': tokens})
    if tokens and tokens[0] == 'shell':
        body = ' '.join(tokens[1:])
        return relay_node.ExecOutcome(
            exitCode=0, stdout=f'{adb_path}|{device_key}|{body}', elapsedMs=5,
        )
    return relay_node.ExecOutcome(
        exitCode=0, stdout=f'{adb_path}|{device_key}|{" ".join(tokens)}', elapsedMs=4,
    )


class _FakeStdout:
    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay

    def read(self, size=4096):
        if not self._chunks:
            return b''
        chunk = self._chunks.pop(0)
        if self._delay:
            time.sleep(self._delay)
        return chunk[:size]


class _FakeProc:
    """日志流的假 adb 子进程：按块吐数据，可被 terminate。"""

    def __init__(self, chunks, delay=0.0):
        self.stdout = _FakeStdout(chunks, delay)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


_REAL_POPEN = subprocess.Popen


def _fake_popen(command, **kwargs):
    """只拦「本测试的假 adb」，其余 subprocess 一律走真实实现。

    这样 patch 期间即使有别的代码（或其它测试线程）调 Popen 也不会被影响。
    """
    if isinstance(command, (list, tuple)) and command and str(command[0]) in FAKE_DEVICES:
        return _FakeProc(LOG_CHUNKS, delay=0.02)
    return _REAL_POPEN(command, **kwargs)


class _patch:
    """临时替换 ``relay_node`` 里的可调用对象。

    名字可含点（如 ``subprocess.Popen``）—— ``Popen`` 属于被引用的 subprocess
    模块，不是 relay_node 自己的属性，所以要走点分路径才能替换。
    """

    def __init__(self, **targets):
        self._targets = targets
        self._saved: list = []

    def __enter__(self):
        for name, value in self._targets.items():
            if '.' in name:
                owner_name, _, attr = name.partition('.')
                owner = getattr(relay_node, owner_name)
            else:
                owner, attr = relay_node, name
            self._saved.append((owner, attr, getattr(owner, attr)))
            setattr(owner, attr, value)
        return self

    def __exit__(self, *_exc):
        # 逆序恢复，保证一定还原到测试开始前的状态。
        for owner, attr, original in reversed(self._saved):
            setattr(owner, attr, original)
        self._saved.clear()
        return False


def _make_node(root, peer_id, role, adb_key, managed=None, **kwargs):
    return relay_node.RelayNode(
        peer_id=peer_id,
        role=role,
        root=os.path.join(root, 'share'),
        adb_path=adb_key,
        trust_path=os.path.join(root, f'{peer_id}-trust.json'),
        host_label=f'{peer_id}-host',
        host_fingerprint=f'fp-{peer_id}',
        heartbeat_interval_s=0.6,
        poll_interval_s=0.1,
        managed_serials=managed,
        **kwargs,
    )


def _wait_until(predicate, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _size_of(path: str) -> int:
    """只探测文件长度，不消费任何读游标。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ----------------------------------------------------------------------
# 纯函数与守卫
# ----------------------------------------------------------------------
def _check_device_parse(_root) -> list:
    sample = (
        '* daemon not running; starting now at tcp:5037\n'
        '* daemon started successfully\n'
        'List of devices attached\n'
        'de47c9b2               device transport_id:2\n'
        'f62ca3df               device product:x model:Pixel_7 device:y transport_id:1\n'
        '98abcdef               unauthorized transport_id:3\n'
        '\n'
        'noise-line-without-state\n'
    )
    devices = relay_node.parse_devices(sample)
    keys = [item['key'] for item in devices]
    models = {item['serial']: item['model'] for item in devices}
    states = {item['serial']: item['state'] for item in devices}
    return [
        ('解析出 3 台设备并跳过 daemon 提示与表头', len(devices) == 3, str(keys)),
        ('设备 key 形如 serial:tid=<id>', 'de47c9b2:tid=2' in keys, str(keys)),
        ('长格式行解析出 model', models.get('f62ca3df') == 'Pixel_7', str(models)),
        ('offline/unauthorized 设备也返回并保留状态',
         states.get('98abcdef') == 'unauthorized', str(states)),
        ('无 transport_id 时 key 退化为裸 serial',
         relay_node.device_key('serialx', None) == 'serialx', ''),
        ('空输入返回空列表', relay_node.parse_devices('') == [], ''),
    ]


def _check_exit_marker(_root) -> list:
    plain, plain_code = relay_node.split_exit_marker('abc\ndef\n__ADM_EXIT:0\n')
    missing, missing_code = relay_node.split_exit_marker('abc\n')
    weird, weird_code = relay_node.split_exit_marker('echo __ADM_EXIT: not-a-number\n')
    last, last_code = relay_node.split_exit_marker('a __ADM_EXIT:9\nout\n__ADM_EXIT:3\n')
    return [
        ('剥离退出码标记并保留命令输出',
         plain == 'abc\ndef' and plain_code == 0, f'{plain!r} {plain_code}'),
        ('无标记时退出码为 None（不假装是 0）', missing_code is None, str(missing_code)),
        ('标记后不是数字时按无标记处理', weird_code is None, str(weird_code)),
        ('命令输出里含同名串时取最后一个标记',
         last == 'a __ADM_EXIT:9\nout' and last_code == 3, f'{last!r} {last_code}'),
    ]


def _check_truncate(_root) -> list:
    short, cut_short = relay_node.truncate_text('hello', 1024)
    long_text = '头' * 100 + '中' * 5000 + '尾' * 100
    out, cut_long = relay_node.truncate_text(long_text, 600)
    empty, cut_empty = relay_node.truncate_text('', 1024)
    return [
        ('短输出不截断', short == 'hello' and cut_short is False, str(cut_short)),
        ('长输出被标记为截断', cut_long is True, str(cut_long)),
        ('长输出保留头尾并写明省略字节', out.startswith('头') and out.endswith('尾') and '省略' in out,
         out[:20]),
        ('空输出安全', empty == '' and cut_empty is False, ''),
    ]


def _check_remote_key(_root) -> list:
    key = relay_node.remote_key('pc-b', 'devB:tid=1')
    peer_id, local = relay_node.split_remote_key(key)
    peer_none, local_plain = relay_node.split_remote_key('devA:tid=2')
    return [
        ('远端 key 形如 relay:<peerId>:<本机key>', key == 'relay:pc-b:devB:tid=1', key),
        ('按前两个冒号切分，tid 里的冒号不歧义',
         (peer_id, local) == ('pc-b', 'devB:tid=1'), f'{peer_id} / {local}'),
        ('本地 key 原样返回（peerId 为空）',
         (peer_none, local_plain) == ('', 'devA:tid=2'), f'{peer_none} / {local_plain}'),
    ]


def _check_execute_guards(_root) -> list:
    empty = relay_node.execute('adb', {'transportId': 2}, [])
    blank_shell = relay_node.execute('adb', {'transportId': 2}, ['shell'])
    bad_adb = relay_node.execute('definitely-not-an-adb-binary-xyz', {'transportId': 2},
                                 ['shell', 'getprop ro.product.model'])
    return [
        ('空命令被拒', bool(empty.error), str(empty.error)),
        ('空 shell 被拒', blank_shell.error == 'shell 命令为空', str(blank_shell.error)),
        ('adb 不存在时给出可读错误而不抛异常',
         bad_adb.error is not None and '无法启动 adb' in bad_adb.error, str(bad_adb.error)),
        ('选择符优先用 transport_id',
         relay_node.device_selector_args({'transportId': 2}) == ['-t', '2'], ''),
        ('无 transport_id 时退回 -s serial',
         relay_node.device_selector_args({'serial': 'devA'}) == ['-s', 'devA'], ''),
        ('无设备时不给选择符', relay_node.device_selector_args(None) == [], ''),
    ]


# ----------------------------------------------------------------------
# 从机准入（不需要启动节点）
# ----------------------------------------------------------------------
def _check_authorize_guard(root) -> list:
    node = _make_node(root, 'pc-b', 'slave', 'node-b')
    now = relay.now_iso()
    node._peers = {
        'pc-a': relay.PeerHeartbeat(peerId='pc-a', role='master', heartbeatAt=now, fingerprint='fp-pc-a'),
        'pc-b': relay.PeerHeartbeat(peerId='pc-b', role='slave', heartbeatAt=now),
    }

    def request_from(peer_id):
        return relay.new_request(
            relay.KIND_EXEC, peer_id, {'key': 'devB:tid=1'},
            {'argv': ['shell', 'getprop ro.product.model']},
        )

    before = node._authorize(request_from('pc-a'))
    node.confirm_peer('pc-a')
    after = node._authorize(request_from('pc-a'))
    stranger = node._authorize(request_from('pc-c'))
    illegal = node._authorize(request_from('../escape'))
    itself = node._authorize(request_from('pc-b'))
    empty_peer = node._authorize(request_from(''))

    # 退役机器（曾作主控）的残留心跳：`peers/` 目录没有清理机制，它会一直留在共享目录里
    # 并继续声明自己为主控。若把它算进冲突判定，从机会永久拒掉每个请求，而用户被告知去改
    # 一台已经关机的机器 —— 自救无门。这里固定住「残留不影响在跑的主控」。
    node._peers = {
        'old-pc': relay.PeerHeartbeat(
            peerId='old-pc', role='master', heartbeatAt='2020-01-01T00:00:00+00:00', fingerprint='fp-old',
        ),
        'pc-a': relay.PeerHeartbeat(peerId='pc-a', role='master', heartbeatAt=now, fingerprint='fp-pc-a'),
        'pc-b': relay.PeerHeartbeat(peerId='pc-b', role='slave', heartbeatAt=now),
    }
    with_retired = node._authorize(request_from('pc-a'))
    retired_health = node._compute_health()

    return [
        ('未确认的主控被拒绝', bool(before) and '未确认' in before, before),
        ('确认后的主控放行', after == '', after),
        ('未出现在心跳里的来源被拒绝', bool(stranger), stranger),
        ('非法来源标识被拒绝', bool(illegal), illegal),
        ('来源标识与本机相同的请求被拒绝', bool(itself), itself),
        ('缺少来源标识的请求被拒绝', bool(empty_peer), empty_peer),
        ('退役主控的残留心跳不会让从机拒掉在跑主控的请求',
         with_retired == '', with_retired),
        ('退役主控只在状态里单独列出，不影响冲突结论',
         retired_health.staleMasters == ['old-pc'] and retired_health.conflict is False,
         retired_health.hint),
    ]


# ----------------------------------------------------------------------
# 端到端：一主一从
# ----------------------------------------------------------------------
def _check_single_peer_end_to_end(root) -> list:
    checks: list = []
    _EXEC_LOG.clear()
    with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        master = _make_node(root, 'pc-a', 'master', 'node-a')
        slave = _make_node(root, 'pc-b', 'slave', 'node-b')
        started_m, reason_m = master.start()
        started_s, reason_s = slave.start()
        checks.append(('主控节点启动成功', started_m, reason_m))
        checks.append(('从机节点启动成功', started_s, reason_s))
        try:
            found_m = master.wait_for_peers(timeout=8, count=1)
            found_s = slave.wait_for_peers(timeout=8, count=1)
            checks.append(('主控发现从机心跳', found_m, master.health().hint))
            checks.append(('从机发现主控心跳', found_s, slave.health().hint))

            # 建立连接时应先提示为主还是为从，且未确认前不下发命令。
            pending = master.pending_confirmations()
            checks.append(('未确认对端计入待确认名单', pending == ['pc-b'], str(pending)))
            checks.append(('主控状态为等待确认',
                           master.refresh().state == relay.STATE_PENDING_CONFIRM,
                           master.health().state))
            checks.append(('从机状态为等待确认',
                           slave.refresh().state == relay.STATE_PENDING_CONFIRM,
                           slave.health().state))

            refused = master.exec_remote('pc-b', 'devB:tid=1', ['shell', 'getprop ro.product.model'], timeout=3)
            checks.append(('未确认时下发命令被从机拒绝',
                           refused.status == relay.STATUS_REJECTED and '未确认' in (refused.error or ''),
                           f'{refused.status} / {refused.error}'))
            checks.append(('被拒绝的请求没有真正执行',
                           not [item for item in _EXEC_LOG if item['node'] == 'node-b'], ''))

            master.confirm_peer('pc-b')
            slave.confirm_peer('pc-a')
            checks.append(('确认后主控状态转为已连接',
                           _wait_until(lambda: master.refresh().state == relay.STATE_CONNECTED, 5),
                           master.health().state))
            checks.append(('确认后从机状态转为已连接',
                           _wait_until(lambda: slave.refresh().state == relay.STATE_CONNECTED, 5),
                           slave.health().state))

            all_devices = master.all_devices()
            local = [item for item in all_devices if item['origin'] == 'local']
            remote = [item for item in all_devices if item['origin'] == 'relay']
            checks.append(('主控设备总览 = 本地 1 台 + 远端 1 台',
                           len(local) == 1 and len(remote) == 1, str([d['key'] for d in all_devices])))
            checks.append(('远端设备 key 带 relay:<peerId>: 前缀',
                           remote and remote[0]['key'] == 'relay:pc-b:devB:tid=1',
                           str(remote[0]['key']) if remote else ''))
            checks.append(('远端设备附带来源与在线信息',
                           bool(remote) and remote[0]['peerId'] == 'pc-b', ''))

            notice = master.link_notice('pc-b')
            checks.append(('首连提示写出双方角色与指纹',
                           all(token in notice for token in ('主控', '从机', 'fp-pc-a', 'fp-pc-b')),
                           notice.replace('\n', ' | ')))

            # 按 key 自动选路：本地设备直连，远端设备经从机。
            local_out = master.exec_any('devA:tid=2', ['shell', 'getprop ro.product.model'])
            checks.append(('主控直接管理本机设备（不经中转）',
                           local_out.ok and 'node-a|devA:tid=2|getprop ro.product.model' in local_out.stdout,
                           local_out.stdout))

            remote_result = master.exec_remote('pc-b', 'devB:tid=1', ['shell', 'getprop ro.product.model'], timeout=6)
            checks.append(('经从机执行远端设备命令并回传结果',
                           remote_result.ok
                           and remote_result.agentId == 'pc-b'
                           and 'node-b|devB:tid=1|getprop ro.product.model' in remote_result.stdout,
                           f'{remote_result.status} / {remote_result.stdout!r} / {remote_result.error}'))
            checks.append(('结果里带退出码与耗时',
                           remote_result.exitCode == 0 and remote_result.durationMs >= 0,
                           f'{remote_result.exitCode} / {remote_result.durationMs}'))
            checks.append(('shell 命令经自动选路走到从机',
                           master.exec_any('relay:pc-b:devB:tid=1', ['shell', 'ls -l /sdcard']).ok, ''))

            denied = master.exec_remote('pc-b', 'devB:tid=1', ['push', '/a', '/b'], timeout=6)
            checks.append(('非白名单子命令被从机拒绝',
                           denied.status == relay.STATUS_REJECTED and '白名单' in (denied.error or ''),
                           f'{denied.status} / {denied.error}'))

            missing = master.exec_remote('pc-b', 'devX:tid=9', ['shell', 'getprop ro.build.version.sdk'], timeout=6)
            checks.append(('请求不存在的设备时明确报错',
                           missing.status == relay.STATUS_ERROR and '没有匹配的设备' in (missing.error or ''),
                           f'{missing.status} / {missing.error}'))

            queue = relay.RelayQueue.for_peer(os.path.join(root, 'share'), 'pc-b')
            checks.append(('两端各自归档已消费的消息，活跃目录清空',
                           _wait_until(lambda: not queue.pending_requests(), 4)
                           and queue.read_result(remote_result.reqId) is None, ''))
        finally:
            master.stop()
            slave.stop()
    return checks


# ----------------------------------------------------------------------
# 一主多从：请求必须落到正确分区
# ----------------------------------------------------------------------
def _check_multi_peer_partition(root) -> list:
    checks: list = []
    _EXEC_LOG.clear()
    with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        master = _make_node(root, 'pc-a', 'master', 'node-a')
        slave_b = _make_node(root, 'pc-b', 'slave', 'node-b')
        slave_c = _make_node(root, 'pc-c', 'slave', 'node-c')
        for node in (master, slave_b, slave_c):
            node.start()
        try:
            master.wait_for_peers(timeout=8, count=2)
            for peer in ('pc-b', 'pc-c'):
                master.confirm_peer(peer)
            slave_b.confirm_peer('pc-a')
            slave_c.confirm_peer('pc-a')
            _wait_until(
                lambda: len([v for v in master.refresh().peers if v.online and v.role == relay.ROLE_SLAVE]) == 2,
                6,
            )

            devices = master.all_devices()
            remote_keys = sorted(item['key'] for item in devices if item['origin'] == 'relay')
            checks.append(('主控看到两台从机的设备',
                           remote_keys == ['relay:pc-b:devB:tid=1', 'relay:pc-c:devC:tid=0'],
                           str(remote_keys)))

            result_b = master.exec_remote('pc-b', 'devB:tid=1', ['shell', 'getprop ro.product.model'], timeout=6)
            result_c = master.exec_remote('pc-c', 'devC:tid=0', ['shell', 'getprop ro.product.model'], timeout=6)
            checks.append(('发往 pc-b 的请求由 pc-b 执行',
                           result_b.ok and result_b.agentId == 'pc-b' and 'node-b|' in result_b.stdout,
                           f'{result_b.agentId} / {result_b.stdout!r}'))
            checks.append(('发往 pc-c 的请求由 pc-c 执行',
                           result_c.ok and result_c.agentId == 'pc-c' and 'node-c|' in result_c.stdout,
                           f'{result_c.agentId} / {result_c.stdout!r}'))
            from_b = [item for item in _EXEC_LOG if item['node'] == 'node-b']
            from_c = [item for item in _EXEC_LOG if item['node'] == 'node-c']
            checks.append(('两台从机各执行且只执行自己那一条',
                           len(from_b) == 1 and len(from_c) == 1, f'{len(from_b)} / {len(from_c)}'))

            # 从机之间不能互相冒名下发。
            cross = relay.RelayQueue.for_peer(os.path.join(root, 'share'), 'pc-c')
            forged = relay.new_request(
                relay.KIND_EXEC, 'pc-b', {'key': 'devC:tid=0'}, {'argv': ['shell', 'getprop ro.build.version.sdk']},
            )
            cross.submit(forged)
            _wait_until(lambda: cross.read_result(forged.reqId) is not None, 5)
            forged_result = cross.read_result(forged.reqId)
            checks.append(('从机以另一从机身份下发的请求被拒绝',
                           forged_result is not None
                           and forged_result.status == relay.STATUS_REJECTED,
                           str(forged_result)))
        finally:
            for node in (master, slave_b, slave_c):
                node.stop()
    return checks


# ----------------------------------------------------------------------
# 从机离线：设备保留灰显 + 新操作 fail-closed
# ----------------------------------------------------------------------
def _check_device_miss_debounce(root) -> list:
    """设备枚举抖动不能当成「设备真的没了」。

    ``adb devices -l`` 在 adb server 忙碌时会瞬时返回空列表（实机复现过），若直接
    采信心跳就会把从机的设备数报成 0，对端设备栏会凭空少一台再冒出来。
    """
    node = _make_node(root, 'pc-b', 'slave', 'node-b')
    real = FAKE_DEVICES['node-b']
    outcomes = []

    def fake(adb_path, timeout=8.0):
        item = outcomes.pop(0)
        if item == 'error':
            return [], 'adb server 忙'
        return [dict(d) for d in item], ''

    with _patch(list_devices=fake):
        # 1 首次枚举：空列表 —— 此前未知，就应该是空。
        outcomes[:] = [[]]
        node._refresh_devices()
        checks_initial_empty = node._devices == []

        # 2 正常枚举一次，设备出现。
        outcomes[:] = [real]
        node._refresh_devices()
        adopted = [d['key'] for d in node._devices]

        # 3 连续两次瞬时为空：设备必须还在。
        outcomes[:] = [[], []]
        node._refresh_devices()
        after_one = [d['key'] for d in node._devices]
        node._refresh_devices()
        after_two = [d['key'] for d in node._devices]

        # 4 第三次仍为空：达到阈值，才移除。
        outcomes[:] = [[]]
        node._refresh_devices()
        after_three = [d['key'] for d in node._devices]

        # 5 枚举报错不能计 miss：重新采纳后，报错多少次都还在。
        outcomes[:] = [real]
        node._refresh_devices()
        outcomes[:] = ['error', 'error', 'error', 'error']
        for _ in range(4):
            node._refresh_devices()
        after_errors = [d['key'] for d in node._devices]

    return [
        ('首次枚举为空时不报错也不留残影', checks_initial_empty, str(node._devices)),
        ('正常枚举后采纳设备', adopted == ['devB:tid=1'], str(adopted)),
        ('单次瞬时为空不移除设备（去抖 1/3）', after_one == ['devB:tid=1'], str(after_one)),
        ('两次瞬时为空仍不移除（去抖 2/3）', after_two == ['devB:tid=1'], str(after_two)),
        ('连续三次未出现才按离线移除（去抖 3/3）', after_three == [], str(after_three)),
        ('枚举报错不计入 miss（报错期间设备不丢）',
         after_errors == ['devB:tid=1'], str(after_errors)),
    ]


def _check_peer_offline_view(root) -> list:
    """从机离线时，远端设备应与本地设备同构（留在列表里标记离线），而不是消失。"""
    checks: list = []
    with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        master = _make_node(root, 'pc-a', 'master', 'node-a')
        slave_b = _make_node(root, 'pc-b', 'slave', 'node-b')
        master.start()
        slave_b.start()
        try:
            master.wait_for_peers(timeout=8, count=1)
            slave_b.confirm_peer('pc-a')
            master.confirm_peer('pc-b')
            online = _wait_until(
                lambda: any(v.peerId == 'pc-b' and v.online for v in master.refresh().peers), 8,
            )
            online_devices = [item for item in master.remote_devices() if item['peerId'] == 'pc-b']
            checks.append(('在线时远端设备带 relay.online=True',
                           online and len(online_devices) == 1
                           and online_devices[0]['relay']['online'] is True
                           and online_devices[0]['relay']['peerId'] == 'pc-b',
                           str(online_devices)))

            # 从机下线：心跳 3 次未刷新即判离线（测试内间隔 0.6s）
            slave_b.stop()
            gone = _wait_until(
                lambda: any(v.peerId == 'pc-b' and not v.online for v in master.refresh().peers), 15,
            )
            lingered = [item for item in master.all_devices() if item['origin'] == 'relay']
            checks.append(('离线后远端设备仍留在列表里（供前端灰显，与本地设备同构）',
                           gone and len(lingered) == 1
                           and lingered[0]['key'] == 'relay:pc-b:devB:tid=1'
                           and lingered[0]['relay']['online'] is False,
                           str(lingered)))
            checks.append(('离线设备的 key 与 localKey 仍可无损还原',
                           bool(lingered) and lingered[0]['localKey'] == 'devB:tid=1'
                           and relay_node.split_remote_key(lingered[0]['key']) == ('pc-b', 'devB:tid=1'),
                           ''))
            checks.append(('离线设备的 ageMs 被上报（前端据此显示掉线时长）',
                           bool(lingered) and isinstance(lingered[0]['relay']['ageMs'], float)
                           and lingered[0]['relay']['ageMs'] > 0,
                           str(lingered[0]['relay'] if lingered else None)))

            checks.append(('超过保留窗口后不再列出（避免永久幽灵设备）',
                           master.remote_devices(linger_s=0) == [],
                           str(master.remote_devices(linger_s=0))))

            # 新操作必须快速失败，不能投出去干等到超时
            started = time.time()
            refused = master.exec_remote('pc-b', 'devB:tid=1', ['shell', 'id'], timeout=5)
            elapsed = time.time() - started
            checks.append(('对离线从机的操作快速失败并给出中文原因',
                           elapsed < 2.0 and not refused.ok and '离线' in (refused.error or ''),
                           f'{elapsed:.2f}s / {refused.error}'))
            checks.append(('快速失败的请求确实没有投递到共享目录（fail-closed）',
                           refused.extra.get('peerOffline') is True
                           and relay.RelayQueue.for_peer(master.root, 'pc-b').pending_requests() == [],
                           str(refused.extra)))
            checks.append(('离线期间该从机的设备没有被真正执行过命令',
                           not [item for item in _EXEC_LOG if item['node'] == 'node-b'
                                and item['argv'][-1] == 'id'],
                           str(_EXEC_LOG[-3:])))
        finally:
            for node in (master, slave_b):
                node.stop()
    return checks


# ----------------------------------------------------------------------
# 过期作废 与 幂等
# ----------------------------------------------------------------------
def _check_expired_and_idempotent(root) -> list:
    checks: list = []
    _EXEC_LOG.clear()
    with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
        slave = _make_node(root, 'pc-b', 'slave', 'node-b')
        slave.start()
        try:
            queue = relay.RelayQueue.for_peer(os.path.join(root, 'share'), 'pc-b')

            stale = relay.new_request(
                relay.KIND_EXEC, 'pc-a', {'key': 'devB:tid=1'}, {'argv': ['shell', 'reboot']}, ttl_s=30,
            )
            past = (datetime.now().astimezone() - timedelta(minutes=5))
            stale.createdAt = past.isoformat(timespec='milliseconds')
            stale.expiresAt = (past + timedelta(seconds=1)).isoformat(timespec='milliseconds')
            queue.submit(stale)
            got = _wait_until(lambda: queue.read_result(stale.reqId) is not None, 6)
            result = queue.read_result(stale.reqId)
            checks.append(('过期请求被判为 expired 而非执行',
                           got and result is not None and result.status == relay.STATUS_EXPIRED,
                           str(result.status if result else 'no result')))
            checks.append(('过期请求确实没有被执行', _EXEC_LOG == [], str(_EXEC_LOG)))
            checks.append(('过期请求的文件被归档',
                           _wait_until(lambda: not queue.pending_requests(), 4), ''))

            # 幂等：结果已存在（主控超时重投，或从机上次处理到一半崩了）时
            # 请求只被归档，绝不重复执行。
            duplicate = relay.new_request(
                relay.KIND_EXEC, 'pc-a', {'key': 'devB:tid=1'}, {'argv': ['shell', 'reboot']},
            )
            queue.write_result(relay.Result(
                reqId=duplicate.reqId, status=relay.STATUS_OK, stdout='preexisting', agentId='pc-b',
            ))
            before = [item for item in _EXEC_LOG if item['node'] == 'node-b']
            queue.submit(duplicate)
            _wait_until(lambda: not queue.pending_requests(), 6)
            after = [item for item in _EXEC_LOG if item['node'] == 'node-b']
            kept = queue.read_result(duplicate.reqId)
            checks.append(('结果已存在时请求被归档且不重复执行',
                           len(after) == len(before) and not queue.pending_requests(),
                           f'{len(before)} → {len(after)} 次执行'))
            checks.append(('已有结果不被新执行覆盖',
                           kept is not None and kept.stdout == 'preexisting', str(kept)))
        finally:
            slave.stop()
    return checks


# ----------------------------------------------------------------------
# 日志透传：单向追加 + 增量读
# ----------------------------------------------------------------------
def _check_log_stream(root) -> list:
    checks: list = []
    _EXEC_LOG.clear()
    with _patch(list_devices=_fake_list_devices, execute=_fake_execute,
                **{'subprocess.Popen': _fake_popen}):
        master = _make_node(root, 'pc-a', 'master', 'node-a')
        slave = _make_node(root, 'pc-b', 'slave', 'node-b')
        master.start()
        slave.start()
        try:
            master.wait_for_peers(timeout=8, count=1)
            master.confirm_peer('pc-b')
            slave.confirm_peer('pc-a')
            _wait_until(lambda: master.refresh().state == relay.STATE_CONNECTED, 5)

            started = master.start_remote_log(
                'pc-b', 'devB:tid=1', ['logcat', '-v', 'brief'], 'log-1', timeout=6,
            )
            checks.append(('从机接受日志流请求并回报流标识',
                           started.ok and started.extra.get('streamId') == 'log-1',
                           f'{started.status} / {started.extra}'))

            stream_out = relay.RelayQueue.for_peer(
                os.path.join(root, 'share'), 'pc-b',
            ).stream('log-1').out_path
            joined = b''.join(LOG_CHUNKS)

            # 探测只用文件大小。绝不能用 read_remote_stream 来「等数据到」——
            # 它会推进游标，把要断言的第一段数据提前消费掉。
            _wait_until(lambda: _size_of(stream_out) > 0, 5)
            data1, offset1, rebuilt1 = master.read_remote_stream('pc-b', 'log-1', 64)
            checks.append(('主控按字节偏移读到远端日志且内容连续',
                           len(data1) > 0 and offset1 == len(data1) and joined.startswith(data1),
                           f'offset={offset1} data={data1!r}'))
            checks.append(('首次读不触发流重建判定', rebuilt1 is False, str(rebuilt1)))

            _wait_until(lambda: _size_of(stream_out) > offset1, 5)
            data2, offset2, _ = master.read_remote_stream('pc-b', 'log-1', 64)
            checks.append(('再读只拿到增量且游标单调前进',
                           offset2 > offset1 and joined[offset1:offset2] == data2,
                           f'{offset1} → {offset2} data={data2!r}'))
            checks.append(('两次读到的内容不重复', data1 != data2, f'{data1!r} / {data2!r}'))

            meta = master.read_remote_stream_meta('pc-b', 'log-1')
            checks.append(('流元信息由从机写入且主控可读',
                           bool(meta) and meta.get('streamId') == 'log-1'
                           and meta.get('deviceKey') == 'devB:tid=1', str(meta)))

            stopped = master.stop_remote_log('pc-b', 'log-1', timeout=6)
            checks.append(('停止流并回报累计字节数',
                           stopped.ok and stopped.extra.get('bytesWritten', 0) > 0,
                           f'{stopped.status} / {stopped.extra}'))
            checks.append(('停止后元信息转为已停止',
                           _wait_until(
                               lambda: (master.read_remote_stream_meta('pc-b', 'log-1') or {}).get('status')
                               in ('stopped', 'ended'), 4,
                           ),
                           str(master.read_remote_stream_meta('pc-b', 'log-1'))))

            bad = master.start_remote_log('pc-b', 'devB:tid=1', ['logcat'], '../escape', timeout=5)
            checks.append(('非法流标识被本地拦下',
                           bad.status == relay.STATUS_REJECTED and '非法' in (bad.error or ''),
                           f'{bad.status} / {bad.error}'))
            follow_up = master.exec_remote('pc-b', 'devB:tid=1', ['shell', 'getprop ro.product.model'], timeout=5)
            checks.append(('非法流标识不影响后续正常请求', follow_up.ok, str(follow_up.status)))
        finally:
            master.stop()
            slave.stop()
    return checks


def run() -> bool:
    checks: list = []
    tmp = tempfile.mkdtemp(prefix='relay-node-test-')
    probes = (
        _check_device_parse,
        _check_exit_marker,
        _check_truncate,
        _check_remote_key,
        _check_execute_guards,
        _check_authorize_guard,
        _check_single_peer_end_to_end,
        _check_multi_peer_partition,
        _check_device_miss_debounce,
        _check_peer_offline_view,
        _check_expired_and_idempotent,
        _check_log_stream,
    )
    try:
        for index, probe in enumerate(probes):
            # 每个用例一个独立目录。信任名单是**持久**的（TOFU 语义），
            # 共用目录会让上一个用例的确认结果串进下一个用例。
            checks += probe(os.path.join(tmp, f'{index:02d}-{probe.__name__}'))
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
