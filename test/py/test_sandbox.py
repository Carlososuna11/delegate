"""The OS sandbox of delegate-run: DELEGATE_SANDBOX and what bwrap confines.

Two layers:

- SandboxModeTest drives the runner with the fake delegate: which mode is
  chosen, what is recorded in result.json, and which flags reach `delegate`.
- SandboxConfinementTest runs the REAL bin/delegate, with a fake `claude`
  that probes the sandbox from the inside and reports what it could do as
  the harness's JSON `result`. It never talks to a provider: the key is a
  fixture and the endpoint does not exist. Skipped when bwrap cannot build a
  sandbox on this machine.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import runner  # noqa: E402

from .test_runner import RunnerBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "delegate-run"
DELEGATE = ROOT / "bin" / "delegate"

BWRAP_PROBLEM = runner._bwrap_unusable()


class SandboxModeTest(RunnerBase):
    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in ("DELEGATE_SANDBOX", "PATH")}
        self.addCleanup(self._restore_env, saved)
        self.argv_file = self.tmp / "argv.json"
        os.environ["FAKE_ARGV_FILE"] = str(self.argv_file)
        os.environ["FAKE_MODE"] = "ok_noop"

    def _argv(self):
        return json.loads(self.argv_file.read_text(encoding="utf-8"))

    def _path_without_bwrap(self):
        """A PATH with git and nothing else: no bwrap to find."""
        bindir = self.tmp / "nobwrap"
        bindir.mkdir()
        os.symlink(shutil.which("git"), bindir / "git")
        os.environ["PATH"] = str(bindir)

    def test_off_records_none_and_passes_no_flag(self):
        os.environ["DELEGATE_SANDBOX"] = "off"
        result = runner.execute(self._req())
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.sandbox, "none")
        self.assertNotIn("--sandbox", self._argv())
        on_disk = json.loads((Path(result.artifacts) / "result.json").read_text())
        self.assertEqual(on_disk["sandbox"], "none")

    def test_auto_falls_back_to_none_without_bwrap(self):
        os.environ["DELEGATE_SANDBOX"] = "auto"
        self._path_without_bwrap()
        result = runner.execute(self._req())
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.sandbox, "none")
        self.assertNotIn("--sandbox", self._argv())

    def test_auto_is_the_default(self):
        os.environ.pop("DELEGATE_SANDBOX", None)
        self._path_without_bwrap()
        result = runner.execute(self._req())
        self.assertEqual(result.sandbox, "none")

    def test_bwrap_mode_without_bwrap_is_infra_failed(self):
        os.environ["DELEGATE_SANDBOX"] = "bwrap"
        self._path_without_bwrap()
        result = runner.execute(self._req())
        self.assertEqual(result.status, "infra_failed")
        self.assertIn("bwrap", result.error)
        self.assertIsNone(result.child_exit)
        self.assertFalse(self.argv_file.exists(), "the child ran unconfined")

    def test_invalid_mode_is_infra_failed(self):
        os.environ["DELEGATE_SANDBOX"] = "maybe"
        result = runner.execute(self._req())
        self.assertEqual(result.status, "infra_failed")
        self.assertIn("DELEGATE_SANDBOX", result.error)
        self.assertFalse(self.argv_file.exists())

    @unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
    def test_auto_with_bwrap_passes_the_gitdir_read_only(self):
        os.environ["DELEGATE_SANDBOX"] = "auto"
        result = runner.execute(self._req())
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.sandbox, "bwrap")
        argv = self._argv()
        self.assertEqual(argv[argv.index("--sandbox") + 1], "bwrap")
        ro = [argv[i + 1] for i, a in enumerate(argv) if a == "--sandbox-ro"]
        self.assertEqual(ro, [str((self.repo / ".git").resolve())])
        # The flags go before the task's `--`.
        self.assertLess(argv.index("--sandbox"), argv.index("--"))


# The fake harness. Runs INSIDE the sandbox, started by the real bin/delegate,
# and reports every probe in the `result` of a Claude Code --json object.
# @...@ placeholders are filled in by the test: the child's environment is
# built from an allowlist, so nothing can reach it through the environment.
_FAKE_CLAUDE = r'''#!/usr/bin/python3 -I
import json, os, signal, socket, subprocess, sys, time

HOME_CANARY = @HOME_CANARY@
KEY_FILE = @KEY_FILE@
OUTSIDE = @OUTSIDE@
REAL_HOME = @REAL_HOME@
probes = {}

def can_read(path):
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False

def can_create(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)

probes["home_canary_readable"] = can_read(HOME_CANARY)
probes["key_file_readable"] = can_read(KEY_FILE)
probes["outside_file_readable"] = can_read(OUTSIDE)
probes["real_ssh_visible"] = os.path.exists(os.path.join(REAL_HOME, ".ssh"))
probes["home_listing"] = sorted(os.listdir(os.environ["HOME"]))

common = git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
gitdir = git("rev-parse", "--absolute-git-dir").stdout.strip()
probes["git_log_rc"] = git("log", "--oneline", "-1").returncode
probes["git_status_rc"] = git("status", "--porcelain").returncode
probes["gitdir_writes"] = {
    "config": can_create(os.path.join(common, "config")),
    "ref": can_create(os.path.join(common, "refs", "heads", "evil")),
    "hook": can_create(os.path.join(common, "hooks", "post-checkout")),
    "worktree_head": can_create(os.path.join(gitdir, "HEAD")),
}
probes["git_branch_rc"] = git("branch", "evil").returncode
probes["worktree_write"] = can_create("sandbox-probe.txt")

try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probes["af_unix"] = "allowed"
except OSError as exc:
    probes["af_unix"] = exc.errno
try:
    socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    probes["dgram_pair"] = "allowed"
except OSError as exc:
    probes["dgram_pair"] = exc.errno
a, b = socket.socketpair()   # stream pairs stay allowed (libuv stdio)
a.close(); b.close()
probes["fds"] = sorted(int(n) for n in os.listdir("/proc/self/fd"))

# The escaper: a chain that moves to a new session (and group) on every fork
# and ignores TERM. It hops faster than a /proc scan can follow, which is the
# documented gap of the runner's cleanup. Its pulse is a counter in the
# worktree (.escaper-beat), the one place the test can see from outside; it
# stops on its own when .escaper-stop appears or after 20 s.
r, w = os.pipe()
if os.fork() == 0:
    os.close(r)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    hops, born = 0, time.monotonic()
    while True:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        if time.monotonic() - born > 20 or os.path.exists(".escaper-stop"):
            os._exit(0)
        hops += 1
        with open(".escaper-beat", "w") as fh:
            fh.write(str(hops))
        if hops == 20:
            os.write(w, b"x")
            os.close(w)
        time.sleep(0.01)
os.close(w)
probes["escaper_running"] = os.read(r, 1) == b"x"

json.dump({"type": "result", "subtype": "success", "is_error": False,
           "result": json.dumps(probes),
           "usage": {"input_tokens": 1, "output_tokens": 1,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
          sys.stdout)
sys.stdout.write("\n")
'''


def _beating(worktree: Path) -> bool:
    """True if the escaper's counter in the worktree still moves."""
    beat = worktree / ".escaper-beat"

    def read():
        try:
            return beat.read_text()
        except OSError:
            return None
    first = read()
    time.sleep(0.5)
    return read() != first


def _stop_escaper(worktree: Path) -> None:
    try:
        (worktree / ".escaper-stop").write_text("stop\n")
    except OSError:
        pass
    time.sleep(0.2)


@unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
class SandboxConfinementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dlgsbx-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for sub in ("bin", "home", "providers.d", "mcp.d", "state"):
            (self.tmp / sub).mkdir()
        keys = self.tmp / "keys"
        keys.mkdir(mode=0o700)
        self.key_file = keys / "api_key.test"
        self.key_file.write_text("fixture-key-not-real\n")
        self.key_file.chmod(0o600)
        os.symlink("api_key.test", keys / "api_key")
        (self.tmp / "providers.d" / "fake").write_text(
            "endpoint\thttps://api.example.test/anthropic\n"
            "auth_env\tANTHROPIC_API_KEY\n"
            f"key_dir\t{keys}\n"
            "model\tfake-model\n"
            "small_model\tfake-small\n"
            "timeout\t60\n"
            "caps\tread,edit,bash\n")
        self.canary = self.tmp / "home" / "canary.txt"
        self.canary.write_text("secret in HOME\n")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("a file nobody mounted\n")
        self.real_home = pwd.getpwuid(os.getuid()).pw_dir
        fake = _FAKE_CLAUDE
        for name, value in (("HOME_CANARY", str(self.canary)), ("KEY_FILE", str(self.key_file)),
                            ("OUTSIDE", str(self.outside)), ("REAL_HOME", self.real_home)):
            fake = fake.replace(f"@{name}@", repr(value))
        claude = self.tmp / "bin" / "claude"
        claude.write_text(fake)
        claude.chmod(0o755)

        self.repo = self.tmp / "repo"
        genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        for args in (["init", "-q", "-b", "main", str(self.repo)],
                     ["-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], check=True, env=genv, capture_output=True)

    def _run(self, task, grants=("edit", "bash")):
        env = {
            "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.tmp / "home"),
            "LANG": "C.UTF-8",
            "DELEGATE_PROVIDERS_DIR": str(self.tmp / "providers.d"),
            "DELEGATE_MCP_DIR": str(self.tmp / "mcp.d"),
            "DELEGATE_STATE_DIR": str(self.tmp / "state"),
            "DELEGATE_BIN": str(DELEGATE),
            "DELEGATE_SANDBOX": "bwrap",
            "DELEGATE_RUN_GRACE": "1",
        }
        request = {"v": 1, "provider": "fake", "repo": str(self.repo),
                   "grants": list(grants), "timeout": 60, "task": task}
        proc = subprocess.run([str(BIN)], input=json.dumps(request).encode(),
                              capture_output=True, env=env, timeout=120)
        out = json.loads(proc.stdout)
        stderr_log = Path(out["artifacts"]) / "stderr.log"
        detail = json.dumps({k: out.get(k) for k in ("status", "error", "child_exit", "sandbox")})
        if stderr_log.exists():
            detail += "\n" + stderr_log.read_text(errors="replace")[-2000:]
        return out, detail

    def test_child_is_confined(self):
        out, detail = self._run("probe the sandbox")
        self.assertEqual(out["status"], "completed", detail)
        self.assertEqual(out["sandbox"], "bwrap")
        probes = json.loads(out["report"])

        # The user's home, the key directory and the rest of the disk are not there.
        self.assertFalse(probes["home_canary_readable"])
        self.assertEqual(probes["home_listing"], [])
        self.assertFalse(probes["key_file_readable"])
        self.assertFalse(probes["outside_file_readable"])
        if os.path.exists(os.path.join(self.real_home, ".ssh")):
            self.assertFalse(probes["real_ssh_visible"])

        # git reads the repository but cannot write its gitdir.
        self.assertEqual(probes["git_log_rc"], 0)
        self.assertEqual(probes["git_status_rc"], 0)
        self.assertEqual(probes["gitdir_writes"], {"config": False, "ref": False,
                                                   "hook": False, "worktree_head": False})
        self.assertNotEqual(probes["git_branch_rc"], 0)
        listed = subprocess.run(["git", "-C", str(self.repo), "branch", "--list", "evil"],
                                capture_output=True, text=True).stdout
        self.assertEqual(listed.strip(), "")

        # The worktree is writable, and what was written reaches the branch.
        self.assertTrue(probes["worktree_write"])
        self.assertIn({"path": "sandbox-probe.txt", "change": "A"}, out["changed_files"])

        # No unix sockets: the abstract namespace is shared with the host.
        self.assertEqual(probes["af_unix"], 97)        # EAFNOSUPPORT
        self.assertEqual(probes["dgram_pair"], 97)
        # Nothing but stdio crosses (the extra one is listdir's own fd).
        self.assertLessEqual(len(probes["fds"]), 4, probes["fds"])
        self.assertEqual(probes["fds"][:3], [0, 1, 2])

    def test_escaper_that_changes_session_dies_with_the_run(self):
        out, detail = self._run("escape")
        self.assertEqual(out["status"], "completed", detail)
        probes = json.loads(out["report"])
        self.assertTrue(probes["escaper_running"])
        worktree = Path(out["worktree"])
        self.addCleanup(_stop_escaper, worktree)
        self.assertFalse(_beating(worktree), "the chain outlived the run")

    def _delegate_direct(self, sandbox):
        """bin/delegate alone, no runner: nothing but the sandbox can clean up."""
        wt = self.tmp / "wt"
        if not wt.exists():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach",
                            str(wt)], check=True, capture_output=True)
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
               "LANG": "C.UTF-8", "DELEGATE_PROVIDERS_DIR": str(self.tmp / "providers.d"),
               "DELEGATE_MCP_DIR": str(self.tmp / "mcp.d")}
        flags = ["--sandbox", "bwrap", "--sandbox-ro", str(self.repo / ".git")] if sandbox else []
        proc = subprocess.run([str(DELEGATE), "fake", "--json", "--edit", "--bash", *flags,
                               "--dir", str(wt), "--", "escape"],
                              capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        return wt, json.loads(json.loads(proc.stdout)["result"])

    def test_escaper_dies_with_the_sandbox_itself(self):
        wt, probes = self._delegate_direct(sandbox=True)
        self.addCleanup(_stop_escaper, wt)
        self.assertTrue(probes["escaper_running"])
        self.assertFalse(_beating(wt), "the chain outlived the sandbox")

    def test_control_without_sandbox_the_escaper_survives(self):
        # Proves the test above can fail: with no sandbox and no runner, the
        # same chain is still alive after delegate returned.
        wt, probes = self._delegate_direct(sandbox=False)
        self.addCleanup(_stop_escaper, wt)
        self.assertTrue(probes["escaper_running"])
        self.assertTrue(_beating(wt))

    def test_read_only_floor_runs_too(self):
        out, detail = self._run("probe the floor", grants=())
        self.assertEqual(out["status"], "completed", detail)
        probes = json.loads(out["report"])
        self.assertEqual(probes["git_log_rc"], 0)
        self.assertFalse(probes["gitdir_writes"]["config"])


class SandboxFlagsTest(unittest.TestCase):
    """bin/delegate refuses bad sandbox flags with 2, before reading the key."""

    def setUp(self):
        SandboxConfinementTest.setUp(self)
        # A key that would make a refusal-after-read visible: unreadable.
        self.key_file.chmod(0o000)

    def _delegate(self, *flags, workdir=None):
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
               "DELEGATE_PROVIDERS_DIR": str(self.tmp / "providers.d"),
               "DELEGATE_MCP_DIR": str(self.tmp / "mcp.d")}
        return subprocess.run([str(DELEGATE), "fake", *flags, "--dir",
                               str(workdir or self.repo), "--", "x"],
                              capture_output=True, env=env, timeout=30)

    def test_unknown_mode_is_refused(self):
        proc = self._delegate("--sandbox", "maybe")
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_read_only_path_without_sandbox_is_refused(self):
        proc = self._delegate("--sandbox-ro", str(self.repo / ".git"))
        self.assertEqual(proc.returncode, 2, proc.stderr)

    @unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
    def test_relative_or_missing_read_only_path_is_refused(self):
        for path in ("repo/.git", str(self.tmp / "missing")):
            proc = self._delegate("--sandbox", "bwrap", "--sandbox-ro", path)
            self.assertEqual(proc.returncode, 2, (path, proc.stderr))

    @unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
    def test_workdir_containing_home_is_refused(self):
        proc = self._delegate("--sandbox", "bwrap", workdir=self.tmp)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(b"HOME", proc.stderr)


    @unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
    def test_a_script_harness_in_home_does_not_mount_home(self):
        # A non-ELF `claude` is mounted with its directory; when that directory
        # is HOME itself, the run refuses instead of mounting the home.
        self.key_file.chmod(0o600)
        home = self.tmp / "home"
        shutil.copy(self.tmp / "bin" / "claude", home / "claude")
        env = {"PATH": f"{home}:/usr/bin:/bin", "HOME": str(home),
               "DELEGATE_PROVIDERS_DIR": str(self.tmp / "providers.d"),
               "DELEGATE_MCP_DIR": str(self.tmp / "mcp.d")}
        proc = subprocess.run([str(DELEGATE), "fake", "--sandbox", "bwrap", "--dir",
                               str(self.repo), "--", "x"],
                              capture_output=True, env=env, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"HOME", proc.stderr)
        self.assertNotIn(b"home_canary_readable", proc.stdout)

if __name__ == "__main__":
    unittest.main()
