#!/usr/bin/env python3
"""Collect SpDiag in a private session using Mock RPCs or an explicitly selected real E2B."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import getpass
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from prepare_spdiag import PACKAGES, digest

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("CreateSandbox", "RunStep", "PauseSandbox", "ResumeSandbox", "DeleteSandbox", "GetStatus")


def select_e2b_target(path, backend=None, template=None):
    """Reuse a reviewed config; never invent routes, credentials, or template resources."""
    config = json.loads(path.read_text(encoding="utf-8"))
    backends = config["backends"]
    if backend is None:
        if len(backends) != 1:
            raise ValueError("config has multiple backends; specify --backend")
        backend = next(iter(backends))
    selected = backends[backend]
    if selected.get("type") != "e2b":
        raise ValueError("selected backend must have type=e2b")
    templates = selected["templates"]
    if template is None:
        if len(templates) != 1:
            raise ValueError("config has multiple templates; specify --template")
        template = next(iter(templates))
    spec = templates[template]
    cpu, memory = int(spec["cpu_count"]), int(spec["memory_mib"])
    if min(cpu, memory) <= 0:
        raise ValueError("template CPU/memory must be positive")
    return backend, template, cpu, memory, selected["api_key_env"]


def verify_counts(csv_path, cases, *, success_only=False):
    expected = {method: Counter() for method in METHODS}
    for case in cases:
        expected[case["method"]]["Good" if case["status"] == "ERROR_OK" else "Bad"] += 1
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = [{k.strip(): v.strip() for k, v in row.items()} for row in csv.DictReader(stream)]
    actual = {}
    for row in rows:
        if row["Program"] != "agent-proxy":
            raise AssertionError(f"unexpected program in private SHM: {row['Program']}")
        method = row["Point"]
        if method not in expected or method in actual:
            raise AssertionError(f"unexpected/duplicate point: {method}")
        if row["Module"] != "service.cc::" + method or int(row["Lvl"]) != 1:
            raise AssertionError(f"wrong module/level: {row}")
        counts = {key: int(row[key]) for key in ("Good", "Bad", "Not", "Ticks")}
        if (counts["Good"] != expected[method]["Good"] or
                counts["Bad"] != expected[method]["Bad"] or counts["Not"] != 0 or
                counts["Ticks"] != sum(expected[method].values())):
            raise AssertionError(f"{method}: expected={dict(expected[method])}, actual={counts}")
        if float(row["Total(ns)"]) <= 0 or float(row["Max(ns)"]) <= 0:
            raise AssertionError(f"missing measured duration: {method}")
        actual[method] = counts
    if set(actual) != set(METHODS):
        raise AssertionError(f"missing points: {set(METHODS) - set(actual)}")
    if success_only:
        if len(cases) != 6 or any(v["Good"] != 1 or v["Bad"] != 0 for v in expected.values()):
            raise AssertionError("success-only workload must have exactly one success per RPC")
    elif not all(expected[m]["Good"] for m in METHODS) or not any(v["Bad"] for v in expected.values()):
        raise AssertionError("workload must exercise all six successes and explicit failures")
    return actual


def verify_durations(csv_path, cases, *, require_long_sample=True):
    # Cross-check independent steady_clock timings, allowing rounding and SDK display
    # precision. The deliberate five-second RunStep must not wrap to < 1 second.
    totals = Counter()
    maxima = Counter()
    for case in cases:
        method, ms = case["method"], case["proxy_elapsed_ms"]
        totals[method] += ms
        maxima[method] = max(maxima[method], ms)
    if require_long_sample and maxima["RunStep"] < 4900:
        raise AssertionError("missing five-second RunStep timing check")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            method = row["Point"].strip()
            for column, expected in (("Total(ns)", totals[method]), ("Max(ns)", maxima[method])):
                if float(row[column]) < expected * 1_000_000 * 0.8:
                    raise AssertionError(
                        f"{method} {column}={row[column].strip()} is below proxy timing "
                        f"({expected} ms). Counters alone are not sufficient: check the SpDiag "
                        "SDK tick-to-ns conversion for long-interval overflow. Do not use these "
                        "durations for benchmark conclusions.")


def run_e2e(command, session, env, *, real_e2b=False):
    # Own process group only. On interruption/timeout, let Python's finally clean its
    # children first, then kill this group if necessary; never search by process name.
    with (session / "e2e.log").open("w", encoding="utf-8") as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                 cwd=ROOT, env=env, start_new_session=True)
        try:
            if real_e2b:
                deadline = time.monotonic() + 600
                # Tail the private log while the real sandbox operations run.
                with (session / "e2e.log").open(encoding="utf-8", errors="replace") as reader:
                    while child.poll() is None:
                        print(reader.read(), end="", flush=True)
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(command, 600)
                        try:
                            child.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass
                    print(reader.read(), end="", flush=True)
                code = child.returncode
            else:
                code = child.wait(timeout=180)
            if code and not real_e2b:
                print((session / "e2e.log").read_text(encoding="utf-8", errors="replace"), flush=True)
                raise RuntimeError(f"E2E failed (exit {code}); see {session / 'e2e.log'}")
            return code
        except BaseException:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=180 if real_e2b else 45)
                except subprocess.TimeoutExpired:
                    pass
            # Also remove any descendants left after an abnormal driver exit.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build-spdiag")
    parser.add_argument("--success-only", action="store_true",
                        help="Collect one success per RPC; omit fault cases and the five-second probe")
    parser.add_argument("--e2b-config", type=Path,
                        help="Opt in to a real E2B basic lifecycle using this existing local config")
    parser.add_argument("--backend", help="Required only if the real config contains multiple backends")
    parser.add_argument("--template", help="Required only if the selected backend has multiple templates")
    parser.add_argument("--allow-create", action="store_true",
                        help="Authorize one real sandbox, command, pause, resume, and deletion")
    args = parser.parse_args()
    real_e2b = args.e2b_config is not None
    if real_e2b and not args.allow_create:
        parser.error("--e2b-config requires --allow-create")
    if not real_e2b and (args.allow_create or args.backend or args.template):
        parser.error("real E2B options require --e2b-config")
    if real_e2b and args.success_only:
        parser.error("--e2b-config already selects the basic lifecycle; omit --success-only")
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        parser.error("real SpDiag validation requires Linux aarch64")
    if real_e2b:
        args.e2b_config = args.e2b_config.resolve()
        backend, template, cpu, memory, key_env = select_e2b_target(
            args.e2b_config, args.backend, args.template)
    os.umask(0o077)
    build = args.build_dir.resolve()
    if not build.is_relative_to(ROOT):
        parser.error("build-dir must stay inside agent_proxy")
    prefix = ROOT / ".deps/spdiag"
    manifest = json.loads((prefix / "agent-proxy-manifest.json").read_text(encoding="utf-8"))
    if manifest["packages"] != PACKAGES:
        raise ValueError("unexpected dependency manifest")
    for name, expected in manifest["files"].items():
        path = prefix / name
        if not path.resolve().is_relative_to(prefix.resolve()) or digest(path) != expected:
            raise ValueError(f"prepared dependency changed: {name}")
    cli = prefix / "usr/bin/spdiag"
    for path in (cli, build / "agent-proxy", build / "agent-proxy-client"):
        if not path.is_file():
            raise FileNotFoundError(path)
    results = ROOT / "test-results"
    if not results.resolve().is_relative_to(ROOT):
        raise ValueError("test-results must stay inside agent_proxy")
    results.mkdir(exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="spdiag-", dir=results))
    session.chmod(0o700)
    name = f"/agent_proxy_{os.getuid()}_{uuid.uuid4().hex}"
    shm_path = Path("/dev/shm") / name[1:]
    if shm_path.exists():
        raise FileExistsError(shm_path)
    # The reviewed CLI loads ./spdiag.conf; --config and --shm are disabled in this RPM.
    # Its working directory is fresh/private and has no config/spdiag.conf to shadow this.
    (session / "spdiag.conf").write_text(
        f"[shm]\nname = {name}\n[general]\ndata_dir = {session / 'data'}\n"
        "history_size = 2\ndump_interval_ms = 1000\nlevel_gate = 1\nnum_cores = 1\n"
        "[perf]\nmaxSlots = 16\nmaxPrograms = 1\n", encoding="utf-8")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(prefix / "usr/lib64") + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    if real_e2b:
        key = env.get(key_env) or getpass.getpass(f"{key_env} (hidden input, do not paste into chat): ")
        if not key or "\n" in key or "\r" in key:
            raise ValueError("API key is empty or malformed")
        env[key_env] = key
    report = {"passed": False, "shm_name": name, "session": str(session), "cleanup_confirmed": False,
              "success_only": args.success_only, "real_e2b": real_e2b}
    if real_e2b:
        report["sandbox_cleanup_confirmed"] = False
    attempted_start = False

    def cli_run(label, *arguments):
        proc = subprocess.run([str(cli), *arguments], cwd=session, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
        (session / (label + ".log")).write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode:
            raise RuntimeError(f"SpDiag {label} exited {proc.returncode}: {proc.stdout}{proc.stderr}")
        return proc.stdout

    print(f"Private SpDiag session: {session}", flush=True)
    if real_e2b:
        print(f"Mode: REAL E2B basic lifecycle; backend={backend}, template={template}, "
              f"cpu={cpu}, memory_mib={memory}, ttl=300s. One new sandbox; no file-tool tests.\n"
              "SpDiag durations are raw/unvalidated due to the known RPM issue; "
              "proxy_elapsed_ms is the independent timing reference.", flush=True)
    elif args.success_only:
        print("Mode: six successful Mock RPCs, one sample each; no injected faults or sleeps.\n"
              "These samples do not validate the known long-interval RPM timing issue.", flush=True)
    try:
        # Empty/default SHM is never passed to either the CLI or the proxy.
        attempted_start = True
        output = cli_run("start", "start")
        if f"SHM: {name}" not in output or not shm_path.is_file():
            raise RuntimeError("CLI did not confirm the dedicated SHM name")
        driver = ROOT / ("scripts/e2b_smoke.py" if real_e2b else "tests/integration/e2e.py")
        command = [sys.executable, str(driver),
                   "--proxy", str(build / "agent-proxy"), "--client", str(build / "agent-proxy-client"),
                   "--report", str(session / "e2e.json"), "--spdiag-shm-name", name]
        if real_e2b:
            command.extend(["--backends", str(args.e2b_config), "--backend", backend,
                            "--template", template, "--cpu", str(cpu), "--memory-mib", str(memory),
                            "--ttl", "300", "--basic-lifecycle", "--allow-create"])
        elif args.success_only:
            command.append("--success-only")
        run_e2e(command, session, env, real_e2b=real_e2b)
        e2e = json.loads((session / "e2e.json").read_text(encoding="utf-8"))
        if real_e2b:
            report["sandbox_cleanup_confirmed"] = e2e["sandbox_cleanup_confirmed"]
            report["create_request_id"] = e2e["create_request_id"]
            report["rpc_cases"] = e2e["perf_cases"]
        report["process_exits"] = e2e["process_exits"]
        # e2e waits for graceful proxy shutdown; SDK Shutdown flushes the final accumulators.
        output = cli_run("show", "show", "--csv", str(session / "csv"))
        print(output)
        csv_files = list((session / "csv").glob("spdiag_perfpoint_summary_*.csv"))
        if len(csv_files) != 1:
            raise RuntimeError("expected one SpDiag summary CSV")
        if not e2e["passed"]:
            raise AssertionError("E2E report did not pass")
        print(f"Proxy shutdown: {report['process_exits']['proxy']}", flush=True)
        report["counters"] = verify_counts(csv_files[0], e2e["perf_cases"],
                                          success_only=args.success_only or real_e2b)
        report["counter_check_passed"] = True
        if real_e2b:
            report["duration_check_passed"] = None
            report["duration_check_status"] = "not_run_known_rpm_long_interval_issue"
        else:
            verify_durations(csv_files[0], e2e["perf_cases"], require_long_sample=not args.success_only)
            report["duration_check_passed"] = True
        report["passed"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
    finally:
        try:
            if attempted_start and shm_path.exists():
                cli_run("stop", "stop")  # Same private cwd/config; never stop the default instance.
            report["cleanup_confirmed"] = not shm_path.exists()
            if not report["cleanup_confirmed"]:
                raise RuntimeError(f"private SHM remains: {shm_path}")
        except BaseException:
            report["cleanup_error"] = traceback.format_exc()
            report["passed"] = False
            print(report["cleanup_error"], file=sys.stderr)
        (session / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for method, counts in report.get("counters", {}).items():
        print(f"{method}: good={counts['Good']} bad={counts['Bad']} abandon={counts['Not']}")
    scope = "real E2B basic lifecycle and SpDiag counters (SDK durations unvalidated)" if real_e2b else "SpDiag six-RPC counters and timing"
    if real_e2b:
        print(f"Real sandbox cleanup confirmed: {report['sandbox_cleanup_confirmed']}")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {scope}; "
          f"cleanup_confirmed={report['cleanup_confirmed']}; report={session / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    # SIGTERM should also run the private-session cleanup path.
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
