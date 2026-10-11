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
import shutil
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


class RunExists(Exception):
    """The request named a run_id that already has a run directory: no new run."""


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
    run_id: str | None = None    # chosen by the caller (remote client); None = generate


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
    # "bwrap" when the child ran inside bubblewrap, "none" when it ran without
    # an OS sandbox (DELEGATE_SANDBOX=off, or auto with bwrap unusable), None
    # when the run ended before that was decided.
    sandbox: str | None = None
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
    # From the harness JSON; None when it did not say. Additive to schema v1.
    num_turns: int | None = None
    harness_seconds: float | None = None  # the harness's own duration_ms / 1000
    v: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["checks"] = [c.__dict__ for c in self.checks]
        # Wall time of the whole run (slot wait, child and checks); derived,
        # so it can never disagree with the two timestamps.
        d["duration_seconds"] = (self.finished_at - self.started_at
                                 if self.started_at is not None
                                 and self.finished_at is not None else None)
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
            json.dump(obj, f, ensure_ascii=True, indent=2)
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


# --- git environment ---------------------------------------------------------
# Shared by the runner and gc: every git call that may meet configuration a
# delegate wrote goes through hardened_git_env(). See runner._hardened_env.

GIT_ENV_KEEP = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
# Replacement refs are ignored: `refs/replace/*` is shared by every worktree,
# so a delegate with bash could make a forbidden edit invisible to the scope
# check. GIT_OPTIONAL_LOCKS=0 keeps `git status` from rewriting the user's
# index while the runner only inspects it.
GIT_FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                 "GIT_OPTIONAL_LOCKS": "0"}
GIT_HARDENED_CONFIG = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"),
                       ("diff.external", ""), ("submodule.recurse", "false"),
                       ("core.pager", "cat"), ("log.showSignature", "false"),
                       ("gpg.program", "/bin/false"), ("gpg.ssh.program", "/bin/false"),
                       ("gpg.x509.program", "/bin/false"), ("gc.auto", "0"),
                       ("maintenance.auto", "false"))


def trusted_path() -> str:
    """The caller's PATH with only absolute entries.

    Commands are resolved against this BEFORE any cwd change: a relative or
    empty entry would resolve inside a worktree, where the delegate can plant
    a `git` or a `delegate` that would then run with the caller's environment.
    """
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e.startswith("/")]
    return os.pathsep.join(entries) or "/usr/bin:/bin"


def which(name: str) -> str | None:
    """Absolute path of `name` on trusted_path(), or None."""
    found = shutil.which(name, path=trusted_path())
    return os.path.abspath(found) if found else None


def hardened_git_env(overrides=()) -> dict:
    """An environment BUILT for git, never inherited: no system or global
    config, none of the caller's variables beyond GIT_ENV_KEEP (its tokens
    never reach a program git starts), and GIT_HARDENED_CONFIG plus
    `overrides` as command-line config. GIT_CONFIG_KEY_n takes any key, even
    one `-c` could not express (a subsection with `=`)."""
    env = {k: os.environ[k] for k in GIT_ENV_KEEP if k in os.environ}
    env.update(GIT_FIXED_ENV)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PATH=trusted_path())
    pairs = list(GIT_HARDENED_CONFIG) + list(overrides)
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env
