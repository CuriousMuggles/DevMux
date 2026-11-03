"""角色仲裁与连接状态（供 UI 直接渲染）。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

from dataclasses import (
    asdict,
    dataclass,
    field,
)
from typing import NamedTuple, Optional

from .relay_constants import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    ROLE_MASTER,
    ROLE_SLAVE,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_LABELS,
    STATE_PENDING_CONFIRM,
    STATE_ROLE_CONFLICT,
    STATE_UNCONFIGURED,
    STATE_WAITING_PEER,
    normalize_role,
    role_label,
)
from .relay_heartbeat import (
    PeerHeartbeat,
    is_own_process_leftover,
    observer_clock_skew_ms,
)
from . import relay_store
from .relay_trust import TrustStore


# ----------------------------------------------------------------------
# 角色仲裁
# ----------------------------------------------------------------------
@dataclass
class RoleArbitration:
    selfRole: str
    masters: list = field(default_factory=list)
    slaves: list = field(default_factory=list)
    onlineMasters: list = field(default_factory=list)
    staleMasters: list = field(default_factory=list)
    master: str = ''
    suggestedMaster: str = ''
    conflict: bool = False
    reason: str = ''

    @property
    def selfRoleLabel(self) -> str:
        return role_label(self.selfRole)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['selfRoleLabel'] = self.selfRoleLabel
        return data


def arbitrate_roles(
    self_id: str,
    self_role: str,
    peers: dict[str, 'PeerHeartbeat'],
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    accepted_master_ids: Optional[list] = None,
    now: Optional[float] = None,
    skew_ms: float = 0.0,
) -> RoleArbitration:
    """把「本机声明的角色 + 所有对端声明的角色」仲裁成一主多从拓扑。

    规则刻意做得**无协商、可复算**：不引入任何选举或租约文件（共享目录上做
    互斥会引入不确定行为），而是靠一条确定性约定 —— **在线的主控端里 peerId
    字典序最小者为唯一合法主控**。所有端按同一份心跳算出同一个结果。

    **只有在线的主控端参与仲裁与冲突判定**（``staleMasters`` 单独列出，仅供
    提示）。这不是优化，是必须的：``peers/<id>.json`` 没有清理机制，一台曾经
    作主控的机器退役或关机后，它的心跳文件会永远留在共享目录里并继续声明
    自己为主控。若把它算进 ``masters``，剩下的机器会永久判成角色冲突，
    而冲突是 fail-closed 的 —— 从机会拒绝每一个请求，用户还被提示去改一台
    已经关机的机器，自救无门。同理，已确认名单里那些不在线的旧主控也不能
    把主控位占住（名单顺序是"最早确认的在前"），否则在跑的主控会被判成
    "不是当前主控端"。

    多个**在线**主控时不做自动降级：``conflict`` 置位，由调用方 fail-closed
    （暂停投递与执行），并提示用户改配置 —— 悄悄改变用户的角色比直接报错更危险。

    「在线」按**共享目录的时基**判定（``skew_ms``：服务器时钟 - 本机时钟）：
    跨机时各机的墙上时钟不可比，直接拿对端时间戳减本机时钟会把在线判成离线
    （或反过来）。
    """
    roster: dict[str, str] = {}
    for peer_id, beat in (peers or {}).items():
        if peer_id and peer_id != self_id:
            roster[str(peer_id)] = normalize_role(beat.role)
    roster[str(self_id)] = normalize_role(self_role)

    masters = sorted(pid for pid, role in roster.items() if role == ROLE_MASTER)
    slaves = sorted(pid for pid, role in roster.items() if role != ROLE_MASTER)
    online_masters = sorted(
        pid for pid in masters
        if pid == self_id or not peers[pid].is_stale(interval_s, now, skew_ms=skew_ms)
    )
    stale_masters = [pid for pid in masters if pid not in set(online_masters)]

    accepted = [str(pid) for pid in (accepted_master_ids or []) if pid]
    # 已确认但当前不在线、或已改声明为从机的旧主控不占主控位。
    accepted_online = [pid for pid in accepted if pid in online_masters]
    if accepted_online:
        master = accepted_online[0]
        reason = f'按已确认名单选择主控端 {master}'
    elif online_masters:
        master = online_masters[0]
        if accepted:
            reason = f'已确认的主控端不在线，改用在线主控端 {master}'
        else:
            reason = f'按 peerId 字典序选定主控端 {master}'
    else:
        master = ''
        reason = '未发现在线的主控端' if masters else '未发现任何主控端'

    conflict = len(online_masters) > 1
    if conflict:
        reason = (f'检测到 {len(online_masters)} 个在线主控端'
                  f'（{"、".join(online_masters)}），建议保留 {online_masters[0]}')

    return RoleArbitration(
        selfRole=normalize_role(self_role),
        masters=masters,
        slaves=slaves,
        onlineMasters=online_masters,
        staleMasters=stale_masters,
        master=master,
        suggestedMaster=online_masters[0] if online_masters else '',
        conflict=conflict,
        reason=reason,
    )


# ----------------------------------------------------------------------
# 连接状态（供 UI 直接渲染）
# ----------------------------------------------------------------------
@dataclass
class PeerView:
    peerId: str
    role: str = ROLE_SLAVE
    online: bool = False
    trusted: bool = False
    needsConfirmation: bool = False
    trustReason: str = ''
    deviceCount: int = 0
    devices: list = field(default_factory=list)
    heartbeatAt: str = ''
    ageMs: float = -1.0
    carrier: str = ''
    fingerprint: str = ''
    host: str = ''
    # 实例级身份与它在池里占的槽位（老版本对端为空/0）。
    nodeId: str = ''
    nodeSlot: int = 0
    # 读取期信息：显示名称与终端恢复依据，不改变心跳的持久格式。
    deploymentId: str = ''
    displayLabel: str = ''
    startedAt: str = ''

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['roleLabel'] = self.roleLabel
        data['devices'] = list(self.devices)
        return data


@dataclass
class RelayHealth:
    enabled: bool = False
    state: str = STATE_UNCONFIGURED
    role: str = ROLE_SLAVE
    peerId: str = ''
    carrierKind: str = ''
    carrierLabel: str = ''
    hint: str = ''
    master: str = ''
    masters: list = field(default_factory=list)
    slaves: list = field(default_factory=list)
    onlineMasters: list = field(default_factory=list)
    staleMasters: list = field(default_factory=list)
    onlineSlaves: list = field(default_factory=list)
    pendingConfirmation: list = field(default_factory=list)
    conflict: bool = False
    deviceCount: int = 0
    # 本机**自己**换过标识后留在共享目录里的旧心跳。它们不是对端，绝不能进
    # ``peers``（否则界面会把「自己」显示成一台对端），但要让用户知道被忽略了。
    selfGhosts: list = field(default_factory=list)
    # **别的机器**换过标识后留下的旧心跳（同指纹同主机、已被该机器在线的标识
    # 取代）。同样不是对端：不滤掉的话，对端列表里会凭空多出一台永远离线的
    # 「主控」，用户看到的就是「从机找到两个主机」。
    supersededPeers: dict = field(default_factory=dict)
    # 池内**发现了但还没配对**的实例。它们是有意不进 ``peers`` 的：配对是准入门槛，
    # 未配对者既不进对端列表也不参与角色仲裁。但完全藏起来又会让人困惑
    # （「明明对端在跑，怎么什么都看不到」），所以单独列出来供界面提示。
    unpairedPeers: list = field(default_factory=list)
    peers: list = field(default_factory=list)
    link: dict = field(default_factory=dict)

    @property
    def stateText(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def roleLabel(self) -> str:
        return role_label(self.role)

    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'state': self.state,
            'stateText': self.stateText,
            'role': self.role,
            'roleLabel': self.roleLabel,
            'peerId': self.peerId,
            'carrierKind': self.carrierKind,
            'carrierLabel': self.carrierLabel,
            'hint': self.hint,
            'master': self.master,
            'masters': list(self.masters),
            'slaves': list(self.slaves),
            'onlineMasters': list(self.onlineMasters),
            'staleMasters': list(self.staleMasters),
            'onlineSlaves': list(self.onlineSlaves),
            'pendingConfirmation': list(self.pendingConfirmation),
            'conflict': self.conflict,
            'deviceCount': self.deviceCount,
            'selfGhosts': list(self.selfGhosts),
            'supersededPeers': dict(self.supersededPeers),
            'unpairedPeers': [dict(item) for item in self.unpairedPeers],
            'peers': [view.to_dict() for view in self.peers],
            'link': dict(self.link),
        }


def is_self_ghost(
    beat: 'PeerHeartbeat',
    *,
    self_fingerprint: str = '',
    self_peer_id: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
    beat_alive: bool = False,
) -> bool:
    """这条心跳是不是「本机自己」不再使用的旧身份？

    同机改了 ``peerId``（设置里的「本机标识」）之后，旧标识的心跳文件会留在共享
    目录里 —— 单写者原则下谁也不能删别人的文件，而它确实也不算对端。它有两个
    来源，必须都认，否则界面会把「本机自己」显示成一台离线对端（角色还是改之前
    那个）——用户看到的就是「对端标识不对」：

    * **同一进程**改标识（改完在进程内重启中转节点）：``pid`` 相同、``startedAt``
      不同。改完立即出现，也由 ``purge_self_heartbeats`` 当场清掉。
    * **上一个进程**留下的：改完标识后整个应用重启过（或这条残留是带缺陷的旧版本
      写下的），``pid`` 已经对不上了。此时判据只能落在时间上：**它的最后心跳早于
      本机这次启动**。

    ``beat_alive`` 是这条时间判据的**一票否决**，别省：同机多开时，**先起**的那个
    实例的最后心跳必然早于后起实例的启动时刻 —— 那正是「残留」的时间特征。只看
    时间会把活着的兄弟吞掉（表现：后起的实例在头几秒看不见先起的那个，直到它下
    一拍心跳才恢复）。真正的残留特征是「**不再更新**」，所以还在跳的一律不是幽灵。

    只按指纹判断是不够的：**同一台机器上跑两个实例**（同机双实例验证）指纹相同，
    那是真对端，不能被吞掉 —— 所以两种情况都必须先排除「这就是我现在用的标识」。
    """
    if self_peer_id and beat.peerId == self_peer_id:
        return False
    if is_own_process_leftover(
        beat, self_fingerprint=self_fingerprint, self_pid=self_pid,
        self_started_at=self_started_at,
    ):
        return True
    # 还在跳动 ⇒ 是活着的同机兄弟，不是本机放弃的旧身份。
    if beat_alive:
        return False
    # 上一个进程的残留：同指纹、不同 pid、且在本机启动前就停了。
    if not self_fingerprint or beat.fingerprint != self_fingerprint:
        return False
    if not self_started_at:
        return False
    return 0.0 < relay_store._epoch_ms(beat.heartbeatAt) < relay_store._epoch_ms(self_started_at)


def is_superseded_ghost(beat: 'PeerHeartbeat', sibling: 'PeerHeartbeat') -> bool:
    """``beat`` 是不是「同一台机器上已被 ``sibling`` 取代」的旧标识？

    调用方负责先确认两者**同指纹同主机**（同一台机器，时钟也相同，下面的时间
    比较才成立）且 ``sibling`` 在线、``beat`` 不在线。这里只回答最后一步：

    它的最后一次心跳**早于**兄弟标识的启动时刻 —— 也就是说，在这台机器当前的
    标识起来之前它就已经不再心跳了，属于被取代的旧标识。

    这条时间判据不能省。同一台机器上**同时**跑两个实例（同机双实例验证）指纹也
    相同，但那是真对端：刚被关掉的那个兄弟实例，它的最后心跳必然**晚于**对方启动
    （对方在跑的时候它还在心跳），所以不会被这条吞掉，仍会留在列表里灰显。
    """
    beat_ms = relay_store._epoch_ms(beat.heartbeatAt)
    sibling_start_ms = relay_store._epoch_ms(sibling.startedAt)
    if beat_ms <= 0 or sibling_start_ms <= 0:
        return False
    return beat_ms < sibling_start_ms


class _PeerScan(NamedTuple):
    """一次对端扫描的结果：时钟归算系数、在线条目与两类被忽略的标识。"""

    skew: float
    entries: list
    ghosts: list
    superseded: dict


def _scan_peers(*, peers, self_id, self_fingerprint, self_pid, self_started_at,
                interval_s, now, server_skew_ms) -> _PeerScan:
    """判定每个心跳的时效与身份，先分出「谁在线、谁是幽灵」。

    ``ghosts`` 是本机自己放弃的旧标识（同机改过「本机标识」留下的残留心跳）；
    ``superseded`` 是别的机器上已被新标识取代的旧标识。两者都只进提示，不进对端列表。
    """
    # 时效判定先把本机时钟归算到**共享目录服务器的时钟**上：两台机器的墙上时钟
    # 差几百秒是常态（现场实测两台 PC 差 393s、PC 与共享服务器差 669s）。不归算
    # 的话，快的一方永远看不到慢的一方在线，慢的一方反而把快的一方当成刚心跳过，
    # 于是「从机说已连接、主控说尚未发现在线的从机」这种自相矛盾的状态必然出现。
    if server_skew_ms is None:
        server_skew_ms = observer_clock_skew_ms(peers, self_id)
    skew = float(server_skew_ms or 0.0)

    ghosts: list[str] = []
    # 先只判定「谁在线」，建视图放到过滤之后：同机旧标识要先被认出来，否则它会带着
    # 旧角色进列表、进仲裁。
    entries: list[tuple[str, PeerHeartbeat, bool]] = []
    for peer_id in sorted(peers):
        if peer_id == self_id:
            continue
        beat = peers[peer_id]
        # 先算时效：它既是「在不在线」，也是幽灵判定的**一票否决**（还在跳的同机
        # 实例是兄弟，不是本机放弃的旧身份 —— 见 is_self_ghost 的 beat_alive）。
        alive = not beat.is_stale(interval_s, now, skew_ms=skew)
        if is_self_ghost(
            beat, self_fingerprint=self_fingerprint, self_peer_id=self_id,
            self_pid=self_pid, self_started_at=self_started_at, beat_alive=alive,
        ):
            ghosts.append(peer_id)
            continue
        entries.append((peer_id, beat, alive))

    # **别的机器**的旧标识：同一台机器（同指纹同主机）有在线的标识时，那些已经
    # 静默的旧标识就不是对端 —— 用户改过「本机标识」的话，旧文件按单写者原则谁也
    # 不会去删，留着它只会让对端列表里多出一台永远离线的「主控」。
    online_by_machine: dict[tuple[str, str], str] = {}
    for peer_id, beat, online in entries:
        if online and beat.fingerprint:
            online_by_machine.setdefault((beat.fingerprint, beat.host), peer_id)
    superseded: dict[str, str] = {}
    for peer_id, beat, online in entries:
        if online or not beat.fingerprint:
            continue
        sibling_id = online_by_machine.get((beat.fingerprint, beat.host))
        if sibling_id and is_superseded_ghost(beat, peers[sibling_id]):
            superseded[peer_id] = sibling_id

    return _PeerScan(skew=skew, entries=entries, ghosts=ghosts, superseded=superseded)


def _build_peer_views(*, entries, superseded, now, skew,
                      trust, pairing_enabled, paired_node_ids):
    """过滤后建对端视图，顺带分出未配对实例。

    未配对实例既不进 ``views`` 也不进仲裁，只作为提示材料（模型 A，见 ``docs/v6`` §9）。
    """
    views: list[PeerView] = []
    unpaired: list[dict] = []
    unpaired_ids: set[str] = set()
    for peer_id, beat, online in entries:
        if peer_id in superseded:
            continue
        # --- 准入门槛（模型 A）---
        # 排在幽灵过滤**之后**：幽灵压根不是对端（连「未配对」都算不上），两件事
        # 混在一起只会让提示互相抵消。这里只做「能不能看见」，真正的「能不能说话」
        # 由 ``RelayNode._authorize`` 与投递前预检负责 —— 读侧过滤挡不住已经写进
        # 分区目录的请求。
        if pairing_enabled:
            allowed = paired_node_ids or set()
            if not beat.nodeId or beat.nodeId not in allowed:
                unpaired_ids.add(peer_id)
                unpaired.append({
                    'peerId': peer_id,
                    'nodeId': beat.nodeId,
                    'role': normalize_role(beat.role),
                    'roleLabel': role_label(beat.role),
                    'online': online,
                    'ageMs': beat.age_ms(now, skew_ms=skew),
                    'host': beat.host,
                    'deviceCount': len(beat.devices),
                    # 老版本对端不上报实例键：不是「另一个实例」，而是「版本过旧」。
                    # 分开标出来，用户才知道该升级哪一端，而不是反复点「配对」。
                    'legacy': not beat.nodeId,
                })
                continue
        trusted, trust_reason = (True, 'ok') if trust is None else trust.check(peer_id, beat.fingerprint)
        views.append(PeerView(
            peerId=peer_id,
            role=normalize_role(beat.role),
            online=online,
            trusted=trusted,
            needsConfirmation=online and not trusted,
            trustReason=trust_reason,
            deviceCount=len(beat.devices),
            devices=list(beat.devices),
            heartbeatAt=beat.heartbeatAt,
            ageMs=beat.age_ms(now, skew_ms=skew),
            carrier=beat.carrier,
            fingerprint=beat.fingerprint,
            host=beat.host,
            nodeId=beat.nodeId,
            nodeSlot=int(beat.nodeSlot or 0),
            startedAt=beat.startedAt,
        ))
    return views, unpaired, unpaired_ids


def _apply_arbitration(health, *, peers, ghosts, superseded, unpaired_ids, views,
                       self_id, self_role, interval_s, accepted_master_ids, now, skew):
    """做角色仲裁，并把结果写进 ``health``；返回仲裁对象供提示层使用。"""
    # 仲裁也不能看见自己的旧身份：那条心跳的角色是改之前那个，留在名单里会让从机
    # 以为自己有个「待确认的主控端」（或者反过来判成角色冲突、直接停摆透传）。
    # 别的机器的旧标识同理：它的旧角色（很可能也是主控）会凭空多出一个主控端，
    # 把在跑的两端直接判成角色冲突 —— 而冲突是 fail-closed 的。
    # 未配对的实例也不能进：它连对端都还不是，让它参与仲裁等于让未授权的实例投票。
    live_peers = {pid: beat for pid, beat in peers.items()
                  if pid not in ghosts and pid not in superseded and pid not in unpaired_ids}
    arbitration = arbitrate_roles(
        self_id, self_role, live_peers,
        interval_s=interval_s, accepted_master_ids=accepted_master_ids, now=now,
        skew_ms=skew,
    )
    health.masters = arbitration.masters
    health.slaves = arbitration.slaves
    health.onlineMasters = arbitration.onlineMasters
    health.onlineSlaves = sorted(v.peerId for v in views if v.role == ROLE_SLAVE and v.online)
    health.master = arbitration.master
    health.staleMasters = list(arbitration.staleMasters)
    health.conflict = arbitration.conflict
    health.pendingConfirmation = sorted(v.peerId for v in views if v.needsConfirmation)
    health.deviceCount = sum(v.deviceCount for v in views if v.online and v.role == ROLE_SLAVE)
    return arbitration


def _compose_state(health, arbitration, *, views, unpaired, enabled, link, link_state,
                   error, self_role, accepted_master_ids, local_device_count) -> RelayHealth:
    """拼出给用户看的一句话结论，并定下最终状态。"""
    # 退役机器留在共享目录里的主控心跳：不影响结论，但要让用户知道它被忽略了，
    # 否则「对端列表里有个永远离线的已确认主控」会一直是个谜。
    stale_note = ''
    if arbitration.staleMasters:
        stale_note = (f'；另有 {len(arbitration.staleMasters)} 个已离线的主控端记录'
                      f'（{"、".join(arbitration.staleMasters)}），不参与仲裁')

    # 别的机器的旧标识：也要点名。不点的话，「我明明把标识改过了，怎么对端列表里
    # 还/不还多一台」都会变成谜 —— 它不是消失了，是被认成同一台机器的旧身份忽略了。
    superseded_note = ''
    if health.supersededPeers:
        pairs = '、'.join(f'{pid}（已被 {health.supersededPeers[pid]} 取代）'
                          for pid in sorted(health.supersededPeers))
        superseded_note = (f'；另有 {len(health.supersededPeers)} 条同一台机器的旧标识记录：'
                           f'{pairs}，已忽略')

    # 本机自己的旧标识：不当作对端，但要在提示里点名，否则用户会以为有一台连不上
    # 的机器（典型来源就是在设置里改了「本机标识」）。
    ghost_note = ''
    if health.selfGhosts:
        ghost_note = (f'；另有 {len(health.selfGhosts)} 条本机旧标识的残留心跳'
                      f'（{"、".join(health.selfGhosts)}），已按「本机自己」忽略')

    # 未配对实例：必须点名。不点的话「对端明明在跑，为什么列表里没有」会是个谜 ——
    # 它不是没被发现，是被准入门槛挡在外面了（这是设计意图，不是故障）。
    unpaired_note = ''
    if unpaired:
        labels = '、'.join(
            f'{item["peerId"]}（{item["roleLabel"]}{"，离线" if not item["online"] else ""}）'
            for item in sorted(unpaired, key=lambda x: x['peerId'])
        )
        legacy = [item for item in unpaired if item['legacy']]
        unpaired_note = (f'；另有 {len(unpaired)} 个池内未配对实例：{labels}，'
                         f'未配对前不参与通讯与角色仲裁')
        if legacy:
            unpaired_note += (f'（其中 {len(legacy)} 个对端版本过旧，未上报实例键，'
                              f'需把该端也升级到本版本）')

    def _done() -> RelayHealth:
        for note in (stale_note, superseded_note, ghost_note, unpaired_note):
            if note and note not in health.hint:
                health.hint += note
        return health

    if not enabled:
        health.state = STATE_UNCONFIGURED
        health.hint = '跨机中转未启用：在设置页填写角色、本机标识与载体后启用'
        return _done()

    if link_state in (STATE_DISCONNECTED, STATE_UNCONFIGURED) or error:
        health.state = STATE_DISCONNECTED
        health.hint = f'载体不可用：{error or link.get("lastError") or link.get("detail") or "未知原因"}'
        return _done()

    if arbitration.conflict:
        health.state = STATE_ROLE_CONFLICT
        health.hint = f'{arbitration.reason}；已暂停透传，请把其余主控改为从机'
        return _done()

    if self_role == ROLE_MASTER:
        online_slaves = [v for v in views if v.role == ROLE_SLAVE and v.online]
        if online_slaves:
            health.state = STATE_CONNECTED
            health.hint = (
                f'本机为主控端，已连接 {len(online_slaves)} 个从机，'
                f'共 {health.deviceCount} 台远端设备'
            )
            if health.pendingConfirmation:
                health.state = STATE_PENDING_CONFIRM
                health.hint = (
                    f'有 {len(health.pendingConfirmation)} 个从机待确认：'
                    f'{"、".join(health.pendingConfirmation)}'
                )
        else:
            health.state = STATE_WAITING_PEER
            # 「等待对端」和「对端在跑但没配对」是两件完全不同的事，混成一句话会让
            # 用户去查网络/心跳。这里按在线未配对实例的有无分开表述。
            online_unpaired = [item for item in unpaired if item['online']]
            health.hint = (
                f'本机为主控端，发现 {len(online_unpaired)} 个未配对的实例，配对后才能通讯'
                if online_unpaired else '本机为主控端，尚未发现在线的从机'
            )
        return _done()

    # 从机
    if not arbitration.onlineMasters:
        health.state = STATE_WAITING_PEER
        online_unpaired = [item for item in unpaired if item['online']]
        health.hint = (
            f'本机为从机，发现 {len(online_unpaired)} 个未配对的主控端，配对后才接受其请求'
            if online_unpaired else '本机为从机，未发现在线的主控端'
        )
        return _done()
    accepted = [str(pid) for pid in (accepted_master_ids or []) if pid]
    if not accepted:
        health.state = STATE_PENDING_CONFIRM
        health.hint = f'本机为从机，主控端 {arbitration.master} 待确认后才接受其请求'
        return _done()
    health.state = STATE_CONNECTED
    local = max(0, int(local_device_count or 0))
    health.hint = (
        f'本机为从机，由主控端 {arbitration.master} 控制，{local} 台设备已对外共享'
        if local else f'本机为从机，由主控端 {arbitration.master} 控制'
    )
    return _done()


def evaluate_health(
    *,
    enabled: bool,
    role: str,
    self_id: str,
    peers: Optional[dict[str, 'PeerHeartbeat']] = None,
    link: Optional[dict] = None,
    trust: Optional[TrustStore] = None,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    accepted_master_ids: Optional[list] = None,
    carrier_kind: str = '',
    error: str = '',
    local_device_count: int = 0,
    self_fingerprint: str = '',
    self_pid: int = 0,
    self_started_at: str = '',
    now: Optional[float] = None,
    server_skew_ms: Optional[float] = None,
    paired_node_ids: Optional[set] = None,
    pairing_enabled: bool = False,
) -> RelayHealth:
    """把心跳、链路状态与信任名单汇总成一份可直接显示的连接状态。

    流程分四步，各自独立可测：扫描对端身份（``_scan_peers``）→ 过滤并建视图
    （``_build_peer_views``）→ 角色仲裁（``_apply_arbitration``）→ 拼结论
    （``_compose_state``）。

    ``hint`` 是给用户看的一句话结论，UI 不需要自己拼装角色与在线数。

    ``pairing_enabled`` + ``paired_node_ids`` 是**准入门槛**（模型 A，见
    ``docs/v6`` §9）：启用配对后，不在名单里的实例一律不进 ``peers``、不进角色
    仲裁，只出现在 ``unpairedPeers`` 里供界面提示。名单按**实例键 nodeId** 匹配，
    不按 peerId —— peerId 是可变的显示名，改一次名就等于换了个身份；而老版本对端
    压根不上报 ``nodeId``（读到空串），此时按 fail-closed 判为未配对，并在提示里
    点明「对端版本过旧」，而不是默认放行。
    """
    peers = dict(peers or {})
    link = dict(link or {})
    self_role = normalize_role(role)
    health = RelayHealth(
        enabled=bool(enabled),
        role=self_role,
        peerId=str(self_id or ''),
        carrierKind=carrier_kind,
        link=link,
    )
    link_state = str(link.get('state') or '')

    scan = _scan_peers(
        peers=peers, self_id=self_id, self_fingerprint=self_fingerprint,
        self_pid=self_pid, self_started_at=self_started_at,
        interval_s=interval_s, now=now, server_skew_ms=server_skew_ms,
    )
    views, unpaired, unpaired_ids = _build_peer_views(
        entries=scan.entries, superseded=scan.superseded, now=now, skew=scan.skew,
        trust=trust, pairing_enabled=pairing_enabled, paired_node_ids=paired_node_ids,
    )
    health.peers = views
    health.selfGhosts = sorted(scan.ghosts)
    health.supersededPeers = dict(scan.superseded)
    health.unpairedPeers = unpaired

    arbitration = _apply_arbitration(
        health, peers=peers, ghosts=scan.ghosts, superseded=scan.superseded,
        unpaired_ids=unpaired_ids, views=views, self_id=self_id, self_role=self_role,
        interval_s=interval_s, accepted_master_ids=accepted_master_ids,
        now=now, skew=scan.skew,
    )
    return _compose_state(
        health, arbitration, views=views, unpaired=unpaired, enabled=enabled,
        link=link, link_state=link_state, error=error, self_role=self_role,
        accepted_master_ids=accepted_master_ids, local_device_count=local_device_count,
    )
