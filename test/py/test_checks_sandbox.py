"""The request's checks run inside bubblewrap, like the delegate's child.

The runner is driven with the fake delegate (FAKE_MODE=ok_noop); the checks
are real commands, so what they can and cannot see is the sandbox the runner
built for them (lib/delegate_run/sandbox.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import contract, runner, sandbox  # noqa: E402

from .test_runner import RunnerBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DELEGATE = ROOT / "bin" / "delegate"
BWRAP_PROBLEM = runner._bwrap_unusable()


def _bash_bpf(arch: str) -> bytes:
    """dg_seccomp_bpf from bin/delegate, extracted and run on its own."""
    src = DELEGATE.read_text(encoding="utf-8")
    funcs = []
    for name in ("_dg_bpf_ins", "dg_seccomp_bpf"):
        start = src.index(f"\n{name}() {{\n") + 1
        end = src.index("\n}\n", start) + 3
        funcs.append(src[start:end])
    script = "".join(funcs) + f'dg_seccomp_bpf "{arch}"\n'
    return subprocess.run(["/bin/bash", "-c", script], check=True,
                          capture_output=True).stdout


class SeccompParityTest(unittest.TestCase):
    def test_python_program_is_the_bash_one_byte_for_byte(self):
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch):
                py = sandbox.seccomp_bpf(arch)
                self.assertEqual(len(py), 21 * 8)
                self.assertEqual(py, _bash_bpf(arch))

    def test_unknown_arch_is_refused(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.seccomp_bpf("riscv64")


class ChecksBase(RunnerBase):
    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in
                 ("HOME", "PATH", "DELEGATE_SANDBOX", "DELEGATE_CHECK_RO")}
        self.addCleanup(self._restore_env, saved)
        os.environ["FAKE_MODE"] = "ok_noop"
        os.environ.pop("DELEGATE_CHECK_RO", None)
        self.argv_file = self.tmp / "argv.json"
        os.environ["FAKE_ARGV_FILE"] = str(self.argv_file)

    def _fake_home(self) -> Path:
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / "canary").write_text("home-canary-7f3a\n")
        (home / ".ssh" / "id_canary").write_text("ssh-canary-91c2\n")
        (home / ".npmrc").write_text("//registry/:_authToken=npmrc-canary-55d0\n")
        os.environ["HOME"] = str(home)
        return home

    def _log(self, result, index=0) -> str:
        return (Path(result.artifacts) / result.checks[index].log).read_text(
            encoding="utf-8", errors="replace")


@unittest.skipIf(BWRAP_PROBLEM, f"bwrap no usable: {BWRAP_PROBLEM}")
class ChecksSandboxTest(ChecksBase):
    def setUp(self):
        super().setUp()
        os.environ["DELEGATE_SANDBOX"] = "auto"

    def test_a_check_cannot_read_the_real_home(self):
        home = self._fake_home()
        probe = ("cat \"$HOME/canary\" \"$HOME/.ssh/id_canary\" \"$HOME/.npmrc\";"
                 f" cat {self.repo}/README.md; cat {self.state}/runs/*/request.json;"
                 " echo LISTING; ls -A \"$HOME\"; echo END")
        result = runner.execute(self._req(checks=(("sh", "-c", probe),)))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.sandbox, "bwrap")
        self.assertEqual(result.checks_sandbox, "bwrap")
        log = self._log(result)
        for canary in ("home-canary-7f3a", "ssh-canary-91c2", "npmrc-canary-55d0"):
            self.assertNotIn(canary, log)
        # The user's checkout and the run directory are not mounted either.
        self.assertNotIn("hello", log)
        self.assertNotIn('"task"', log)
        listing = log.split("LISTING\n", 1)[1].split("END", 1)[0]
        self.assertEqual(listing.strip(), "", f"HOME is not empty inside: {listing!r}")
        self.assertTrue(home.joinpath("canary").exists())
        on_disk = json.loads((Path(result.artifacts) / "result.json").read_text())
        self.assertEqual(on_disk["checks_sandbox"], "bwrap")
        self.assertEqual(on_disk["v"], 1)

    def test_a_check_writes_the_worktree_but_not_the_gitdir(self):
        cfg = self.repo / ".git" / "config"
        before = cfg.read_bytes()
        probe = ("echo built > out.txt && cat out.txt && "
                 f"(echo pwned >> {cfg} && echo CONFIG-WRITTEN || echo config-ro) && "
                 "git log --oneline -1 >/dev/null && echo git-reads")
        result = runner.execute(self._req(checks=(("sh", "-c", probe),)))
        log = self._log(result)
        self.assertEqual(result.checks[0].exit, 0, log)
        self.assertIn("built", log)
        self.assertIn("config-ro", log)
        self.assertIn("git-reads", log)
        self.assertNotIn("CONFIG-WRITTEN", log)
        self.assertEqual(cfg.read_bytes(), before)

    def test_a_check_cannot_create_a_unix_socket(self):
        code = ("import socket\n"
                "try:\n"
                "    socket.socket(socket.AF_UNIX)\n"
                "    print('UNIX-OK')\n"
                "except OSError as e:\n"
                "    print('unix-denied', e.errno)\n")
        result = runner.execute(self._req(checks=(("python3", "-c", code),)))
        log = self._log(result)
        self.assertIn("unix-denied 97", log)
        self.assertNotIn("UNIX-OK", log)

    def test_a_check_runs_a_tool_from_the_home_toolchain(self):
        home = self._fake_home()
        bindir = home / ".nvm" / "versions" / "node" / "v0.0.0" / "bin"
        bindir.mkdir(parents=True)
        tool = bindir / "faketool"
        tool.write_text("#!/bin/sh\necho toolchain-ok\ncat \"$HOME/canary\"\n")
        tool.chmod(0o755)
        os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
        result = runner.execute(self._req(checks=(("faketool",),)))
        log = self._log(result)
        self.assertEqual(result.checks_sandbox, "bwrap")
        self.assertIn("toolchain-ok", log)
        self.assertNotIn("home-canary-7f3a", log)

    def test_a_check_runs_the_python_of_a_venv_outside_the_worktree(self):
        project = self.tmp / "project"
        project.mkdir()
        (project / "secret.txt").write_text("sibling-canary-0b1e\n")
        subprocess.run(["/usr/bin/python3", "-m", "venv", "--without-pip",
                        str(project / ".venv")], check=True)
        code = ("import sys\nprint('venv-ok', sys.prefix)\n"
                f"try:\n    print(open('{project}/secret.txt').read())\n"
                "except OSError as e:\n    print('sibling-hidden')\n")
        result = runner.execute(self._req(
            checks=((str(project / ".venv" / "bin" / "python"), "-c", code),)))
        log = self._log(result)
        self.assertEqual(result.checks[0].exit, 0, log)
        self.assertIn(f"venv-ok {project / '.venv'}", log)
        self.assertIn("sibling-hidden", log)
        self.assertNotIn("sibling-canary-0b1e", log)

    @unittest.skipUnless(shutil.which("node"), "no hay node en el PATH")
    def test_a_check_runs_the_real_node(self):
        # The caller's real HOME and PATH: node from nvm (or /usr) must run.
        node = os.path.realpath(shutil.which("node"))
        home = os.environ.get("HOME", "")
        if not (node.startswith("/usr/") or node.startswith(os.path.join(home, ".nvm", "versions"))):
            self.skipTest(f"node fuera de /usr y de ~/.nvm: {node}")
        probe = ("const fs=require('fs');console.log('node', 40+2);"
                 "for (const p of ['.ssh','.config','.claude']) {"
                 " try { fs.readdirSync(require('os').homedir()+'/'+p); console.log('SAW', p) }"
                 " catch (e) { console.log('hidden', p) } }")
        result = runner.execute(self._req(checks=(("node", "-e", probe),)))
        log = self._log(result)
        self.assertEqual(result.checks[0].exit, 0, log)
        self.assertIn("node 42", log)
        self.assertNotIn("SAW", log)

    def test_a_missing_command_is_127_like_without_sandbox(self):
        result = runner.execute(self._req(checks=(("no-such-tool-xyz",),
                                                  ("/nonexistent/bin/tool",))))
        self.assertEqual([c.exit for c in result.checks], [127, 127])

    def test_an_executable_in_a_sensitive_dir_is_not_mounted(self):
        home = self._fake_home()
        cfg = home / ".config" / "bin"
        cfg.mkdir(parents=True)
        (cfg / "leaky").write_text("#!/bin/sh\necho LEAKY-RAN\n")
        (cfg / "leaky").chmod(0o755)
        result = runner.execute(self._req(checks=((str(cfg / "leaky"),),)))
        log = self._log(result)
        self.assertNotEqual(result.checks[0].exit, 0)
        self.assertNotIn("LEAKY-RAN", log)
        self.assertIn("no se monta", log)

    def test_check_ro_exposes_an_extra_directory(self):
        extra = self.tmp / "browsers"
        extra.mkdir()
        (extra / "chrome").write_text("browser-bits\n")
        os.environ["DELEGATE_CHECK_RO"] = str(extra)
        result = runner.execute(self._req(checks=(("cat", str(extra / "chrome")),)))
        self.assertEqual(result.checks[0].exit, 0, self._log(result))
        self.assertIn("browser-bits", self._log(result))

    def test_check_ro_over_secrets_fails_before_the_child(self):
        home = self._fake_home()
        for bad in (str(home / ".ssh"), str(home), str(self.state), "relative/path",
                    str(home / ".config")):
            with self.subTest(bad=bad):
                if bad.endswith(".config"):
                    (home / ".config").mkdir(exist_ok=True)
                if self.argv_file.exists():
                    self.argv_file.unlink()
                os.environ["DELEGATE_CHECK_RO"] = bad
                result = runner.execute(self._req(checks=(("true",),)))
                self.assertEqual(result.status, "infra_failed")
                self.assertIn("DELEGATE_CHECK_RO", result.error)
                self.assertFalse(self.argv_file.exists(), "the child ran anyway")

    def test_no_checks_leaves_checks_sandbox_null(self):
        result = runner.execute(self._req())
        self.assertEqual(result.status, "completed", result.error)
        self.assertIsNone(result.checks_sandbox)

    def test_a_check_left_running_in_the_background_dies(self):
        marker = self.tmp / "survivor"
        # setsid + fork: escapes the session, not the pid namespace.
        probe = (f"setsid sh -c 'sleep 3; echo alive > {marker}' & echo started")
        result = runner.execute(self._req(checks=(("sh", "-c", probe),)))
        self.assertEqual(result.checks[0].exit, 0)
        import time
        time.sleep(4)
        self.assertFalse(marker.exists())


class ChecksWithoutSandboxTest(ChecksBase):
    def test_off_runs_checks_on_the_host_as_before(self):
        self._fake_home()
        os.environ["DELEGATE_SANDBOX"] = "off"
        result = runner.execute(self._req(checks=(("sh", "-c", 'cat "$HOME/canary"'),)))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.sandbox, "none")
        self.assertEqual(result.checks_sandbox, "none")
        self.assertIn("home-canary-7f3a", self._log(result))

    def test_off_ignores_check_ro(self):
        os.environ["DELEGATE_SANDBOX"] = "off"
        os.environ["DELEGATE_CHECK_RO"] = "relative/path"
        result = runner.execute(self._req(checks=(("true",),)))
        self.assertEqual(result.status, "completed", result.error)


if __name__ == "__main__":
    unittest.main()
