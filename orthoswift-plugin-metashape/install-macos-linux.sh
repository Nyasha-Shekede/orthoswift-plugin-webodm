#!/usr/bin/env sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$HERE/orthoswift/install.py"
if [ ! -f "$SCRIPT" ]; then
  SCRIPT="$HERE/install.py"
fi

for PY in python3.12 python3.11 python3.10; do
  if command -v "$PY" >/dev/null 2>&1; then
    exec "$PY" "$SCRIPT"
  fi
done
echo "OrthoSWIFT requires 64-bit Python 3.10, 3.11, or 3.12." >&2
exit 1
