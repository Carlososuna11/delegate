"""The bubblewrap sandbox the runner puts the request's checks in.

The delegate's child is sandboxed by bin/delegate (`--sandbox bwrap`). The
checks are started by the runner itself, after the child, and they run code
the delegate wrote (a package.json script, a test), so they get the same
profile, built here:

- /usr (and the /bin, /lib* links), /etc, the resolv.conf target read-only;
  a private /proc, a minimal /dev, a tmpfs /tmp;
- a private tmpfs HOME: nothing of the real home is there except what is
  bound on top of it below;
- the worktree read-write and the repository's gitdir read-only (mounted
  after the worktree, like the child's);
- user, mount, pid, ipc, uts and cgroup namespaces, --disable-userns,
  --new-session, --die-with-parent;
- the seccomp program of bin/delegate (dg_seccomp_bpf), byte for byte: no
  unix sockets, no datagram socketpairs, no io_uring, no foreign arch. A test
  compares both.

What a check needs from the home, read-only and nothing else:

- TOOLCHAIN_ROOTS that exist under HOME: interpreters and runtimes (nvm's
  node versions, uv's managed pythons, rustup toolchains, pyenv versions,
  cargo's and bun's bin). None of them holds credentials. Package CACHES
  (~/.npm, ~/.cache/uv, pnpm's store) are NOT bound: read-only they are
  useless (npm and uv write to them) and read-write a check could poison a
  cache the user's host tools later execute from, outside any sandbox. A
  check starts with an empty cache in the private HOME, and installs go to
  the network.
- The check's own executable when the host resolves it outside every mount
  above (a venv's `.venv/bin/pytest`, a tool in ~/bin): its venv when it is
  one (the directory holding pyvenv.cfg), else the file alone.
- DELEGATE_CHECK_RO: extra paths, os.pathsep-separated, that the machine's
  owner (never the request) decides to expose, e.g. ~/.cache/ms-playwright.

A bind is refused when it is, contains or lies inside SENSITIVE (ssh and gpg
keys, ~/.config — where the provider key dirs live —, cloud and registry
credentials, Claude's and Codex's state) or the delegate state dir, when it is
HOME or contains it, or when it contains the worktree. The list is a guard on
paths a caller or owner names; what is exposed by default is the allowlist.

The network is shared, as for the child: checks reach the internet (npm ci)
and TCP services on localhost (a test database); unix sockets do not work.
"""
from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

# Relative to HOME. Read-only, only if they exist.
TOOLCHAIN_ROOTS = (".nvm/versions", ".local/share/uv/python", ".rustup/toolchains",
                   ".cargo/bin", ".pyenv/versions", ".bun/bin")

# Relative to HOME. Never bound, never inside or around a bind.
SENSITIVE = (".ssh", ".gnupg", ".config", ".aws", ".azure", ".kube", ".docker",
             ".claude", ".claude.json", ".codex", ".netrc", ".npmrc", ".pypirc",
             ".git-credentials", ".gitconfig", ".password-store", ".pki",
             ".vault-token", ".local/share/keyrings", ".local/state", ".mozilla",
             ".bash_history", ".zsh_history", ".python_history")

_SYSTEM_DIRS = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
_ARCHES = {"x86_64": (0xC000003E, 41, 53), "aarch64": (0xC00000B7, 198, 199)}


class SandboxError(Exception):
    """The sandbox for the checks cannot be built as configured."""


def seccomp_bpf(machine: str) -> bytes:
    """The seccomp program of bin/delegate's dg_seccomp_bpf, for `machine`.

    Kept in step with the Bash one by test_sandbox's parity test: change both.
    """
    try:
        arch, nr_socket, nr_pair = _ARCHES[machine]
    except KeyError:
        raise SandboxError(f"no hay filtro seccomp para la arquitectura {machine}") from None
    nr_uring = 425
    ld, jeq, jge, and_, ret = 0x20, 0x15, 0x35, 0x54, 0x06
    allow, kill = 0x7FFF0000, 0x80000000
    eafnosupport, enosys = 0x00050000 | 97, 0x00050000 | 38
    prog = [
        (ld, 0, 0, 4),                    # 0 A = arch
        (jeq, 1, 0, arch),                # 1 native? -> 3
        (ret, 0, 0, kill),                # 2
        (ld, 0, 0, 0),                    # 3 A = nr
        (jge, 15, 0, 0x40000000),         # 4 x32 -> 20
        (jeq, 5, 0, nr_socket),           # 5 socket -> 11
        (jeq, 7, 0, nr_pair),             # 6 socketpair -> 14
        (jeq, 12, 0, nr_uring),           # 7 io_uring_setup -> 20
        (jeq, 11, 0, nr_uring + 1),       # 8 io_uring_enter -> 20
        (jeq, 10, 0, nr_uring + 2),       # 9 io_uring_register -> 20
        (ret, 0, 0, allow),               # 10
        (ld, 0, 0, 16),                   # 11 A = low word of args[0] (domain)
        (jeq, 5, 0, 1),                   # 12 AF_UNIX -> 18
        (ret, 0, 0, allow),               # 13
        (ld, 0, 0, 24),                   # 14 A = low word of args[1] (type)
        (and_, 0, 0, 15),                 # 15 drop SOCK_CLOEXEC / SOCK_NONBLOCK
        (jeq, 2, 0, 1),                   # 16 SOCK_STREAM -> 19
        (jeq, 1, 0, 5),                   # 17 SOCK_SEQPACKET -> 19
        (ret, 0, 0, eafnosupport),        # 18
        (ret, 0, 0, allow),               # 19
        (ret, 0, 0, enosys),              # 20
    ]
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _overlaps(a: str, b: str) -> bool:
    return _within(a, b) or _within(b, a)


class ChecksSandbox:
    """The bwrap prefix for every check of one run.

    Built once, before the child runs, so a bad DELEGATE_CHECK_RO fails the
    run (infra_failed) before a provider is paid. `wrap(argv, env)` gives the
    argv for one check: the prefix plus that check's own executable bind.
    """

    def __init__(self, bwrap: str, worktree: Path, gitdirs: list[str], home: str | None,
                 state_root: Path, machine: str, extra_ro: str = ""):
        self.bwrap = bwrap
        self.worktree = os.path.realpath(worktree)
        self.home = home if home and home.startswith("/") and home != "/" else None
        self.bpf = seccomp_bpf(machine)
        self.denied = [os.path.realpath(state_root)]
        if self.home:
            real_home = os.path.realpath(self.home)
            for rel in SENSITIVE:
                self.denied.append(os.path.join(real_home, rel))
                self.denied.append(os.path.join(self.home, rel))
        self.visible = [*_SYSTEM_DIRS, self.worktree]
        self.toolchain: list[tuple[str, str]] = []
        if self.home:
            for rel in TOOLCHAIN_ROOTS:
                nominal = os.path.join(self.home, rel)
                if os.path.isdir(nominal) and self._allowed(nominal) is None:
                    self.toolchain.append((os.path.realpath(nominal), nominal))
                    self.visible.append(nominal)
        self.extra: list[tuple[str, str]] = []
        for raw in (extra_ro or "").split(os.pathsep):
            if not raw:
                continue
            if not raw.startswith("/") or not os.path.exists(raw):
                raise SandboxError(f"DELEGATE_CHECK_RO: {raw!r} no es una ruta absoluta que exista")
            why = self._allowed(raw)
            if why:
                raise SandboxError(f"DELEGATE_CHECK_RO: no monto {raw}: {why}")
            self.extra.append((os.path.realpath(raw), raw))
            self.visible.append(raw)
        self.gitdirs = list(gitdirs)

    def _allowed(self, path: str) -> str | None:
        """None if `path` may be bound read-only; otherwise why not."""
        candidates = {os.path.abspath(path), os.path.realpath(path)}
        for cand in candidates:
            if cand == "/":
                return "es la raíz"
            if self.home:
                for home in {self.home, os.path.realpath(self.home)}:
                    if _within(home, cand):
                        return "es el HOME o lo contiene"
            for bad in self.denied:
                if _overlaps(cand, bad):
                    return f"se cruza con {bad}"
            if _within(self.worktree, cand):
                return "contiene el worktree"
        return None

    def _executable_bind(self, argv0: str, path_env: str) -> tuple[tuple[str, str] | None, str | None]:
        """The bind the check's executable needs: ((src, dest) or None, note)."""
        if "/" in argv0:
            if not argv0.startswith("/"):
                return None, None          # relative: inside the worktree
            found = argv0
        else:
            found = shutil.which(argv0, path=path_env)
            if not found:
                return None, None
            found = os.path.abspath(found)
        if any(_within(found, v) for v in self.visible):
            return None, None
        target = found
        bindir = os.path.dirname(found)
        if os.path.basename(bindir) == "bin" and \
                os.path.isfile(os.path.join(os.path.dirname(bindir), "pyvenv.cfg")):
            target = os.path.dirname(bindir)   # a venv: its scripts need its python
        why = self._allowed(target)
        if why:
            return None, f"delegate-run: el ejecutable {found} no se monta en el sandbox: {why}\n"
        return (os.path.realpath(target), target), None

    def wrap(self, argv: list[str], path_env: str, seccomp_fd: int) -> tuple[list[str], str | None]:
        """(full argv, note for the check's log or None) for one check."""
        out = [self.bwrap, "--unshare-all", "--share-net", "--unshare-user",
               "--disable-userns", "--die-with-parent", "--new-session",
               "--ro-bind", "/usr", "/usr"]
        for d in ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"):
            if os.path.islink(d):
                out += ["--symlink", os.readlink(d), d]
            elif os.path.isdir(d):
                out += ["--ro-bind", d, d]
        out += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp"]
        resolv = os.path.realpath("/etc/resolv.conf")
        if resolv.startswith("/run/") and os.path.isfile(resolv):
            out += ["--ro-bind", resolv, resolv]
        if self.home:
            out += ["--perms", "0700", "--tmpfs", self.home]
        for src, dest in self.toolchain + self.extra:
            out += ["--ro-bind", src, dest]
        bind, note = self._executable_bind(argv[0], path_env)
        if bind:
            out += ["--ro-bind", bind[0], bind[1]]
        out += ["--bind", self.worktree, self.worktree]
        for p in self.gitdirs:
            out += ["--ro-bind", p, p]
        out += ["--setenv", "TMPDIR", "/tmp", "--seccomp", str(seccomp_fd),
                "--chdir", self.worktree, "--", *argv]
        return out, note
