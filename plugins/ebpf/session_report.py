"""Validate correlated syscall samples; never join independent captures."""
import re


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(name+' 无效')
    return value


def correlated_report(report):
    if not isinstance(report,dict) or report.get('schemaVersion')!=1:
        raise ValueError('联合采集协议版本无效')
    session=report.get('sessionId')
    if not isinstance(session,str) or not re.fullmatch('[a-f0-9]{32}',session):
        raise ValueError('会话标识无效')
    pid=integer(report.get('pid'),'PID',1)
    start=integer(report.get('startNs'),'采集起点')
    end=integer(report.get('endNs'),'采集终点',start+1)
    threshold=integer(report.get('slowThresholdNs'),'慢调用阈值',1)
    samples=report.get('slowCalls')
    if not isinstance(samples,list) or len(samples)>4096:
        raise ValueError('慢调用列表无效')
    seen=set();calls=[]
    for sample in samples:
        if not isinstance(sample,dict) or sample.get('sessionId')!=session or sample.get('pid')!=pid:
            raise ValueError('拒绝跨会话或跨进程关联')
        tid=integer(sample.get('tid'),'TID',1)
        syscall=integer(sample.get('syscallId'),'系统调用号')
        enter=integer(sample.get('enterNs'),'入口时间',start)
        exit_time=integer(sample.get('exitNs'),'出口时间',enter)
        if exit_time>end or exit_time-enter<threshold:
            raise ValueError('事件不在采集范围内或不满足慢调用阈值')
        key=(tid,enter)
        if key in seen:raise ValueError('重复事件')
        seen.add(key)
        stacks={}
        for kind in ('kernel','user'):
            stack=sample.get(kind+'Stack')
            # Missing stacks remain explicit; an empty/failed stack cannot be
            # turned into a zero-cost call or a fabricated function chain.
            if stack is None:
                stacks[kind+'Stack']=None;continue
            if not isinstance(stack,list) or not 1<=len(stack)<=127:
                raise ValueError('调用栈无效')
            if any(not isinstance(a,str) or not re.fullmatch('0x[0-9a-fA-F]{1,16}',a) for a in stack):
                raise ValueError('调用栈地址无效')
            stacks[kind+'Stack']=list(stack)
        calls.append(dict(tid=tid,syscallId=syscall,enterNs=enter,exitNs=exit_time,
                          durationNs=exit_time-enter,**stacks))
    quality=report.get('quality')
    if not isinstance(quality,dict):raise ValueError('缺少采集质量')
    quality={name:integer(quality.get(name),name) for name in
             ('completedCalls','unmatchedExits','mapUpdateFailures','stackFailures','droppedSlowCalls')}
    if len(calls)>quality['completedCalls']:raise ValueError('慢调用数大于完成调用数')
    probes=report.get('kernelProbeRanges',[])
    if not isinstance(probes,list) or len(probes)>2:raise ValueError('探针代码范围无效')
    ranges=[]
    for probe in probes:
        if not isinstance(probe,dict) or probe.get('name') not in ('devmux_sys_enter','devmux_sys_exit'):
            raise ValueError('探针标识无效')
        address=probe.get('start');size=integer(probe.get('size'),'探针代码大小',1)
        if not isinstance(address,str) or not re.fullmatch('0x[0-9a-fA-F]{1,16}',address) or size>1048576 or int(address,16)+size>2**64:
            raise ValueError('探针代码范围无效')
        start_address=int(address,16)
        if start_address<2**63 or any(probe['name']==p['name'] or
            start_address<int(p['start'],16)+p['size'] and int(p['start'],16)<start_address+size for p in ranges):
            raise ValueError('探针代码范围冲突或不在内核地址空间')
        ranges.append(dict(name=probe['name'],start=address,size=size))
    return dict(schemaVersion=1,sessionId=session,pid=pid,startNs=start,endNs=end,
                slowThresholdNs=threshold,slowCalls=sorted(calls,key=lambda c:c['durationNs'],reverse=True),
                quality=quality,kernelProbeRanges=ranges)
