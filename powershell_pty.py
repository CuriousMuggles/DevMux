"""PowerShell PTY：直接读取原生输出，避开pywinpty桥接线程的阻塞EOF探测。"""
import threading

from winpty import PtyProcess


class PowerShellPtyProcess(PtyProcess):
    """复用启动/输入/尺寸接口，不建立socket桥或调用原生iseof。"""

    def __init__(self, pty):
        self.pty = pty
        self.pid = pty.pid
        self.argv = self.env = self.launch_dir = None
        self.closed = False
        self.flag_eof = False
        self.delayafterterminate = .1
        self.delayafterclose = .1
        self._reader_stop = threading.Event()
        self._close_lock = threading.Lock()
        self._pending = ''

    def read(self, size=8192):
        if self._reader_stop.is_set():
            raise EOFError('PowerShell PTY已关闭')
        if size <= 0:
            raise ValueError('读取长度必须大于0')
        data = self._pending or self.pty.read(blocking=False)
        if data:
            self._pending = data[size:]
            return data[:size]
        # 先取最后一段输出，再检查退出；不能调用可能阻塞的iseof()。
        if not self.pty.isalive():
            self.flag_eof = True
            raise EOFError('PowerShell PTY已退出')
        self._reader_stop.wait(.01)
        return ''

    def close(self, force=False):
        with self._close_lock:
            if self._reader_stop.is_set():
                return
            self._reader_stop.set()
            try:
                if self.pty.isalive() and not self.terminate(force):
                    raise OSError('无法结束PowerShell PTY')
            finally:
                self.closed = True
                try:
                    self.pty.cancel_io()
                except Exception:
                    pass
