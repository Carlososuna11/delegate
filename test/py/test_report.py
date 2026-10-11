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
                                    "branch", "started_at", "finished_at"})
        self.assertEqual(row["provider"], "qwen")
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

    def test_shipped_table_loads_and_has_no_invented_prices(self):
        rows = report.load_prices()
        self.assertEqual(rows, {})
        self.assertTrue(report.PRICES_PATH.is_file())


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


if __name__ == "__main__":
    unittest.main()
