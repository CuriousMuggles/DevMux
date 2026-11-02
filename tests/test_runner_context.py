"""Credential preflight must distinguish environment failure from test failure."""
import unittest
from unittest.mock import patch
from tests import run_all


class ContextTests(unittest.TestCase):
    def test_environment_block_propagates_without_running_test_group(self):
        with patch.object(run_all, 'check_mijia_context', side_effect=run_all.UserContextRequired('DPAPI')):
            with self.assertRaises(run_all.UserContextRequired):
                run_all.run_test('Mijia', 'tests.test_mijia_service')

    def test_real_group_failure_is_not_reclassified(self):
        from tests import test_mijia_service
        with patch.object(run_all, 'check_mijia_context'), patch.object(test_mijia_service, 'run', return_value=False):
            self.assertFalse(run_all.run_test('Mijia', 'tests.test_mijia_service'))

    def test_blocked_group_keeps_full_suite_unsuccessful(self):
        with patch.object(run_all, 'TESTS', [('Mijia', 'tests.test_mijia_service')]), patch.object(
            run_all, 'check_mijia_context', side_effect=run_all.UserContextRequired('DPAPI')):
            self.assertFalse(run_all.main())


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ContextTests)).wasSuccessful()
