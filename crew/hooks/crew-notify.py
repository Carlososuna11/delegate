#!/usr/bin/python3 -I
"""Prompt hook of crew: announce what changed in this project.

The harness feeds one event on stdin (`session_id`, `cwd`,
`hook_event_name`). The hook resolves the project of `cwd` and prints one
`hookSpecificOutput` line listing the assignments that finished, are waiting,
are blocked or failed since this session last looked. It never talks to the
host: it reads the per-project cache written by `crew refresh` and, when that
cache is missing, corrupt or older than `REFRESH_AFTER`, starts a detached
`crew refresh --cwd <cwd>` (`CREW_BIN`, or `bin/crew` of this repo). The
single flight is the refresh lock of Task 3.

The line carries only fixed phrases and validated integers: never a title or
any text an agent wrote, so nothing an agent says reaches the context this
way. Everything is wrapped: any failure exits 0 with no output, so a broken
hook never gets in the way of a session, and it must answer well under a
second.

It runs with `-I`, so sys.path carries neither this script's directory nor the
user site; ../lib is added here, resolving symlinks, because claude-config
invokes the hook through a link.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def _bootstrap():
    """Put <repo>/lib on sys.path and return <repo>, from this file's real path."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__))))
    lib = os.path.join(root, "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return root


try:
    _ROOT = _bootstrap()
    from crew import state  # noqa: E402
    from crew.project import project_of  # noqa: E402
except Exception:  # an installed tree out of place: never break the session
    sys.exit(0)

REFRESH_ENV = "CREW_BIN"
REFRESH_AFTER = 60.0          # seconds before a cache is considered stale
SEEN_TTL = 7 * 24 * 60 * 60.0  # a session's bookkeeping is dropped after this
MAX_FIELD = 400               # a hook field longer than this is not believable
MINUTE_LIMIT = 90             # above this the age is shown in hours

PREFIX = "Encargos de este proyecto: "
SUFFIX = '. Usa "crew status" para verlos.'
# Only these states are announced; `trabajando` and `desconocido` are not.
FINISHED = ("entregado", "turno-terminado")


# The only events this hook is registered for; anything else stays silent, so
# every field of the output is a fixed value.
EVENTS = ("UserPromptSubmit", "SessionStart")


def _event(raw):
    """(session_id, cwd, event name) from the hook's stdin, or None."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    values = [data.get(key)
              for key in ("session_id", "cwd", "hook_event_name")]
    for value in values:
        if not isinstance(value, str) or not value or len(value) > MAX_FIELD:
            return None
    session, cwd, name = values
    if name not in EVENTS or not os.path.isdir(cwd):
        return None
    return session, cwd, name


def _number(value):
    """The assignment number, or None when the cache does not hold one."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _timestamp(value):
    """Seconds since the epoch for an ISO-8601 `updated_at`, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _age(updated_at, now):
    """` hace 40 min` / ` hace 2 h`, or "" when the timestamp is unusable."""
    moment = _timestamp(updated_at)
    if moment is None:
        return ""
    minutes = max(1, int(max(0.0, now - moment) // 60))
    if minutes <= MINUTE_LIMIT:
        return f" hace {minutes} min"
    return f" hace {round(minutes / 60)} h"


def _phrase(state_name, number, updated_at, now):
    """The fixed phrase of one assignment, or None if it is not announced."""
    if state_name in FINISHED:
        return f"#{number} terminó"
    if state_name == "esperando":
        return f"#{number} espera tu respuesta{_age(updated_at, now)}"
    if state_name == "bloqueado":
        return f"#{number} está bloqueado"
    if state_name == "falló":
        return f"#{number} falló"
    return None


def _seen_with(seen, session, fresh, now):
    """`seen` without expired sessions, with this session's new revs."""
    kept = {}
    for key, value in seen.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        at = value.get("at")
        if not isinstance(at, (int, float)) or now - at > SEEN_TTL:
            continue
        kept[key] = value
    previous = kept.get(session)
    revs = previous.get("revs") if isinstance(previous, dict) else None
    revs = dict(revs) if isinstance(revs, dict) else {}
    for _number_, _phrase_, agent_id, rev in fresh:
        revs[agent_id] = rev
    kept[session] = {"at": now, "revs": revs}
    return kept


def _announce(cache, session, now):
    """The line for this session's novelties, marking them as seen.

    Mutates `cache["seen"]` when there is something to announce and returns
    the line; returns None (and touches nothing) otherwise.
    """
    agents = cache.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    seen = cache.get("seen")
    seen = seen if isinstance(seen, dict) else {}
    previous = seen.get(session)
    revs = previous.get("revs") if isinstance(previous, dict) else None
    revs = revs if isinstance(revs, dict) else {}
    fresh = []
    for agent_id, agent in agents.items():
        if not isinstance(agent_id, str) or not isinstance(agent, dict):
            continue
        number = _number(agent.get("n"))
        rev = agent.get("rev")
        if number is None or not isinstance(rev, str) or not rev:
            continue
        if revs.get(agent_id) == rev:
            continue
        phrase = _phrase(agent.get("state"), number, agent.get("updated_at"),
                         now)
        if phrase is None:
            continue
        fresh.append((number, phrase, agent_id, rev))
    if not fresh:
        return None
    fresh.sort(key=lambda item: item[0])
    cache["seen"] = _seen_with(seen, session, fresh, now)
    parts = " · ".join(item[1] for item in fresh)
    return f"{PREFIX}{parts}{SUFFIX}"


def _spawn_refresh(cwd):
    """Start `crew refresh --cwd <cwd>` detached, and do not wait for it.

    A binary that cannot even start is silent: the line of this event is
    worth more than the refresh.
    """
    binary = os.environ.get(REFRESH_ENV) or os.path.join(_ROOT, "bin", "crew")
    try:
        subprocess.Popen(
            [binary, "refresh", "--cwd", cwd],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except OSError:
        pass


def _line(session, cwd):
    """The line to inject for this event, or None when there is nothing new."""
    project = project_of(cwd)
    if project is None:
        return None
    slug = project.slug
    now = time.time()
    with state._lock(f"{slug}.cache.lock", blocking=True):
        # Task 3's reader: a missing or corrupt cache comes back empty and
        # with refreshed_at 0, so it is stale and silent at once.
        cache = state._read(slug)
        stale = now - cache.get("refreshed_at", 0.0) > REFRESH_AFTER
        line = _announce(cache, session, now)
        if line is not None:
            state._write(slug, cache)
    if stale:
        _spawn_refresh(cwd)
    return line


def main():
    try:
        if sys.stdin.isatty():
            return 0
        event = _event(sys.stdin.read())
        if event is None:
            return 0
        session, cwd, name = event
        line = _line(session, cwd)
        if line is not None:
            payload = {"hookSpecificOutput": {"hookEventName": name,
                                              "additionalContext": line}}
            sys.stdout.write(json.dumps(payload) + "\n")
    except Exception:  # the hook must never fail the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
