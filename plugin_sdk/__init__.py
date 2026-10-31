"""Public backend helpers for DevMux plugins, API version 1."""
import logging
from pathlib import Path


class Plugin:
    """Optional base class. create_plugin(root) and call(action, arguments) remain the contract."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data_dir = self.root.parent / '.data' / self.root.name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger('devmux.plugin.' + self.root.name)
        self._cleanups = []
        self._disposed = False

    def on_dispose(self, callback):
        if self._disposed:
            raise RuntimeError('插件已释放')
        self._cleanups.append(callback)
        return callback

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        for callback in reversed(self._cleanups):
            try:
                callback()
            except Exception:
                self.logger.exception('插件资源释放失败')
        self._cleanups.clear()

    def close(self):
        self.dispose()
