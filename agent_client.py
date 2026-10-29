"""本机 Agent 薄客户端及多实例发现；不启动第二套 ADB 后端。"""
import asyncio
import json
import os
from pathlib import Path
import uuid

import websockets


def runtime_directory():
    return Path(os.environ.get('ADB_AGENT_RUNTIME_DIR') or
                Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ADBManager' / 'runtime')


def instances():
    import relay_app
    records = []
    for path in runtime_directory().glob('*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if relay_app._pid_alive(int(data['pid'])):
                records.append(data)
        except (OSError, ValueError, KeyError):
            continue
    return records


def choose(instance=None):
    candidates = [r for r in instances() if instance is None or r['instanceId'] == instance]
    if len(candidates) != 1:
        raise RuntimeError('没有可用实例或存在多个实例；使用 instances 查询，再用 --instance 明确选择')
    return candidates[0]


async def call(name, args, instance=None, wait=660):
    args = dict(args)
    if name == 'windows_exec':
        args.setdefault('jobId', uuid.uuid4().hex)
    if name == 'extension_call' and ((args.get('tool') == 'db-flash' and args.get('action') == 'start')
                                   or (args.get('tool') == 'serial' and args.get('action') == 'open')):
        nested = args.get('arguments', {})
        if not isinstance(nested, dict):
            raise ValueError('扩展动作参数必须为对象')
        nested = dict(nested)
        nested.setdefault('requestId', uuid.uuid4().hex)
        args['arguments'] = nested
    runtime = choose(instance)
    async with websockets.connect(f"ws://127.0.0.1:{int(runtime['wsPort'])}/agent",
                                  max_size=8*1024*1024) as ws:
        await ws.send(json.dumps(dict(type='agent-auth', token=runtime['token'])))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if not auth.get('success'):
            raise PermissionError('实例凭据已失效，请重新发现')
        identifier = uuid.uuid4().hex
        await ws.send(json.dumps(dict(type='agent-call', id=identifier, name=name, arguments=args), ensure_ascii=False))
        try:
            result = json.loads(await asyncio.wait_for(ws.recv(), wait))
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as exc:
            tracking = {'jobId': args.get('jobId'), 'target': args.get('target'), 'state': 'unknown'}
            if name == 'extension_call':
                tracking.update(tool=args.get('tool'), action=args.get('action'),
                    requestId=args.get('arguments', {}).get('requestId'), operationUncertain=True)
            return dict(type='agent-result', id=identifier, success=False,
                        error=f'连接响应未取得，操作结果未知：{type(exc).__name__}；请查询原任务，不要自动重试',
                        data=tracking)
        if result.get('id') != identifier:
            raise RuntimeError('Agent 响应标识不匹配')
        return result


def public_instances():
    return [{k: v for k, v in record.items() if k != 'token'} for record in instances()]
