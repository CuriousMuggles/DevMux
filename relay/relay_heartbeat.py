"""心跳：脉冲写入、读取与时钟归算。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import threading
import time
import uuid
from dataclasses import (
    asdict,
    dataclass,
    field,
    replace,
)
from typing import Optional

from .relay_constants import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    HEARTBEAT_GRACE_MS,
    HEARTBEAT_MISS_COUNT,
    RELAY_SCHEMA,
    role_label,
)
from . import relay_file_io
from .relay_pool import _shared_age_ms
from .relay_protocol import (
    check_peer_id,
    valid_peer_id,
)
from . import relay_store


# ----------------------------------------------------------------------
# 心跳
# ----------------------------------------------------------------------
@dataclass
class PeerHeartbeat:
    peerId: str
    role: str
    appVersion: str = ''
    pid: int = 0
    startedAt: str = ''
    heartbeatAt: str = ''
    devices: list = field(default_factory=list)
    # 以下字段用于「连接前提示主/从」与防重名：指纹变化即要求重新确认。
    fingerprint: str = ''
    carrier: str = ''
    linkRttMs: float = 0.0
    host: str = ''
    # **实例级**身份：同机多开时每个实例一个，持久化在本机、跨重启不变。
    #
    # 为什么不复用 ``fingerprint``：指纹是**机器级**的（由 MachineGuid 派生），同机
    # 多开的两个实例指纹一模一样，正是 ``is_self_ghost`` / ``is_superseded_ghost``
    # 用来识别「同机旧实例」的依据，语义不能动。而配对要的是「这个实例是谁」，
    # 必须能区分同机的两个实例 —— 所以另开一层。
    #
    # 老版本写的心跳没有这两个字段，读出来是空串/0；配对启用时它们会被判为
    # 「未配对」并**拒绝放行**（fail-closed），提示里会点明对端版本过旧。
    nodeId: str = ''
    nodeSlot: int = 0
    # 读取时由 ``read_peers`` 填上的**共享目录 mtime**，属于服务器时钟。它不是
    # 心跳内容的一部分，不写入文件（``to_dict`` 会剔除），只用于跨机时效判定。
    fileMtimeMs: float = 0.0
    heartbeatSeq: int = 0
    uptimeSeconds: int = 0
    # 可选控制通道能力；缺失表示只支持旧逐文件队列。
    controlJournal: int = 0
    # 写者当前根的代次前缀；本机路径可跨端不同，读端只与该来源的公告比较。
    connectionScope: str = ''
    # 只属于读端的观测状态，不能写回共享目录或配置。
    sequenceObserved: bool = False
    lastSequenceAdvanceMono: float = -1.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop('fileMtimeMs', None)
        data.pop('sequenceObserved', None)
        data.pop('lastSequenceAdvanceMono', None)
        return {'schema': RELAY_SCHEMA, **data}

    @classmethod
    def from_dict(cls, data: dict) -> 'PeerHeartbeat':
        devices = data.get('devices')
        return cls(
            peerId=str(data.get('peerId', '')),
            role=str(data.get('role', '')),
            appVersion=str(data.get('appVersion', '')),
            pid=int(data.get('pid') or 0),
            startedAt=str(data.get('startedAt', '')),
            heartbeatAt=str(data.get('heartbeatAt', '')),
            devices=list(devices) if isinstance(devices, list) else [],
            fingerprint=str(data.get('fingerprint', '')),
            carrier=str(data.get('carrier', '')),
            linkRttMs=float(data.get('linkRttMs') or 0.0),
            host=str(data.get('host', '')),
            nodeId=str(data.get('nodeId', '')),
            nodeSlot=int(data.get('nodeSlot') or 0),
            heartbeatSeq=max(0, int(data.get('heartbeatSeq') or 0)),
            uptimeSeconds=max(0, int(data.get('uptimeSeconds') or 0)),
            controlJournal=max(0, int(data.get('controlJournal') or 0)),
            connectionScope=str(data.get('connectionScope') or ''),
        )

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    @property
    def server_skew_ms(self) -> float:
        """写者本机时钟相对**共享目录服务器时钟**的偏差（服务器 - 本机，毫秒）。

        心跳文件的 mtime 由共享目录服务器打戳，而 ``heartbeatAt`` 是写者本机的
        墙上时钟；两者之差就是写者时钟相对服务器的偏差。量它不需要额外探针文件：
        本机自己写的心跳就在同一个目录里，它的 mtime 与本机写下的 ``heartbeatAt``
        一对，偏差就出来了，而且每 2 秒随心跳刷新（见 ``observer_clock_skew_ms``）。

        读不到 mtime（非共享目录载体、stat 失败）时返回 0：那种情况下两端本来就在
        同一台机器上，没有偏差可言。
        """
        if self.fileMtimeMs <= 0:
            return 0.0
        beat = relay_store._epoch_ms(self.heartbeatAt)
        if beat <= 0:
            return 0.0
        return float(self.fileMtimeMs) - beat

    def age_ms(self, now: Optional[float] = None, *, skew_ms: float = 0.0) -> float:
        """距上次心跳的时长（毫秒）。

        **不能**直接拿 ``heartbeatAt`` 减本机时钟：那是写者机器的墙上时钟，而两台
        机器的时钟差几百秒是常态（现场实测两台 PC 差 393s、PC 与共享服务器差 669s）。
        据此算出来的「年龄」完全不可用 —— 快的一方永远看不到慢的一方在线，慢的一方
        反而把快的一方当成刚心跳过，于是出现「从机说已连接、主控说没发现在线的从机」
        这种自相矛盾的状态。

        所以优先用**心跳文件的 mtime**：它由共享目录服务器打戳，是所有端共同的时基。
        把本机时钟按 ``skew_ms``（服务器时钟 - 本机时钟，由 ``observer_clock_skew_ms``
        从本机自己的心跳上量得）归算过去，再与该文件的 mtime 相减即可。

        mtime 拿不到时退回「对端时间戳 vs 本机时钟」的老算法：只有同机（同进程或
        同机双实例）才会走到这里，同机没有时钟偏差，老算法是对的。

        返回 ``-1.0`` 表示这条心跳既没有 mtime 也没有可解析的时间戳，调用方应判离线。
        """
        if self.sequenceObserved:
            if self.lastSequenceAdvanceMono < 0:
                return -1.0  # 初见旧值不能证明在线，必须观察到下一次推进。
            return max(0.0, (time.monotonic() - self.lastSequenceAdvanceMono) * 1000.0)
        return _shared_age_ms(self.fileMtimeMs, self.heartbeatAt, now, skew_ms)

    def is_stale(
        self,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        now: Optional[float] = None,
        *,
        skew_ms: float = 0.0,
    ) -> bool:
        """超过 ``interval × HEARTBEAT_MISS_COUNT + 容差`` 未刷新即判离线。

        容差是给**跨机共享目录**留的：SMB 客户端会缓存文件属性（默认 1 秒量级），
        再加上一次慢的设备枚举就可能把两拍心跳拉到 4~5 秒（实测 4.6s），刚好顶到
        6 秒门槛上。判离线本来就不需要精确到秒（新操作在本地 fail-fast，不等超时），
        所以宁可放宽，也不要让健康的对端在两拍之间闪一下离线。
        """
        age = self.age_ms(now, skew_ms=skew_ms)
        if age < 0:
            return True
        if self.sequenceObserved:
            # 每秒发布一次；连续3秒未观察到序号推进即离线，恢复推进自动上线。
            return age > 3000.0
        threshold = max(1.0, float(interval_s)) * HEARTBEAT_MISS_COUNT * 1000.0
        return age > threshold + HEARTBEAT_GRACE_MS


class HeartbeatSequenceObserver:
    """读端独立计时，只认同一启动代次递增序号，不使用墙钟或mtime。"""

    def __init__(self):
        self._states = {}
        self._retired = {}
        self._lock = threading.Lock()

    def observe(self, beat, *, channel='', now=None):
        if beat.heartbeatSeq <= 0:
            return beat  # 旧端保留原共享目录时基的兼容规则。
        observed_at = time.monotonic() if now is None else float(now)
        key = (channel, beat.peerId)
        identity = (beat.nodeId, beat.pid, beat.startedAt)
        with self._lock:
            state = self._states.get(key)
            if state is None or state[0] != identity:
                retired = self._retired.setdefault(key, set())
                if identity in retired:
                    return replace(beat, sequenceObserved=True, lastSequenceAdvanceMono=-1.0)
                if state is not None:
                    retired.add(state[0])
                state = (identity, beat.heartbeatSeq, -1.0, beat)
            elif beat.heartbeatSeq > state[1]:
                state = (identity, beat.heartbeatSeq, observed_at, beat)
            # 同值、缓存回退和迟到的旧快照都不能延长在线期限。
            self._states[key] = state
            return replace(state[3], sequenceObserved=True, lastSequenceAdvanceMono=state[2])


def host_fingerprint(salt: str = 'adb-manager-relay') -> str:
    """本机稳定指纹，用于识别「同一个 peerId 被两台机器使用」。

    **这不是安全凭证**：它由机器标识与主机名派生，可被伪造，只用于发现重名
    与配置漂移。真正的信任边界是显式确认过的 peerId 与命令白名单。
    """
    seed = ''
    if platform.system() == 'Windows':
        try:
            import winreg  # noqa: PLC0415 - 仅 Windows 需要

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Cryptography',
            ) as handle:
                seed = str(winreg.QueryValueEx(handle, 'MachineGuid')[0])
        except (OSError, ImportError, AttributeError):
            seed = ''
    if not seed:
        seed = platform.node() or 'unknown-host'
    raw = f'{salt}|{seed}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def peer_heartbeat_path(peers_dir: str, peer_id: str) -> str:
    return os.path.join(peers_dir, f'{check_peer_id(peer_id)}.json')


def write_heartbeat(peers_dir: str, beat: PeerHeartbeat, *, snapshot: bool = False,
                    pulse_only: bool = False, publish_pulse: bool = True) -> str:
    """写入自己的心跳。本机 peerId 非法属于配置错误，直接抛出而不是静默跳过。"""
    beat.peerId = check_peer_id(beat.peerId)
    path = peer_heartbeat_path(peers_dir, beat.peerId)
    if snapshot and publish_pulse:
        # 同名 JSON 的原子替换可能被 SMB 继续缓存旧对象；独立追加日志保持文件身份。
        pulse_path = heartbeat_pulse_path(peers_dir, beat)
        with open(pulse_path, 'ab') as pulse:
            # 同一文件对象内回卷，避免长期运行无限增长；绝不替换文件身份。
            if pulse.tell() >= 2 * 1024 * 1024:
                pulse.truncate(0)
            pulse.write((json.dumps(beat.to_dict(), ensure_ascii=False) + '\n').encode('utf-8'))
    if pulse_only:
        return path
    relay_store.atomic_write_json(path, beat.to_dict())
    if snapshot:
        # 唯一文件名避免 SMB 长期复用同一路径的旧内容；旧端仍读取原文件。
        prefix = f'{beat.peerId}.beat-{beat.pid}-'
        snapshot_path = os.path.join(peers_dir, f'{prefix}{uuid.uuid4().hex}.beat')
        relay_store.atomic_write_json(snapshot_path, beat.to_dict())
        # 只清理本机自己 peerId 的快照，保留最新三份供并发读取；不删除别人的历史。
        # 同一 peerId 是单写者（含重启后的新 pid 也属同一身份的延续），所以按
        # peerId 全量保留三份即可 —— 旧实现只清当前 pid 前缀，每次重启都会把
        # 上一个实例的快照永久留下（实测 peers/ 里累积了 250 个 500B 的 beat 文件）。
        # 一次目录枚举取得元数据，避免每个快照再发一次 SMB 查询。
        own = []
        beat_pattern = re.compile(rf'^{re.escape(beat.peerId)}\.beat-\d+-[0-9a-f]+\.beat$')
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                if not beat_pattern.match(entry.name):
                    continue
                try:
                    own.append((entry.stat().st_mtime, entry.path))
                except OSError:
                    continue
        own.sort()
        for _mtime, old in own[:-3]:
            try:
                os.unlink(old)
            except OSError:
                pass
    return path


def heartbeat_pulse_path(peers_dir: str, beat: PeerHeartbeat) -> str:
    generation = hashlib.sha256(beat.startedAt.encode('utf-8')).hexdigest()[:12]
    return os.path.join(peers_dir, f'{check_peer_id(beat.peerId)}.pulse-{int(beat.pid)}-{generation}')


def _pid_alive(pid: int) -> bool:
    """Windows 下的进程存活探测（``os.kill(pid, 0)`` 在 Windows 上是**真杀**，不能用）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import ctypes  # noqa: PLC0415 - 仅 Windows 需要
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True  # 拿不准时按「还活着」处理：宁可留着，不可误删活跃对端


def purge_dead_pulses(peers_dir: str, peer_id: str, *, self_pid: int) -> list[str]:
    """清理本机自己 peerId 下、已停止进程遗留的 pulse 心跳日志，返回删除的文件名。

    ``peers/<peerId>.pulse-<pid>-<generation>`` 是实例运行期的追加日志（2MB 上限），
    正常退出时由 ``drop_own_pulse`` 删除；**异常退出**（崩溃、断电）会留下一个
    1-2MB 的文件，每次重启累积一个。同一 peerId 单写者，启动时把「pid 已不存活」
    的 pulse 清掉即可回收空间；pid 仍存活（防御性保留同机兄弟）的一律不碰。
    """
    removed: list[str] = []
    pattern = re.compile(rf'^{re.escape(check_peer_id(peer_id))}\.pulse-(\d+)-[0-9a-f]{{12}}$')
    try:
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                match = pattern.match(entry.name)
                if not match:
                    continue
                if int(match.group(1)) == int(self_pid) or _pid_alive(int(match.group(1))):
                    continue
                try:
                    os.unlink(entry.path)
                    removed.append(entry.name)
                except OSError:
                    continue
    except OSError:
        pass
    return removed


def drop_own_pulse(peers_dir: str, beat: PeerHeartbeat) -> bool:
    """正常退出时删除本机自己的 pulse 心跳日志（谁写谁清）。"""
    try:
        path = heartbeat_pulse_path(peers_dir, beat)
        if os.path.isfile(path):
            os.unlink(path)
            return True
    except OSError:
        pass
    return False


def read_heartbeat_pulse(peers_dir: str, beat: PeerHeartbeat) -> PeerHeartbeat:
    """只读尾部完整记录；大小属性陈旧时继续读到真实 EOF，不截掉新心跳。"""
    path = heartbeat_pulse_path(peers_dir, beat)
    try:
        info = relay_file_io.stat(path)
        offset = max(0, info.st_size - 65536)
        content = relay_file_io.read_bytes(path, offset)
        if offset and not content:
            # 回卷后旧大小属性可能仍大于真实文件，归零读取完整新一拍。
            content = relay_file_io.read_bytes(path)
        lines = content.split(b'\n')[:-1]
        for line in reversed(lines):
            try:
                latest = PeerHeartbeat.from_dict(json.loads(line.decode('utf-8')))
            except (ValueError, TypeError, UnicodeError):
                continue
            if (latest.peerId != beat.peerId or latest.pid != beat.pid
                    or latest.startedAt != beat.startedAt or latest.nodeId != beat.nodeId):
                continue
            if (latest.heartbeatSeq >= beat.heartbeatSeq if latest.heartbeatSeq
                    else latest.heartbeatAt >= beat.heartbeatAt):
                latest.fileMtimeMs = relay_file_io.stat(path).st_mtime * 1000.0
                return latest
            break
    except OSError:
        pass
    return beat


def read_peers(peers_dir: str) -> dict[str, PeerHeartbeat]:
    """读取全部对端心跳；损坏、半截或 peerId 非法的记录一律跳过。

    peerId 会被拼成目录名与文件名，而它来自共享目录里的内容，因此这里必须
    过滤：一个写着 ``../../`` 的心跳文件不能变成上层的路径片段。

    顺带把每个文件的 **mtime** 记进 ``PeerHeartbeat.fileMtimeMs``：它由共享目录
    服务器打戳，是跨机判断「这条心跳还新不新鲜」时唯一可信的时基（见
    ``PeerHeartbeat.age_ms``）。stat 失败只意味着退化成老算法，不该让整条心跳消失。
    """
    peers: dict[str, PeerHeartbeat] = {}
    # 按文件名先分组并选最新版本；旧快照只在新文件已消失或损坏时回退。
    # 共享目录的目录项本身携带 mtime，无需逐文件重复枚举整个目录。
    candidates = {}
    try:
        with os.scandir(peers_dir) as entries:
            for entry in entries:
                if entry.name.endswith('.json'):
                    identity = entry.name[:-5]
                    priority = 0
                else:
                    match = re.fullmatch(r'(.+)\.beat-\d+-[0-9a-f]+\.beat', entry.name)
                    if not match:
                        continue
                    identity = match.group(1)
                    priority = 1
                if not valid_peer_id(identity):
                    continue
                try:
                    mtime = entry.stat().st_mtime * 1000.0
                except OSError:
                    continue
                candidates.setdefault(identity, []).append((mtime, priority, entry.path))
    except OSError:
        return peers
    for identity, versions in candidates.items():
        if relay_file_io.is_remote(peers_dir):
            # SMB 目录项可滞后数秒。直接查询稳定心跳的原生句柄补充最新 mtime，
            # 内容仍经无缓存读取；唯一快照作为兼容与瞬时替换的回退。
            stable_path = peer_heartbeat_path(peers_dir, identity)
            try:
                fresh_mtime = relay_file_io.stat(stable_path).st_mtime * 1000.0
                versions = [v for v in versions if v[2] != stable_path]
                versions.append((fresh_mtime, 0, stable_path))
            except OSError:
                pass
        for mtime, _priority, path in sorted(versions, reverse=True):
            data = relay_store.read_json(path)
            if not data:
                continue
            beat = PeerHeartbeat.from_dict(data)
            if not valid_peer_id(beat.peerId):
                continue
            beat.fileMtimeMs = mtime
            beat = read_heartbeat_pulse(peers_dir, beat)
            previous = peers.get(beat.peerId)
            if previous is None or beat.fileMtimeMs >= previous.fileMtimeMs:
                peers[beat.peerId] = beat
            break
    return peers


def observer_clock_skew_ms(
    peers: Optional[dict[str, 'PeerHeartbeat']],
    self_id: str,
) -> float:
    """从**本机自己**的心跳记录量出「共享目录服务器时钟 - 本机时钟」（毫秒）。

    跨机时效判定要把本机时钟归算到共享目录服务器的时钟上，而服务器时钟拿不到
    ——除非借一份本机自己写的、由服务器打戳的文件。本机的心跳就是：它由
    ``RelayNode._beat`` 每 2 秒写一次，mtime 由服务器打戳，与本机写下的
    ``heartbeatAt`` 之差就是偏差。不需要额外探针文件，也不需要两端约定新字段，
    所以**只升级一端也能修好单向可见**。

    拿不到（本机心跳还没写、载体不是共享目录）时返回 0：那时退化成各机自比的
    老行为，对同机实例是对的。
    """
    beat = (peers or {}).get(str(self_id or ''))
    if beat is None:
        return 0.0
    return beat.server_skew_ms


def purge_self_heartbeats(
    peers_dir: str,
    *,
    fingerprint: str,
    pid: int,
    started_at: str,
    keep_peer_id: str,
) -> list[str]:
    """删掉**本机自己**旧标识残留的心跳，返回被删掉的标识。

    只动 ``is_own_process_leftover`` 判定为「同机同进程的另一次身份」的文件 —— 那本来
    就是本进程自己写的，符合「谁写谁清」。刻意**不**用更宽的 ``is_self_ghost``：那条
    还会把「上一个进程留下的残留」算进来，而别人的进程写的文件不该由本机删（单写者
    原则）。那种残留交给 ``evaluate_health`` 的幽灵过滤在显示层忽略，文件留着无害。
    文件读不到或删不掉（共享目录只读、被占用）都只是失败，不影响启动。
    """
    removed: list[str] = []
    for peer_id, beat in read_peers(peers_dir).items():
        if peer_id == keep_peer_id:
            continue
        if not is_own_process_leftover(
            beat, self_fingerprint=fingerprint, self_pid=pid,
            self_started_at=started_at,
        ):
            continue
        try:
            os.remove(peer_heartbeat_path(peers_dir, peer_id))
            removed.append(peer_id)
        except OSError:
            continue
    return removed


def is_own_process_leftover(
    beat: 'PeerHeartbeat',
    *,
    self_fingerprint: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
) -> bool:
    """这条心跳是不是**本进程自己**在另一次身份下写的？

    满足「同指纹 + 同 pid + 启动时刻不同」——即同一个进程改了 ``peerId`` 之后的
    旧标识记录。因为确实是自己写的，符合「谁写谁清」，可以安全删除（见
    ``purge_self_heartbeats``）。

    时间戳是身份的一部分：同一进程里每次启动节点都会重取 ``startedAt``，所以
    「同 pid 不同 startedAt」精确对应「同一进程的另一次身份」。
    """
    if not self_fingerprint or beat.fingerprint != self_fingerprint:
        return False
    if not self_pid or int(beat.pid or 0) != int(self_pid):
        return False
    return bool(beat.startedAt) and beat.startedAt != self_started_at
