"""RelayNode 的运行时策略常量。

与 adb 执行相关的（超时、输出上限、退出码标记）放在 :mod:`relay_adb`；这里只留
节点自身的策略：心跳节奏、在线判定的宽限、设备去抖阈值、流读取块大小。

常量放成独立模块，是为了让四个 mixin 都能读到同一份值 —— 它们不能 import 门面
``relay_node``（那会成环），也不该各自复制一份字面量。
"""
from __future__ import annotations

import re
import time


# 连接验证节奏：验证成功后按 OK 间隔复查，失败/未验证时按重试间隔密集探测。
# 原实现无论成败都按 5s 复查，在线从机会以 ~6s 一次的频率持续产生
# check-connection 请求与回执文件（实测约 4000 文件/天），是共享目录小文件的
# 最大来源；验证成功后退避到 30s 可将该写放大降低约 10 倍，从机重启仍会在
# 一个 OK 间隔内被新代次请求拒绝（fail-closed），安全性不变。
CONNECTION_VERIFY_OK_INTERVAL_S = 30.0


CONNECTION_VERIFY_RETRY_INTERVAL_S = 5.0


APP_VERSION = '1.2'


APP_STARTED_MONOTONIC = time.monotonic()


STREAM_READ_LIMIT = 16 * 1024


# 从机离线后，其远端设备在设备栏里**保留多久**（秒）。
#
# 设计上从机离线不等于设备消失：本地设备离线也只是灰显，远端设备必须同构，
# 否则「操作无区别」在离线这一瞬就破了（设计文档 §5.4 / §7.3）。
# 但心跳文件会永远留在共享目录里，所以超过这个时长就当作对端已退场，不再列出，
# 避免换机器、改 peerId 之后留下永久幽灵设备。
REMOTE_DEVICE_LINGER_S = 300.0


# 设备枚举去抖：设备必须连续这么多次枚举都没出现，才算真的离线被移除。
# 与 adb_worker.py 的离线去抖同源 —— 「枚举漏了一次」和「设备真的拔了」必须分开。
DEVICE_MISS_THRESHOLD = 3


STREAM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
