"""命令策略：允许的子命令白名单。

由 relay.py 按既有分段拆出；relay.py 只做再导出。
"""

from __future__ import annotations

from typing import Optional


# ----------------------------------------------------------------------
# 命令策略
# ----------------------------------------------------------------------
# 保守的默认白名单：只放行**真实存在**的查询类与日志类子命令。需要 install、
# push 等变更类命令时，在 relay.json 里显式扩展，而不是放宽这里的默认值。
#
# 注意 ``getprop`` 不在这里：它不是 adb 的子命令（实测 adb 会回
# ``unknown command getprop``），设备属性要走 ``shell getprop <key>``。
# 把不存在的子命令留在白名单里，等于给调用方一个假的「已放行」信号。
DEFAULT_ALLOWED_SUBCOMMANDS: tuple[str, ...] = (
    'devices',
    'version',
    'get-state',
    'get-serialno',
    'logcat',
    'shell',
)

# 设备由设备端依据请求里的 device 字段自行拼装，不允许操作端指定目标设备。
# 只有**子命令位置**的选择符才算越权：设备端拼命令时形如
# ``adb <selector> <argv...>``，能被 adb 当全局选项解析的只有紧跟在 adb 后面的
# 那个 token，也就是 argv[0]。排在子命令之后的 ``-t`` / ``-s`` 是子命令自己的
# 合法参数（``logcat -t 20`` 取最近 20 行、``logcat -s TAG`` 静默过滤器），
# 一律拒绝会把正常用法一起挡掉。
_DEVICE_SELECTOR_TOKENS = frozenset({'-s', '--serial', '-t', '--transport-id'})


def check_command(
    argv,
    *,
    allow_shell: bool = False,
    allowed_subcommands: Optional[tuple[str, ...]] = None,
) -> tuple[bool, str]:
    """校验一条待透传的 adb 命令，返回 ``(是否放行, 原因)``。

    这个函数应在**设备端**调用：安全边界必须落在被控端，操作端的配置再宽松
    也不能覆盖设备端的拒绝。无论是否允许任意 shell，以下一律拒绝：

    - 空命令；
    - 子命令位置上出现设备选择符（``-s`` / ``--serial`` / ``-t`` / ``--transport-id``）；
    - 非白名单子命令；
    - 未开启开关时的 ``shell``。
    """
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)) or not argv:
        return False, '命令为空'
    tokens = [str(part) for part in argv]
    subcommand = tokens[0]
    if not subcommand:
        return False, '命令为空'

    # 先判选择符：命中时给的原因比「不在白名单内」更能说明问题。
    if subcommand in _DEVICE_SELECTOR_TOKENS:
        return False, (f'请求不得在子命令位置指定设备选择符（{subcommand}）：'
                       f'设备由设备端依据 device 字段决定')

    allowed = DEFAULT_ALLOWED_SUBCOMMANDS if allowed_subcommands is None else tuple(allowed_subcommands)
    if subcommand not in allowed:
        return False, f'子命令不在白名单内：{subcommand}'

    if subcommand == 'shell' and not allow_shell:
        return False, '设备端未开启「允许任意 shell」'

    return True, 'ok'
