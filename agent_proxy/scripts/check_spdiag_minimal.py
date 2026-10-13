#!/usr/bin/env python3
"""Build/run only a two-sleep SDK probe against the already prepared RPM pair."""
import csv
import json
import math
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
POINTS = {"Sleep1s": 1000, "Sleep5s": 5000}


def read_samples(output):
    samples = {}
    for line in output.splitlines():
        if not line.startswith("SAMPLE\t"):
            continue
        _, point, requested, elapsed = line.split("\t")
        if point not in POINTS or point in samples or int(requested) != POINTS[point]:
            raise ValueError(f"unexpected/duplicate sample: {line}")
        samples[point] = int(elapsed)
        if samples[point] < POINTS[point] * 1_000_000:
            raise ValueError(f"reference sleep was too short: {line}")
    if samples.keys() != POINTS.keys():
        raise ValueError("missing independent steady_clock samples")
    return samples


def compare(csv_path, samples):
    results = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            row = {k.strip(): v.strip() for k, v in raw.items()}
            point = row["Point"]
            if (row["Program"] != "spdiag-minimal" or point not in POINTS or point in results or
                    row["Module"] != "main.cc::measure" or int(row["Lvl"]) != 1):
                raise ValueError(f"unexpected point in private SHM: {row}")
            counts = {k: int(row[k]) for k in ("Ticks", "Good", "Bad", "Not")}
            durations = {k: float(row[k]) for k in ("Total(ns)", "Avg(ns)", "Min(ns)", "Max(ns)")}
            expected = samples[point]
            # One sample per point. Compare against actual steady time, not scheduled sleep.
            # A 10% tolerance handles overhead/display precision but cannot hide a 5s wrap.
            timing_ok = all(math.isfinite(v) and abs(v - expected) <= expected * 0.1
                            for v in durations.values())
            counts_ok = counts == {"Ticks": 1, "Good": 1, "Bad": 0, "Not": 0}
            results[point] = {"steady_ns": expected, "spdiag_ns": durations, "counts": counts,
                              "passed": timing_ok and counts_ok}
    if results.keys() != POINTS.keys():
        raise ValueError("missing SpDiag points")
    return results


def main():
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        raise RuntimeError("requires Linux aarch64 and the already prepared RPM pair")
    os.umask(0o077)
    prefix = ROOT / ".deps/spdiag"
    manifest = json.loads((prefix / "agent-proxy-manifest.json").read_text(encoding="utf-8"))
    if manifest["packages"] != PACKAGES:
        raise ValueError("unexpected RPM manifest; use the existing prepare_spdiag.py workflow")
    for name, expected in manifest["files"].items():
        path = prefix / name
        if not path.resolve().is_relative_to(prefix.resolve()) or digest(path) != expected:
            raise ValueError(f"prepared dependency changed: {name}")
    results = ROOT / "test-results"
    if not results.resolve().is_relative_to(ROOT):
        raise ValueError("test-results must stay inside agent_proxy")
    results.mkdir(exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="spdiag-minimal-", dir=results))
    name = f"/spdiag_minimal_{os.getuid()}_{uuid.uuid4().hex}"
    shm_path = Path("/dev/shm") / name[1:]
    if shm_path.exists():
        raise FileExistsError(shm_path)
    # This RPM's CLI reads ./spdiag.conf; --shm/--config are not supported.
    (session / "spdiag.conf").write_text(
        f"[shm]\nname = {name}\n[general]\ndata_dir = {session / 'data'}\n"
        "history_size = 2\ndump_interval_ms = 1000\nlevel_gate = 1\nnum_cores = 1\n"
        "[perf]\nmaxSlots = 16\nmaxPrograms = 1\n", encoding="utf-8")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(prefix / "usr/lib64") + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    report = {"passed": False, "cleanup_confirmed": False, "shm_name": name,
              "session": str(session), "packages": PACKAGES}
    cli = str(prefix / "usr/bin/spdiag")
    attempted_start = False

    def run(label, command, timeout=30):
        # Never signal by process name: only this subprocess and its own descendants.
        log = session / (label + ".log")
        with log.open("w", encoding="utf-8") as stream:
            child = subprocess.Popen(command, cwd=session, env=env, stdout=stream,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                raise
        output = log.read_text(encoding="utf-8", errors="replace")
        if code:
            print(output, flush=True)
            raise RuntimeError(f"{label} failed (exit {code}); log: {log}")
        return output

    print(f"Private SDK-only session: {session}", flush=True)
    for package, sha in PACKAGES.items():
        print(f"RPM SHA256 {package}: {sha}", flush=True)
    try:
        build = session / "build"
        print("Building only spdiag-minimal (parallel=1)...", flush=True)
        run("configure", ["cmake", "-S", str(ROOT / "tests/spdiag_minimal"), "-B", str(build),
                          "-DCMAKE_BUILD_TYPE=Release",
                          f"-DSpDiag_DIR={prefix / 'usr/lib64/cmake/SpDiag'}"], 120)
        run("build", ["cmake", "--build", str(build), "--parallel", "1"], 180)
        attempted_start = True
        output = run("start", [cli, "start"])
        if f"SHM: {name}" not in output or not shm_path.is_file():
            raise RuntimeError("CLI did not confirm the dedicated SHM name")
        print("Measuring one 1-second sleep and one 5-second sleep...", flush=True)
        output = run("probe", [str(build / "spdiag-minimal"), name])
        print(output, flush=True)
        mappings = [Path(line.split("\t", 1)[1]).resolve() for line in output.splitlines()
                    if line.startswith("SDK_MAPPING\t")]
        if (len(mappings) != 1 or not mappings[0].is_relative_to((prefix / "usr/lib64").resolve()) or
                digest(mappings[0]) != manifest["files"]["usr/lib64/libspdiag.so.1"]):
            raise ValueError("probe did not load the verified RPM shared library")
        report["loaded_sdk"] = str(mappings[0])
        report["loaded_sdk_sha256"] = digest(mappings[0])
        samples = read_samples(output)
        output = run("show", [cli, "show", "--csv", str(session / "csv")])
        print(output, flush=True)
        files = list((session / "csv").glob("spdiag_perfpoint_summary_*.csv"))
        if len(files) != 1:
            raise ValueError("expected exactly one summary CSV")
        report["measurements"] = compare(files[0], samples)
        report["passed"] = all(item["passed"] for item in report["measurements"].values())
        for point, item in report["measurements"].items():
            print(f"{point}: steady={item['steady_ns'] / 1e6:.3f} ms; "
                  f"SpDiag={item['spdiag_ns']['Total(ns)'] / 1e6:.3f} ms; "
                  f"counts={item['counts']}; {'PASS' if item['passed'] else 'FAIL'}", flush=True)
    except BaseException:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        try:
            if attempted_start and shm_path.exists():
                run("stop", [cli, "stop"])  # Only the random name in this private cwd/config.
            report["cleanup_confirmed"] = not shm_path.exists()
            if not report["cleanup_confirmed"]:
                raise RuntimeError(f"private SHM remains: {shm_path}")
        except BaseException:
            report["cleanup_error"] = traceback.format_exc()
            report["passed"] = False
            print(report["cleanup_error"], file=sys.stderr, flush=True)
        (session / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: SDK-only 1s/5s; "
          f"cleanup_confirmed={report['cleanup_confirmed']}; report={session / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
