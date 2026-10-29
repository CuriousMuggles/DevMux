"""人工清理闲置中转缓存；不调度后台合并，不中断活动链路。"""
from __future__ import annotations

import json
import heapq
import os
import re
import stat
import tempfile
import time
import zipfile
from collections import Counter, deque
from pathlib import Path

import relay
import relay_file_io

SETTLE_S = 30
PROTOCOL_IDLE_S = 600
SCAN_LIMIT = 100000
TIME_LIMIT_S = 45
JSON_LIMIT = 8 * 1024 * 1024
SCAN_PAGE_SIZE = 256
END_STATES = {'ended', 'closed', 'stopped', 'error', 'failed', 'cancelled'}
RESULT_STATES = {'ok', 'error', 'rejected', 'expired', 'failed', 'cancelled'}


class ChangedFile(Exception):
    """枚举后文件发生变化，保留而不是强行删除。"""


def clear_idle_cache(root, protected_paths=(), *, peer_id, pool_id='', active_requests=None, scan_state=None):
    target = Path(relay.validate_cache_root(root, protected_paths))
    peer = relay.check_peer_id(peer_id, '本实例标识')
    result = dict(root=str(target), scope='idle-cache', success=True, removedFiles=0,
                  removedDirs=0, trimmedFiles=0, scanned=0, keptFiles=0,
                  scannedBytes=0, freedBytes=0, keptBytes=0, errorCount=0,
                  errors=[], remaining=[], limited=False, notice='', keptReasons={},
                  completed=False, resumed=False)
    reasons, seen = Counter(), set()
    started = time.monotonic()
    online_owners, beat_snapshots = set(), {}

    def failure(path, error):
        result['success'] = False
        result['errorCount'] += 1
        if len(result['errors']) < 20:
            relative = os.path.relpath(path, target)
            result['errors'].append(dict(path=relative, error=str(error)))
            result['remaining'].append(relative)

    def guard(path):
        current = Path(path)
        while True:
            if relay._cache_link(str(current)):
                raise OSError('清理路径包含链接，已保留')
            if current == target:
                return
            if target not in current.parents:
                raise OSError('清理路径越界')
            current = current.parent

    clock_path = Path(relay.resolve_pool_root(str(target), pool_id)) / 'peers' / (peer + '.json')
    try:
        guard(clock_path)
        reference = relay_file_io.stat(str(clock_path)).st_mtime
    except FileNotFoundError:
        result['notice'] = '缺少自身共享心跳时钟参考，未清理；请启用中转后重试'
        return result
    except OSError as exc:
        failure(clock_path, exc)
        return result

    def old(info, seconds):
        return reference - info.st_mtime >= seconds

    def fresh(path, before):
        guard(path)
        current = relay_file_io.stat(str(path))
        if (current.st_size != before.st_size
                or abs(current.st_mtime - before.st_mtime) > .000001
                or (getattr(current, 'st_ino', 0) and getattr(before, 'st_ino', 0)
                    and current.st_ino != before.st_ino)):
            raise ChangedFile('文件正在变化')

    def busy(req_id):
        return req_id in (active_requests() if active_requests is not None else ())

    def read_json(path, limit=JSON_LIMIT):
        guard(path)
        with open(path, 'rb') as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError('元信息过大，无法确认闲置')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('元信息不是对象')
        return value

    def keep(path, info, reason):
        if str(path) in seen:
            return
        seen.add(str(path))
        result['scanned'] += 1
        result['scannedBytes'] += info.st_size
        result['keptFiles'] += 1
        result['keptBytes'] += info.st_size
        reasons[reason] += 1

    def removed(path, info, replacement_size=None):
        if str(path) in seen:
            return
        seen.add(str(path))
        result['scanned'] += 1
        result['scannedBytes'] += info.st_size
        if replacement_size is None:
            result['removedFiles'] += 1
            result['freedBytes'] += info.st_size
        else:
            result['trimmedFiles'] += 1
            result['freedBytes'] += max(0, info.st_size - replacement_size)
            result['keptBytes'] += replacement_size

    def delete(path, info):
        fresh(path, info)
        os.unlink(path)
        removed(path, info)

    def replace(path, info, write, allowed=None):
        temporary = None
        try:
            if allowed is not None and not allowed():
                keep(path, info, '在途请求')
                return
            fresh(path, info)
            fd, name = tempfile.mkstemp(prefix='.cache-clear-', suffix='.partial', dir=path.parent)
            os.close(fd)
            temporary = Path(name)
            write(temporary)
            size = temporary.stat().st_size
            if size >= info.st_size:
                keep(path, info, '已是最小记录')
                return
            fresh(path, info)
            if allowed is not None and not allowed():
                keep(path, info, '在途请求')
                return
            os.replace(temporary, path)
            temporary = None
            removed(path, info, size)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def owner_idle(deployment, owner):
        """新鲜stat复核主心跳和所有pulse；任何较新写入都保留。"""
        directory = deployment / 'peers'
        owner_key = (str(deployment), owner)
        if owner_key in online_owners:
            return False  # 仅缓存保守的在线结论；失联结论不能作为后续删除凭据复用。
        try:
            guard(directory)
            with os.scandir(directory) as entries:
                clocks = [Path(entry.path) for entry in entries
                          if entry.name == owner + '.json' or entry.name.startswith(owner + '.pulse-')]
            for clock in clocks:
                guard(clock)
                if not old(relay_file_io.stat(str(clock)), PROTOCOL_IDLE_S):
                    online_owners.add(owner_key)
                    return False
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def stream_group(path, info, deployment, owner, archive=False):
        suffix = next((s for s in ('.meta.json', '.out', '.in') if path.name.endswith(s)), None)
        if suffix is None:
            return False
        identity = path.name[:-len(suffix)]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', identity):
            return False
        metadata = path.parent / (identity + '.meta.json')
        meta_info = None
        try:
            meta_info = os.lstat(metadata)
            meta = read_json(metadata, 65536)
            fresh(metadata, meta_info)
        except FileNotFoundError:
            meta = {}
        except (ValueError, OSError):
            keep(path, info, '会话元信息无法确认')
            return True
        if meta.get('streamId', identity) != identity:
            keep(path, info, '会话元信息身份不一致')
            return True
        ended = meta.get('status', meta.get('state')) in END_STATES or meta.get('closed') is True
        if (not identity.startswith(('sess-', 'log-', 'win-', 'serial-'))
                and meta.get('streamId') != identity):
            keep(path, info, '未知文件')
            return True
        if not ended and not owner_idle(deployment, owner):
            keep(path, info, '活动或状态未知的会话')
            return True
        if archive and (deployment / 'streams' / owner / (identity + '.meta.json')).exists():
            keep(path, info, '会话仍在活动流目录')
            return True
        group = []
        for extension in ('.out', '.in', '.meta.json'):
            member = path.parent / (identity + extension)
            try:
                guard(member)
                item = os.lstat(member)
                if not stat.S_ISREG(item.st_mode) or not old(item, SETTLE_S if ended else PROTOCOL_IDLE_S):
                    keep(path, info, '近期变化的会话')
                    return True
                group.append((member, item))
            except FileNotFoundError:
                continue
        for member, item in group:
            fresh(member, item)
        if not ended and not owner_idle(deployment, owner):
            keep(path, info, '所属实例已恢复写入')
            return True
        # 先删数据，最后删元信息；每项前复核结束状态对应的原元信息没有改变。
        for member, item in group:
            if str(member) not in seen:
                if meta_info is not None:
                    fresh(metadata, meta_info)
                delete(member, item)
        return True

    def handle(path, info, deployment, slot, relative):
        # 只保留的项复用枚举属性；候选正文/变更仍重新取得文件身份并检查完整路径。
        if not stat.S_ISREG(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 1024):
            keep(path, info, '链接或特殊文件')
            return
        if not old(info, SETTLE_S):
            keep(path, info, '近期写入')
            return
        if (slot in {'results', 'archive'} and info.st_size < 4096
                and re.fullmatch(r'[A-Za-z0-9_-]{1,100}\.json', path.name)
                and not path.name.startswith('r-')):
            keep(path, info, '小型去重记录')
            return
        candidate = (slot == 'streams' or (slot == 'archive' and (path.suffix in {'.zip', '.partial', '.json', '.out', '.in'}
                     or '.zip.' in path.name)) or slot in {'requests', 'results', 'peers'})
        if not candidate:
            keep(path, info, '活动日志、身份/索引或未知文件')
            return
        guard(path)
        info = os.lstat(path)  # Windows DirEntry文件ID可能为0；正文读取前必须取真实ID。
        if not stat.S_ISREG(info.st_mode) or not old(info, SETTLE_S):
            keep(path, info, '近期写入或特殊文件')
            return
        owner = relative[0] if len(relative) > 1 else ''
        if slot == 'streams' and owner and stream_group(path, info, deployment, owner):
            return
        if slot == 'archive' and owner and len(relative) == 5 and relative[1] == 'transfers':
            source, transfer_id = relative[2:4]
            if (relay.valid_peer_id(source) and re.fullmatch(r'[0-9a-f]{32}', transfer_id)
                    and path.name in {'upload.zip', 'download.zip', 'upload.partial', 'download.partial'}):
                if (old(info, PROTOCOL_IDLE_S) and owner_idle(deployment, owner)
                        and owner_idle(deployment, source)):
                    delete(path, info)
                else:
                    keep(path, info, '传输端点在线或包仍在近期使用')
                return
        if slot == 'archive' and owner:
            if (re.fullmatch(r'sess-[A-Za-z0-9-]{1,100}\.zip(?:\.[0-9a-f]{6})?\.partial', path.name)
                    and old(info, PROTOCOL_IDLE_S) and owner_idle(deployment, owner)):
                delete(path, info)
                return
            session = re.fullmatch(r'(sess-[A-Za-z0-9-]{1,100})\.zip(?:\.[0-9a-f]{6})?', path.name)
            if session:
                if (deployment / 'streams' / owner / (session[1] + '.meta.json')).exists():
                    keep(path, info, '会话仍在活动流目录')
                    return
                with zipfile.ZipFile(path) as package:
                    item = package.getinfo(session[1] + '.meta.json')
                    if item.file_size > 65536:
                        raise ValueError('会话元信息过大')
                    meta = json.loads(package.read(item))
                if isinstance(meta, dict) and meta.get('streamId', session[1]) == session[1]:
                    if meta.get('status') in END_STATES or (old(info, PROTOCOL_IDLE_S) and owner_idle(deployment, owner)):
                        delete(path, info)
                        return
                keep(path, info, '活动或状态未知的会话')
                return
            if stream_group(path, info, deployment, owner, archive=True):
                return
            if re.fullmatch(r'compact-[0-9a-f]{32}\.zip', path.name) and old(info, PROTOCOL_IDLE_S):
                with zipfile.ZipFile(path) as package:
                    index_info = package.getinfo('__index__.json')
                    if index_info.file_size > 32 * 1024 * 1024:
                        raise ValueError('索引过大')
                    raw = package.read(index_info)
                    only_index = package.namelist() == ['__index__.json']
                if only_index:
                    keep(path, info, '已是最小索引')
                    return
                manifest = json.loads(raw)
                if (not isinstance(manifest, dict) or manifest.get('schema') != 1
                        or not isinstance(manifest.get('records'), list) or len(manifest['records']) > 10000
                        or any(not isinstance(record, dict) or not isinstance(record.get('reqId'), str)
                               for record in manifest['records'])):
                    raise ValueError('归档索引非法')
                def index_only(temporary):
                    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                        package.writestr('__index__.json', raw)
                replace(path, info, index_only)
                return
        if slot in {'requests', 'results', 'archive'} and path.name.endswith('.json') and old(info, PROTOCOL_IDLE_S):
            data = read_json(path)
            req_id = data.get('reqId')
            if not isinstance(req_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', req_id):
                keep(path, info, '非中转协议文件')
                return
            if busy(req_id):
                keep(path, info, '在途请求')
                return
            if data.get('kind') and path.name.startswith('r-') and slot in {'requests', 'archive'}:
                ttl = (relay._epoch_ms(data.get('expiresAt')) - relay._epoch_ms(data.get('createdAt'))) / 1000
                if ttl > 0 and relay._epoch_ms(data.get('createdAt')) > 0 and old(info, ttl + PROTOCOL_IDLE_S):
                    if not busy(req_id):
                        delete(path, info)
                        return
                keep(path, info, '未确认过期的请求')
                return
            if (data.get('status') in RESULT_STATES and slot in {'results', 'archive'}
                    and re.fullmatch(re.escape(req_id) + r'\.json(?:\.[0-9a-f]{6})?', path.name)):
                # 正文可清，reqId/状态仍是重启去重依据，不能直接删记录。
                slim = {**data, 'stdout': '', 'stderr': '', 'extra': {'cacheCleared': True}}
                encoded = json.dumps(slim, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                if info.st_size - len(encoded) < 4096:
                    keep(path, info, '小型去重记录')
                    return
                if not busy(req_id):
                    replace(path, info, lambda temporary: temporary.write_bytes(encoded), allowed=lambda: not busy(req_id))
                    return
        if slot == 'peers' and old(info, PROTOCOL_IDLE_S):
            if re.fullmatch(r'.+\.pulse-\d+-[0-9a-f]{12}', path.name):
                delete(path, info)
                return
            match = re.fullmatch(r'(.+)\.beat-\d+-[0-9a-f]+\.beat', path.name)
            if match:
                key = (str(path.parent), match[1])
                if key not in beat_snapshots:
                    with os.scandir(path.parent) as entries:
                        siblings = sorted([(entry.stat(follow_symlinks=False).st_mtime, entry.name)
                                           for entry in entries if re.fullmatch(re.escape(match[1]) + r'\.beat-\d+-[0-9a-f]+\.beat', entry.name)], reverse=True)
                    beat_snapshots[key] = {name for _, name in siblings[:3]}
                if path.name not in beat_snapshots[key]:
                    delete(path, info)
                    return
        keep(path, info, '活动日志、身份/索引或未知文件')

    def exhausted():
        if result['scanned'] >= SCAN_LIMIT or time.monotonic() - started >= TIME_LIMIT_S:
            result['limited'] = True
            return True
        return False

    # 队列只在本机内存保存；每层最多一页，目录枚举句柄不跨点击保留。
    state = scan_state if scan_state is not None else {}
    identity = (str(target), peer, pool_id)
    if state.get('identity') != identity:
        state.clear()
    pending = state.get('pending')
    result['resumed'] = bool(pending)
    if pending is None:
        state['identity'] = identity
        pending = deque()
        deployments = [target]
        pool = target / 'pool'
        if pool.exists():
            guard(pool)
            with os.scandir(pool) as entries:
                deployments.extend(Path(entry.path) for entry in entries
                    if relay.valid_peer_id(entry.name) and entry.is_dir(follow_symlinks=False)
                    and not relay._cache_link(entry.path))
        for deployment in deployments:
            for slot in ('archive', 'streams', 'requests', 'results', 'peers'):
                pending.append(('dir', deployment / slot, deployment, slot, (), ''))
            pending.append(('dir', deployment, deployment, 'unknown', (), ''))
        state['pending'] = pending

    while pending and not exhausted():
        kind, path, deployment, slot, relative, extra = pending.popleft()
        info = None
        try:
            if kind == 'dir':
                if len(relative) > 8:
                    result['limited'] = True
                    continue
                guard(path)
                with os.scandir(path) as entries:
                    def candidates():
                        for entry in entries:
                            if entry.name <= extra:
                                continue
                            if slot == 'unknown' and not relative and (entry.name in
                                    {'archive', 'streams', 'requests', 'results', 'peers'}
                                    or (deployment == target and entry.name == 'pool')):
                                continue
                            yield entry
                    page = heapq.nsmallest(SCAN_PAGE_SIZE + 1, candidates(), key=lambda entry: entry.name)
                has_more = len(page) > SCAN_PAGE_SIZE
                page = page[:SCAN_PAGE_SIZE]
                if has_more:
                    pending.appendleft(('dir', path, deployment, slot, relative, page[-1].name))
                elif relative and slot != 'unknown':
                    pending.appendleft(('finish', path, deployment, slot, relative, None))
                for entry in reversed(page):
                    pending.appendleft(('entry', Path(entry.path), deployment, slot,
                                        relative + (entry.name,), entry))
            elif kind == 'finish':
                guard(path)
                with os.scandir(path) as entries:
                    empty = next(entries, None) is None
                if empty and old(os.lstat(path), PROTOCOL_IDLE_S) and owner_idle(deployment, relative[0]):
                    guard(path)
                    try:
                        os.rmdir(path)
                        result['removedDirs'] += 1
                    except OSError:
                        pass  # 并发创建不强删；也不影响后续目录续扫。
            elif str(path) not in seen:
                info = extra.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 1024):
                    keep(path, info, '链接或特殊文件')
                elif stat.S_ISDIR(info.st_mode):
                    pending.appendleft(('dir', path, deployment, slot, relative, ''))
                else:
                    handle(path, info, deployment, slot, relative)
        except ChangedFile:
            keep(path, info, '清理前文件发生变化')
        except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
            if kind == 'entry':
                keep(path, info, '元信息无法证明闲置')
        except FileNotFoundError:
            continue
        except OSError as exc:
            failure(path, exc)
            if kind == 'entry' and info is not None:
                keep(path, info, '占用或权限失败')
    result['completed'] = not pending
    result['limited'] = result['limited'] or bool(pending)
    if not pending:
        state.clear()
    result['keptReasons'] = dict(reasons)
    result['notice'] = '体积按本次已扫描文件的逻辑大小统计，可能与磁盘占用不同。'
    if result['limited']:
        result['notice'] += '本批仅部分完成，仍有未扫描文件；再次确认清理将继续扫描。'
    return result
