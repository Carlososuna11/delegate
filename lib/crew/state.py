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
question asked in another turn is announced again. Logs are only fetched for
idle agents whose rev changed (or whose cached state is not an idle one); a
running agent is `trabajando` without asking for its text.
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
LOGS_TAIL = 20

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

# `owner.repo`, lowercase: safe as a file name and as a remote label.
SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def last_message(logs_text: str) -> str | None:
    """The agent's last complete message in `paseo logs --filter text`.

    User turns are lines starting with "[User] "; the agent's text is every
    unprefixed line after the last one (blank lines included). Trailing blank
    lines are dropped. When the tail cut the "[User] " line off, the whole
    output is the last message.
    """
    if not isinstance(logs_text, str):
        return None
    lines = logs_text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.startswith("[User] "):
            start = index + 1
    message = lines[start:]
    while message and not message[-1].strip():
        message.pop()
    if not message:
        return None
    return "\n".join(message)


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
    """The text without ANSI escapes or C0 controls (tab and newline stay).

    Agent text is untrusted and may carry terminal control sequences; the CLI
    only shows it as a quote, so the bytes that would act on a terminal (or
    hide content) are removed before quoting. DEL goes too. An unterminated
    escape sequence consumes what follows it, as a terminal would.
    """
    if not isinstance(text, str):
        return None
    return CONTROL_RE.sub("", ANSI_RE.sub("", text))


def classify(agent: dict, last_msg: str | None) -> str:
    """One of trabajando, esperando, entregado, bloqueado, turno-terminado,
    falló or desconocido. Only the last non-empty line of `last_msg` counts."""
    status = agent.get("status") if isinstance(agent, dict) else None
    status = status.lower() if isinstance(status, str) else ""
    if "error" in status:
        return "falló"
    if status == "running":
        return "trabajando"
    if status != "idle":
        return "desconocido"
    marker = _last_line(last_msg)
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
    directory = Path(raw) if raw else Path.home() / ".local" / "state" / "crew"
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
    with _lock(f"{slug}.cache.lock", blocking=True):
        return _read(slug)


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
    with _lock(f"{slug}.cache.lock", blocking=True):
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
    with _lock(f"{slug}.refresh.lock", blocking=False) as acquired:
        if not acquired:
            return load_cache(slug)
        before = load_cache(slug)
        deadline = time.monotonic() + limit
        try:
            agents = _collect(paseo, slug, before, deadline)
        except CrewError as exc:
            raise CrewError(f"{exc}; la caché anterior quedó igual",
                            exc.code) from None
        with _lock(f"{slug}.cache.lock", blocking=True):
            current = _read(slug)
            current["refreshed_at"] = time.time()
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
        rev = detail.get("UpdatedAt") if isinstance(detail, dict) else None
        if not isinstance(rev, str) or not rev:
            rev = None
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
def _lock(name: str, blocking: bool):
    """An flock on `<state>/<name>`, yielding whether it was acquired."""
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
