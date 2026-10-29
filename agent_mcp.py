"""MCP stdio 工具入口；stdout 只输出 UTF-8 JSON-RPC，复用本机常驻实例。"""
import argparse
import asyncio
import json
import sys

from agent_api import TOOLS, validate
from agent_client import call
from app_version import APP_VERSION

VERSIONS = ('2025-03-26', '2024-11-05')


async def dispatch(message, instance=None):
    identifier = message.get('id')
    if identifier is None:
        return None
    response = dict(jsonrpc='2.0', id=identifier)
    method = message.get('method')
    params = message.get('params') or {}
    try:
        if method == 'initialize':
            version = params.get('protocolVersion')
            response['result'] = dict(protocolVersion=version if version in VERSIONS else VERSIONS[0],
                capabilities={'tools': {}}, serverInfo={'name': 'devmux', 'version': APP_VERSION})
        elif method == 'ping':
            response['result'] = {}
        elif method == 'tools/list':
            response['result'] = {'tools': TOOLS}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            validate(name, args)
            result = await call(name, args, instance)
            failed = not result.get('success') or result.get('data', {}).get('success') is False
            response['result'] = dict(content=[{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], isError=failed)
        else:
            response['error'] = dict(code=-32601, message='未知方法')
    except Exception as exc:
        if method == 'tools/call':
            response['result'] = dict(content=[{'type': 'text', 'text': f'{type(exc).__name__}: {exc}'}], isError=True)
        else:
            response['error'] = dict(code=-32602, message=str(exc))
    return response


def main():
    parser = argparse.ArgumentParser(description='DevMux MCP stdio')
    parser.add_argument('--instance')
    options = parser.parse_args()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('无效 JSON-RPC 请求')
            response = asyncio.run(dispatch(message, options.instance))
        except Exception as exc:
            response = dict(jsonrpc='2.0', id=None, error=dict(code=-32700, message=str(exc)))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    main()
