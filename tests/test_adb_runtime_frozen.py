"""正式单文件包退出回归：私有端口/虚构USB序列号，不触碰5037或真实设备。"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _version(port):
    with socket.create_connection(('127.0.0.1', port), timeout=1) as sock:
        payload = b'host:version'
        sock.sendall(f'{len(payload):04x}'.encode() + payload)
        response = sock.recv(64)
        assert response.startswith(b'OKAY'), response
        return response.hex()


def _wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('等待冻结验证条件超时')


def _server(adb, directory, env):
    port = _port()
    log = (directory / ('adb-server-' + str(port) + '.log')).open('wb')
    process = subprocess.Popen([str(adb), '-L', f'tcp:{port}',
        '--one-device', 'ADB_MANAGER_CLEANUP_TEST_NO_DEVICE', 'server', 'nodaemon'],
        cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        creationflags=CREATE_NO_WINDOW)
    log.close()
    def ready():
        assert process.poll() is None, '隔离ADB提前退出'
        try:
            return bool(_version(port))
        except OSError:
            return False
    try:
        _wait(ready)
        return process, port
    except BaseException:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        raise


def run(executable):
    binary = Path(executable).resolve()
    assert binary.is_file(), binary
    runtime = ROOT / '.agent-runtime' / 'mei-cleanup-20261015'
    runtime.mkdir(parents=True, exist_ok=True)
    # 留存结果和服务日志；测试只结束自己通过 Popen 创建的进程。
    directory = Path(tempfile.mkdtemp(prefix='frozen-', dir=runtime)).resolve()
    assert directory.is_relative_to(runtime.resolve())
    env = {**os.environ, 'ADB_PATH': '', 'ADB_MANAGER_TOOL_CACHE': str(directory / 'cache'),
           'ANDROID_USER_HOME': str(directory / 'android-user'), 'ADB_MDNS_AUTO_CONNECT': '0',
           'PYTHONDONTWRITEBYTECODE': '1'}
    server = None
    hosts = []
    result = {'executable': str(binary), 'success': False, 'cases': []}
    try:
        # 原路径的资源占用可逆复现；仅读版本，ADB仅允许虚构USB序列号。
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(binary))
        baseline = directory / '_MEI-baseline' / 'adb'
        baseline.mkdir(parents=True)
        assert baseline.parent.resolve().parent == directory
        for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll'):
            (baseline / name).write_bytes(archive.extract('adb\\' + name))
        original, port = _server(baseline / 'adb.exe', directory, env)
        try:
            try:
                shutil.rmtree(baseline.parent)
            except PermissionError as exc:
                result['baseline'] = {'cleanupBlocked': True, 'winerror': exc.winerror,
                                      'serverAlive': original.poll() is None, 'port': port}
            else:
                raise AssertionError('未复现原ADB路径占用')
            assert original.poll() is None
        finally:
            original.terminate()
            original.wait(timeout=10)
        shutil.rmtree(baseline.parent)
        print('  [PASS] 原_MEI路径真实ADB常驻时删除失败，结束本测试服务后可清理', flush=True)

        scripts = directory / 'scripts'
        scripts.mkdir()
        probe = scripts / 'download_image.py'
        probe.write_text("""import json, sys, time
from pathlib import Path
import adb_worker
result = Path(sys.argv[1])
release = Path(sys.argv[2])
result.write_text(json.dumps({'mei':sys._MEIPASS,'adb':adb_worker.ADB_PATH}),encoding='utf-8')
deadline = time.monotonic()+40
while not release.exists():
    if time.monotonic()>deadline: raise RuntimeError('冻结宿主等待退出指令超时')
    time.sleep(.05)
""", encoding='utf-8')
        for index in range(2):
            metadata = directory / f'host-{index}.json'
            release = directory / f'host-{index}.release'
            with (directory / f'host-{index}.log').open('wb') as log:
                host = subprocess.Popen([str(binary), '--db-download-host', str(probe),
                    str(metadata), str(release)], cwd=directory, env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=CREATE_NO_WINDOW)
            hosts.append(host)
            def ready():
                assert host.poll() is None, '冻结宿主提前退出'
                return metadata.is_file()
            _wait(ready)
            data = json.loads(metadata.read_text('utf-8'))
            mei = Path(data['mei']).resolve()
            adb = Path(data['adb']).resolve()
            assert mei.is_dir() and not adb.is_relative_to(mei), data
            assert adb.is_relative_to(directory / 'cache'), data
            if server is None:
                server, port = _server(adb, directory, env)
                first_adb = adb
            else:
                assert adb == first_adb, '第二宿主必须复用正在运行的同版本缓存'
            release.touch()
            assert host.wait(timeout=20) == 0
            _wait(lambda: not mei.exists(), timeout=5)
            assert server.poll() is None
            version = _version(port)
            data.update({'exitCode': host.returncode, 'meiRemoved': not mei.exists(),
                         'serverAlive': True, 'port': port, 'versionResponse': version})
            result['cases'].append(data)
            print(f'  [PASS] 冻结宿主{index+1}退出0、_MEI自动移除，私有端口ADB仍存活', flush=True)
        result['success'] = True
        return True
    finally:
        for host in hosts:
            if host.poll() is None:
                host.terminate()
                host.wait(timeout=10)
        if server is not None:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=10)
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('  证据：' + str(directory / 'result.json'), flush=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('请指定本次构建的 DevMux.exe')
    sys.exit(0 if run(sys.argv[1]) else 1)
