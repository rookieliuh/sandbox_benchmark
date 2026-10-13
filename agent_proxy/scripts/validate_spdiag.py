#!/usr/bin/env python3
"""Check real SpDiag counters using loopback Mock RPCs and a private SHM session."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import traceback
import uuid

from prepare_spdiag import PACKAGES, digest

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("CreateSandbox", "RunStep", "PauseSandbox", "ResumeSandbox", "DeleteSandbox", "GetStatus")


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


def run_e2e(command, session, env):
    # Own process group only. On interruption/timeout, let Python's finally clean its
    # children first, then kill this group if necessary; never search by process name.
    with (session / "e2e.log").open("w", encoding="utf-8") as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                 cwd=ROOT, env=env, start_new_session=True)
        try:
            code = child.wait(timeout=180)
            if code:
                raise RuntimeError(f"E2E failed (exit {code}); see {session / 'e2e.log'}")
        except BaseException:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=45)
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
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        parser.error("real SpDiag validation requires Linux aarch64")
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
    report = {"passed": False, "shm_name": name, "session": str(session), "cleanup_confirmed": False,
              "success_only": args.success_only}
    attempted_start = False

    def cli_run(label, *arguments):
        proc = subprocess.run([str(cli), *arguments], cwd=session, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
        (session / (label + ".log")).write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode:
            raise RuntimeError(f"SpDiag {label} exited {proc.returncode}: {proc.stdout}{proc.stderr}")
        return proc.stdout

    print(f"Private SpDiag session: {session}", flush=True)
    if args.success_only:
        print("Mode: six successful Mock RPCs, one sample each; no injected faults or sleeps.\n"
              "These samples do not validate the known long-interval RPM timing issue.", flush=True)
    try:
        # Empty/default SHM is never passed to either the CLI or the proxy.
        attempted_start = True
        output = cli_run("start", "start")
        if f"SHM: {name}" not in output or not shm_path.is_file():
            raise RuntimeError("CLI did not confirm the dedicated SHM name")
        command = [sys.executable, str(ROOT / "tests/integration/e2e.py"),
                   "--proxy", str(build / "agent-proxy"), "--client", str(build / "agent-proxy-client"),
                   "--report", str(session / "e2e.json"), "--spdiag-shm-name", name]
        if args.success_only:
            command.append("--success-only")
        run_e2e(command, session, env)
        # e2e waits for graceful proxy shutdown; SDK Shutdown flushes the final accumulators.
        output = cli_run("show", "show", "--csv", str(session / "csv"))
        print(output)
        csv_files = list((session / "csv").glob("spdiag_perfpoint_summary_*.csv"))
        if len(csv_files) != 1:
            raise RuntimeError("expected one SpDiag summary CSV")
        e2e = json.loads((session / "e2e.json").read_text(encoding="utf-8"))
        if not e2e["passed"]:
            raise AssertionError("E2E report did not pass")
        report["counters"] = verify_counts(csv_files[0], e2e["perf_cases"], success_only=args.success_only)
        report["counter_check_passed"] = True
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
    print(f"{'PASS' if report['passed'] else 'FAIL'}: SpDiag six-RPC counters and timing; "
          f"cleanup_confirmed={report['cleanup_confirmed']}; report={session / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    # SIGTERM should also run the private-session cleanup path.
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
