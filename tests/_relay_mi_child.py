"""同机多开验证的子进程入口（由 tests/test_relay_multi_instance.py 拉起）。

为什么要真进程：本轮修的是「同一台机器上开两个实例，彼此看不见」。那条链路上有
一个判据只能靠**真实 pid** 才能验 —— ``relay_app._local_taken_peer_ids`` 用
「同机 + pid 仍活着」把「活着的兄弟」和「我上一轮留下的残留心跳」分开，同进程里的
两个对象是模拟不出来的（``os.getpid()`` 永远相同）。

写完结果就进入 ``--hold`` 等待、**不主动退出**：父进程要在两个实例都还活着的时候
读各自的状态，否则先退出的那个会被对方按「同机旧身份」正确过滤掉，测试就测反了。

参数由父进程拼：
    python -B tests/_relay_mi_child.py --root <共享目录> --config <配置路径>
        --role slave --notice '<多开说明，空串=第一个实例>' --wait 6 --out <结果 json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import relay_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, help='共享目录')
    parser.add_argument('--config', required=True, help='本实例的配置文件路径')
    parser.add_argument('--role', default='slave')
    parser.add_argument('--pool', default='')
    parser.add_argument('--notice', default='', help='非空 = 模拟「让出了默认配置」的多开实例')
    parser.add_argument('--wait', type=float, default=6.0, help='启动后等多久再取状态')
    parser.add_argument('--hold', type=float, default=90.0, help='取完状态后停留多久等父进程收尾')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    config = relay_app.load_config(args.config)
    config.enabled = True
    config.role = args.role
    config.root = args.root
    config.poolId = args.pool
    relay_app.save_config(args.config, config)

    controller = relay_app.RelayController(
        config_path=args.config,
        # 本机没有真机也无所谓：关掉本机设备枚举，adb 路径只是一个占位。
        adb_path=os.path.join(os.environ.get('TEMP') or '/tmp', 'no-such-adb.exe'),
        local_devices_enabled=False,
        config_notice=args.notice,
    )
    started, reason = controller.start()
    time.sleep(max(0.5, args.wait))
    status = controller.status()

    payload = {
        'started': bool(started),
        'reason': reason,
        'pid': os.getpid(),
        # peerId 是**生效值**（多开时已退让），configPeerId 是配置里的首选值 ——
        # 两者不同正是「自动退让生效」的证据。
        'peerId': status.get('peerId'),
        'configPeerId': controller.config.peerId,
        'nodeId': status.get('nodeId'),
        'state': status.get('state'),
        'peers': [view.get('peerId') for view in (status.get('peers') or [])],
        # 启用池/配对后，未配对的对端走的是这条路（界面上那张「可配对的目标」列表）。
        'unpairedPeers': [
            {'nodeId': item.get('nodeId'), 'peerId': item.get('peerId')}
            for item in (status.get('unpairedPeers') or [])
        ],
        'selfGhosts': status.get('selfGhosts'),
    }
    # 先落盘再收尾：控制器停掉后状态就没有意义了。
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    time.sleep(max(0.0, args.hold))
    controller.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
