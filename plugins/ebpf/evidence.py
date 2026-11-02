"""Evidence-only summaries and bounded kernel symbol lookup."""
from bisect import bisect_right
import re


def analyze_latency(result):
    counts = result.get('buckets')
    width = result.get('bucketWidthNs')
    samples = result.get('samples')
    if (not isinstance(counts, list) or not 2 <= len(counts) <= 1024 or
            any(type(n) is not int or n < 0 for n in counts) or
            type(width) is not int or width <= 0 or
            type(samples) is not int or samples != sum(counts)):
        raise ValueError('延迟计数或分桶无效')
    threshold = (len(counts)-1)*width
    report = {
        'schemaVersion': 1,
        'basis': '当前进程已完成系统调用的延迟分桶',
        'facts': [{'metric': 'completedCalls', 'value': samples},
                  {'metric': 'overflowCalls', 'value': counts[-1], 'lowerBoundNs': threshold}],
        'overflowRatio': counts[-1]/samples if samples else None,
        'quality': {'status': 'partial', 'lostEvents': None, 'unmatchedExits': None,
                    'stackFailures': None},
        'limitations': ['当前协议未报告丢失与未匹配事件，不能证明计数完整',
                        '最后一桶只有耗时下界，不能推算精确最大值',
                        '耗时包含阻塞、调度等待和探针开销，不能据此判定根因'],
        'recommendations': ([] if not samples else
            ['在同一会话中关联慢调用的 syscall 和调用栈后再定位原因'])}
    if result.get('session'):
        from session_report import correlated_report
        session=correlated_report({**result['session'],'slowCalls':[
            {**s,'sessionId':result['session']['sessionId'],'pid':result['session']['pid']}
            for s in result['session']['slowCalls']]})
        report['quality'].update(session['quality'])
        report['limitations'][0]='仅保留最近一条慢调用；其余满足阈值的调用计入 droppedSlowCalls，不能据此推断慢调用排名'
        report['recommendations']=['结合已关联慢调用的 syscall 与栈检查阻塞点；入口栈不能单独证明根因']
    return report


def resolve_kernel(addresses, text):
    if not isinstance(text, str) or len(text) > 32*1024*1024:
        raise ValueError('符号表过大或无效')
    if not isinstance(addresses, list) or len(addresses) > 127:
        raise ValueError('地址列表无效')
    symbols = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and re.fullmatch('[0-9a-fA-F]{16}', fields[0]):
            value = int(fields[0], 16)
            if value:
                symbols.setdefault(value, fields[2])
    ordered = sorted(symbols)
    output = []
    for address in addresses:
        if not isinstance(address, str) or not re.fullmatch('0x[0-9a-fA-F]{1,16}', address):
            raise ValueError('栈地址无效')
        value = int(address,16); index = bisect_right(ordered,value)-1
        # Never extend the last symbol to arbitrary addresses: there is no
        # next-symbol boundary to support the attribution.
        matched = 0 <= index < len(ordered)-1 and value < ordered[index+1]
        output.append({'address': address, 'symbol': symbols[ordered[index]] if matched else None,
                       'offset': value-ordered[index] if matched else None,
                       'status': 'resolved' if matched else 'unresolved'})
    return output
