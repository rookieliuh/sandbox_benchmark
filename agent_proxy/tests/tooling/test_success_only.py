"""Check that the successful workload issues only the six intended normal RPCs."""
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
from e2e import Suite, stop_child


class SuccessOnlyTest(unittest.TestCase):
    def test_normal_shutdown_is_recorded(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        self.assertEqual(stop_child(process),
                         {"returncode": 0, "forced_kill": False, "normal_exit": True})
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    def test_sigterm_exit_is_not_treated_as_normal_flush(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = -15
        self.assertFalse(stop_child(process)["normal_exit"])

    def test_forced_kill_is_recorded_as_abnormal(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 10), -9]
        result = stop_child(process)
        self.assertFalse(result["normal_exit"])
        self.assertTrue(result["forced_kill"])
        process.kill.assert_called_once_with()

    def test_already_failed_child_is_not_signalled_again(self):
        process = Mock()
        process.poll.return_value = 1
        process.wait.return_value = 1
        self.assertFalse(stop_child(process)["normal_exit"])
        process.terminate.assert_not_called()

    def suite(self):
        suite = Suite("unused-client", 0, 0, Path("unused-artifacts"))
        suite.call = Mock(side_effect=[
            {"sandbox": {"sandbox_id": "test-sandbox", "state": "SANDBOX_READY"}},
            {"sandbox": {"state": "SANDBOX_READY"}},
            {"result_json": json.dumps({"exit_code": 0, "stdout": "hello"})},
            {"sandbox": {"state": "SANDBOX_PAUSED"}},
            {"sandbox": {"state": "SANDBOX_READY"}},
            {"cleanup_confirmed": True},
        ])
        return suite

    def test_exact_six_normal_calls(self):
        suite = self.suite()
        suite.run_success_only()
        calls = suite.call.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ["CreateSandbox", "GetStatus", "RunStep", "PauseSandbox",
                          "ResumeSandbox", "DeleteSandbox"])
        self.assertEqual(calls[0].args[1]["template"], "base")
        self.assertEqual(json.loads(calls[2].args[1]["arguments_json"]), {"command": "printf hello"})
        self.assertEqual(calls[2].args[2], "ERROR_OK")

    def test_unexpected_status_still_deletes_own_sandbox(self):
        suite = self.suite()
        suite.call.side_effect = [
            {"sandbox": {"sandbox_id": "test-sandbox", "state": "SANDBOX_READY"}},
            RuntimeError("unexpected status failure"),
            {"cleanup_confirmed": True},
        ]
        with self.assertRaisesRegex(RuntimeError, "unexpected status failure"):
            suite.run_success_only()
        self.assertEqual(suite.call.call_args.args, ("DeleteSandbox", {"sandbox_id": "test-sandbox"}))


if __name__ == "__main__":
    unittest.main()
