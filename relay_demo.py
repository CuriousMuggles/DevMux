"""跨机中转 —— 主从两个应用的双设备模拟演示。

在一台电脑上同时跑「主应用（主控）」与「从应用（从机）」两个节点进程内实例，
它们通过一个共享目录（模拟中转服务器的共享文件夹）通讯，各自管理一台真实设备：

- 主应用**直接**管理自己的设备（不走中转）；
- 主应用**通过从应用**管理另一台设备（命令、结果、日志都经共享目录透传）。

用法::

    python relay_demo.py                          # 自动挑选两台在线设备
    python relay_demo.py --master de47c9b2 --slave f62ca3df
    python relay_demo.py --root D:\\relay         # 指定共享目录（默认临时目录）
    python relay_demo.py --keep-going             # 演示结束后不停止节点，便于手工观察

这是**演示脚本**，不是产品入口：真机跨机部署时两个应用各自跑自己的实例
（``relay_node.RelayNode``），共享目录指向同一个中转服务器。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

import relay
import relay_node

WIDTH = 68


# ----------------------------------------------------------------------
# 输出辅助
# ----------------------------------------------------------------------
def _rule(char: str = '-') -> None:
    print(char * WIDTH)


def _stage(number, title: str) -> None:
    print()
    _rule('=')
    print(f'  阶段 {number}：{title}')
    _rule('=')


def _item(text: str = '') -> None:
    print(f'  {text}' if text else '')


def _make_sink(tag: str):
    """把节点事件转成一行带角色前缀的日志。"""
    labels = {
        'info': '信息', 'state': '状态', 'command': '执行', 'result': '结果',
        'reject': '拒绝', 'stream': '流  ', 'trust': '信任', 'error': '错误',
    }

    def sink(event: dict) -> None:
        label = labels.get(event.get('kind', ''), event.get('kind', ''))
        print(f'  [{tag}] {label} | {event.get("text", "")}')

    return sink


def _print_health(node: relay_node.RelayNode, title: str) -> None:
    health = node.refresh()
    _item(f'{title}：{health.stateText} —— {health.hint}')
    for view in health.peers:
        online = '在线' if view.online else '离线'
        trust = '已确认' if view.trusted else view.trustReason
        _item(f'    · 对端 {view.peerId}（{view.roleLabel}·{online}）'
              f' 设备 {view.deviceCount} 台　{trust}')


def _print_outcome(label: str, outcome: relay_node.ExecOutcome) -> None:
    if outcome.error:
        _item(f'{label}：失败 —— {outcome.error}')
        return
    code = outcome.exitCode if outcome.exitCode is not None else '不可得'
    text = (outcome.stdout or '').strip() or (outcome.stderr or '').strip()
    _item(f'{label}：退出码 {code}，耗时 {outcome.elapsedMs}ms'
          f'{"（已截断）" if outcome.truncated else ""} → {text}')


def _print_result(label: str, result: relay.Result) -> None:
    if result.status != relay.STATUS_OK:
        _item(f'{label}：{result.status} —— {result.error}')
        return
    code = result.exitCode if result.exitCode is not None else '不可得'
    text = (result.stdout or '').strip() or (result.stderr or '').strip()
    _item(f'{label}：退出码 {code}，耗时 {result.durationMs}ms，'
          f'由 {result.agentId} 执行 → {text}')


def _print_tree(root: str, limit: int = 26) -> None:
    """打印共享目录结构，证明消息都有落盘痕迹（审计）。"""
    shown = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        depth = os.path.relpath(dirpath, root).count(os.sep)
        if depth > 4:
            dirnames[:] = []
            continue
        name = '.' if dirpath == root else os.path.relpath(dirpath, root)
        _item(f'    {name}{os.sep}')
        for filename in sorted(filenames)[:6]:
            _item(f'      {filename}')
            shown += 1
            if shown >= limit:
                _item('      …（其余略）')
                return


# ----------------------------------------------------------------------
# 设备挑选
# ----------------------------------------------------------------------
def _pick_device(devices: list, serial: str):
    for device in devices:
        if device['serial'] == serial or device['key'] == serial:
            return device
    return None


# ----------------------------------------------------------------------
# 演示主体
# ----------------------------------------------------------------------
def run_demo(args) -> int:
    devices, error = relay_node.list_devices(args.adb)
    if error:
        _item(f'枚举设备失败：{error}')
        return 2
    online = [device for device in devices if device.get('state') == 'device']
    if len(online) < 2:
        _item(f'需要至少 2 台处于 device 状态的设备，当前只有 {len(online)} 台：'
              f'{[d["key"] for d in online]}')
        return 2

    master_dev = _pick_device(online, args.master) if args.master else online[0]
    if master_dev is None:
        _item(f'找不到主应用要管理的设备：{args.master}')
        return 2
    candidates = [device for device in online if device['key'] != master_dev['key']]
    slave_dev = (_pick_device(candidates, args.slave) if args.slave else None) or candidates[0]
    if slave_dev is None:
        _item(f'找不到从应用要管理的设备：{args.slave}')
        return 2

    base = args.root or os.path.join(
        tempfile.gettempdir(), 'adb-relay-demo', time.strftime('%Y%m%d-%H%M%S'),
    )
    share = os.path.join(base, 'relay-share')
    if not args.root:
        # 默认目录每次干净，才能如实演示「未确认 → 拒绝」这一段；
        # 用户显式指定目录时沿用其中的信任名单（语义等同重启应用）。
        shutil.rmtree(share, ignore_errors=True)
    os.makedirs(share, exist_ok=True)

    print()
    _rule('=')
    print('  跨机中转演示：主应用直管一台设备，经从应用管另一台设备')
    _rule('=')
    _item(f'本机识别到 {len(online)} 台在线设备：{[d["key"] for d in online]}')
    _item(f'共享目录（模拟中转服务器）：{share}')
    _item('拓扑：')
    _item(f'    主应用 pc-a ──┐')
    _item(f'                 ├── 共享目录 ── 命令 / 结果 / 日志')
    _item(f'    从应用 pc-b ──┘')
    _item(f'    主应用直接管理：{master_dev["key"]}（serial={master_dev.get("serial")}，'
          f'transport_id={master_dev.get("transportId")}）')
    _item(f'    从应用对外共享：{slave_dev["key"]}（serial={slave_dev.get("serial")}，'
          f'transport_id={slave_dev.get("transportId")}）')
    _item('    注：这两台嵌入式设备的 ro.product.model 等标准属性为空（adb devices -l 也不带')
    _item('        model 字段），故演示用 /proc/sys/kernel/random/boot_id 区分设备。')

    master = relay_node.RelayNode(
        peer_id='pc-a', role='master', root=share, adb_path=args.adb,
        managed_serials=[master_dev['serial']],
        trust_path=os.path.join(base, 'trust-pc-a.json'),
        host_label='PC-A 办公室主机',
        host_fingerprint=f'pc-a-{master_dev["serial"][:6]}',
        heartbeat_interval_s=1.0,
        event_sink=_make_sink('主应用 pc-a'),
    )
    slave = relay_node.RelayNode(
        peer_id='pc-b', role='slave', root=share, adb_path=args.adb,
        managed_serials=[slave_dev['serial']],
        trust_path=os.path.join(base, 'trust-pc-b.json'),
        host_label='PC-B 实验室主机',
        host_fingerprint=f'pc-b-{slave_dev["serial"][:6]}',
        heartbeat_interval_s=1.0,
        event_sink=_make_sink('从应用 pc-b'),
    )

    try:
        # --- 1 起两个应用并互相发现 ---
        _stage(1, '启动主从两个应用，互相发现')
        ok_m, reason_m = master.start()
        ok_s, reason_s = slave.start()
        if not (ok_m and ok_s):
            _item(f'节点启动失败：主应用 {reason_m}；从应用 {reason_s}')
            return 2
        found = master.wait_for_peers(timeout=10, count=1) and slave.wait_for_peers(timeout=10, count=1)
        _item(f'互相发现对端：{"成功" if found else "失败（超时）"}')
        _print_health(master, '主应用视角')
        _print_health(slave, '从应用视角')

        # --- 2 未确认前不下发命令 ---
        _stage(2, '建立连接前：先提示身份，未确认则拒绝执行')
        for node, tag in ((master, '主应用'), (slave, '从应用')):
            pending = node.pending_confirmations()
            _item(f'{tag}待确认对端：{pending or "（无）"}')
        _item('主应用未确认前先试着下发一条命令：')
        refused = master.exec_remote('pc-b', slave_dev['key'], ['shell', 'cat /proc/sys/kernel/random/boot_id'], timeout=5)
        _print_result('  下发结果', refused)
        _item(f'  说明：命令没有到设备上执行 —— 安全边界落在从机。')

        # --- 3 双方确认，连接建立 ---
        _stage(3, '确认对端身份，连接建立')
        _item('（等同用户在首连确认框里点「确认」）')
        master.confirm_peer('pc-b')
        slave.confirm_peer('pc-a')
        connected = master.wait_for_peers(timeout=3, count=1)
        time.sleep(0.5)
        _print_health(master, '主应用视角')
        _print_health(slave, '从应用视角')
        _item('连接建立提示（主应用侧）：')
        for line in master.link_notice('pc-b').splitlines():
            _item(line)

        # --- 4 设备总览：远端设备与本地设备同列 ---
        _stage(4, '主应用的设备总览（本地与远端无区别）')
        for device in master.all_devices():
            origin = '本地直连' if device['origin'] == 'local' else f'来自 {device["peerId"]}'
            _item(f'    {device["key"]}　{origin}　state={device.get("state")}')
        _item(f'  合计 {len(master.all_devices())} 台（本地 1 台 + 远端 1 台）')

        # --- 5 主应用直接管理自己的设备 ---
        _stage(5, f'主应用直接管理 {master_dev["key"]}（不经中转）')
        _print_outcome('  shell cat /proc/sys/kernel/random/boot_id（本机设备身份）',
                       master.run_local(master_dev['key'],
                                        ['shell', 'cat /proc/sys/kernel/random/boot_id']))
        _print_outcome('  shell uptime（本机负载）',
                       master.run_local(master_dev['key'], ['shell', 'uptime']))
        _print_outcome('  shell id -u（走 shell 通道）',
                       master.run_local(master_dev['key'], ['shell', 'id -u; echo "argv 拼接正常"']))

        # --- 6 主应用经从应用管理另一台设备 ---
        _stage(6, f'主应用经从应用管理 {slave_dev["key"]}（命令 + 结果透传）')
        remote_boot = master.exec_remote(
            'pc-b', slave_dev['key'],
            ['shell', 'cat /proc/sys/kernel/random/boot_id'], timeout=10,
        )
        _print_result('  shell cat /proc/sys/kernel/random/boot_id', remote_boot)
        _print_result('  shell uptime',
                      master.exec_remote('pc-b', slave_dev['key'], ['shell', 'uptime'], timeout=10))
        _print_result('  shell uname -a',
                      master.exec_remote('pc-b', slave_dev['key'],
                                         ['shell', 'uname -a'], timeout=10))
        _print_result('  按 key 自动选路（同一条调用，走到远端）',
                      master.exec_any(f'relay:pc-b:{slave_dev["key"]}',
                                      ['shell', 'echo 远端执行 $(date)'], timeout=10))
        if remote_boot.ok:
            local_boot = (
                master.run_local(master_dev['key'],
                                 ['shell', 'cat /proc/sys/kernel/random/boot_id']).stdout or ''
            ).strip()
            _item(f'  对照：主应用自己那台 boot_id={local_boot}')
            _item(f'        从应用那台   boot_id={remote_boot.stdout.strip()}')
            _item('        → 两个 ID 不同，确认管的是两台不同设备')

        # --- 7 安全边界：非法命令被从机拒绝 ---
        _stage(7, '安全边界：非白名单命令与越权设备指定都被从机拒绝')
        _print_result('  push（不在白名单）',
                      master.exec_remote('pc-b', slave_dev['key'], ['push', '/tmp/a', '/tmp/b'], timeout=5))
        _print_result('  install（不在白名单）',
                      master.exec_remote('pc-b', slave_dev['key'], ['install', '/tmp/a.apk'], timeout=5))
        _print_result('  子命令位置塞设备选择符（试图指定别的设备）',
                      master.exec_remote('pc-b', slave_dev['key'], ['-t', '9', 'shell', 'id'], timeout=5))
        _item('  对照：-t 出现在子命令**之后**是它自己的参数，正常放行 ——')
        _print_result('  logcat -d -t 5（只看最近 5 行）',
                      master.exec_remote('pc-b', slave_dev['key'], ['logcat', '-d', '-t', '5'], timeout=10))

        # --- 8 日志透传 ---
        _stage(8, '日志透传：从机抓远端设备 logcat，主应用按偏移增量读')
        stream_id = 'log-1'
        started = master.start_remote_log(
            'pc-b', slave_dev['key'], ['logcat', '-v', 'brief'], stream_id, timeout=10,
        )
        _print_result('  启动日志流', started)
        if started.ok:
            total = 0
            for round_index in range(1, 4):
                deadline = time.time() + 6
                while time.time() < deadline:
                    data, offset, _rebuilt = master.read_remote_stream('pc-b', stream_id, 4096)
                    if data:
                        break
                    time.sleep(0.2)
                else:
                    data, offset = b'', total
                total = offset
                preview = data.decode('utf-8', 'replace').splitlines()[:3]
                _item(f'  第 {round_index} 轮：新增 {len(data)} 字节，累计 offset={total}')
                for line in preview:
                    _item(f'      | {line[:96]}')
                time.sleep(0.6)
            _print_result('  停止日志流', master.stop_remote_log('pc-b', stream_id, timeout=10))

        # --- 9 从应用下线 ---
        if not args.keep_going:
            _stage(9, '从应用下线后，主应用实时反映连接状态')
            _item('停止从应用节点…')
            slave.stop()
            for _ in range(20):
                time.sleep(0.4)
                health = master.refresh()
                if health.state != relay.STATE_CONNECTED:
                    break
            _print_health(master, '主应用视角')
            lingered = master.remote_devices()
            _item('  远端设备仍留在列表中，仅标记离线（与本地设备灰显同构）：')
            for device in lingered:
                mark = device['relay']
                _item(f'    {device["key"]}　对端 {mark["peerId"]}　'
                      f'relay.online={mark["online"]}　'
                      f'心跳已中断 {mark["ageMs"] / 1000.0:.1f}s')
            if not lingered:
                _item('    （无 —— 若出现这行说明设备被误删而非灰显）')
            _item('  此时对远端设备发起新操作，应快速失败而不是干等超时：')
            began = time.time()
            refused = master.exec_remote(
                'pc-b', slave_dev['key'], ['shell', 'id'], timeout=5,
            )
            _item(f'    耗时 {time.time() - began:.2f}s（等待超时本是 5s）→ {refused.error}')
        else:
            _stage(9, '按 --keep-going 跳过从应用下线演示')

        # --- 10 留痕 ---
        _stage(10, '共享目录里的通讯留痕（归档保留审计）')
        _item(f'  {share}')
        _print_tree(share)
        _item('  说明：requests/ 与 results/ 里活跃消息已被消费归档，'
              'archive/<对端>/ 中按月份保留。')
    finally:
        if not args.keep_going:
            # stop() 可重复调用，这里无需判断是否已停过。
            slave.stop()
            master.stop()
        print()
        _rule('=')
        _item(f'演示结束。共享目录（可打开查看）：{share}')
        _rule('=')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='跨机中转：主从两个应用的双设备模拟演示',
    )
    parser.add_argument('--master', default='', help='主应用直接管理的设备 serial')
    parser.add_argument('--slave', default='', help='从应用对外共享的设备 serial')
    parser.add_argument('--root', default='', help='共享目录（默认临时目录，每次干净）')
    parser.add_argument('--adb', default=relay_node.DEFAULT_ADB_PATH, help='adb 可执行文件路径')
    parser.add_argument('--keep-going', action='store_true', help='演示结束后不停止节点')
    args = parser.parse_args(argv)
    try:
        return run_demo(args)
    except KeyboardInterrupt:
        _item('已中断')
        return 130


if __name__ == '__main__':
    sys.exit(main())
