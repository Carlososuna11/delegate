"""delegate-run: a supervised, verifiable wrapper around `delegate`.

Design: docs/specs/2026-10-10-delegate-v2-design.md, "Fase B". Python 3.12
standard library only. `delegate` stays the exec primitive; this package never
reads, prints or forwards a provider key.

Module ownership (each one is implemented independently against the contract
in `contract.py`; do not change the contract without changing the spec):
  contract.py  shared types, statuses and state-dir helpers (complete)
  request.py   parse and validate a request from stdin
  runner.py    the run lifecycle, slots and cancellation
  report.py    status, list, cost and gc over the run directories
"""
