from mijia_service import MijiaService
from plugin_sdk import Plugin as BasePlugin

class Plugin(BasePlugin):
    def __init__(self, root):
        super().__init__(root)
        self.service = MijiaService(self.data_dir)
        self.owner = 'plugin-window'
        self.on_dispose(lambda:self.service.cancel(self.owner))
    def call(self, action, arguments):
        return {'success':True,'data':self.service.call(self.owner, action, arguments)}

def create_plugin(root):
    return Plugin(root)
