"""Identify the GitHub project of a directory.

The owner of `origin` is what picks the destination in the personal config
(`~/.config/crew/config.json`); the slug is the label every agent of the
project carries. Only github.com origins are recognized: an organization key
means a GitHub owner, so a GitLab remote must not match one by accident.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# `git@github.com:owner/repo(.git)`, `https://github.com/owner/repo(.git)`,
# `ssh://git@github.com/owner/repo` and the same without a user or scheme.
_REMOTE_RE = re.compile(
    r"\A(?:(?:https?|ssh|git)://)?(?:[^@/]+@)?github\.com[:/]"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]*)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")


@dataclass(frozen=True)
class Project:
    owner: str   # GitHub owner, as the config spells it
    repo: str
    slug: str    # `owner.repo` in lowercase: the crew-project label


def parse_remote(url: str | None) -> Project | None:
    """Parse a GitHub remote URL; None for anything else."""
    match = _REMOTE_RE.match(url or "")
    if not match:
        return None
    owner, repo = match.group("owner"), match.group("repo")
    if owner in (".", "..") or repo in (".", ".."):
        return None
    return Project(owner=owner, repo=repo, slug=f"{owner}.{repo}".lower())


def project_of(cwd: str) -> Project | None:
    """The project of the repository at `cwd`, from `git remote get-url origin`."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_remote(proc.stdout.strip())
