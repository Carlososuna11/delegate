"""Shared contract between request, runner and report. Complete: no TODOs.

Changing anything here changes the result schema callers verify against, so
it goes through the spec first.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

# Terminal statuses of a run, in the spec's order. Only COMPLETED exits 0.
COMPLETED = "completed"          # delegate exited 0 and every check exited 0
CHECKS_FAILED = "checks_failed"  # delegate exited 0, at least one check did not
TASK_FAILED = "task_failed"      # delegate exited non-zero (other than 2)
REJECTED = "rejected"            # delegate exited 2: flags, config or account
TIMED_OUT = "timed_out"          # wall-clock limit hit; TERM, grace, KILL
CANCELLED = "cancelled"          # `delegate-run cancel <run_id>`
BUSY = "busy"                    # no free slot and the request said wait=false
INFRA_FAILED = "infra_failed"    # git, disk, missing binary: not the model's fault
INVALID_RESULT = "invalid_result"  # the child's JSON output could not be parsed
STATUSES = (COMPLETED, CHECKS_FAILED, TASK_FAILED, REJECTED, TIMED_OUT,
            CANCELLED, BUSY, INFRA_FAILED, INVALID_RESULT)
RUNNING = "running"              # written while the run is alive; never final
# Derived by `status`/`list`, never written by the runner: result.json still
# says RUNNING but no process holds the run lock, so the runner died (SIGKILL,
# OOM, reboot) before it could write a terminal status.
ABANDONED = "abandoned"
# Derived: a run directory whose result.json is missing or cannot be parsed
# and whose runner is not alive.
UNREADABLE = "unreadable"

GRANTS = ("edit", "bash")


class RequestError(ValueError):
    """A request that must be refused before anything runs. Message is for humans."""


@dataclass(frozen=True)
class Request:
    provider: str
    repo: str                    # absolute path to a git work tree
    task: str
    base: str = "HEAD"           # resolved to a commit sha by the runner
    account: str | None = None
    grants: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    timeout: int = 1800          # seconds, 60..14400
    checks: tuple[tuple[str, ...], ...] = ()
    scope: tuple[str, ...] = ()  # fnmatch globs, relative to the repo root; empty = anything
    snapshot: bool = False       # allow a dirty tree by committing it to a temp ref
    wait: bool = True            # wait for a free slot instead of returning BUSY


@dataclass
class CheckResult:
    cmd: list[str]
    exit: int | None             # None when the check timed out
    log: str                     # path relative to the run dir
    seconds: float


@dataclass
class Result:
    run_id: str
    status: str
    provider: str
    base: str | None = None
    branch: str | None = None
    worktree: str | None = None
    child_exit: int | None = None
    model: str | None = None
    # The account name the request asked for; None means delegate chose its
    # default or active account. delegate-run never opens a key file, so it
    # cannot report a key fingerprint (see the spec).
    account: str | None = None
    grants: list[str] = field(default_factory=list)
    changed_files: list[dict] = field(default_factory=list)   # {"path": str, "change": "A"|"M"|"D"|"R"|"?"}
    scope_violation: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    usage: dict | None = None    # {"input","output","cache_read","cache_write"}; None = unknown, never 0
    cost_usd_estimate: float | None = None
    report: str | None = None    # the delegate's final text: evidence, not authorization
    error: str | None = None     # set for INFRA_FAILED, REJECTED, INVALID_RESULT
    started_at: float | None = None
    finished_at: float | None = None
    artifacts: str | None = None
    v: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["checks"] = [c.__dict__ for c in self.checks]
        return d


def state_root() -> Path:
    """$DELEGATE_STATE_DIR, else ~/.local/state/delegate. Created 700."""
    root = Path(os.environ.get("DELEGATE_STATE_DIR")
                or Path.home() / ".local" / "state" / "delegate")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def new_run_id(now: float | None = None) -> str:
    """YYYYMMDD-HHMMSS-<6 hex>, sortable by start time."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{stamp}-{secrets.token_hex(3)}"


RUN_ID_RE = re.compile(r"\A[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z")


def run_dir(run_id: str) -> Path:
    # Exact shape only: a run_id names a directory, a worktree and a branch,
    # and arrives from argv in `status`, `cancel` and Fase C's ssh calls.
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id inválido: {run_id!r}")
    return state_root() / "runs" / run_id


def lock_path(rd: Path) -> Path:
    """The run lock: held (LOCK_EX) by the runner for its whole life."""
    return rd / "lock"


def runner_alive(rd: Path) -> bool:
    """True while some process holds the run lock, i.e. the runner is alive.

    flock is released by the kernel when the holder dies, however it dies, so
    this cannot be fooled by a stale pid file.
    """
    try:
        fd = os.open(lock_path(rd), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def write_json_atomic(path: Path, obj) -> None:
    """Write then rename, mode 600: a reader never sees half a file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
