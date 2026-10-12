#!/usr/bin/env bash
# Capture build/test output without masking a failed build behind tee.
set -u
set -o pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p validation-logs
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
LOG="validation-logs/build-and-test-$RUN_ID.log"
bash scripts/build_and_test.sh 2>&1 | tee "$LOG"
BUILD_STATUS=${PIPESTATUS[0]}
printf '%s\n' "$BUILD_STATUS" > validation-logs/last-exit-code.txt
printf '%s\n' "$LOG" > validation-logs/last-build-log.txt
bash scripts/collect_diagnostics.sh
COLLECT_STATUS=$?
if [[ $BUILD_STATUS -ne 0 ]]; then
  printf 'Build/test failed (exit %s). See %s and the diagnostics archive.\n' "$BUILD_STATUS" "$LOG" >&2
  exit "$BUILD_STATUS"
fi
if [[ $COLLECT_STATUS -ne 0 ]]; then
  echo 'Build/test passed, but diagnostics collection failed.' >&2
  exit "$COLLECT_STATUS"
fi
echo 'Build and all CTest tests passed.'
