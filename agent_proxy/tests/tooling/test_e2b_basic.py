"""Offline safety/flow checks: stub child processes; never contact a real deployment."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import e2b_smoke
from validate_spdiag import select_e2b_target


class RealBasicFlowTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "test-results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="e2b-basic-tools-", dir=ROOT / "test-results")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        for name in ("proxy", "client", "config.json"):
            (self.directory / name).write_text("{}", encoding="utf-8")
        self.calls = []
        self.files = {}
        self.fail_method = None
        self.corrupt_after_resume = False
        self.ignore_edit = False
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.returncode = 0
        self.process.wait.return_value = 0

    def response(self, command, **kwargs):
        method = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--method="))
        body = json.loads(kwargs["input"])
        self.calls.append((method, body))
        code = "ERROR_OPERATION_TIMEOUT" if method == self.fail_method else "ERROR_OK"
        state = "SANDBOX_PAUSED" if method == "PauseSandbox" else "SANDBOX_READY"
        result = {"success": code == "ERROR_OK", "error": {"code": code},
                  "meta": {"proxy_elapsed": {"value_ms": "5"}},
                  "sandbox": {"sandbox_id": "own-test-id", "state": state},
                  "operation": {"info": {"state": "OPERATION_SUCCEEDED"}},
                  "cleanup_confirmed": code == "ERROR_OK"}
        if method == "RunStep":
            args = json.loads(body["arguments_json"])
            tool = body["tool"]
            value = {}
            if tool == "bash":
                self.assertEqual(args["command"], "printf hello")
                value = {"stdout": "hello", "exit_code": 0}
            elif tool == "write":
                self.files[args["path"]] = args["content"]
            elif tool == "edit":
                if not self.ignore_edit:
                    self.files[args["path"]] = self.files[args["path"]].replace(args["oldString"], args["newString"])
            elif tool == "read":
                value = {"content": self.files[args["path"]]}
                if self.corrupt_after_resume and any(m == "ResumeSandbox" for m, _ in self.calls):
                    value = {"content": "lost original content"}
            result["result_json"] = json.dumps(value)
        return subprocess.CompletedProcess(command, 0 if code == "ERROR_OK" else 2, json.dumps(result), "")

    def run_driver(self, basic=True, allow=True):
        argv = ["e2b_smoke", "--proxy", "proxy", "--client", "client", "--backends", "config.json",
                "--backend", "actual", "--template", "base", "--cpu", "1", "--memory-mib", "1024",
                "--report", "report.json", "--spdiag-shm-name", "/agent_proxy_test"]
        if basic:
            argv.append("--basic-lifecycle")
        if allow:
            argv.append("--allow-create")
        with patch.object(sys, "argv", argv), patch.object(e2b_smoke, "free_port", return_value=12345), \
                patch.object(e2b_smoke.socket, "create_connection"), \
                patch.object(e2b_smoke.subprocess, "Popen", return_value=self.process) as popen, \
                patch.object(e2b_smoke.subprocess, "run", side_effect=self.response), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = e2b_smoke.main()
        self.assertIn("--graceful_quit_on_sigterm=true", popen.call_args.args[0])
        self.assertIn("--spdiag_shm_name=/agent_proxy_test", popen.call_args.args[0])
        return code, json.loads((self.directory / "report.json").read_text())

    def test_basic_six_successes_no_file_tools_or_extra_queries(self):
        code, report = self.run_driver()
        self.assertEqual(code, 0)
        self.assertTrue(report["sandbox_cleanup_confirmed"])
        self.assertEqual([method for method, _ in self.calls],
                         ["CreateSandbox", "GetStatus", "RunStep", "PauseSandbox", "ResumeSandbox", "DeleteSandbox"])
        self.assertEqual(len(report["perf_cases"]), 6)
        self.assertEqual(self.calls[-1][1]["create_request_id"], self.calls[0][1]["meta"]["request_id"])

    def test_failed_create_is_not_retried_and_uses_request_id_cleanup(self):
        self.fail_method = "CreateSandbox"
        code, report = self.run_driver()
        self.assertEqual(code, 1)
        self.assertEqual([method for method, _ in self.calls], ["CreateSandbox", "GetStatus", "DeleteSandbox"])
        self.assertTrue(report["sandbox_cleanup_confirmed"])

    def test_cleanup_failure_is_not_reported_as_pass(self):
        self.fail_method = "DeleteSandbox"
        code, report = self.run_driver()
        self.assertEqual(code, 1)
        self.assertFalse(report["sandbox_cleanup_confirmed"])
        self.assertFalse(report["passed"])

    def test_abnormal_proxy_exit_is_not_reported_as_pass(self):
        self.process.returncode = -15
        code, report = self.run_driver()
        self.assertEqual(code, 1)
        self.assertFalse(report["process_exits"]["proxy"]["normal_exit"])

    def test_default_full_smoke_retains_file_tools(self):
        code, report = self.run_driver(basic=False)
        self.assertEqual(code, 0)
        self.assertEqual([body["tool"] for method, body in self.calls if method == "RunStep"],
                         ["bash", "write", "read", "edit", "read", "read"])
        self.assertEqual(report["file_checks"], {"write_read": True, "edit_read": True, "after_resume": True})
        paths = {json.loads(body["arguments_json"])["path"] for method, body in self.calls
                 if method == "RunStep" and body["tool"] != "bash"}
        self.assertEqual(paths, {"/tmp/" + report["run_id"] + ".txt"})
        self.assertEqual(len(report["perf_cases"]), 13)
        self.assertTrue(all(body["sandbox_id"] == "own-test-id" for method, body in self.calls
                            if method == "RunStep"))

    def test_content_lost_after_resume_fails_and_cleans_own_sandbox(self):
        self.corrupt_after_resume = True
        code, report = self.run_driver(basic=False)
        self.assertEqual(code, 1)
        self.assertEqual(report["file_checks"], {"write_read": True, "edit_read": True, "after_resume": False})
        self.assertTrue(report["sandbox_cleanup_confirmed"])
        self.assertEqual(self.calls[-1][1]["create_request_id"], report["create_request_id"])

    def test_successful_edit_response_with_wrong_contents_fails(self):
        self.ignore_edit = True
        code, report = self.run_driver(basic=False)
        self.assertEqual(code, 1)
        self.assertEqual(report["file_checks"], {"write_read": True, "edit_read": False, "after_resume": False})
        self.assertTrue(report["sandbox_cleanup_confirmed"])
        self.assertFalse(any(method == "PauseSandbox" for method, _ in self.calls))

    def test_no_create_opt_in_stops_before_any_rpc(self):
        with self.assertRaises(SystemExit) as error:
            self.run_driver(allow=False)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls, [])

    def test_select_existing_resource_spec_and_reject_ambiguous_backend(self):
        item = {"type": "e2b", "api_key_env": "PRIVATE_KEY", "templates": {
            "existing": {"cpu_count": 1, "memory_mib": 1024}}}
        path = self.directory / "config.json"
        path.write_text(json.dumps({"backends": {"actual": item}}))
        self.assertEqual(select_e2b_target(path), ("actual", "existing", 1, 1024, "PRIVATE_KEY"))
        path.write_text(json.dumps({"backends": {"actual": item, "another": item}}))
        with self.assertRaisesRegex(ValueError, "multiple backends"):
            select_e2b_target(path)
        self.assertEqual(select_e2b_target(path, "actual"), ("actual", "existing", 1, 1024, "PRIVATE_KEY"))


if __name__ == "__main__":
    unittest.main()
