#!/usr/bin/env bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/orthoswift/install.py"
if [ ! -f "$SCRIPT" ]; then
  SCRIPT="$HERE/install.py"
fi

for PY in python3.12 python3.11 python3.10; do
  if command -v "$PY" >/dev/null 2>&1; then
    "$PY" "$SCRIPT"
    echo ""
    echo "Installation complete. Press any key to close..."
    read -n 1 -s -r
    exit 0
  fi
done
echo "OrthoSWIFT requires 64-bit Python 3.10, 3.11, or 3.12." >&2
read -n 1 -s -r
exit 1
