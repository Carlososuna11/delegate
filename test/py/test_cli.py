"""End-to-end tests of bin/delegate-run with the fake delegate.

Every test talks to the executable the way a caller does: a JSON request on
stdin, a JSON result on stdout, an exit code, and the subcommands. Nothing is
imported from the package except the contract constants.

Run from the repo root:
    python3 -m unittest discover -s test/py -t .
"""
from __future__ import annotations

import fcntl
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

from delegate_run import contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "delegate-run"
FAKE = Path(__file__).resolve().parent / "fake_delegate.py"


def _stat(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = data[data.rfind(")") + 2:].split()
    return fields[0], int(fields[3])  # state, session id


def _alive(pid):
    st = _stat(pid)
    return st is not None and st[0] != "Z"


def _session_members(sid):
    out = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = _stat(int(name))
            if st and st[1] == sid and st[0] != "Z":
                out.append(int(name))
    return out


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dlgcli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.tmp)),
            "DELEGATE_STATE_DIR": str(self.state),
            "DELEGATE_BIN": str(FAKE),
            "DELEGATE_SLOTS": "2",
            "DELEGATE_RUN_GRACE": "0.5",
            "DELEGATE_RUN_SLACK": "0.5",
        }
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    # -- helpers -------------------------------------------------------------

    def git(self, *args, repo=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        return subprocess.run(["git", "-C", str(repo or self.repo), *args], check=True,
                              capture_output=True, text=True, env=env).stdout

    def request(self, **kw):
        req = {"v": 1, "provider": "deepseek", "repo": str(self.repo),
               "task": "haz algo", "timeout": 60}
        req.update(kw)
        return req

    def run_cli(self, req, mode="ok_noop", extra_env=None, timeout=60):
        env = dict(self.env, FAKE_MODE=mode, **(extra_env or {}))
        raw = req if isinstance(req, (str, bytes)) else json.dumps(req)
        proc = subprocess.run([str(BIN)], input=raw if isinstance(raw, bytes) else raw.encode(),
                              capture_output=True, env=env, timeout=timeout)
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, out, proc.stderr.decode()

    def start_cli(self, req, mode, extra_env=None):
        env = dict(self.env, FAKE_MODE=mode, **(extra_env or {}))
        proc = subprocess.Popen([str(BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        proc.stdin.write(json.dumps(req).encode())
        proc.stdin.close()
        self.procs.append(proc)
        return proc

    @staticmethod
    def finish(proc, timeout=30):
        """(stdout, returncode) of a started run. communicate() trips on the
        stdin we already closed."""
        proc.wait(timeout=timeout)
        return proc.stdout.read(), proc.returncode

    def sub(self, *args):
        proc = subprocess.run([str(BIN), *args], capture_output=True, env=self.env,
                              text=True, timeout=60)
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, out, proc.stderr

    def wait_for(self, predicate, what, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def live_run(self):
        """run_id of the single run whose child has started."""
        def find():
            runs = self.state / "runs"
            if runs.is_dir():
                for rd in runs.iterdir():
                    if (rd / "child.pid").exists():
                        return rd.name
            return None
        return self.wait_for(find, "the child to start")

    def child_pids(self, run_id):
        wt = self.state / "worktrees" / run_id
        pid = int(self.wait_for(lambda: (wt / "child.pid").exists() and
                                (wt / "child.pid").read_text(), "child.pid"))
        gc_file = wt / "grandchild.pid"
        grand = int(gc_file.read_text()) if gc_file.exists() else None
        return pid, grand

    def assert_tree_gone(self, pid, grand):
        self.assertFalse(_alive(pid), "the delegate outlived the run")
        self.assertEqual(_session_members(pid), [], "its session still has processes")
        if grand is not None:
            self.assertFalse(_alive(grand), "a setsid grandchild outlived the run")

    # -- statuses and exit codes ---------------------------------------------

    def test_completed_exit_zero_and_branch(self):
        code, out, _ = self.run_cli(self.request(grants=["edit"]), "ok_edit")
        self.assertEqual((code, out["status"]), (0, contract.COMPLETED))
        self.assertEqual(out["changed_files"], [{"path": "edited.txt", "change": "A"}])
        branch = f"delegate/{out['run_id']}"
        self.assertEqual(out["branch"], branch)
        self.assertIn("fake delegate", self.git("show", f"{branch}:edited.txt"))
        on_disk = json.loads((Path(out["artifacts"]) / "result.json").read_text())
        self.assertEqual(on_disk["status"], contract.COMPLETED)
        self.assertEqual(on_disk["run_id"], out["run_id"])

    def test_delegate_never_commits_and_user_tree_untouched(self):
        head = self.git("rev-parse", "HEAD").strip()
        code, out, _ = self.run_cli(self.request(), "self_commit")
        self.assertEqual(code, 0)
        branch = out["branch"]
        # One commit by "delegate deepseek" on top of the base, whatever the
        # delegate did to its own HEAD.
        self.assertEqual(self.git("rev-parse", f"{branch}^").strip(), head)
        self.assertEqual(self.git("log", "-1", "--format=%an", branch).strip(),
                         "delegate deepseek")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_checks_failed_exit_one(self):
        code, out, _ = self.run_cli(self.request(checks=[["true"], ["false"]]))
        self.assertEqual((code, out["status"]), (1, contract.CHECKS_FAILED))
        self.assertEqual([c["exit"] for c in out["checks"]], [0, 1])
        for c in out["checks"]:
            self.assertTrue((Path(out["artifacts"]) / c["log"]).exists())

    def test_checks_come_from_the_request_not_the_tree(self):
        code, out, _ = self.run_cli(self.request(checks=[["false"]]), "plant_checks")
        self.assertEqual(out["status"], contract.CHECKS_FAILED)
        self.assertEqual([c["cmd"] for c in out["checks"]], [["false"]])

    def test_task_failed(self):
        code, out, _ = self.run_cli(self.request(), "fail")
        self.assertEqual((code, out["status"], out["child_exit"]), (1, contract.TASK_FAILED, 1))

    def test_rejected(self):
        code, out, _ = self.run_cli(self.request(), "reject")
        self.assertEqual((code, out["status"], out["child_exit"]), (1, contract.REJECTED, 2))

    def test_invalid_result(self):
        code, out, _ = self.run_cli(self.request(), "badjson")
        self.assertEqual((code, out["status"]), (1, contract.INVALID_RESULT))
        self.assertIsNone(out["usage"])

    def test_scope_violation_and_untracked_counted(self):
        code, out, _ = self.run_cli(self.request(scope=["src/**"]), "outside_scope")
        self.assertEqual(out["changed_files"], [{"path": "outside.txt", "change": "A"}])
        self.assertEqual(out["scope_violation"], ["outside.txt"])
        # The status alone is not the verdict: the caller checks both.
        self.assertEqual(out["status"], contract.COMPLETED)

    def test_timed_out_kills_everything(self):
        code, out, _ = self.run_cli(self.request(), "leak_hang",
                                    extra_env={"DELEGATE_RUN_WALL": "1"})
        self.assertEqual((code, out["status"]), (1, contract.TIMED_OUT))
        pid, grand = self.child_pids(out["run_id"])
        self.assert_tree_gone(pid, grand)

    def test_cancelled_via_subcommand(self):
        proc = self.start_cli(self.request(timeout=300), "leak_hang")
        run_id = self.live_run()
        pid, _ = self.child_pids(run_id)
        wt = self.state / "worktrees" / run_id
        self.wait_for(lambda: (wt / "grandchild.pid").exists(), "grandchild")
        grand = int((wt / "grandchild.pid").read_text())
        code, out, _ = self.sub("cancel", run_id)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["status"], contract.CANCELLED)
        stdout, rc = self.finish(proc)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout)["status"], contract.CANCELLED)
        self.assert_tree_gone(pid, grand)

    def test_cancel_a_finished_run_is_refused(self):
        _, out, _ = self.run_cli(self.request())
        code, got, _ = self.sub("cancel", out["run_id"])
        self.assertEqual(code, 1)
        self.assertFalse(got["cancelled"])
        self.assertEqual(got["status"], contract.COMPLETED)

    def test_busy_with_slots_full(self):
        slots = self.state / "slots"
        slots.mkdir(parents=True)
        holder = open(slots / "0", "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        code, out, _ = self.run_cli(self.request(wait=False),
                                    extra_env={"DELEGATE_SLOTS": "1"})
        self.assertEqual((code, out["status"]), (1, contract.BUSY))
        self.assertIsNone(out["worktree"])

    def test_two_runs_one_slot_wait(self):
        env = {"DELEGATE_SLOTS": "1"}
        first = self.start_cli(self.request(timeout=300), "sleep", env)
        run_id = self.live_run()
        code, out, _ = self.run_cli(self.request(wait=False), extra_env=env)
        self.assertEqual(out["status"], contract.BUSY)
        second = self.start_cli(self.request(), "ok_noop", env)
        time.sleep(0.5)
        self.assertIsNone(second.poll(), "the waiting run must not start yet")
        self.sub("cancel", run_id)
        self.finish(first)
        stdout, _ = self.finish(second)
        self.assertEqual(json.loads(stdout)["status"], contract.COMPLETED)

    # -- the user's tree -----------------------------------------------------

    def test_dirty_tree_rejected_without_running(self):
        (self.repo / "new.txt").write_text("wip\n")
        argv_file = self.tmp / "argv.json"
        code, out, _ = self.run_cli(self.request(), extra_env={"FAKE_ARGV_FILE": str(argv_file)})
        self.assertEqual((code, out["status"]), (1, contract.REJECTED))
        self.assertIsNone(out["child_exit"])
        self.assertFalse(argv_file.exists(), "the delegate ran on a dirty tree")

    def test_snapshot_runs_on_the_dirty_state(self):
        (self.repo / "README.md").write_text("changed\n")
        (self.repo / "new.txt").write_text("wip\n")
        code, out, _ = self.run_cli(self.request(snapshot=True), "ok_edit")
        self.assertEqual(code, 0)
        branch = out["branch"]
        self.assertEqual(self.git("show", f"{branch}:new.txt"), "wip\n")
        self.assertEqual(self.git("show", f"{branch}:README.md"), "changed\n")
        # The branch's own change is only what the delegate did.
        self.assertEqual(out["changed_files"], [{"path": "edited.txt", "change": "A"}])
        self.assertEqual(sorted(self.git("status", "--porcelain").splitlines()),
                         [" M README.md", "?? new.txt"])

    # -- refused before running ----------------------------------------------

    def test_invalid_request_exit_two_and_no_run(self):
        for raw in ("not json", json.dumps({"v": 1}),
                    json.dumps(self.request(base="main\u0000x"))):
            with self.subTest(raw=raw[:30]):
                code, out, err = self.run_cli(raw)
                self.assertEqual(code, 2)
                self.assertIsNone(out)
                self.assertIn("delegate-run:", err)
        self.assertFalse((self.state / "runs").exists() and
                         any((self.state / "runs").iterdir()))

    def test_unknown_subcommand_and_bad_options(self):
        self.assertEqual(self.sub("nope")[0], 2)
        self.assertEqual(self.sub("list", "--limit", "x")[0], 2)
        self.assertEqual(self.sub("status", "../etc")[0], 2)
        self.assertEqual(self.sub("cancel")[0], 2)
        self.assertEqual(self.sub("gc", "--days")[0], 2)

    # -- subcommands ---------------------------------------------------------

    def test_status_list_cost(self):
        _, done, _ = self.run_cli(self.request())
        _, failed, _ = self.run_cli(self.request(), "fail")
        code, got, _ = self.sub("status", done["run_id"])
        self.assertEqual((code, got["status"], got["alive"]), (0, contract.COMPLETED, False))
        self.assertEqual(self.sub("status", "20000101-000000-abcdef")[0], 1)
        code, rows, _ = self.sub("list")
        self.assertEqual(code, 0)
        self.assertEqual([r["run_id"] for r in rows], [failed["run_id"], done["run_id"]])
        code, rows, _ = self.sub("list", "--limit", "1")
        self.assertEqual(len(rows), 1)
        code, cost, _ = self.sub("cost", "--days", "1")
        self.assertEqual(code, 0)
        self.assertEqual(cost["runs"], 2)
        # No price rows shipped: unknown, and said so.
        self.assertIsNone(cost["total_cost_usd_estimate"])
        self.assertIn("cota inferior", cost["note"])

    def test_list_shows_a_live_run_as_running(self):
        proc = self.start_cli(self.request(timeout=300), "sleep")
        run_id = self.live_run()
        code, rows, _ = self.sub("list")
        self.assertEqual([(r["run_id"], r["status"], r["alive"]) for r in rows],
                         [(run_id, contract.RUNNING, True)])
        self.sub("cancel", run_id)
        self.finish(proc)

    def test_gc_removes_finished_and_never_a_live_run(self):
        _, done, _ = self.run_cli(self.request(), "ok_edit")
        proc = self.start_cli(self.request(timeout=300), "sleep")
        live = self.live_run()
        code, removed, _ = self.sub("gc", "--days", "0", "--dry-run")
        self.assertEqual([r["run_id"] for r in removed], [done["run_id"]])
        self.assertTrue(Path(done["worktree"]).exists())
        code, removed, _ = self.sub("gc", "--days", "0")
        self.assertEqual(removed, [{"run_id": done["run_id"], "removed": True, "error": None}])
        self.assertFalse(Path(done["worktree"]).exists())
        self.assertNotIn(done["branch"], self.git("branch", "--list"))
        self.assertTrue((self.state / "runs" / live).exists())
        self.assertTrue((self.state / "worktrees" / live).exists())
        self.sub("cancel", live)
        self.finish(proc)


if __name__ == "__main__":
    unittest.main()
