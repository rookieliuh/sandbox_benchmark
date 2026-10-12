#!/usr/bin/env bash
# Source inside a subshell. Never writes shell startup files or system configuration.
AP_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
AP_LOCAL_PREFIX="$AP_PROJECT_DIR/.deps/local"
if [[ -d "$AP_LOCAL_PREFIX" ]]; then
  export PATH="$AP_LOCAL_PREFIX/bin:$PATH"
  export CMAKE_PREFIX_PATH="$AP_LOCAL_PREFIX${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
  export PKG_CONFIG_PATH="$AP_LOCAL_PREFIX/lib/pkgconfig:$AP_LOCAL_PREFIX/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export LD_LIBRARY_PATH="$AP_LOCAL_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
