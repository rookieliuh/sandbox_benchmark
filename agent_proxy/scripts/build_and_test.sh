#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build}"
BRPC_ROOT="${BRPC_ROOT:-$ROOT/.deps/brpc-install}"
JOBS="${JOBS:-2}"
if [[ "$(uname -s)" != Linux ]]; then
  echo 'This build requires Linux. See README.md.' >&2
  exit 1
fi
for tool in cmake g++ protoc pkg-config python3 git; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing dependency: $tool. Install the packages listed in README.md." >&2
    exit 1
  fi
done
if [[ ! -f "$BRPC_ROOT/include/brpc/server.h" ]]; then
  SOURCE="$ROOT/.deps/brpc-src"
  if [[ ! -d "$SOURCE" ]]; then
    git clone --depth 1 --branch 1.12.1 https://github.com/apache/brpc.git "$SOURCE"
  fi
  if [[ "$(git -C "$SOURCE" describe --tags --exact-match)" != 1.12.1 ]]; then
    echo 'Unexpected bRPC checkout; require tag 1.12.1.' >&2
    exit 1
  fi
  cmake -S "$SOURCE" -B "$ROOT/.deps/brpc-build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$BRPC_ROOT" \
    -DBUILD_UNIT_TESTS=OFF -DBUILD_BRPC_TOOLS=OFF -DWITH_GLOG=OFF -DWITH_SNAPPY=OFF
  cmake --build "$ROOT/.deps/brpc-build" --parallel "$JOBS"
  cmake --install "$ROOT/.deps/brpc-build"
fi
cmake -S "$ROOT" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBRPC_ROOT="$BRPC_ROOT"
cmake --build "$BUILD_DIR" --parallel "$JOBS"
ctest --test-dir "$BUILD_DIR" --output-on-failure
