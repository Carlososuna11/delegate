"""Parse and validate a delegate-run request (JSON on stdin).

The request is the only input `delegate-run` trusts. Everything is checked
here, once, before a worktree exists: a request that cannot run cleanly is
refused with a `RequestError`, never half-run. The job of this module is to
produce a `Request` that the runner can consume without re-checking types.

Nothing here touches the filesystem, the network, the environment or a
provider key.
"""
from __future__ import annotations

import json
import os
import re

from .contract import GRANTS, SCHEMA_VERSION, Request, RequestError
from .scope import ScopeError, compile_glob

# Every key a request may carry. Anything else is refused by name so a typo
# never silently becomes a default.
_KNOWN_KEYS = frozenset({
    "v", "provider", "account", "repo", "base", "grants", "mcp",
    "timeout", "task", "checks", "scope", "snapshot", "wait",
})

# Keys the dataclass has no default for: they must be present.
_REQUIRED_KEYS = ("v", "provider", "repo", "task")

# Provider names and MCP server ids share the same shape.
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Same rule as `delegate` (lib/accounts.sh, _DG_ACCOUNT_NAME_RE): refusing here
# keeps a doomed request from ever taking a slot or creating a worktree.
_ACCOUNT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

TIMEOUT_MIN = 60
TIMEOUT_MAX = 14400
BASE_MAX = 200
TASK_MAX = 100000
CHECKS_MAX = 20
SCOPE_MAX = 50

DEFAULT_BASE = "HEAD"
DEFAULT_TIMEOUT = 1800


def _describe(value) -> str:
    """A JSON-flavoured type name, for messages a human reads."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "booleano"
    if isinstance(value, (int, float)):
        return "número"
    if isinstance(value, str):
        return "texto"
    if isinstance(value, list):
        return "lista"
    if isinstance(value, dict):
        return "objeto"
    return type(value).__name__


def _is_int(value) -> bool:
    """True only for a real integer: bool is not an int here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _load(raw) -> dict:
    """Decode and decode JSON: bytes or str in, object out."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RequestError(f"la petición no es UTF-8 válido: {exc}") from None
    elif isinstance(raw, str):
        text = raw
    else:
        raise RequestError(
            f"la petición debe ser texto o bytes, no {_describe(raw)}")

    if not text.strip():
        raise RequestError("petición vacía: se esperaba un objeto JSON por stdin")

    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise RequestError(f"JSON inválido: {exc}") from None

    if not isinstance(obj, dict):
        raise RequestError(f"la petición debe ser un objeto JSON, no {_describe(obj)}")
    return obj


def _no_duplicates(pairs: list) -> dict:
    """json.loads keeps the last of two equal keys; a request must not be ambiguous."""
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise RequestError(f"clave repetida en la petición: {key!r}")
        obj[key] = value
    return obj


def _text(obj: dict, key: str) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise RequestError(f"'{key}' debe ser texto, no {_describe(value)}")
    return value


def _text_list(obj: dict, key: str) -> list[str]:
    value = obj[key]
    if not isinstance(value, list):
        raise RequestError(f"'{key}' debe ser una lista, no {_describe(value)}")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise RequestError(
                f"'{key}'[{index}] debe ser texto, no {_describe(item)}")
    return value


def _flag(obj: dict, key: str, default: bool) -> bool:
    value = obj.get(key, default)
    if not isinstance(value, bool):
        raise RequestError(f"'{key}' debe ser true o false, no {_describe(value)}")
    return value


def _check_version(obj: dict) -> None:
    version = obj["v"]
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise RequestError(f"'v' debe ser exactamente {SCHEMA_VERSION}")


def _check_provider(obj: dict) -> str:
    provider = _text(obj, "provider")
    if not _ID_PATTERN.match(provider):
        raise RequestError(
            "'provider' debe ser un identificador en minúsculas "
            "(empieza con letra; letras, dígitos y '-'; hasta 32 caracteres), "
            f"recibido {provider!r}")
    return provider


def _check_account(obj: dict) -> str | None:
    account = obj.get("account")
    if account is None:
        return None
    if not isinstance(account, str):
        raise RequestError(
            f"'account' debe ser texto o null, no {_describe(account)}")
    if not _ACCOUNT_PATTERN.match(account):
        raise RequestError(
            "'account' debe empezar con minúscula o dígito y tener hasta 32 "
            f"caracteres de [a-z0-9_-], recibido {account!r}")
    return account


def _check_repo(obj: dict) -> str:
    repo = _text(obj, "repo")
    if "\0" in repo:
        raise RequestError("'repo' no puede contener un byte NUL")
    if not repo:
        raise RequestError("'repo' no puede estar vacío")
    if not os.path.isabs(repo):
        raise RequestError(f"'repo' debe ser una ruta absoluta, recibido {repo!r}")
    return repo


def _check_base(obj: dict) -> str:
    base = obj.get("base", DEFAULT_BASE)
    if not isinstance(base, str):
        raise RequestError(f"'base' debe ser texto, no {_describe(base)}")
    if not base:
        raise RequestError("'base' no puede estar vacío")
    if len(base) > BASE_MAX:
        raise RequestError(f"'base' no puede pasar de {BASE_MAX} caracteres")
    if base.startswith("-"):
        raise RequestError(f"'base' no puede empezar con '-', recibido {base!r}")
    # isspace() does not cover NUL or the other C0 controls; a NUL would cut
    # the argument git sees, so every control character is refused.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7f
           for char in base):
        raise RequestError(
            f"'base' no puede tener espacios ni caracteres de control, recibido {base!r}")
    return base


def _check_grants(obj: dict) -> tuple[str, ...]:
    if "grants" not in obj:
        return ()
    raw = _text_list(obj, "grants")
    seen: set[str] = set()
    for index, grant in enumerate(raw):
        if grant not in GRANTS:
            raise RequestError(
                f"'grants'[{index}]={grant!r} no es un grant válido; "
                f"permitidos: {', '.join(GRANTS)}")
        if grant in seen:
            raise RequestError(f"'grants' repite {grant!r}")
        seen.add(grant)
    return tuple(raw)


def _check_mcp(obj: dict) -> tuple[str, ...]:
    if "mcp" not in obj:
        return ()
    raw = _text_list(obj, "mcp")
    seen: set[str] = set()
    for index, server in enumerate(raw):
        if not _ID_PATTERN.match(server):
            raise RequestError(
                f"'mcp'[{index}]={server!r} no es un id válido: empieza con "
                "letra; letras, dígitos y '-'; hasta 32 caracteres")
        if server in seen:
            raise RequestError(f"'mcp' repite {server!r}")
        seen.add(server)
    return tuple(raw)


def _check_timeout(obj: dict) -> int:
    timeout = obj.get("timeout", DEFAULT_TIMEOUT)
    if not _is_int(timeout):
        raise RequestError(
            f"'timeout' debe ser un entero, no {_describe(timeout)}")
    if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise RequestError(
            f"'timeout' debe estar entre {TIMEOUT_MIN} y {TIMEOUT_MAX} "
            f"segundos, recibido {timeout}")
    return timeout


def _check_task(obj: dict) -> str:
    task = _text(obj, "task")
    if not task:
        raise RequestError("'task' no puede estar vacía")
    if len(task) > TASK_MAX:
        raise RequestError(
            f"'task' no puede pasar de {TASK_MAX} caracteres")
    if "\0" in task:
        raise RequestError("'task' no puede contener un byte NUL")
    return task


def _check_checks(obj: dict) -> tuple[tuple[str, ...], ...]:
    if "checks" not in obj:
        return ()
    raw = obj["checks"]
    if not isinstance(raw, list):
        raise RequestError(f"'checks' debe ser una lista, no {_describe(raw)}")
    if len(raw) > CHECKS_MAX:
        raise RequestError(f"'checks' no puede pasar de {CHECKS_MAX} entradas")
    checks: list[tuple[str, ...]] = []
    for index, argv in enumerate(raw):
        if not isinstance(argv, list):
            raise RequestError(
                f"'checks'[{index}] debe ser una lista de argumentos (argv), "
                f"no {_describe(argv)}: los checks nunca se pasan como una "
                "cadena de shell")
        if not argv:
            raise RequestError(
                f"'checks'[{index}] está vacío: hace falta al menos el comando")
        for position, arg in enumerate(argv):
            if not isinstance(arg, str):
                raise RequestError(
                    f"'checks'[{index}][{position}] debe ser texto, "
                    f"no {_describe(arg)}")
            if not arg:
                raise RequestError(
                    f"'checks'[{index}][{position}] no puede estar vacío")
            if "\0" in arg:
                raise RequestError(
                    f"'checks'[{index}][{position}] no puede contener NUL")
        checks.append(tuple(argv))
    return tuple(checks)


def _check_scope(obj: dict) -> tuple[str, ...]:
    if "scope" not in obj:
        return ()
    raw = _text_list(obj, "scope")
    if len(raw) > SCOPE_MAX:
        raise RequestError(f"'scope' no puede pasar de {SCOPE_MAX} entradas")
    for index, pattern in enumerate(raw):
        if not pattern:
            raise RequestError(f"'scope'[{index}] no puede estar vacío")
        if "\0" in pattern:
            raise RequestError(f"'scope'[{index}] no puede contener NUL")
        if os.path.isabs(pattern):
            raise RequestError(
                f"'scope'[{index}]={pattern!r} debe ser relativo a la raíz "
                "del repo")
        if ".." in pattern.split("/"):
            raise RequestError(
                f"'scope'[{index}]={pattern!r} no puede salir del repo ('..')")
        try:
            compile_glob(pattern)
        except ScopeError as exc:
            raise RequestError(f"'scope'[{index}]: {exc}") from None
    return tuple(raw)


def parse_request(raw: bytes | str) -> Request:
    """Return a validated Request or raise RequestError. Never runs anything."""
    obj = _load(raw)

    unknown = sorted(key for key in obj if key not in _KNOWN_KEYS)
    if unknown:
        raise RequestError(f"claves desconocidas: {', '.join(unknown)}")

    missing = [key for key in _REQUIRED_KEYS if key not in obj]
    if missing:
        raise RequestError(f"faltan claves obligatorias: {', '.join(missing)}")

    _check_version(obj)

    return Request(
        provider=_check_provider(obj),
        repo=_check_repo(obj),
        task=_check_task(obj),
        base=_check_base(obj),
        account=_check_account(obj),
        grants=_check_grants(obj),
        mcp=_check_mcp(obj),
        timeout=_check_timeout(obj),
        checks=_check_checks(obj),
        scope=_check_scope(obj),
        snapshot=_flag(obj, "snapshot", False),
        wait=_flag(obj, "wait", True),
    )
