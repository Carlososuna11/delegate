"""Code-rule packs in delegate-run: the `rules` request key, the selection by
`origin`, what reaches `delegate`, what result.json records, and the remote
client resolving the packs on the laptop.

Run from the repo root:
    python3 -m unittest discover -s test/py -t .
"""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run import contract, request, runner  # noqa: E402

from .test_remote import RemoteBase  # noqa: E402
from .test_runner import RunnerBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
INNOVARES_ORIGIN = "git@github.com:Innovares/miempanada.git"
INNOVARES_REMOTE = "github.com[:/]Innovares/"


def write_pack(rules_dir: Path, pack_id: str, accounts: str, remotes: str | None,
               text: str = "Regla de prueba.\n") -> str:
    """A pack the way `delegate rules add` leaves it. Returns its sha."""
    d = rules_dir / pack_id
    d.mkdir(parents=True, mode=0o700)
    (d / "rules.md").write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    lines = [f"accounts\t{accounts}"]
    if remotes:
        lines.append(f"remotes\t{remotes}")
    lines += ["source\t/devkit/delegate/rules.md", f"sha256\t{sha}"]
    (d / "pack").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sha


def core_sha() -> str:
    return hashlib.sha256((ROOT / "rules" / "core.md").read_bytes()).hexdigest()


class RequestRulesTest(unittest.TestCase):
    def parse(self, **extra):
        doc = {"v": 1, "provider": "deepseek", "repo": "/tmp/x", "task": "t", **extra}
        return request.parse_request(json.dumps(doc))

    def test_absent_means_automatic(self):
        self.assertIsNone(self.parse().rules)

    def test_empty_list_turns_selection_off(self):
        self.assertEqual(self.parse(rules=[]).rules, ())

    def test_ids_are_kept_in_order(self):
        self.assertEqual(self.parse(rules=["innovares", "kontigo"]).rules,
                         ("innovares", "kontigo"))

    def test_bad_shapes_are_refused(self):
        for bad in ("innovares", ["../x"], ["Innovares"], ["a", "a"], [1]):
            with self.subTest(bad=bad), self.assertRaises(contract.RequestError):
                self.parse(rules=bad)


class RunnerRulesTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.rules_dir = self.tmp / "rules.d"
        self.rules_dir.mkdir(mode=0o700)
        self.assertEqual(os.environ["DELEGATE_RULES_DIR"], str(self.rules_dir))
        os.environ["FAKE_MODE"] = "ok_noop"
        self.argv_file = self.tmp / "argv.json"
        os.environ["FAKE_ARGV_FILE"] = str(self.argv_file)
        self.sha = write_pack(self.rules_dir, "innovares", "personal", INNOVARES_REMOTE)

    def argv(self):
        return json.loads(self.argv_file.read_text(encoding="utf-8"))

    def rules_args(self):
        argv = self.argv()
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--rules"]

    def set_origin(self, url):
        self._git(self.repo, "remote", "add", "origin", url)

    def test_origin_selects_the_pack_on_an_edit_run(self):
        self.set_origin(INNOVARES_ORIGIN)
        result = runner.execute(self._req(grants=("edit",)))
        self.assertEqual(result.status, contract.COMPLETED, result.error)
        self.assertEqual(self.rules_args(), ["innovares"])
        self.assertEqual(result.rules, {"core": core_sha(), "packs": {"innovares": self.sha}})
        on_disk = json.loads((Path(result.artifacts) / "result.json").read_text())
        self.assertEqual(on_disk["rules"], result.rules)

    def test_https_origin_matches_too(self):
        self.set_origin("https://github.com/Innovares/pharma.git")
        runner.execute(self._req(grants=("edit",)))
        self.assertEqual(self.rules_args(), ["innovares"])

    def test_other_origin_selects_nothing(self):
        self.set_origin("git@github.com:Carlososuna11/delegate.git")
        result = runner.execute(self._req(grants=("edit",)))
        self.assertEqual(self.rules_args(), [])
        self.assertEqual(result.rules, {"core": core_sha(), "packs": {}})

    def test_no_origin_selects_nothing(self):
        runner.execute(self._req(grants=("edit",)))
        self.assertEqual(self.rules_args(), [])

    def test_read_only_run_gets_no_automatic_pack_and_no_core(self):
        self.set_origin(INNOVARES_ORIGIN)
        result = runner.execute(self._req())
        self.assertEqual(self.rules_args(), [])
        self.assertEqual(result.rules, {"core": None, "packs": {}})

    def test_empty_rules_turns_selection_off(self):
        self.set_origin(INNOVARES_ORIGIN)
        runner.execute(self._req(grants=("edit",), rules=()))
        self.assertEqual(self.rules_args(), [])

    def test_explicit_rules_win_and_reach_a_read_only_run(self):
        # A personal fork of an Innovares repo: the origin does not match, the
        # caller asks for the pack by name.
        self.set_origin("git@github.com:Carlososuna11/miempanada.git")
        result = runner.execute(self._req(rules=("innovares",)))
        self.assertEqual(self.rules_args(), ["innovares"])
        self.assertEqual(result.rules["packs"], {"innovares": self.sha})

    def test_account_outside_the_pack_is_not_auto_selected(self):
        # Kontigo never gets the Innovares pack by selection; asking for it by
        # name is delegate's refusal to make (exit 2), not a silent pick here.
        self.set_origin(INNOVARES_ORIGIN)
        runner.execute(self._req(grants=("edit",), account="kontigo"))
        self.assertEqual(self.rules_args(), [])

    def test_unknown_explicit_pack_is_recorded_without_a_sha(self):
        result = runner.execute(self._req(rules=("nadie",)))
        self.assertEqual(self.rules_args(), ["nadie"])
        self.assertEqual(result.rules["packs"], {"nadie": None})

    def test_request_json_keeps_what_was_asked(self):
        self.set_origin(INNOVARES_ORIGIN)
        result = runner.execute(self._req(grants=("edit",)))
        req_doc = json.loads((Path(result.artifacts) / "request.json").read_text())
        self.assertIsNone(req_doc["rules"])


class RemoteRulesTest(RemoteBase):
    def setUp(self):
        super().setUp()
        self.laptop_rules = self.tmp / "laptop-rules"
        self.laptop_rules.mkdir(mode=0o700)
        self.env["DELEGATE_RULES_DIR"] = str(self.laptop_rules)
        self.remote_rules = self.tmp / "remote-rules"
        self.remote_rules.mkdir(mode=0o700)
        self.remote_env["DELEGATE_RULES_DIR"] = str(self.remote_rules)
        self.remote_argv = self.tmp / "remote-argv.json"
        self.remote_env["FAKE_ARGV_FILE"] = str(self.remote_argv)
        self.git("remote", "add", "origin", INNOVARES_ORIGIN)

    def remote_rules_args(self):
        argv = json.loads(self.remote_argv.read_text())
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--rules"]

    def test_client_resolves_the_packs_and_sends_them(self):
        sha = write_pack(self.laptop_rules, "innovares", "personal", INNOVARES_REMOTE)
        write_pack(self.remote_rules, "innovares", "personal", INNOVARES_REMOTE)
        code, out, err = self.run_remote(self.request(grants=["edit"]))
        self.assertEqual(code, 0, err)
        rreq = json.loads((self.rstate / "runs" / out["run_id"] / "request.json").read_text())
        # The mirror has no origin: the host gets the list, never decides it.
        self.assertEqual(rreq["rules"], ["innovares"])
        self.assertEqual(self.remote_rules_args(), ["innovares"])
        self.assertEqual(out["rules"]["packs"], {"innovares": sha})
        self.assertNotIn("difieren", err)

    def test_different_pack_on_the_host_is_reported(self):
        write_pack(self.laptop_rules, "innovares", "personal", INNOVARES_REMOTE)
        write_pack(self.remote_rules, "innovares", "personal", INNOVARES_REMOTE,
                   text="Otra version.\n")
        code, out, err = self.run_remote(self.request(grants=["edit"]))
        self.assertEqual(code, 0, err)
        self.assertIn("difieren", err)
        self.assertIn("innovares", err)

    def test_no_pack_on_the_laptop_sends_none_and_the_host_selects_none(self):
        # The host has a matching pack, but the choice is the laptop's: with
        # none chosen there, the key is left out (an older host still accepts
        # the request) and the mirror's missing origin selects nothing.
        write_pack(self.remote_rules, "innovares", "personal", INNOVARES_REMOTE)
        code, out, err = self.run_remote(self.request(grants=["edit"]))
        self.assertEqual(code, 0, err)
        rreq = json.loads((self.rstate / "runs" / out["run_id"] / "request.json").read_text())
        self.assertIsNone(rreq["rules"])
        self.assertEqual(self.remote_rules_args(), [])
        self.assertEqual(out["rules"]["packs"], {})


if __name__ == "__main__":
    unittest.main()
