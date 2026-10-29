"""验证跨机中转传输原语：原子写、排序、幂等、过期、增量读与命令策略。

全部在本地临时目录中运行，不依赖第二台机器、共享盘、网络或真实 ADB 设备，
也不启动任何线程或服务。
"""
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay  # noqa: E402


def _check_layout(root: str) -> list:
    queue = relay.RelayQueue(os.path.join(root, 'layout'))
    queue.ensure_layout()
    directories = (
        queue.requests_dir, queue.results_dir, queue.streams_dir,
        queue.peers_dir, queue.archive_dir,
    )
    manifest = relay.read_json(queue.manifest_path) or {}
    return [
        ('布局目录全部创建', all(os.path.isdir(d) for d in directories), str(directories)),
        ('清单文件写入 schema 版本', manifest.get('schema') == relay.RELAY_SCHEMA, str(manifest)),
    ]


def _check_atomic_write(root: str) -> list:
    path = os.path.join(root, 'atomic', 'a.json')
    relay.atomic_write_json(path, {'k': 'v'})
    names = os.listdir(os.path.dirname(path))
    return [
        ('原子写不残留临时文件', names == ['a.json'], str(names)),
        ('原子写内容可读回', relay.read_json(path) == {'k': 'v'}, str(relay.read_json(path))),
        ('读取不存在的文件返回 None', relay.read_json(os.path.join(root, 'atomic', 'nope.json')) is None, ''),
    ]


def _check_provisional_ignored(root: str) -> list:
    queue = relay.RelayQueue(os.path.join(root, 'provisional'))
    queue.ensure_layout()
    request = relay.new_request('exec', 'agent-1', {'key': 'k'}, {'argv': ['version']})
    queue.submit(request)

    tmp_name = 'r-0000000000001-000001-dead.json.tmp-abcd'
    conflict_name = 'r-0000000000001-000001-dead.json (1)'
    for name, body in ((tmp_name, '{半截 JSON'), (conflict_name, '{}')):
        with open(os.path.join(queue.requests_dir, name), 'w', encoding='utf-8') as handle:
            handle.write(body)

    pending = queue.pending_requests()
    return [
        ('临时文件与冲突副本不计入待处理', len(pending) == 1, f'{len(pending)} 条'),
        ('正式请求可正确反序列化',
         bool(pending) and pending[0][1].reqId == request.reqId and pending[0][1].kind == 'exec', ''),
        ('损坏消息不会让枚举抛异常',
         isinstance(relay.list_messages(queue.requests_dir, 'r-'), list), ''),
    ]


def _check_ordering(root: str) -> list:
    queue = relay.RelayQueue(os.path.join(root, 'ordering'))
    queue.ensure_layout()
    stamps = (
        '2026-09-23T14:50:47.001+08:00',
        '2026-09-23T14:50:47.002+08:00',
        '2026-09-23T14:50:47.010+08:00',
    )
    for index, stamp in enumerate(stamps):
        request = relay.Request(
            reqId=f'id{index}', kind='exec', peer='agent-1', device={},
            payload={'i': index}, createdAt=stamp, expiresAt=stamp,
        )
        queue.submit(request)
    order = [item.payload['i'] for _path, item in queue.pending_requests()]
    names = sorted(name for name in os.listdir(queue.requests_dir) if name.startswith('r-') and name.endswith('.json'))
    # 文件名里的时间戳是 epoch 毫秒（定宽 13 位），字典序因此等于时间序。
    widths = {len(name.split('-')[1]) for name in names}
    ids_in_order = [name.rsplit('-', 1)[-1].removesuffix('.json') for name in names]
    return [
        ('待处理请求按时间升序', order == [0, 1, 2], str(order)),
        ('文件名时间戳定宽 13 位，字典序即时间序',
         names == sorted(names) and widths == {13} and ids_in_order == ['id0', 'id1', 'id2'],
         f'{names} / widths={widths}'),
    ]


def _check_expiry(root: str) -> list:
    past = relay.Request(
        'x', 'exec', 'a', {}, {},
        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:30+00:00',
    )
    created = datetime.now(timezone.utc)
    future = relay.Request(
        'y', 'exec', 'a', {}, {},
        created.isoformat(), (created + timedelta(seconds=60)).isoformat(),
    )
    malformed = relay.Request('z', 'exec', 'a', {}, {}, 'bad', 'bad')
    missing = relay.Request('w', 'exec', 'a', {}, {}, created.isoformat(), '')
    return [
        ('过期请求判为过期', past.is_expired() is True, ''),
        ('未过期请求判为有效', future.is_expired() is False, ''),
        ('expiresAt 无法解析时按过期处理', malformed.is_expired() is True, ''),
        ('expiresAt 缺失时按过期处理', missing.is_expired() is True, ''),
    ]


def _check_idempotency(root: str) -> list:
    queue = relay.RelayQueue(os.path.join(root, 'idempotency'))
    queue.ensure_layout()
    request = relay.new_request('exec', 'a', {}, {'argv': ['version']})
    queue.submit(request)
    before = queue.has_result(request.reqId)
    queue.write_result(relay.Result(
        reqId=request.reqId, status=relay.STATUS_OK, exitCode=0, stdout='Android Debug Bridge\n',
    ))
    after = queue.has_result(request.reqId)
    result = queue.read_result(request.reqId)
    return [
        ('未处理请求没有结果文件', before is False, ''),
        ('写入结果后可判定已处理', after is True, ''),
        ('结果可正确读回',
         result is not None and result.ok and result.stdout.startswith('Android Debug Bridge')
         and result.exitCode == 0, ''),
        ('读取不存在的结果返回 None', queue.read_result('nope') is None, ''),
    ]


def _check_stream_tail(root: str) -> list:
    path = os.path.join(root, 'streams', 's1.out')
    writer = relay.StreamWriter(path)
    tail = relay.StreamTail(path, 0)
    checks = [('流尚未创建时读为空', tail.read_new() == b'', '')]

    writer.append(b'hello ')
    writer.append('中文'.encode('utf-8'))
    expected = b'hello ' + '中文'.encode('utf-8')
    first = tail.read_new()
    checks.append(('增量读拿到全部新增字节', first == expected, repr(first)))
    checks.append(('无新增时读为空', tail.read_new() == b'', ''))

    writer.append(b'again')
    checks.append(('第二次增量读只取新字节', tail.read_new() == b'again', ''))

    with open(path, 'wb') as handle:
        handle.write(b'new')
    rebuilt = tail.read_new()
    checks.append(('文件重建后 offset 归零并重读',
                   rebuilt == b'new' and tail.rebuilt is True, repr(rebuilt)))
    return checks


def _check_stream_meta(root: str) -> list:
    channel = relay.StreamChannel(os.path.join(root, 'streams'), 'sess-1')
    channel.write_meta({'deviceKey': 'de47c9b2:tid=2', 'kind': 'terminal', 'state': 'open'})
    meta = channel.read_meta() or {}
    return [
        ('流元数据包含 schema 与 streamId',
         meta.get('schema') == relay.RELAY_SCHEMA and meta.get('streamId') == 'sess-1', str(meta)),
        ('输入与输出是两个独立文件', channel.out_path != channel.in_path, ''),
        ('元数据读取不存在的流返回 None',
         relay.StreamChannel(os.path.join(root, 'streams'), 'missing').read_meta() is None, ''),
    ]


def _check_heartbeat(root: str) -> list:
    peers_dir = os.path.join(root, 'peers')
    beat = relay.PeerHeartbeat(
        peerId='pc-b', role=relay.ROLE_AGENT, appVersion='v1.1', pid=4321,
        startedAt=relay.now_iso(), heartbeatAt=relay.now_iso(),
        devices=[{'key': 'de47c9b2:tid=2', 'serial': 'de47c9b2', 'transportId': 2, 'state': 'device'}],
    )
    relay.write_heartbeat(peers_dir, beat)
    peers = relay.read_peers(peers_dir)
    stale = relay.PeerHeartbeat(
        peerId='pc-c', role=relay.ROLE_AGENT, heartbeatAt='2020-01-01T00:00:00+00:00',
    )
    empty = relay.PeerHeartbeat(peerId='pc-d', role=relay.ROLE_AGENT)
    return [
        ('心跳可写入并读回',
         'pc-b' in peers and peers['pc-b'].devices[0]['transportId'] == 2, str(list(peers))),
        ('新鲜心跳判为在线', peers['pc-b'].is_stale(2.0) is False, ''),
        ('超期心跳判为离线', stale.is_stale(2.0) is True, ''),
        ('缺少 heartbeatAt 时判为离线', empty.is_stale(2.0) is True, ''),
    ]


def _check_command_policy(root: str) -> list:
    allowed, _ = relay.check_command(['version'])
    shell_default, shell_reason = relay.check_command(['shell', 'ls'])
    shell_on, _ = relay.check_command(['shell', 'ls'], allow_shell=True)
    selector, selector_reason = relay.check_command(['-t', '2', 'shell', 'ls'], allow_shell=True)
    unknown, unknown_reason = relay.check_command(['install', 'x.apk'])
    empty, _ = relay.check_command([])
    listed, _ = relay.check_command(['logcat', '-d', '-t', '20'])
    filtered, _ = relay.check_command(['logcat', '-s', 'ActivityManager'])
    prop, prop_reason = relay.check_command(['getprop', 'ro.build.version.sdk'])
    extended, _ = relay.check_command(['reboot'], allowed_subcommands=('reboot',))
    return [
        ('白名单子命令放行', allowed is True, ''),
        ('默认拒绝任意 shell', shell_default is False, shell_reason),
        ('显式开启后允许 shell', shell_on is True, ''),
        ('子命令位置的选择符被拒绝', selector is False, selector_reason),
        ('非白名单子命令被拒绝', unknown is False, unknown_reason),
        ('空命令被拒绝', empty is False, ''),
        ('logcat -t N 不误伤（-t 是 logcat 自己的参数）', listed is True, ''),
        ('logcat -s TAG 不误伤（-s 是 logcat 的静默过滤器）', filtered is True, ''),
        ('getprop 被拒：它不是有效的 adb 子命令（设备属性走 shell getprop）',
         prop is False and '白名单' in prop_reason, prop_reason),
        ('在设备端扩展白名单后 reboot 放行', extended is True, ''),
    ]


def _check_end_to_end(root: str) -> list:
    """用一个临时目录扮演共享盘，跑完一次命令的完整往返。"""
    queue = relay.RelayQueue(os.path.join(root, 'e2e'))
    queue.ensure_layout()
    checks = []

    request = relay.new_request(
        relay.KIND_EXEC, 'pc-b',
        {'key': 'de47c9b2:tid=2', 'serial': 'de47c9b2', 'transportId': 2},
        {'argv': ['logcat', '-d', '-t', '20']},
        ttl_s=30,
    )
    request_path = queue.submit(request)

    pending = queue.pending_requests()
    checks.append(('设备端看到 1 条待处理请求', len(pending) == 1, str(len(pending))))
    if len(pending) != 1:
        return checks

    _path, incoming = pending[0]
    checks.append(('请求内容完整送达',
                   incoming.payload['argv'] == ['logcat', '-d', '-t', '20']
                   and incoming.device.get('transportId') == 2, str(incoming.to_dict())))
    checks.append(('未过期且无结果即允许执行',
                   (not incoming.is_expired()) and (not queue.has_result(incoming.reqId)), ''))
    checks.append(('设备端按白名单放行',
                   relay.check_command(incoming.payload['argv'])[0] is True, ''))

    queue.write_result(relay.Result(
        reqId=incoming.reqId, status=relay.STATUS_OK, exitCode=0, stdout='34\n',
        agentId='pc-b', durationMs=41,
    ))

    relay.atomic_write_json(request_path, request.to_dict())
    checks.append(('重复投递同一 reqId 被去重', queue.has_result(incoming.reqId) is True, ''))

    result = queue.read_result(request.reqId)
    checks.append(('操作端读到正确结果',
                   result is not None and result.stdout == '34\n'
                   and result.exitCode == 0 and result.agentId == 'pc-b', ''))

    queue.archive_file(request_path)
    queue.archive_file(queue.result_path(request.reqId))
    checks.append(('归档后活动目录清空',
                   queue.pending_requests() == [] and queue.read_result(request.reqId) is None, ''))
    months = os.listdir(queue.archive_dir) if os.path.isdir(queue.archive_dir) else []
    checks.append(('归档落到月份目录', len(months) == 1, str(months)))

    if months:
        old = os.path.join(queue.archive_dir, months[0], 'old.json')
        relay.atomic_write_json(old, {'x': 1})
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))
        removed = queue.prune_archive(retention_days=7)
        checks.append(('超期归档被清理且保留未超期文件',
                       removed == 1 and not os.path.exists(old), str(removed)))

    expired = relay.new_request(relay.KIND_EXEC, 'pc-b', {}, {'argv': ['version']})
    queue.submit(expired)
    late = [item for _p, item in queue.pending_requests() if item.reqId == expired.reqId][0]
    checks.append(('TTL 有效期内新请求判为未过期', late.is_expired() is False, ''))
    checks.append(('过期请求不会被执行',
                   relay.Request.from_dict({**expired.to_dict(), 'expiresAt': '2020-01-01T00:00:00+00:00'}).is_expired() is True, ''))
    return checks


PROBES = (
    _check_layout,
    _check_atomic_write,
    _check_provisional_ignored,
    _check_ordering,
    _check_expiry,
    _check_idempotency,
    _check_stream_tail,
    _check_stream_meta,
    _check_heartbeat,
    _check_command_policy,
    _check_end_to_end,
)


def _check_fast_notifications(root):
    from unittest.mock import patch
    channel = os.path.join(root, 'fast-notifications')
    writer = relay.RelayQueue.for_peer(channel, 'slave')
    writer.ensure_layout()
    writer.prepare_fast_results()
    reader = relay.RelayQueue.for_peer(channel, 'slave')
    request = relay.new_request(relay.KIND_EXEC, 'master', payload={'argv': ['shell', 'ls -1F /']})
    path = writer.submit(request)
    with patch.object(relay, 'list_messages', return_value=[]), patch.object(relay, 'read_json', return_value=None):
        found = reader.pending_requests(source_peer_ids=['master'])
        notified = len(found) == 1 and found[0][1].reqId == request.reqId
        writer.write_result(relay.Result(reqId=request.reqId, status=relay.STATUS_OK, stdout='data/'))
        result = reader.read_result(request.reqId)
        replied = result is not None and result.stdout == 'data/'
    writer.archive_file(path)
    writer.archive_file(writer.result_path(request.reqId))
    restarted = relay.RelayQueue.for_peer(channel, 'slave')
    archived_safe = restarted.has_result(request.reqId)
    # 半条追加记录稍后补齐，只交付一次，完整的下一条不会被损坏行卡住。
    next_request = relay.new_request(relay.KIND_EXEC, 'master')
    next_filename = relay.make_req_filename(next_request, 2)
    journal = os.path.join(writer.requests_dir, '.from-master.jsonl')
    payload = relay.json.dumps({'file': next_filename, 'request': next_request.to_dict()}).encode() + b'\n'
    with open(journal, 'ab') as stream:
        stream.write(payload[:len(payload)//2])
    with patch.object(relay, 'list_messages', return_value=[]):
        incomplete = reader.pending_requests(source_peer_ids=['master']) == []
        with open(journal, 'ab') as stream:
            stream.write(payload[len(payload)//2:])
        completed = reader.pending_requests(source_peer_ids=['master'])
        once = reader.pending_requests(source_peer_ids=['master']) == []
    return [('新文件及目录不可见时追加通知仍传递请求和回执', notified and replied, ''),
            ('节点重启后归档回执仍阻止请求重放', archived_safe, ''),
            ('半条追加请求补齐后只交付一次', incomplete and len(completed) == 1 and once, '')]


def _check_journal_rotation(root):
    """追加通知日志有界：超限回卷、读端陈旧 offset 回退后仍读到最新回执。"""
    from unittest.mock import patch
    channel = os.path.join(root, 'journal-rotation')
    writer = relay.RelayQueue.for_peer(channel, 'slave')
    writer.ensure_layout()
    writer.prepare_fast_results()
    journal = os.path.join(writer.results_dir, '.replies.jsonl')
    bounded = True
    with patch.object(relay, 'FAST_JOURNAL_MAX_BYTES', 256):
        for i in range(40):
            writer.write_result(relay.Result(reqId=f'roll-{i}', status=relay.STATUS_OK))
    bounded = os.path.getsize(journal) <= 256 + 4096
    reader = relay.RelayQueue.for_peer(channel, 'slave')
    # 模拟长期运行的读进程：写端回卷后仍持有一个越过新文件末尾的旧 offset。
    reader._fast_offsets[journal] = 10 ** 9
    with patch.object(relay, 'list_messages', return_value=[]), \
            patch.object(relay, 'read_json', return_value=None):
        latest = reader.read_result('roll-39')
    tail_read = latest is not None and latest.status == relay.STATUS_OK
    # 请求日志同一机制：回卷后不破坏目录枚举与追加交付。
    with patch.object(relay, 'FAST_JOURNAL_MAX_BYTES', 256):
        for i in range(30):
            req = relay.new_request(relay.KIND_EXEC, 'master', payload={'argv': ['echo', str(i)]})
            writer.submit(req)
    req_journal = os.path.join(writer.requests_dir, '.from-master.jsonl')
    requests_bounded = os.path.getsize(req_journal) <= 256 + 4096
    req_reader = relay.RelayQueue.for_peer(channel, 'slave')
    req_reader._fast_offsets[req_journal] = 10 ** 9
    with patch.object(relay, 'list_messages', return_value=[]):
        recent = req_reader.pending_requests(source_peer_ids=['master'])
    requests_tail = len(recent) >= 1 and recent[-1][1].payload['argv'][1] == '29'
    return [('追加日志超限回卷后大小有界', bounded, ''),
            ('写端回卷后读端陈旧 offset 回退并读到最新回执', tail_read, ''),
            ('请求日志回卷同样有界', requests_bounded, ''),
            ('请求日志回卷后仍交付尾部请求', requests_tail, '')]


def run() -> bool:
    checks = []
    tmp = tempfile.mkdtemp(prefix='relay-test-')
    try:
        for probe in PROBES:
            checks += probe(tmp)
        checks += _check_fast_notifications(tmp)
        checks += _check_journal_rotation(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    for name, passed, detail in checks:
        suffix = '' if passed else f' — {detail}'
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{suffix}")
        ok = ok and passed
    return ok


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
