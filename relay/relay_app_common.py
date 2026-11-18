"""RelayController 与各 mixin 共享的常量与纯函数（B7 拆分，见 docs/internal/流程/优化建议清单.md）。

为什么单独成模块：mixin 不能反向 import ``relay_app``（那会在导入期成环），而
这些常量原本住在 ``relay_app`` 里。下沉到这里之后，方向是
``relay_app_common ← relay_app_* ← relay_app``，依赖仍然单向。

**这里是唯一生效的调整点**：各 mixin 按模块属性读取（``relay_app_common.SESSION_*``），
``relay_app`` 里的同名符号只是兼容再导出 —— 给 ``relay_app.SESSION_*`` 赋值不会
影响已经拆出去的方法。改退避/重试策略请改本模块。
"""
from __future__ import annotations

import os
import re
import time

import relay
from . import relay_node

APP_VERSION = relay_node.APP_VERSION
EVENT_LIMIT = 200
STATUS_POLL_INTERVAL_S = 1.0
SESSION_POLL_INTERVAL_S = 0.5
SESSION_REOPEN_BACKOFF_S = 5.0
SESSION_MAX_FAILURES = 5
# 超过快速重试上限后转慢速重试，而不是永久放弃。
#
# 设备或 PTY 暂不可用后可能自行恢复。若不设慢速重试，这一轮失败就
# 把该设备**永久**钉成「没有终端」，用户只能重启应用才能恢复。
SESSION_RETRY_SLOW_S = 60.0
# 仅给旧版本从机的拒绝原因提供升级提示，不影响准入判定。
# 仅用于把提示写得更可执行，不参与任何控制流判断。
PEER_UNCONFIRMED_MARKERS = ('未确认',)
SESSION_ID_PREFIX = 'sess'
# 操作端不允许自带设备选择符：设备必须由从机依据 device 字段自行拼装。
DEVICE_SELECTOR_TOKENS = frozenset({'-s', '--serial', '-t', '--transport-id'})

PEER_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')
DEFAULT_ALLOWED_SUBCOMMANDS = list(relay.DEFAULT_ALLOWED_SUBCOMMANDS)


def default_peer_id() -> str:
    """默认标识：主机名清洗成安全字符。"""
    raw = os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'pc'
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '-', str(raw)).strip('-.')
    return (cleaned or 'pc')[:32]


def default_session_id() -> str:
    return f'{SESSION_ID_PREFIX}-{int(time.time() * 1000)}-{os.urandom(3).hex()}'


def strip_device_selector(argv) -> list:
    """剥掉操作端自带的设备选择符。

    主控界面里的每条命令都带着 ``-s <serial>`` 或 ``-t <id>``；这两个值在从机
    上是**没有意义甚至错误**的（同一 serial 在从机上可能是另一台设备，transport
    id 更是每次连接都会变）。所以跨机执行时一律剥掉，由从机按 device 字段自选。

    **只剥开头连续的选择符对**，不碰子命令之后的同名参数：``logcat -t 20`` 里的
    ``-t`` 是「取最近 20 行」，``logcat -s TAG`` 是静默过滤器 —— 全表扫描式地删
    会静默改掉命令语义，比拒绝更难发现。设备选择符只有紧跟在 adb 之后才有全局
    含义，位置天然就在开头。
    """
    tokens = [str(part) for part in (argv or [])]
    index = 0
    while index < len(tokens):
        if tokens[index] in DEVICE_SELECTOR_TOKENS:
            index += 2  # 选择符 + 它的值
            continue
        break
    return tokens[index:]


def extract_argv(payload: dict) -> list:
    """从 ``exec-adb`` 的载荷里取出 argv（字符串形式走 shlex 拆分）。"""
    argv = payload.get('argv')
    if isinstance(argv, list):
        return [str(part) for part in argv]
    args = payload.get('args') or ''
    if not args:
        return []
    import shlex  # noqa: PLC0415 - 仅此处需要
    try:
        return shlex.split(str(args))
    except ValueError:
        return str(args).split()
