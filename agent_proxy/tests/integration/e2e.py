"""Native bRPC contract tests against a loopback E2B/Connect fixture."""
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
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ("base", "missing", "mock-rate-limit", "mock-legacy", "mock-unready",
             "mock-create-drop", "mock-slow-pause", "mock-delete-stuck", "mock-health-drop",
             "mock-health-200")


def check(value, message):
    if not value:
        raise AssertionError(message)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("child process exited during startup; inspect proxy.log/mock.log")
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

    def call(self, method, body, expected="ERROR_OK"):
        with self.lock:
            self.sequence += 1
            number = self.sequence
        name = f"case-{number:03d}-{method}"
        body = dict(body)
        body["meta"] = {"request_id": name, "run_id": "mock-run", "task_instance_id": "replay-1",
                        "task_id": "task-1", "trace_id": "trace-1", **body.get("meta", {})}
        if method != "GetStatus":
            body.setdefault("timeout_ms", 10000)
        if method == "CreateSandbox":
            body.setdefault("spec", {"cpu_count": 1, "memory_mib": 512})
            body.setdefault("ttl_seconds", 300)
        if method == "RunStep":
            body.setdefault("step_id", number)
        path = self.artifacts / (name + ".request.json")
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        start = time.monotonic()
        proc = subprocess.run([self.client, f"--server=127.0.0.1:{self.port}",
                               f"--method={method}", f"--request={path}"],
                              capture_output=True, text=True, encoding="utf-8", timeout=25)
        (self.artifacts / (name + ".stderr.log")).write_text(proc.stderr, encoding="utf-8")
        (self.artifacts / (name + ".response.json")).write_text(proc.stdout, encoding="utf-8")
        check(proc.returncode in (0, 2), f"{name}: client exit {proc.returncode}: {proc.stderr}")
        result = json.loads(proc.stdout)
        code = result["error"]["code"]
        check(code == expected, f"{name}: expected {expected}, got {result}")
        check(result["meta"]["request_id"] == body["meta"]["request_id"], "request ID mismatch")
        check(result["meta"]["trace_id"] == body["meta"]["trace_id"], "trace ID mismatch")
        check(int(result["meta"]["proxy_elapsed"]["value_ms"]) >= 0, "missing timing")
        check(result["success"] == (code == "ERROR_OK"), "success/error mismatch")
        check((proc.returncode == 0) == result["success"], "client result exit code mismatch")
        if method == "RunStep":
            check(result["error_message"] == result["error"]["message"], "error text mismatch")
            check(int(result["step_id"]) == int(body["step_id"]), "step ID mismatch")
            json.loads(result["result_json"])
        with self.lock:
            self.records.append({"name": name, "method": method, "expected": expected,
                                 "status": code, "rpc_wall_ms": (time.monotonic()-start)*1000})
        return result

    def create(self, template="base", **kwargs):
        response = self.call("CreateSandbox", {"backend": "mock", "template": template, **kwargs})
        return response["sandbox"]["sandbox_id"], response

    def step(self, sid, tool, args, expected="ERROR_OK", **kwargs):
        return self.call("RunStep", {"sandbox_id": sid, "tool": tool,
                                     "arguments_json": json.dumps(args, ensure_ascii=False), **kwargs}, expected)

    def operation(self, request_id):
        return self.call("GetStatus", {"operation_request_id": request_id})["operation"]

    def stats(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.mock_port}/__mock__/stats", headers={"X-API-Key": "mock-key"})
        with self.opener.open(req, timeout=3) as response:
            return json.load(response)

    def starts(self):
        return sum(path.endswith("/Start") for _, path in self.stats()["calls"])

    def run(self):
        # The default fixture uses real envd's empty 204 throughout create/status/resume.
        # Keep coverage for compatible backends that return 200 as well.
        compatible, _ = self.create("mock-health-200")
        check(self.call("GetStatus", {"sandbox_id": compatible})["sandbox"]["state"] == "SANDBOX_READY",
              "HTTP 200 health compatibility")
        check(self.call("DeleteSandbox", {"sandbox_id": compatible})["cleanup_confirmed"],
              "cleanup HTTP 200 health fixture")
        sid, created = self.create(meta={"request_id": "create-primary", "phase": "PHASE_CREATE"})
        check(sid.startswith("ap-"), "proxy identity required")
        check(created["sandbox"]["state"] == "SANDBOX_READY", "verified create readiness")
        check(created["sandbox"]["effective_spec"] == {"cpu_count": 1, "memory_mib": "512"}, "effective resources")
        check(created["operation"]["state"] == "OPERATION_SUCCEEDED", "create operation state")
        record = self.operation("create-primary")
        check(record["create_result"] == created, "original response must be preserved exactly")
        check(record["original_meta"]["phase"] == "PHASE_CREATE", "original benchmark metadata")
        self.call("CreateSandbox", {"backend": "mock", "template": "base",
                  "meta": {"request_id": "create-primary", "phase": "PHASE_CREATE"}}, "ERROR_DUPLICATE_REQUEST")
        self.call("CreateSandbox", {"backend": "mock", "template": "base", "ttl_seconds": 100,
                  "meta": {"request_id": "create-primary", "phase": "PHASE_CREATE"}}, "ERROR_REQUEST_ID_CONFLICT")
        check(self.operation("create-primary")["create_result"] == created, "duplicate must not overwrite record")
        self.call("GetStatus", {"operation_request_id": "create-primary", "meta": {"request_id": "create-primary"}},
                  "ERROR_REQUEST_ID_CONFLICT")
        self.call("GetStatus", {"operation_request_id": "not-recorded"}, "ERROR_NOT_FOUND")
        self.call("GetStatus", {}, "ERROR_INVALID_ARGUMENT")
        self.call("GetStatus", {"sandbox_id": "not-registered"}, "ERROR_NOT_FOUND")
        check(self.call("GetStatus", {"sandbox_id": sid})["sandbox"]["state"] == "SANDBOX_READY", "running status")
        self.call("CreateSandbox", {"backend": "absent", "template": "base"}, "ERROR_UNSUPPORTED_BACKEND")
        self.call("CreateSandbox", {"backend": "mock", "template": "base", "spec": {"cpu_count": 2, "memory_mib": 512}},
                  "ERROR_UNSUPPORTED_CAPABILITY")
        self.call("CreateSandbox", {"backend": "mock", "template": "unconfigured"}, "ERROR_UNSUPPORTED_CAPABILITY")
        self.call("CreateSandbox", {"backend": "mock", "template": "base", "ttl_seconds": 0}, "ERROR_INVALID_ARGUMENT")
        invalid = self.call("CreateSandbox", {"backend": "mock", "template": "base", "timeout_ms": 0}, "ERROR_INVALID_ARGUMENT")
        cleanup = self.call("DeleteSandbox", {"create_request_id": invalid["meta"]["request_id"]})
        check(cleanup["cleanup_confirmed"] and not cleanup["sandbox_id"], "no dispatch means no created resource")
        self.call("CreateSandbox", {"backend": "mock", "template": "missing"}, "ERROR_NOT_FOUND")
        self.call("CreateSandbox", {"backend": "mock", "template": "mock-rate-limit"}, "ERROR_BACKEND_ERROR")

        bash = json.loads(self.step(sid, "bash", {"command": "printf hello"})["result_json"])
        check(bash["stdout"] == "hello" and bash["exit_code"] == 0, "normalized bash result")
        check(base64.b64decode(bash["stdout_base64"]) == b"hello", "lossless output")
        nonzero = json.loads(self.step(sid, "bash", {"command": "exit 2"})["result_json"])
        check(nonzero["exit_code"] == 2, "nonzero exit is a valid tool return")
        stderr = json.loads(self.step(sid, "bash", {"command": "mock:stderr"})["result_json"])
        check(stderr["stderr"] == "diagnostic\n", "stderr")
        self.step(sid, "bash", {"command": "printf hello"}, "ERROR_INVALID_ARGUMENT", step_id=0)
        self.step(sid, "bash", {"command": "printf hello"}, "ERROR_INVALID_ARGUMENT", timeout_ms=0)
        self.step(sid, "python", {}, "ERROR_UNSUPPORTED_TOOL")
        rejected = self.step(sid, "read", {"path": "/tmp/mock-backend-error"}, "ERROR_BACKEND_ERROR")
        check(rejected["error"]["backend_code"] == "HTTP_500" and
              all(x in rejected["error"]["message"] for x in
                  ("source=envd", "phase=operation", "method=GET", "resource=file", "http_status=500")),
              "surface file HTTP failure without losing stage")
        check("private-backend-diagnostic" not in json.dumps(rejected), "never expose raw backend error body")
        self.call("RunStep", {"sandbox_id": sid, "tool": "bash", "arguments_json": "{"}, "ERROR_INVALID_ARGUMENT")
        content = '你好 agent\nquotes: " \\\n'
        write = self.step(sid, "write", {"path": "/tmp/a space.txt", "content": content},
                          step_id=101, meta={"request_id": "write-once"})
        saved = self.operation("write-once")
        check(saved["step_result"] == write, "stored tool response")
        self.step(sid, "write", {"path": "/tmp/a space.txt", "content": content}, "ERROR_DUPLICATE_REQUEST",
                  step_id=101, meta={"request_id": "write-once"})
        check(json.loads(self.step(sid, "read", {"path": "/tmp/a space.txt"})["result_json"])["content"] == content, "UTF-8 file")
        self.step(sid, "edit", {"path": "/tmp/a space.txt", "oldString": "agent", "newString": "sandbox"})
        failed = self.step(sid, "edit", {"path": "/tmp/a space.txt", "oldString": "missing", "newString": "x"}, "ERROR_INVALID_STATE")
        check(self.operation(failed["meta"]["request_id"])["step_result"] == failed, "store definitive failures")
        self.step(sid, "write", {"path": "/tmp/duplicate", "content": "xx xx"})
        self.step(sid, "edit", {"path": "/tmp/duplicate", "oldString": "xx", "newString": "y"}, "ERROR_INVALID_STATE")
        self.step(sid, "write", {"path": "/tmp/empty", "content": ""})
        check(json.loads(self.step(sid, "read", {"path": "/tmp/empty"})["result_json"])["content"] == "", "empty file")
        self.step(sid, "read", {"path": "/tmp/missing"}, "ERROR_NOT_FOUND")
        self.step(sid, "write", {"path": "/missing-parent/a", "content": "x"}, "ERROR_NOT_FOUND")
        self.step(sid, "read", {"path": "relative"}, "ERROR_INVALID_ARGUMENT")
        self.step(sid, "read", {"path": "/tmp/a", "paht": "typo"}, "ERROR_INVALID_ARGUMENT")
        self.step(sid, "edit", {"path": "/tmp/a", "oldString": "", "newString": "x"}, "ERROR_INVALID_ARGUMENT")

        paused = self.call("PauseSandbox", {"sandbox_id": sid})
        check(paused["sandbox"]["state"] == "SANDBOX_PAUSED", "verified pause")
        check(self.call("GetStatus", {"sandbox_id": sid})["sandbox"]["state"] == "SANDBOX_PAUSED", "paused status")
        self.call("PauseSandbox", {"sandbox_id": sid})
        self.step(sid, "bash", {"command": "printf hello"}, "ERROR_INVALID_STATE")
        resumed = self.call("ResumeSandbox", {"sandbox_id": sid})
        check(resumed["sandbox"]["sandbox_id"] == sid and resumed["sandbox"]["state"] == "SANDBOX_READY", "stable identity after resume")
        self.call("ResumeSandbox", {"sandbox_id": sid})
        check("sandbox" in json.loads(self.step(sid, "read", {"path": "/tmp/a space.txt"})["result_json"])["content"], "preserved file")
        slow, _ = self.create("mock-slow-pause")
        check(self.call("PauseSandbox", {"sandbox_id": slow})["sandbox"]["state"] == "SANDBOX_PAUSED", "poll asynchronous pause")
        self.call("DeleteSandbox", {"sandbox_id": slow})

        large = json.loads(self.step(sid, "bash", {"command": "mock:large-output"})["result_json"])
        check(large["stdout_truncated"] and len(large["stdout"]) == 1024*1024, "output cap")
        self.step(sid, "read", {"path": "/tmp/mock-binary"}, "ERROR_UNSUPPORTED_CAPABILITY")
        self.step(sid, "read", {"path": "/tmp/mock-oversize"}, "ERROR_INVALID_ARGUMENT")
        for cmd in ("mock:bad-trailer", "mock:truncate", "mock:malformed"):
            self.step(sid, "bash", {"command": cmd}, "ERROR_BACKEND_ERROR")
        timed = self.step(sid, "bash", {"command": "mock:sleep:3000"}, "ERROR_TOOL_TIMEOUT", timeout_ms=150)
        tool = json.loads(timed["result_json"])
        check(tool["exit_code"] is None and tool["termination"] == "TIMED_OUT", "unknown exit code must be null")
        record = self.operation(timed["meta"]["request_id"])
        check(record["info"]["state"] == "OPERATION_UNKNOWN" and "step_result" not in record, "unknown operations have no final response")
        check(any(path.endswith("/SendSignal") for _, path in self.stats()["calls"]), "timeout signal")

        before = self.starts()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            active = executor.submit(self.step, sid, "bash", {"command": "mock:sleep:1200"},
                                     step_id=202, meta={"request_id": "active-step"})
            deadline = time.monotonic()+5
            while self.starts() <= before:
                check(time.monotonic() < deadline, "active command not started")
                time.sleep(0.01)
            running = self.operation("active-step")
            check(running["info"]["state"] == "OPERATION_RUNNING" and "step_result" not in running, "live operation query")
            self.step(sid, "bash", {"command": "mock:sleep:1200"}, "ERROR_DUPLICATE_REQUEST",
                      step_id=202, meta={"request_id": "active-step"})
            self.call("PauseSandbox", {"sandbox_id": sid}, "ERROR_INVALID_STATE")
            self.call("GetStatus", {"sandbox_id": sid})
            active.result()
        lost = self.step(sid, "write", {"path": "/tmp/mock-write-drop", "content": "committed"}, "ERROR_BACKEND_ERROR")
        check(lost["operation"]["state"] == "OPERATION_UNKNOWN", "lost write acknowledgement")
        check(json.loads(self.step(sid, "read", {"path": "/tmp/mock-write-drop"})["result_json"])["content"] == "committed", "read back write")
        count_before = sum(m == "POST" and p == "/sandboxes" for m, p in self.stats()["calls"])
        lost_create = self.call("CreateSandbox", {"backend": "mock", "template": "mock-create-drop",
                                "meta": {"request_id": "lost-create"}}, "ERROR_BACKEND_ERROR")
        check(lost_create["operation"]["state"] == "OPERATION_UNKNOWN", "lost create acknowledgement")
        self.call("CreateSandbox", {"backend": "mock", "template": "mock-create-drop",
                  "meta": {"request_id": "lost-create"}}, "ERROR_DUPLICATE_REQUEST")
        count_after = sum(m == "POST" and p == "/sandboxes" for m, p in self.stats()["calls"])
        check(count_after-count_before == 1, "never replay uncertain create")
        uncertain = self.call("DeleteSandbox", {"create_request_id": "lost-create"}, "ERROR_STATUS_UNAVAILABLE")
        check(not uncertain["cleanup_confirmed"], "missing ID cannot confirm cleanup")
        self.call("DeleteSandbox", {"create_request_id": "not-recorded"}, "ERROR_NOT_FOUND")
        self.call("DeleteSandbox", {"create_request_id": "write-once"}, "ERROR_INVALID_ARGUMENT")

        health_before = sum(p.endswith('/health') for _, p in self.stats()["calls"])
        unready = self.call("CreateSandbox", {"backend": "mock", "template": "mock-unready", "timeout_ms": 1000}, "ERROR_OPERATION_TIMEOUT")
        check(unready["sandbox"]["sandbox_id"] and not unready["success"], "retain cleanup handle on readiness failure")
        check("envd readiness deadline exceeded" in unready["error"]["message"] and
              "last_http_status=503" in unready["error"]["message"], "preserve readiness failure diagnostic")
        health_count = sum(p.endswith('/health') for _, p in self.stats()["calls"]) - health_before
        check(1 <= health_count <= 4, "bound unhealthy sandbox polling load")
        cleaned = self.call("DeleteSandbox", {"create_request_id": unready["meta"]["request_id"]})
        check(cleaned["cleanup_confirmed"], "cleanup failed readiness via create ID")
        dropped_health = self.call("CreateSandbox", {"backend": "mock", "template": "mock-health-drop",
                                                    "timeout_ms": 1000}, "ERROR_OPERATION_TIMEOUT")
        check("last_http_status=0" in dropped_health["error"]["message"] and
              dropped_health["error"]["backend_code"].startswith("CURL_"),
              "distinguish HTTP rejection from health transport failure")
        check(self.call("DeleteSandbox", {"create_request_id": dropped_health["meta"]["request_id"]})
              ["cleanup_confirmed"], "cleanup after health transport failure")
        create_count = sum(m == "POST" and p == "/sandboxes" for m, p in self.stats()["calls"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            active_create = executor.submit(self.call, "CreateSandbox",
                {"backend": "mock", "template": "mock-unready", "timeout_ms": 1000,
                 "meta": {"request_id": "active-create"}}, "ERROR_OPERATION_TIMEOUT")
            deadline = time.monotonic()+5
            while sum(m == "POST" and p == "/sandboxes" for m, p in self.stats()["calls"]) <= create_count:
                check(time.monotonic() < deadline, "create did not reach backend")
                time.sleep(0.01)
            refused = self.call("DeleteSandbox", {"create_request_id": "active-create"}, "ERROR_INVALID_STATE")
            check(not refused["cleanup_confirmed"], "active create cannot be declared cleaned")
            active_create.result()
        check(self.call("DeleteSandbox", {"create_request_id": "active-create"})["cleanup_confirmed"], "cleanup after create finished")
        legacy, _ = self.create("mock-legacy")
        self.step(legacy, "write", {"path": "/tmp/legacy", "content": "multipart"})
        check(json.loads(self.step(legacy, "read", {"path": "/tmp/legacy"})["result_json"])["content"] == "multipart", "legacy upload")
        self.call("DeleteSandbox", {"sandbox_id": legacy})
        stuck, _ = self.create("mock-delete-stuck")
        stuck_result = self.call("DeleteSandbox", {"sandbox_id": stuck, "timeout_ms": 150}, "ERROR_OPERATION_TIMEOUT")
        check(not stuck_result["cleanup_confirmed"], "204 without absence does not confirm cleanup")

        ids = [self.create()[0] for _ in range(3)]
        starts = self.starts()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.step, x, "bash", {"command": "mock:sleep:1500"}) for x in ids[:2]]
            deadline = time.monotonic()+5
            while self.starts() < starts+2:
                check(time.monotonic() < deadline, "workers did not become busy")
                time.sleep(0.01)
            queued = self.step(ids[2], "read", {"path": "/tmp/a"}, "ERROR_OPERATION_TIMEOUT", timeout_ms=100)
            check(queued["operation"]["dispatch_state"] == "DISPATCH_NOT_STARTED", "queued expiry cannot reach backend")
            check(int(queued["meta"]["proxy_elapsed"]["value_ms"]) < 1000, "queued expiry delivered too late")
            for job in jobs:
                job.result()
        for x in ids:
            self.call("DeleteSandbox", {"sandbox_id": x})
        check(self.call("DeleteSandbox", {"create_request_id": "create-primary"})["cleanup_confirmed"], "cleanup by create ID")
        check(self.call("DeleteSandbox", {"sandbox_id": sid})["cleanup_confirmed"], "repeat delete")
        check(self.call("GetStatus", {"sandbox_id": sid})["sandbox"]["state"] == "SANDBOX_DELETED", "deleted tombstone")
        self.step(sid, "bash", {"command": "printf hello"}, "ERROR_INVALID_STATE")


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
              "envd_url_pattern": f"http://127.0.0.1:{mock_port}/envd/{{sandbox_id}}",
              "templates": {t: {"backend_template_id": t, "cpu_count": 1, "memory_mib": 512} for t in TEMPLATES}}}}
    config_path = artifacts / "backends.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    processes, logs = [], []
    suite = Suite(args.client.resolve(), proxy_port, mock_port, artifacts)
    report = {"passed": False, "artifacts": str(artifacts.resolve())}
    env = dict(os.environ, MOCK_E2B_API_KEY="mock-key", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")

    def start(command, name, port):
        log = (artifacts / (name + ".log")).open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        processes.append(process)
        wait_port(port, process)
        return process

    try:
        start([sys.executable, str(ROOT / "tests/mock_backend/server.py"), "--port", str(mock_port)], "mock", mock_port)
        start([str(args.proxy.resolve()), f"--listen=127.0.0.1:{proxy_port}", f"--backends={config_path}",
               "--control_threads=2", "--step_threads=2"], "proxy", proxy_port)
        suite.run()
        # Independent instance: record exhaustion must reject before creating any sandbox.
        capacity_port = unused_port()
        start([str(args.proxy.resolve()), f"--listen=127.0.0.1:{capacity_port}", f"--backends={config_path}",
               "--max_records=1"], "capacity", capacity_port)
        limited = Suite(args.client.resolve(), capacity_port, mock_port, artifacts)
        limited.sequence = 1000
        first = limited.call("CreateSandbox", {"backend": "mock", "template": "base", "timeout_ms": 0}, "ERROR_INVALID_ARGUMENT")
        limited.call("CreateSandbox", {"backend": "mock", "template": "base"}, "ERROR_STORAGE_UNAVAILABLE")
        limited.operation(first["meta"]["request_id"])
        suite.records.extend(limited.records)
        smoke = subprocess.run([sys.executable, str(ROOT / "scripts/e2b_smoke.py"),
                                "--proxy", str(args.proxy.resolve()), "--client", str(args.client.resolve()),
                                "--backends", str(config_path.resolve()), "--backend", "mock",
                                "--template", "base", "--cpu", "1", "--memory-mib", "512", "--allow-create"],
                               cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        (artifacts / "smoke.log").write_text(smoke.stdout + smoke.stderr, encoding="utf-8")
        check(smoke.returncode == 0, "smoke driver failed against Mock: " + smoke.stdout + smoke.stderr)
        check("Cleanup confirmed: true" in smoke.stdout, "smoke driver prints cleanup result")
        check(all(f"RunStep[{tool}]: ERROR_OK" in smoke.stdout for tool in ("bash", "write", "read", "edit")),
              "smoke driver identifies each tool")
        report["smoke_driver_passed"] = True
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
