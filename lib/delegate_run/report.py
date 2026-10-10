"""Read-only views over the run directories, plus gc."""
from __future__ import annotations


def status(run_id: str) -> dict:
    raise NotImplementedError


def list_runs(limit: int = 20) -> list[dict]:
    raise NotImplementedError


def cost(days: int = 7) -> dict:
    raise NotImplementedError


def gc(days: int = 14, dry_run: bool = False) -> list[str]:
    raise NotImplementedError
