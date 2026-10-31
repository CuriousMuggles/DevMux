"""Plugin subprocess host. Protocol: one JSON request and response per line."""
import importlib.util
import json
import sys
import logging
from pathlib import Path

def run(directory):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = Path(directory).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8-sig'))
    entry = manifest.get('backend')
    if not isinstance(entry, str):
        raise ValueError('插件缺少后端入口')
    path = (root / entry).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix != '.py':
        raise ValueError('无效的插件后端入口')
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('devmux_plugin', path)
    module = importlib.util.module_from_spec(spec)
    protocol = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    spec.loader.exec_module(module)
    plugin = module.create_plugin(root)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get('action') == '__shutdown__':
                    print(json.dumps({'success': True}), file=protocol, flush=True)
                    break
                if request.get('action') == '__can_stop__':
                    check = getattr(plugin, 'can_stop', lambda: True)
                    print(json.dumps({'success': True, 'result': bool(check())}), file=protocol, flush=True)
                    continue
                if request.get('action') == '__ready__':
                    print(json.dumps({'success': True, 'result': {}}), file=protocol, flush=True)
                    continue
                result = plugin.call(request['action'], request.get('arguments', {}))
                print(json.dumps({'success': True, 'result': result}, ensure_ascii=False), file=protocol, flush=True)
            except Exception as exc:
                print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False), file=protocol, flush=True)
    finally:
        close = getattr(plugin, 'close', None)
        try:
            if close:close()
        finally:
            dispose=getattr(plugin,'dispose',None)
            if dispose:dispose()

if __name__ == '__main__':
    run(sys.argv[1])
