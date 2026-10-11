"""Fase C: run delegate-run on another host (caracas) over ssh.

Client side (the laptop), `delegate-run --remote <host> < peticion.json`:

  1. Validate the request here, choose its run_id (the client must know it to
     ask `status` after a dropped connection) and resolve the base in the
     local repository, with the same dirty-tree and snapshot rules as a local
     run.
  2. `ssh host delegate-run mirror <name>`: the remote creates (first use) or
     finds its bare mirror of this repository and says where it is.
  3. `git push host:<mirror> <base>:refs/delegate/base/<run_id>`: a new ref in
     a namespace of its own, never forced, never a user branch.
  4. `ssh host delegate-run` with the request on stdin, `repo` replaced by the
     mirror and `base` by the full commit id.
  5. On a transport failure, ask `delegate-run status <run_id>` with backoff
     before anything else: a cut connection does not mean the run did not
     happen. Only a run the remote has never heard of is sent again, and the
     same run_id makes a duplicate impossible (the remote refuses it).
  6. `git fetch` refs/heads/delegate/<run_id> into the local repository
     (never forced), check it is one commit on top of the base, and keep a
     copy of the remote result.json under the local state dir, with `host`.

Every remote command line is fixed text plus values that passed a strict
pattern (run_id, mirror name), quoted anyway. The request travels on stdin
only. ssh runs with BatchMode, a connect timeout, keepalives, and no agent,
X11 or port forwarding: nothing of the laptop crosses but the request and
the git objects of the base.

Server side, `delegate-run mirror <name>`: the bare mirror under
$DELEGATE_MIRROR_ROOT (default ~/delegate/repos), created atomically.

Knobs (tests shorten them):
  DELEGATE_REMOTE_SSH        the ssh executable (absolute path; default `ssh`)
  DELEGATE_REMOTE_POLL       first status-poll interval, seconds (2)
  DELEGATE_REMOTE_POLL_MAX   longest interval between polls (30)
  DELEGATE_REMOTE_GIVEUP     seconds without reaching the host before giving
                             up and leaving the run to `delegate-run fetch` (900)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat as _stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import contract, request as _request, runner

# A host as ssh_config names it, optionally user@: no ':' (git would read
# host:path wrongly), no '/', and never a leading '-' (an ssh option).
HOST_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}"
                     r"(?:@[A-Za-z0-9_][A-Za-z0-9_.-]{0,252})?\Z")
MIRROR_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
# What the client accepts as the mirror path the remote reports: absolute,
# plain characters only (it goes inside git's own single quotes on the
# remote command line), ending in .git, no '..' segment.
MIRROR_PATH_RE = re.compile(r"\A(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+\.git\Z")

SSH_OPTIONS = (
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    # Nothing of the laptop is lent to the remote: no agent (a delegate with
    # bash on the remote could use it), no X11, no port forwarding.
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "PermitLocalCommand=no",
)

# Exit codes of `delegate-run --remote` and `fetch` beyond 0/1/2.
EXIT_RUN_EXISTS = 3   # the run_id already exists: no new run was created
EXIT_UNKNOWN = 4      # the host could not be reached; the run may be alive

SSH_TRANSPORT_EXIT = 255
CALL_TIMEOUT = 120    # seconds for a short remote call (mirror, status)
RESENDS = 3

TERMINAL = frozenset(contract.STATUSES) | {contract.ABANDONED, contract.UNREADABLE}


class RemoteError(Exception):
    """A failure the client reports and stops on. Carries the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --- knobs -------------------------------------------------------------------

def _knob(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def check_host(host: str) -> str:
    if not isinstance(host, str) or not HOST_RE.match(host):
        raise RemoteError(f"host inválido: {host!r} (un alias de ssh_config o user@host, "
                          "sin ':' ni '/', y sin empezar con '-')", 2)
    return host


def _ssh_bin() -> str:
    raw = os.environ.get("DELEGATE_REMOTE_SSH")
    if raw:
        if not os.path.isabs(raw):
            raise RemoteError("DELEGATE_REMOTE_SSH tiene que ser una ruta absoluta", 2)
        return raw
    found = contract.which("ssh")
    if not found:
        raise RemoteError("no encuentro ssh en el PATH", 2)
    return found


def ssh_argv(host: str, remote_cmd: list[str]) -> list[str]:
    """ssh [options] -- host '<cmd>'. The command is one quoted string of
    fixed words and validated values: the remote shell sees nothing else."""
    return [_ssh_bin(), *SSH_OPTIONS, "--", host, shlex.join(remote_cmd)]


def _git_ssh_env() -> dict:
    """What git needs to push and fetch over the same hardened ssh."""
    return {"GIT_SSH_COMMAND": shlex.join([_ssh_bin(), *SSH_OPTIONS]),
            "GIT_SSH_VARIANT": "ssh"}


# --- local state -------------------------------------------------------------

def _pending_dir() -> Path:
    d = contract.state_root() / "remote"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _pending_path(run_id: str) -> Path:
    contract.run_dir(run_id)  # validates the shape
    return _pending_dir() / f"{run_id}.json"


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def mirror_name(repo: Path) -> str:
    """<basename>-<10 hex of the absolute path>: two repos named `web` on the
    laptop never share a mirror, and the name is safe on any command line."""
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", repo.name)
    base = re.sub(r"\A[^A-Za-z0-9]+", "", base)[:40].rstrip(".") or "repo"
    digest = hashlib.sha256(os.fsencode(str(repo))).hexdigest()[:10]
    return f"{base}-{digest}"


# --- remote calls --------------------------------------------------------------

def _call(host: str, remote_cmd: list[str], payload: bytes | None = None,
          timeout: float | None = CALL_TIMEOUT, quiet: bool = False):
    """(returncode, stdout bytes, stderr text). A local timeout counts as a
    transport failure (255)."""
    try:
        proc = subprocess.run(ssh_argv(host, remote_cmd),
                              input=payload if payload is not None else b"",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return SSH_TRANSPORT_EXIT, b"", "ssh: tiempo agotado"
    except OSError as exc:
        raise RemoteError(f"no se pudo ejecutar ssh: {exc}", 2) from exc
    err = proc.stderr.decode("utf-8", "replace")
    if err and not quiet:
        _say(err.rstrip("\n"), raw=True)
    return proc.returncode, proc.stdout, err


def _say(message: str, raw: bool = False) -> None:
    try:
        sys.stderr.write(message + "\n" if raw else f"delegate-run: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _json_object(raw: bytes):
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _remote_mirror(host: str, name: str) -> str:
    rc, out, err = _call(host, ["delegate-run", "mirror", name])
    if rc == SSH_TRANSPORT_EXIT:
        raise RemoteError(f"no se pudo conectar con {host}: {err.strip() or 'ssh falló'}", 1)
    if rc == 127:
        raise RemoteError(f"delegate-run no está instalado en {host} "
                          "(ver scripts/install-remote.sh)", 1)
    obj = _json_object(out) if rc == 0 else None
    path = obj.get("path") if obj else None
    if not isinstance(path, str) or not MIRROR_PATH_RE.match(path) \
            or "/../" in path or "/./" in path:
        raise RemoteError(f"{host} no devolvió la ruta del mirror (código {rc})", 1)
    return path


def _remote_status(host: str, run_id: str):
    """("found", result) | ("missing", None) | ("transport", None)."""
    rc, out, _err = _call(host, ["delegate-run", "status", run_id], quiet=True)
    if rc == 0:
        obj = _json_object(out)
        if obj is not None and obj.get("run_id") == run_id:
            return "found", obj
        return "transport", None
    if rc == 1:
        return "missing", None
    return "transport", None


# --- git ---------------------------------------------------------------------

def _push_base(repo: Path, host: str, mirror: str, base: str, run_id: str) -> None:
    ref = f"refs/delegate/base/{run_id}"
    proc = runner._git(repo, ["push", "--quiet", "--recurse-submodules=no", "--no-signed",
                              f"{host}:{mirror}", f"{base}:{ref}"],
                       check=False, env=runner._git_env(_git_ssh_env()))
    if proc.returncode != 0:
        raise RemoteError(f"no se pudo empujar la base a {host}: {proc.stderr.strip()}", 1)


def _fetch_branch(repo: Path, host: str, mirror: str, run_id: str, base: str) -> str | None:
    """Bring delegate/<run_id> into the local repo. None on success, else why.

    Not forced: a local branch with that name and another commit is never
    moved. The fetched commit must be exactly one commit on top of the base:
    a branch the remote filled with anything else is deleted again.
    """
    ref = f"refs/heads/delegate/{run_id}"
    proc = runner._git(repo, ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                              "--recurse-submodules=no", f"{host}:{mirror}", f"{ref}:{ref}"],
                       check=False, env=runner._git_env(_git_ssh_env()))
    if proc.returncode != 0:
        return f"git fetch falló: {proc.stderr.strip()}"
    tip = runner._rev(repo, ref)
    parent = runner._rev(repo, f"{ref}^") if tip else None
    second = runner._git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^2"], check=False)
    if tip is None or parent != base or second.returncode == 0:
        if tip:
            runner._git(repo, ["update-ref", "--no-deref", "-d", ref, tip], check=False)
        return "la rama traída no es un commit sobre la base pedida; se borró"
    return None


# --- results -------------------------------------------------------------------

def _local_copy(run_id: str, host: str, remote_result: dict, local_req: dict,
                pending: dict) -> dict:
    """Store the remote result under runs/<run_id>/ with `host`, so status,
    list, stats, cost and gc see it like any local run. Paths that only exist
    on the remote move to remote_*; gc then only touches the local branch."""
    result = dict(remote_result)
    result["host"] = host
    result["remote_artifacts"] = remote_result.get("artifacts")
    result["remote_worktree"] = remote_result.get("worktree")
    result["worktree"] = None
    rd = contract.run_dir(run_id)
    rd.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        rd.mkdir(mode=0o700)
    except FileExistsError:
        raise RemoteError(f"ya existe una copia local de la corrida {run_id}",
                          EXIT_RUN_EXISTS) from None
    result["artifacts"] = str(rd)
    contract.write_json_atomic(rd / "request.json", local_req)
    contract.write_json_atomic(rd / "remote.json", pending)
    contract.write_json_atomic(rd / "result.json", result)
    try:
        _pending_path(run_id).unlink()
    except FileNotFoundError:
        pass
    return result


def _exit_for(result: dict) -> int:
    return 0 if result.get("status") == contract.COMPLETED else 1


def _complete(pending: dict, remote_result: dict) -> tuple[dict, int]:
    """Fetch the branch of a terminal remote run and store the local copy."""
    run_id, host = pending["run_id"], pending["host"]
    remote_result = dict(remote_result)
    branch = remote_result.get("branch")
    if branch is not None:
        if branch != f"delegate/{run_id}":
            remote_result["branch"] = None
            remote_result["status"] = contract.INFRA_FAILED
            remote_result["error"] = f"el remoto reportó una rama inesperada: {branch!r}"
        else:
            why = None
            for attempt in range(3):
                why = _fetch_branch(Path(pending["repo"]), host, pending["mirror"],
                                    run_id, pending["base"])
                if why is None or "no es un commit sobre la base" in why:
                    break
                time.sleep(_knob("DELEGATE_REMOTE_POLL", 2.0) * (attempt + 1))
            if why is not None:
                if "no es un commit sobre la base" not in why:
                    # Transport: the run is fine and stays fetchable later.
                    raise RemoteError(f"la corrida {run_id} terminó en {host} pero no se pudo "
                                      f"traer la rama ({why}); reintenta con "
                                      f"`delegate-run fetch {run_id}`", EXIT_UNKNOWN)
                remote_result["branch"] = None
                remote_result["status"] = contract.INFRA_FAILED
                remote_result["error"] = why
    result = _local_copy(run_id, host, remote_result, pending["local_request"], pending)
    return result, _exit_for(result)


def _unknown(pending: dict, why: str) -> tuple[dict, int]:
    out = {"v": contract.SCHEMA_VERSION, "run_id": pending["run_id"],
           "host": pending["host"], "status": "unknown", "error": why,
           "fetch": f"delegate-run fetch {pending['run_id']}"}
    return out, EXIT_UNKNOWN


# --- waiting -------------------------------------------------------------------

def _wait(pending: dict, payload: bytes | None) -> tuple[dict, int]:
    """Poll status until the run is terminal; resend only a run never seen.

    `payload` is None when resending is not allowed (`fetch`): there the
    remote has to know the run already.
    """
    host, run_id = pending["host"], pending["run_id"]
    interval = _knob("DELEGATE_REMOTE_POLL", 2.0)
    cap = _knob("DELEGATE_REMOTE_POLL_MAX", 30.0)
    giveup = _knob("DELEGATE_REMOTE_GIVEUP", 900.0)
    last_contact = time.monotonic()
    missing, resends, seen = 0, 0, False
    while True:
        kind, obj = _remote_status(host, run_id)
        if kind == "found":
            last_contact = time.monotonic()
            seen, missing = True, 0
            if obj.get("status") in TERMINAL:
                return _complete(pending, obj)
        elif kind == "missing":
            last_contact = time.monotonic()
            missing += 1
            if seen:
                return _unknown(pending, f"{host} ya no tiene la corrida {run_id}")
            if payload is None:
                return _unknown(pending, f"{host} no tiene la corrida {run_id}")
            # Twice in a row, apart: a request still in flight has had time
            # to create its run dir. A duplicate is refused remotely anyway.
            if missing >= 2:
                if resends >= RESENDS:
                    return _unknown(pending, f"{host} no registró la corrida tras "
                                             f"{RESENDS} reenvíos")
                resends += 1
                missing = 0
                _say(f"{host} no conoce la corrida {run_id}: se reenvía la petición "
                     f"({resends}/{RESENDS})")
                done = _send(pending, payload, first=False)
                if done is not None:
                    return done
        elif time.monotonic() - last_contact > giveup:
            return _unknown(pending, f"sin conexión con {host} durante {giveup:g}s; la "
                                     "corrida puede seguir viva")
        time.sleep(interval)
        interval = min(interval * 2, cap)


def _send(pending: dict, payload: bytes, first: bool):
    """Send the request once. A terminal (result, exit) or None to poll."""
    host, run_id = pending["host"], pending["run_id"]
    rc, out, err = _call(host, ["delegate-run"], payload, timeout=None, quiet=True)
    if err:
        _say(err.rstrip("\n"), raw=True)
    obj = _json_object(out) if rc in (0, 1) else None
    if obj is not None and obj.get("run_id") == run_id and obj.get("status") in TERMINAL:
        return _complete(pending, obj)
    if rc == EXIT_RUN_EXISTS:
        if first:
            raise RemoteError(f"{host} ya tiene una corrida {run_id}", EXIT_RUN_EXISTS)
        return None  # an earlier send got there: poll it
    if rc == 2:
        raise RemoteError(f"{host} rechazó la petición: {err.strip()}", 2)
    if rc == 127:
        raise RemoteError(f"delegate-run no está instalado en {host}", 1)
    _say(f"la conexión con {host} se cortó (código {rc}); se consulta el estado "
         f"de {run_id} antes de reintentar")
    return None


def _detach(pending: dict, payload: bytes) -> tuple[dict, int]:
    """Start the run and return once the remote has registered it.

    The ssh session is then closed on purpose, which is a transport cut the
    runner is built to survive (HUP ignored, result written to disk anyway).
    """
    host, run_id = pending["host"], pending["run_id"]
    proc = subprocess.Popen(ssh_argv(host, ["delegate-run"]), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except OSError:
        pass
    deadline = time.monotonic() + _knob("DELEGATE_REMOTE_GIVEUP", 900.0)
    interval = min(_knob("DELEGATE_REMOTE_POLL", 2.0), 2.0)
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read()
                obj = _json_object(out) if proc.returncode in (0, 1) else None
                if obj is not None and obj.get("run_id") == run_id \
                        and obj.get("status") in TERMINAL:
                    return _complete(pending, obj)
                if proc.returncode == EXIT_RUN_EXISTS:
                    raise RemoteError(f"{host} ya tiene una corrida {run_id}", EXIT_RUN_EXISTS)
                if proc.returncode == 2:
                    raise RemoteError(f"{host} rechazó la petición", 2)
                return _wait(pending, payload)
            kind, _obj = _remote_status(host, run_id)
            if kind == "found":
                return ({"v": contract.SCHEMA_VERSION, "run_id": run_id, "host": host,
                         "status": contract.RUNNING, "detached": True,
                         "fetch": f"delegate-run fetch {run_id}"}, 0)
            time.sleep(interval)
        return _unknown(pending, f"{host} no confirmó la corrida a tiempo")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# --- entry points ----------------------------------------------------------------

def run_remote(raw: bytes, host: str, detach: bool = False) -> tuple[dict | None, int]:
    """`delegate-run --remote <host> [--detach]`. Returns (stdout JSON, exit)."""
    host = check_host(host)
    try:
        req = _request.parse_request(raw)
    except contract.RequestError as exc:
        raise RemoteError(str(exc), 2) from None
    previous = os.umask(0o077)
    try:
        return _run_remote(req, host, detach)
    finally:
        os.umask(previous)


def _run_remote(req: contract.Request, host: str, detach: bool):
    run_id = req.run_id or contract.new_run_id()
    if contract.run_dir(run_id).exists() or _pending_path(run_id).exists():
        raise RemoteError(f"ya existe una corrida local {run_id}", EXIT_RUN_EXISTS)
    work = Path(tempfile.mkdtemp(dir=_pending_dir(), prefix=".base-"))
    try:
        repo, bare = runner._toplevel(req.repo)
        base = runner._resolve_base(repo, req, run_id, work, bare)
    except (runner._Refused, runner._Infra) as exc:
        status = (contract.REJECTED if isinstance(exc, runner._Refused)
                  else contract.INFRA_FAILED)
        return _refused_locally(req, run_id, host, status, str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    local_req = runner._request_dict(req)
    local_req.update(repo=str(repo), base=base, run_id=run_id, host=host)
    try:
        mirror = _remote_mirror(host, mirror_name(repo))
        _push_base(repo, host, mirror, base, run_id)
    except RemoteError as exc:
        if exc.code != 1:
            raise
        return _refused_locally(req, run_id, host, contract.INFRA_FAILED, str(exc),
                                local_req, base)

    remote_req = runner._request_dict(req)
    remote_req.update(v=contract.SCHEMA_VERSION, repo=mirror, base=base, snapshot=False,
                      run_id=run_id)
    payload = json.dumps(remote_req, ensure_ascii=True).encode()
    pending = {"v": 1, "run_id": run_id, "host": host, "mirror": mirror,
               "repo": str(repo), "base": base, "created_at": time.time(),
               "local_request": local_req}
    contract.write_json_atomic(_pending_path(run_id), pending)
    try:
        if detach:
            return _detach(pending, payload)
        done = _send(pending, payload, first=True)
        return done if done is not None else _wait(pending, payload)
    except RemoteError as exc:
        if exc.code in (2, EXIT_RUN_EXISTS) or "no está instalado" in str(exc):
            # The remote refused before creating the run: nothing to fetch.
            try:
                _pending_path(run_id).unlink()
            except FileNotFoundError:
                pass
        raise


def _refused_locally(req, run_id, host, status, error, local_req=None, base=None):
    """A run that never left the laptop still gets a result, like a local run
    the runner refuses: stored with `host`, exit 1."""
    now = time.time()
    result = contract.Result(run_id=run_id, status=status, provider=req.provider,
                             account=req.account, grants=list(req.grants), base=base,
                             error=error, started_at=now, finished_at=now).to_json()
    result["host"] = host
    if local_req is None:
        local_req = runner._request_dict(req)
        local_req.update(run_id=run_id, host=host)
    rd = contract.run_dir(run_id)
    rd.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rd.mkdir(mode=0o700)
    result["artifacts"] = str(rd)
    contract.write_json_atomic(rd / "request.json", local_req)
    contract.write_json_atomic(rd / "result.json", result)
    return result, 1


def fetch(run_id: str, host: str | None = None) -> tuple[dict, int]:
    """`delegate-run fetch <run_id> [--remote host]`: wait for a detached (or
    cut) remote run, bring its branch and store the local copy."""
    contract.run_dir(run_id)
    local = _read_json(contract.run_dir(run_id) / "result.json")
    if isinstance(local, dict) and local.get("host"):
        if host is not None and local["host"] != host:
            raise RemoteError(f"la corrida {run_id} es de {local['host']}, no de {host}", 2)
        return local, _exit_for(local)
    pending = _read_json(_pending_path(run_id))
    if not isinstance(pending, dict) or pending.get("run_id") != run_id:
        raise RemoteError(f"no hay una corrida remota pendiente {run_id}", 1)
    if host is not None and pending.get("host") != host:
        raise RemoteError(f"la corrida {run_id} es de {pending.get('host')}, no de {host}", 2)
    check_host(pending.get("host"))
    mirror = pending.get("mirror")
    if not isinstance(mirror, str) or not MIRROR_PATH_RE.match(mirror):
        raise RemoteError(f"el registro pendiente de {run_id} está dañado", 1)
    previous = os.umask(0o077)
    try:
        return _wait(pending, None)
    finally:
        os.umask(previous)


def remote_status(run_id: str, host: str) -> tuple[bytes, int]:
    """`delegate-run status <run_id> --remote host`: the remote's own answer."""
    contract.run_dir(run_id)
    rc, out, _err = _call(check_host(host), ["delegate-run", "status", run_id])
    return out, rc


# --- server side -----------------------------------------------------------------

def mirror_root() -> Path:
    raw = os.environ.get("DELEGATE_MIRROR_ROOT")
    root = Path(raw) if raw else Path.home() / "delegate" / "repos"
    if not root.is_absolute():
        raise RemoteError("DELEGATE_MIRROR_ROOT tiene que ser una ruta absoluta", 2)
    return root


def _plain_git(cwd, *args) -> subprocess.CompletedProcess:
    git = contract.which("git")
    if git is None:
        raise RemoteError("no encuentro git en el PATH", 1)
    return subprocess.run([git, *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True,
                          env=contract.hardened_git_env())


def mirror(name: str) -> dict:
    """`delegate-run mirror <name>`: the bare mirror, created on first use.

    Created in a temporary directory and renamed into place, so two clients
    racing on first use never see half a repository. A symlink or anything
    that is not a bare repository at that path is refused, never reused.
    """
    if not isinstance(name, str) or not MIRROR_NAME_RE.match(name) or ".." in name:
        raise RemoteError(f"nombre de mirror inválido: {name!r}", 2)
    root = mirror_root()
    previous = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / f"{name}.git"
        if not os.path.lexists(path):
            tmp = Path(tempfile.mkdtemp(dir=root, prefix=".init-"))
            try:
                proc = _plain_git(tmp, "init", "--bare", "--quiet", "--initial-branch=main", ".")
                if proc.returncode != 0:
                    raise RemoteError(f"git init falló: {proc.stderr.strip()}", 1)
                # Objects pushed by a client are checked as they arrive.
                _plain_git(tmp, "config", "receive.fsckObjects", "true")
                try:
                    os.rename(tmp, path)
                except OSError:
                    pass  # another client won the race; its mirror is used
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        os.umask(previous)
    st = os.lstat(path)
    if not _stat.S_ISDIR(st.st_mode):
        raise RemoteError(f"{path} existe y no es un directorio", 1)
    proc = _plain_git(path, "rev-parse", "--is-bare-repository")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise RemoteError(f"{path} existe y no es un repositorio bare", 1)
    return {"mirror": name, "path": str(path)}
