"""跨机中转的应用级验证：起**两个真实的应用实例**，各自启用中转，跑通全链路。

与 ``relay_demo.py`` 的区别：那个脚本直接调用库（``RelayNode``），验证的是传输层；
本脚本起的是真正的 ``server.py``（HTTP + WebSocket + ADBWorker + 前端接口在同一进程里），
并以一个真实的 WebSocket 客户端扮演界面，因此验证的是**应用能不能用**：

    ① 两个实例互相发现 → ② 首连确认 → ③ 主控窗口的设备列表里出现从机设备
    → ④ 远端终端真实往返（按键 → 从机 PTY → 真机 → 回到主控终端）
    → ⑤ 远端设备上执行真实命令 → ⑥ 文件传输的明确门禁
    → ⑦ 从机应用退出后设备灰显且操作快速失败

启动前会在共享目录里预置一台**已退役主控**的残留心跳（``retired-pc``）：真实部署中
它必然出现（``peers/`` 没有清理机制，退役机器的记录会永远留着），而它一旦参与角色
仲裁，剩下的机器就会永久判成冲突 —— 冲突是 fail-closed，从机会拒掉每个请求。演示把
它固定放进现场，证明两个实例照常连通（用 ``--no-stale-master`` 可跳过）。

同机模拟两台电脑的注意点：两台设备物理上都接在这一台 PC 上，所以**两个实例的
adb 都能看到两台设备**；真实跨机时不会这样。为了不让「本机直连」把跨机链路
掩盖掉，脚本默认让**主控实例完全屏蔽本机设备识别**
（``DEVMUX_NO_LOCAL_DEVICES=1``）：主控窗口里一台本机设备都没有，它列出的
每一台设备都必须经从机转发，终端/命令/目录浏览的每一次操作也都只能走中转。
这比旧脚本（主控自己也能直连两台设备、断言只看 ``relay:`` 前缀）严格得多。
从机侧照常识别并只对外共享 ``--slave-serial`` 那一台。想回到旧行为加
``--master-local-devices``。

用法：

    python relay_app_demo.py                 # 临时共享目录，跑完保留留痕
    python relay_app_demo.py --root \\\\srv\\relay   # 两台电脑时用它
    python relay_app_demo.py --slave-serial f62ca3df  # 指定从机共享哪台设备
    python relay_app_demo.py --master-local-devices   # 主控照常识别本机设备（旧行为）
    python relay_app_demo.py --no-stale-master  # 不预置退役主控的残留心跳
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

import relay

ROOT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
OK = '  [ OK ]'
BAD = '  [FAIL]'

# 演示里预先放进共享目录的「已退役主控」标识。真实部署中它来自一台曾作主控、
# 后来退役或换角色的机器：`peers/` 没有清理机制，它的心跳文件会永远留在那里。
RETIRED_MASTER_ID = 'retired-pc'


def rule(char='-', width=74):
    print(char * width)


def item(text=''):
    print(f'  {text}')


def stage(index, title):
    print()
    rule('=')
    print(f'  阶段 {index}：{title}')
    rule('=')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run_adb(adb_path: str, args) -> str:
    proc = subprocess.run(
        [adb_path, *args], capture_output=True, text=True, timeout=20,
        encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    return (proc.stdout or '') + (proc.stderr or '')


def list_local_devices(adb_path: str, attempts: int = 5) -> list[dict]:
    """枚举本机设备，带重试。

    ``adb devices -l`` 在 adb server 忙碌时会瞬时返回空列表（本项目里已多次实测），
    验证脚本若只调一次就会把「两台设备」判成「没有设备」而直接退出。
    """
    for attempt in range(attempts):
        devices = []
        for line in run_adb(adb_path, ['devices', '-l']).splitlines():
            line = line.strip()
            if not line or line.startswith('List'):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            tid = None
            for token in parts[2:]:
                if token.startswith('transport_id:'):
                    try:
                        tid = int(token.split(':', 1)[1])
                    except ValueError:
                        pass
            devices.append({'serial': parts[0], 'state': parts[1], 'transportId': tid})
        if devices:
            return devices
        time.sleep(1.0 + attempt)
    return []


class AppInstance:
    """一个真实的应用实例（server.py 子进程 + 一个 WebSocket 客户端）。"""

    def __init__(self, name: str, config_path: str, share: str, role: str,
                 *, peer_id: str, adb_path: str, managed_serials, log_path: str,
                 local_devices_enabled: bool = True):
        self.name = name
        self.config_path = config_path
        self.share = share
        self.role = role
        self.peer_id = peer_id
        self.adb_path = adb_path
        self.managed_serials = list(managed_serials or [])
        self.local_devices_enabled = bool(local_devices_enabled)
        self.log_path = log_path
        self.http_port = free_port()
        self.ws_port = free_port()
        self.proc = None
        self.log_file = None
        self.ws = None
        self.inbox: list[dict] = []
        self._reader = None
        self.devices: list[dict] = []

    # --- 配置与进程 ---
    def write_config(self):
        payload = {
            'schema': 1,
            'enabled': True,
            'role': self.role,
            'peerId': self.peer_id,
            'carrierKind': 'shared-folder',
            'root': self.share,
            'allowedSubcommands': ['devices', 'version', 'get-state', 'get-serialno', 'logcat', 'shell'],
            'allowShell': True,
            'managedSerials': self.managed_serials,
            'heartbeatIntervalS': 1.0,
        }
        Path(self.config_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8',
        )

    def start(self):
        self.write_config()
        env = dict(os.environ)
        env.update({
            'RELAY_CONFIG': self.config_path,
            'HTTP_PORT': str(self.http_port),
            'WS_PORT': str(self.ws_port),
            'DEVMUX_HEADLESS': '1',
            'ADB_PATH': self.adb_path,
        })
        if not self.local_devices_enabled:
            # 屏蔽本机设备识别：这个实例窗口里一台本机设备都不会出现，也不把
            # 本机设备共享给对端。同机模拟时这是把「本机直连」这个假象去掉的
            # 唯一办法 —— 否则主控能直接摸到两台设备，跨机链路没被真正验证。
            env['DEVMUX_NO_LOCAL_DEVICES'] = '1'
        self.log_file = open(self.log_path, 'w', encoding='utf-8')
        # -u：日志直接重定向到文件时 Python 默认块缓冲，出错时文件会是空的，
        # 恰好把最有用的诊断信息吞掉。
        self.proc = subprocess.Popen(
            [PYTHON, '-u', '-B', str(ROOT_DIR / 'server.py')],
            cwd=str(ROOT_DIR), env=env,
            stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # --- WebSocket 客户端 ---
    async def connect(self, timeout: float = 60.0):
        deadline = time.time() + timeout
        last_error = ''
        while time.time() < deadline:
            if not self.is_alive():
                raise RuntimeError(f'{self.name} 进程已退出，见 {self.log_path}')
            try:
                self.ws = await websockets.connect(f'ws://127.0.0.1:{self.ws_port}', max_size=8 * 1024 * 1024)
                self._reader = asyncio.create_task(self._pump())
                return
            except Exception as exc:
                last_error = f'{type(exc).__name__}: {exc}'
                await asyncio.sleep(0.5)
        raise RuntimeError(f'{self.name} 无法连接 WebSocket（{self.ws_port}）：{last_error}')

    async def _pump(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.inbox.append(msg)
                if len(self.inbox) > 4000:
                    del self.inbox[:2000]
                if msg.get('type') == 'devices-updated':
                    self.devices = msg.get('devices') or []
        except Exception:
            pass

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def request(self, payload: dict, timeout: float = 20.0) -> dict:
        payload = dict(payload)
        payload.setdefault('id', f'req-{time.time()}')
        await self.send(payload)
        return await self.wait_for(
            lambda m: m.get('id') == payload['id'], timeout, f'{payload.get("type")} 响应',
        )

    async def wait_for(self, predicate, timeout: float, what: str) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in list(self.inbox):
                if predicate(msg):
                    return msg
            await asyncio.sleep(0.1)
        raise TimeoutError(f'等待超时（{timeout:g}s）：{what}')

    def has_devices(self) -> bool:
        return bool(self.devices)

    def latest_status(self) -> dict:
        """收件箱里最新的一条中转状态快照。

        界面是持续被推送状态并即时重绘的，所以断言必须看**最新**快照；拿某一个
        历史快照做判断会得出「会话没建起来」这种假故障。
        """
        for msg in reversed(self.inbox):
            if msg.get('type') == 'relay-status':
                return msg
        return {}

    def remote_devices(self) -> list[dict]:
        return [d for d in self.devices if str(d.get('key', '')).startswith('relay:')]

    def terminal_text(self, key: str) -> str:
        import base64
        chunks = [
            base64.b64decode(msg['data']).decode('utf-8', errors='replace')
            for msg in self.inbox
            if msg.get('type') == 'terminal-data' and msg.get('key') == key
        ]
        return ''.join(chunks)


async def wait_until(predicate, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise TimeoutError(f'等待超时（{timeout:g}s）：{what}')


def _tail(path: str, lines: int = 20) -> str:
    try:
        content = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return '\n'.join(f'    | {line}' for line in content[-lines:])


def plant_retired_master(share: str) -> str:
    """在共享目录里预置一份「已退役主控」的残留心跳（心跳时间停在 2020 年）。

    这是多机部署里必然出现的东西，而且没法靠清理文件解决（每个文件一个写者，
    别人的历史记录不该被本机删）：一台曾作主控的机器退役后，``peers/<id>.json``
    会永远留在共享目录里并继续声明自己为主控。演示把它固定放进现场，用来证明
    它**不会**影响在跑的两端 —— 否则从机会永久判成角色冲突（fail-closed），
    拒掉每一个请求，而提示还让用户去改那台已经关机的机器。
    """
    peers_dir = os.path.join(share, relay.SLOT_PEERS)
    os.makedirs(peers_dir, exist_ok=True)
    return relay.write_heartbeat(peers_dir, relay.PeerHeartbeat(
        peerId=RETIRED_MASTER_ID,
        role=relay.ROLE_MASTER,
        heartbeatAt='2020-01-01T00:00:00+00:00',
        fingerprint='retired-host-fingerprint',
        host='已退役的旧机器',
    ))


def master_advertised_devices(share: str, peer_id: str) -> list:
    """从共享目录里读某个对端心跳中声明的设备 key 列表。

    为什么不去看从机侧的设备列表：从机的远端设备列表只收**角色为从机**的对端，
    主控的设备本来就不会出现在里面 —— 拿它判断「主控有没有共享设备」永远为空，
    等于没验证。心跳才是「这台机器声明了哪些设备」的唯一事实来源。
    """
    try:
        beats = relay.read_peers(os.path.join(share, relay.SLOT_PEERS))
    except Exception:
        return []
    beat = beats.get(peer_id)
    return [str(device.get('key') or '') for device in (beat.devices if beat else [])]


async def run_demo(args) -> int:
    failures: list[str] = []
    adb_path = args.adb or os.environ.get('ADB_PATH') or 'adb'

    rule('=')
    print('  DevMux 跨机中转 · 应用级验证（两个真实应用实例）')
    rule('=')

    devices = list_local_devices(adb_path)
    item(f'本机 adb 设备：{[(d["serial"], d["transportId"]) for d in devices] or "（无）"}')
    if len(devices) < 2:
        print()
        item('需要两台设备才能演示「主控直管一台、经从机管另一台」。')
        return 2

    master_serial = args.master_serial or devices[0]['serial']
    slave_serial = args.slave_serial or devices[1]['serial']
    blind_master = not getattr(args, 'master_local_devices', False)
    base = args.root or os.path.join(tempfile.gettempdir(), 'adb-relay-app-demo', time.strftime('%Y%m%d-%H%M%S'))
    share = os.path.join(base, 'relay-share')
    if not args.root:
        shutil.rmtree(base, ignore_errors=True)
    os.makedirs(share, exist_ok=True)
    item(f'共享目录（模拟中转服务器）：{share}')
    if blind_master:
        item(f'主控：完全屏蔽本机设备识别（本机那 {len(devices)} 台设备对它不存在），'
             f'窗口里的设备必须全部来自从机')
    else:
        item(f'主控直管设备：{master_serial}')
    item(f'从机对外共享：{slave_serial}')
    if not getattr(args, 'no_stale_master', False):
        item(f'预置残留：已退役主控 {RETIRED_MASTER_ID} 的心跳（停在 2020 年，'
             f'模拟退役机器留在共享目录里的记录）')
        plant_retired_master(share)

    master = AppInstance(
        '主控应用', os.path.join(base, 'relay-pc-a.json'), share, 'master',
        peer_id=args.peer_a, adb_path=adb_path, managed_serials=[],
        log_path=os.path.join(base, 'app-pc-a.log'), local_devices_enabled=not blind_master,
    )
    slave = AppInstance(
        '从机应用', os.path.join(base, 'relay-pc-b.json'), share, 'slave',
        peer_id=args.peer_b, adb_path=adb_path, managed_serials=[slave_serial],
        log_path=os.path.join(base, 'app-pc-b.log'),
    )

    exit_code = 0
    try:
        stage(1, '起两个真实应用实例（各自启用中转，一主一从）')
        master.start()
        slave.start()
        item(f'主控应用 HTTP {master.http_port} / WS {master.ws_port}（角色 master，标识 {args.peer_a}）')
        item(f'从机应用 HTTP {slave.http_port} / WS {slave.ws_port}（角色 slave，标识 {args.peer_b}）')
        await master.connect()
        await slave.connect()
        item('两个实例的 WebSocket 控制通道已连上（与界面同一条协议）')

        master_status = await wait_until(
            lambda: next((m for m in reversed(master.inbox)
                          if m.get('type') == 'relay-status' and m.get('enabled')), None),
            30, '主控应用的中转状态',
        )
        slave_status = await wait_until(
            lambda: next((m for m in reversed(slave.inbox)
                          if m.get('type') == 'relay-status' and m.get('enabled')), None),
            30, '从机应用的中转状态',
        )
        print(OK, f'主控应用：{master_status.get("roleLabel")} {master_status.get("peerId")}｜{master_status.get("stateText")}')
        print(OK, f'从机应用：{slave_status.get("roleLabel")} {slave_status.get("peerId")}｜{slave_status.get("stateText")}')

        stage(2, '两个应用互相发现 → 首连确认（需求①：主/从提示与实时状态）')
        seen = await wait_until(
            lambda: next((m for m in reversed(master.inbox)
                          if m.get('type') == 'relay-status'
                          and any(p.get('peerId') == args.peer_b for p in (m.get('peers') or []))), None),
            30, '主控发现从机心跳',
        )
        peer = next(p for p in seen['peers'] if p['peerId'] == args.peer_b)
        print(OK, f'主控发现对端：{peer["peerId"]}（{peer["roleLabel"]}），主机 {peer.get("host")}，'
                  f'指纹 {peer.get("fingerprint")}，待确认={peer.get("needsConfirmation")}')

        request_event = None
        try:
            request_event = await master.wait_for(
                lambda m: m.get('type') == 'relay-peer-request' and m.get('peerId') == args.peer_b,
                10, '首连确认推送',
            )
        except TimeoutError:
            pass
        if request_event:
            print(OK, '主控界面收到首连确认推送：')
            for line in str(request_event.get('detail') or '').splitlines():
                item(line)
        else:
            print(BAD, '未收到首连确认推送（界面靠状态兜底也能弹框）')
            failures.append('首连确认推送缺失')

        before = await master.request({'type': 'relay-status'})
        item(f'  确认前状态：{before.get("stateText")}（{before.get("hint")}）')

        for app, peer_id in ((master, args.peer_b),):
            answer = await app.request({'type': 'relay-confirm-peer', 'peerId': peer_id, 'accept': True})
            print(OK if answer.get('success') else BAD,
                  f'{app.name}确认对端 {peer_id}：{answer.get("output")}')
            if not answer.get('success'):
                failures.append(f'{app.name}确认对端失败：{answer.get("output")}')

        after = await wait_until(
            lambda: next((m for m in reversed(master.inbox)
                          if m.get('type') == 'relay-status' and m.get('state') == 'connected'), None),
            20, '主控进入已连接状态',
        )
        print(OK, f'确认后状态：{after.get("stateText")}｜{after.get("hint")}')
        item(f'  主控窗口的角色徽标：{after.get("roleLabel")} {after.get("peerId")}（状态点：已连接）')

        # 退役主控的残留心跳（本脚本启动前就放进了共享目录）：必须在两端都被忽略。
        # 这是真实的跨机故障场景 —— 一旦被算进冲突判定，从机会拒掉每个请求且无法自救。
        retired_rows = [p for p in (after.get('peers') or []) if p.get('peerId') == RETIRED_MASTER_ID]
        ignored = (RETIRED_MASTER_ID in (after.get('staleMasters') or [])
                   and after.get('conflict') is False and after.get('state') == 'connected')
        print(OK if ignored else BAD,
              f'退役主控 {RETIRED_MASTER_ID} 的残留心跳被忽略：状态仍为「{after.get("stateText")}」，'
              f'冲突={after.get("conflict")}，离线主控记录={after.get("staleMasters")}')
        if retired_rows:
            item(f'  残留记录仍列在对端里（如实标注离线，供人辨认）：'
                 f'在线={retired_rows[0].get("online")}　角色={retired_rows[0].get("roleLabel")}')
        if not ignored:
            failures.append('退役主控的残留心跳影响了在跑的两端（被判成角色冲突或状态异常）')

        slave_status = await slave.request({'type': 'relay-status'})
        item(f'  从机侧同一份残留的结论：{slave_status.get("stateText")}'
             f'　冲突={slave_status.get("conflict")}'
             f'　离线主控记录={slave_status.get("staleMasters")}')

        stage(3, '从机设备出现在主控窗口的设备列表里（需求②③）')
        remote = await wait_until(
            lambda: master.remote_devices() or None, 25, '远端设备混入设备列表',
        )
        remote_key = str(remote[0].get('key'))
        print(OK, f'主控设备列表新增：{remote_key}')
        item(f'  serial={remote[0].get("serial")}　transportId={remote[0].get("transportId")}'
             f'　来源={remote[0].get("relay", {}).get("peerId")}　在线={remote[0].get("relay", {}).get("online")}')
        local_keys = [d['key'] for d in master.devices if not str(d['key']).startswith('relay:')]
        if blind_master:
            # 主控本机设备识别已屏蔽：列表里有且只有从机那一台。这条断言是「跨机
            # 链路真的被验证过」的分界线 —— 本机一台设备都没有，后面每一次终端
            # 输入、命令执行、目录浏览都不存在「其实走了本机直连」的可能。
            only_remote = (
                not local_keys and len(master.devices) == 1
                and str(master.devices[0].get('key')) == remote_key
            )
            print(OK if only_remote else BAD,
                  f'主控本机设备识别已屏蔽：窗口里共 {len(master.devices)} 台设备，'
                  f'本机设备 {len(local_keys)} 台，其余全部经从机转发')
            if not only_remote:
                failures.append(
                    f'屏蔽本机设备后主控仍列出了本机设备（{local_keys}）或设备数不为 1'
                    f'（{[d.get("key") for d in master.devices]}）'
                )
            master_shared = master_advertised_devices(share, args.peer_a)
            never_shares = master_shared == []
            print(OK if never_shares else BAD,
                  f'主控发到共享目录的心跳里设备数为 0（屏蔽后它没有可共享的本机设备）：'
                  f'{master_shared}')
            if not never_shares:
                failures.append(f'屏蔽后主控仍在共享目录里声明设备：{master_shared}')
            item('  注：两台设备仍物理接在同一台 PC 上（同机模拟的固有限制），但主控看不到它们，'
                 '只能通过从机操作。')
        else:
            item(f'  本机设备 key（同机模拟时两台设备都在本机 adb 里）：{local_keys}')
            item('  注：加 --master-local-devices 时会保留旧行为 —— 主控本机直连的同一台设备'
                 '与经从机共享的设备并存，真实跨机时不会。')

        stage(4, '远端终端真实往返：主控按键 → 从机 PTY → 真机 → 回到主控终端')
        session = await wait_until(
            lambda: next((s for s in (master.latest_status().get('sessions') or [])
                          if s.get('opened')), None),
            30, '远端终端会话建立',
        )
        print(OK, f'主控自动为远端设备开启会话：{session.get("streamId")}（{session.get("cols")}x{session.get("rows")}）')
        printer = await master.wait_for(
            lambda m: m.get('type') == 'terminal-data' and m.get('key') == remote_key,
            25, '远端终端的初始输出（设备 shell 提示符）',
        )
        print(OK, f'远端终端已回传输出（{len(printer.get("data", ""))} 字节 base64）')
        item(f'  实测远端终端首屏：{master.terminal_text(remote_key)[:120]!r}')

        menu = await slave.request({'type': 'relay-status'})
        item(f'  从机正在对外提供的会话：{menu.get("servedSessions")}')

        token = f'RELAY_E2E_{int(time.time())}'
        await master.send({'type': 'write-terminal', 'key': remote_key, 'data': f'echo {token}\n'})
        answered = await wait_until(
            lambda: token in master.terminal_text(remote_key), 25,
            f'远端终端回显 {token}',
        )
        print(OK, f'远端终端执行成功，主控终端里看到了：{token}')

        await master.send({'type': 'resize-terminal', 'key': remote_key, 'cols': 132, 'rows': 40})
        await asyncio.sleep(1.0)

        stage(5, '远端设备上执行真实命令（经从机，选择符由从机自己决定）')
        boot = await master.request({
            'type': 'exec-adb', 'key': remote_key, 'timeout': 20,
            'argv': ['-t', '99', 'shell', 'cat', '/proc/sys/kernel/random/boot_id'],
        }, timeout=40)
        print(OK if boot.get('success') else BAD, f'远端命令执行：{boot.get("output", "").strip()[:80]!r}')
        if not boot.get('success'):
            failures.append(f'远端命令执行失败：{boot.get("output")}')
        item('  说明：命令里故意带了错误的 -t 99，从机侧把它剥掉并用自己的设备 —— '
             '主控无法（也不该）指定从机上的设备。')

        listing = await master.request({
            'type': 'list-device-dir', 'key': remote_key, 'serial': remote[0].get('serial'),
            'transportId': remote[0].get('transportId'), 'path': '/',
        }, timeout=40)
        entries = listing.get('entries') or []
        print(OK if listing.get('success') else BAD,
              f'远端设备目录浏览：{len(entries)} 项（前几项：{[e["name"] for e in entries[:6]]}）')
        if not listing.get('success'):
            failures.append(f'远端目录浏览失败：{listing.get("output")}')

        stage(6, '文件传输门禁：明确拒绝，而不是含糊失败（设计文档 §5.2）')
        blocked = await master.request({
            'type': 'exec-adb', 'key': remote_key, 'timeout': 15,
            'argv': ['-t', '1', 'push', 'D:\\tmp\\a.txt', '/data/local/tmp/a.txt'],
        }, timeout=40)
        print(OK if not blocked.get('success') else BAD, f'上传被拒：{blocked.get("output")}')
        if blocked.get('success'):
            failures.append('push 未被拒绝')

        stage(7, '从机应用退出：设备灰显 + 新操作快速失败')
        slave.stop()
        offline = await wait_until(
            lambda: next((d for d in master.remote_devices()
                          if (d.get('relay') or {}).get('online') is False), None),
            40, '远端设备转为离线（心跳中断）',
        )
        print(OK, f'设备仍在主控列表里但标记离线：{offline["key"]}'
                  f'（掉线 {(offline.get("relay") or {}).get("ageMs", 0) / 1000:.1f}s）')
        started = time.time()
        fast = await master.request({
            'type': 'exec-adb', 'key': remote_key, 'timeout': 8,
            'argv': ['shell', 'id'],
        }, timeout=30)
        elapsed = time.time() - started
        print(OK if not fast.get('success') and elapsed < 5 else BAD,
              f'对离线设备的操作 {elapsed:.2f}s 内失败：{fast.get("output", "")[:90]}')
        if fast.get('success'):
            failures.append('离线设备的操作未被快速拒绝')

        stage(8, '收尾')
        item(f'共享目录留痕：{share}')
        for name in ('peers', 'archive', 'requests', 'results', 'streams'):
            path = os.path.join(share, name)
            count = len(os.listdir(path)) if os.path.isdir(path) else 0
            item(f'  {name}/：{count} 项')
        item(f'应用日志：{master.log_path}（主控） / {slave.log_path}（从机）')
    except Exception as exc:
        failures.append(f'{type(exc).__name__}: {exc}')
        print()
        print(BAD, f'演示中断：{type(exc).__name__}: {exc}')
        # 光看进程日志不足以定位跨机故障：中转自己的结论在状态快照与事件里。
        for app in (master, slave):
            status = app.latest_status()
            item(f'  {app.name} 会话表：{status.get("sessions")}')
            item(f'  {app.name} 待确认：{status.get("pendingConfirmation")}'
                 f'　状态：{status.get("stateText")}')
        events = [m for m in master.inbox if m.get('type') == 'relay-event'][-12:]
        for event in events:
            item(f'  主控事件 [{event.get("kind")}] {event.get("text")}')
        for app in (master, slave):
            print(f'  --- {app.name} 日志尾部（{app.log_path}）---')
            print(_tail(app.log_path, 15) or '    | （无输出）')
    finally:
        if not args.keep_alive:
            slave.stop()
            master.stop()
        else:
            item(f'--keep-alive：两个实例仍在运行（HTTP {master.http_port}/{slave.http_port}），'
                 f'请自行结束进程')

    print()
    rule('=')
    if failures:
        print(f'  结果：{len(failures)} 项未通过')
        for text in failures:
            item(f'- {text}')
        exit_code = 1
    else:
        suffix = ('（主控本机设备识别已屏蔽，所有设备操作都只能经从机完成）'
                  if not getattr(args, 'master_local_devices', False) else '')
        print('  结果：全部通过 —— 两个真实应用实例之间，主控窗口可以直接管理和操作从机设备'
              + suffix)
    rule('=')
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description='跨机中转应用级验证（两个真实应用实例）')
    parser.add_argument('--root', help='共享目录（默认用临时目录模拟中转服务器）')
    parser.add_argument('--adb', help='adb 可执行文件路径')
    parser.add_argument('--peer-a', default='pc-a', help='主控应用的本机标识')
    parser.add_argument('--peer-b', default='pc-b', help='从机应用的本机标识')
    parser.add_argument('--master-serial', help='主控直管的设备序列号（默认第一台）')
    parser.add_argument('--slave-serial', help='从机对外共享的设备序列号（默认第二台）')
    parser.add_argument('--keep-alive', action='store_true', help='保留两个实例进程（排障用）')
    parser.add_argument('--no-stale-master', action='store_true',
                        help='不在共享目录里预置「已退役主控」的残留心跳')
    parser.add_argument('--master-local-devices', action='store_true',
                        help='主控照常识别本机设备（旧行为）；默认屏蔽，主控只看得到从机的设备')
    args = parser.parse_args()
    return asyncio.run(run_demo(args))


if __name__ == '__main__':
    sys.exit(main())
