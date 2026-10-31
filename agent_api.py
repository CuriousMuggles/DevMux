"""Agent 和 UI 共用的工具入口，复用已有 ADB 会话和文件传输。"""
import asyncio
import os
import threading
import time
import uuid

import relay_node
from windows_executor import OutputBuffer


def tool(name, description, properties=None, required=()):
    return dict(name=name, description=description, inputSchema=dict(type='object',
        properties=properties or {}, required=list(required), additionalProperties=False))

STRING = {'type': 'string'}
INTEGER = {'type': 'integer', 'minimum': 0}
TARGET = {'target': STRING}
SESSION = {'sessionId': STRING}
JOB = {**TARGET, 'jobId': STRING}
TOOLS = [
    tool('targets_list', '列出当前实例的电脑和设备；使用完整目标 ID'),
    tool('target_status', '查询电脑 PowerShell 能力与权限', TARGET, ['target']),
    tool('windows_exec', '启动独立 PowerShell 任务；不随 Agent 退出停止，用 job_status 查询',
         {**TARGET, 'script': STRING, 'cwd': STRING, 'shell': {'type': 'string', 'enum': ['powershell', 'pwsh']},
          'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400}, 'jobId': STRING}, ['target', 'script']),
    tool('job_status', '查询任务状态；连接超时不能当作任务失败', JOB, ['target', 'jobId']),
    tool('job_output', '按字节游标读取输出，使用 nextCursor 继续',
         {**JOB, 'stream': {'type': 'string', 'enum': ['stdout', 'stderr']}, 'cursor': INTEGER,
          'limit': {'type': 'integer', 'minimum': 4, 'maximum': 65536}}, ['target', 'jobId']),
    tool('job_cancel', '取消普通任务及其子进程；固化等操作须有专用取消策略', JOB, ['target', 'jobId']),
    tool('jobs_list', '列出当前调用实例的电脑任务', TARGET, ['target']),
    tool('adb_exec', '执行指定设备的 ADB argv；不要包含设备选择符',
         {**TARGET, 'argv': {'type': 'array', 'items': STRING, 'minItems': 1},
          'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 300}}, ['target', 'argv']),
    tool('files_transfer', 'push/pull 文件；电脑远端路径限 target_status.filesRoot 内的相对路径；本机路径在主控电脑',
         {**TARGET, 'direction': {'type': 'string', 'enum': ['push', 'pull']},
          'paths': {'type': 'array', 'items': STRING, 'minItems': 1}, 'destination': STRING},
         ['target', 'direction', 'paths', 'destination']),
    tool('session_open', '打开电脑 PowerShell 或接入已有设备 shell；设备输入独占，关闭仅释放接入',
         {**TARGET, 'cwd': STRING, 'shell': {'type': 'string', 'enum': ['powershell', 'pwsh']},
          'cols': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
          'rows': {'type': 'integer', 'minimum': 1, 'maximum': 1000}}, ['target']),
    tool('session_read', '读取会话合并输出；PTY 无通用命令退出码',
         {**SESSION, 'cursor': INTEGER, 'limit': {'type': 'integer', 'minimum': 4, 'maximum': 65536}}, ['sessionId']),
    tool('session_write', '输入按原样发送；回车使用换行，Ctrl+C 使用字符 U+0003',
         {**SESSION, 'data': STRING}, ['sessionId', 'data']),
    tool('session_resize', '调整终端尺寸', {**SESSION, 'cols': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
         'rows': {'type': 'integer', 'minimum': 1, 'maximum': 1000}}, ['sessionId', 'cols', 'rows']),
    tool('session_close', '关闭电脑会话或释放设备输入权，不终止已有设备 shell', SESSION, ['sessionId']),
    tool('extensions_list', '发现本机已启用扩展；可指定target查询从机已安装的远程插件及其启用/授权状态', TARGET),
    tool('extension_capabilities', '查询指定电脑的扩展能力、参数说明和独立权限',
         {**TARGET, 'tool': STRING}, ['target', 'tool']),
    tool('extension_call', '调用已启用插件或核心扩展动作；先查询extensions_list和extension_capabilities。DB start 自动生成 requestId；超时查询原请求，禁止盲目重试。远程 permission 禁止。',
         {**TARGET, 'tool': STRING, 'action': STRING,
          'arguments': {'type': 'object', 'additionalProperties': True}}, ['target', 'tool', 'action']),
]


def validate(name, args):
    definition = next((t for t in TOOLS if t['name'] == name), None)
    if not definition or not isinstance(args, dict):
        raise ValueError('工具或参数无效')
    schema = definition['inputSchema']
    if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        raise ValueError('工具参数缺失或包含未知字段')
    for key, value in args.items():
        spec = schema['properties'][key]
        kind = spec['type']
        valid = (kind == 'boolean' and type(value) is bool or
                 kind == 'string' and isinstance(value, str) or
                 kind == 'integer' and type(value) is int or
                 kind == 'number' and type(value) in (int, float) or
                 kind == 'object' and isinstance(value, dict) or
                 kind == 'array' and isinstance(value, list) and all(isinstance(x, str) for x in value))
        if not valid or ('enum' in spec and value not in spec['enum']):
            raise ValueError(f'参数 {key} 类型或取值无效')
        if kind in ('integer', 'number'):
            import math
            if not math.isfinite(value) or ('minimum' in spec and value < spec['minimum']) or ('exclusiveMinimum' in spec and value <= spec['exclusiveMinimum']) or ('maximum' in spec and value > spec['maximum']):
                raise ValueError(f'参数 {key} 超出范围')
        if kind == 'array' and len(value) < spec.get('minItems', 0):
            raise ValueError(f'参数 {key} 不能为空')


class AgentAPI:
    def __init__(self, manager):
        self.manager = manager
        self.sessions = {}
        self.lock = threading.RLock()
        self.history = {}

    def publish(self, key, data):
        with self.lock:
            buffer = self.history.setdefault(key, OutputBuffer(128*1024))
            buffer.append(data)

    def notify(self, key, active):
        self.manager._broadcast_async({'type': 'agent-control', 'key': key, 'active': active})

    def targets(self):
        manager = self.manager
        targets = [dict(target='computer:local', kind='computer', local=True,
            label=os.environ.get('COMPUTERNAME', '本机'), online=True,
            role=manager.relay.config.role,
            windowsAllowed=manager.relay.config.allowWindows)]
        node = manager.relay.node
        if node:
            with node._io_lock:
                for peer, beat in node._peers.items():
                    if peer == node.peer_id or node._pairing_blocks(peer):
                        continue
                    targets.append(dict(target='computer:'+beat.nodeId, kind='computer', local=False,
                        label=node._peer_display_label(peer), peerId=peer, nodeId=beat.nodeId, role=beat.role,
                        online=not bool(node._peer_offline_reason(peer))))
        for device in manager._merged_devices():
            targets.append(dict(target='device:'+device['key'], kind='device', key=device['key'],
                label=device.get('serial', device['key']), online=device.get('relay', {}).get('online', True), device=device))
        return targets

    def resolve(self, target, kind=None):
        candidates = [t for t in self.targets() if t['target'] == target and (kind is None or t['kind'] == kind)]
        if len(candidates) != 1:
            raise ValueError('目标不存在或有歧义，请先调用 targets_list')
        if not candidates[0]['online']:
            raise RuntimeError('目标已离线')
        return candidates[0]

    async def windows(self, target, action, args):
        computer = self.resolve(target, 'computer')
        manager = self.manager
        if computer['local']:
            if action != 'capabilities' and not manager.relay.config.allowWindows:
                raise PermissionError('本机未允许 Windows PowerShell，请在「窗口 → PowerShell」中启用')
            data = await manager._run_blocking(manager.windows.call, action, args, 'local-agent')
            if action == 'capabilities':
                data['allowed'] = manager.relay.config.allowWindows
            return data
        return await manager._run_blocking(manager.relay.windows_call, computer['nodeId'], action, args)

    def claimed(self, key):
        with self.lock:
            return any(r.get('key') == key and r['state'] == 'running' for r in self.sessions.values())

    def take_local(self, key):
        """GUI 显式接管使旧 Agent 接入失效，不重复发送旧输入。"""
        with self.lock:
            for identifier, record in list(self.sessions.items()):
                if record.get('key') == key:
                    record['state'] = 'closed'
                    if record.get('local'):
                        self.manager.worker.terminal_hub.agent_release(key, record['generation'])
                    self.notify(key, False)
                    self.sessions.pop(identifier, None)

    def ended(self, key):
        self.take_local(key)
        with self.lock:
            self.history.pop(key, None)

    async def _open(self, args):
        target = self.resolve(args['target'])
        identifier = uuid.uuid4().hex
        if target['kind'] == 'computer':
            remote = await self.windows(target['target'], 'session_open', args)
            record = dict(kind='computer', target=target['target'], remoteId=remote['sessionId'], state='running')
        else:
            key = target['key']
            with self.lock:
                if self.claimed(key):
                    raise PermissionError('该设备已有 Agent 接入')
                if self.manager._is_remote_key(key):
                    task = getattr(self.manager, '_remote_io_tasks', {}).get('input', {}).get(key)
                    if task and not task.done():
                        raise PermissionError('界面输入尚未排空，请稍后接入')
                    generation = self.manager.relay.session_generation(key)
                    if generation is None or not self.manager.relay.session_input_ready(key) or not self.manager.relay.session_input_allowed(key):
                        raise PermissionError('远端终端未就绪或已由从机接管')
                    local = False
                else:
                    generation = self.manager.worker.terminal_hub.agent_claim(key)
                    local = True
                record = dict(kind='device', target=target['target'], key=key, local=local,
                              generation=generation, state='running', touched=time.monotonic())
                self.history.setdefault(key, OutputBuffer(128*1024))
        with self.lock:
            full = len(self.sessions) >= 128
            if not full:
                self.sessions[identifier] = record
        if full:
            if record['kind'] == 'computer':
                await self.windows(record['target'], 'session_close', {'sessionId': record['remoteId']})
            elif record['local']:
                self.manager.worker.terminal_hub.agent_release(record['key'], record['generation'])
            raise RuntimeError('Agent 会话数达到上限')
        if record.get('key'):
            self.notify(record['key'], True)
        return dict(sessionId=identifier, target=target['target'], state='running')

    async def _session(self, name, args):
        identifier = args['sessionId']
        with self.lock:
            record = self.sessions.get(identifier)
        if not record or record['state'] != 'running':
            raise RuntimeError('Agent 会话已结束或控制权已变化')
        if record['kind'] == 'computer':
            try:
                data = await self.windows(record['target'], name, {**args, 'sessionId': record['remoteId']})
            finally:
                if name == 'session_close':
                    self.sessions.pop(identifier, None)
            return {**data, 'sessionId': identifier}
        key = record['key']
        self.resolve(record['target'], 'device')
        if record['local']:
            state = self.manager.worker.terminal_hub.state(key)
            current = state['generation'] if state else None
        else:
            current = self.manager.relay.session_generation(key)
        if current != record['generation']:
            record['state'] = 'closed'
            self.sessions.pop(identifier, None)
            self.notify(key, False)
            raise PermissionError('设备会话代次已变化，请重新接入')
        record['touched'] = time.monotonic()
        if name == 'session_read':
            return dict(sessionId=identifier, state='running', **self.history[key].read(args.get('cursor', 0), args.get('limit', 32768)))
        if name == 'session_close':
            if record['local']:
                self.manager.worker.terminal_hub.agent_release(key, record['generation'])
            self.sessions.pop(identifier, None)
            self.notify(key, False)
            return dict(sessionId=identifier, state='closed')
        if name == 'session_write':
            if len(args['data']) > 65536:
                raise ValueError('终端输入超过 64 KiB')
            data = args['data'].encode('utf-8')
            if record['local']:
                self.manager.worker.terminal_hub.agent_write(key, current, data)
            else:
                if not await self.manager._run_blocking(self.manager.relay.session_input, key, data):
                    raise PermissionError('从机终端已收回输入权')
        elif name == 'session_resize':
            if record['local']:
                self.manager.worker.terminal_hub.agent_resize(key, current, args['cols'], args['rows'])
            elif not await self.manager._run_blocking(self.manager.relay.session_resize, key, args['cols'], args['rows']):
                raise PermissionError('从机终端已收回输入权')
        return dict(sessionId=identifier, state='running')

    async def call(self, name, args):
        validate(name, args)
        if name in ('extensions_list', 'extension_capabilities', 'extension_call'):
            from agent_extensions import invoke
            return await invoke(self, name, args)
        if name == 'targets_list':
            return {'targets': self.targets()}
        if name == 'target_status':
            return await self.windows(args['target'], 'capabilities', {})
        operations = {'windows_exec': 'start', 'job_status': 'status', 'job_output': 'output',
                      'job_cancel': 'cancel', 'jobs_list': 'list'}
        if name in operations:
            data = await self.windows(args['target'], operations[name], args)
            return {'target': args['target'], **data}
        if name == 'session_open':
            return await self._open(args)
        if name.startswith('session_'):
            return await self._session(name, args)
        if name == 'files_transfer':
            argv = [args['direction'], *args['paths'], args['destination']]
            target = self.resolve(args['target'])
            if target['kind'] == 'computer':
                if target['local']:
                    if not self.manager.relay.config.allowWindows:
                        raise PermissionError('本机未允许 Windows PowerShell')
                    def copy():
                        import relay_transfer
                        from pathlib import Path
                        if args['direction'] == 'push':
                            paths = [Path(value) for value in args['paths']]
                            destination = self.manager.windows.file_path(args['destination'], create=True)
                        else:
                            paths = [self.manager.windows.file_path(value) for value in args['paths']]
                            destination = args['destination']
                        for path in paths:
                            for item in relay_transfer.tree(path):
                                pass
                        relay_transfer.save_download(paths, destination)
                        return dict(success=True, output='电脑文件传输完成')
                    return await self.manager._run_blocking(copy)
                return await self.manager._run_blocking(self.manager.relay.windows_transfer,
                                                      target['nodeId'], argv)
            device = target
            if self.manager._is_remote_key(device['key']):
                return await self.manager._exec_adb(argv=argv, key=device['key'])
            selector = ['-t', str(device['device']['transportId'])] if device['device'].get('transportId') else ['-s', device['device']['serial']]
            return await self.manager._exec_adb(argv=selector+argv, key=device['key'])
        device = self.resolve(args['target'], 'device')
        argv = args['argv']
        if argv[0].startswith('-'):
            raise ValueError('请使用目标 ID，不允许自带设备或服务端选择符')
        if argv[0] in ('push', 'pull'):
            raise ValueError('文件传输请使用 files_transfer')
        if self.manager._is_remote_key(device['key']):
            node = self.manager.relay.node
            if node is None:
                raise RuntimeError('跨机中转已停止')
            peer, local_key = relay_node.split_remote_key(device['key'])
            result = await self.manager._run_blocking(node.exec_remote, peer, local_key, argv,
                                                     timeout=args.get('timeout', 30))
            return dict(success=result.ok and result.exitCode == 0, exitCode=result.exitCode,
                stdout=result.stdout, stderr=result.stderr, output=result.stdout+result.stderr,
                error=result.error, truncated=result.truncated, requestId=result.reqId,
                exitCodeAvailable=result.extra.get('exitCodeAvailable', result.exitCode is not None),
                target=device['target'], status=result.status)
        outcome = await self.manager._run_blocking(relay_node.execute, self.manager.relay.adb_path,
                        device['device'], argv, timeout=args.get('timeout', 30))
        return dict(success=outcome.exitCode == 0 and not outcome.error, **vars(outcome))

    def close(self):
        with self.lock:
            for record in self.sessions.values():
                if record['kind'] == 'device' and record['local']:
                    self.manager.worker.terminal_hub.agent_release(record['key'], record['generation'])
            self.sessions.clear()
            self.history.clear()
