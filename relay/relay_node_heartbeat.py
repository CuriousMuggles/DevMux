"""RelayNode 的心跳与在线状态（``NodeHeartbeatMixin``）。

三件事：启停后台循环、按拍写心跳并清理自己的旧标识、周期性刷新本机设备列表。
共享目录 IO 全部在后台线程里，``self._io_lock`` 只保护内存状态 —— 慢网络不能
挡住在线判定和界面请求（这条约束见 relay_node 的模块文档）。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import relay
from . import relay_adb
from . import relay_carrier
from . import relay_journal
from .relay_node_constants import (
    APP_STARTED_MONOTONIC,
    APP_VERSION,
    DEVICE_MISS_THRESHOLD,
)


class NodeHeartbeatMixin:
    """启停与心跳。依赖 ``RelayNode.__init__`` 建立的属性，不单独实例化。"""

    # --- 生命周期 ---
    def start(self) -> tuple[bool, str]:
        """打开载体、校验清单、启动后台循环。返回 ``(是否成功, 原因)``。"""
        with self._io_lock:
            if self.control_journal:
                try:
                    self._journal_queue(self.peer_id)
                except (OSError, ValueError, relay_journal.sqlite3.Error) as exc:
                    return False, f'控制日志本机状态库不可用：{exc}'
            if not self.carrier.open():
                status = self.carrier.status()
                reason = status.get('lastError') or status.get('detail') or '载体不可用'
                return False, reason
            self.queue.ensure_layout()
            # 启用池才把池特性写进清单（``manifest_features`` 是这条规则的唯一出口）。
            relay_carrier.ensure_manifest(
                self.queue, self.carrier.kind, relay.manifest_features(self.pool_id)
            )
            ok, reason = relay_carrier.check_manifest(relay.read_json(self.queue.manifest_path))
            if not ok:
                # 对端要求本版本不认识的特性时明确拒绝启动，而不是半功能运行。
                self.carrier.close()
                return False, reason
        self._started_at = relay.now_iso()
        # 改过「本机标识」再启动时，旧标识的心跳会留在共享目录里。单写者原则禁止删
        # 别人的文件，但那是**本进程自己**写的，谁写谁清。不清理它，界面就会把本机
        # 显示成一台离线「对端」（角色还是改之前那个），看起来就像对端标识不对。
        # 删不掉（只读共享目录、被占用）也不影响启动：evaluate_health 的幽灵过滤兜底。
        try:
            removed = relay.purge_self_heartbeats(
                self.queue.peers_dir,
                fingerprint=self.host_fingerprint,
                pid=os.getpid(),
                started_at=self._started_at,
                keep_peer_id=self.peer_id,
            )
        except Exception:
            removed = []
        if removed:
            self._emit('info', f'已清理本机旧标识的残留心跳：{"、".join(removed)}')
        # 异常退出（崩溃/断电）会在 peers/ 留下本机旧进程的 pulse 日志（1-2MB），
        # 正常退出路径已由 drop_own_pulse 删除；这里按「pid 已不存活」补收。
        try:
            dead_pulses = relay.purge_dead_pulses(self.queue.peers_dir, self.peer_id,
                                                  self_pid=os.getpid())
        except Exception:
            dead_pulses = []
        if dead_pulses:
            self._emit('info', f'已清理本机旧进程的 pulse 心跳日志：{len(dead_pulses)} 个')
        self.queue.prepare_fast_results()
        self._stop_flag.clear()
        self._heartbeat_write_lock = threading.Lock()
        # 心跳独立于设备枚举、目录扫描和请求处理，未配对也持续公告在线。
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f'relay-heartbeat-{self.peer_id}', daemon=True)
        self._heartbeat_thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor_heartbeats,
                                                name=f'relay-heartbeat-read-{self.peer_id}', daemon=True)
        self._monitor_thread.start()
        # 先打一拍：让 health() 立刻是真实结论，而不是 RelayHealth 的占位值。
        self._beat()
        self._thread = threading.Thread(target=self._loop, name=f'relay-{self.peer_id}', daemon=True)
        self._thread.start()
        self._emit('info', f'节点已启动：{self.host_label}，角色 {relay.role_label(self.role)}，'
                          f'载体 {self.carrier.status().get("kindLabel") or self.carrier.kind}')
        return True, 'ok'

    def stop(self) -> None:
        self._stop_flag.set()
        if self.windows_service is not None:
            self.windows_service.close_sessions()
        if self.extension_tools is not None:
            self.extension_tools.close_owner()
        if self.windows_service is not None:
            if self._owns_windows_service:
                self.windows_service.close()
        self._stop_flag.set()
        with self._windows_stream_lock:
            windows_streams = list(self._windows_streams.values())
            self._windows_streams.clear()
        for stream in windows_streams:
            stream.stop()
        with self._windows_stream_lock:
            serial_streams=list(self._serial_streams.values());self._serial_streams.clear()
        for stream in serial_streams:stream.stop()
        monitor_thread = getattr(self, '_monitor_thread', None)
        if monitor_thread is not None:
            monitor_thread.join(timeout=1.0)
        self._monitor_pool.shutdown(wait=False, cancel_futures=True)
        heartbeat_thread = getattr(self, '_heartbeat_thread', None)
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=3.0)
        # 先停日志流：每条流都会 terminate 自己的 adb 子进程并写一次 meta，
        # 顺序反了会留下「进程已死但 meta 还写着 running」的假状态。
        for stream_id in list(self._streams):
            stream = self._streams.pop(stream_id, None)
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:  # 清理路径绝不抛出
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        # 后台发现仍可能持有共享文件句柄；退出前给它们有限时间完成，避免旧任务
        # 在新实例启动后继续扫描，或让文件清理撞上尚未关闭的读取句柄。
        deadline = time.monotonic() + 3.0
        pending_readers = list(self._monitor_futures.values())
        pending_readers.extend((getattr(self, '_discovery_future', None), getattr(self, '_presence_future', None),
                                getattr(self, '_heartbeat_compat_future', None)))
        for future in pending_readers:
            if future is not None:
                try:
                    future.result(timeout=max(0.0, deadline - time.monotonic()))
                except Exception:
                    pass
        # 远端会话：先停本端读取线程，再回收从机侧 PTY（meta 要写进共享目录，
        # 必须赶在 carrier.close() 之前）。顺序反了会留下「PTY 已死但 meta 还
        # 写着 running」的假状态，主控那边就会一直等一个不会再来的输出。
        for key in list(self._readers):
            reader = self._readers.pop(key, None)
            if reader is not None:
                try:
                    reader.stop()
                except Exception:
                    pass
        if self._sessions is not None:
            try:
                self._sessions.stop_all()
            except Exception:
                pass
        self._device_pool.shutdown(wait=False, cancel_futures=True)
        self._discovery_pool.shutdown(wait=False, cancel_futures=True)
        self._presence_pool.shutdown(wait=False, cancel_futures=True)
        self._heartbeat_compat_pool.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False)
        self._terminal_pool.shutdown(wait=False, cancel_futures=True)
        self._archive_pool.shutdown(wait=False, cancel_futures=True)
        # 回执到达客户端后仍可能在归档；停止时给读写任务有限时间释放句柄。
        finish_deadline = time.monotonic() + 3.0
        while self._inflight and time.monotonic() < finish_deadline:
            time.sleep(.01)
        with self._archive_lock:
            archives = list(self._archive_futures)
        for future in archives:
            try:
                future.result(timeout=max(0.0, finish_deadline-time.monotonic()))
            except Exception:
                pass
        self._shared_release_pool.shutdown(wait=False, cancel_futures=True)
        # 正常退出删除本机自己的 pulse 心跳日志（谁写谁清），避免每次重启在
        # peers/ 里留下一个 1-2MB 的 pulse 文件。
        try:
            relay.drop_own_pulse(self.queue.peers_dir, relay.PeerHeartbeat(
                peerId=self.peer_id, role=self.role, pid=os.getpid(),
                nodeId=self.node_id, startedAt=self._started_at))
        except Exception:
            pass
        with self._io_lock:
            self.carrier.close()
        if self.pool_id:
            # 谁写谁清：退出时撤掉自己的注册项，槽位立即可复用。
            self.release_registry()
        self._emit('info', '节点已停止')
        if self._journal_runtime is not None and not self._inflight and not self.is_running():
            self._journal_runtime.close()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- 主循环 ---
    def _heartbeat_loop(self) -> None:
        """单独写本机心跳；单次共享目录故障后继续重试。"""
        while not self._stop_flag.is_set():
            started = time.monotonic()
            try:
                self._write_heartbeat(primary_only=True)
            except Exception as exc:
                self._emit('error', f'心跳写入失败：{type(exc).__name__}: {exc}')
            self._stop_flag.wait(max(0.0, self.heartbeat_interval_s - (time.monotonic() - started)))

    def _monitor_heartbeats(self):
        """已发现的对端只读自己的固定心跳路径，不等部署/设备/名册扫描。"""
        while not self._stop_flag.is_set():
            with self._io_lock:
                peers = list(self._peers.items())
            for peer_id, beat in peers:
                if peer_id == self.peer_id or beat.heartbeatSeq <= 0:
                    continue
                future = self._monitor_futures.get(peer_id)
                if future is None or future.done():
                    self._monitor_futures[peer_id] = self._monitor_pool.submit(
                        self._read_peer_sequence, peer_id, beat, self.channel_root(peer_id))
            self._stop_flag.wait(0.25)

    def _read_peer_sequence(self, peer_id, beat, root):
        started = time.monotonic()
        latest = relay.read_heartbeat_pulse(os.path.join(root, relay.SLOT_PEERS), beat)
        elapsed = round((time.monotonic() - started) * 1000)
        with self._io_lock:
            current = self._peers.get(peer_id)
            if (self._stop_flag.is_set() or current is None
                    or (current.nodeId, current.pid, current.startedAt) != (beat.nodeId, beat.pid, beat.startedAt)):
                return
            self._peers[peer_id] = self._sequence_observer.observe(latest, channel=root)
            self._heartbeat_read_metrics[peer_id] = elapsed
            self._health = self._compute_health()
            age = self._peers[peer_id].age_ms()
        self._notice_slow_heartbeat('读取', peer_id, elapsed, latest.heartbeatSeq, age)

    def _notice_slow_heartbeat(self, operation, peer_id, elapsed, sequence, age=None):
        """慢访问限频记录；日志不占共享目录锁，也不改变在线判定。"""
        if elapsed < 1000:
            return
        now = time.monotonic()
        key = (operation, peer_id)
        with self._heartbeat_diagnostics_lock:
            previous = self._heartbeat_slow_notices.get(key)
            if previous is not None and now - previous < 30:
                return
            self._heartbeat_slow_notices[key] = now
        suffix = f'，距最后观测推进 {max(0, round(age))}ms' if age is not None else ''
        self._emit('info', f'心跳{operation}耗时 {elapsed}ms：{peer_id}，序号 {sequence}{suffix}')

    def _beat(self, *, refresh_devices=True) -> None:
        """一次心跳：刷新本机设备 → 写心跳 → 读对端 → 重算状态。

        抽出来是为了让 ``start()`` 也能先打一拍：不这么做，新节点在循环首轮之前
        ``health()`` 还是占位值（``state=unconfigured``），刚保存完配置的界面会先
        闪一下「未启用」，甚至停在那个结论上（状态推送去抖只认变化）。
        """
        # 注册表在共享目录上，不放进 ``_io_lock``：那是网络 IO，占着锁会让界面拉
        # 状态的线程一起卡住。
        if self._stop_flag.is_set():
            return
        if refresh_devices:
            self._refresh_presence()
        # 设备枚举可能等待 adb 超时，不能占用流与控制回执的锁。
        if refresh_devices:
            self._refresh_devices()
        if self._stop_flag.is_set():
            return
        # 慢共享目录扫描不能占住准入及终端请求的锁。
        if refresh_devices:
            heartbeat_thread = getattr(self, '_heartbeat_thread', None)
            if heartbeat_thread is None or not heartbeat_thread.is_alive():
                self._write_heartbeat()
        peers = self._read_shared_peers()
        with self._io_lock:
            self._peers = peers
            self._health = self._compute_health()
        self._notice_state_change()

    def _refresh_presence(self) -> None:
        """刷新池注册项。失败不影响通讯（注册表只是名册），只在流水里报一次。"""
        if self._stop_flag.is_set():
            # 未启用池：目录结构与老版本**逐字节一致**，连 registry/ 都不建。
            # 这是「poolId 为空 = 今天的行为」这条兼容承诺的一部分。
            return
        try:
            if self.pool_id:
                self._refresh_registry()
            roots = relay.deployment_roots(self.base_root)
            # 其他池仍可出现在部署选择器中，但其注册项不能成为当前根的配对候选。
            registries = ({self.pool_id: relay.read_registry(self.root)} if self.pool_id else {})
            if not self._stop_flag.is_set():
                # 完整替换快照，状态线程只读内存；慢名册不能拖住连接状态刷新。
                self._presence_snapshot = (roots, registries)
        except Exception as exc:
            if not self._registry_warned:
                self._registry_warned = True
                self._emit('info', f'池注册表暂不可写（不影响通讯）：{type(exc).__name__}: {exc}')

    def deployment_ids(self) -> list[str]:
        return (list(self._presence_snapshot[0]) if self._presence_snapshot is not None
                else list(relay.deployment_roots(self.base_root)))

    def _loop(self) -> None:
        next_beat = 0.0
        while not self._stop_flag.is_set():
            try:
                if time.time() >= next_beat:
                    # 慢 adb devices 不能让控制队列和目录浏览等到下一轮。
                    if self._device_refresh_future is None or self._device_refresh_future.done():
                        self._device_refresh_future = self._device_pool.submit(self._refresh_devices)
                    if self._presence_future is None or self._presence_future.done():
                        self._presence_future = self._presence_pool.submit(self._refresh_presence)
                    if self._discovery_future is None or self._discovery_future.done():
                        self._discovery_future = self._discovery_pool.submit(self._beat, refresh_devices=False)
                    next_beat = time.time() + self.heartbeat_interval_s
                if self.role == relay.ROLE_SLAVE:
                    self._release_shared_on_master_loss()
                    self._process_requests()
            except Exception as exc:  # 单次异常不能让循环死掉
                self._emit('error', f'中转循环异常：{type(exc).__name__}: {exc}')
            self._stop_flag.wait(self.poll_interval_s)

    def _release_shared_on_master_loss(self):
        sessions = self._sessions
        if sessions is None or not getattr(sessions.backend, 'shared', False):
            return
        # 用已有心跳观测检查，主循环不增加任何共享目录读写。
        skew = relay.observer_clock_skew_ms(self._peers, self.peer_id)
        online = any(beat.role == relay.ROLE_MASTER and not beat.is_stale(self.heartbeat_interval_s, skew_ms=skew)
                     for beat in self._admissible_peers().values())
        if online and not self._health.conflict:
            return
        if not sessions.snapshot():
            return
        future = self._shared_release_future
        if future is None or future.done():
            self._shared_release_future = self._shared_release_pool.submit(sessions.stop_all)

    def _refresh_devices(self) -> None:
        with self._device_refresh_lock:
            self._refresh_devices_once()

    def _refresh_devices_once(self) -> None:
        """刷新本机设备列表。

        带**去抖**：``adb devices -l`` 在 adb server 忙碌时会瞬时返回空列表或漏掉
        某台设备（实测首次调用即可复现）。若直接采信，一次抖动就会让「本机设备」
        在心跳里变成 0 台 —— 对端设备栏会凭空少一台再冒出来，从机自己还会把随后
        的请求误判成「没有匹配的设备」。

        所以：设备必须**连续 ``DEVICE_MISS_THRESHOLD`` 次枚举都没出现**才算真的
        离线，与 ``adb_worker.py`` 对离线设备的去抖语义一致。枚举**报错**时不计
        miss —— 列表本身不可信，不该据此丢设备。
        """
        if not self.local_devices_enabled:
            # 屏蔽本机设备：不去枚举，也不保留任何已知设备。否则对端会收到一份
            # 本机根本没打算共享（甚至本机根本不该看见）的设备清单。
            self._devices = []
            self._device_miss.clear()
            self._device_error = ''
            return
        devices, error = relay_adb.list_devices(self.adb_path)
        self._device_error = error
        if error:
            self._emit('error', f'枚举本机设备失败：{error}')
            return

        seen = {str(device.get('key') or ''): device for device in devices if device.get('key')}
        merged: list[dict] = []
        for key, device in seen.items():
            self._device_miss.pop(key, None)
            merged.append(device)
        # 本次没出现的已知设备：累计 miss，够阈值才剔除（保留上一次的字段，
        # 这样对端看到的仍是最后一台已知设备的状态，而不是空白记录）。
        for device in self._devices:
            key = str(device.get('key') or '')
            if not key or key in seen:
                continue
            misses = self._device_miss.get(key, 0) + 1
            self._device_miss[key] = misses
            if misses < DEVICE_MISS_THRESHOLD:
                self._emit('info', f'设备 {key} 未出现在枚举结果中（{misses}/{DEVICE_MISS_THRESHOLD}）')
                merged.append(device)
            else:
                self._emit('info', f'设备 {key} 连续 {misses} 次未出现，按离线移除')
                self._device_miss.pop(key, None)
        self._devices = merged

    def _managed_devices(self) -> list[dict]:
        """本机对外负责的设备。``managed_serials`` 为空表示管全部。"""
        if not self.managed_serials:
            return list(self._devices)
        wanted = set(self.managed_serials)
        return [
            device for device in self._devices
            if device.get('serial') in wanted or device.get('key') in wanted
        ]

    def _write_heartbeat(self, *, primary_only=False) -> None:
        heartbeat_lock = getattr(self, '_heartbeat_write_lock', None)
        if heartbeat_lock is None:
            self._write_heartbeat_once(primary_only=primary_only)
        else:
            with heartbeat_lock:
                self._write_heartbeat_once(primary_only=primary_only)

    def _write_heartbeat_once(self, *, primary_only=False) -> None:
        # 被停止的发现任务不能晚到写回心跳，复活旧实例或干扰退出清理。
        if self._stop_flag.is_set():
            return
        self._heartbeat_sequence += 1
        beat = relay.PeerHeartbeat(
            peerId=self.peer_id,
            role=self.role,
            appVersion=APP_VERSION,
            pid=os.getpid(),
            startedAt=self._started_at,
            heartbeatAt=relay.now_iso(),
            heartbeatSeq=self._heartbeat_sequence,
            uptimeSeconds=max(0, int(time.monotonic() - APP_STARTED_MONOTONIC)),
            devices=[dict(device) for device in self._managed_devices()],
            fingerprint=self.host_fingerprint,
            carrier=self.carrier.kind,
            linkRttMs=float(self.carrier.status().get('latencyMs') or 0.0),
            host=self.host_label,
            # 实例级身份：对端靠它做准入门判（配对按实例键匹配，不按可变的 peerId）。
            nodeId=self.node_id,
            nodeSlot=self._slot,
            controlJournal=relay_journal.VERSION if self.control_journal else 0,
            connectionScope=self._connection_scope,
        )
        started = time.monotonic()
        relay.write_heartbeat(self.queue.peers_dir, beat, snapshot=True, pulse_only=True)
        self._notice_slow_heartbeat('写入', self.peer_id,
                                   round((time.monotonic() - started) * 1000), beat.heartbeatSeq)
        if primary_only:
            pending = self._heartbeat_compat_future
            if pending is None or pending.done():
                self._heartbeat_compat_future = self._heartbeat_compat_pool.submit(self._publish_heartbeat_compat, beat)
        else:
            self._publish_heartbeat_compat(beat)

    def _publish_heartbeat_compat(self, beat) -> None:
        # 旧版 JSON、快照及清理可能慢；不能阻塞真正用于在线判定的追加心跳。
        with self._heartbeat_compat_lock:
            if not self._stop_flag.is_set():
                relay.write_heartbeat(self.queue.peers_dir, beat, snapshot=True, publish_pulse=False)
