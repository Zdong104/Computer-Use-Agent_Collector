#!/bin/bash
# CUA Collector V2 — Launch script
# Handles the miniconda libstdc++ version conflict by preloading the system version
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
BUILD_DIR="$SCRIPT_DIR/build"

# Check if built
if [ ! -f "$BUILD_DIR/cua_capture"*.so ]; then
    echo "❌ cua_capture module not built!"
    echo "   Run: cd $BUILD_DIR && cmake .. -DPython3_EXECUTABLE=$VENV_PYTHON && make -j\$(nproc)"
    exit 1
fi

# Preload system libstdc++ to avoid miniconda version conflict
# (miniconda bundles an older libstdc++ missing GLIBCXX_3.4.32)
SYSTEM_LIBSTDCXX="/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
if [ -f "$SYSTEM_LIBSTDCXX" ]; then
    export LD_PRELOAD="$SYSTEM_LIBSTDCXX"
fi

exec "$VENV_PYTHON" "$SCRIPT_DIR/collector_v2.py" "$@"
