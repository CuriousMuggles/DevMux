"""JSON CLI：python -B agent_cli.py 工具名 --args '{...}'。"""
import argparse
import asyncio
import json
import sys

from agent_api import TOOLS
from agent_client import call, public_instances


def main():
    parser = argparse.ArgumentParser(description='DevMux Agent 接入')
    parser.add_argument('tool', choices=['instances', 'tools']+[t['name'] for t in TOOLS])
    parser.add_argument('--instance', help='运行实例 ID；多实例必须明确指定')
    parser.add_argument('--args', default='{}', help='工具参数 JSON')
    parser.add_argument('--args-file', help='从 UTF-8 JSON 文件读取参数，避免 shell 引号问题')
    options = parser.parse_args()
    try:
        if options.tool == 'instances':
            result = dict(success=True, data={'instances': public_instances()})
        elif options.tool == 'tools':
            result = dict(success=True, data={'tools': TOOLS})
        else:
            if options.args_file:
                from pathlib import Path
                args = json.loads(Path(options.args_file).read_text(encoding='utf-8-sig'))
            else:
                args = json.loads(options.args)
            result = asyncio.run(call(options.tool, args, options.instance))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('success') and result.get('data', {}).get('success', True) else 1
    except Exception as exc:
        print(json.dumps(dict(success=False, error=f'{type(exc).__name__}: {exc}'), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
