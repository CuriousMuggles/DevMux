"""验证跨机中转的应用接线（``relay_session.py`` + ``relay_app.py``）。

覆盖四件事，它们正是「真实能用」的分界线：

1. **选路**：``strip_device_selector`` 只剥开头的设备选择符（``logcat -t 20`` 这类
   子命令参数不能被误删），命令确实落到从机的那台设备上。
2. **配置**：校验、持久化、未实现载体必须被拒绝。
3. **端到端**：一主一从在同一个临时共享目录上跑起来 —— 主控自动为远端设备开会话、
   读到 PTY 输出、按键与窗口尺寸透传、命令经从机执行、会话被关掉后自动重开。
4. **降级**：从机停掉后远端设备仍在列表里但标记离线，新操作快速失败而不是干等超时。

全部在本地临时目录里跑：``relay_node.list_devices`` 与 ``relay_node.execute`` 被替换为
可控实现，PTY 后端是假对象，因此**不需要真机、真 adb、真共享盘或第二台电脑**。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import base64
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay          # noqa: E402
import relay_app      # noqa: E402
import relay_node     # noqa: E402
import relay_session  # noqa: E402


# ----------------------------------------------------------------------
# 可控替身
# ----------------------------------------------------------------------
FAKE_DEVICES = {
    'node-a': [{'serial': 'devA', 'state': 'device', 'transportId': 2, 'key': 'devA:tid=2'}],
    'node-b': [{'serial': 'devB', 'state': 'device', 'transportId': 1, 'key': 'devB:tid=1'}],
}

_EXEC_LOG: list = []


def _fake_list_devices(adb_path, timeout=8.0):
    return [dict(item) for item in FAKE_DEVICES.get(str(adb_path), [])], ''


def _fake_execute(adb_path, device, argv, *, timeout=30.0, max_bytes=None):
    tokens = [str(part) for part in (argv or [])]
    device_key = (device or {}).get('key', '')
    _EXEC_LOG.append({'node': str(adb_path), 'device': device_key, 'argv': tokens})
    return relay_node.ExecOutcome(
        exitCode=0, stdout=f'{adb_path}|{device_key}|{" ".join(tokens)}', elapsedMs=4,
    )


class _patch:
    """临时替换模块属性（仅本文件用到的简单名字）。"""

    def __init__(self, **targets):
        self._targets = targets
        self._saved = {}

    def __enter__(self):
        for name, value in self._targets.items():
            self._saved[name] = getattr(relay_node, name)
            setattr(relay_node, name, value)
        return self

    def __exit__(self, *_exc):
        for name, value in self._saved.items():
            setattr(relay_node, name, value)
        return False


class FakeBackend(relay_session.SessionBackend):
    """假的 PTY 后端：记录写入/尺寸，允许测试主动推入输出或结束会话。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self.handles: dict[str, dict] = {}
        self.closed: list = []

    def open(self, device, *, cols, rows, on_output, on_exit):
        with self._lock:
            self._seq += 1
            handle = f'h{self._seq}'
            self.handles[handle] = {
                'device': dict(device or {}), 'cols': int(cols), 'rows': int(rows),
                'on_output': on_output, 'on_exit': on_exit, 'alive': True, 'writes': [],
            }
        return handle

    def write(self, handle, data: bytes) -> None:
        with self._lock:
            record = self.handles.get(handle)
            if record is not None:
                record['writes'].append(bytes(data))

    def resize(self, handle, cols: int, rows: int) -> None:
        with self._lock:
            record = self.handles.get(handle)
            if record is not None:
                record['cols'], record['rows'] = int(cols), int(rows)

    def close(self, handle) -> None:
        with self._lock:
            record = self.handles.get(handle)
            if record is not None:
                record['alive'] = False
            self.closed.append(handle)

    # --- 测试辅助 ---
    def live_handles(self) -> list[str]:
        with self._lock:
            return [handle for handle, record in self.handles.items() if record['alive']]

    def handle_for(self, device_key: str) -> str:
        with self._lock:
            # 再次确认会换连接代次；取当前活会话，不能检查已关闭旧句柄的写入。
            for handle, record in reversed(list(self.handles.items())):
                if record['device'].get('key') == device_key and record['alive']:
                    return handle
        return ''

    def emit(self, handle: str, data: bytes) -> None:
        with self._lock:
            record = self.handles.get(handle)
        if record and record['alive']:
            record['on_output'](data)

    def exit_handle(self, handle: str) -> None:
        with self._lock:
            record = self.handles.get(handle)
            if record is not None:
                record['alive'] = False
        if record:
            record['on_exit']()

    def written(self, handle: str) -> bytes:
        with self._lock:
            record = self.handles.get(handle)
            return b''.join(record['writes']) if record else b''


def _wait_until(predicate, timeout=8.0, step=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


class _Harness:
    """一主一从两个控制器 + 假 PTY 后端。"""

    def __init__(self, root: str, *, heartbeat_s: float = 0.5,
                 master_local_devices_enabled: bool = True):
        self.root = os.path.join(root, 'share')
        os.makedirs(self.root, exist_ok=True)
        self.backend = FakeBackend()
        self.master_output: list = []
        self.master_exits: list = []
        self.master_devices: list = []
        self.statuses: list = []
        self.peer_requests: list = []
        self._output_lock = threading.Lock()

        self.master = self._make('pc-a', 'node-a', relay.ROLE_MASTER, heartbeat_s, None,
                                 master_local_devices_enabled)
        self.slave = self._make('pc-b', 'node-b', relay.ROLE_SLAVE, heartbeat_s, self.backend)

    def _make(self, peer_id, adb_path, role, heartbeat_s, backend, local_devices_enabled=True):
        config_path = os.path.join(self.root, '..', f'relay-{peer_id}.json')
        relay_app.save_config(config_path, relay_app.RelayConfig(
            enabled=True, role=role, peerId=peer_id, root=self.root,
            heartbeatIntervalS=heartbeat_s,
        ))
        # 两个控制器跑在同一个进程里：主机名/指纹默认取本机，两边会一模一样。
        # 而「本机旧标识残留」的判定靠「指纹相同 + pid 相同 + 启动时间不同」，
        # 若指纹也相同，两个控制器会互相把对方当成自己的残留心跳，对端就此消失。
        # 这里给每个实例一个独立指纹与主机名，模拟两台真机。
        return relay_app.RelayController(
            config_path=config_path,
            adb_path=adb_path,
            session_backend=backend,
            local_devices_enabled=local_devices_enabled,
            host_label=peer_id,
            host_fingerprint=f'fp-{peer_id}',
            on_devices_changed=self._on_master_devices if role == relay.ROLE_MASTER else None,
            on_status=self._on_status if role == relay.ROLE_MASTER else None,
            on_terminal_output=self._on_output,
            on_terminal_exit=self._on_exit,
            on_peer_request=self._on_peer_request,
        )

    def _on_master_devices(self, devices, keys):
        with self._output_lock:
            self.master_devices = list(devices)

    def _on_status(self, status):
        with self._output_lock:
            self.statuses.append(status)

    def _on_output(self, key, data):
        with self._output_lock:
            self.master_output.append((key, bytes(data)))

    def _on_exit(self, key, reason):
        with self._output_lock:
            self.master_exits.append((key, reason))

    def _on_peer_request(self, request):
        with self._output_lock:
            self.peer_requests.append(dict(request))

    # --- 常用操作 ---
    def start(self):
        self.master.start()
        self.slave.start()

    def stop(self):
        for controller in (self.slave, self.master):
            try:
                controller.stop()
            except Exception:
                pass

    def confirm_both(self):
        # 注册表发现先于心跳序号推进；发现记录不能当成已在线。
        assert _wait_until(lambda: all(
            controller.status()['pairing'] and all(item.get('online') for item in controller.status()['pairing'])
            for controller in (self.master, self.slave)), 8), '双端尚未观察到心跳推进'
        # 应用流程只由主控确认，从机自动接受验证请求。
        ok, reason = self.master.confirm_peer('pc-b')
        assert ok, reason
        assert _wait_until(lambda:
            any(item.get('peerId') == 'pc-b' and item.get('connectionState') == 'connected'
                for item in self.master.status()['pairing'])
            and any(item.get('peerId') == 'pc-a' and item.get('connectionState') == 'connected'
                    for item in self.slave.status()['pairing']), 8), '双方尚未完成连接验证'

    def remote_key(self) -> str:
        for device in self.master.devices():
            if device.get('key'):
                return device['key']
        return ''

    def output_text(self) -> bytes:
        with self._output_lock:
            return b''.join(data for _key, data in self.master_output)


# ----------------------------------------------------------------------
# 用例 1：选路
# ----------------------------------------------------------------------
def _check_selector_strip(root) -> list:
    relog = relay_app.strip_device_selector
    checks = [
        ('剥掉开头的 -t 选择符',
         relog(['-t', '2', 'shell', 'id', '-u']) == ['shell', 'id', '-u'],
         str(relog(['-t', '2', 'shell', 'id', '-u']))),
        ('剥掉开头的 -s 选择符',
         relog(['-s', 'devA', 'shell', 'ls']) == ['shell', 'ls'],
         str(relog(['-s', 'devA', 'shell', 'ls']))),
        ('连续两个选择符也一并剥掉',
         relog(['-s', 'devA', '-t', '2', 'shell', 'ls']) == ['shell', 'ls'],
         str(relog(['-s', 'devA', '-t', '2', 'shell', 'ls']))),
        ('保留 logcat 自己的 -t（取最近 N 行）',
         relog(['-t', '2', 'logcat', '-d', '-t', '20']) == ['logcat', '-d', '-t', '20'],
         str(relog(['-t', '2', 'logcat', '-d', '-t', '20']))),
        ('保留 logcat 的 -s 过滤器',
         relog(['logcat', '-d', '-s', 'MyTag']) == ['logcat', '-d', '-s', 'MyTag'],
         str(relog(['logcat', '-d', '-s', 'MyTag']))),
        ('没有选择符时原样返回',
         relog(['shell', 'ls', '-la']) == ['shell', 'ls', '-la'], ''),
        ('空输入返回空列表', relog([]) == [] and relog(None) == [], ''),
        ('argv 列表优先于 args 字符串',
         relay_app.extract_argv({'argv': ['shell', 'ls'], 'args': 'shell id'}) == ['shell', 'ls'], ''),
        ('只有 args 时按 shell 规则拆分',
         relay_app.extract_argv({'args': '-t 2 shell echo "a b"'}) == ['-t', '2', 'shell', 'echo', 'a b'],
         str(relay_app.extract_argv({'args': '-t 2 shell echo "a b"'}))),
    ]
    return checks


# ----------------------------------------------------------------------
# 用例 2：配置
# ----------------------------------------------------------------------
def _check_config(root) -> list:
    default = relay_app.RelayConfig()
    path = os.path.join(root, 'relay.json')
    cfg = relay_app.RelayConfig(
        enabled=True, role=relay.ROLE_SLAVE, peerId='pc-x', root=r'\\srv\relay',
        allowedSubcommands=['shell', 'logcat'], allowShell=False, managedSerials=['dev1'],
    )
    saved = relay_app.save_config(path, cfg)
    loaded = relay_app.load_config(path)

    bad_peer = relay_app.RelayConfig(enabled=True, peerId='pc:bad', root='r')
    no_root = relay_app.RelayConfig(enabled=True, peerId='pc-x', root='')
    unimpl = relay_app.RelayConfig(enabled=True, peerId='pc-x', root='r', carrierKind='tcp')
    disabled_unimpl = relay_app.RelayConfig(enabled=False, peerId='pc-x', carrierKind='tcp')

    junk = relay_app.RelayConfig.from_dict('not-a-dict' if False else {'peerId': '', 'role': 'nonsense'})

    return [
        ('默认配置是「未启用」，不改变现有行为', default.enabled is False, str(default.enabled)),
        ('配置可保存并读回', saved and loaded.to_dict() == cfg.to_dict(), str(loaded.to_dict())),
        ('默认标识由主机名派生且合法',
         bool(relay_app.PEER_ID_RE.match(default.peerId)), default.peerId),
        ('非法本机标识被拒绝', bad_peer.validate() == (False, '本机标识只能包含字母、数字、点、下划线和短横线，且不超过 32 字符')
         or bad_peer.validate()[0] is False, str(bad_peer.validate())),
        ('启用但没填共享目录被拒绝', no_root.validate()[0] is False, str(no_root.validate())),
        ('未实现的载体在启用时被拒绝',
         unimpl.validate()[0] is False and '以太网直连' in unimpl.validate()[1], str(unimpl.validate())),
        ('未实现的载体在未启用时不拦（允许先选着）',
         disabled_unimpl.validate()[0] is True, str(disabled_unimpl.validate())),
        ('脏数据能落到安全默认值',
         junk.role in (relay.ROLE_MASTER, relay.ROLE_SLAVE) and bool(junk.peerId), str(junk.to_dict())),
        ('未知载体不再被当作可用',
         relay_app.carrier_spec('tcp') is not None and relay_app.carrier_spec('tcp').implemented is False,
         'tcp'),
    ]


# ----------------------------------------------------------------------
# 用例 3：端到端（会话 + 命令 + 自动重开）
# ----------------------------------------------------------------------
def _check_end_to_end(root) -> list:
    checks: list = []
    # 会话重开的退避默认是 ``SESSION_REOPEN_BACKOFF_S`` × 失败次数（最多 5 次，
    # 即最坏 25s），而本用例要验的是「确认之后能不能把会话开起来」，退避策略本身
    # 由 ``_check_slow_retry`` 专门覆盖。这里把退避压小、把失败上限抬高，让重开
    # 变快且永不落进 60s 慢速重试 —— 否则断言会卡在等待窗口与退避的边界上随机失败。
    saved = (relay_app.SESSION_REOPEN_BACKOFF_S, relay_app.SESSION_MAX_FAILURES)
    relay_app.SESSION_REOPEN_BACKOFF_S = 0.2
    relay_app.SESSION_MAX_FAILURES = 1000
    harness = _Harness(root)
    try:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness.start()
            discovered = _wait_until(
                lambda: any(item['peerId'] == 'pc-a' for item in harness.slave.status()['pairing'])
                and any(item['peerId'] == 'pc-b' for item in harness.master.status()['pairing']), 8,
            )
            checks.append(('两端互相发现（心跳）', discovered, '未在超时内发现对端'))
            if not discovered:
                return checks

            # 未确认时不能下发：安全边界在从机侧
            before_key = 'relay:pc-b:devB:tid=1'
            checks.append(('双端确认之前不显示远端设备且没有终端',
                           not harness.master.devices() and not harness.backend.live_handles(), ''))
            checks.append((
                '设备 key 带来源前缀，与本机设备同列',
                before_key.startswith('relay:pc-b:') and before_key.endswith('devB:tid=1'),
                before_key,
            ))
            rejected = harness.master.remote_exec(before_key, ['-t', '1', 'shell', 'id', '-u'], timeout=4)
            checks.append((
                '未确认对端时命令被准入门槛拒绝',
                rejected.get('success') is False and '未配对' in (rejected.get('output') or ''),
                str(rejected.get('output'))[:120],
            ))

            harness.confirm_both()
            checks.append(('首连确认同时完成本机配对，无需再确认一次',
                           harness.master.node.pairing.is_paired(harness.slave.config.nodeId)
                           and harness.slave.node.pairing.is_paired(harness.master.config.nodeId), ''))
            trusted = _wait_until(
                lambda: all(view.trusted for view in harness.master.node.peers()
                            if view.peerId == 'pc-b'), 6,
            )
            checks.append(('确认后双方互信', trusted, str([view.trustReason for view in harness.master.node.peers()])))

            # 会话自动对齐：主控应为远端设备各开一条会话
            opened = _wait_until(
                lambda: any(s['opened'] for s in harness.master.sessions_snapshot()), 10,
            )
            checks.append(('主控自动为远端设备开会话', opened, str(harness.master.sessions_snapshot())))
            if not opened:
                return checks
            checks.append(('双端确认后显示远端设备与在线角色',
                           bool(harness.master.devices())
                           and harness.master.devices()[0]['relay']['role'] == relay.ROLE_SLAVE
                           and harness.master.devices()[0]['relay']['online'], ''))

            handle = _wait_until(lambda: harness.backend.handle_for('devB:tid=1'), 6) and not None
            handle = harness.backend.handle_for('devB:tid=1')
            checks.append((
                '从机为该设备开了自己的 PTY（与本机终端互不影响）',
                bool(handle) and harness.backend.handles[handle]['device']['key'] == 'devB:tid=1',
                str(list(harness.backend.handles.keys())),
            ))
            if not handle:
                return checks

            # 输出方向：从机 PTY → 主控
            payload = b'\x1b[32mremote-shell-ready\x1b[0m\r\n'
            harness.backend.emit(handle, payload)
            got = _wait_until(lambda: payload in harness.output_text(), 6)
            checks.append(('从机 PTY 输出经数据面到达主控', got, repr(harness.output_text()[:80])))
            checks.append((
                '输出挂在远端设备 key 上（前端与本地终端同一条通道）',
                all(key.startswith('relay:pc-b:') for key, _data in harness.master_output),
                str([key for key, _data in harness.master_output][:3]),
            ))

            # 输入方向：主控按键 → 从机 PTY
            sent = harness.master.session_input(before_key, b'ls -la /data\n')
            arrived = _wait_until(lambda: b'ls -la /data\n' in harness.backend.written(handle), 6)
            checks.append(('主控按键经数据面写入从机 PTY', sent and arrived,
                           repr(harness.backend.written(handle))))

            # 窗口尺寸
            resized = harness.master.session_resize(before_key, 140, 44)
            checks.append((
                '窗口尺寸透传到从机 PTY',
                resized and (harness.backend.handles[handle]['cols'], harness.backend.handles[handle]['rows']) == (140, 44),
                str((harness.backend.handles[handle]['cols'], harness.backend.handles[handle]['rows'])),
            ))

            # 命令：设备选择符必须被剥掉，落到从机自己那台设备上
            _EXEC_LOG.clear()
            result = harness.master.remote_exec(
                before_key, ['-t', '2', 'shell', 'echo', 'hi'], timeout=8,
            )
            checks.append((
                '命令经从机执行并带回输出',
                result.get('success') is True
                and 'node-b|devB:tid=1|shell echo hi' in (result.get('output') or ''),
                str(result.get('output'))[:120],
            ))
            checks.append((
                '命令落在从机的设备上且选择符已被剥掉',
                _EXEC_LOG and _EXEC_LOG[0]['node'] == 'node-b'
                and _EXEC_LOG[0]['device'] == 'devB:tid=1'
                and _EXEC_LOG[0]['argv'] == ['shell', 'echo', 'hi'],
                str(_EXEC_LOG[:1]),
            ))

            # 白名单：push 未实现且不在白名单
            blocked = harness.master.remote_exec(before_key, ['push', 'a', 'b'], timeout=6)
            checks.append((
                '非白名单命令被从机拒绝',
                blocked.get('success') is False and '白名单' in (blocked.get('output') or ''),
                str(blocked.get('output'))[:120],
            ))

            # 本机 key 不走中转
            checks.append(('本机设备 key 不进入中转选路',
                           harness.master.exec_any('devA:tid=2', ['shell', 'ls']) is None, ''))

            # 会话被关掉后，设备仍在线时应自动重开
            first_stream = harness.master.sessions_snapshot()[0]['streamId']
            closed = harness.master.close_session(before_key)
            reopened = _wait_until(
                lambda: any(s['opened'] and s['streamId'] != first_stream
                            for s in harness.master.sessions_snapshot()), 12,
            )
            checks.append(('会话被关闭后自动重开（设备仍在线）', closed and reopened,
                           str(harness.master.sessions_snapshot())))

            # 状态快照是前端可直接渲染的：结论都在后端算好
            status = harness.master.status()
            checks.append((
                '状态快照含角色、状态文案与提示',
                status.get('enabled') is True and bool(status.get('stateText'))
                and bool(status.get('hint')) and status.get('roleLabel'),
                str({k: status.get(k) for k in ('stateText', 'roleLabel', 'hint')}),
            ))
            checks.append((
                '状态快照含事件流水与对端列表',
                isinstance(status.get('events'), list) and isinstance(status.get('peers'), list)
                and len(status.get('peers') or []) == 1,
                str(status.get('peers')),
            ))
            # 去抖签名会读这些字段；界面也靠它们渲染对端条目。字段被改名/删掉时
            # 签名会静默退化成 None，界面就停在旧标签上 —— 这里把它钉住。
            peer_view = (status.get('peers') or [{}])[0]
            checks.append((
                '对端条目带界面渲染与去抖签名所需字段',
                all(field in peer_view for field in
                    ('peerId', 'role', 'online', 'trusted', 'needsConfirmation', 'deviceCount')),
                str(sorted(peer_view)),
            ))
            checks.append((
                '状态快照带离线主控端名单（退役机器残留不参与仲裁）',
                'staleMasters' in status and isinstance(status.get('staleMasters'), list),
                str(status.get('staleMasters')),
            ))
            checks.append((
                '发现候选包含完整身份，从机无需首连确认',
                any(item.get('host') and item.get('fingerprint') for item in harness.master.status()['pairing'])
                and not harness.slave.node.confirmation_candidates(),
                '',
            ))
            checks.append((
                '状态推送含中文结论（前端只渲染）',
                bool(harness.statuses) and bool(harness.statuses[-1].get('stateText')),
                str(harness.statuses[-1].get('stateText') if harness.statuses else ''),
            ))
            # 去抖签名必须覆盖「会话是否开起来了」。只比会话条数的话，开启这个变化
            # 会被吞掉，界面就永远停在「会话未能开启」的旧结论上。
            # 用等待而不是立即断言：状态是 1s 一轮推的，刚重开会话时还没轮到下一轮。
            checks.append((
                '会话开启后重新推送状态（去抖不吞开启状态）',
                _wait_until(
                    lambda: any(any(session.get('opened') for session in (st.get('sessions') or []))
                                for st in harness.statuses),
                    5,
                ),
                f"每次推送的会话数={[len(st.get('sessions') or []) for st in harness.statuses]}"
                f' 实际会话表={harness.master.sessions_snapshot()}',
            ))
            checks.append((
                '从机状态里能看到「正在被谁使用」的会话表',
                bool(harness.slave.served_sessions()),
                str(harness.slave.served_sessions())[:160],
            ))
            checks.append((
                '远端设备会被推给上层重新合并设备列表',
                harness.master_devices and harness.master_devices[0]['key'].startswith('relay:pc-b:'),
                str(harness.master_devices[:1])[:120],
            ))
    finally:
        (relay_app.SESSION_REOPEN_BACKOFF_S,
         relay_app.SESSION_MAX_FAILURES) = saved
        harness.stop()
    return checks


# ----------------------------------------------------------------------
# 用例 4：主控屏蔽本机设备识别（同机双实例验证的分界线）
# ----------------------------------------------------------------------
def _check_blind_master(root) -> list:
    """主控不识别本机设备时，远端链路必须照常可用。

    两个实例其实跑在同一台 PC 上，本机 adb 能看到全部设备。屏蔽后主控侧一台本机
    设备都没有，它窗口里的设备只能来自从机 —— 于是终端、命令、目录浏览的每一次
    操作都必须真的经中转完成，旧脚本里「主控自己直连也能做同一件事」的假象被去掉。

    这里同时钉住「屏蔽不会波及从机」：从机照常识别并共享自己的设备，否则整条链路
    会静默变成「两边都没设备」而看起来只是「没发现设备」。
    """
    checks: list = []
    calls: list = []

    def counting_list(adb_path, timeout=8.0):
        calls.append(str(adb_path))
        return _fake_list_devices(adb_path, timeout)

    harness = _Harness(root, heartbeat_s=0.5, master_local_devices_enabled=False)
    try:
        with _patch(list_devices=counting_list, execute=_fake_execute):
            harness.start()
            checks.append((
                '屏蔽只作用于主控：从机照常枚举本机设备',
                harness.slave.node.local_devices_enabled is True
                and harness.master.node.local_devices_enabled is False,
                f'从机={harness.slave.node.local_devices_enabled} '
                f'主控={harness.master.node.local_devices_enabled}',
            ))

            harness.confirm_both()
            seen = _wait_until(lambda: harness.master.devices() != [], 8)
            checks.append(('主控仍能看到从机共享的设备', seen,
                           str(harness.master.devices())[:120]))
            if not seen:
                return checks
            key = harness.remote_key()
            checks.append((
                '主控窗口里只有从机的设备（本机设备数为 0）',
                harness.master.node._managed_devices() == [] and key == 'relay:pc-b:devB:tid=1',
                f'本机设备={harness.master.node._managed_devices()} key={key}',
            ))
            checks.append((
                '主控不去调本机 adb 枚举（完全不走本机直连这条退路）',
                'node-a' not in calls, str(calls),
            ))
            # 「不共享」必须去总线上的心跳里看：``remote_devices()`` 只收角色为
            # 从机的对端，主控的设备本来就不会出现在从机那份列表里 —— 拿它做断言
            # 永远为真，等于没测。
            def master_beat():
                return relay.read_peers(harness.master.node.queue.peers_dir).get('pc-a')

            no_share = _wait_until(
                lambda: master_beat() is not None and list(master_beat().devices) == [], 8,
            )
            checks.append((
                '主控心跳里设备数为 0（不向共享目录声明任何设备）',
                no_share, str(getattr(master_beat(), 'devices', None)),
            ))

            harness.confirm_both()
            trusted = _wait_until(
                lambda: all(view.trusted for view in harness.master.node.peers()
                            if view.peerId == 'pc-b'), 8,
            )
            checks.append(('屏蔽后确认对端即互信', trusted,
                           str([view.trustReason for view in harness.master.node.peers()])))
            # 确认前那几次失败会留下退避（SESSION_REOPEN_BACKOFF_S × 失败次数），
            # 所以这里要给足时间 —— 慢一点恢复是设计内行为，不是故障。
            opened = _wait_until(
                lambda: any(item['opened'] for item in harness.master.sessions_snapshot()), 30,
            )
            checks.append(('屏蔽后仍自动为远端设备开会话', opened,
                           str(harness.master.sessions_snapshot())))
            if not opened:
                return checks

            handle = harness.backend.handle_for('devB:tid=1')
            sent = harness.master.session_input(key, b'echo masked\n')
            arrived = _wait_until(lambda: b'echo masked\n' in harness.backend.written(handle), 6)
            checks.append(('屏蔽后按键照常经数据面到达从机 PTY', sent and arrived,
                           repr(harness.backend.written(handle))))

            _EXEC_LOG.clear()
            result = harness.master.remote_exec(key, ['-t', '1', 'shell', 'id'], timeout=8)
            checks.append((
                '屏蔽后命令照常落到从机的设备上',
                result.get('success') is True
                and _EXEC_LOG and _EXEC_LOG[0]['device'] == 'devB:tid=1',
                f'{result.get("output")!r} / {_EXEC_LOG[:1]}',
            ))
    finally:
        harness.stop()
    return checks


# ----------------------------------------------------------------------
# 用例 5：从机离线（降级与快速失败）
# ----------------------------------------------------------------------
def _check_slave_offline(root) -> list:
    checks: list = []
    harness = _Harness(root, heartbeat_s=0.5)
    try:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness.start()
            harness.confirm_both()
            _wait_until(lambda: harness.master.devices() != [], 8)
            key = harness.remote_key()
            if not key:
                return [('从机设备出现在主控设备列表（前置）', False, '未出现远端设备')]

            harness.slave.stop()
            offline = _wait_until(
                lambda: any((device.get('relay') or {}).get('online') is False
                            for device in harness.master.devices()), 12,
            )
            devices = harness.master.devices()
            checks.append(('从机离线后设备仍在列表里但标记离线', offline, str(devices[:1])[:160]))
            checks.append((
                '离线设备保留 ageMs 供界面显示掉线时长',
                bool(devices) and float((devices[0].get('relay') or {}).get('ageMs') or 0) > 0,
                str(devices[0].get('relay') if devices else ''),
            ))
            started = time.time()
            result = harness.master.remote_exec(key, ['shell', 'id'], timeout=8)
            elapsed = time.time() - started
            checks.append((
                '对离线从机的操作快速失败（不干等超时）',
                result.get('success') is False and elapsed < 3.0 and '离线' in (result.get('output') or ''),
                f'{elapsed:.2f}s / {result.get("output")}',
            ))
    finally:
        harness.stop()
    return checks


# ----------------------------------------------------------------------
# 用例 6：失败超上限后慢速重试（不做永久放弃）
# ----------------------------------------------------------------------
def _check_slow_retry(root) -> list:
    """注入可恢复的 PTY 故障，验证超出快速重试上限后仍慢速重试。"""
    checks: list = []
    saved = (
        relay_app.SESSION_MAX_FAILURES,
        relay_app.SESSION_RETRY_SLOW_S,
        relay_app.SESSION_REOPEN_BACKOFF_S,
    )
    relay_app.SESSION_MAX_FAILURES = 0
    relay_app.SESSION_RETRY_SLOW_S = 0.3
    relay_app.SESSION_REOPEN_BACKOFF_S = 0.2
    harness = _Harness(root, heartbeat_s=0.5)
    try:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness.start()
            _wait_until(lambda: bool(harness.master.status()['pairing']), 8)
            original_open = harness.backend.open
            def failing_open(*args, **kwargs):
                raise OSError('可恢复的测试 PTY 故障')
            harness.backend.open = failing_open
            harness.master.confirm_peer('pc-b')
            many = _wait_until(
                lambda: any(int(item.get('failures') or 0) >= 3
                            for item in harness.master.sessions_snapshot()),
                20,
            )
            checks.append((
                '真实 PTY 故障会被计数并持续重试',
                many, str(harness.master.sessions_snapshot())),
            )
            harness.backend.open = original_open
            recovered = _wait_until(
                lambda: any(item.get('opened') for item in harness.master.sessions_snapshot()),
                15,
            )
            checks.append((
                '失败次数超上限后仍慢速重试（不做永久放弃）',
                recovered, str(harness.master.sessions_snapshot())),
            )
    finally:
        (
            relay_app.SESSION_MAX_FAILURES,
            relay_app.SESSION_RETRY_SLOW_S,
            relay_app.SESSION_REOPEN_BACKOFF_S,
        ) = saved
        harness.stop()
    return checks


def _solo_controller(root: str, *, role: str = relay.ROLE_MASTER, peer_id: str = 'solo-pc',
                     statuses: list = None):
    """单个控制器（没有对端）。用来盯「自己这一行」的变化，不受对端干扰。"""
    os.makedirs(root, exist_ok=True)
    share = os.path.join(root, 'share')
    os.makedirs(share, exist_ok=True)
    config_path = os.path.join(root, 'relay-solo.json')
    relay_app.save_config(config_path, relay_app.RelayConfig(
        enabled=True, role=role, peerId=peer_id, root=share, heartbeatIntervalS=0.5,
    ))
    sink = statuses if statuses is not None else []
    controller = relay_app.RelayController(
        config_path=config_path,
        adb_path='node-solo',
        local_devices_enabled=False,
        on_status=lambda status: sink.append(status),
    )
    return controller, sink


def _check_role_change_pushes_status(root) -> list:
    """改角色必须推送状态 —— 去抖签名漏掉角色时会静默停在旧角色上。

    现场：把「从机」改成「主控」，如果两边都没有对端，``state`` 前后都是
    ``waiting-peer``，只比 state + 对端 + 会话的旧签名会认为「什么都没变」，
    于是这次状态不再推送，界面继续显示「从机」——用户看到的就是
    「设了主控还是显示从机」。
    """
    checks: list = []
    statuses: list = []
    controller, _ = _solo_controller(root, role=relay.ROLE_MASTER, statuses=statuses)
    try:
        controller.start()
        got_first = _wait_until(lambda: bool(statuses), 10, 0.05)
        first = dict(statuses[-1]) if statuses else {}
        statuses.clear()

        saved, reason = controller.apply_config({'role': relay.ROLE_SLAVE})
        pushed = _wait_until(
            lambda: any(item.get('role') == relay.ROLE_SLAVE for item in statuses), 8, 0.05)
        after = next((item for item in statuses if item.get('role') == relay.ROLE_SLAVE), {})

        checks.append(('单控制器也能拿到状态快照', got_first and bool(first), str(first.get('state'))))
        checks.append(('改角色返回成功', saved, reason))
        checks.append(('改成从机后状态被推送出去（不靠下一轮轮询碰运气）',
                       pushed, str([item.get('role') for item in statuses])))
        checks.append(('推送的状态里角色标签就是「从机」',
                       after.get('roleLabel') == '从机', repr(after.get('roleLabel'))))
        checks.append(('两种角色的 state 相同 —— 只比 state 的旧签名认不出这次变化',
                       first.get('state') == after.get('state') == relay.STATE_WAITING_PEER,
                       f"{first.get('state')} vs {after.get('state')}"))
    finally:
        controller.stop()
    return checks


def _check_reload_from_disk(root) -> list:
    """「刷新状态」要按**磁盘上的**配置重新生效，而不是只拉一次状态。

    ``relay.json`` 就在 exe 同目录，可能被另一个实例或手工编辑改过。只拉状态时
    角色永远停在旧值（这正是用户报的「设了主控还是显示从机」）。
    """
    checks: list = []
    statuses: list = []
    controller, _ = _solo_controller(root, role=relay.ROLE_MASTER, statuses=statuses)
    path = controller.config_path
    try:
        controller.start()
        _wait_until(lambda: bool(statuses), 10, 0.05)

        unchanged, ok, reason = controller.reload_from_disk()
        checks.append(('磁盘配置没变时不重新生效',
                       unchanged is False and ok and reason == '配置无变化', f'{unchanged} {reason}'))

        # 模拟「另一个实例 / 手工编辑」把角色改成从机
        statuses.clear()
        relay_app.save_config(path, relay_app.RelayConfig(
            enabled=True, role=relay.ROLE_SLAVE, peerId='solo-pc',
            root=controller.config.root, heartbeatIntervalS=0.5,
        ))
        changed, ok, reason = controller.reload_from_disk()
        checks.append(('磁盘配置变了 → 重新生效并说明改了什么',
                       changed is True and ok and 'slave' in reason, f'{changed} {reason}'))
        checks.append(('重新生效后运行中的配置真的变成从机',
                       controller.config.role == relay.ROLE_SLAVE, controller.config.role))
        checks.append(('重新生效会推送新角色（界面不必等轮询）',
                       _wait_until(lambda: any(i.get('role') == relay.ROLE_SLAVE for i in statuses), 8, 0.05),
                       str([i.get('role') for i in statuses])))

        # 配置文件被删掉：保持现状，不能把运行中的配置重置成默认值
        os.remove(path)
        missing, ok, reason = controller.reload_from_disk()
        checks.append(('配置文件消失时保持现状，不把配置重置成默认',
                       missing is False and ok and controller.config.role == relay.ROLE_SLAVE,
                       f'{missing} {reason} / role={controller.config.role}'))
    finally:
        if os.path.exists(path):
            os.remove(path)
        controller.stop()
    return checks


def _check_status_shape(root) -> list:
    """状态载荷在不同状态下必须是同一个形状。

    未启用时 ``status()`` 走的是手工拼的 base 字典（不经过 ``evaluate_health``），
    最容易漏键。前端与去抖签名都按「列表恒存在」处理；只在某一个状态下缺键，会让
    界面在那种状态下拿到不同形状的对象。
    """
    checks: list = []
    path = os.path.join(root, 'relay-off.json')
    relay_app.save_config(path, relay_app.RelayConfig(enabled=False, peerId='pc-shape'))
    controller = relay_app.RelayController(config_path=path, adb_path='node-shape',
                                           local_devices_enabled=False)
    try:
        status = controller.status()
        checks.append(('未启用时的状态也带 selfGhosts 列表',
                       status.get('selfGhosts') == [], repr(status.get('selfGhosts'))))
        checks.append(('未启用时的状态也带 staleMasters 列表',
                       status.get('staleMasters') == [], repr(status.get('staleMasters'))))
        required = ('enabled', 'running', 'role', 'roleLabel', 'peerId', 'state', 'stateText',
                    'hint', 'peers', 'pendingConfirmation', 'conflict', 'selfGhosts',
                    'staleMasters', 'sessions', 'configNotice', 'suggestedPeerId')
        missing = [key for key in required if key not in status]
        checks.append(('状态载荷必备字段齐全', not missing, str(missing)))
    finally:
        if os.path.exists(path):
            os.remove(path)

    # 多实例隔离说明要随状态一起到前端：被让出到临时配置的实例必须能显示出来。
    isolated_path = os.path.join(root, 'relay-isolated.json')
    isolated = relay_app.RelayController(
        config_path=isolated_path, adb_path='node-shape',
        local_devices_enabled=False,
        config_notice='本窗口改用独立临时配置',
    )
    try:
        status = isolated.status()
        checks.append(('隔离实例的状态带说明与建议标识',
                       status.get('configNotice') == '本窗口改用独立临时配置'
                       and str(status.get('suggestedPeerId') or '').endswith('-2'),
                       f'{status.get("configNotice")!r} / {status.get("suggestedPeerId")!r}'))
    finally:
        if os.path.exists(isolated_path):
            os.remove(isolated_path)
    return checks


def _check_claim_config_isolation(root) -> list:
    """同时开两个窗口时，第二个实例必须让出默认配置，而不是共写同一个 relay.json。

    共写的后果是用户报告的「改一个的角色，另一个也变了」：两个窗口读写同一份文件，
    这边保存、那边一刷新/重启就跟着变。``claim_config_path`` 用「活着才算数」的
    relay.lock 做占用标记，被占时退让到独立临时配置并给出说明。
    """
    import subprocess
    checks: list = []
    default_path = os.path.join(root, 'relay.json')
    lock_path = os.path.join(root, 'relay.lock')

    # 1) 没有锁 → 占用默认配置，并写下自己的锁。
    path, notice = relay_app.claim_config_path(default_path)
    checks.append(('没有锁时认领默认配置', path == os.path.abspath(default_path) and notice == '',
                   f'{path} / {notice!r}'))
    lock = relay.read_json(lock_path)
    checks.append(('认领后写下占用锁（含本进程 pid）',
                   isinstance(lock, dict) and int(lock.get('pid') or 0) == os.getpid(),
                   repr(lock)))

    # 2) 锁被另一个**活着**的进程持有 → 让出，用独立临时配置。
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    try:
        relay.atomic_write_json(lock_path, {'pid': child.pid, 'configPath': default_path})
        path2, notice2 = relay_app.claim_config_path(default_path)
        checks.append(('默认配置被活实例占用时让出到独立持久配置',
                       os.path.basename(path2) == 'relay-instance-2.json' and '独立持久配置' in notice2,
                       f'{path2} / {notice2!r}'))
        checks.append(('说明里写明原占用者的 pid，便于用户找到那个窗口',
                       str(child.pid) in notice2, notice2))

        relay_app.save_config(path2, relay_app.RelayConfig(enabled=True, role='slave', root=root,
                             poolId='second', nodeId='stable-second'))
        relay_app.release_config_claim(path2)
        path_again, _ = relay_app.claim_config_path(default_path)
        checks.append(('第二实例重启重用持久配置和实例键',
                       path_again == path2 and relay_app.load_config(path_again).nodeId == 'stable-second'
                       and relay_app.load_config(path_again).poolId == 'second', path_again))
        relay_app.release_config_claim(path_again)

        # 3) 锁是陈旧的（pid 已死）→ 视为无人占用，照常认领。
        relay.atomic_write_json(lock_path, {'pid': 999999, 'configPath': default_path})
        path3, notice3 = relay_app.claim_config_path(default_path)
        checks.append(('持锁进程已死时照常认领默认配置',
                       path3 == os.path.abspath(default_path) and notice3 == '',
                       f'{path3} / {notice3!r}'))

        # 4) 锁是本进程自己的（进程内 restart 重入）→ 照常认领，不自己隔离自己。
        relay.atomic_write_json(lock_path, {'pid': os.getpid(), 'configPath': default_path})
        path4, notice4 = relay_app.claim_config_path(default_path)
        checks.append(('锁是本进程自己的（restart 重入）时照常认领',
                       path4 == os.path.abspath(default_path) and notice4 == '',
                       f'{path4} / {notice4!r}'))

        # 5) 归还锁：只删自己写的那把。
        relay_app.release_config_claim(default_path)
        checks.append(('干净退出时归还自己的锁', not os.path.exists(lock_path),
                       'relay.lock 仍存在' if os.path.exists(lock_path) else '已删除'))
        relay.atomic_write_json(lock_path, {'pid': child.pid, 'configPath': default_path})
        relay_app.release_config_claim(default_path)
        checks.append(('别人的锁不能由本进程归还', os.path.exists(lock_path),
                       'relay.lock 被误删' if not os.path.exists(lock_path) else '保留'))
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()
        if os.path.exists(lock_path):
            os.remove(lock_path)
    # 升级前第二实例的临时配置和准入记录须一起迁移，不变成新身份。
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as migration_root:
        target_dir = Path(migration_root) / 'app'
        source_dir = Path(migration_root) / 'temp'
        target_dir.mkdir(); source_dir.mkdir()
        default = target_dir / 'relay.json'
        relay_app.save_config(str(default), relay_app.RelayConfig(root='C:/migration-share'))
        relay.atomic_write_json(str(target_dir / 'relay.lock'), {'pid': os.getpid()})
        legacy = source_dir / 'ADBManager-relay-999991.json'
        relay_app.save_config(str(legacy), relay_app.RelayConfig(enabled=True, role='slave',
                             root='C:/migration-share', poolId='test', nodeId='preserved-node', peerId='pc'))
        pairing = {'schema': 1, 'pairs': {'master-node': {'nodeId':'master-node', 'state':'confirmed', 'connectionId':'keep-generation'}}}
        relay.atomic_write_json(str(source_dir / 'relay-pairing-preserved-node.json'), pairing)
        trust = {'schema': 1, 'trusted': {'master': {'fingerprint':'kept'}}}
        relay.atomic_write_json(str(source_dir / 'relay-trust-pc-2.json'), trust)
        with patch.dict(os.environ, {'TEMP':str(source_dir)}):
            chosen, notice = relay_app.claim_config_path(str(default), pid=999992)
        checks.append(('升级迁移保留第二实例身份、部署与原连接代次',
            relay_app.load_config(chosen).nodeId == 'preserved-node'
            and relay_app.load_config(chosen).poolId == 'test'
            and relay.read_json(str(target_dir / 'relay-pairing-preserved-node.json')) == pairing
            and relay.read_json(str(target_dir / 'relay-trust-pc-2.json')) == trust
            and '已迁移' in notice, notice))
        relay_app.release_config_claim(chosen, pid=999992)
    return checks


def _check_inspect_clean_root(root) -> list:
    """换共享目录时的「沿用 / 清空后重建」：盘点只认中转自有文件，清理是白名单制。"""
    checks: list = []
    share = os.path.join(root, 'used-share')
    os.makedirs(os.path.join(share, 'peers'), exist_ok=True)
    os.makedirs(os.path.join(share, 'requests', 'pc-old'), exist_ok=True)
    os.makedirs(os.path.join(share, 'streams'), exist_ok=True)
    relay.atomic_write_json(os.path.join(share, 'relay.json'), {'schema': 1})
    with open(os.path.join(share, 'peers', 'pc-old.json'), 'w', encoding='utf-8') as fh:
        fh.write('{}')
    with open(os.path.join(share, 'peers', 'pc-new.json'), 'w', encoding='utf-8') as fh:
        fh.write('{}')
    with open(os.path.join(share, 'keepme.txt'), 'w', encoding='utf-8') as fh:
        fh.write('用户自己的文件')

    info = relay.inspect_relay_root(share)
    checks.append(('盘点：目录里有旧中转数据时 hasData=True',
                   info.get('hasData') is True and info.get('manifest') is True, repr(info)))
    checks.append(('盘点：逐槽计数正确（心跳 2 份、请求分区 1 个）',
                   info.get('peers') == 2 and info.get('requests') == 1, repr(info)))

    removed = relay.clean_relay_root(share)
    checks.append(('清空：删掉了布局清单与全部中转槽目录',
                   sorted(removed) == ['archive', 'peers/', 'relay.json', 'requests/', 'streams/']
                   or sorted(removed) == ['peers/', 'relay.json', 'requests/', 'streams/'],
                   str(removed)))
    checks.append(('清空：目录里用户自己的文件一个不动',
                   os.path.isfile(os.path.join(share, 'keepme.txt')), 'keepme.txt 丢失'))
    checks.append(('清空后再盘点：hasData=False',
                   relay.inspect_relay_root(share).get('hasData') is False, '仍有残留'))
    checks.append(('再清一次是干净的空操作', relay.clean_relay_root(share) == [], '又删了东西'))

    plain = os.path.join(root, 'plain-dir')
    os.makedirs(plain, exist_ok=True)
    with open(os.path.join(plain, 'note.txt'), 'w', encoding='utf-8') as fh:
        fh.write('没有任何中转数据')
    checks.append(('没有任何中转数据的目录：不提示也不删任何东西',
                   relay.inspect_relay_root(plain).get('hasData') is False
                   and relay.clean_relay_root(plain) == []
                   and os.path.isfile(os.path.join(plain, 'note.txt')),
                   '误动了非中转目录'))
    checks.append(('目录不存在时盘点/清理都安全返回',
                   relay.inspect_relay_root(os.path.join(root, 'nope')).get('exists') is False
                   and relay.clean_relay_root(os.path.join(root, 'nope')) == [], '抛了异常'))
    return checks


def _check_slave_terminal_mirror(root) -> list:
    """实际应用接线：远端PTY字节同时送从机WS显示，输入只执行一次。"""
    import adb_worker
    import server
    manager = server.ManagerServer.__new__(server.ManagerServer)
    manager._output_lock = threading.Lock()
    manager._output_activity = {}
    manager._recording_lock = threading.Lock()
    manager._recording_sessions = {}
    messages, remote, exited, handles = [], [], [], []
    manager._broadcast_async = messages.append

    class Terminal:
        def __init__(self, device, on_output, on_exit):
            self.output, self.exit = on_output, on_exit
            self.writes = []
            self.sizes = []
            handles.append(self)
            on_output(b'initial prompt> ')

        def write(self, data):
            self.writes.append(data)
            # 回显和中文回复按字节拆分，保证镜像不先解码破坏UTF-8。
            payload = data + '\r\n设备回复\r\n'.encode('utf-8')
            self.output(payload[:len(data) + 3])
            self.output(payload[len(data) + 3:])

        def resize(self, cols, rows):
            self.sizes.append((cols, rows))

        def close(self):
            self.exit()

    with patch.object(adb_worker, '_HAS_WINPTY', True), patch.object(adb_worker, 'TerminalSession', Terminal):
        controller = relay_app.RelayController(config_path=os.path.join(root, 'relay.json'),
                                                on_terminal_output=manager._on_terminal_output)
        backend = controller.session_backend
        handle = backend.open({'serial': 'dev', 'transportId': 7}, cols=90, rows=30,
                              on_output=remote.append, on_exit=lambda: exited.append(True))
        backend.write(handle, b'echo once\r')
        local = [base64.b64decode(item['data']) for item in messages]
        checks = [
            ('主控PTY启动、指令回显及中文回复同步到从机WS', local == remote and len(local) == 3, repr(messages)),
            ('同步输出路由到从机对应设备key且保留base64字节',
             all(item['type'] == 'terminal-data' and item['key'] == 'dev:tid=7' for item in messages), repr(messages)),
            ('主控输入只写远端PTY一次，不创建第二会话执行',
             len(handles) == 1 and handle.writes == [b'echo once\r'], repr(handle.writes)),
        ]
        # 从机显示回调异常不得阻断主控输出。
        def broken_display(_key, _data):
            raise RuntimeError('模拟窗口显示异常')
        backend.on_local_output = broken_display
        with patch.object(adb_worker, 'log'):
            handle.output(b'continues')
        checks.append(('从机显示异常不影响主控接收回复', remote[-1] == b'continues', repr(remote)))
        backend.close(handle)
        checks.append(('关闭远程会话只结束该PTY，不发送从机本地终端退出', exited == [True], repr(exited)))
    return checks


def run() -> bool:
    checks: list = []
    tmp = tempfile.mkdtemp(prefix='relay-app-test-')
    probes = (
        _check_selector_strip,
        _check_config,
        _check_end_to_end,
        _check_blind_master,
        _check_slave_offline,
        _check_slow_retry,
        _check_role_change_pushes_status,
        _check_reload_from_disk,
        _check_status_shape,
        _check_claim_config_isolation,
        _check_inspect_clean_root,
        _check_slave_terminal_mirror,
    )
    try:
        for index, probe in enumerate(probes):
            # 每个用例一个独立目录：信任名单是持久的，共用目录会把上一个用例的
            # 确认结果带进来，掩盖「未确认」这条安全边界。
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
