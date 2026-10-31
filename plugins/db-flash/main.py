from db_flash import DBFlashService, RealExecutor
from db_flash_extension import DBFlashExtension
from plugin_sdk import Plugin as BasePlugin

class Plugin(BasePlugin):
    def __init__(self, root):
        super().__init__(root)
        self.service = DBFlashService(RealExecutor())
        self.on_dispose(self.service.close)
        self.extension = DBFlashExtension(self.service, self.data_dir / 'config.json')
    def call(self, action, arguments):
        if action == '__owned__':
            return self.extension.call_for_owner(arguments['action'], arguments['arguments'], owner=arguments['owner'])
        if action == 'capabilities':
            return {'success':True,'data':{'allowed':True,'remoteAllowed':self.extension.remote_allowed}}
        if action == 'permission':
            return self.extension.set_remote_permission(arguments.get('allowed'))
        return self.extension.call(action, arguments)
    def can_stop(self):
        return (self.service.status() or {}).get('state') != 'running'

def create_plugin(root):
    return Plugin(root)
