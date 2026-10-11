#!/usr/bin/env bash
# Python suite of delegate-run (Fase B), so test/run.sh covers it too.
# The real-harness smoke inside it is skipped unless DELEGATE_REAL_HARNESS=1.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
python3 -m unittest discover -s test/py -t . 2>&1 | tail -n 30
exit "${PIPESTATUS[0]}"
