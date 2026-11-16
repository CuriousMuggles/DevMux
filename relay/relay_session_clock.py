"""每对 peer 的会话级时钟估计。

**为什么需要它**：请求由主控写、从机读，判定"这条命令还新不新"必须有一个两端
都认的时间轴。历史上直接用主控写入的 ``expiresAt`` 去比从机本地 ``time.time()``，
等于假设两端对过时；实测出现过从机快 214 秒导致每条请求一落地即"已过期"，整条
链路全废（界面还显示"已连接"，因为在线判定用的是心跳序号 + 单调钟，不受影响）。

**做法**：把"对端墙钟与本机墙钟差多少"显式测出来，按对端各存一份，用本机单调钟
推进，周期性重测。测量有两个独立来源：

- ``handshake``：``check-connection`` 往返的四时间戳（T1 请求写入 / T2 对端收到 /
  T3 对端完成 / T4 本端读到）。取 RTT 最小的样本 —— RTT 最小意味着路径最短，
  单向延迟不对称带来的误差最小。
- ``mtime``：心跳文件的 mtime 由共享目录服务器打戳，与写者写下的 ``heartbeatAt``
  之差就是"服务器 − 写者"；两端各一份相减，服务器钟抵消，得到两端真实钟差。
  零额外流量、随心跳刷新，但受共享目录时间戳精度限制。

**单调化的方向是有讲究的**：offset 变小会让折算后的时间倒退，本来已过期的请求
会"复活"，这是危险方向，必须限速；offset 变大会让请求提前过期，这是安全方向，
立即生效即可。突变（超过 ``CLOCK_JUMP_ALERT_MS``）说明某台机器的系统时间被外部
改了，必须硬跳跟上并留一条告警，否则永远差那么多。

刻意**不做限幅**：时区设错、虚拟机快照恢复都可能带来几小时偏差，限幅会把真实
偏差截成假的"差不多对上了"，正是这套机制要防的那类故障。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .relay_constants import (
    CLOCK_JUMP_ALERT_MS,
    CLOCK_SLEW_FLOOR_MS,
    CLOCK_SLEW_RATE_MS_PER_S,
    CLOCK_SOURCE_DIVERGE_MS,
    CLOCK_SYNC_TTL_S,
)

SOURCE_HANDSHAKE = 'handshake'
SOURCE_MTIME = 'mtime'


class PeerClock:
    """单个对端的时钟估计。字段全部由 ``SessionClock`` 维护。"""

    __slots__ = (
        'peer_id', 'offset_ms', 'target_ms', 'measured_mono', 'samples',
        'source', 'rtt_ms', 'best_rtt_ms', 'jumps', 'note', 'mtime_ms',
    )

    def __init__(self, peer_id: str = '') -> None:
        self.peer_id = peer_id
        self.offset_ms = 0.0        # 已采纳值：对端墙钟 − 本机墙钟（毫秒）
        self.target_ms = 0.0        # 最新候选值，倒退方向尚未走完
        self.measured_mono = 0.0    # 上次采纳/观察时的本机单调钟
        self.samples = 0
        self.source = ''
        self.rtt_ms = 0.0
        self.best_rtt_ms = 0.0
        self.jumps = 0
        self.note = ''
        self.mtime_ms = 0.0         # 心跳 mtime 推导值：只做展示与互证，不参与判定

    def age_ms(self, now_mono: Optional[float] = None) -> float:
        if not self.measured_mono:
            return float('inf')
        current = time.monotonic() if now_mono is None else float(now_mono)
        return max(0.0, (current - self.measured_mono) * 1000.0)

    def synced(self, now_mono: Optional[float] = None,
               ttl_s: float = CLOCK_SYNC_TTL_S) -> bool:
        return self.samples > 0 and self.age_ms(now_mono) <= ttl_s * 1000.0

    def info(self, now_mono: Optional[float] = None) -> dict:
        age = self.age_ms(now_mono)
        return {
            'clockSkewMs': round(self.offset_ms, 1),
            'clockTargetSkewMs': round(self.target_ms, 1),
            'clockSource': self.source,
            'clockSamples': self.samples,
            'clockRttMs': round(self.rtt_ms, 1),
            'clockBestRttMs': round(self.best_rtt_ms, 1),
            'clockMeasuredAgeMs': (None if age == float('inf') else round(age)),
            'clockSynced': self.synced(now_mono),
            'clockMtimeSkewMs': round(self.mtime_ms, 1) if self.mtime_ms else None,
            'clockJumps': self.jumps,
            'clockNote': self.note,
        }


class SessionClock:
    """按对端维护会话级时钟估计。线程安全；不写全局状态、不碰系统时间。"""

    def __init__(self, *, slew_rate_ms_per_s: float = CLOCK_SLEW_RATE_MS_PER_S,
                 slew_floor_ms: float = CLOCK_SLEW_FLOOR_MS,
                 jump_alert_ms: float = CLOCK_JUMP_ALERT_MS,
                 sync_ttl_s: float = CLOCK_SYNC_TTL_S,
                 diverge_ms: float = CLOCK_SOURCE_DIVERGE_MS) -> None:
        self.slew_rate_ms_per_s = float(slew_rate_ms_per_s)
        self.slew_floor_ms = float(slew_floor_ms)
        self.jump_alert_ms = float(jump_alert_ms)
        self.sync_ttl_s = float(sync_ttl_s)
        self.diverge_ms = float(diverge_ms)
        self._clocks: dict[str, PeerClock] = {}
        self._lock = threading.Lock()

    # --- 观测 ---
    def observe_handshake(self, peer_id: str, offset_ms: float, rtt_ms: float = 0.0,
                          now_mono: Optional[float] = None) -> PeerClock:
        """采纳一次往返测量。调用方负责按 RTT 择优，这里只做单调化。"""
        return self._observe(peer_id, offset_ms, SOURCE_HANDSHAKE, rtt_ms=rtt_ms,
                             now_mono=now_mono)

    def observe_mtime(self, peer_id: str, offset_ms: float,
                      now_mono: Optional[float] = None) -> PeerClock:
        """采纳一次心跳 mtime 推导值。

        **只用于展示与互证，不参与时效判定**：这种值由共享目录服务器的 mtime 打戳
        得来，共享目录一旦不可用就量不出新值；把它缓存下来当判定依据，会在载体
        断开时继续用一个陈旧偏差，比不用更糟。判定只信有 RTT 证据的握手样本。
        """
        peer = str(peer_id or '')
        with self._lock:
            clock = self._clocks.get(peer)
            if clock is None:
                clock = self._clocks[peer] = PeerClock(peer)
            clock.mtime_ms = float(offset_ms)
            if clock.samples and abs(clock.mtime_ms - clock.offset_ms) > self.diverge_ms:
                clock.note = ('心跳 mtime 与握手测量相差 %.0fms：以握手为准（共享目录'
                              '时间戳精度或写者时钟可能异常）'
                              % (clock.mtime_ms - clock.offset_ms))
            return clock

    def _observe(self, peer_id: str, candidate_ms: float, source: str, *,
                 rtt_ms: float = 0.0, now_mono: Optional[float] = None) -> PeerClock:
        peer = str(peer_id or '')
        now = time.monotonic() if now_mono is None else float(now_mono)
        candidate = float(candidate_ms)
        with self._lock:
            clock = self._clocks.get(peer)
            if clock is None:
                clock = self._clocks[peer] = PeerClock(peer)
            if (source == SOURCE_HANDSHAKE and rtt_ms > 0
                    and clock.best_rtt_ms > 0 and rtt_ms > clock.best_rtt_ms * 3.0):
                # 回执隔了很久才被读到：单向延迟占比过高，这种样本的钟差几乎
                # 全是排队时间，采纳只会把估计拖脏。宁可这一轮不用。
                return clock
            self._apply(clock, candidate, now, source, rtt_ms)
            return clock

    def _apply(self, clock: PeerClock, candidate: float, now: float,
               source: str, rtt_ms: float) -> None:
        if clock.samples <= 0:
            clock.offset_ms = clock.target_ms = candidate
            clock.note = ''
        else:
            delta = candidate - clock.offset_ms
            if delta >= 0:
                # 时间前进：请求更早过期，是安全方向，立即生效。
                clock.offset_ms = clock.target_ms = candidate
                clock.note = ''
            elif abs(delta) > self.jump_alert_ms:
                # 突变说明系统时间被外部改过，必须跟上，否则永远差那么多。
                clock.offset_ms = clock.target_ms = candidate
                clock.jumps += 1
                clock.note = f'时钟突变 {delta:+.0f}ms，已硬跳跟随（两端系统时间被调整？）'
            else:
                # 时间倒退会让已过期请求复活：按真实经过时间给出可倒退预算。
                elapsed_s = ((now - clock.measured_mono) if clock.measured_mono else 0.0)
                budget = max(self.slew_floor_ms, self.slew_rate_ms_per_s * max(0.0, elapsed_s))
                clock.offset_ms = clock.offset_ms - min(-delta, budget)
                clock.target_ms = candidate
                clock.note = ('时钟倒退中：已限速 %.0fms，剩余 %.0fms'
                              % (min(-delta, budget), max(0.0, clock.target_ms - clock.offset_ms)))
        clock.measured_mono = now
        clock.samples += 1
        clock.source = source
        if rtt_ms > 0:
            clock.rtt_ms = float(rtt_ms)
            clock.best_rtt_ms = (float(rtt_ms) if clock.best_rtt_ms <= 0
                                 else min(clock.best_rtt_ms, float(rtt_ms)))

    # --- 查询 ---
    def offset_ms(self, peer_id: str, now_mono: Optional[float] = None) -> float:
        """当前采纳的钟差；未测量或已老化时返回 0（退回各机自比的老行为）。"""
        clock = self.clock(peer_id)
        if clock is None or not clock.synced(now_mono, self.sync_ttl_s):
            return 0.0
        return clock.offset_ms

    def clock(self, peer_id: str) -> Optional[PeerClock]:
        with self._lock:
            return self._clocks.get(str(peer_id or ''))

    def synced(self, peer_id: str, now_mono: Optional[float] = None) -> bool:
        clock = self.clock(peer_id)
        return bool(clock and clock.synced(now_mono, self.sync_ttl_s))

    def info(self, peer_id: str, now_mono: Optional[float] = None) -> dict:
        clock = self.clock(peer_id)
        if clock is None:
            return {'clockSkewMs': None, 'clockSource': '', 'clockSamples': 0,
                    'clockRttMs': None, 'clockMeasuredAgeMs': None,
                    'clockSynced': False, 'clockNote': ''}
        return clock.info(now_mono)

    def forget(self, peer_id: str) -> None:
        with self._lock:
            self._clocks.pop(str(peer_id or ''), None)

    def clear(self) -> None:
        with self._lock:
            self._clocks.clear()


def handshake_offset_ms(t1: float, t2: float, t3: float, t4: float) -> tuple[float, float]:
    """由四时间戳算 (钟差, 往返)，单位毫秒。``t1..t4`` 是各端墙钟的 epoch 秒。

    ``t2``/``t3`` 由对端写下（收到 / 完成），``t1``/``t4`` 由本端写下（写入 / 读到）。
    处理耗时对消，剩下的误差只有单向延迟不对称。
    """
    offset = ((t2 - t1) + (t3 - t4)) / 2.0
    rtt = (t4 - t1) - (t3 - t2)
    return offset * 1000.0, max(0.0, rtt * 1000.0)
