"""The run lifecycle: slot, worktree, delegate, changes, checks, commit, result.

Design: docs/specs/2026-10-10-delegate-v2-design.md, "Ciclo". The runner never
reads, prints or forwards a provider key: `delegate` reads it from its own 600
file. Every git call and every child command is an argv, never a shell string.

Process supervision. The child runs in its own session, but a session is not
a fence: `delegate` runs the harness under GNU `timeout`, which moves itself to
a new process group, and anything below may call setsid(). So the runner makes
itself a child subreaper (prctl), which keeps every orphaned descendant
attached to it, and cleans up by walking the process tree plus the child's
session. The child's leader is left a zombie (waitid WNOWAIT) until the
cleanup is over, so its pid, and with it the session id, cannot be reused
while the runner is still signalling by them.

Cancellation is a marker file, not a signal across processes:
`delegate-run cancel` writes `cancel-requested` in the run dir and the runner,
which polls for it while it waits on a slot, the child or a check, does the
killing itself. Only the runner knows its process tree.

Environment knobs. The defaults are the values the spec fixes; the last one
exists only so the tests do not wait a full minute:

  DELEGATE_SLOTS       concurrent runs (default 2)
  DELEGATE_BIN         the delegate executable (default: `delegate` on PATH)
  DELEGATE_RUN_GRACE   seconds between SIGTERM and SIGKILL (default 15)
  DELEGATE_RUN_SLACK   seconds the runner waits past the request timeout (30)
  DELEGATE_RUN_WALL    overrides timeout + slack entirely (tests only)
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import contract, report as _report
from .contract import CheckResult, Request, Result

DEFAULT_SLOTS = 2
MAX_SLOTS = 64
DEFAULT_GRACE = 15.0
DEFAULT_SLACK = 30.0
CHECK_MAX_TIMEOUT = 1800
POLL = 0.05
CANCEL_MARKER = "cancel-requested"
DELEGATE_TIMEOUT_EXIT = 124      # GNU timeout's code when it had to stop the harness

_PR_SET_CHILD_SUBREAPER = 36

# Variables a check is allowed to see. Checks run code the delegate wrote, so
# they get a BUILT environment, like `delegate` gives its child: the caller's
# tokens (GITHUB_TOKEN, AWS_*, PGPASSWORD...) never reach model-written code.
_CHECK_ENV_KEEP = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
                   "TERM", "TMPDIR", "TZ", "SHELL")

# Git for the runner's own calls: the user's config is trusted, the caller's
# GIT_* environment is not carried (GIT_DIR or GIT_INDEX_FILE from a hook would
# point every call somewhere else), hooks never run (the worktree holds content
# the delegate wrote), and nothing daemonizes behind the runner's back.
_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
               "-c", "maintenance.auto=false", "-c", "core.quotePath=false")


def _trusted_path() -> str:
    """The caller's PATH with only absolute entries.

    Commands are resolved against this BEFORE any cwd change: a relative or
    empty entry would resolve inside the worktree, where the delegate can plant
    a `git` or a `delegate` that the runner would then run with its full
    environment.
    """
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e.startswith("/")]
    return os.pathsep.join(entries) or "/usr/bin:/bin"


def _resolve(name: str) -> str:
    if "/" in name:
        if not os.path.isabs(name):
            raise _Infra(f"{name!r} tiene que ser una ruta absoluta o un nombre en el PATH")
        return name
    found = shutil.which(name, path=_trusted_path())
    if not found:
        raise _Infra(f"no encuentro {name!r} en el PATH")
    return os.path.abspath(found)


class _Infra(Exception):
    """A git or OS failure that is not the model's fault: infra_failed."""


class _Refused(Exception):
    """The request cannot run against this repo as asked: rejected."""


class _Stop(Exception):
    """Cancel marker or a TERM/INT to the runner: stop and report cancelled."""


# --- knobs ------------------------------------------------------------------

def _seconds(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _grace() -> float:
    return _seconds("DELEGATE_RUN_GRACE", DEFAULT_GRACE)


def _slack() -> float:
    return _seconds("DELEGATE_RUN_SLACK", DEFAULT_SLACK)


def _wall(req: Request) -> float:
    override = _seconds("DELEGATE_RUN_WALL", None)
    return override if override is not None else req.timeout + _slack()


def _slot_count() -> int:
    raw = os.environ.get("DELEGATE_SLOTS")
    try:
        count = int(raw) if raw else DEFAULT_SLOTS
    except ValueError:
        count = DEFAULT_SLOTS
    return min(max(1, count), MAX_SLOTS)


# --- stop requests ----------------------------------------------------------

class _Run:
    """Per-run mutable state the waits consult."""

    def __init__(self, rd: Path):
        self.rd = rd
        self.signalled: int | None = None

    def stop_requested(self) -> bool:
        return self.signalled is not None or (self.rd / CANCEL_MARKER).exists()

    def check_stop(self) -> None:
        if self.stop_requested():
            raise _Stop()


def _install_handlers(run: _Run):
    """TERM/INT become a cancel; HUP is ignored. Main thread only.

    HUP is ignored on purpose: Fase C runs this over `ssh -T`, and a dropped
    connection must not take the run down with it; the caller asks
    `delegate-run status` afterwards.
    """
    def on_stop(signum, _frame):
        run.signalled = signum

    saved = {}
    try:
        for sig, handler in ((signal.SIGTERM, on_stop), (signal.SIGINT, on_stop),
                             (signal.SIGHUP, signal.SIG_IGN)):
            saved[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread (in-process tests)
        pass
    return saved


def _restore_handlers(saved) -> None:
    for sig, handler in saved.items():
        signal.signal(sig, handler)


# --- process tree -----------------------------------------------------------

def _become_subreaper() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _stat(pid: int):
    """(ppid, sid, starttime, state) from /proc/<pid>/stat, or None."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = data.rfind(")")
    fields = data[end + 2:].split()
    try:
        # fields[0] is field 3 (state); ppid=4, sid=6, starttime=22.
        return int(fields[1]), int(fields[3]), int(fields[19]), fields[0]
    except (IndexError, ValueError):
        return None


def _snapshot_procs() -> dict[int, tuple]:
    procs = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            info = _stat(int(name))
            if info is not None:
                procs[int(name)] = info
    return procs


def _tree(root: int, sid: int | None) -> dict[int, int]:
    """{pid: starttime} of root, its descendants and its session; never us.

    Descendants are followed from the root and from the runner itself (an
    orphan reparented to the subreaper is the runner's child now). Members of
    the root's session are added even when they escaped the tree, which is the
    case when the runner could not become a subreaper.
    """
    me = os.getpid()
    procs = _snapshot_procs()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _sid, _start, _state) in procs.items():
        children.setdefault(ppid, []).append(pid)
    found: dict[int, int] = {}
    stack = [root] + [p for p in children.get(me, []) if p != root]
    while stack:
        pid = stack.pop()
        if pid in found or pid == me or pid not in procs:
            continue
        found[pid] = procs[pid][2]
        stack.extend(children.get(pid, []))
    if sid is not None and sid > 1:
        for pid, (_ppid, psid, start, _state) in procs.items():
            if psid == sid and pid != me:
                found.setdefault(pid, start)
    return found


def _live(pids: dict[int, int]) -> dict[int, int]:
    """The subset still running as the same process (same starttime, not zombie)."""
    out = {}
    for pid, start in pids.items():
        info = _stat(pid)
        if info is not None and info[2] == start and info[3] != "Z":
            out[pid] = start
    return out


def _signal_all(pids: dict[int, int], sig: int) -> None:
    for pid, start in pids.items():
        info = _stat(pid)
        if info is None or info[2] != start:
            continue  # gone, or the pid now names someone else
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _reap_orphans(pids) -> None:
    """Reap zombies that became our children through the subreaper."""
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _terminate_tree(root: int, sid: int | None, grace: float) -> None:
    """TERM everything under root, wait up to grace, KILL what is left.

    Rescans on every poll so a process forked during shutdown is caught too.
    The root must not have been reaped yet (zombie at most).
    """
    seen: dict[int, int] = {}
    deadline = time.monotonic() + grace
    targets = _live(_tree(root, sid))
    while targets:
        seen.update(targets)
        _signal_all(targets, signal.SIGTERM)
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL)
        _reap_orphans([p for p in seen if p != root])
        targets = _live(_tree(root, sid))
    for _ in range(100):
        targets = _live(_tree(root, sid))
        if not targets:
            break
        seen.update(targets)
        _signal_all(targets, signal.SIGKILL)
        time.sleep(POLL)
        _reap_orphans([p for p in seen if p != root])
    _reap_orphans([p for p in seen if p != root])


def _child_exited(pid: int) -> bool:
    """True once pid has exited; leaves it a zombie so its pid stays reserved."""
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return info is not None


# --- slots ------------------------------------------------------------------

def _acquire_slot(wait: bool, run: _Run):
    """Lock one of state_root()/slots/0..N-1. Returns the open fd or None."""
    slots = contract.state_root() / "slots"
    slots.mkdir(mode=0o700, parents=True, exist_ok=True)
    count = _slot_count()
    while True:
        for index in range(count):
            try:
                fd = os.open(slots / str(index),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except OSError as exc:
                raise _Infra(f"no se pudo abrir el slot {index}: {exc}") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            return fd
        if not wait:
            return None
        run.check_stop()
        time.sleep(POLL * 2)


# --- git --------------------------------------------------------------------

def _git_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Helpers git runs by name (git-lfs, a credential helper) resolve against
    # this PATH with the worktree as cwd: no relative entries.
    env["PATH"] = _trusted_path()
    if extra:
        env.update(extra)
    return env


def _git(cwd, args, check: bool = True, env: dict | None = None,
         text: bool = True) -> subprocess.CompletedProcess:
    cmd = [_resolve("git"), *_GIT_CONFIG, *args]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              text=text, env=env or _git_env())
    except OSError as exc:
        raise _Infra(f"no se pudo ejecutar git: {exc}") from exc
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        detail = err.strip() or f"código {proc.returncode}"
        raise _Infra(f"git {' '.join(args[:2])} falló: {detail}")
    return proc


def _toplevel(repo: str) -> Path:
    if not os.path.isdir(repo):
        raise _Refused(f"'repo' no es un directorio: {repo}")
    proc = _git(repo, ["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise _Refused(f"'repo' no es un árbol de trabajo de git: {repo}")
    return Path(proc.stdout.strip())


def _rev(repo: Path, rev: str) -> str | None:
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                       f"{rev}^{{commit}}"], check=False)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _dirty(repo: Path) -> bool:
    # --untracked-files=all regardless of status.showUntrackedFiles: a tree dirty
    # only by new files is still a tree the caller may think is being used.
    proc = _git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    return bool(proc.stdout)


def _snapshot(repo: Path, head: str, run_id: str, rd: Path) -> str:
    """Commit the working tree as it is, untracked files included, to a ref.

    Uses a throwaway index, so the user's index and working tree are not
    touched. Ignored files stay out, exactly as they would in a commit.
    """
    index = rd / "snapshot.index"
    env = _git_env({"GIT_INDEX_FILE": str(index)})
    try:
        _git(repo, ["read-tree", head], env=env)
        _git(repo, ["add", "-A", "--", "."], env=env)
        tree = _git(repo, ["write-tree"], env=env).stdout.strip()
    finally:
        try:
            index.unlink()
        except FileNotFoundError:
            pass
    commit = _git(repo, ["commit-tree", "--no-gpg-sign", tree, "-p", head,
                         "-m", f"delegate-run snapshot {run_id}"],
                  env=_git_env(_identity("snapshot"))).stdout.strip()
    _git(repo, ["update-ref", f"refs/delegate/snapshot/{run_id}", commit, ""])
    return commit


def _resolve_base(repo: Path, req: Request, run_id: str, rd: Path) -> str:
    """The commit to run from. Refuses a dirty tree unless snapshot is asked.

    The working tree only matters when the base IS the checked-out commit:
    running from another commit never reads it, so its state is irrelevant.
    """
    sha = _rev(repo, req.base)
    if sha is None:
        raise _Refused(f"no se pudo resolver la base {req.base!r} a un commit")
    head = _rev(repo, "HEAD")
    if sha != head:
        if req.snapshot:
            raise _Refused("snapshot=true solo vale con la base en el commit "
                           "actual (HEAD): el snapshot es el estado de tu árbol")
        return sha
    if not _dirty(repo):
        return sha
    if not req.snapshot:
        raise _Refused("el árbol de trabajo tiene cambios sin commit o archivos "
                       "nuevos; usa snapshot=true para correr sobre ese estado, "
                       "o limpia el repo antes de delegar")
    return _snapshot(repo, head, run_id, rd)


def _identity(provider: str) -> dict:
    name = f"delegate {provider}"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": "delegate@localhost",
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": "delegate@localhost"}


# --- scope ------------------------------------------------------------------

def _glob_regex(pattern: str) -> re.Pattern:
    """Path glob: `*` and `?` stay inside one segment, `**` spans segments.

    A pattern with no wildcard names a file or a directory and matches
    everything under it. Unlike fnmatch, `src/*.ts` does not match
    `src/deep/x.ts`: a scope must not be wider than it reads.
    """
    if not any(c in pattern for c in "*?["):
        p = pattern.rstrip("/")
        return re.compile(re.escape(p) + r"(?:/.*)?\Z", re.S)
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 2 if pattern[i + 1:i + 2] in ("!", "]") else i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
                continue
            body = pattern[i + 1:j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"(?![/])[{body}]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z", re.S)


def in_scope(path: str, scope) -> bool:
    return any(_glob_regex(p).match(path) for p in scope)


# --- changes ----------------------------------------------------------------

def _stage(worktree: Path) -> str:
    """Stage everything the delegate left (untracked too) and return the tree.

    This tree is what the scope check sees and what gets committed: checks run
    later and may write files, and none of that reaches the branch.
    """
    _git(worktree, ["add", "-A", "--", "."])
    return _git(worktree, ["write-tree"]).stdout.strip()


def _compute_changes(worktree: Path, base: str, tree: str, req: Request,
                     result: Result, rd: Path) -> None:
    # -z: a path with a newline or a quote is one record, never two.
    # --no-renames: a rename is a delete plus an add, so both ends are checked
    # against the scope; with rename detection only the new path was.
    raw = _git(worktree, ["diff-tree", "-r", "-z", "--no-renames", "--name-status",
                          base, tree], text=False).stdout
    parts = raw.split(b"\0")
    changed = []
    for i in range(0, len(parts) - 1, 2):
        code = parts[i].decode("ascii", "replace")[:1]
        path = parts[i + 1].decode("utf-8", "backslashreplace")
        changed.append({"path": path, "change": code})
    result.changed_files = changed
    if req.scope:
        result.scope_violation = [c["path"] for c in changed
                                  if not in_scope(c["path"], req.scope)]
    patch = _git(worktree, ["diff-tree", "-r", "-p", "--binary", "--no-renames",
                            base, tree], text=False).stdout
    (rd / "patch.diff").write_bytes(patch)


def _commit(worktree: Path, base: str, tree: str, result: Result, req: Request) -> str:
    """Create delegate/<run_id> pointing at a commit of exactly `tree`.

    Plumbing on purpose: no hooks (they would run code from a tree the model
    wrote, with the runner's environment) and no signing (the commit is the
    delegate's, not the user's). The branch is created, never moved: it fails
    if it already exists.
    """
    one_line = " ".join(req.task.split())[:60]
    message = f"delegate {result.run_id}: {one_line}\n\nprovider: {req.provider}\n"
    commit = _git(worktree, ["commit-tree", "--no-gpg-sign", tree, "-p", base,
                             "-m", message],
                  env=_git_env(_identity(req.provider))).stdout.strip()
    branch = f"delegate/{result.run_id}"
    _git(worktree, ["update-ref", f"refs/heads/{branch}", commit, ""])
    return branch


# --- child ------------------------------------------------------------------

def _child_argv(req: Request, worktree: Path) -> list[str]:
    argv = [_resolve(os.environ.get("DELEGATE_BIN") or "delegate"), req.provider, "--json"]
    if req.account:
        argv += ["--account", req.account]
    if "edit" in req.grants:
        argv.append("--edit")
    if "bash" in req.grants:
        argv.append("--bash")
    for server in req.mcp:
        argv += ["--mcp", server]
    argv += ["--dir", str(worktree), "--timeout", str(req.timeout), "--", req.task]
    return argv


def _supervise(argv, cwd: Path, stdout, stderr, wall: float, run: _Run,
               env: dict | None = None) -> tuple[int, str | None]:
    """Run argv in its own session; returns (exit code, stop reason or None).

    Whatever the outcome, nothing the child started is alive when this
    returns: leftovers of a clean exit are terminated too.
    """
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, env=env,
                                start_new_session=True)
    except OSError as exc:
        raise _Infra(f"no se pudo lanzar {argv[0]!r}: {exc}") from exc
    pid = proc.pid
    try:
        (run.rd / "child.pid").write_text(str(pid), encoding="utf-8")
    except OSError:
        pass
    reason = None
    deadline = time.monotonic() + wall
    try:
        while not _child_exited(pid):
            if run.stop_requested():
                reason = "cancel"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            time.sleep(POLL)
    finally:
        # Session id == pid: start_new_session made the child a session leader,
        # and its unreaped zombie keeps that id from being reused.
        _terminate_tree(pid, pid, _grace() if reason or not _child_exited(pid) else 1.0)
        proc.wait()
        try:
            (run.rd / "child.pid").unlink()
        except OSError:
            pass
    return proc.returncode, reason


# --- harness json -----------------------------------------------------------

def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _apply_harness_json(result: Result, rd: Path) -> bool:
    """Read the child's stdout as a Claude Code --json object. False if broken.

    `total_cost_usd` is deliberately ignored: the harness prices every model
    with Anthropic's table, which says nothing about what DeepSeek or Qwen
    charge. The cost comes from prices.tsv or stays null.
    """
    raw = (rd / "stdout.log").read_text(encoding="utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        result.error = "la salida JSON de delegate no se pudo leer (ver stdout.log)"
        return False
    if not isinstance(data, dict):
        result.error = "la salida de delegate no es un objeto JSON (ver stdout.log)"
        return False
    report = data.get("result")
    if isinstance(report, str):
        result.report = report
    elif report is not None:
        result.report = json.dumps(report, ensure_ascii=False)
    usage = data.get("usage")
    if isinstance(usage, dict):
        result.usage = {
            "input": _count(usage.get("input_tokens")),
            "output": _count(usage.get("output_tokens")),
            "cache_read": _count(usage.get("cache_read_input_tokens")),
            "cache_write": _count(usage.get("cache_creation_input_tokens")),
        }
    models = data.get("modelUsage")
    if isinstance(models, dict) and models:
        # The main model is the one that did the work; a small model may also
        # appear (titles, summaries) and must not be reported as the model.
        def weight(item):
            u = item[1] if isinstance(item[1], dict) else {}
            return sum(_count(u.get(k)) or 0 for k in ("inputTokens", "outputTokens"))
        result.model = max(models.items(), key=weight)[0]
    result.cost_usd_estimate = _estimate(result, models)
    if data.get("is_error") is True:
        result.error = "el harness reportó is_error=true"
    return True


def _estimate(result: Result, model_usage) -> float | None:
    """Price the run with prices.tsv; a broken or empty table gives None."""
    try:
        prices = _report.load_prices()
    except ValueError:
        return None
    return _report.estimate_cost(result.provider, result.model, result.usage,
                                 model_usage, prices)


# --- checks -----------------------------------------------------------------

def _check_env(worktree: Path) -> dict:
    env = {k: os.environ[k] for k in _CHECK_ENV_KEEP if k in os.environ}
    # Only absolute PATH entries outside the worktree: an empty, relative or
    # in-tree entry would let the delegate plant the `npm` the check runs.
    wt = os.path.realpath(worktree)
    keep = []
    for entry in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        if not entry.startswith("/"):
            continue
        real = os.path.realpath(entry)
        if real == wt or real.startswith(wt + os.sep):
            continue
        keep.append(entry)
    env["PATH"] = os.pathsep.join(keep) or "/usr/bin:/bin"
    return env


def _run_checks(req: Request, result: Result, rd: Path, worktree: Path,
                run: _Run) -> bool:
    """Run every check argv in the worktree. True if any did not exit 0."""
    if not req.checks:
        return False
    checks_dir = rd / "checks"
    checks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    limit = min(req.timeout, CHECK_MAX_TIMEOUT)
    env = _check_env(worktree)
    failed = False
    for index, cmd in enumerate(req.checks):
        run.check_stop()
        log_rel = f"checks/{index}.log"
        started = time.monotonic()
        with open(rd / log_rel, "wb") as log:
            try:
                code, reason = _supervise(list(cmd), worktree, log, subprocess.STDOUT,
                                          limit, run, env=env)
            except _Infra as exc:
                log.write(f"no se pudo ejecutar el check: {exc}\n".encode())
                code, reason = 127, None
        if reason == "cancel":
            raise _Stop()
        exit_code = None if reason == "timeout" else code
        result.checks.append(CheckResult(cmd=list(cmd), exit=exit_code, log=log_rel,
                                         seconds=round(time.monotonic() - started, 3)))
        if exit_code != 0:
            failed = True
    return failed


# --- lifecycle --------------------------------------------------------------

def _request_dict(req: Request) -> dict:
    data = dict(req.__dict__)
    data["grants"] = list(req.grants)
    data["mcp"] = list(req.mcp)
    data["checks"] = [list(c) for c in req.checks]
    data["scope"] = list(req.scope)
    return data


def _run(req: Request, result: Result, run: _Run) -> None:
    rd = run.rd
    repo = _toplevel(req.repo)
    run.check_stop()
    base = _resolve_base(repo, req, result.run_id, rd)
    result.base = base

    worktree = contract.state_root() / "worktrees" / result.run_id
    worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _git(repo, ["worktree", "add", "--detach", "--quiet", str(worktree), base])
    result.worktree = str(worktree)
    run.check_stop()

    with open(rd / "stdout.log", "wb") as out, open(rd / "stderr.log", "wb") as err:
        exit_code, reason = _supervise(_child_argv(req, worktree), worktree, out, err,
                                       _wall(req), run)
    result.child_exit = exit_code

    # The changes are computed from the worktree whatever happened: a run that
    # timed out may still have left work worth inspecting on its branch.
    tree = _stage(worktree)
    _compute_changes(worktree, base, tree, req, result, rd)

    parsed = _apply_harness_json(result, rd)
    harness_error = result.error
    result.error = None
    if reason == "cancel":
        result.status = contract.CANCELLED
    elif reason == "timeout":
        result.status = contract.TIMED_OUT
        result.error = f"delegate-run cortó la corrida a los {_wall(req):g}s"
    elif exit_code == DELEGATE_TIMEOUT_EXIT:
        result.status = contract.TIMED_OUT
        result.error = f"delegate cortó al harness a los {req.timeout}s (exit 124)"
    elif exit_code == 2:
        result.status = contract.REJECTED
        result.error = "delegate rechazó la corrida (flags, config o cuenta; ver stderr.log)"
    elif exit_code != 0:
        result.status = contract.TASK_FAILED
        result.error = f"delegate salió con {exit_code} (ver stderr.log)"
    elif not parsed:
        result.status = contract.INVALID_RESULT
        result.error = harness_error
    elif harness_error:  # is_error=true with exit 0
        result.status = contract.TASK_FAILED
        result.error = harness_error
    else:
        try:
            failed = _run_checks(req, result, rd, worktree, run)
            result.status = contract.CHECKS_FAILED if failed else contract.COMPLETED
        except _Stop:
            result.status = contract.CANCELLED

    if result.changed_files:
        result.branch = _commit(worktree, base, tree, result, req)


def execute(req: Request) -> Result:
    """Run one request to a terminal Result, writing result.json atomically."""
    previous = os.umask(0o077)
    try:
        return _execute(req)
    finally:
        os.umask(previous)


def _new_run_dir() -> tuple[str, Path]:
    for _ in range(10):
        run_id = contract.new_run_id()
        rd = contract.run_dir(run_id)
        rd.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            rd.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id, rd
    raise _Infra("no se pudo crear un directorio de corrida único")


def _execute(req: Request) -> Result:
    run_id, rd = _new_run_dir()
    # The run lock is taken before anything else is written, and held until
    # result.json is terminal: `status`, `cancel` and `gc` read liveness from
    # it, and the kernel drops it however the runner dies.
    lock_fd = os.open(contract.lock_path(rd),
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    run = _Run(rd)
    saved = _install_handlers(run)
    _become_subreaper()
    result = Result(run_id=run_id, status=contract.RUNNING, provider=req.provider,
                    account=req.account, grants=list(req.grants),
                    started_at=time.time(), artifacts=str(rd))
    slot = None
    try:
        contract.write_json_atomic(rd / "request.json", _request_dict(req))
        contract.write_json_atomic(rd / "result.json", result.to_json())
        slot = _acquire_slot(req.wait, run)
        if slot is None:
            result.status = contract.BUSY
            result.error = "no hay slot libre y la petición pidió wait=false"
        else:
            _run(req, result, run)
    except _Stop:
        result.status = contract.CANCELLED
    except _Refused as exc:
        result.status = contract.REJECTED
        result.error = str(exc)
    except _Infra as exc:
        result.status = contract.INFRA_FAILED
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - the result must still be written
        result.status = contract.INFRA_FAILED
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if result.status == contract.CANCELLED and run.signalled is not None:
            result.error = f"el runner recibió la señal {signal.Signals(run.signalled).name}"
        if slot is not None:
            os.close(slot)
        result.finished_at = time.time()
        try:
            contract.write_json_atomic(rd / "result.json", result.to_json())
        finally:
            _restore_handlers(saved)
            os.close(lock_fd)
    return result


# --- cancel -----------------------------------------------------------------

def cancel(run_id: str, wait: float | None = None) -> dict:
    """Ask a live run to stop and wait for it to write its terminal result.

    Returns {"run_id", "cancelled", "status", "error"}. `cancelled` is True
    only when the run was alive and ended as CANCELLED.
    """
    rd = contract.run_dir(run_id)
    out = {"run_id": run_id, "cancelled": False, "status": None, "error": None}
    if not rd.is_dir():
        out["error"] = "no existe esa corrida"
        return out
    if not contract.runner_alive(rd):
        out["status"] = _recorded_status(rd)
        out["error"] = "la corrida no está viva"
        return out
    fd = os.open(rd / CANCEL_MARKER, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.write(fd, str(int(time.time())).encode())
    os.close(fd)
    deadline = time.monotonic() + (wait if wait is not None else _grace() + 120)
    while contract.runner_alive(rd) and time.monotonic() < deadline:
        time.sleep(POLL)
    out["status"] = _recorded_status(rd)
    if contract.runner_alive(rd):
        out["error"] = "la corrida sigue viva después de pedir la cancelación"
    out["cancelled"] = out["status"] == contract.CANCELLED
    return out


def _recorded_status(rd: Path):
    try:
        data = json.loads((rd / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("status") if isinstance(data, dict) else None
