"""Versioned collector results. Unknown or incomplete data stays explicit."""
import math


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} 必须是大于等于 {minimum} 的整数')
    return value


def validate_report(report):
    if not isinstance(report, dict) or report.get('schemaVersion') != 1:
        raise ValueError('不支持的采集结果版本')
    result = {'schemaVersion': 1}
    result['pid'] = integer(report.get('pid'), 'PID', 1)
    result['durationNs'] = integer(report.get('durationNs'), '采集时长', 1)
    width = integer(report.get('bucketWidthNs'), '分桶宽度', 1)
    buckets = report.get('latencyBuckets')
    if not isinstance(buckets, list) or not 2 <= len(buckets) <= 1024:
        raise ValueError('延迟分桶数量无效')
    buckets = [integer(value, '分桶计数') for value in buckets]
    samples = integer(report.get('completedCalls'), '完成调用数')
    if sum(buckets) != samples:
        raise ValueError('完成调用数与分桶计数不一致')
    result.update(bucketWidthNs=width, latencyBuckets=buckets, completedCalls=samples,
                  unmatchedExits=integer(report.get('unmatchedExits'), '未匹配出口数'),
                  droppedEvents=integer(report.get('droppedEvents'), '丢失事件数'))
    stacks = report.get('stacks')
    if not isinstance(stacks, list) or len(stacks) > 4096:
        raise ValueError('调用栈列表无效')
    result['stacks'] = []
    for stack in stacks:
        if not isinstance(stack, dict) or stack.get('kind') not in ('kernel', 'user'):
            raise ValueError('调用栈类型无效')
        frames = stack.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 127:
            raise ValueError('调用栈帧数量无效')
        clean = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise ValueError('调用栈帧无效')
            address, symbol = frame.get('address'), frame.get('symbol')
            if not isinstance(address, str) or not address.startswith('0x') or len(address) > 18:
                raise ValueError('调用栈地址无效')
            try:
                int(address[2:], 16)
            except ValueError:
                raise ValueError('调用栈地址无效') from None
            if symbol is not None and (not isinstance(symbol, str) or not symbol or len(symbol) > 4096):
                raise ValueError('调用栈符号无效')
            clean.append({'address': address, 'symbol': symbol})
        result['stacks'].append({'kind': stack['kind'], 'count': integer(stack.get('count'), '栈样本数', 1),
                                 'frames': clean})
    return result


def quantile_bounds(report, percentile):
    """Return bucket bounds in ns; the final bucket has no finite upper bound."""
    if not isinstance(percentile, (int, float)) or isinstance(percentile, bool) or not math.isfinite(percentile) or not 0 < percentile <= 100:
        raise ValueError('分位数必须在 0–100 范围内')
    if not report['completedCalls']:
        return None
    rank = math.ceil(report['completedCalls'] * percentile / 100)
    count = 0
    for index, value in enumerate(report['latencyBuckets']):
        count += value
        if count >= rank:
            width = report['bucketWidthNs']
            return (index * width, None if index == len(report['latencyBuckets']) - 1 else (index + 1) * width)
