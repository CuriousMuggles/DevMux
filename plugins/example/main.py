from plugin_sdk import Plugin


class Tool(Plugin):
    def call(self, action, arguments):
        if action == 'echo':
            self.logger.info('Echo invoked')
            return {'text': arguments.get('text', '')}
        raise ValueError('Unknown action: ' + str(action))


def create_plugin(root):
    return Tool(root)
