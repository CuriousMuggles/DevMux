"""Manage a plugin's isolated process and serialized requests."""
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

class PluginProcess:
    def __init__(self, directory):
        self.lock = threading.RLock()
        directory=Path(directory)
        data=directory.parent/'.data'/directory.name
        data.mkdir(parents=True,exist_ok=True)
        self.log_path=data/'runtime.log'
        if self.log_path.is_file() and self.log_path.stat().st_size>2*1024*1024:
            self.log_path.write_bytes(self.log_path.read_bytes()[-1024*1024:])
        self.log_stream=self.log_path.open('ab')
        host = Path(__file__).with_name('plugin_host.py')
        command = ([sys.executable, '--plugin-host', str(directory)] if getattr(sys, 'frozen', False)
                   else [sys.executable, str(host), str(directory)])
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.log_stream, text=True, encoding='utf-8',
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except BaseException:
            self.log_stream.close();raise

        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def call(self, action, arguments=None, timeout=30):
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError('插件参数必须为对象')
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError('插件进程已退出'+self.diagnostic())
            self.process.stdin.write(json.dumps({'action': action, 'arguments': arguments or {}}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            try:
                line = self.responses.get(timeout=timeout)
            except queue.Empty:
                self.close()
                raise TimeoutError("插件调用超时，结果未确认，不会自动重试")
            if not line:
                raise RuntimeError('插件进程未返回结果'+self.diagnostic())
            response = json.loads(line)
            if not response.get('success'):
                raise RuntimeError(response.get('error', '插件调用失败'))
            return response.get('result')

    def diagnostic(self):
        try:
            with self.log_path.open('rb') as stream:
                stream.seek(max(0,self.log_path.stat().st_size-4000))
                text=stream.read().decode('utf-8',errors='replace').strip()
            return '\n'+text if text else ''
        except OSError:return ''

    def close(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(json.dumps({'action': '__shutdown__'}) + '\n')
                    self.process.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.reader.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.log_stream.close()
