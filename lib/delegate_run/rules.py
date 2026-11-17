"""Which code-rule packs a delegate-run request gets, and what it records.

`delegate` owns the rules: it validates each pack (files, permissions, sha,
accounts) and refuses a bad one with exit 2 before reading the key. This module
only CHOOSES: when a request does not say `rules`, an edit run gets the packs
whose `remotes` pattern matches the repository's `remote.origin.url`. It reads
the packs' metadata, never their text, and never a key.

A pack the request names is passed as is, even if this machine cannot read it:
`delegate` is the one that refuses it, visibly. A pack this module cannot parse
is never auto-selected, and that is said on stderr.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from .contract import Request
from .request import _ID_PATTERN

_CORE = Path(__file__).resolve().parents[2] / "rules" / "core.md"
_PACK_KEYS = frozenset({"accounts", "remotes", "source", "sha256"})


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


def read_pack(pack_id: str) -> dict | None:
    """The metadata of one pack as {"accounts": [...], "remotes": [...],
    "sha256": str}, or None when it is missing or does not parse."""
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
        if not sep or "\t" in value or not value or key not in _PACK_KEYS or key in meta:
            return None
        meta[key] = value
    if "accounts" not in meta or "sha256" not in meta:
        return None
    try:
        remotes = [re.compile(p) for p in meta.get("remotes", "").split(",") if p]
    except re.error:
        return None
    return {"accounts": meta["accounts"].split(","), "remotes": remotes,
            "sha256": meta["sha256"]}


def _pack_ids() -> list[str]:
    try:
        entries = sorted(os.listdir(rules_dir()))
    except OSError:
        return []
    return [e for e in entries if _ID_PATTERN.match(e) and (rules_dir() / e).is_dir()]


def auto_select(origin: str | None, account: str | None) -> tuple[str, ...]:
    """Packs whose `remotes` match <origin>. With a known <account>, only the
    packs that account may use: selection never sets up a refusal. With no
    account (delegate picks its default), delegate checks the one it picks."""
    if not origin:
        return ()
    chosen = []
    for pack_id in _pack_ids():
        meta = read_pack(pack_id)
        if meta is None:
            _warn(f"el paquete de reglas {pack_id} no se pudo leer: no se selecciona "
                  f"(delegate rules check {pack_id})")
            continue
        if not any(p.search(origin) for p in meta["remotes"]):
            continue
        if account is not None and account not in meta["accounts"]:
            continue
        chosen.append(pack_id)
    return tuple(chosen)


def effective(req: Request, origin: str | None) -> tuple[str, ...]:
    """What the run passes as --rules: the request's own list when it has one
    (`[]` included), else the automatic selection, and that only on edit runs."""
    if req.rules is not None:
        return req.rules
    if "edit" not in req.grants:
        return ()
    return auto_select(origin, req.account)


def summary(rules: tuple[str, ...], grants) -> dict:
    """result.json's `rules`: the core's sha on edit runs, and each pack's
    pinned sha (None when this machine has no readable pack by that id)."""
    packs = {}
    for pack_id in rules:
        meta = read_pack(pack_id)
        packs[pack_id] = meta["sha256"] if meta else None
    return {"core": core_sha() if "edit" in grants else None, "packs": packs}
