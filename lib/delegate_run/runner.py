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
  DELEGATE_SANDBOX     auto (default), bwrap or off; see _decide_sandbox

OS sandbox. With bwrap usable, `delegate` is told to run its child (the
harness and everything it starts) inside bubblewrap: private mount, pid, ipc
and user namespaces, the worktree read-write, the repository's gitdir
read-only, a tmpfs HOME and /tmp, and no unix sockets (seccomp). `delegate`
itself runs outside, so the key file is never mounted in the sandbox. The
network is shared: see CLAUDE.md, "Sandbox".
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
import sys
import time
from pathlib import Path

from . import contract, report as _report, scope as _scope
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

# Git for the runner's own calls: the caller's GIT_* environment is not
# carried (GIT_DIR or GIT_INDEX_FILE from a hook would point every call
# somewhere else), hooks never run and nothing daemonizes behind the runner's
# back. Before the child runs, the repository is the user's and its config is
# honoured (a smudge filter such as LFS works). After it ran, the config is
# the delegate's to write (a worktree shares .git/config with the user's
# checkout, and --bash reaches it), so every call goes through
# _hardened_env: see contract.hardened_git_env.
_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
               "-c", "maintenance.auto=false", "-c", "core.quotePath=false")
# Executable keys a repository can define under a name of its choosing: each
# one found is overridden with an empty value (an empty `required` is false).
_HARDENED_KEYS_RE = r"^(filter\..*\.(clean|smudge|process|required)|diff\..*\.(command|textconv))$"

_trusted_path = contract.trusted_path

SANDBOX_MODES = ("auto", "bwrap", "off")
# The architectures bin/delegate has a seccomp program for.
_SANDBOX_ARCHES = ("x86_64", "aarch64")
# The namespaces bin/delegate asks for, on a throwaway command: if this fails,
# the real sandbox would fail too (no user namespaces, an AppArmor policy
# without a profile for bwrap, a bwrap too old for --disable-userns).
_BWRAP_PROBE = ("--unshare-all", "--share-net", "--unshare-user", "--disable-userns",
                "--die-with-parent", "--new-session", "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev", "--", "/bin/true")


def _resolve(name: str) -> str:
    if "/" in name:
        if not os.path.isabs(name):
            raise _Infra(f"{name!r} tiene que ser una ruta absoluta o un nombre en el PATH")
        return name
    found = contract.which(name)
    if not found:
        raise _Infra(f"no encuentro {name!r} en el PATH")
    return found


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
    """(ppid, sid, starttime, state, pgrp) from /proc/<pid>/stat, or None."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = data.rfind(")")
    fields = data[end + 2:].split()
    try:
        # fields[0] is field 3 (state); ppid=4, pgrp=5, sid=6, starttime=22.
        return int(fields[1]), int(fields[3]), int(fields[19]), fields[0], int(fields[2])
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


def _tree(root: int, sids) -> dict[int, int]:
    """{pid: starttime} of root, its descendants and its sessions; never us.

    Descendants are followed from the root and from the runner itself (an
    orphan reparented to the subreaper is the runner's child now). Members of
    the sessions in `sids` are added even when they escaped the tree, which is
    the case when the runner could not become a subreaper.
    """
    if isinstance(sids, int) or sids is None:
        sids = {sids} if sids else set()
    me = os.getpid()
    procs = _snapshot_procs()
    children: dict[int, list[int]] = {}
    for pid, info in procs.items():
        children.setdefault(info[0], []).append(pid)
    found: dict[int, int] = {}
    stack = [root] + [p for p in children.get(me, []) if p != root]
    while stack:
        pid = stack.pop()
        if pid in found or pid == me or pid not in procs:
            continue
        found[pid] = procs[pid][2]
        stack.extend(children.get(pid, []))
    for pid, info in procs.items():
        if info[1] in sids and info[1] > 1 and pid != me:
            found.setdefault(pid, info[2])
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


class _Groups:
    """Sessions and process groups the tree has been seen in.

    A /proc scan is not atomic: a process that forks and exits in a loop is
    gone by the time it is signalled, and its successor was not in the scan.
    Session and group ids survive fork, so every one seen is remembered: the
    session widens later scans, and the group is signalled with killpg, which
    the kernel applies to every member at once. Never the runner's own.
    """

    def __init__(self, sid: int | None):
        self.own = {os.getsid(0), os.getpgrp()}
        self.sids = {sid} if sid and sid > 1 else set()
        self.pgrps: set[int] = set()

    def note(self, pids) -> None:
        for pid in pids:
            info = _stat(pid)
            if info is None:
                continue
            if info[1] > 1 and info[1] not in self.own:
                self.sids.add(info[1])
            if info[4] > 1 and info[4] not in self.own:
                self.pgrps.add(info[4])

    def kill(self, sig: int) -> None:
        for pgrp in self.pgrps:
            try:
                os.killpg(pgrp, sig)
            except (ProcessLookupError, PermissionError):
                pass


def _terminate_tree(root: int, sid: int | None, grace: float) -> None:
    """TERM everything under root, wait up to grace, KILL what is left.

    Rescans on every poll so a process forked during shutdown is caught too,
    and only stops after several scans in a row find nothing (see _Groups).
    The root must not have been reaped yet (zombie at most).
    """
    groups = _Groups(sid)
    seen: dict[int, int] = {}

    def scan() -> dict[int, int]:
        # Zombies count for learning groups: a fork-and-exit chain leaves its
        # dead links as the subreaper's zombies, whose stat still names the
        # session and group of the live link.
        tree = _tree(root, groups.sids)
        groups.note(tree)
        return _live(tree)

    deadline = time.monotonic() + grace
    targets = scan()
    while targets:
        seen.update(targets)
        _signal_all(targets, signal.SIGTERM)
        groups.kill(signal.SIGTERM)
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL)
        targets = scan()
        _reap_orphans([p for p in seen if p != root])
    quiet = 0
    for _ in range(200):
        targets = scan()
        groups.kill(signal.SIGKILL)
        if not targets:
            quiet += 1
            if quiet >= 3:
                break
            time.sleep(POLL / 5)
            continue
        quiet = 0
        seen.update(targets)
        _signal_all(targets, signal.SIGKILL)
        time.sleep(POLL / 5)
        _reap_orphans([p for p in seen if p != root])
    _reap_orphans([p for p in seen if p != root])
    # Orphans that exited on their own before the cleanup are zombies of the
    # subreaper and were never "live": reap them too, never the root (its
    # owner reaps it).
    me = os.getpid()
    ours = _tree(root, groups.sids)
    _reap_orphans([pid for pid, info in _snapshot_procs().items()
                   if info[0] == me and info[3] == "Z" and pid != root and pid in ours])


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

def _path(raw: bytes) -> str:
    """Git's bytes as a str that round-trips: UTF-8, other bytes as surrogates."""
    return raw.decode("utf-8", "surrogateescape")


def _git_env(extra: dict | None = None) -> dict:
    """Environment for git BEFORE the child runs: the user's own repository."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(contract.GIT_FIXED_ENV)
    # Helpers git runs by name (git-lfs, a credential helper) resolve against
    # this PATH: no relative entries.
    env["PATH"] = _trusted_path()
    if extra:
        env.update(extra)
    return env


def _hardened_env(pin: list[str], extra: dict | None = None) -> dict:
    """Environment for git AFTER the child ran.

    The filter and driver names are read from the pinned repository on every
    call: reading config runs nothing, and a name added later is caught.
    """
    base = contract.hardened_git_env()
    proc = _git(".", [*pin, "config", "-z", "--name-only", "--get-regexp", _HARDENED_KEYS_RE],
                check=False, env=base, text=False)
    if proc.returncode not in (0, 1):  # 1: no key matched
        raise _Infra("git config falló al leer los filtros del repositorio: "
                     + proc.stderr.decode("utf-8", "replace").strip())
    keys = [(_path(k), "") for k in proc.stdout.split(b"\0") if k]
    env = contract.hardened_git_env(keys)
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


def _wgit(worktree: Path, gitdir: str, args, extra: dict | None = None,
          text: bool = True) -> subprocess.CompletedProcess:
    """A hardened git call inside the delegate's worktree, pinned to its gitdir."""
    pin = _pin(worktree, gitdir)
    return _git(worktree, [*pin, *args], env=_hardened_env(pin, extra), text=text)


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


def _dirty(repo: Path) -> tuple[bool, list[str]]:
    """(the tree has changes, submodules with uncommitted content inside).

    --untracked-files=all regardless of status.showUntrackedFiles: a tree dirty
    only by new files is still a tree the caller may think is being used.
    Porcelain v2 says which entries are submodules and whether their content
    (not just their commit) changed: a snapshot cannot carry that content.
    """
    proc = _git(repo, ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
                text=False)
    records = proc.stdout.split(b"\0")
    dirty, submodules, i = False, [], 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        dirty = True
        kind = rec[:1]
        if kind == b"2":
            i += 1  # the rename's original path is the next record
        if kind in (b"1", b"2"):
            fields = rec.split(b" ", 8)
            sub = fields[2] if len(fields) > 2 else b"N..."
            if sub.startswith(b"S") and (sub[2:3] == b"M" or sub[3:4] == b"U"):
                submodules.append(_path(fields[-1]))
    return dirty, submodules


def _hidden_changes(repo: Path) -> list[str]:
    """Paths whose changes the user's index tells git not to look at.

    assume-unchanged hides an edit from `git status` (and so from the dirty
    check and the snapshot); skip-worktree does the same for a file that is
    present. A sparse checkout's absent files are skip-worktree too, and those
    are fine: there is nothing on disk to lose.
    """
    out = _git(repo, ["ls-files", "-v", "-z"], text=False).stdout
    hidden = []
    for rec in out.split(b"\0"):
        if len(rec) < 3:
            continue
        tag, path = rec[:1], _path(rec[2:])
        if tag.islower() or (tag == b"S" and os.path.lexists(repo / path)):
            hidden.append(path)
    return hidden


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
    _git(repo, ["update-ref", "--no-deref", f"refs/delegate/snapshot/{run_id}", commit, ""])
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
    hidden = _hidden_changes(repo)
    if hidden:
        raise _Refused("el índice marca archivos como assume-unchanged o skip-worktree "
                       f"({', '.join(hidden[:5])}): git no ve sus cambios, así que no se "
                       "puede saber si el árbol está limpio; quita esas marcas con "
                       "`git update-index --no-assume-unchanged --no-skip-worktree`")
    dirty, submodules = _dirty(repo)
    if not dirty:
        return sha
    if submodules:
        raise _Refused("hay submódulos con cambios sin commit adentro "
                       f"({', '.join(submodules[:5])}): ni el snapshot ni el worktree "
                       "llevan ese contenido; haz commit en el submódulo antes de delegar")
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

_glob_regex = _scope.compile_glob
in_scope = _scope.in_scope


# --- changes ----------------------------------------------------------------

def _pin(worktree: Path, gitdir: str) -> list[str]:
    """Explicit --git-dir/--work-tree for every git call inside the worktree.

    The worktree's `.git` is a FILE the delegate can rewrite: pointed at a
    repository it built, with a `filter.*.clean` or `core.fsmonitor` in its
    config, the runner's own `git add` would run model-chosen programs with
    the runner's environment. The gitdir is read once, before the child runs,
    and the `.git` file is never consulted again.
    """
    return ["--git-dir", gitdir, "--work-tree", str(worktree)]


def _stage(worktree: Path, gitdir: str) -> str:
    """Stage everything the delegate left (untracked too) and return the tree.

    This tree is what the scope check sees and what gets committed: checks run
    later and may write files, and none of that reaches the branch.
    """
    _wgit(worktree, gitdir, ["add", "-A", "--", "."])
    return _wgit(worktree, gitdir, ["write-tree"]).stdout.strip()


def _compute_changes(worktree: Path, gitdir: str, base: str, tree: str, req: Request,
                     result: Result, rd: Path) -> None:
    # -z: a path with a newline or a quote is one record, never two.
    # --no-renames: a rename is a delete plus an add, so both ends are checked
    # against the scope; with rename detection only the new path was.
    raw = _wgit(worktree, gitdir, ["diff-tree", "-r", "-z", "--no-renames", "--name-status",
                                   base, tree], text=False).stdout
    parts = raw.split(b"\0")
    changed = []
    for i in range(0, len(parts) - 1, 2):
        code = parts[i].decode("ascii", "replace")[:1]
        # surrogateescape is reversible: the bytes b"\xff.txt" and the literal
        # name "\\xff.txt" stay distinct strings, so a scope naming one never
        # admits the other (backslashreplace made them equal). JSON output is
        # ASCII-escaped, which carries the surrogate through.
        path = _path(parts[i + 1])
        changed.append({"path": path, "change": code})
    result.changed_files = changed
    if req.scope:
        result.scope_violation = [c["path"] for c in changed
                                  if not in_scope(c["path"], req.scope)]
    patch = _wgit(worktree, gitdir, ["diff-tree", "-r", "-p", "--binary", "--no-renames",
                                     "--no-ext-diff", "--no-textconv", base, tree],
                  text=False).stdout
    (rd / "patch.diff").write_bytes(patch)


def _commit(worktree: Path, gitdir: str, base: str, tree: str, result: Result, req: Request) -> str:
    """Create delegate/<run_id> pointing at a commit of exactly `tree`.

    Plumbing on purpose: no hooks (they would run code from a tree the model
    wrote, with the runner's environment) and no signing (the commit is the
    delegate's, not the user's). The branch is created, never moved: it fails
    if it already exists.
    """
    one_line = " ".join(req.task.split())[:60]
    message = f"delegate {result.run_id}: {one_line}\n\nprovider: {req.provider}\n"
    commit = _wgit(worktree, gitdir, ["commit-tree", "--no-gpg-sign", tree, "-p", base,
                                      "-m", message], extra=_identity(req.provider)).stdout.strip()
    branch = f"delegate/{result.run_id}"
    # --no-deref: a symbolic ref planted at this name must not move its target.
    _wgit(worktree, gitdir, ["update-ref", "--no-deref", f"refs/heads/{branch}", commit, ""])
    return branch


# --- sandbox ----------------------------------------------------------------

def _sandbox_mode() -> str:
    raw = os.environ.get("DELEGATE_SANDBOX") or "auto"
    if raw not in SANDBOX_MODES:
        raise _Infra(f"DELEGATE_SANDBOX={raw!r} no es válido: usa auto, bwrap u off")
    return raw


def _bwrap_unusable() -> str | None:
    """None if bwrap can build the sandbox here; otherwise why not."""
    machine = os.uname().machine
    if machine not in _SANDBOX_ARCHES:
        return f"no hay filtro seccomp para la arquitectura {machine}"
    bwrap = contract.which("bwrap")
    if not bwrap:
        return "no encuentro bwrap en el PATH (instálalo con 'sudo apt install bubblewrap')"
    try:
        proc = subprocess.run([bwrap, *_BWRAP_PROBE], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env={"PATH": "/usr/bin:/bin"}, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bwrap no arrancó: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"código {proc.returncode}"
        return f"bwrap no pudo crear el sandbox: {detail}"
    return None


def _decide_sandbox(result: Result) -> bool:
    """Whether the child runs inside bwrap; records the answer in result.sandbox.

    auto: bwrap if it works here, else no sandbox, said on stderr and in
    result.sandbox ("none"). bwrap: no sandbox is a failure (infra_failed),
    never a silent downgrade. off: no sandbox.
    """
    mode = _sandbox_mode()
    if mode == "off":
        result.sandbox = "none"
        return False
    why = _bwrap_unusable()
    if why is None:
        result.sandbox = "bwrap"
        return True
    if mode == "bwrap":
        raise _Infra(f"DELEGATE_SANDBOX=bwrap, pero {why}")
    result.sandbox = "none"
    sys.stderr.write(f"delegate-run: aviso: la corrida va SIN sandbox del sistema ({why}); "
                     "DELEGATE_SANDBOX=bwrap la haría fallar en vez de seguir\n")
    return False


def _sandbox_ro(worktree: Path, gitdir: str) -> list[str]:
    """Paths the sandbox mounts read-only: the repository's git directory.

    The worktree's `.git` names its own gitdir (.git/worktrees/<run_id>),
    which names the common one (objects, refs, config, hooks). Both are
    mounted read-only, so git in the sandbox reads history but cannot write
    a ref, the config or a hook of the user's repository.
    """
    common = _git(worktree, ["rev-parse", "--path-format=absolute",
                             "--git-common-dir"]).stdout.strip()
    if not common:
        raise _Infra("no se pudo leer el gitdir común del worktree")
    paths = [common]
    real_common = os.path.realpath(common)
    real_gitdir = os.path.realpath(gitdir)
    if not (real_gitdir == real_common or real_gitdir.startswith(real_common + os.sep)):
        paths.append(gitdir)
    return paths


# --- child ------------------------------------------------------------------

def _child_argv(req: Request, worktree: Path, sandbox_ro: list[str] | None = None) -> list[str]:
    argv = [_resolve(os.environ.get("DELEGATE_BIN") or "delegate"), req.provider, "--json"]
    if sandbox_ro is not None:
        argv += ["--sandbox", "bwrap"]
        for path in sandbox_ro:
            argv += ["--sandbox-ro", path]
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
    result.num_turns = _count(data.get("num_turns"))
    duration_ms = _count(data.get("duration_ms"))
    result.harness_seconds = None if duration_ms is None else duration_ms / 1000
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
                                 model_usage, prices, at=result.started_at)


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
    sandboxed = _decide_sandbox(result)
    repo = _toplevel(req.repo)
    run.check_stop()
    base = _resolve_base(repo, req, result.run_id, rd)
    result.base = base

    worktree = contract.state_root() / "worktrees" / result.run_id
    worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _git(repo, ["worktree", "add", "--detach", "--quiet", str(worktree), base])
    result.worktree = str(worktree)
    gitdir = _git(worktree, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    if not gitdir:
        raise _Infra("no se pudo leer el gitdir del worktree")
    sandbox_ro = _sandbox_ro(worktree, gitdir) if sandboxed else None
    run.check_stop()

    with open(rd / "stdout.log", "wb") as out, open(rd / "stderr.log", "wb") as err:
        exit_code, reason = _supervise(_child_argv(req, worktree, sandbox_ro), worktree,
                                       out, err, _wall(req), run)
    result.child_exit = exit_code

    # The changes are computed from the worktree whatever happened: a run that
    # timed out may still have left work worth inspecting on its branch.
    tree = _stage(worktree, gitdir)
    _compute_changes(worktree, gitdir, base, tree, req, result, rd)

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
        result.branch = _commit(worktree, gitdir, base, tree, result, req)
    # A stop asked for after the child exited (TERM while staging or
    # committing, a cancel during a run with no checks) is honoured up to
    # here, the last point before the terminal result is written: a run told
    # to stop never reports success. The branch, if any, stays for inspection.
    if result.status in (contract.COMPLETED, contract.CHECKS_FAILED) and run.stop_requested():
        result.status = contract.CANCELLED
        result.error = "se pidió detener la corrida durante el post-proceso"


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
