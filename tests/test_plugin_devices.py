"""Plugin device permissions and remote-window confinement."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from plugin_devices import dispatch


class DeviceBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.devices = [dict(target='device:a', kind='device', device={}),
            dict(target='device:b', kind='device', device={'relay': {'nodeId': 'slave'}}),
            dict(target='device:c', kind='device', device={'relay': {'nodeId': 'other'}})]
        def resolve(target, kind):
            if kind == 'computer':
                return {'nodeId': 'slave'}
            return next(d for d in self.devices if d['target'] == target)
        self.agent = SimpleNamespace(resolve=resolve, targets=lambda: self.devices,
                                     call=AsyncMock(return_value={'success': True}))
        self.manager = SimpleNamespace(agent=self.agent)
        self.record = {'plugin_permissions': ['device-adb'], 'plugin_target': 'computer:slave'}

    async def test_missing_permission_rejects_even_discovery(self):
        with self.assertRaises(PermissionError):
            await dispatch(self.manager, {}, {'operation': 'targets'})
        self.agent.call.assert_not_called()

    async def test_remote_discovery_excludes_local_and_other_slave(self):
        result = await dispatch(self.manager, self.record, {'operation': 'targets'})
        self.assertEqual([d['target'] for d in result['targets']], ['device:b'])

    async def test_remote_exec_cannot_escape_window_target(self):
        for target in ['device:a', 'device:c']:
            with self.assertRaises(PermissionError):
                await dispatch(self.manager, self.record, {'operation': 'exec', 'target': target})
        self.agent.call.assert_not_called()

    async def test_exec_uses_existing_agent_validation_and_routing(self):
        args = {'operation': 'exec', 'target': 'device:b', 'argv': ['shell', 'id'], 'timeout': 5}
        await dispatch(self.manager, self.record, args)
        self.agent.call.assert_awaited_once_with('adb_exec',
            {'target': 'device:b', 'argv': ['shell', 'id'], 'timeout': 5})


def run():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DeviceBridgeTests)).wasSuccessful()
