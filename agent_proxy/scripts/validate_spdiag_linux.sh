#!/usr/bin/env bash
# Project-local build + Mock/SpDiag validation; no system install or service management.
set -euo pipefail
umask 077
AP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AP_ROOT"
source "$AP_ROOT/scripts/use_local_deps.sh"
if [[ "$(uname -s)" != Linux || "$(uname -m)" != aarch64 ]]; then
  echo 'These reviewed SpDiag RPMs require Linux aarch64.' >&2
  exit 1
fi
# Check output paths, including parent symlinks, before writing.
python3 - "$AP_ROOT" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
for name in (".deps", "build-spdiag", "validation-logs", "test-results"):
    if not (root / name).resolve().is_relative_to(root):
        raise SystemExit(f"Refusing output path outside agent_proxy: {name}")
PY
mkdir -p validation-logs
AP_RUN="$(date -u +%Y%m%dT%H%M%SZ)-$$"
AP_LOG="$AP_ROOT/validation-logs/spdiag-validation-$AP_RUN.log"

ap_validate() {
  local ap_file ap_ldd
  local -a ap_protobuf_args=()
  local ap_brpc="$AP_ROOT/.deps/brpc-install"
  local ap_spdiag="$AP_ROOT/.deps/spdiag/usr"
  for ap_file in "$ap_brpc/include/brpc/server.h" \
    "$ap_spdiag/lib64/cmake/SpDiag/SpDiagConfig.cmake" \
    "$ap_spdiag/lib64/libspdiag.so.1.0.0" "$ap_spdiag/bin/spdiag"; do
    if [[ ! -f "$ap_file" ]]; then
      echo "Missing project dependency: $ap_file" >&2
      echo 'Prepare the RPM pair first; this script never installs system packages.' >&2
      return 1
    fi
  done
  printf 'Architecture: %s\n' "$(uname -m)"
  for ap_file in "$ap_spdiag/lib64/libspdiag.so.1.0.0" "$ap_spdiag/bin/spdiag"; do
    printf 'Checking shared libraries: %s\n' "$ap_file"
    ap_ldd="$(ldd "$ap_file")" || return 1
    printf '%s\n' "$ap_ldd"
    if [[ "$ap_ldd" == *"not found"* ]]; then
      echo 'Unresolved runtime dependency; stop and report this output. No system changes made.' >&2
      return 1
    fi
  done
  if [[ -f "$AP_LOCAL_PREFIX/include/google/protobuf/message.h" && \
        -f "$AP_LOCAL_PREFIX/lib/libprotobuf.so" ]]; then
    ap_protobuf_args=("-DProtobuf_INCLUDE_DIR=$AP_LOCAL_PREFIX/include"
      "-DProtobuf_LIBRARY=$AP_LOCAL_PREFIX/lib/libprotobuf.so"
      "-DProtobuf_PROTOC_EXECUTABLE=$AP_LOCAL_PREFIX/bin/protoc"
      "-DProtobuf_PROTOC_LIBRARY=$AP_LOCAL_PREFIX/lib/libprotoc.so")
  fi
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
  cmake -S "$AP_ROOT" -B "$AP_ROOT/build-spdiag" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_TESTING=ON \
    -DBRPC_ROOT="$ap_brpc" -DSpDiag_DIR="$ap_spdiag/lib64/cmake/SpDiag" \
    "${ap_protobuf_args[@]}"
  cmake --build "$AP_ROOT/build-spdiag" --parallel 1
  ctest --test-dir "$AP_ROOT/build-spdiag" -j 1 --output-on-failure
  python3 -B "$AP_ROOT/scripts/validate_spdiag.py" --build-dir "$AP_ROOT/build-spdiag"
}

printf 'Full log: %s\n' "$AP_LOG"
# Keep errexit inside the pipeline subshell so failures cannot test stale binaries.
set +e
(set -e; ap_validate) 2>&1 | tee "$AP_LOG"
AP_PIPE_STATUS=("${PIPESTATUS[@]}")
set -e
AP_STATUS="${AP_PIPE_STATUS[0]}"
if [[ "$AP_STATUS" -eq 0 && "${AP_PIPE_STATUS[1]}" -ne 0 ]]; then
  AP_STATUS="${AP_PIPE_STATUS[1]}"
fi
printf '%s\n' "$AP_LOG" > "$AP_ROOT/validation-logs/last-spdiag-log.txt"
printf '%s\n' "$AP_STATUS" > "$AP_ROOT/validation-logs/last-spdiag-exit-code.txt"
printf 'SpDiag validation exit: %s\nFull log: %s\n' "$AP_STATUS" "$AP_LOG"
exit "$AP_STATUS"
