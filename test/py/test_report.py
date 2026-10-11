"""Tests for delegate_run.report: status, list, cost and gc."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import contract, report  # noqa: E402

HEADER = ("provider\tmodel\tinput_per_mtok\toutput_per_mtok\t"
          "cache_read_per_mtok\tcache_write_per_mtok\tas_of")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(path))
    git("-C", str(path), "config", "user.email", "t@example.com")
    git("-C", str(path), "config", "user.name", "tester")
    (path / "f.txt").write_text("hi\n", encoding="utf-8")
    git("-C", str(path), "add", "f.txt")
    git("-C", str(path), "commit", "-qm", "init")


def write_run(run_id, result=None, request=None):
    d = contract.run_dir(run_id)
    if request is not None:
        contract.write_json_atomic(d / "request.json", request)
    if result is not None:
        contract.write_json_atomic(d / "result.json", result)
    return d


def hold_lock(test, rd):
    """Hold the run lock the way a live runner does, until the test ends."""
    fd = os.open(contract.lock_path(rd), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    test.addCleanup(os.close, fd)


class ReportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = os.environ.get("DELEGATE_STATE_DIR")
        os.environ["DELEGATE_STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.old_env is None:
            os.environ.pop("DELEGATE_STATE_DIR", None)
        else:
            os.environ["DELEGATE_STATE_DIR"] = self.old_env

    def result(self, run_id, **kw):
        base = {"v": 1, "run_id": run_id, "status": contract.COMPLETED}
        base.update(kw)
        return base


class StatusTests(ReportTestBase):
    def test_returns_parsed_result_dict(self):
        write_run("20260101-000000-aaaaaa",
                  result=self.result("20260101-000000-aaaaaa", provider="deepseek"))
        got = report.status("20260101-000000-aaaaaa")
        self.assertEqual(got["provider"], "deepseek")
        self.assertEqual(got["status"], contract.COMPLETED)

    def test_missing_run_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            report.status("20200101-000000-abcdef")

    def test_error_message_is_spanish(self):
        try:
            report.status("20200101-000000-abcdef")
        except FileNotFoundError as e:
            self.assertIn("no existe", str(e))
        else:
            self.fail("expected FileNotFoundError")

    def test_running_result_is_returned_while_the_lock_is_held(self):
        rd = write_run("20260101-000000-bbbbbb",
                       result=self.result("20260101-000000-bbbbbb", status=contract.RUNNING))
        hold_lock(self, rd)
        got = report.status("20260101-000000-bbbbbb")
        self.assertEqual(got["status"], contract.RUNNING)
        self.assertTrue(got["alive"])

    def test_running_without_runner_is_abandoned(self):
        write_run("20260101-000000-bbbbbb",
                  result=self.result("20260101-000000-bbbbbb", status=contract.RUNNING))
        got = report.status("20260101-000000-bbbbbb")
        self.assertEqual(got["status"], contract.ABANDONED)
        self.assertFalse(got["alive"])

    def test_invalid_run_id_is_value_error(self):
        for bad in ("..", "../x", "20260101-000000-zzzzzz", "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                report.status(bad)


class ListRunsTests(ReportTestBase):
    def test_newest_first(self):
        for rid in ("20260101-000000-aaaaaa", "20260102-000000-bbbbbb",
                    "20260103-000000-cccccc"):
            write_run(rid, result=self.result(rid))
        got = report.list_runs()
        self.assertEqual([r["run_id"] for r in got],
                         ["20260103-000000-cccccc", "20260102-000000-bbbbbb",
                          "20260101-000000-aaaaaa"])

    def test_limit(self):
        for rid in ("20260101-000000-aaaaaa", "20260102-000000-bbbbbb",
                    "20260103-000000-cccccc"):
            write_run(rid, result=self.result(rid))
        got = report.list_runs(limit=2)
        self.assertEqual([r["run_id"] for r in got],
                         ["20260103-000000-cccccc", "20260102-000000-bbbbbb"])

    def test_fields(self):
        write_run("20260101-000000-aaaaaa",
                  result=self.result("20260101-000000-aaaaaa", provider="qwen",
                                     base="deadbeef", branch="delegate/x",
                                     started_at=10.0, finished_at=20.0))
        row = report.list_runs()[0]
        self.assertEqual(set(row), {"run_id", "status", "alive", "provider", "base",
                                    "branch", "host", "started_at", "finished_at"})
        self.assertEqual(row["provider"], "qwen")
        self.assertIsNone(row["host"])
        self.assertEqual(row["started_at"], 10.0)

    def test_lists_malformed_missing_and_running_results(self):
        write_run("20260101-000000-aaaaaa", result=self.result("20260101-000000-aaaaaa"))
        bad = contract.run_dir("20260102-000000-bbbbbb")
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "result.json").write_text("not json", encoding="utf-8")
        write_run("20260103-000000-cccccc", request={"repo": "/x"})  # no result
        live = write_run("20260104-000000-dddddd", request={"repo": "/x"})
        hold_lock(self, live)  # a runner that has not written result.json yet
        got = {r["run_id"]: r["status"] for r in report.list_runs()}
        self.assertEqual(got, {
            "20260101-000000-aaaaaa": contract.COMPLETED,
            "20260102-000000-bbbbbb": contract.UNREADABLE,
            "20260103-000000-cccccc": contract.UNREADABLE,
            "20260104-000000-dddddd": contract.RUNNING,
        })

    def test_ignores_dirs_that_are_not_run_ids(self):
        (contract.state_root() / "runs" / "junk").mkdir(parents=True)
        self.assertEqual(report.list_runs(), [])

    def test_empty_when_no_runs(self):
        self.assertEqual(report.list_runs(), [])


class PricesTests(ReportTestBase):
    def prices_file(self, content):
        p = Path(self.tmp.name) / "prices.tsv"
        p.write_text(content, encoding="utf-8")
        return p

    def test_parses_rows(self):
        p = self.prices_file(f"# comment\n{HEADER}\ndeepseek\tchat\t1\t2\t0.5\t0.25\t2026-01-01\n")
        rows = report.load_prices(p)
        self.assertEqual(rows[("deepseek", "chat")]["input_per_mtok"], 1.0)
        self.assertEqual(rows[("deepseek", "chat")]["output_per_mtok"], 2.0)
        self.assertEqual(rows[("deepseek", "chat")]["as_of"], "2026-01-01")

    def test_rejects_wrong_column_count(self):
        p = self.prices_file(f"{HEADER}\ndeepseek\tchat\t1\t2\t0.5\n")
        with self.assertRaises(ValueError) as ctx:
            report.load_prices(p)
        self.assertIn("columnas", str(ctx.exception))

    def test_rejects_non_numeric_price(self):
        p = self.prices_file(f"{HEADER}\ndeepseek\tchat\tx\t2\t0.5\t0.25\t2026-01-01\n")
        with self.assertRaises(ValueError) as ctx:
            report.load_prices(p)
        self.assertIn("numérico", str(ctx.exception))

    def test_rejects_bad_header(self):
        p = self.prices_file("a\tb\tc\n")
        with self.assertRaises(ValueError):
            report.load_prices(p)

    def test_rejects_non_finite_negative_and_duplicate_prices(self):
        for row in ("p\tm\tnan\t1\t1\t1\t2026-01-01",
                    "p\tm\tinf\t1\t1\t1\t2026-01-01",
                    "p\tm\t-1\t1\t1\t1\t2026-01-01",
                    "p\tm\t1\t1\t1\t1\t"):
            with self.subTest(row=row):
                path = Path(self.tmp.name) / "p.tsv"
                path.write_text(HEADER + "\n" + row + "\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    report.load_prices(path)
        path = Path(self.tmp.name) / "p.tsv"
        ok = "p\tm\t1\t1\t1\t1\t2026-01-01"
        path.write_text(HEADER + "\n" + ok + "\n" + ok + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            report.load_prices(path)

    def test_shipped_table_says_where_every_price_comes_from(self):
        rows = report.load_prices(report.PRICES_PATH)
        self.assertIn(("deepseek", "deepseek-flash[1m]"), rows)
        for key, row in rows.items():
            with self.subTest(key=key):
                self.assertTrue(row["source"].startswith("https://"))
                self.assertRegex(row["as_of"], r"^\d{4}-\d{2}-\d{2}$")
        # Qwen's Token Plan bills credits with no published per-token rate.
        self.assertFalse([k for k in rows if k[0] == "qwen"])


class CostTests(ReportTestBase):
    def test_sums_and_counts_unknown_usage(self):
        r1 = self.result("20260101-000000-aaaaaa", provider="deepseek",
                         model="chat", started_at=time.time(),
                         usage={"input": 100, "output": 50, "cache_read": 10,
                                "cache_write": 5})
        r2 = self.result("20260102-000000-bbbbbb", provider="deepseek",
                         model="chat", started_at=time.time(), usage=None)
        write_run("20260101-000000-aaaaaa", result=r1)
        write_run("20260102-000000-bbbbbb", result=r2)
        out = report.cost(days=7)
        self.assertEqual(out["runs"], 2)
        b = out["by_provider_model"]["deepseek/chat"]
        self.assertEqual(b["runs"], 2)
        self.assertEqual(b["input"], 100)
        self.assertEqual(b["output"], 50)
        self.assertEqual(b["runs_with_unknown_usage"], 1)

    def test_uses_result_cost_when_present(self):
        r = self.result("20260101-000000-aaaaaa", provider="deepseek",
                        model="chat", started_at=time.time(), usage=None,
                        cost_usd_estimate=1.5)
        write_run("20260101-000000-aaaaaa", result=r)
        out = report.cost(days=7)
        self.assertAlmostEqual(out["total_cost_usd_estimate"], 1.5)
        self.assertEqual(out["note"], "estimación")

    def test_computes_from_prices_table(self):
        p = Path(self.tmp.name) / "prices.tsv"
        p.write_text(f"{HEADER}\ndeepseek\tchat\t1\t2\t0.5\t0.5\t2026-01-01\n",
                     encoding="utf-8")
        r = self.result("20260101-000000-aaaaaa", provider="deepseek",
                        model="chat", started_at=time.time(),
                        usage={"input": 1_000_000, "output": 1_000_000,
                               "cache_read": 0, "cache_write": 0},
                        cost_usd_estimate=None)
        write_run("20260101-000000-aaaaaa", result=r)
        out = report.cost(days=7, prices_path=p)
        self.assertAlmostEqual(out["total_cost_usd_estimate"], 3.0)
        self.assertEqual(out["price_table"], str(p))

    def test_lower_bound_note_when_cost_unknown(self):
        r = self.result("20260101-000000-aaaaaa", provider="deepseek",
                        model="unknown-model", started_at=time.time(),
                        usage={"input": 1, "output": 1, "cache_read": 0,
                               "cache_write": 0})
        write_run("20260101-000000-aaaaaa", result=r)
        out = report.cost(days=7)
        self.assertIn("cota inferior", out["note"])
        # Nothing known: null, never a 0.0 that reads as "free".
        self.assertIsNone(out["total_cost_usd_estimate"])
        self.assertIsNone(out["by_provider_model"]["deepseek/unknown-model"]["cost_usd_estimate"])

    def test_unknown_usage_is_null_not_zero(self):
        r = self.result("20260101-000000-aaaaaa", provider="deepseek", model="m",
                        started_at=time.time(), usage=None)
        write_run("20260101-000000-aaaaaa", result=r)
        bucket = report.cost(days=7)["by_provider_model"]["deepseek/m"]
        self.assertIsNone(bucket["input"])
        self.assertEqual(bucket["runs_with_unknown_usage"], 1)

    def test_state_dir_price_table_wins(self):
        (Path(self.tmp.name) / "prices.tsv").write_text(
            HEADER + "\ndeepseek\tm\t1\t2\t0.5\t0\t2026-10-01\n", encoding="utf-8")
        self.assertEqual(report.default_prices_path(), Path(self.tmp.name) / "prices.tsv")
        r = self.result("20260101-000000-aaaaaa", provider="deepseek", model="m",
                        started_at=time.time(),
                        usage={"input": 1_000_000, "output": 1_000_000,
                               "cache_read": 0, "cache_write": 0})
        write_run("20260101-000000-aaaaaa", result=r)
        self.assertAlmostEqual(report.cost(days=7)["total_cost_usd_estimate"], 3.0)


    def test_ignores_runs_older_than_days(self):
        now = time.time()
        write_run("20260101-000000-aaaaaa",
                  result=self.result("20260101-000000-aaaaaa", provider="deepseek",
                                     model="chat", started_at=now, usage=None))
        write_run("20260102-000000-bbbbbb",
                  result=self.result("20260102-000000-bbbbbb", provider="deepseek",
                                     model="chat", started_at=now - 40 * 86400,
                                     usage=None))
        out = report.cost(days=7)
        self.assertEqual(out["runs"], 1)

    def test_empty_state(self):
        out = report.cost(days=7)
        self.assertEqual(out["runs"], 0)
        self.assertEqual(out["by_provider_model"], {})
        self.assertEqual(out["total_cost_usd_estimate"], 0.0)
        self.assertEqual(out["note"], "estimación")


class EstimateCostTests(unittest.TestCase):
    PRICES = {("deepseek", "big"): {"input_per_mtok": 1.0, "output_per_mtok": 2.0,
                                    "cache_read_per_mtok": 0.1, "cache_write_per_mtok": 0.0,
                                    "as_of": "2026-10-01"}}

    def per(self, i, o, cr=0, cw=0):
        return {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr,
                "cacheCreationInputTokens": cw}

    def test_per_model_breakdown(self):
        got = report.estimate_cost("deepseek", "big", None,
                                   {"big": self.per(1_000_000, 500_000, 1_000_000)},
                                   self.PRICES)
        self.assertAlmostEqual(got, 1.0 + 1.0 + 0.1)

    def test_any_model_without_price_gives_none(self):
        got = report.estimate_cost("deepseek", "big", None,
                                   {"big": self.per(1, 1), "small": self.per(1, 1)},
                                   self.PRICES)
        self.assertIsNone(got)

    def test_missing_count_gives_none(self):
        per = self.per(1, 1)
        del per["cacheReadInputTokens"]
        self.assertIsNone(report.estimate_cost("deepseek", "big", None, {"big": per},
                                               self.PRICES))

    def test_top_level_usage_fallback(self):
        usage = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0}
        self.assertAlmostEqual(report.estimate_cost("deepseek", "big", usage, None,
                                                    self.PRICES), 1.0)
        self.assertIsNone(report.estimate_cost("deepseek", "other", usage, None,
                                               self.PRICES))


class GcTests(ReportTestBase):
    def old(self, run_id, **kw):
        kw.setdefault("finished_at", time.time() - 30 * 86400)
        return self.result(run_id, **kw)

    def test_removes_finished_run_with_worktree_and_branch(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        wt = contract.state_root() / "worktrees" / run_id
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(wt), "HEAD")
        git("-C", str(repo), "branch", f"delegate/{run_id}")
        write_run(run_id,
                  result=self.old(run_id, worktree=str(wt), branch=f"delegate/{run_id}"),
                  request={"repo": str(repo)})

        out = report.gc(days=14)
        self.assertEqual(out, [{"run_id": run_id, "removed": True, "error": None}])
        self.assertFalse(wt.exists())
        self.assertFalse(contract.run_dir(run_id).exists())
        ref = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                              f"refs/heads/delegate/{run_id}"], capture_output=True)
        self.assertNotEqual(ref.returncode, 0)

    def test_dry_run_touches_nothing(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        wt = contract.state_root() / "worktrees" / run_id
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(wt), "HEAD")
        git("-C", str(repo), "branch", f"delegate/{run_id}")
        write_run(run_id,
                  result=self.old(run_id, worktree=str(wt), branch=f"delegate/{run_id}"),
                  request={"repo": str(repo)})

        out = report.gc(days=14, dry_run=True)
        self.assertEqual(out, [{"run_id": run_id, "removed": False, "error": None}])
        self.assertTrue(wt.exists())
        self.assertTrue(contract.run_dir(run_id).exists())
        ref = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                              f"refs/heads/delegate/{run_id}"], capture_output=True)
        self.assertEqual(ref.returncode, 0)

    def test_skips_running_run(self):
        run_id = "20200101-000000-aaaaaa"
        write_run(run_id, result=self.old(run_id, status=contract.RUNNING))
        self.assertEqual(report.gc(days=14), [])
        self.assertTrue(contract.run_dir(run_id).exists())

    def test_skips_run_without_result(self):
        run_id = "20200101-000000-aaaaaa"
        write_run(run_id, request={"repo": "/x"})
        self.assertEqual(report.gc(days=14), [])
        self.assertTrue(contract.run_dir(run_id).exists())

    def test_skips_recent_run(self):
        run_id = "20260101-000000-aaaaaa"
        write_run(run_id, result=self.result(run_id, finished_at=time.time()))
        self.assertEqual(report.gc(days=14), [])
        self.assertTrue(contract.run_dir(run_id).exists())

    def test_repo_gone_still_removes_our_worktree_dir(self):
        run_id = "20200101-000000-aaaaaa"
        wt = contract.state_root() / "worktrees" / run_id
        wt.mkdir(parents=True)
        write_run(run_id,
                  result=self.old(run_id, worktree=str(wt)),
                  request={"repo": str(Path(self.tmp.name) / "no-such-repo")})
        out = report.gc(days=14)
        self.assertEqual(out, [{"run_id": run_id, "removed": True, "error": None}])
        self.assertFalse(wt.exists())

    def test_never_touches_a_run_whose_lock_is_held(self):
        run_id = "20200101-000000-aaaaaa"
        rd = write_run(run_id, result=self.old(run_id))
        hold_lock(self, rd)
        self.assertEqual(report.gc(days=0), [])
        self.assertTrue(rd.exists())

    def test_abandoned_run_is_collected_only_when_old(self):
        old_id, new_id = "20200101-000000-aaaaaa", "20200101-000000-bbbbbb"
        long_ago = time.time() - 30 * 86400
        write_run(old_id, result=self.result(old_id, status=contract.RUNNING,
                                             started_at=long_ago))
        write_run(new_id, result=self.result(new_id, status=contract.RUNNING,
                                             started_at=time.time() - 3600))
        out = report.gc(days=0)
        self.assertEqual([o["run_id"] for o in out], [old_id])
        self.assertFalse(contract.run_dir(old_id).exists())
        self.assertTrue(contract.run_dir(new_id).exists())

    def test_snapshot_ref_is_removed(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        git("-C", str(repo), "update-ref", f"refs/delegate/snapshot/{run_id}", "HEAD")
        write_run(run_id, result=self.old(run_id), request={"repo": str(repo)})
        self.assertTrue(report.gc(days=14)[0]["removed"])
        ref = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                              f"refs/delegate/snapshot/{run_id}"], capture_output=True)
        self.assertNotEqual(ref.returncode, 0)

    def test_no_repo_still_removes_dir_without_worktree(self):
        run_id = "20200101-000000-aaaaaa"
        write_run(run_id, result=self.old(run_id))
        out = report.gc(days=14)
        self.assertEqual(out, [{"run_id": run_id, "removed": True, "error": None}])
        self.assertFalse(contract.run_dir(run_id).exists())

    def test_never_follows_a_foreign_worktree_or_branch(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        other = Path(self.tmp.name) / "other-wt"
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(other), "HEAD")
        for run_id, kw in (("20200101-000000-aaaaaa", {"worktree": str(other)}),
                           ("20200101-000000-bbbbbb", {"branch": "master"})):
            write_run(run_id, result=self.old(run_id, **kw), request={"repo": str(repo)})
        out = report.gc(days=14)
        self.assertEqual([o["removed"] for o in out], [False, False])
        self.assertTrue(other.exists())
        branches = subprocess.run(["git", "-C", str(repo), "branch", "--list"],
                                  capture_output=True, text=True).stdout
        self.assertIn("master", branches)


    # -- what a delegate with bash can leave behind --------------------------

    def _finished_run(self, repo, run_id):
        wt = contract.state_root() / "worktrees" / run_id
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(wt), "HEAD")
        write_run(run_id, result=self.old(run_id, worktree=str(wt),
                                          branch=f"delegate/{run_id}"),
                  request={"repo": str(repo)})
        return wt

    def test_a_symbolic_branch_ref_never_deletes_its_target(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        self._finished_run(repo, run_id)
        main = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        git("-C", str(repo), "symbolic-ref", f"refs/heads/delegate/{run_id}",
            f"refs/heads/{main}")
        out = report.gc(days=14)
        self.assertTrue(out[0]["removed"], out)
        alive = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                                f"refs/heads/{main}"], capture_output=True)
        self.assertEqual(alive.returncode, 0, "gc deleted the branch the symref pointed at")

    def test_a_symlinked_worktree_dir_never_removes_its_target(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        wt = self._finished_run(repo, run_id)
        git("-C", str(repo), "worktree", "remove", "--force", str(wt))
        victim = contract.state_root() / "worktrees" / "20990101-000000-bbbbbb"
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(victim), "HEAD")
        wt.symlink_to(victim, target_is_directory=True)
        report.gc(days=14)
        self.assertTrue(victim.is_dir(), "gc removed the worktree the symlink pointed at")
        listed = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True).stdout
        self.assertIn(str(victim), listed)
        self.assertFalse(wt.is_symlink())

    def test_an_unrelated_moved_worktree_keeps_its_registration(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        self._finished_run(repo, run_id)
        other = Path(self.tmp.name) / "unrelated"
        git("-C", str(repo), "worktree", "add", "--detach", "-q", str(other), "HEAD")
        other.rename(Path(self.tmp.name) / "unrelated-moved")
        report.gc(days=14)
        self.assertTrue((repo / ".git" / "worktrees" / "unrelated").exists(),
                        "gc pruned a registration that was not its own")

    def test_gc_never_runs_a_git_from_a_relative_path_entry(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        run_id = "20200101-000000-aaaaaa"
        wt = self._finished_run(repo, run_id)
        marker = Path(self.tmp.name) / "planted-ran"
        planted = wt / "git"
        planted.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        planted.chmod(0o755)
        old_path, old_cwd = os.environ["PATH"], os.getcwd()
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        self.addCleanup(os.chdir, old_cwd)
        os.environ["PATH"] = ".:" + old_path
        os.chdir(wt)
        report.gc(days=14)
        self.assertFalse(marker.exists(), "gc ran the git planted in the worktree")


class CostRepricingTests(ReportTestBase):
    def test_a_run_whose_models_are_not_all_priced_stays_unknown(self):
        # One model of two has a price: the run's estimate is None, and the
        # aggregate must not reprice its whole usage as the priced model.
        p = Path(self.tmp.name) / "prices.tsv"
        p.write_text(f"{HEADER}\ndeepseek\tbig\t1\t1\t0\t0\t2026-01-01\n", encoding="utf-8")
        run_id = "20260101-000000-aaaaaa"
        rd = write_run(run_id, result=self.result(
            run_id, provider="deepseek", model="big", started_at=time.time(),
            usage={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
            cost_usd_estimate=None))
        per = {"inputTokens": 500_000, "outputTokens": 0, "cacheReadInputTokens": 0,
               "cacheCreationInputTokens": 0}
        (rd / "stdout.log").write_text(json.dumps({"modelUsage": {"big": per, "small": per}}),
                                       encoding="utf-8")
        out = report.cost(days=7, prices_path=p)
        self.assertIsNone(out["total_cost_usd_estimate"])
        self.assertIn("cota inferior", out["note"])

    def test_a_table_filled_later_prices_every_model_of_an_old_run(self):
        p = Path(self.tmp.name) / "prices.tsv"
        p.write_text(f"{HEADER}\ndeepseek\tbig\t1\t0\t0\t0\t2026-01-01\n"
                     "deepseek\tsmall\t2\t0\t0\t0\t2026-01-01\n", encoding="utf-8")
        run_id = "20260101-000000-aaaaaa"
        rd = write_run(run_id, result=self.result(
            run_id, provider="deepseek", model="big", started_at=time.time(),
            usage={"input": 2_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
            cost_usd_estimate=None))
        per = {"inputTokens": 1_000_000, "outputTokens": 0, "cacheReadInputTokens": 0,
               "cacheCreationInputTokens": 0}
        (rd / "stdout.log").write_text(json.dumps({"modelUsage": {"big": per, "small": per}}),
                                       encoding="utf-8")
        out = report.cost(days=7, prices_path=p)
        self.assertAlmostEqual(out["total_cost_usd_estimate"], 3.0)


if __name__ == "__main__":
    unittest.main()



HEADER_V2 = HEADER + "\tsource\tpeak_utc\toffpeak_factor"
URL = "https://example.com/pricing"
WED_PEAK = 1791358200.0       # Wednesday 2026-10-07 07:30 UTC
WED_OFFPEAK = 1791374400.0    # Wednesday 2026-10-07 12:00 UTC
WED_PEAK_END = 1791367200.0   # Wednesday 2026-10-07 10:00 UTC: end is exclusive
SAT_MORNING = 1791617400.0    # Saturday 2026-10-10 07:30 UTC


class PricesV2Tests(ReportTestBase):
    def table(self, *rows):
        p = Path(self.tmp.name) / "p.tsv"
        p.write_text(HEADER_V2 + "\n" + "".join(r + "\n" for r in rows), encoding="utf-8")
        return p

    def test_parses_source_and_schedule(self):
        p = self.table(f"ds\tflash\t0.3\t1.2\t0.006\t0.3\t2026-10-10\t{URL}"
                       "\tmon-fri 01-04,06-10\t0.5",
                       f"q\tm\t1\t1\t1\t1\t2026-10-10\t{URL}\t-\t-")
        rows = report.load_prices(p)
        flash = rows[("ds", "flash")]
        self.assertEqual(flash["source"], URL)
        self.assertEqual(flash["peak_utc"], (frozenset(range(5)), ((1, 4), (6, 10))))
        self.assertEqual(flash["offpeak_factor"], 0.5)
        self.assertIsNone(rows[("q", "m")]["peak_utc"])

    def test_legacy_header_still_loads(self):
        p = Path(self.tmp.name) / "p.tsv"
        p.write_text(HEADER + "\nds\tm\t1\t1\t1\t1\t2026-01-01\n", encoding="utf-8")
        row = report.load_prices(p)[("ds", "m")]
        self.assertIsNone(row["source"])
        self.assertIsNone(row["peak_utc"])

    def test_rejects_bad_v2_rows(self):
        base = "ds\tm\t1\t1\t1\t1\t2026-10-10\t"
        for row in (base + "\t-\t-",                               # no source
                    base + "http://x.example\t-\t-",               # not https
                    base + f"{URL}\tmon-fri 01-04\t-",             # half a schedule
                    base + f"{URL}\t-\t0.5",
                    base + f"{URL}\tmon-fri 01-04\t0",             # factor out of (0, 1]
                    base + f"{URL}\tmon-fri 01-04\t1.5",
                    base + f"{URL}\tmon-fri 01-04\tnan",
                    base + f"{URL}\tfri-mon 01-04\t0.5",           # reversed days
                    base + f"{URL}\tmonday 01-04\t0.5",
                    base + f"{URL}\tmon-fri 04-01\t0.5",           # reversed hours
                    base + f"{URL}\tmon-fri 01-25\t0.5",
                    base + f"{URL}\tmon-fri\t0.5",
                    base + f"{URL}\t-"):                            # column missing
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    report.load_prices(self.table(row))


class PeakPricingTests(unittest.TestCase):
    ROW = {"input_per_mtok": 0.3, "output_per_mtok": 1.2, "cache_read_per_mtok": 0.006,
           "cache_write_per_mtok": 0.3, "as_of": "2026-10-10", "source": URL,
           "peak_utc": (frozenset(range(5)), ((1, 4), (6, 10))), "offpeak_factor": 0.5}
    PRICES = {("deepseek", "deepseek-flash[1m]"): ROW}
    USAGE = {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000,
             "cache_write": 0}
    PEAK = 0.3 + 1.2 + 0.006

    def cost(self, at):
        return report.estimate_cost("deepseek", "deepseek-flash[1m]", self.USAGE, None,
                                    self.PRICES, at=at)

    def test_peak_window_on_a_weekday(self):
        self.assertAlmostEqual(self.cost(WED_PEAK), self.PEAK)

    def test_off_peak_is_the_factor(self):
        self.assertAlmostEqual(self.cost(WED_OFFPEAK), self.PEAK / 2)
        self.assertAlmostEqual(self.cost(WED_PEAK_END), self.PEAK / 2)

    def test_weekend_is_off_peak_all_day(self):
        self.assertAlmostEqual(self.cost(SAT_MORNING), self.PEAK / 2)

    def test_unknown_time_is_priced_at_peak(self):
        # Never under-estimate: without a start time the higher price applies.
        self.assertAlmostEqual(self.cost(None), self.PEAK)

    def test_per_model_breakdown_uses_the_time_too(self):
        per = {"inputTokens": 1_000_000, "outputTokens": 0, "cacheReadInputTokens": 0,
               "cacheCreationInputTokens": 0}
        got = report.estimate_cost("deepseek", None, None, {"deepseek-flash[1m]": per},
                                   self.PRICES, at=SAT_MORNING)
        self.assertAlmostEqual(got, 0.15)


class StatsTests(ReportTestBase):
    """Synthetic result.json fixtures; nothing runs."""

    def setUp(self):
        super().setUp()
        self.prices = Path(self.tmp.name) / "fixture-prices.tsv"
        self.prices.write_text(
            HEADER_V2 + "\n"
            f"deepseek\tflash\t1\t2\t0.1\t1\t2026-10-10\t{URL}\t-\t-\n",
            encoding="utf-8")
        self.now = time.time()
        self.seq = 0

    def add(self, provider="deepseek", model="flash", status=contract.COMPLETED,
            age_days=0.0, seconds=100.0, usage="default", files=1, scope=(),
            cost=None, turns=None):
        self.seq += 1
        run_id = f"20260101-0000{self.seq:02d}-{self.seq:06x}"
        started = self.now - age_days * 86400 - 1000
        if usage == "default":
            usage = {"input": 1_000_000, "output": 500_000, "cache_read": 2_000_000,
                     "cache_write": 0}
        r = self.result(run_id, provider=provider, model=model, status=status,
                        started_at=started, finished_at=started + seconds, usage=usage,
                        changed_files=[{"path": f"f{i}", "change": "M"} for i in range(files)],
                        scope_violation=list(scope), cost_usd_estimate=cost)
        if turns is not None:
            r["num_turns"] = turns
        write_run(run_id, result=r)
        return run_id

    def stats(self, **kw):
        return report.stats(prices_path=self.prices, **kw)

    def test_mixed_providers_statuses_and_rates(self):
        self.add(seconds=60, turns=4)
        self.add(seconds=120, status=contract.CHECKS_FAILED, files=3, turns=6)
        self.add(seconds=600, status=contract.TIMED_OUT, scope=("x.txt",))
        self.add(provider="qwen", model="qwen3.8-max", seconds=300)
        out = self.stats(days=7)
        self.assertEqual(out["runs"], 4)
        ds = out["by_provider_model"]["deepseek/flash"]
        self.assertEqual(ds["runs"], 3)
        self.assertEqual(ds["status"], {contract.CHECKS_FAILED: 1, contract.COMPLETED: 1,
                                        contract.TIMED_OUT: 1})
        self.assertAlmostEqual(ds["first_try_success_rate"], 1 / 3)
        self.assertAlmostEqual(ds["duration_seconds"]["median"], 120)
        self.assertAlmostEqual(ds["duration_seconds"]["p90"], 600)
        self.assertEqual(ds["num_turns"], {"median": 5, "known_runs": 2})
        self.assertAlmostEqual(ds["files_changed_per_run"], 5 / 3)
        self.assertEqual(ds["scope_violation_runs"], 1)
        self.assertEqual(ds["tokens"]["input"], {"total": 3_000_000, "per_run": 1_000_000})
        # 1 + 1 + 0.2 = 2.2 per run at the fixture prices.
        self.assertAlmostEqual(ds["cost_usd_estimate"]["known_total"], 6.6)
        self.assertAlmostEqual(ds["cost_usd_estimate"]["per_run"], 2.2)
        self.assertEqual(out["by_provider"]["deepseek"]["runs"], 3)
        qw = out["by_provider_model"]["qwen/qwen3.8-max"]
        self.assertEqual(qw["first_try_success_rate"], 1.0)

    def test_unknown_model_price_is_never_zero(self):
        self.add()
        self.add(provider="qwen", model="qwen3.8-max")
        out = self.stats(days=7)
        qw = out["by_provider_model"]["qwen/qwen3.8-max"]["cost_usd_estimate"]
        self.assertEqual(qw, {"known_total": None, "known_runs": 0, "per_run": None,
                              "unknown_runs": 1})
        self.assertAlmostEqual(out["cost_usd_estimate"]["known_total"], 2.2)
        self.assertEqual(out["cost_usd_estimate"]["unknown_runs"], 1)
        self.assertIn("cota inferior", report.format_stats(out))

    def test_unknown_usage(self):
        self.add(usage=None)
        self.add(usage={"input": 10, "output": None, "cache_read": 0, "cache_write": 0})
        self.add()
        ds = self.stats(days=7)["by_provider_model"]["deepseek/flash"]
        self.assertEqual(ds["runs_with_unknown_usage"], 2)
        # Averages over the one run whose usage is known, not over three.
        self.assertEqual(ds["tokens"]["input"], {"total": 1_000_000, "per_run": 1_000_000})
        self.assertEqual(ds["cost_usd_estimate"]["unknown_runs"], 2)

    def test_all_usage_unknown_is_null(self):
        self.add(usage=None)
        ds = self.stats(days=7)["by_provider_model"]["deepseek/flash"]
        self.assertEqual(ds["tokens"]["output"], {"total": None, "per_run": None})
        out = self.stats(days=7)
        self.assertIsNone(out["cost_usd_estimate"]["known_total"])

    def test_recorded_cost_wins_over_the_table(self):
        self.add(cost=0.5)
        ds = self.stats(days=7)["by_provider_model"]["deepseek/flash"]
        self.assertAlmostEqual(ds["cost_usd_estimate"]["known_total"], 0.5)

    def test_days_filter(self):
        self.add(age_days=1)
        self.add(age_days=10)
        self.add(age_days=40)
        self.assertEqual(self.stats(days=7)["runs"], 1)
        self.assertEqual(self.stats(days=30)["runs"], 2)
        self.assertEqual(self.stats(days=0)["runs"], 0)

    def test_provider_filter(self):
        self.add()
        self.add(provider="qwen", model="qwen3.8-max")
        out = self.stats(days=7, provider="qwen")
        self.assertEqual(list(out["by_provider_model"]), ["qwen/qwen3.8-max"])
        self.assertEqual(out["provider"], "qwen")

    def test_live_runs_are_left_out(self):
        run_id = self.add(status=contract.RUNNING)
        hold_lock(self, contract.run_dir(run_id))
        out = self.stats(days=7)
        self.assertEqual((out["runs"], out["running"]), (0, 1))

    def test_abandoned_run_counts_as_abandoned(self):
        self.add(status=contract.RUNNING)
        ds = self.stats(days=7)["by_provider_model"]["deepseek/flash"]
        self.assertEqual(ds["status"], {contract.ABANDONED: 1})
        self.assertEqual(ds["first_try_success_rate"], 0)

    def test_num_turns_falls_back_to_the_harness_json(self):
        run_id = self.add()
        (contract.run_dir(run_id) / "stdout.log").write_text(
            json.dumps({"num_turns": 7}), encoding="utf-8")
        ds = self.stats(days=7)["by_provider_model"]["deepseek/flash"]
        self.assertEqual(ds["num_turns"], {"median": 7, "known_runs": 1})

    def test_empty_state(self):
        out = self.stats(days=7)
        self.assertEqual(out["runs"], 0)
        self.assertIsNone(out["cost_usd_estimate"]["known_total"])
        self.assertIn("sin corridas", report.format_stats(out))

    def test_percentile_is_nearest_rank(self):
        self.assertIsNone(report._percentile([], 0.9))
        self.assertEqual(report._percentile([5], 0.9), 5)
        self.assertEqual(report._percentile(list(range(1, 11)), 0.9), 9)
        self.assertEqual(report._percentile(list(range(1, 12)), 0.9), 10)
