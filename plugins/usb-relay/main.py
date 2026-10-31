from pathlib import Path
from usb_relay import USBRelayService
from plugin_sdk import Plugin

class Relay(Plugin):
    def __init__(self, root):
        super().__init__(root)
        self.service = USBRelayService(self.data_dir / 'config.json')
        self.on_dispose(lambda:self.service.call('close', {}))
    def call(self, action, arguments):
        if action == '__owned__':
            return self.call(arguments['action'], arguments['arguments'])
        if action == 'permission':
            return self.service.set_remote_permission(arguments.get('allowed'))
        if action not in self.service.ACTIONS:
            raise ValueError('不支持的继电器动作')
        return self.service.call(action, arguments)

def create_plugin(root):
    return Relay(root)
