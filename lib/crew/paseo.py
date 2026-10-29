"""Talk to the remote agent runtime over ssh.

Every remote command line is fixed text plus values that were validated first
(agent ids match `ID_RE`) or quoted with `shlex.quote`. Text that can be
arbitrary — the brief, an answer — travels on ssh's stdin and is read on the
remote side with `"$(cat)"`, behind a `--` so it is never taken for an option.
That puts it in the remote argv (visible in the host's `ps`) and strips
trailing newlines, which is why briefs carry no secrets: lib/crew/brief.py
refuses them before they get here.

The remote `run --no-wait -q` prints only the agent id on stdout and writes
its notices ("Created workspace ...", "Tip: ...") on stderr. That stderr is
ignored; the id is the last stdout line shaped like an id.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid

# Same shape `lib/delegate_run/remote.py` accepts: an ssh_config alias,
# optionally user@, never an option and never something ssh reads as host:path.
HOST_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}"
                     r"(?:@[A-Za-z0-9_][A-Za-z0-9_.-]{0,252})?\Z")
ID_RE = re.compile(r"\A[0-9a-f-]{7,36}\Z")
# Rest of a `~/...` destination: plain path characters, no `..` segment.
TILDE_PATH_RE = re.compile(r"\A[A-Za-z0-9._/-]+\Z")
# A title starts with the assignment number: "#4 · algo".
NUMBER_RE = re.compile(r"\A#(\d+) · ")

# Copied from lib/delegate_run/remote.py on purpose (that module is not
# imported nor modified): no agent, no X11, no forwarding, no local command.
SSH_OPTIONS = (
    "-T",
    "-a",
    "-x",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "PermitLocalCommand=no",
)

LAUNCH_TIMEOUT = 60
RECONCILE_SECONDS = 5.0
EMPTY_BRIEF = "el brief está vacío; escribe el encargo antes de lanzarlo"


class CrewError(Exception):
    """A failure the CLI reports and stops on. Carries the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class LocalExecError(CrewError):
    """The local ssh process never started, so nothing reached the host."""


def check_host(host: str) -> str:
    if not isinstance(host, str) or not HOST_RE.match(host):
        raise CrewError(f"host inválido: {host!r} (un alias de ssh_config o "
                        "user@host, sin ':' ni '/', y sin empezar con '-')", 2)
    return host


def ssh_prefix() -> list[str]:
    """The ssh executable plus its hardened options.

    CREW_SSH (tests, or another transport) must be an absolute path, like
    DELEGATE_REMOTE_SSH in delegate-run.
    """
    raw = os.environ.get("CREW_SSH") or ""
    if raw:
        if not os.path.isabs(raw):
            raise CrewError("CREW_SSH tiene que ser una ruta absoluta", 2)
        return [raw, *SSH_OPTIONS]
    found = shutil.which("ssh")
    if not found:
        raise CrewError("no encuentro ssh en el PATH", 2)
    return [found, *SSH_OPTIONS]


def quote_cwd(cwd: str) -> str:
    """Quote a remote working directory, expanding `~/` on the host.

    `shlex.quote("~/x")` would produce `'~/x'`, and the tilde would never
    expand: the directory has to stay a word that starts with `~/`. So only
    the rest is quoted, and it is validated first (plain path characters, no
    `..`). Absolute paths are quoted whole.
    """
    if not isinstance(cwd, str) or not cwd:
        raise CrewError("el destino del proyecto no es una ruta válida", 2)
    if cwd.startswith("~/"):
        rest = cwd[2:]
        if not TILDE_PATH_RE.match(rest) or ".." in rest.split("/"):
            raise CrewError(f"destino inválido en la config: {cwd!r}", 2)
        return "~/" + shlex.quote(rest)
    if not cwd.startswith("/"):
        raise CrewError(f"destino inválido en la config: {cwd!r} "
                        "(tiene que ser una ruta absoluta o empezar con ~/)", 2)
    return shlex.quote(cwd)


def check_brief(brief: str) -> None:
    """Refuse a brief with nothing in it: there is no assignment to send."""
    if not isinstance(brief, str) or not brief.strip():
        raise CrewError(EMPTY_BRIEF, 2)


def _quote_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not ID_RE.match(agent_id):
        raise CrewError(f"id de encargo inválido: {agent_id!r}", 2)
    return shlex.quote(agent_id)


def next_number(agents: list[dict]) -> int:
    """One more than the highest `#n` in the titles; 1 when there are none."""
    highest = 0
    for agent in agents:
        name = agent.get("name")
        if isinstance(name, str):
            match = NUMBER_RE.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def resolve(agents: list[dict], n: int) -> dict:
    """The single agent titled `#<n> · ...`."""
    matches = [agent for agent in agents
               if isinstance(agent.get("name"), str)
               and agent["name"].startswith(f"#{n} · ")]
    if not matches:
        raise CrewError(f"no hay encargo #{n} en este proyecto", 2)
    if len(matches) > 1:
        if len(matches) == 2:
            raise CrewError(f"hay dos encargos #{n}; usa el id", 2)
        raise CrewError(f"hay {len(matches)} encargos #{n}; usa el id", 2)
    return matches[0]


class Paseo:
    """The remote runtime, reached only through ssh."""

    def __init__(self, host: str, ssh: list[str]):
        self.host = check_host(host)
        self.ssh = list(ssh)

    def run(self, cmd: str, stdin: str | None = None, timeout: int = 20) -> str:
        """Run one remote command; return its stdout. Stderr is discarded."""
        argv = [*self.ssh, "--", self.host, cmd]
        try:
            proc = subprocess.run(
                argv, input=(stdin or "").encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CrewError("el host no respondió a tiempo", 1) from None
        except OSError as exc:
            # The request never left: the ssh executable did not start at all.
            raise LocalExecError(f"no se pudo ejecutar ssh: {exc}", 2) from exc
        if proc.returncode != 0:
            raise CrewError(f"el host remoto falló (código {proc.returncode})", 1)
        return proc.stdout.decode("utf-8", "replace")

    def _ls_label(self, label: str) -> list[dict]:
        out = self.run(f"paseo ls -g -a --label {shlex.quote(label)} --json")
        try:
            agents = json.loads(out)
        except ValueError:
            raise CrewError("el host no devolvió la lista de encargos", 1) from None
        if not isinstance(agents, list):
            raise CrewError("el host no devolvió la lista de encargos", 1)
        return [agent for agent in agents if isinstance(agent, dict)]

    def ls(self, slug: str) -> list[dict]:
        """Every agent labelled with the project."""
        if not isinstance(slug, str) or not slug:
            raise CrewError("proyecto inválido", 2)
        return self._ls_label(f"crew-project={slug}")

    def launch(self, title: str, cwd: str, slug: str, brief: str) -> str:
        """Start an agent with `brief` as its prompt; return its id.

        The brief goes on ssh's stdin and is read on the host with `"$(cat)"`,
        behind `--` so a brief that starts with a dash is a plain word and not
        an option of the runtime. The request is labelled with a fresh uuid so
        a dropped connection can be reconciled: the agent may exist even when
        its id never came back. A failure to start ssh locally is different:
        nothing reached the host, so there is nothing to reconcile.
        """
        check_brief(brief)
        request = str(uuid.uuid4())
        cmd = ("paseo run --no-wait -q --provider claude --mode auto "
               f"--title {shlex.quote(title)} --cwd {quote_cwd(cwd)} "
               f"--label crew=1 --label crew-project={shlex.quote(slug)} "
               f"--label crew-req={shlex.quote(request)} -- \"$(cat)\"")
        try:
            out = self.run(cmd, stdin=brief, timeout=LAUNCH_TIMEOUT)
        except LocalExecError:
            raise
        except CrewError:
            found = self._find_by_request(request)
            if found:
                return found
            raise
        agent_id = _last_id(out)
        if agent_id:
            return agent_id
        found = self._find_by_request(request)
        if found:
            return found
        raise CrewError("el host no devolvió el id del encargo", 1)

    def _find_by_request(self, request: str) -> str | None:
        """Look for the agent labelled with `crew-req=<uuid>`, retrying briefly."""
        deadline = time.monotonic() + RECONCILE_SECONDS
        while True:
            try:
                agents = self._ls_label(f"crew-req={request}")
            except CrewError:
                return None  # the host is unreachable: report the original failure
            for agent in agents:
                agent_id = agent.get("id")
                if isinstance(agent_id, str) and ID_RE.match(agent_id):
                    return agent_id
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def logs(self, agent_id: str, tail: int) -> str:
        """The last `tail` messages of the agent, as text."""
        if not isinstance(tail, int) or tail <= 0:
            raise CrewError("tail inválido", 2)
        return self.run(f"paseo logs {_quote_id(agent_id)} --tail {tail} --filter text")

    def send(self, agent_id: str, text: str) -> str:
        """Deliver `text` to the agent without waiting for its turn."""
        return self.run(f"paseo send --no-wait {_quote_id(agent_id)} \"$(cat)\"",
                        stdin=text, timeout=LAUNCH_TIMEOUT)

    def stop(self, agent_id: str) -> str:
        return self.run(f"paseo stop {_quote_id(agent_id)}")


def _last_id(stdout: str) -> str | None:
    """The last stdout line shaped like an agent id (notices go on stderr)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if ID_RE.match(line):
            return line
    return None
