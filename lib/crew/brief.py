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
    # A bare mention ("los tokens ghp_ no se copian") has no value body, so the
    # prefix alone is not enough: these patterns need the token characters.
    ("token de GitHub", re.compile(
        r"\b(?:gh[pous]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    # The lookbehind keeps hyphenated prose ("my-sk-learn-...") from reading as
    # a key; a real `sk-...` value is never glued to a preceding word.
    ("clave de API", re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{20,}")),
    ("clave de AWS", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("clave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("URL con credenciales", re.compile(r"\w+://[^\s/:@]+:[^\s/@]+@")),
    # Keyword anywhere in the name, any case, at the start of the name too:
    # `API_KEY="x"`, `password=hunter2`, `AWS_SECRET_ACCESS_KEY=abc`,
    # `DATABASE_URL=...`. A mention without `:` or `=` still passes.
    ("asignación de credencial", re.compile(
        r"(?i)\b(?:[A-Za-z0-9_]*_)?"
        r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|DATABASE_URL)"
        r"[A-Za-z0-9_]*\s*[:=]\s*\S+")),
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


def _flat(text: str) -> str:
    """Collapse every run of whitespace to one space and strip the ends.

    Structured fields go into the brief as a single line, so they cannot inject
    headings, list items or markers of their own.
    """
    return re.sub(r"\s+", " ", text).strip()


def _bullets(items: list[str]) -> str:
    if not items:
        return "Ninguna."
    return "\n".join(f"- {_flat(item)}" for item in items)


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
        f"- Proyecto: {_flat(project)}",
        f"- Rama base: {_flat(base)}",
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
