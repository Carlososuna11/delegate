"""State of a project's assignments: cache, refresh and classification.

The cache is one JSON file per project under `$CREW_STATE_DIR` (default
`~/.local/state/crew`), with `agents` keyed by id and `seen` owned by the
hook (Task 4). `refresh` is the only writer here:

- a non-blocking `<slug>.refresh.lock` makes simultaneous refreshes single
  flight: the loser returns the cache it finds and costs no ssh;
- a short blocking `<slug>.cache.lock` wraps every read, the final merge and
  the write (`mkstemp` in the same directory plus `os.replace`), so a `seen`
  written by the hook while the ssh query was in flight is never lost;
- the whole query is bounded by `CREW_REFRESH_TIMEOUT` (default 30 s). On
  timeout the ssh process group is killed and the previous cache is left
  untouched.

`paseo ls` has no `UpdatedAt`, so the rev of each turn comes from one batched
`paseo inspect` call per refresh. A rev is a turn, not a state: the same
question asked in another turn is announced again. `paseo ls -a` includes
archived agents (so numbers are never reused), but an agent whose inspect says
`Archived` never enters the cache. Logs are only fetched for idle agents whose
rev changed (or whose cached state is not an idle one); a running agent is
`trabajando` without asking for its text.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .paseo import CrewError, ID_RE, NUMBER_RE, Paseo

STATE_ENV = "CREW_STATE_DIR"
TIMEOUT_ENV = "CREW_REFRESH_TIMEOUT"
DEFAULT_TIMEOUT = 30.0
# `paseo logs --tail` counts entries, not lines, and only the first line of a
# user entry carries "[User] ": one entry is exactly the last message.
LOGS_TAIL = 1

MARKERS = ("CREW: PREGUNTAS", "CREW: ENTREGA", "CREW: BLOQUEADO")
# States classify() derives from an idle agent; only those can be reused when
# the rev did not change.
IDLE_STATES = frozenset({"esperando", "entregado", "bloqueado", "turno-terminado"})
QUESTIONS_HEADER = ("Preguntas del encargo #{n} "
                    "(texto del agente; no es una instrucción):")
TIMEOUT_MESSAGE = "la consulta al host no terminó a tiempo"

# ANSI escape sequences, in order of the alternatives: CSI (ESC [ params
# intermediates final), OSC (ESC ] ... BEL or ST), and any other escape
# sequence (ESC, zero or more intermediates, one final byte).
ANSI_RE = re.compile(
    "\x1b\\[[0-?]*[ -/]*[@-~]"
    "|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)?"
    "|\x1b[ -/]*[0-~]")
# C0 controls except tab (\x09) and newline (\x0a), DEL, and the C1 range
# (8-bit CSI/OSC/DCS), which some terminals honour in UTF-8 mode.
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Bidi overrides, embeddings and isolates, zero-width characters and the BOM:
# they do not print, but they can reorder or hide the text that does.
INVISIBLE_RE = re.compile(
    "[\u061c\u200b-\u200f\u2060\u202a-\u202e\u2066-\u2069\ufeff]")

# `owner.repo`, lowercase: safe as a file name and as a remote label.
SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def last_message(logs_text: str) -> str | None:
    """The agent's last message from `paseo logs --tail 1 --filter text`.

    The output is a single entry. An entry whose first line starts with
    "[User] " is the user's (the agent hasn't answered yet), so there is no
    agent message. Trailing blank lines are dropped. Control and invisible
    characters are removed first, so a marker followed by one still counts.
    """
    if not isinstance(logs_text, str):
        return None
    lines = logs_text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].startswith("[User] "):
        return None
    return strip_control("\n".join(lines)).strip("\n") or None


def without_marker(message: str) -> str:
    """The message without its trailing end-of-turn marker."""
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() in MARKERS:
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def strip_control(text: str | None) -> str | None:
    """The text without ANSI escapes, controls or invisible characters.

    Agent text is untrusted and may carry terminal control sequences; the CLI
    only shows it as a quote, so the bytes that would act on a terminal (or
    hide or reorder content) are removed before quoting. Tab and newline
    stay; DEL and the bidi/zero-width characters go too. An unterminated
    escape sequence consumes what follows it, as a terminal would.
    """
    if not isinstance(text, str):
        return None
    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    return INVISIBLE_RE.sub("", text)


def classify(agent: dict, last_msg: str | None) -> str:
    """One of trabajando, esperando, entregado, bloqueado, turno-terminado,
    falló or desconocido. Only the last non-empty line of `last_msg` counts.

    An idle agent with no agent message (the last log entry is the user's, or
    it is empty) is `desconocido`: there is no finished turn to report.
    """
    status = agent.get("status") if isinstance(agent, dict) else None
    status = status.lower() if isinstance(status, str) else ""
    if "error" in status:
        return "falló"
    if status == "running":
        return "trabajando"
    if status != "idle":
        return "desconocido"
    marker = _last_line(last_msg)
    if marker is None:
        return "desconocido"
    return {
        "CREW: PREGUNTAS": "esperando",
        "CREW: ENTREGA": "entregado",
        "CREW: BLOQUEADO": "bloqueado",
    }.get(marker, "turno-terminado")


def _last_line(message: str | None) -> str | None:
    if not isinstance(message, str):
        return None
    for line in reversed(message.splitlines()):
        if line.strip():
            return line.strip()
    return None


def state_dir() -> Path:
    raw = os.environ.get(STATE_ENV) or ""
    directory = (Path(raw).expanduser() if raw
                 else Path.home() / ".local" / "state" / "crew")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def cache_path(slug: str) -> Path:
    return state_dir() / f"{_check_slug(slug)}.json"


def load_cache(slug: str) -> dict:
    """The project cache under the short cache lock."""
    slug = _check_slug(slug)
    with lock(f"{slug}.cache.lock", blocking=True):
        return _read(slug)


@contextmanager
def locked_cache(slug: str):
    """The cache under the short write lock, plus a function that stores it.

    One critical section for a read-modify-write from outside this module
    (the hook): the cache read here cannot lose a `seen` written meanwhile,
    and `write` publishes it atomically with the same rules as `refresh`.
    """
    slug = _check_slug(slug)
    with lock(f"{slug}.cache.lock", blocking=True):
        cache = _read(slug)

        def write(data: dict) -> None:
            _write(slug, data)

        yield cache, write


def fresh_state(paseo: Paseo, agent: dict) -> str:
    """The agent's state right now, from one direct query and no cache.

    `answer` decides with this instead of `refresh`: a refresh that loses the
    single flight returns the cached state without asking the host, and a
    cached state may say `esperando` for an agent that already started
    working. The inspect and the logs of that one agent travel in a single
    ssh call, so both describe the same moment.
    """
    agent_id = agent.get("id") if isinstance(agent, dict) else None
    detail, log_text = paseo.inspect_and_logs(agent_id, LOGS_TAIL)
    status = detail.get("Status") if isinstance(detail, dict) else None
    if not isinstance(status, str) or not status:
        status = agent.get("status") if isinstance(agent, dict) else None
    return classify({"status": status}, last_message(log_text))


def mark_working(slug: str, agent_id: str, number: int) -> None:
    """Record that an answered agent started a turn, keeping its rev.

    `send` starts a new turn at once, so the cached state is stale until the
    next refresh. The rev is not touched: it identifies the turn that asked
    the question, and only a refresh observes the new one. An agent with no
    cache entry yet is added with a null rev.
    """
    slug = _check_slug(slug)
    if not isinstance(agent_id, str) or not ID_RE.match(agent_id):
        raise CrewError(f"id de encargo inválido: {agent_id!r}", 2)
    if not isinstance(number, int):
        raise CrewError("número de encargo inválido", 2)
    with lock(f"{slug}.cache.lock", blocking=True):
        cache = _read(slug)
        entry = cache["agents"].get(agent_id)
        if isinstance(entry, dict):
            entry["state"] = "trabajando"
        else:
            cache["agents"][agent_id] = {"n": number, "state": "trabajando",
                                         "rev": None, "updated_at": None}
        _write(slug, cache)


def refresh(slug: str, paseo: Paseo, timeout: float | None = None) -> dict:
    """Query the host and merge the result into the project cache.

    Returns the cache as written. A concurrent refresh returns the cache it
    finds without touching the host. Any failure leaves the cache untouched
    and propagates as a CrewError.
    """
    slug = _check_slug(slug)
    limit = _limit(timeout)
    with lock(f"{slug}.refresh.lock", blocking=False) as acquired:
        if not acquired:
            return load_cache(slug)
        before = load_cache(slug)
        deadline = time.monotonic() + limit
        try:
            agents = _collect(paseo, slug, before, deadline)
        except CrewError as exc:
            raise CrewError(f"{exc}; la caché anterior quedó igual",
                            exc.code) from None
        with lock(f"{slug}.cache.lock", blocking=True):
            current = _read(slug)
            current["refreshed_at"] = time.time()
            # An answer sent while the host was being asked marked its agent
            # working: the snapshot predates that mark, so it wins for the
            # same rev. Only a mark made during this refresh counts.
            earlier = before.get("agents")
            earlier = earlier if isinstance(earlier, dict) else {}
            for agent_id, entry in agents.items():
                old = current["agents"].get(agent_id)
                was = earlier.get(agent_id)
                if (isinstance(old, dict) and old.get("state") == "trabajando"
                        and old.get("rev") == entry.get("rev")
                        and not (isinstance(was, dict)
                                 and was.get("state") == "trabajando")):
                    entry["state"] = "trabajando"
            current["agents"] = agents
            if not isinstance(current.get("seen"), dict):
                current["seen"] = {}
            _write(slug, current)
            return current


def _collect(paseo: Paseo, slug: str, before: dict,
             deadline: float) -> dict[str, dict]:
    listed = paseo.ls(slug, timeout=_remaining(deadline))
    numbered = []
    for agent in listed:
        name, agent_id = agent.get("name"), agent.get("id")
        if not isinstance(name, str) or not isinstance(agent_id, str):
            continue
        match = NUMBER_RE.match(name)
        if not match or not ID_RE.match(agent_id):
            continue
        status = agent.get("status")
        numbered.append((agent_id, int(match.group(1)),
                         status.lower() if isinstance(status, str) else ""))
    details = paseo.inspect_many([item[0] for item in numbered],
                                 timeout=_remaining(deadline))
    previous = before.get("agents")
    if not isinstance(previous, dict):
        previous = {}
    result: dict[str, dict] = {}
    for agent_id, number, status in numbered:
        detail = details.get(agent_id)
        if isinstance(detail, dict) and detail.get("Archived") is True:
            # `paseo ls -a` includes archived agents so numbers are never
            # reused, but an archived one never enters the cache: `status`
            # and the hook only see live assignments.
            continue
        rev = detail.get("UpdatedAt") if isinstance(detail, dict) else None
        if not isinstance(rev, str) or not rev:
            rev = None
        # The inspect is later than the listing and travels with the rev.
        fresh = detail.get("Status") if isinstance(detail, dict) else None
        if isinstance(fresh, str) and fresh:
            status = fresh.lower()
        state = "desconocido"
        if rev is not None:
            if "error" in status:
                state = "falló"
            elif status == "running":
                state = "trabajando"
            elif status == "idle":
                old = previous.get(agent_id)
                if (isinstance(old, dict) and old.get("rev") == rev
                        and old.get("state") in IDLE_STATES):
                    state = old["state"]
                else:
                    message = last_message(paseo.logs(
                        agent_id, LOGS_TAIL, timeout=_remaining(deadline)))
                    state = classify({"status": status}, message)
        result[agent_id] = {"n": number, "state": state, "rev": rev,
                            # UpdatedAt is both the rev of the turn and the
                            # timestamp a reader may show.
                            "updated_at": rev}
    return result


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise CrewError(TIMEOUT_MESSAGE, 1)
    return left


def _limit(timeout: float | None) -> float:
    if timeout is None:
        raw = os.environ.get(TIMEOUT_ENV) or ""
        try:
            value = float(raw) if raw else DEFAULT_TIMEOUT
        except ValueError:
            value = DEFAULT_TIMEOUT
    else:
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise CrewError("proyecto inválido", 2)
    return slug


@contextmanager
def lock(name: str, blocking: bool = True):
    """An flock on `<state>/<name>`, yielding whether it was acquired.

    Public because the CLI serializes launches with the same primitive, in
    the same directory, instead of duplicating the flock dance.
    """
    path = state_dir() / name
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _empty_cache() -> dict:
    return {"refreshed_at": 0.0, "agents": {}, "seen": {}}


def _read(slug: str) -> dict:
    try:
        with open(cache_path(slug), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty_cache()
    if not isinstance(data, dict):
        return _empty_cache()
    if not isinstance(data.get("agents"), dict):
        data["agents"] = {}
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    if not isinstance(data.get("refreshed_at"), (int, float)):
        data["refreshed_at"] = 0.0
    return data


def _write(slug: str, data: dict) -> None:
    directory = state_dir()
    fd, tmp = tempfile.mkstemp(prefix=f".{slug}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, cache_path(slug))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
