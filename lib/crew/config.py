"""The personal destinations of crew: host and one remote cwd per GitHub owner.

The file lives outside the repo, in `$CREW_CONFIG` or
`~/.config/crew/config.json`:
`{"host": "caracas", "orgs": {"<owner>": "<remote cwd>"}}`.
An owner missing from `orgs` means "this project is not going to the host".

Both the CLI and the hook read it, so it is a small module of its own. Owners
are compared with `casefold`: GitHub owners are case-insensitive, and a config
that spells one differently must still match.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paseo import CrewError

DEFAULT_HOST = "caracas"
CONFIG_ENV = "CREW_CONFIG"
NO_DESTINATION = "este proyecto no tiene destino en caracas"


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


def destination_for(config: dict, owner: str) -> str | None:
    """The remote cwd configured for `owner`, matched case-insensitively."""
    if not isinstance(owner, str):
        return None
    wanted = owner.casefold()
    orgs = config.get("orgs") if isinstance(config, dict) else None
    if not isinstance(orgs, dict):
        return None
    for key, destination in orgs.items():
        if isinstance(key, str) and key.casefold() == wanted:
            return destination if isinstance(destination, str) else None
    return None
