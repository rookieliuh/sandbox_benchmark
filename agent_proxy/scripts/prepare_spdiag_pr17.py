"""Build the reviewed SpDiag PR in a fresh project-local prefix, without patching it."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from prepare_spdiag import digest

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://gitcode.com/openeuler/spdiag.git"
REVISION = "dd134e4fbb583ecd5be45ac3cde3f1cf5cc66de6"
PR_REF = "refs/merge-requests/17/head"
OPTIONS = {
    "CMAKE_BUILD_TYPE": "RelWithDebInfo",
    "CMAKE_INSTALL_LIBDIR": "lib64",
    "CMAKE_INSTALL_BINDIR": "bin",
    "CMAKE_INSTALL_INCLUDEDIR": "include",
    "CMAKE_INSTALL_SYSCONFDIR": "etc",
    "BUILD_TESTS": "OFF",
    "BUILD_EXAMPLES": "OFF",
    "SPDIAG_BUILD_SHARED": "ON",
    "ENABLE_PERCENTILE": "ON",
    "ENABLE_PERFLOG": "ON",
    "ENABLE_MEMPOINT": "OFF",
    "ENABLE_OB_MEMORY": "OFF",
    "ENABLE_OB_CACHE": "OFF",
    "SPDIAG_LOG_TO_FILE": "OFF",
}


def prepare():
    deps = ROOT / ".deps"
    if not deps.resolve().is_relative_to(ROOT):
        raise ValueError(".deps must stay inside agent_proxy")
    deps.mkdir(exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="spdiag-pr17-", dir=deps))
    source, build, prefix = session / "source", session / "build", session / "install"
    env = dict(os.environ)
    # Do not let a packaging shell redirect installation outside the private prefix.
    env.pop("DESTDIR", None)
    print(f"Private PR build: {session}", flush=True)
    print(f"SpDiag PR #17 expected commit: {REVISION}", flush=True)

    def run(label, command, timeout=120):
        print(f"SpDiag PR build: {label}", flush=True)
        log = session / (label + ".log")
        with log.open("w", encoding="utf-8") as output:
            child = subprocess.Popen(command, cwd=session, env=env, stdout=output,
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
        text = log.read_text(encoding="utf-8", errors="replace")
        if code:
            print(text, flush=True)
            raise RuntimeError(f"{label} failed (exit {code}); log: {log}")
        return text.strip()

    run("init", ["git", "init", str(source)])
    run("fetch", ["git", "-C", str(source), "fetch", "--depth", "1", REPOSITORY, PR_REF])
    actual = run("revision", ["git", "-C", str(source), "rev-parse", "FETCH_HEAD"])
    if actual != REVISION:
        raise ValueError(f"PR head changed to {actual}; review it before changing the pinned revision")
    run("checkout", ["git", "-C", str(source), "checkout", "--detach", REVISION])
    run("configure", ["cmake", "-S", str(source), "-B", str(build),
                      f"-DCMAKE_INSTALL_PREFIX={prefix / 'usr'}",
                      *[f"-D{key}={value}" for key, value in OPTIONS.items()]])
    run("build", ["cmake", "--build", str(build), "--parallel", "1"], timeout=900)
    run("install", ["cmake", "--install", str(build)])
    if run("source-status", ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("SpDiag source changed during build; refusing to validate a modified source tree")
    files = {}
    for path in prefix.rglob("*"):
        if path.is_file():
            if not path.resolve().is_relative_to(prefix):
                raise ValueError("installed dependency escapes private prefix")
            files[path.relative_to(prefix).as_posix()] = digest(path)
    for required in ("usr/lib64/libspdiag.so.1", "usr/bin/spdiag",
                     "usr/lib64/cmake/SpDiag/SpDiagConfig.cmake"):
        if required not in files:
            raise ValueError(f"missing built dependency: {required}")
    manifest = {"source": {"repository": REPOSITORY, "commit": REVISION,
                           "pr": 17, "cmake_options": OPTIONS}, "files": files}
    (prefix / "agent-proxy-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Prepared PR-only SDK: {prefix}", flush=True)
    return prefix, manifest
