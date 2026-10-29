"""低影响历史清理：活动连接、协议文件及其他实例保持不变。"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
import relay_app


def make_history(share, peer='cache-test', session='sess-old', age_s=172800,
                 status='ended', pool_id=''):
    deployment = Path(relay.resolve_pool_root(str(share), pool_id))
    reference = deployment / 'peers' / (peer + '.json')
    reference.parent.mkdir(parents=True, exist_ok=True)
    if not reference.exists():
        reference.write_text('{}', encoding='utf-8')
    clock = reference.stat().st_mtime
    archive = deployment / 'archive' / peer
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / (session + '.zip')
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr(session + '.meta.json', json.dumps({'streamId': session, 'status': status}))
        package.writestr(session + '.out', '历史输出')
    os.utime(path, (clock - age_s, clock - age_s))
    return path


def run():
    from tests.test_relay_cache_active_only import CacheTests
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CacheTests)).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
