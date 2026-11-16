"""版本号解析与比较；主程序与插件升级共用同一套判定规则。

只做「谁更新」这一件事，不涉及渠道、灰度或强制升级策略。版本号来自手工维护的
清单与 tag，格式不受控，所以解析必须宽容（v 前缀、段数不足、预发布后缀都收），
比较必须严格（预发布永远低于同号正式版）。解析失败一律抛 ValueError，由调用方
决定是「拒绝升级」还是「按未知处理」，不在这里猜。
"""
import re

# 段数允许 1~4 段：插件版本是 1.0.0，主程序文件版本是 3.1.7.0，两边都要能用。
_PATTERN = re.compile(
    r'^v?(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?'
    r'(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?'
    r'(?:\+[0-9A-Za-z.-]+)?$')


def _prerelease_key(text):
    """把 beta.2 之类的后缀拆成可比较的段；数字段按数值比，其余按字符串比。"""
    parts = []
    for piece in re.split(r'[.-]', text):
        parts.append((0, int(piece), '') if piece.isdigit() else (1, 0, piece))
    return tuple(parts)


def parse(value):
    """解析为可比较的键；格式不符抛 ValueError。"""
    match = _PATTERN.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError('无法解析版本号: ' + repr(value))
    numbers = tuple(int(part or 0) for part in match.groups()[:4])
    prerelease = match.group(5)
    # 第二位区分正式版与预发布：正式版恒高于同号预发布，此时第三位的具体内容无关。
    return (numbers, 1, ()) if prerelease is None else (numbers, 0, _prerelease_key(prerelease))


def compare(left, right):
    """返回 -1 / 0 / 1；任一侧无法解析时抛 ValueError。"""
    first, second = parse(left), parse(right)
    return (first > second) - (first < second)


def is_newer(candidate, current):
    """candidate 是否比 current 新。相等或更旧都返回 False。"""
    return compare(candidate, current) > 0


def satisfies_min(app_version, minimum):
    """当前主程序是否满足插件声明的最低版本；未声明即不限制。"""
    if not minimum:
        return True
    return compare(app_version, str(minimum)) >= 0
