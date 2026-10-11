"""Optional smoke against the real harness and a real provider.

Skipped unless DELEGATE_REAL_HARNESS=1. It spends real tokens (a few cents at
most) and needs a configured DeepSeek account; delegate reads the key itself,
this test never touches it. It runs THIS checkout's bin/delegate, not the one
installed on PATH.

    DELEGATE_REAL_HARNESS=1 python3 -m unittest test.py.test_real_harness
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "delegate-run"
DELEGATE = ROOT / "bin" / "delegate"


@unittest.skipUnless(os.environ.get("DELEGATE_REAL_HARNESS") == "1",
                     "set DELEGATE_REAL_HARNESS=1 to run against the real provider")
class RealHarnessSmoke(unittest.TestCase):
    def test_trivial_edit_completes_on_a_branch(self):
        tmp = Path(tempfile.mkdtemp(prefix="dlgreal-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "repo"
        repo.mkdir()
        env_git = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True, env=env_git).stdout

        git("init", "-q", "-b", "main")
        (repo / "README.md").write_text("smoke test repo\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "init")

        request = {
            "v": 1, "provider": "deepseek", "repo": str(repo), "grants": ["edit"],
            "timeout": 300,
            # An edit to an existing file, not a new one: with Claude Code
            # 2.1.295 the harness answers "No such tool available: Write"
            # although delegate grants it (see the spec, Fase B, limits).
            "task": ("In README.md replace the word 'smoke' with 'hola', using the "
                     "Edit tool. Do not create, modify or delete any other file. "
                     "Then reply 'done'."),
            "checks": [["grep", "-qx", "hola test repo", "README.md"]],
            "scope": ["README.md"],
        }
        env = dict(os.environ, DELEGATE_BIN=str(DELEGATE),
                   DELEGATE_STATE_DIR=str(tmp / "state"))
        proc = subprocess.run([str(BIN)], input=json.dumps(request).encode(),
                              capture_output=True, env=env, timeout=420)
        out = json.loads(proc.stdout)
        detail = json.dumps({k: out.get(k) for k in
                             ("status", "error", "child_exit", "changed_files",
                              "scope_violation", "checks", "model", "usage", "sandbox")},
                            ensure_ascii=False)
        self.assertEqual(out["status"], "completed", detail)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out["scope_violation"], [], detail)
        self.assertEqual(out["changed_files"], [{"path": "README.md", "change": "M"}])
        self.assertEqual(git("show", f"{out['branch']}:README.md").strip(), "hola test repo")
        self.assertEqual(git("status", "--porcelain"), "")
        self.assertIsNotNone(out["usage"], "the harness JSON carried no usage")
        # On a machine where bwrap works, the default (auto) must have used it.
        if os.environ.get("DELEGATE_SANDBOX", "auto") in ("auto", "bwrap") and shutil.which("bwrap"):
            self.assertEqual(out["sandbox"], "bwrap", detail)
        print("\nreal smoke:", detail)


if __name__ == "__main__":
    unittest.main()
