"""Real C++ bRPC client -> C++ Proxy -> HTTP/Connect mock, managed as child processes."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("child process exited during startup; inspect saved logs")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("child process did not listen")


class Suite:
    def __init__(self, client, port, mock_port, artifacts):
        self.client, self.port, self.mock_port = str(client), port, mock_port
        self.artifacts = artifacts
        self.lock = threading.Lock()
        self.records = []
        self.sequence = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, body, expected="OK"):
        with self.lock:
            self.sequence += 1
            name = f"case-{self.sequence:03d}-{method}"
        body = dict(body)
        body["context"] = {"request_id": name, **body.get("context", {})}
        path = self.artifacts / (name + ".request.json")
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        start = time.monotonic()
        proc = subprocess.run([self.client, f"--server=127.0.0.1:{self.port}",
                               f"--method={method}", f"--request={path}"],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)
        (self.artifacts / (name + ".stderr.log")).write_text(proc.stderr, encoding="utf-8")
        (self.artifacts / (name + ".response.json")).write_text(proc.stdout, encoding="utf-8")
        check(proc.returncode in (0, 2), f"{name}: client exit {proc.returncode}: {proc.stderr}")
        result = json.loads(proc.stdout)
        context = result["context"]
        code = context["status"]["code"]
        check(code == expected, f"{name}: expected {expected}, got {result}")
        check(context["request_id"] == body["context"]["request_id"], "request ID mismatch")
        check(int(context["timing"]["proxy_total_us"]) >= 0, "missing timing")
        check((proc.returncode == 0) == (code == "OK"), "client result exit code mismatch")
        with self.lock:
            self.records.append({"name": name, "method": method, "expected": expected,
                                 "status": code, "rpc_wall_ms": (time.monotonic()-start)*1000})
        return result

    def create(self, template="base", **kwargs):
        response = self.call("CreateSandbox", {"backend": "mock", "template_id": template, **kwargs})
        return response["sandbox"]["ref"], response

    def step(self, ref, tool, args, expected="OK", **kwargs):
        return self.call("RunStep", {"ref": ref, "tool": tool,
                                     "arguments_json": json.dumps(args, ensure_ascii=False), **kwargs}, expected)

    def stats(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.mock_port}/__mock__/stats", headers={"X-API-Key": "mock-key"})
        with self.opener.open(req, timeout=3) as response:
            return json.load(response)

    def run(self):
        ref, created = self.create()
        check(created["sandbox"]["envd_ready"] is True, "create readiness")
        check(created["completion_basis"] == "ENVD_READY", "create completion basis")
        status = self.call("GetStatus", {"ref": ref})
        check(status["sandbox"]["state"] == "RUNNING", "running status")
        bash = self.step(ref, "bash", {"command": "printf hello"})
        check(base64.b64decode(bash["bash"]["stdout"]) == b"hello", "stdout")
        check(bash["bash"]["exit_code"] == 0, "omitted backend zero exit code")
        nonzero = self.step(ref, "bash", {"command": "exit 2"})
        check(nonzero["bash"]["exit_code"] == 2, "nonzero command is a normal result")
        stderr = self.step(ref, "bash", {"command": "mock:stderr"})
        check(base64.b64decode(stderr["bash"]["stderr"]) == b"diagnostic\n", "stderr")
        content = "你好 agent\nquotes: \" \\\n"
        self.step(ref, "write", {"path": "/tmp/a space.txt", "content": content})
        read = self.step(ref, "read", {"path": "/tmp/a space.txt"})
        check(read["read"]["content"] == content, "UTF-8 file roundtrip")
        self.step(ref, "edit", {"path": "/tmp/a space.txt", "oldString": "agent", "newString": "sandbox"})
        self.step(ref, "edit", {"path": "/tmp/a space.txt", "oldString": "missing", "newString": "x"}, "EDIT_CONFLICT")
        self.step(ref, "write", {"path": "/tmp/duplicate", "content": "xx xx"})
        self.step(ref, "edit", {"path": "/tmp/duplicate", "oldString": "xx", "newString": "y"}, "EDIT_CONFLICT")
        self.step(ref, "write", {"path": "/tmp/empty", "content": ""})
        check(self.step(ref, "read", {"path": "/tmp/empty"})["read"]["content"] == "", "empty file")
        self.step(ref, "read", {"path": "/tmp/missing"}, "NOT_FOUND")
        self.step(ref, "write", {"path": "/missing-parent/a", "content": "x"}, "NOT_FOUND")
        self.step(ref, "read", {"path": "relative"}, "INVALID_ARGUMENT")
        self.step(ref, "read", {"path": "/tmp/a", "paht": "typo"}, "INVALID_ARGUMENT")
        self.step(ref, "edit", {"path": "/tmp/a", "oldString": "", "newString": "x"}, "INVALID_ARGUMENT")
        self.step(ref, "read", {"path": "/tmp/a"}, "INVALID_ARGUMENT", execution_timeout_ms=1)
        self.step(ref, "python", {}, "UNSUPPORTED")
        self.call("RunStep", {"ref": ref, "tool": "bash", "arguments_json": "{"}, "INVALID_ARGUMENT")
        self.call("GetStatus", {"ref": ref, "context": {"timeout_ms": 0}}, "INVALID_ARGUMENT")
        self.call("CreateSandbox", {"backend": "mock", "template_id": "base", "ttl_seconds": 0}, "INVALID_ARGUMENT")
        self.call("GetStatus", {"ref": {"backend": "absent", "sandbox_id": "abc"}}, "UNSUPPORTED")
        self.call("CreateSandbox", {"backend": "mock", "template_id": "missing"}, "NOT_FOUND")
        rate = self.call("CreateSandbox", {"backend": "mock", "template_id": "mock-rate-limit"}, "RATE_LIMITED")
        check(rate["context"]["status"]["retry_after_ms"] == 1000, "Retry-After")

        self.call("PauseSandbox", {"ref": ref})
        check(self.call("GetStatus", {"ref": ref})["sandbox"]["state"] == "PAUSED", "paused status")
        check(self.call("PauseSandbox", {"ref": ref})["already_paused"], "idempotent pause")
        self.step(ref, "bash", {"command": "printf hello"}, "INVALID_STATE")
        resumed = self.call("ResumeSandbox", {"ref": ref, "ttl_seconds": 1})
        check(not resumed["already_running"], "resume from pause")
        check(resumed["sandbox"]["expires_at"] >= created["sandbox"]["expires_at"], "TTL must not shorten")
        check(self.call("ResumeSandbox", {"ref": ref})["already_running"], "connect running sandbox")
        check("sandbox" in self.step(ref, "read", {"path": "/tmp/a space.txt"})["read"]["content"], "pause preserves file")

        large = self.step(ref, "bash", {"command": "mock:large-output"})["bash"]
        check(large["stdout_truncated"] and len(base64.b64decode(large["stdout"])) == 1024*1024, "output cap")
        self.step(ref, "read", {"path": "/tmp/mock-binary"}, "UNSUPPORTED")
        self.step(ref, "read", {"path": "/tmp/mock-oversize"}, "PAYLOAD_TOO_LARGE")
        self.step(ref, "bash", {"command": "mock:bad-trailer"}, "BACKEND_UNAVAILABLE")
        self.step(ref, "bash", {"command": "mock:truncate"}, "BACKEND_PROTOCOL_ERROR")
        self.step(ref, "bash", {"command": "mock:malformed"}, "BACKEND_PROTOCOL_ERROR")
        timed = self.step(ref, "bash", {"command": "mock:sleep:3000"}, "DEADLINE_EXCEEDED",
                          execution_timeout_ms=150, context={"timeout_ms": 2500})
        check(timed["bash"]["termination"] == "TIMED_OUT", "timeout termination")
        check(timed["context"]["outcome"] == "UNKNOWN", "timeout outcome")
        check(timed["context"]["status"]["retry_advice"] == "RECONCILE_FIRST", "no blind retry")
        check(any(path.endswith("/SendSignal") for _, path in self.stats()["calls"]), "timeout cleanup signal")
        before = sum(path.endswith("/Start") for _, path in self.stats()["calls"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            active = executor.submit(self.step, ref, "bash", {"command": "mock:sleep:1200"})
            deadline = time.monotonic()+5
            while sum(path.endswith("/Start") for _, path in self.stats()["calls"]) <= before:
                check(time.monotonic() < deadline, "concurrent command did not start")
                time.sleep(0.01)
            self.call("PauseSandbox", {"ref": ref}, "SANDBOX_BUSY")
            self.call("GetStatus", {"ref": ref})
            active.result()
        lost = self.step(ref, "write", {"path": "/tmp/mock-write-drop", "content": "committed"}, "BACKEND_UNAVAILABLE")
        check(lost["context"]["outcome"] == "UNKNOWN", "lost write acknowledgement")
        check(self.step(ref, "read", {"path": "/tmp/mock-write-drop"})["read"]["content"] == "committed", "reconcile write")
        count_before = sum(method == "POST" and path == "/sandboxes" for method, path in self.stats()["calls"])
        self.call("CreateSandbox", {"backend": "mock", "template_id": "mock-create-drop"}, "BACKEND_UNAVAILABLE")
        count_after = sum(method == "POST" and path == "/sandboxes" for method, path in self.stats()["calls"])
        check(count_after-count_before == 1, "Create must not retry")

        legacy, _ = self.create("mock-legacy")
        self.step(legacy, "write", {"path": "/tmp/legacy", "content": "multipart"})
        check(self.step(legacy, "read", {"path": "/tmp/legacy"})["read"]["content"] == "multipart", "legacy multipart upload")
        self.call("DeleteSandbox", {"ref": legacy})
        ack_ref, ack = self.create("mock-unready", wait_ready=False)
        check(ack["completion_basis"] == "BACKEND_ACK" and "envd_ready" not in ack["sandbox"], "control-only completion")
        self.call("DeleteSandbox", {"ref": ack_ref})
        unready = self.call("CreateSandbox", {"backend": "mock", "template_id": "mock-unready", "context": {"timeout_ms": 250}}, "DEADLINE_EXCEEDED")
        check(unready["sandbox"]["ref"]["sandbox_id"], "retain ID after readiness timeout")
        check(unready["context"]["outcome"] == "COMPLETED", "creation known despite readiness timeout")
        self.call("DeleteSandbox", {"ref": unready["sandbox"]["ref"]})
        # Saturate both step workers. A queued deadline must still be delivered promptly.
        refs = [self.create()[0] for _ in range(3)]
        starts = sum(path.endswith("/Start") for _, path in self.stats()["calls"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.step, target, "bash", {"command": "mock:sleep:1500"}) for target in refs[:2]]
            deadline = time.monotonic()+5
            while sum(path.endswith("/Start") for _, path in self.stats()["calls"]) < starts+2:
                check(time.monotonic() < deadline, "workers did not become busy")
                time.sleep(0.01)
            queued = self.step(refs[2], "read", {"path": "/tmp/a"}, "DEADLINE_EXCEEDED", context={"timeout_ms": 100})
            check(queued["context"]["backend_request_count"] == 0, "expired queued operation must not reach backend")
            check(int(queued["context"]["timing"]["proxy_total_us"]) < 1000000, "queued deadline delivered too late")
            for job in jobs:
                job.result()
        for target in refs:
            self.call("DeleteSandbox", {"ref": target})
        self.call("DeleteSandbox", {"ref": ref})
        check(self.call("DeleteSandbox", {"ref": ref})["already_absent"], "idempotent deletion")
        self.call("DeleteSandbox", {"ref": ref, "ignore_missing": False}, "NOT_FOUND")
        self.call("GetStatus", {"ref": ref}, "NOT_FOUND")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True, type=Path)
    parser.add_argument("--client", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    check(args.proxy.is_file() and args.client.is_file(), "compile both C++ binaries first")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix="e2e-", dir=args.report.parent))
    mock_port, proxy_port = unused_port(), unused_port()
    while proxy_port == mock_port:
        proxy_port = unused_port()
    config = {"backends": {"mock": {"type": "e2b", "profile": "kasandbox",
              "api_url": f"http://127.0.0.1:{mock_port}", "api_key_env": "MOCK_E2B_API_KEY",
              "envd_url_pattern": f"http://127.0.0.1:{mock_port}/envd/{{sandbox_id}}"}}}
    config_path = artifacts / "backends.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    processes, logs = [], []
    suite = Suite(args.client.resolve(), proxy_port, mock_port, artifacts)
    report = {"passed": False, "artifacts": str(artifacts.resolve())}
    try:
        env = dict(os.environ, MOCK_E2B_API_KEY="mock-key", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        for command, name, port in [
            ([sys.executable, str(ROOT / "tests/mock_backend/server.py"), "--port", str(mock_port)], "mock", mock_port),
            ([str(args.proxy.resolve()), f"--listen=127.0.0.1:{proxy_port}", f"--backends={config_path}",
              "--control_threads=2", "--step_threads=2"], "proxy", proxy_port),
        ]:
            log = (artifacts / (name + ".log")).open("w", encoding="utf-8")
            logs.append(log)
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
            processes.append(process)
            wait_port(port, process)
        suite.run()
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        report["cases"] = suite.records
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {len(suite.records)} RPC cases; report: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
