"""Fase C: `delegate-run --remote` end to end, with ssh replaced by fake_ssh.py.

The "remote" is a second state dir, HOME and mirror root on this machine;
fake_ssh runs each command there with `bash -c` and an environment of its
own, the way sshd would. Everything else is real: git push and fetch go
through the same fake ssh, and the remote side is the real bin/delegate-run
with the fake delegate.

Run from the repo root:
    python3 -m unittest discover -s test/py -t .
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import contract, remote  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "delegate-run"
FAKE = Path(__file__).resolve().parent / "fake_delegate.py"
FAKE_SSH = Path(__file__).resolve().parent / "fake_ssh.py"
HOST = "buildbox"

# Remote commands the client may send. Anything else on an ssh command line
# means a request field leaked into it.
_ALLOWED_CMD = (r"\Adelegate-run\Z",
                r"\Adelegate-run status [0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z",
                r"\Adelegate-run mirror [A-Za-z0-9][A-Za-z0-9_.-]*\Z",
                r"\Agit-(receive|upload)-pack '/[A-Za-z0-9_./-]+\.git'\Z")

SLOW_EDIT = "import time; time.sleep(%s); open('edited.txt', 'w').write('remote\\n')"


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dlgremote-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "laptop-state"
        self.repo = self.tmp / "my repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        self.rhome = self.tmp / "remote-home"
        self.rbin = self.rhome / "bin"
        self.rbin.mkdir(parents=True)
        (self.rbin / "delegate-run").symlink_to(BIN)
        self.rstate = self.rhome / ".local" / "state" / "delegate"
        self.ssh_state = self.tmp / "ssh-state"
        self.ssh_state.mkdir()
        self.ssh_log = self.tmp / "ssh.log"
        self.remote_env = {
            "HOME": str(self.rhome),
            "PATH": f"{self.rbin}:/usr/local/bin:/usr/bin:/bin",
            "DELEGATE_BIN": str(FAKE),
            "DELEGATE_SANDBOX": "off",
            "DELEGATE_RUN_GRACE": "0.5",
            "DELEGATE_RUN_SLACK": "0.5",
            "FAKE_MODE": "ok_edit",
        }
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "laptop-home"),
            "DELEGATE_STATE_DIR": str(self.state),
            "DELEGATE_REMOTE_SSH": str(FAKE_SSH),
            "DELEGATE_REMOTE_POLL": "0.1",
            "DELEGATE_REMOTE_POLL_MAX": "0.3",
            "DELEGATE_REMOTE_GIVEUP": "20",
            "FAKE_SSH_LOG": str(self.ssh_log),
            "FAKE_SSH_STATE": str(self.ssh_state),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        }

    # -- helpers -------------------------------------------------------------

    def git(self, *args, repo=None, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        proc = subprocess.run(["git", "-C", str(repo or self.repo), *args],
                              capture_output=True, text=True, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip() if check else proc

    def request(self, **kw):
        req = {"v": 1, "provider": "deepseek", "repo": str(self.repo),
               "task": "haz algo", "timeout": 60}
        req.update(kw)
        return req

    def cli(self, args, stdin=b"", remote_env=None, env=None, timeout=60):
        renv = dict(self.remote_env, **(remote_env or {}))
        full = dict(self.env, FAKE_REMOTE_ENV=json.dumps(renv), **(env or {}))
        proc = subprocess.run([str(BIN), *args], input=stdin, capture_output=True,
                              env=full, timeout=timeout)
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, out, proc.stderr.decode()

    def run_remote(self, req=None, extra=(), **kw):
        raw = json.dumps(req if req is not None else self.request()).encode()
        return self.cli(["--remote", HOST, *extra], raw, **kw)

    def remote_cli(self, *args, stdin=b"", env=None):
        renv = dict(self.remote_env, **(env or {}))
        proc = subprocess.run([str(BIN), *args], input=stdin, capture_output=True,
                              env=renv, cwd=str(self.rhome), timeout=60)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def calls(self):
        if not self.ssh_log.exists():
            return []
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()]

    def run_calls(self):
        return [c for c in self.calls() if c["cmd"] == "delegate-run"]

    def mirror(self):
        mirrors = list((self.rhome / "delegate" / "repos").glob("*.git"))
        self.assertEqual(len(mirrors), 1, mirrors)
        return mirrors[0]

    def remote_runs(self):
        root = self.rstate / "runs"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def wait_remote_terminal(self, run_id, timeout=20):
        deadline = time.monotonic() + timeout
        path = self.rstate / "runs" / run_id / "result.json"
        while time.monotonic() < deadline:
            try:
                if json.loads(path.read_text())["status"] != "running":
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.05)
        self.fail(f"remote run {run_id} never finished")

    def assert_clean_transport(self):
        """Every ssh call is hardened and carries only an allowed command."""
        import re
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            argv = call["argv"]
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("ForwardAgent=no", argv)
            self.assertIn("ClearAllForwardings=yes", argv)
            self.assertIn("-T", argv)
            self.assertTrue(any(re.match(p, call["cmd"]) for p in _ALLOWED_CMD),
                            f"unexpected remote command: {call['cmd']!r}")

    # -- happy path ------------------------------------------------------------

    def test_happy_path_pushes_base_and_fetches_branch(self):
        head = self.git("rev-parse", "HEAD")
        code, out, err = self.run_remote()
        self.assertEqual(code, 0, err)
        run_id = out["run_id"]
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["host"], HOST)
        self.assertEqual(out["base"], head)
        self.assertEqual(out["branch"], f"delegate/{run_id}")
        self.assertIsNone(out["worktree"])
        self.assertEqual(out["artifacts"], str(self.state / "runs" / run_id))
        self.assertTrue(out["remote_artifacts"].startswith(str(self.rstate)))
        # The branch is local now: one commit on top of the base.
        self.assertEqual(self.git("rev-parse", f"delegate/{run_id}^"), head)
        self.assertEqual(self.git("show", f"delegate/{run_id}:edited.txt"), "written by the fake delegate")
        # The mirror got the base under its own namespace, nothing else.
        mirror = self.mirror()
        self.assertTrue(mirror.name.startswith("my-repo-"))
        self.assertEqual(self.git("rev-parse", f"refs/delegate/base/{run_id}", repo=mirror), head)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/heads/main",
                                  repo=mirror), "")
        # The remote ran against the mirror with the full sha.
        rreq = json.loads((self.rstate / "runs" / run_id / "request.json").read_text())
        self.assertEqual(rreq["repo"], str(mirror))
        self.assertEqual(rreq["base"], head)
        self.assertEqual(rreq["run_id"], run_id)
        # The local copy is what status/list/stats see.
        local = json.loads((self.state / "runs" / run_id / "result.json").read_text())
        self.assertEqual(local, out)
        lreq = json.loads((self.state / "runs" / run_id / "request.json").read_text())
        self.assertEqual(lreq["repo"], str(self.repo))
        self.assertFalse((self.state / "remote" / f"{run_id}.json").exists())
        code, st, _ = self.cli(["status", run_id])
        self.assertEqual((code, st["status"], st["host"]), (0, "completed", HOST))
        code, stats, _ = self.cli(["stats", "--json"])
        self.assertEqual(stats["by_host"], {HOST: 1})
        self.assertEqual(stats["runs"], 1)
        code, rows, _ = self.cli(["list"])
        self.assertEqual(rows[0]["host"], HOST)
        self.assertEqual(len(self.run_calls()), 1)
        self.assert_clean_transport()

    def test_second_run_reuses_the_mirror(self):
        self.assertEqual(self.run_remote()[0], 0)
        (self.repo / "b.txt").write_text("b\n")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "b")
        code, out, err = self.run_remote()
        self.assertEqual(code, 0, err)
        self.mirror()  # still exactly one
        self.assertEqual(self.git("rev-parse", f"delegate/{out['run_id']}^"),
                         self.git("rev-parse", "HEAD"))

    def test_no_changes_means_no_branch_and_no_fetch(self):
        code, out, err = self.run_remote(remote_env={"FAKE_MODE": "ok_noop"})
        self.assertEqual(code, 0, err)
        self.assertIsNone(out["branch"])
        self.assertFalse([c for c in self.calls() if "upload-pack" in c["cmd"]])

    def test_mirror_root_under_a_dot_directory(self):
        root = self.rhome / ".cache" / "mirrors"
        code, out, err = self.run_remote(remote_env={"DELEGATE_MIRROR_ROOT": str(root)})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list(root.glob("*.git"))), 1)

    def test_mirror_path_check(self):
        for ok in ("/home/u/delegate/repos/a-1.git", "/home/u/.cache/r/a.git"):
            self.assertTrue(remote._mirror_path_ok(ok), ok)
        for bad in ("home/a.git", "/a/../b.git", "/a/./b.git", "/a/b", "/a/b'c.git",
                    "/a/b c.git", "/a/$(x).git", None, 3):
            self.assertFalse(remote._mirror_path_ok(bad), bad)

    def test_caller_chosen_run_id_is_refused(self):
        code, out, err = self.run_remote(self.request(run_id="20260101-000000-abcdef"))
        self.assertEqual((code, out), (2, None))
        self.assertIn("el cliente", err)
        self.assertEqual(self.calls(), [])

    # -- local base rules --------------------------------------------------------

    def test_dirty_tree_is_rejected_before_any_ssh(self):
        (self.repo / "README.md").write_text("changed\n")
        code, out, _ = self.run_remote()
        self.assertEqual((code, out["status"], out["host"]), (1, "rejected", HOST))
        self.assertIsNone(out["child_exit"])
        self.assertEqual(self.calls(), [])
        # Stored like any refused run, so stats count it.
        self.assertTrue((self.state / "runs" / out["run_id"] / "result.json").exists())

    def test_snapshot_ships_the_dirty_state(self):
        (self.repo / "README.md").write_text("changed\n")
        (self.repo / "new.txt").write_text("new\n")
        code, out, err = self.run_remote(self.request(snapshot=True))
        self.assertEqual(code, 0, err)
        run_id = out["run_id"]
        self.assertEqual(out["base"], self.git("rev-parse", f"refs/delegate/snapshot/{run_id}"))
        self.assertEqual(self.git("show", f"delegate/{run_id}:new.txt"), "new")
        self.assertEqual(self.git("show", f"delegate/{run_id}:README.md"), "changed")
        rreq = json.loads((self.rstate / "runs" / run_id / "request.json").read_text())
        self.assertFalse(rreq["snapshot"])

    def test_invalid_request_and_host_never_reach_ssh(self):
        code, out, err = self.cli(["--remote", HOST], b'{"v": 1}')
        self.assertEqual((code, out), (2, None))
        for host in ("-oProxyCommand=touch x", "a:b", "a/b", "", "a b"):
            code, out, err = self.cli(["--remote", host], json.dumps(self.request()).encode())
            self.assertEqual(code, 2, host)
            self.assertIsNone(out)
        self.assertEqual(self.calls(), [])

    # -- transport failures ------------------------------------------------------

    def test_drop_mid_run_recovers_through_status(self):
        drop = {"match": r"\Adelegate-run\Z", "times": 1, "after": 0.3}
        code, out, err = self.run_remote(
            env={"FAKE_SSH_DROP": json.dumps(drop)},
            remote_env={"FAKE_MODE": "script", "FAKE_SCRIPT": SLOW_EDIT % 1.5})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["status"], "completed")
        self.assertIn("se cortó", err)
        self.assertEqual(self.git("show", f"delegate/{out['run_id']}:edited.txt"), "remote")
        # Recovered by asking, never by running it again, and the asking
        # started while the remote run was still going.
        self.assertEqual(len(self.run_calls()), 1)
        rres = json.loads((self.rstate / "runs" / out["run_id"] / "result.json").read_text())
        first_poll = min(c["t"] for c in self.calls()
                         if c["cmd"].startswith("delegate-run status"))
        self.assertLess(first_poll, rres["finished_at"])
        self.assertEqual(self.remote_runs(), [out["run_id"]])
        self.assertTrue([c for c in self.calls() if c["cmd"].startswith("delegate-run status")])
        self.assert_clean_transport()

    def test_drop_after_the_run_finished_is_not_rerun(self):
        drop = {"match": r"\Adelegate-run\Z", "times": 1, "after": 30}
        code, out, err = self.run_remote(env={"FAKE_SSH_DROP": json.dumps(drop)})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.run_calls()), 1)
        self.assertEqual(self.remote_runs(), [out["run_id"]])

    def test_request_that_never_arrived_is_resent_once(self):
        fail = {"match": r"\Adelegate-run\Z", "times": 1}
        code, out, err = self.run_remote(env={"FAKE_SSH_FAIL": json.dumps(fail)})
        self.assertEqual(code, 0, err)
        self.assertIn("se reenvía", err)
        self.assertEqual(len(self.run_calls()), 2)
        self.assertEqual(self.remote_runs(), [out["run_id"]])

    def test_unreachable_host_leaves_run_for_fetch(self):
        drop = {"match": r"\Adelegate-run\Z", "times": 1, "after": 0.2}
        fail = {"match": r"\Adelegate-run status", "times": 1000}
        code, out, err = self.run_remote(
            env={"FAKE_SSH_DROP": json.dumps(drop), "FAKE_SSH_FAIL": json.dumps(fail),
                 "DELEGATE_REMOTE_GIVEUP": "1"},
            remote_env={"FAKE_MODE": "script", "FAKE_SCRIPT": SLOW_EDIT % 0.5})
        self.assertEqual(code, remote.EXIT_UNKNOWN, err)
        run_id = out["run_id"]
        self.assertEqual(out["status"], "unknown")
        self.assertTrue((self.state / "remote" / f"{run_id}.json").exists())
        self.assertFalse((self.state / "runs" / run_id).exists())
        # The host is back: fetch collects the run that kept going.
        self.wait_remote_terminal(run_id)
        code, out, err = self.cli(["fetch", run_id])
        self.assertEqual((code, out["status"]), (0, "completed"), err)
        self.assertEqual(self.git("show", f"delegate/{run_id}:edited.txt"), "remote")
        self.assertEqual(len(self.run_calls()), 1)

    # -- run_id uniqueness ---------------------------------------------------------

    def in_process(self, run_id, env=None, remote_env=None):
        """Run the client in this process with new_run_id pinned to run_id,
        to put it in front of a run the host already has."""
        from unittest import mock
        renv = dict(self.remote_env, **(remote_env or {}))
        full = dict(self.env, FAKE_REMOTE_ENV=json.dumps(renv), **(env or {}))
        raw = json.dumps(self.request()).encode()
        with mock.patch.dict(os.environ, full, clear=True), \
                mock.patch.object(contract, "new_run_id", return_value=run_id):
            try:
                return remote.run_remote(raw, HOST)
            except remote.RemoteError as exc:
                return None, exc.code

    def plant_remote_run(self, run_id, base="0" * 40, status="completed"):
        rd = self.rstate / "runs" / run_id
        rd.mkdir(parents=True)
        (rd / "result.json").write_text(json.dumps(
            {"v": 1, "run_id": run_id, "status": status, "provider": "deepseek",
             "base": base, "branch": None}))
        return rd / "result.json"

    def test_duplicate_run_id_on_remote_is_rejected(self):
        run_id = "20260101-000000-aaaaaa"
        planted = self.plant_remote_run(run_id)
        before = planted.read_bytes()
        out, code = self.in_process(run_id)
        self.assertEqual((out, code), (None, remote.EXIT_RUN_EXISTS))
        self.assertEqual(planted.read_bytes(), before)
        self.assertFalse((self.state / "remote" / f"{run_id}.json").exists())
        self.assertFalse((self.state / "runs" / run_id).exists())

    def test_drop_never_adopts_another_requests_run(self):
        # The host already has a finished run with this id (another base);
        # the send drops before the duplicate refusal arrives. Status finds
        # the old run, and it must not be taken as this request's.
        run_id = "20260101-000000-eeeeee"
        self.plant_remote_run(run_id, base="1" * 40)
        drop = {"match": r"\Adelegate-run\Z", "times": 5, "after": 0}
        out, code = self.in_process(run_id, env={"FAKE_SSH_DROP": json.dumps(drop)})
        self.assertEqual(code, remote.EXIT_UNKNOWN)
        self.assertIn("no corresponde", out["error"])
        self.assertFalse((self.state / "runs" / run_id).exists())

    def test_unreadable_is_believed_only_after_several_polls(self):
        run_id = "20260101-000000-ffffff"
        (self.rstate / "runs" / run_id).mkdir(parents=True)  # no lock, no result
        fail = {"match": r"\Adelegate-run\Z", "times": 100}
        out, code = self.in_process(run_id, env={"FAKE_SSH_FAIL": json.dumps(fail)})
        self.assertEqual((code, out["status"]), (1, "unreadable"))
        polls = [c for c in self.calls() if c["cmd"].startswith("delegate-run status")]
        self.assertGreaterEqual(len(polls), remote.UNREADABLE_POLLS)

    def test_fetch_never_moves_an_existing_local_ref(self):
        head = self.git("rev-parse", "HEAD")
        cases = (("plain", lambda rid: self.git("branch", f"delegate/{rid}", head)),
                 ("symref", lambda rid: self.git("symbolic-ref", f"refs/heads/delegate/{rid}",
                                                 "refs/heads/main")),
                 ("tmp-symref", lambda rid: self.git("symbolic-ref",
                                                     f"refs/delegate/fetch/{rid}",
                                                     "refs/heads/main")))
        for i, (name, plant) in enumerate(cases):
            run_id = f"20260101-00000{i}-abcabc"
            plant(run_id)
            out, code = self.in_process(run_id)
            self.assertEqual((code, out["status"]), (1, "infra_failed"), name)
            self.assertIn("no se toca", out["error"])
            self.assertEqual(self.git("rev-parse", "main"), head, name)
        self.assertEqual(self.git("rev-parse", "delegate/20260101-000000-abcabc"), head)

    def test_local_runner_refuses_id_reserved_by_remote_client(self):
        run_id = "20260101-000000-cccccd"
        (self.state / "remote").mkdir(parents=True)
        (self.state / "remote" / f"{run_id}.json").write_text("{}\n")
        req = json.dumps(self.request(run_id=run_id)).encode()
        env = {"DELEGATE_BIN": str(FAKE), "DELEGATE_SANDBOX": "off", "FAKE_MODE": "ok_noop"}
        code, out, err = self.cli([], req, env=env)
        self.assertEqual((code, out), (3, None))
        self.assertIn("reservado", err)
        self.assertFalse((self.state / "runs" / run_id).exists())

    def test_local_runner_refuses_existing_run_id(self):
        run_id = "20260101-000000-cccccc"
        req = json.dumps(self.request(run_id=run_id)).encode()
        env = {"DELEGATE_BIN": str(FAKE), "DELEGATE_SANDBOX": "off", "FAKE_MODE": "ok_noop"}
        self.assertEqual(self.cli([], req, env=env)[0], 0)
        result = self.state / "runs" / run_id / "result.json"
        before = result.read_bytes()
        code, out, err = self.cli([], req, env=env)
        self.assertEqual((code, out), (3, None))
        self.assertEqual(result.read_bytes(), before)

    def test_run_id_field_is_validated(self):
        for bad in ("x", "20260101-000000-ABCDEF", "../20260101-000000-abcdef", 7):
            code, out, err = self.cli([], json.dumps(self.request(run_id=bad)).encode())
            self.assertEqual((code, out), (2, None), bad)
            self.assertIn("run_id", err)

    # -- remote statuses propagate ---------------------------------------------------

    def test_remote_failures_propagate(self):
        for mode, status in (("fail", "task_failed"), ("reject", "rejected"),
                             ("badjson", "invalid_result")):
            code, out, err = self.run_remote(remote_env={"FAKE_MODE": mode})
            self.assertEqual((code, out["status"], out["host"]), (1, status, HOST), mode)
            local = json.loads((self.state / "runs" / out["run_id"] / "result.json").read_text())
            self.assertEqual(local["status"], status)
        code, stats, _ = self.cli(["stats", "--json"])
        self.assertEqual(stats["by_host"], {HOST: 3})

    def test_checks_failed_still_fetches_the_branch(self):
        code, out, err = self.run_remote(self.request(checks=[["false"]]))
        self.assertEqual((code, out["status"]), (1, "checks_failed"), err)
        self.assertEqual(self.git("show", f"delegate/{out['run_id']}:edited.txt"), "written by the fake delegate")

    def test_remote_without_delegate_run_is_infra_failed(self):
        (self.rbin / "delegate-run").unlink()
        code, out, err = self.run_remote()
        self.assertEqual((code, out["status"]), (1, "infra_failed"), err)
        self.assertIn("no está instalado", out["error"])

    # -- injection -------------------------------------------------------------------

    def test_no_shell_injection_through_repo_name_or_task(self):
        marker = self.tmp / "PWNED"
        evil = self.tmp / f"x'; touch {marker}; echo '$(touch {marker}2)`touch {marker}3`"
        shutil.move(str(self.repo), str(evil))
        self.repo = evil
        task = f"'; touch {marker}4; ' $(touch {marker}5) `touch {marker}6` \"; touch {marker}7"
        code, out, err = self.run_remote(self.request(task=task))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("PWNED*")), [])
        rreq = json.loads((self.rstate / "runs" / out["run_id"] / "request.json").read_text())
        self.assertEqual(rreq["task"], task)
        self.assert_clean_transport()

    def test_mirror_command_refuses_bad_names_and_squatters(self):
        for bad in ("../x", ".hidden", "a/b", "a b", "-x", "x" * 81):
            code, out, err = self.remote_cli("mirror", bad)
            self.assertEqual(code, 2, bad)
        root = self.rhome / "delegate" / "repos"
        root.mkdir(parents=True)
        (root / "squat.git").symlink_to(self.repo)
        code, out, err = self.remote_cli("mirror", "squat")
        self.assertEqual(code, 1)
        self.assertIn("no es un directorio", err)
        (root / "plain.git").mkdir()
        code, out, err = self.remote_cli("mirror", "plain")
        self.assertEqual(code, 1)
        code, out, err = self.remote_cli("mirror", "fresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["path"], str(root / "fresh.git"))
        self.assertEqual(self.git("rev-parse", "--is-bare-repository", repo=root / "fresh.git"),
                         "true")

    # -- fetch verification -------------------------------------------------------------

    def test_branch_not_on_base_is_refused(self):
        # A remote whose delegate-run moves the branch onto an unrelated commit.
        wrapper = self.rbin / "delegate-run"
        wrapper.unlink()
        wrapper.write_text(
            "#!/bin/bash\n"
            f"out=$({BIN} \"$@\"); rc=$?\n"
            "if [ \"$#\" = 0 ]; then\n"
            "  id=$(printf '%s' \"$out\" | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"run_id\"])')\n"
            "  m=$(ls -d \"$HOME\"/delegate/repos/*.git)\n"
            "  t=$(git -C \"$m\" mktree </dev/null)\n"
            "  c=$(GIT_AUTHOR_NAME=x GIT_AUTHOR_EMAIL=x@x GIT_COMMITTER_NAME=x "
            "GIT_COMMITTER_EMAIL=x@x git -C \"$m\" commit-tree \"$t\" -m evil)\n"
            "  git -C \"$m\" update-ref \"refs/heads/delegate/$id\" \"$c\"\n"
            "fi\n"
            "printf '%s\\n' \"$out\"; exit $rc\n")
        wrapper.chmod(0o755)
        code, out, err = self.run_remote()
        self.assertEqual((code, out["status"]), (1, "infra_failed"), err)
        self.assertIsNone(out["branch"])
        self.assertIn("no es un commit sobre la base", out["error"])
        self.assertEqual(self.git("branch", "--list", "delegate/*"), "")

    # -- detach and fetch ----------------------------------------------------------------

    def test_detach_returns_at_once_and_fetch_collects(self):
        started = time.monotonic()
        code, out, err = self.run_remote(
            extra=("--detach",),
            remote_env={"FAKE_MODE": "script", "FAKE_SCRIPT": SLOW_EDIT % 2})
        self.assertEqual(code, 0, err)
        self.assertLess(time.monotonic() - started, 2)
        run_id = out["run_id"]
        self.assertEqual((out["status"], out["detached"]), ("running", True))
        code, st, _ = self.cli(["status", run_id, "--remote", HOST])
        self.assertEqual(code, 0)
        code, out, err = self.cli(["fetch", run_id, "--remote", HOST])
        self.assertEqual((code, out["status"]), (0, "completed"), err)
        self.assertEqual(self.git("show", f"delegate/{run_id}:edited.txt"), "remote")
        # Idempotent: a second fetch prints the stored copy.
        code, again, _ = self.cli(["fetch", run_id])
        self.assertEqual((code, again), (0, out))
        code, _, err = self.cli(["fetch", run_id, "--remote", "otra"])
        self.assertEqual(code, 2)
        self.assertEqual(len(self.run_calls()), 1)

    def test_fetch_of_unknown_run(self):
        code, out, err = self.cli(["fetch", "20260101-000000-dddddd"])
        self.assertEqual((code, out), (1, None))

    # -- remote gc -------------------------------------------------------------------------

    def test_remote_gc_removes_the_base_ref(self):
        code, out, err = self.run_remote()
        self.assertEqual(code, 0, err)
        run_id = out["run_id"]
        mirror = self.mirror()
        code, gc_out, err = self.remote_cli("gc", "--days", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(gc_out), [{"run_id": run_id, "removed": True, "error": None}])
        self.assertEqual(self.git("for-each-ref", repo=mirror), "")
        # The local copy and branch are the laptop's own gc's business.
        self.assertTrue(self.git("rev-parse", f"delegate/{run_id}"))
        code, gc_out, _ = self.cli(["gc", "--days", "0"])
        self.assertEqual(gc_out, [{"run_id": run_id, "removed": True, "error": None}])
        self.assertEqual(self.git("branch", "--list", "delegate/*"), "")


class BareRunnerTest(unittest.TestCase):
    """The remote side: delegate-run against a bare repository."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dlgbare-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = self.tmp / "src"
        src.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", "-C", str(src), *args], check=True, env=env)
        self.bare = self.tmp / "m.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(self.bare)], check=True)
        self.sha = subprocess.run(["git", "-C", str(self.bare), "rev-parse", "main"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp),
                    "DELEGATE_STATE_DIR": str(self.tmp / "state"), "DELEGATE_BIN": str(FAKE),
                    "DELEGATE_SANDBOX": "off", "FAKE_MODE": "ok_edit"}

    def run_cli(self, **kw):
        req = {"v": 1, "provider": "deepseek", "repo": str(self.bare), "task": "t",
               "timeout": 60}
        req.update(kw)
        proc = subprocess.run([str(BIN)], input=json.dumps(req).encode(),
                              capture_output=True, env=self.env, timeout=60)
        return proc.returncode, json.loads(proc.stdout)

    def test_full_sha_runs(self):
        code, out = self.run_cli(base=self.sha)
        self.assertEqual((code, out["status"]), (0, "completed"))
        branch = subprocess.run(["git", "-C", str(self.bare), "rev-parse",
                                 f"delegate/{out['run_id']}^"], capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(branch, self.sha)

    def test_names_short_shas_and_snapshot_are_refused(self):
        for kw in ({}, {"base": "main"}, {"base": self.sha[:12]},
                   {"base": "0" * 40}, {"base": self.sha, "snapshot": True}):
            code, out = self.run_cli(**kw)
            self.assertEqual((code, out["status"]), (1, "rejected"), kw)
            self.assertIsNone(out["child_exit"])


if __name__ == "__main__":
    unittest.main()
