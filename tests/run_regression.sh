#!/bin/bash
# Local helper to run the tagui.run() regression suite in isolated mode.
# Uses python -I so PYTHONPATH / site-packages cannot shadow the repo copy,
# and runs from this directory so the fixture path with spaces is found.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Running tagui.run() regression suite..."
echo "repo:  $(cd "$HERE/.." && pwd)"
echo "python: $(python3 --version 2>&1)"
echo

python3 -I "$HERE/run_regression.py"
