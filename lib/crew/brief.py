"""Build the brief for one assignment and refuse anything secret-shaped.

A brief is the whole text an agent receives: Carlos's words quoted, the repo
and base branch, the decisions already made in the chat, the project rules, the
instruction to read the repo's `CLAUDE.md`, and the behaviour contract last.

The secret patterns are a backstop, not the rule: the skill and the contract
also forbid copying secrets or `.env` contents. They scan the **complete** text
that will travel — `build_brief` checks its own output — and the error names
the pattern, never the matched value.
"""
from __future__ import annotations

import re
from pathlib import Path

# Resolved from this file, never from the cwd: the CLI may run anywhere.
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "crew" / "contract.md"

# (name shown in the error, compiled pattern). Keep them in one place so the
# error can say which backstop fired without echoing the value.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("token de GitHub", re.compile(r"ghp_|gho_|github_pat_")),
    ("clave de API", re.compile(r"sk-[A-Za-z0-9-]{20,}")),
    ("clave de AWS", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("clave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("URL con credenciales", re.compile(r"\w+://[^\s/:@]+:[^\s/@]+@")),
    # The composite pattern below needs something before DATABASE_URL, so a
    # bare assignment would slip through; this backstop covers it.
    ("URL de base de datos", re.compile(r"\bDATABASE_URL\s*[:=]\s*\S+")),
    ("asignación de credencial", re.compile(
        r"\b[A-Z][A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|DATABASE_URL)"
        r"\s*[:=]\s*\S+")),
]


class SecretInBrief(ValueError):
    """Raised when a brief contains something shaped like a secret."""


def check_secrets(text: str) -> None:
    """Reject `text` if any secret pattern matches; never echo the value."""
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise SecretInBrief(
                f"El texto contiene algo con forma de secreto (patrón: {name}). "
                "No incluyas secretos ni contenido de .env: si hace falta una "
                "credencial, deja el nombre de la variable, no el valor."
            )


def load_contract() -> str:
    """Return `crew/contract.md`, resolved relative to this package."""
    return _CONTRACT_PATH.read_text(encoding="utf-8")


def _quote(text: str) -> str:
    """Quote every line so the dictation reads as Carlos's words, not orders."""
    lines = text.strip("\n").splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _bullets(items: list[str]) -> str:
    if not items:
        return "Ninguna."
    return "\n".join(f"- {item}" for item in items)


def build_brief(words: str, project: str, base: str, decisions: list[str],
                constraints: list[str], contract: str) -> str:
    """Assemble the full brief and refuse it if anything looks like a secret."""
    brief = "\n".join([
        "# Encargo",
        "",
        "## Lo que pidió Carlos",
        "",
        _quote(words),
        "",
        "## Repo y rama base",
        "",
        f"- Proyecto: {project}",
        f"- Rama base: {base}",
        "",
        "## Decisiones ya tomadas en el chat",
        "",
        _bullets(decisions),
        "",
        "## Reglas del proyecto",
        "",
        _bullets(constraints),
        "",
        "Antes de escribir código, lee el `CLAUDE.md` del repo y sigue sus reglas. "
        "Si algo de este brief choca con ese archivo, pregunta antes de seguir.",
        "",
        "## Contrato de trabajo",
        "",
        contract.strip(),
        "",
    ])
    check_secrets(brief)
    return brief
