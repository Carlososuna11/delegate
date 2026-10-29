"""Command line of crew: `crew launch` and `crew brief`.

`launch` never asks questions: it reads a finished brief from a file (the
skill builds it with `crew brief`), refuses anything secret-shaped, picks the
destination from the personal config by GitHub owner, and starts one agent
under a project-wide numbering lock. Numbering is serialized locally so two
launches of the same project never pick the same `#n`.

The config lives outside the repo, in `$CREW_CONFIG` or
`~/.config/crew/config.json`:
`{"host": "caracas", "orgs": {"<owner>": "<remote cwd>"}}`.
Locks live in `$CREW_STATE_DIR` or `~/.local/state/crew`.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .brief import SecretInBrief, build_brief, check_secrets, load_contract
from .paseo import CrewError, Paseo, check_brief, next_number, ssh_prefix
from .project import project_of

USAGE = """uso: crew launch --title T --brief-file F [--cwd DIR]
       crew brief --words-file W [--decision D]... [--constraint C]... [--base B]
"""

DEFAULT_HOST = "caracas"
CONFIG_ENV = "CREW_CONFIG"
STATE_ENV = "CREW_STATE_DIR"

_LAUNCH_OPTIONS = {"--title": False, "--brief-file": False, "--cwd": False}
_BRIEF_OPTIONS = {"--words-file": False, "--base": False,
                  "--decision": True, "--constraint": True}


def _error(message: str) -> None:
    sys.stderr.write(f"crew: {message}\n")


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV) or ""
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "crew" / "config.json"


def load_config() -> dict:
    """The personal destinations. A missing file means no destinations."""
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"host": DEFAULT_HOST, "orgs": {}}
    except (OSError, ValueError) as exc:
        raise CrewError(f"no pude leer la config de crew ({path}): {exc}", 2) from None
    if not isinstance(data, dict):
        raise CrewError("la config de crew tiene que ser un objeto JSON", 2)
    orgs = data.get("orgs", {})
    if not isinstance(orgs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in orgs.items()):
        raise CrewError("orgs en la config de crew tiene que ser un objeto de textos", 2)
    host = data.get("host", DEFAULT_HOST)
    if not isinstance(host, str):
        raise CrewError("host en la config de crew tiene que ser texto", 2)
    return {"host": host, "orgs": orgs}


def state_dir() -> Path:
    raw = os.environ.get(STATE_ENV) or ""
    directory = Path(raw) if raw else Path.home() / ".local" / "state" / "crew"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


@contextmanager
def _launch_lock(slug: str):
    """One launch at a time per project: the number is read and consumed here."""
    path = state_dir() / f"{slug}.launch.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _options(args: list[str], spec: dict[str, bool]) -> dict:
    """Parse `--name value` pairs; `True` in spec means repeatable."""
    out: dict = {}
    i = 0
    while i < len(args):
        name = args[i]
        if name not in spec:
            raise CrewError(f"opción desconocida: {name}", 2)
        if i + 1 >= len(args) or not args[i + 1] or args[i + 1].startswith("-"):
            raise CrewError(f"{name} necesita un valor", 2)
        value = args[i + 1]
        if spec[name]:
            out.setdefault(name, []).append(value)
        elif name in out:
            raise CrewError(f"opción repetida: {name}", 2)
        else:
            out[name] = value
        i += 2
    return out


def _require(opts: dict, names: tuple[str, ...]) -> None:
    for name in names:
        if name not in opts:
            raise CrewError(f"falta {name}", 2)


def _read_text(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CrewError(f"no pude leer {what}: {exc}", 2) from None
    except ValueError:  # UnicodeDecodeError, on anything that is not UTF-8
        raise CrewError(f"no pude leer {what}: el archivo no es texto UTF-8", 2) from None


def _current_branch(cwd: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        raise CrewError("no estoy en una rama; usa --base", 2)
    return proc.stdout.strip()


def cmd_launch(opts: dict) -> int:
    title = opts["--title"]
    # The title travels in the remote argv, quoted: it goes through the same
    # secret backstop, before anything leaves this machine.
    check_secrets(title)
    brief = _read_text(opts["--brief-file"], "el brief")
    check_secrets(brief)  # the file is re-checked here, not only when built
    check_brief(brief)

    cwd = opts.get("--cwd") or os.getcwd()
    project = project_of(cwd)
    if project is None:
        raise CrewError("no encuentro el repo de esta carpeta", 2)
    config = load_config()
    destination = config["orgs"].get(project.owner)
    if destination is None:
        raise CrewError("este proyecto no tiene destino en caracas", 2)

    paseo = Paseo(config["host"], ssh_prefix())
    with _launch_lock(project.slug):
        number = next_number(paseo.ls(project.slug))
        full_title = f"#{number} · {title}"
        agent_id = paseo.launch(full_title, destination, project.slug, brief)
    sys.stdout.write(json.dumps(
        {"n": number, "id": agent_id, "title": full_title},
        ensure_ascii=True) + "\n")
    return 0


def cmd_brief(opts: dict) -> int:
    words = _read_text(opts["--words-file"], "el dictado")
    cwd = os.getcwd()
    project = project_of(cwd)
    if project is None:
        raise CrewError("no encuentro el repo de esta carpeta", 2)
    base = opts.get("--base") or _current_branch(cwd)
    brief = build_brief(words, f"{project.owner}/{project.repo}", base,
                        opts.get("--decision", []), opts.get("--constraint", []),
                        load_contract())
    sys.stdout.write(brief)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command == "launch":
            opts = _options(rest, _LAUNCH_OPTIONS)
            _require(opts, ("--title", "--brief-file"))
            return cmd_launch(opts)
        if command == "brief":
            opts = _options(rest, _BRIEF_OPTIONS)
            _require(opts, ("--words-file",))
            return cmd_brief(opts)
        raise CrewError(f"subcomando desconocido: {command}", 2)
    except SecretInBrief as exc:
        _error(str(exc))
        return 2
    except CrewError as exc:
        _error(str(exc))
        return exc.code
