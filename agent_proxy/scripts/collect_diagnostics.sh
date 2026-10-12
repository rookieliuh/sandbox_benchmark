#!/usr/bin/env bash
# Collect only build/test diagnostics, never environment values or backend credentials.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p validation-logs test-results
{
  date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
  uname -a
  if [[ -r /etc/os-release ]]; then cat /etc/os-release; fi
  git rev-parse HEAD || true
  git status --short || true
  for tool in g++ cmake protoc python3 pkg-config; do
    if command -v "$tool" >/dev/null 2>&1; then "$tool" --version; else printf 'Missing tool: %s\n' "$tool"; fi
  done
  if command -v pkg-config >/dev/null 2>&1; then
    for package in libcurl protobuf openssl; do pkg-config --modversion "$package" || true; done
  fi
  if command -v dpkg-query >/dev/null 2>&1; then
    dpkg-query -W -f='${Package}\t${Version}\n' \
      build-essential cmake git pkg-config python3 libssl-dev libgflags-dev \
      libprotobuf-dev libprotoc-dev protobuf-compiler libleveldb-dev zlib1g-dev \
      libcurl4-openssl-dev nlohmann-json3-dev || true
  fi
  if [[ -d .deps/brpc-src/.git ]]; then git -C .deps/brpc-src rev-parse HEAD; fi
} > validation-logs/environment.txt 2>&1
FILES=(validation-logs)
for item in \
  build/test-results build/Testing/Temporary build/CMakeCache.txt \
  build/CMakeFiles/CMakeConfigureLog.yaml build/CMakeFiles/CMakeOutput.log build/CMakeFiles/CMakeError.log \
  .deps/brpc-build/CMakeCache.txt .deps/brpc-build/CMakeFiles/CMakeConfigureLog.yaml \
  .deps/brpc-build/CMakeFiles/CMakeOutput.log .deps/brpc-build/CMakeFiles/CMakeError.log; do
  if [[ -e "$item" ]]; then FILES+=("$item"); fi
done
ARCHIVE="test-results/agent-proxy-diagnostics-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
tar -czf "$ARCHIVE" -- "${FILES[@]}"
printf 'Diagnostics archive: %s/%s\n' "$ROOT" "$ARCHIVE"
