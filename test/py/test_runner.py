"""Tests for lib/delegate_run/runner.py and the bin/delegate-run CLI.

Run from the repo root:
    python3 -m unittest discover -s test/py -t .

Requests are built directly as contract.Request objects; request.parse_request
is written in parallel and is not exercised here.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import contract, runner  # noqa: E402
from delegate_run.contract import Request  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_delegate.py"
BIN = Path(__file__).resolve().parents[2] / "bin" / "delegate-run"

_ENV_KEYS = ("DELEGATE_STATE_DIR", "DELEGATE_BIN", "DELEGATE_SLOTS",
             "DELEGATE_RUN_GRACE", "DELEGATE_RUN_SLACK", "FAKE_MODE",
             "FAKE_ARGV_FILE")


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dlgtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.repo = self.tmp / "repo"
        self._init_repo()

        saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        self.addCleanup(self._restore_env, saved)
        os.environ["DELEGATE_STATE_DIR"] = str(self.state)
        os.environ["DELEGATE_BIN"] = str(FAKE)
        os.environ["DELEGATE_SLOTS"] = "2"
        os.environ["DELEGATE_RUN_GRACE"] = "0.5"
        os.environ["DELEGATE_RUN_SLACK"] = "0.5"
        os.environ.pop("FAKE_ARGV_FILE", None)
        os.chmod(FAKE, 0o755)

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- helpers -------------------------------------------------------------

    def _git_env(self):
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        return env

    def _git(self, repo, *args, check=True):
        return subprocess.run(["git", "-C", str(repo), *args], check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=self._git_env())

    def _init_repo(self):
        self.repo.mkdir()
        self._git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-q", "-m", "init")

    def _head(self):
        return self._git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def _child_pid(self, result):
        pid_file = Path(result.worktree) / "child.pid"
        return int(pid_file.read_text(encoding="utf-8").strip())

    def _req(self, **kwargs):
        base = dict(provider="deepseek", repo=str(self.repo), task="do the thing",
                    timeout=60)
        base.update(kwargs)
        return Request(**base)

    def _runs_dir(self):
        return self.state / "runs"

    def _wait_for_run(self):
        for _ in range(400):
            runs = self._runs_dir()
            if runs.exists():
                for run in runs.iterdir():
                    if (run / "child.pid").exists():
                        return run.name
            time.sleep(0.05)
        self.fail("the run never wrote run_dir/child.pid")

    def _wait_for(self, path):
        for _ in range(400):
            if path.exists():
                return path
            time.sleep(0.05)
        self.fail(f"{path} never appeared")



class RunnerTest(RunnerBase):
    # -- completed -----------------------------------------------------------

    def test_completed_noop(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual(result.child_exit, 0)
        self.assertIsNone(result.branch)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(result.scope_violation, [])
        self.assertEqual(result.usage,
                         {"input": 11, "output": 7, "cache_read": 3, "cache_write": 2})
        # The main model, not the first key of modelUsage.
        self.assertEqual(result.model, "deepseek-chat")
        # The harness's total_cost_usd uses Anthropic prices: never trusted.
        # The shipped price table is empty, so the estimate is unknown.
        self.assertIsNone(result.cost_usd_estimate)

    def test_cost_comes_from_the_price_table(self):
        header = ("provider\tmodel\tinput_per_mtok\toutput_per_mtok\t"
                  "cache_read_per_mtok\tcache_write_per_mtok\tas_of\n")
        rows = ("deepseek\tdeepseek-chat\t1000000\t1000000\t0\t0\t2026-10-01\n"
                "deepseek\tsmall-model\t1000000\t1000000\t0\t0\t2026-10-01\n")
        (self.state / "prices.tsv").write_text(header + rows, encoding="utf-8")
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req())
        # $1 per token in and out, cache free: (2+1) + (11+7), both models priced.
        self.assertAlmostEqual(result.cost_usd_estimate, 21.0)
        (self.state / "prices.tsv").write_text(header + rows.splitlines()[0] + "\n",
                                               encoding="utf-8")
        # One model without a row: no estimate, not a partial one.
        self.assertIsNone(runner.execute(self._req()).cost_usd_estimate)
        self.assertEqual(result.report, "fake delegate finished")
        self.assertEqual(result.base, self._head())

    def test_completed_edit_creates_branch_and_patch(self):
        os.environ["FAKE_MODE"] = "ok_edit"
        result = runner.execute(self._req(grants=("edit",)))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual(result.changed_files, [{"path": "edited.txt", "change": "A"}])
        self.assertEqual(result.scope_violation, [])
        branch = f"delegate/{result.run_id}"
        self.assertEqual(result.branch, branch)
        self._git(self.repo, "rev-parse", "--verify", branch)
        content = self._git(self.repo, "show", f"{branch}:edited.txt").stdout
        self.assertIn("fake delegate", content)
        patch = (Path(result.artifacts) / "patch.diff").read_text(encoding="utf-8")
        self.assertIn("edited.txt", patch)

    def test_untracked_file_is_reported_as_added(self):
        # A file the delegate creates is untracked; `git add -A` must catch it.
        os.environ["FAKE_MODE"] = "ok_edit"
        result = runner.execute(self._req())
        self.assertEqual([c["change"] for c in result.changed_files], ["A"])

    # -- failure exits -------------------------------------------------------

    def test_task_failed(self):
        os.environ["FAKE_MODE"] = "fail"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.TASK_FAILED)
        self.assertEqual(result.child_exit, 1)

    def test_rejected(self):
        os.environ["FAKE_MODE"] = "reject"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.REJECTED)
        self.assertEqual(result.child_exit, 2)
        self.assertTrue(result.error)

    def test_invalid_result(self):
        os.environ["FAKE_MODE"] = "badjson"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.INVALID_RESULT)
        self.assertTrue(result.error)

    def test_missing_delegate_is_infra_failed(self):
        os.environ["DELEGATE_BIN"] = str(self.tmp / "no-such-binary")
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.INFRA_FAILED)
        self.assertTrue(result.error)

    # -- scope ---------------------------------------------------------------

    def test_scope_violation(self):
        os.environ["FAKE_MODE"] = "outside_scope"
        result = runner.execute(self._req(scope=("src/**",)))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual(result.changed_files, [{"path": "outside.txt", "change": "A"}])
        self.assertEqual(result.scope_violation, ["outside.txt"])

    # -- checks --------------------------------------------------------------

    def test_checks_pass(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(checks=(("true",),)))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual(result.checks[0].exit, 0)
        self.assertTrue((Path(result.artifacts) / result.checks[0].log).exists())

    def test_checks_failed(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(checks=(("true",), ("false",))))
        self.assertEqual(result.status, contract.CHECKS_FAILED)
        self.assertEqual([c.exit for c in result.checks], [0, 1])

    def test_check_timeout_is_none(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(checks=(("sleep", "5"),), timeout=1))
        self.assertEqual(result.status, contract.CHECKS_FAILED)
        self.assertIsNone(result.checks[0].exit)

    # -- timeouts and cancel -------------------------------------------------

    def test_timeout_escalates_term_to_kill(self):
        os.environ["FAKE_MODE"] = "sleep_ignore_term"
        result = runner.execute(self._req(timeout=1))
        self.assertEqual(result.status, contract.TIMED_OUT)
        self.assertTrue(result.error)
        child_pid = self._child_pid(result)
        self.assertFalse(Path(f"/proc/{child_pid}").exists(),
                         "SIGKILL must have reaped the child that ignored SIGTERM")

    def test_cancel_reports_cancelled(self):
        os.environ["FAKE_MODE"] = "sleep_ignore_term"
        box = {}
        thread = threading.Thread(target=lambda: box.update(r=runner.execute(self._req(timeout=300))))
        thread.start()
        try:
            run_id = self._wait_for_run()
            # Let the child record its pid before cancelling, so the assertion
            # that KILL reaped it is not a race with process startup.
            self._wait_for(self.state / "worktrees" / run_id / "child.pid")
            out = runner.cancel(run_id)
            self.assertTrue(out["cancelled"], out)
            self.assertEqual(out["status"], contract.CANCELLED)
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "execute did not return after cancel")
            self.assertEqual(box["r"].status, contract.CANCELLED)
            child_pid = self._child_pid(box["r"])
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
        finally:
            if thread.is_alive():
                runner.cancel(run_id)

    def test_cancel_unknown_run_is_false(self):
        self.assertFalse(runner.cancel("20000101-000000-abcdef")["cancelled"])

    # -- slots ---------------------------------------------------------------

    def test_busy_when_no_slot_and_no_wait(self):
        self._hold_slot()
        os.environ["DELEGATE_SLOTS"] = "1"
        result = runner.execute(self._req(wait=False))
        self.assertEqual(result.status, contract.BUSY)
        self.assertTrue(result.error)

    def test_waits_for_a_free_slot(self):
        holder = self._hold_slot()
        os.environ["DELEGATE_SLOTS"] = "1"
        os.environ["FAKE_MODE"] = "ok_noop"
        box = {}
        thread = threading.Thread(target=lambda: box.update(r=runner.execute(self._req(wait=True))))
        thread.start()
        time.sleep(0.4)
        holder.close()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["r"].status, contract.COMPLETED)

    def _hold_slot(self, index=0):
        slots = self.state / "slots"
        slots.mkdir(parents=True, exist_ok=True)
        holder = open(slots / str(index), "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(holder.close)
        return holder

    # -- dirty tree ----------------------------------------------------------

    def test_dirty_tree_rejected(self):
        (self.repo / "README.md").write_text("modified\n", encoding="utf-8")
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.REJECTED)
        self.assertTrue(result.error)
        self.assertIsNone(result.worktree)
        # The user's working tree is never touched.
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "modified\n")

    def test_dirty_tree_snapshot(self):
        (self.repo / "README.md").write_text("modified\n", encoding="utf-8")
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(snapshot=True))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertNotEqual(result.base, self._head())
        snapshot_readme = (Path(result.worktree) / "README.md").read_text(encoding="utf-8")
        self.assertEqual(snapshot_readme, "modified\n")
        # The user's working tree is still the dirty one, untouched.
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "modified\n")

    # -- artifacts -----------------------------------------------------------

    def test_request_and_result_files(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(task="dime algo"))
        rd = Path(result.artifacts)
        self.assertEqual(rd.stat().st_mode & 0o777, 0o700)
        request_doc = json.loads((rd / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request_doc["task"], "dime algo")
        on_disk = json.loads((rd / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], contract.COMPLETED)
        self.assertFalse((rd / "child.pid").exists())
        # The runner's lock is released once the result is terminal.
        self.assertFalse(contract.runner_alive(rd))

    def test_child_argv_shape(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        argv_file = self.tmp / "argv.json"
        os.environ["FAKE_ARGV_FILE"] = str(argv_file)
        runner.execute(self._req(account="kontigo", grants=("edit", "bash"),
                                 mcp=("context7",), task="arregla el bug"))
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        self.assertEqual(argv[0], str(FAKE))
        self.assertEqual(argv[1], "deepseek")
        self.assertEqual(argv[2], "--json")
        self.assertIn("--account", argv)
        self.assertIn("--edit", argv)
        self.assertIn("--bash", argv)
        self.assertEqual(argv[argv.index("--mcp") + 1], "context7")
        self.assertEqual(argv[argv.index("--timeout") + 1], "60")
        self.assertEqual(argv[-2:], ["--", "arregla el bug"])

    # -- CLI -----------------------------------------------------------------

    def test_cli_help(self):
        proc = subprocess.run([str(BIN), "help"], capture_output=True, text=True,
                              env=dict(os.environ))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("uso:", proc.stdout)

    def test_cli_cancel_unknown_run(self):
        proc = subprocess.run([str(BIN), "cancel", "20000101-000000-abcdef"],
                              capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(proc.returncode, 1)
        out = json.loads(proc.stdout)
        self.assertEqual(out["run_id"], "20000101-000000-abcdef")
        self.assertFalse(out["cancelled"])


def _alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2] != "Z"


class ProcessCleanupTest(RunnerBase):
    """Nothing the delegate started survives the run, however it ends."""

    def _grandchild(self, result):
        path = Path(result.worktree) / "grandchild.pid"
        return int(path.read_text(encoding="utf-8"))

    def test_leftover_after_clean_exit_is_killed(self):
        os.environ["FAKE_MODE"] = "leak_ok"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertFalse(_alive(self._grandchild(result)),
                         "a setsid grandchild that ignores TERM outlived the run")

    def test_timeout_kills_grandchild_in_another_session(self):
        os.environ["FAKE_MODE"] = "leak_hang"
        result = runner.execute(self._req(timeout=1))
        self.assertEqual(result.status, contract.TIMED_OUT)
        self.assertFalse(_alive(self._child_pid(result)))
        self.assertFalse(_alive(self._grandchild(result)))

    def test_delegate_timeout_exit_is_timed_out(self):
        os.environ["FAKE_MODE"] = "timeout_exit"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.TIMED_OUT)
        self.assertEqual(result.child_exit, 124)

    def test_term_to_runner_cancels_and_cleans_up(self):
        os.environ["FAKE_MODE"] = "leak_hang"
        # Drive execute() in a separate process, so the TERM reaches a runner
        # that installed its handlers in the main thread, as the CLI does.
        script = (
            "import sys, os; sys.path.insert(0, %r)\n"
            "from delegate_run import runner, contract\n"
            "r = runner.execute(contract.Request(provider='deepseek', repo=%r, task='t', timeout=300))\n"
            "print(r.status, r.worktree)\n"
        ) % (str(Path(__file__).resolve().parents[2] / "lib"), str(self.repo))
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                text=True, env=dict(os.environ))
        run_id = self._wait_for_run()
        wt = self.state / "worktrees" / run_id
        self._wait_for(wt / "grandchild.pid")
        self._wait_for(wt / "child.pid")
        proc.terminate()
        out, _ = proc.communicate(timeout=30)
        status, worktree = out.split()
        self.assertEqual(status, contract.CANCELLED)
        self.assertFalse(_alive(int((wt / "child.pid").read_text())))
        self.assertFalse(_alive(int((wt / "grandchild.pid").read_text())))


class ScopeAndChangesTest(RunnerBase):
    def test_rename_checks_both_ends(self):
        os.environ["FAKE_MODE"] = "rename"
        result = runner.execute(self._req(scope=("src/**",)))
        self.assertEqual(sorted((c["path"], c["change"]) for c in result.changed_files),
                         [("README.md", "D"), ("src/README.md", "A")])
        self.assertEqual(result.scope_violation, ["README.md"])

    def test_newline_in_name_is_one_path(self):
        os.environ["FAKE_MODE"] = "odd_names"
        result = runner.execute(self._req(scope=("src/**",)))
        self.assertEqual(result.changed_files, [{"path": "evil\nsrc/ok.txt", "change": "A"}])
        self.assertEqual(result.scope_violation, ["evil\nsrc/ok.txt"])

    def test_glob_semantics(self):
        cases = [
            ("src/*.ts", "src/a.ts", True),
            ("src/*.ts", "src/deep/a.ts", False),
            ("src/**", "src/deep/a.ts", True),
            ("src/**/*.ts", "src/a.ts", True),
            ("src/**/*.ts", "src/x/y/a.ts", True),
            ("src", "src/x/y", True),
            ("src", "srcx/y", False),
            ("README.md", "README.md", True),
            ("*.md", "docs/a.md", False),
            ("src/[ab].ts", "src/a.ts", True),
            ("src/[!ab].ts", "src/a.ts", False),
            ("src/?.ts", "src//.ts", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(runner.in_scope(path, [pattern]), expected)

    def test_delegate_commit_is_still_measured_against_base(self):
        os.environ["FAKE_MODE"] = "self_commit"
        result = runner.execute(self._req())
        self.assertEqual(result.changed_files, [{"path": "edited.txt", "change": "A"}])
        parent = self._git(self.repo, "rev-parse", f"{result.branch}^").stdout.strip()
        self.assertEqual(parent, result.base)
        author = self._git(self.repo, "log", "-1", "--format=%an", result.branch).stdout.strip()
        self.assertEqual(author, "delegate deepseek")

    def test_check_writes_do_not_reach_the_branch(self):
        os.environ["FAKE_MODE"] = "ok_edit"
        result = runner.execute(self._req(checks=(("sh", "-c", "echo x > from-check.txt"),)))
        self.assertEqual(result.status, contract.COMPLETED)
        files = self._git(self.repo, "ls-tree", "--name-only", result.branch).stdout.split()
        self.assertIn("edited.txt", files)
        self.assertNotIn("from-check.txt", files)

    def test_repo_hooks_never_run(self):
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        marker = self.tmp / "hook-ran"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        os.environ["FAKE_MODE"] = "ok_edit"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertFalse(marker.exists())

    def test_is_error_with_exit_zero_is_task_failed(self):
        os.environ["FAKE_MODE"] = "is_error"
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.TASK_FAILED)
        self.assertIn("is_error", result.error)


class ChecksEnvTest(RunnerBase):
    def test_checks_do_not_see_caller_secrets(self):
        os.environ["FAKE_MODE"] = "ok_noop"
        os.environ["GITHUB_TOKEN"] = "sentinel-token-value"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        result = runner.execute(self._req(checks=(("env",),)))
        log = (Path(result.artifacts) / result.checks[0].log).read_text()
        self.assertNotIn("sentinel-token-value", log)
        self.assertIn("PATH=", log)

    def test_checks_never_resolve_a_binary_from_the_worktree(self):
        os.environ["FAKE_MODE"] = "plant_bin"
        old_path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        os.environ["PATH"] = ".:" + old_path
        result = runner.execute(self._req(checks=(("mycheck",),)))
        self.assertEqual(result.status, contract.CHECKS_FAILED)
        self.assertEqual(result.checks[0].exit, 127)


class PlantedBinaryTest(RunnerBase):
    def test_runner_never_runs_a_git_planted_in_the_worktree(self):
        # The delegate leaves an executable named `git` in the worktree, and
        # the caller's PATH has a relative entry. The runner's own git calls
        # after the child (add, write-tree, commit-tree) must not run it.
        os.environ["FAKE_MODE"] = "plant_git"
        marker = self.tmp / "planted-git-ran"
        os.environ["FAKE_MARKER"] = str(marker)
        self.addCleanup(os.environ.pop, "FAKE_MARKER", None)
        old_path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        os.environ["PATH"] = ".:" + old_path
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.COMPLETED, result.error)
        self.assertFalse(marker.exists(), "the runner ran the delegate's git")


class GitdirHijackTest(RunnerBase):
    def test_runner_ignores_a_rewritten_dot_git_file(self):
        os.environ["FAKE_MODE"] = "hijack_gitdir"
        marker = self.tmp / "filter-ran"
        os.environ["FAKE_MARKER"] = str(marker)
        self.addCleanup(os.environ.pop, "FAKE_MARKER", None)
        result = runner.execute(self._req())
        self.assertFalse(marker.exists(), "the runner ran a filter from the delegate's repo")
        # The branch lives in the real repository, not in the planted one.
        self.assertEqual(result.status, contract.COMPLETED, result.error)
        self._git(self.repo, "rev-parse", "--verify", result.branch)


class DirtyTreeTest(RunnerBase):
    def test_untracked_only_is_dirty(self):
        (self.repo / "new.txt").write_text("wip\n", encoding="utf-8")
        result = runner.execute(self._req())
        self.assertEqual(result.status, contract.REJECTED)
        self.assertIsNone(result.child_exit)
        self.assertIsNone(result.worktree)

    def test_untracked_only_ignores_show_untracked_config(self):
        self._git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / "new.txt").write_text("wip\n", encoding="utf-8")
        self.assertEqual(runner.execute(self._req()).status, contract.REJECTED)

    def test_snapshot_includes_untracked_and_leaves_index_alone(self):
        (self.repo / "README.md").write_text("modified\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("wip\n", encoding="utf-8")
        index_before = (self.repo / ".git" / "index").read_bytes()
        status_before = self._git(self.repo, "status", "--porcelain").stdout
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(snapshot=True))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual((Path(result.worktree) / "new.txt").read_text(), "wip\n")
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(self._git(self.repo, "status", "--porcelain").stdout, status_before)
        ref = f"refs/delegate/snapshot/{result.run_id}"
        self.assertEqual(self._git(self.repo, "rev-parse", ref).stdout.strip(), result.base)
        parent = self._git(self.repo, "rev-parse", f"{result.base}^").stdout.strip()
        self.assertEqual(parent, self._head())

    def test_dirty_tree_is_irrelevant_for_another_base(self):
        first = self._head()
        (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
        self._git(self.repo, "add", "b.txt")
        self._git(self.repo, "commit", "-q", "-m", "second")
        (self.repo / "wip.txt").write_text("wip\n", encoding="utf-8")
        os.environ["FAKE_MODE"] = "ok_noop"
        result = runner.execute(self._req(base=first))
        self.assertEqual(result.status, contract.COMPLETED)
        self.assertEqual(result.base, first)

    def test_snapshot_with_another_base_is_rejected(self):
        first = self._head()
        (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
        self._git(self.repo, "add", "b.txt")
        self._git(self.repo, "commit", "-q", "-m", "second")
        result = runner.execute(self._req(base=first, snapshot=True))
        self.assertEqual(result.status, contract.REJECTED)

    def test_unknown_base_is_rejected(self):
        result = runner.execute(self._req(base="no-such-ref"))
        self.assertEqual(result.status, contract.REJECTED)
        self.assertIn("no-such-ref", result.error)

    def test_not_a_repo_is_rejected(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        result = runner.execute(self._req(repo=str(plain)))
        self.assertEqual(result.status, contract.REJECTED)


if __name__ == "__main__":
    unittest.main()
