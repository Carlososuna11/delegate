"""Which code-rule packs a delegate-run request gets, and what it records.

`delegate` owns the rules: it validates each pack (files, permissions, sha,
accounts) and refuses a bad one with exit 2 before reading the key. This module
only CHOOSES: when a request does not say `rules`, an edit run gets the packs
whose `remotes` pattern matches the repository's `remote.origin.url`. It reads
the packs' metadata, never their text, and never a key.

A pack the request names is passed as is, even if this machine cannot read it:
`delegate` is the one that refuses it, visibly. A pack whose metadata
`delegate` would refuse (the same format checks) is never auto-selected, and
that is said on stderr. A pack that passes them but fails a check this module
does not repeat (permissions, the text no longer matching its sha) is
selected, and `delegate` fails the run closed with exit 2: never a run with
rules nobody reviewed.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

from . import contract
from .contract import Request
from .request import _ACCOUNT_PATTERN, _ID_PATTERN

_CORE = Path(__file__).resolve().parents[2] / "rules" / "core.md"
_PACK_KEYS = frozenset({"accounts", "remotes", "source", "sha256"})
# Same checks as lib/rules.sh (_DG_RULES_RE_REMOTE, _DG_RULES_RE_POSIX_CLASS,
# _DG_RULES_RE_SHA): a pack delegate would refuse is never chosen here.
_REMOTE_CHARSET = re.compile(r"^[\]A-Za-z0-9._:/@^$|()?+*\[-]+$")
_POSIX_CLASS = re.compile(r"\[:[^]]*:\]|\[\.[^]]*\.\]")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def rules_dir() -> Path:
    """$DELEGATE_RULES_DIR, else ~/.config/delegate/rules.d (same as delegate)."""
    raw = os.environ.get("DELEGATE_RULES_DIR")
    if raw:
        return Path(raw)
    return Path(os.environ.get("HOME", "")) / ".config" / "delegate" / "rules.d"


def core_sha() -> str | None:
    """sha256 of the core sheet installed next to this runner."""
    try:
        return hashlib.sha256(_CORE.read_bytes()).hexdigest()
    except OSError:
        return None


def _warn(message: str) -> None:
    try:
        sys.stderr.write(f"delegate-run: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _compile_remotes(value: str) -> list | None:
    if not _REMOTE_CHARSET.match(value) or _POSIX_CLASS.search(value):
        return None
    patterns = value.split(",")
    if not all(patterns):
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            return [re.compile(p) for p in patterns]
    except (re.error, FutureWarning, DeprecationWarning):
        return None


def read_pack(pack_id: str) -> dict | None:
    """The metadata of one pack as {"accounts": [...], "remotes": [...],
    "sha256": str}, or None when it is missing or fails one of delegate's
    format checks."""
    if not _ID_PATTERN.match(pack_id):
        return None
    path = rules_dir() / pack_id / "pack"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("\t")
        if (not sep or "\t" in value or not value or _CONTROL.search(value)
                or key not in _PACK_KEYS or key in meta):
            return None
        meta[key] = value
    if "accounts" not in meta or not _SHA.match(meta.get("sha256", "")):
        return None
    accounts = meta["accounts"].split(",")
    if not all(_ACCOUNT_PATTERN.match(a) for a in accounts):
        return None
    remotes = _compile_remotes(meta["remotes"]) if "remotes" in meta else []
    if remotes is None:
        return None
    return {"accounts": accounts, "remotes": remotes, "sha256": meta["sha256"]}


def _pack_ids() -> list[str]:
    try:
        entries = sorted(os.listdir(rules_dir()))
    except OSError:
        return []
    return [e for e in entries if _ID_PATTERN.match(e) and (rules_dir() / e).is_dir()]


def default_account(provider: str) -> str | None:
    """The account `delegate <provider>` uses when none is passed, asked to
    delegate itself (`delegate account <provider> current`, which reads no
    key), so the answer is its own and not a copy of its rules. None when it
    cannot say."""
    name = os.environ.get("DELEGATE_BIN") or "delegate"
    exe = name if os.path.isabs(name) else (None if "/" in name else contract.which(name))
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "account", provider, "current"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    if proc.returncode != 0 or not _ACCOUNT_PATTERN.match(out):
        return None
    return out


def auto_select(origin: str | None, account: str | None, provider: str) -> tuple[str, ...]:
    """Packs whose `remotes` match <origin> and that the run's account may
    use, so selection never sets up a refusal. With no <account>, the one
    delegate will fall back to; when that is unknown, nothing is selected."""
    if not origin:
        return ()
    matched = []
    for pack_id in _pack_ids():
        meta = read_pack(pack_id)
        if meta is None:
            _warn(f"el paquete de reglas {pack_id} no pasa las comprobaciones de delegate: "
                  f"no se selecciona (delegate rules check {pack_id})")
            continue
        if any(p.search(origin) for p in meta["remotes"]):
            matched.append((pack_id, meta))
    if not matched:
        return ()
    if account is None:
        account = default_account(provider)
        if account is None:
            _warn(f"no sé qué cuenta va a usar delegate {provider}: no se elige ningún "
                  "paquete de reglas (pasa account en la petición)")
            return ()
    return tuple(pack_id for pack_id, meta in matched if account in meta["accounts"])


def effective(req: Request, origin: str | None) -> tuple[str, ...]:
    """What the run passes as --rules: the request's own list when it has one
    (`[]` included), else the automatic selection, and that only on edit runs."""
    if req.rules is not None:
        return req.rules
    if "edit" not in req.grants:
        return ()
    return auto_select(origin, req.account, req.provider)


def summary(rules: tuple[str, ...], grants) -> dict:
    """result.json's `rules`: the core's sha on edit runs, and each pack's
    pinned sha (None when this machine has no readable pack by that id)."""
    packs = {}
    for pack_id in rules:
        meta = read_pack(pack_id)
        packs[pack_id] = meta["sha256"] if meta else None
    return {"core": core_sha() if "edit" in grants else None, "packs": packs}
