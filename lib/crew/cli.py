"""Command line of crew.

`launch` never asks questions: it reads a finished brief from a file (the
skill builds it with `crew brief`), refuses anything secret-shaped, picks the
destination from the personal config by GitHub owner, and starts one agent
under a project-wide numbering lock. Numbering is serialized locally so two
launches of the same project never pick the same `#n`.

`refresh`, `status`, `questions`, `answer` and `cancel` work on the state of
the project in the current directory (`--cwd` moves it for the first two).
`status` and `questions` quote the agent's text as untrusted: a fixed header,
every line behind `> `, no end-of-turn marker, and no terminal escapes or
control bytes. `answer` takes the project launch lock, asks the host for the
target agent's state directly (never the cache, which a lost single-flight
refresh can leave stale), and only sends when the assignment is still waiting,
so two chats cannot answer the same turn twice.

The config lives outside the repo, in `$CREW_CONFIG` or
`~/.config/crew/config.json`:
`{"host": "caracas", "orgs": {"<owner>": "<remote cwd>"}}`.
Locks live in `$CREW_STATE_DIR` or `~/.local/state/crew`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .brief import SecretInBrief, build_brief, check_secrets, load_contract
from .config import NO_DESTINATION, destination_for, load_config
from .paseo import (CrewError, Paseo, check_brief, next_number, resolve,
                    ssh_prefix)
from .project import project_of
from .state import (LOGS_TAIL, QUESTIONS_HEADER, fresh_state, last_message,
                    lock, mark_working, refresh, strip_control,
                    without_marker)

USAGE = """uso: crew launch --title T --brief-file F [--cwd DIR]
       crew brief --words-file W [--decision D]... [--constraint C]... [--base B]
       crew refresh [--cwd DIR]
       crew status [--cwd DIR]
       crew questions N
       crew answer N     (la respuesta va por stdin)
       crew cancel N
"""

_LAUNCH_OPTIONS = {"--title": False, "--brief-file": False, "--cwd": False}
_BRIEF_OPTIONS = {"--words-file": False, "--base": False,
                  "--decision": True, "--constraint": True}
_CWD_OPTIONS = {"--cwd": False}


def _error(message: str) -> None:
    sys.stderr.write(f"crew: {message}\n")


@contextmanager
def _launch_lock(slug: str):
    """One launch at a time per project: the number is read and consumed here."""
    with lock(f"{slug}.launch.lock", blocking=True):
        yield


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


def _project_for(cwd: str):
    project = project_of(cwd)
    if project is None:
        raise CrewError("no encuentro el repo de esta carpeta", 2)
    return project


def cmd_launch(opts: dict) -> int:
    title = opts["--title"]
    # The title travels in the remote argv, quoted: it goes through the same
    # secret backstop, before anything leaves this machine.
    check_secrets(title)
    brief = _read_text(opts["--brief-file"], "el brief")
    check_secrets(brief)  # the file is re-checked here, not only when built
    check_brief(brief)

    cwd = opts.get("--cwd") or os.getcwd()
    project = _project_for(cwd)
    paseo, destination = _target(project)

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
    project = _project_for(cwd)
    base = opts.get("--base") or _current_branch(cwd)
    brief = build_brief(words, f"{project.owner}/{project.repo}", base,
                        opts.get("--decision", []), opts.get("--constraint", []),
                        load_contract())
    sys.stdout.write(brief)
    return 0


def _target(project) -> tuple[Paseo, str]:
    """The runtime for this project and its remote cwd, or a refusal.

    A project whose owner is not in the config never reaches the host: the
    same refusal is used by every command that would talk to it.
    """
    config = load_config()
    destination = destination_for(config, project.owner)
    if destination is None:
        raise CrewError(NO_DESTINATION, 2)
    return Paseo(config["host"], ssh_prefix()), destination


def _paseo_for(project) -> Paseo:
    return _target(project)[0]


def _number_arg(args: list[str]) -> int:
    if not args:
        raise CrewError("falta el número del encargo", 2)
    if len(args) > 1:
        raise CrewError(f"argumento de más: {args[1]}", 2)
    try:
        number = int(args[0])
    except ValueError:
        raise CrewError(f"número de encargo inválido: {args[0]}", 2) from None
    if number < 1:
        raise CrewError(f"número de encargo inválido: {args[0]}", 2)
    return number


def _quoted(number: int, text: str) -> str:
    """The agent's text as a quote: fixed header, every line behind `> `.

    Terminal escapes and control bytes are dropped first: the text is
    untrusted and must not act on the terminal that shows the quote.
    """
    text = strip_control(text) or ""
    lines = "\n".join(f"> {line}" if line.strip() else ">"
                      for line in text.splitlines())
    return f"{QUESTIONS_HEADER.format(n=number)}\n{lines}\n"


def _question_block(number: int, agent_id: str, paseo: Paseo) -> str:
    message = last_message(paseo.logs(agent_id, LOGS_TAIL))
    if message is None:
        return ""
    text = without_marker(message)
    if not text.strip():
        return ""
    return _quoted(number, text)


def cmd_refresh(opts: dict) -> int:
    project = _project_for(opts.get("--cwd") or os.getcwd())
    refresh(project.slug, _paseo_for(project))
    return 0


def cmd_status(opts: dict) -> int:
    project = _project_for(opts.get("--cwd") or os.getcwd())
    paseo = _paseo_for(project)
    cache = refresh(project.slug, paseo)
    entries = sorted(
        ((int(entry["n"]), agent_id, entry)
         for agent_id, entry in cache.get("agents", {}).items()
         if isinstance(entry, dict) and isinstance(entry.get("n"), int)),
        key=lambda item: item[0])
    if not entries:
        sys.stdout.write("no hay encargos en este proyecto\n")
        return 0
    out = "".join(f"#{number} — {entry.get('state', 'desconocido')}\n"
                  for number, _agent_id, entry in entries)
    for number, agent_id, entry in entries:
        if entry.get("state") != "esperando":
            continue
        block = _question_block(number, agent_id, paseo)
        if block:
            out += "\n" + block
    sys.stdout.write(out)
    return 0


def cmd_questions(args: list[str]) -> int:
    number = _number_arg(args)
    project = _project_for(os.getcwd())
    paseo = _paseo_for(project)
    agent = resolve(paseo.ls(project.slug), number)
    message = last_message(paseo.logs(agent["id"], LOGS_TAIL))
    if message is None:
        raise CrewError(f"el encargo #{number} no tiene un mensaje que mostrar", 1)
    sys.stdout.write(_quoted(number, without_marker(message)))
    return 0


def cmd_answer(args: list[str]) -> int:
    number = _number_arg(args)
    answer = sys.stdin.read()
    # The answer travels on ssh's stdin: refuse a secret before it can.
    check_secrets(answer)
    if not answer.strip():
        raise CrewError("la respuesta está vacía; escribe lo que quieres responder", 2)
    project = _project_for(os.getcwd())
    paseo = _paseo_for(project)
    # The launch lock serializes answers (and launches): two chats must not
    # answer the same turn twice.
    with _launch_lock(project.slug):
        # The number and the state both come from the host right now. A
        # refresh could lose the single flight and return a stale cache that
        # says `esperando` for an agent that already started working.
        agent = resolve(paseo.ls(project.slug), number)
        state = fresh_state(paseo, agent)
        if state != "esperando":
            if state == "trabajando":
                raise CrewError(
                    f"el encargo #{number} ya está trabajando; "
                    "tu respuesta no se envió", 2)
            raise CrewError(
                f"el encargo #{number} no está esperando respuesta "
                f"(estado: {state}); tu respuesta no se envió", 2)
        paseo.send(agent["id"], answer)
        # `send` started a turn: the cached state is stale from here on.
        mark_working(project.slug, agent["id"], number)
    sys.stdout.write(f"respuesta enviada al encargo #{number}\n")
    return 0


def cmd_cancel(args: list[str]) -> int:
    number = _number_arg(args)
    project = _project_for(os.getcwd())
    paseo = _paseo_for(project)
    agent = resolve(paseo.ls(project.slug), number)
    paseo.stop(agent["id"])
    sys.stdout.write(f"encargo #{number} cancelado\n")
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
        if command == "refresh":
            return cmd_refresh(_options(rest, _CWD_OPTIONS))
        if command == "status":
            return cmd_status(_options(rest, _CWD_OPTIONS))
        if command == "questions":
            return cmd_questions(rest)
        if command == "answer":
            return cmd_answer(rest)
        if command == "cancel":
            return cmd_cancel(rest)
        raise CrewError(f"subcomando desconocido: {command}", 2)
    except SecretInBrief as exc:
        _error(str(exc))
        return 2
    except CrewError as exc:
        _error(str(exc))
        return exc.code
    except OSError:
        # The state directory could not be used (permissions, a file in the
        # way): a short line, never a traceback.
        _error("no pude usar el directorio de estado")
        return 2
    except KeyboardInterrupt:
        return 130
