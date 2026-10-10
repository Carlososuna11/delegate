"""The run lifecycle: slot, worktree, delegate, checks, commit, result."""
from __future__ import annotations

from .contract import Request, Result  # noqa: F401


def execute(req: Request) -> Result:
    raise NotImplementedError


def cancel(run_id: str) -> bool:
    raise NotImplementedError
