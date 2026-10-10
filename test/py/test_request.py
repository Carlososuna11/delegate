"""Tests for lib/delegate_run/request.py: parse_request validation.

Run from the repo root:
    python3 -m unittest discover -s test/py -t .
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Make `delegate_run` importable from the source tree, not an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from delegate_run.contract import GRANTS, Request, RequestError  # noqa: E402
from delegate_run.request import parse_request  # noqa: E402


def base(**overrides) -> dict:
    """A minimal valid request, with overrides applied."""
    req = {"v": 1, "provider": "deepseek", "repo": "/repo", "task": "haz algo"}
    req.update(overrides)
    return req


class RequestTestCase(unittest.TestCase):
    """Shared helpers: parse a JSON-serializable object, or assert rejection."""

    def parse(self, obj) -> Request:
        return parse_request(json.dumps(obj, ensure_ascii=False))

    def rejects(self, obj, *needles: str) -> str:
        with self.assertRaises(RequestError) as ctx:
            parse_request(json.dumps(obj, ensure_ascii=False))
        msg = str(ctx.exception)
        self.assertTrue(msg, "el mensaje de error no puede estar vacío")
        for needle in needles:
            self.assertIn(needle, msg)
        return msg

    def rejects_raw(self, raw, *needles: str) -> str:
        with self.assertRaises(RequestError) as ctx:
            parse_request(raw)
        msg = str(ctx.exception)
        self.assertTrue(msg)
        for needle in needles:
            self.assertIn(needle, msg)
        return msg


class ValidRequestTests(RequestTestCase):
    def test_minimal_request_uses_contract_defaults(self):
        req = self.parse(base())
        self.assertIsInstance(req, Request)
        self.assertEqual(req.provider, "deepseek")
        self.assertEqual(req.repo, "/repo")
        self.assertEqual(req.task, "haz algo")
        self.assertEqual(req.base, "HEAD")
        self.assertIsNone(req.account)
        self.assertEqual(req.grants, ())
        self.assertEqual(req.mcp, ())
        self.assertEqual(req.timeout, 1800)
        self.assertEqual(req.checks, ())
        self.assertEqual(req.scope, ())
        self.assertFalse(req.snapshot)
        self.assertTrue(req.wait)

    def test_full_request_round_trips(self):
        obj = base(
            account="work-1_ab",
            base="main~1",
            grants=["edit", "bash"],
            mcp=["github", "fs-1"],
            timeout=600,
            checks=[["npm", "test"], ["python3", "-m", "pytest", "-q"]],
            scope=["src/**", "tests/a.py"],
            snapshot=True,
            wait=False,
        )
        req = self.parse(obj)
        self.assertEqual(req.account, "work-1_ab")
        self.assertEqual(req.base, "main~1")
        self.assertEqual(req.grants, ("edit", "bash"))
        self.assertEqual(req.mcp, ("github", "fs-1"))
        self.assertEqual(req.timeout, 600)
        self.assertEqual(req.checks,
                         (("npm", "test"), ("python3", "-m", "pytest", "-q")))
        self.assertEqual(req.scope, ("src/**", "tests/a.py"))
        self.assertTrue(req.snapshot)
        self.assertFalse(req.wait)

    def test_tuple_fields_are_tuples_not_lists(self):
        req = self.parse(base(grants=["edit"], mcp=["fs"],
                              checks=[["true"]], scope=["a/**"]))
        self.assertIsInstance(req.grants, tuple)
        self.assertIsInstance(req.mcp, tuple)
        self.assertIsInstance(req.checks, tuple)
        self.assertIsInstance(req.checks[0], tuple)
        self.assertIsInstance(req.scope, tuple)

    def test_empty_grants_mcp_checks_scope_are_allowed(self):
        req = self.parse(base(grants=[], mcp=[], checks=[], scope=[]))
        self.assertEqual(req.grants, ())
        self.assertEqual(req.mcp, ())
        self.assertEqual(req.checks, ())
        self.assertEqual(req.scope, ())

    def test_accepts_bytes_input(self):
        raw = json.dumps(base()).encode("utf-8")
        self.assertEqual(parse_request(raw).repo, "/repo")

    def test_accepts_non_ascii_in_task_and_repo(self):
        req = self.parse(base(task="revisa el archivo de configuración",
                              repo="/tmp/répo"))
        self.assertEqual(req.task, "revisa el archivo de configuración")
        self.assertEqual(req.repo, "/tmp/répo")

    def test_repo_is_not_normalized(self):
        req = self.parse(base(repo="/repo/sub/../sub/"))
        self.assertEqual(req.repo, "/repo/sub/../sub/")

    def test_request_error_is_a_value_error(self):
        self.assertTrue(issubclass(RequestError, ValueError))

    def test_provider_length_32_is_accepted(self):
        provider = "a" + "b" * 31
        self.assertEqual(self.parse(base(provider=provider)).provider, provider)

    def test_timeout_bounds_are_inclusive(self):
        self.assertEqual(self.parse(base(timeout=60)).timeout, 60)
        self.assertEqual(self.parse(base(timeout=14400)).timeout, 14400)

    def test_task_at_max_length_is_accepted(self):
        task = "t" * 100000
        self.assertEqual(self.parse(base(task=task)).task, task)

    def test_task_whitespace_only_is_accepted(self):
        # Only emptiness is refused; the runner decides what a blank task means.
        self.assertEqual(self.parse(base(task=" ")).task, " ")

    def test_checks_at_max_count_is_accepted(self):
        checks = [["true", str(i)] for i in range(20)]
        self.assertEqual(len(self.parse(base(checks=checks)).checks), 20)

    def test_scope_at_max_count_is_accepted(self):
        scope = ["dir%d/**" % i for i in range(50)]
        self.assertEqual(len(self.parse(base(scope=scope)).scope), 50)


class InputEncodingTests(RequestTestCase):
    def test_empty_bytes_is_rejected(self):
        self.rejects_raw(b"")

    def test_empty_string_is_rejected(self):
        self.rejects_raw("")

    def test_whitespace_only_is_rejected(self):
        self.rejects_raw("   \n\t ")

    def test_invalid_utf8_is_rejected(self):
        self.rejects_raw(b'{"task": "\xff\xfe"}')

    def test_invalid_json_is_rejected(self):
        self.rejects_raw("{not json}", "JSON")

    def test_truncated_json_is_rejected(self):
        self.rejects_raw('{"v": 1', "JSON")

    def test_non_text_raw_is_rejected(self):
        self.rejects_raw(123)
        self.rejects_raw(None)
        self.rejects_raw({"v": 1})

    def test_top_level_array_is_rejected(self):
        self.rejects_raw("[1, 2]", "objeto")

    def test_top_level_string_is_rejected(self):
        self.rejects_raw('"hola"', "objeto")

    def test_top_level_number_is_rejected(self):
        self.rejects_raw("7", "objeto")

    def test_top_level_null_is_rejected(self):
        self.rejects_raw("null", "objeto")

    def test_top_level_bool_is_rejected(self):
        self.rejects_raw("true", "objeto")


class VersionTests(RequestTestCase):
    def test_missing_version_is_rejected(self):
        obj = base()
        obj.pop("v", None)
        self.rejects(obj, "v")

    def test_version_one_is_the_only_valid_value(self):
        self.assertEqual(self.parse(base(v=1)).provider, "deepseek")

    def test_version_zero_is_rejected(self):
        self.rejects(base(v=0), "v")

    def test_version_two_is_rejected(self):
        self.rejects(base(v=2), "v")

    def test_version_string_is_rejected(self):
        self.rejects(base(v="1"), "v")

    def test_version_float_is_rejected(self):
        # 1.0 == 1 but is not an int.
        self.rejects(base(v=1.0), "v")

    def test_version_bool_is_rejected(self):
        # True == 1 in Python: bool must not pass as the schema version.
        self.rejects(base(v=True), "v")

    def test_version_null_is_rejected(self):
        self.rejects(base(v=None), "v")


class UnknownKeyTests(RequestTestCase):
    def test_single_unknown_key_is_named(self):
        self.rejects(base(wat=1), "wat")

    def test_every_unknown_key_is_named(self):
        self.rejects(base(zzz=1, aaa=2), "aaa", "zzz")

    def test_typo_of_a_known_key_is_named(self):
        obj = base()
        obj["providerr"] = "deepseek"
        self.rejects(obj, "providerr")

    def test_all_documented_keys_together_are_accepted(self):
        req = self.parse(base(account=None, base="HEAD", grants=["edit"],
                              mcp=[], timeout=60, checks=[["true"]],
                              scope=[], snapshot=False, wait=True))
        self.assertEqual(req.timeout, 60)


class ProviderTests(RequestTestCase):
    def test_valid_providers(self):
        for provider in ("a", "deepseek", "a1", "a-b-c", "z" * 32):
            with self.subTest(provider=provider):
                self.assertEqual(self.parse(base(provider=provider)).provider,
                                 provider)

    def test_missing_provider_is_rejected(self):
        obj = base()
        obj.pop("provider", None)
        self.rejects(obj, "provider")

    def test_uppercase_is_rejected(self):
        self.rejects(base(provider="DeepSeek"), "provider")

    def test_underscore_is_rejected(self):
        self.rejects(base(provider="deep_seek"), "provider")

    def test_leading_digit_is_rejected(self):
        self.rejects(base(provider="1deepseek"), "provider")

    def test_leading_dash_is_rejected(self):
        self.rejects(base(provider="-deepseek"), "provider")

    def test_trailing_whitespace_is_rejected(self):
        self.rejects(base(provider="deepseek "), "provider")

    def test_empty_provider_is_rejected(self):
        self.rejects(base(provider=""), "provider")

    def test_too_long_provider_is_rejected(self):
        self.rejects(base(provider="a" * 33), "provider")

    def test_provider_wrong_types_are_rejected(self):
        for value in (1, 1.5, True, None, ["deepseek"], {"p": 1}):
            with self.subTest(value=value):
                self.rejects(base(provider=value), "provider")


class AccountTests(RequestTestCase):
    def test_null_account_is_accepted(self):
        self.assertIsNone(self.parse(base(account=None)).account)

    def test_absent_account_defaults_to_none(self):
        obj = base()
        obj.pop("account", None)
        self.assertIsNone(self.parse(obj).account)

    def test_valid_accounts(self):
        for account in ("a", "work", "b-c_d", "0" * 32, "9x"):
            with self.subTest(account=account):
                self.assertEqual(self.parse(base(account=account)).account,
                                 account)

    def test_empty_account_is_rejected(self):
        self.rejects(base(account=""), "account")

    def test_too_long_account_is_rejected(self):
        self.rejects(base(account="a" * 33), "account")

    def test_account_outside_delegate_charset_is_rejected(self):
        # Same rule as delegate's _DG_ACCOUNT_NAME_RE.
        for account in ("Work", "a.b", "-a", "_a", ".._..", "a/b"):
            with self.subTest(account=account):
                self.rejects(base(account=account), "account")

    def test_account_with_space_is_rejected(self):
        self.rejects(base(account="wor k"), "account")

    def test_account_with_slash_is_rejected(self):
        self.rejects(base(account="a/b"), "account")

    def test_account_with_non_ascii_is_rejected(self):
        self.rejects(base(account="cuentañ"), "account")

    def test_account_wrong_types_are_rejected(self):
        for value in (1, True, ["a"], {"name": "a"}, 0.5):
            with self.subTest(value=value):
                self.rejects(base(account=value), "account")


class RepoTests(RequestTestCase):
    def test_absolute_repo_is_accepted(self):
        self.assertEqual(self.parse(base(repo="/srv/repo")).repo, "/srv/repo")

    def test_missing_repo_is_rejected(self):
        obj = base()
        obj.pop("repo", None)
        self.rejects(obj, "repo")

    def test_relative_repo_is_rejected(self):
        self.rejects(base(repo="repo"), "repo")

    def test_dot_relative_repo_is_rejected(self):
        self.rejects(base(repo="./repo"), "repo")

    def test_tilde_repo_is_rejected(self):
        self.rejects(base(repo="~/repo"), "repo")

    def test_empty_repo_is_rejected(self):
        self.rejects(base(repo=""), "repo")

    def test_repo_with_nul_is_rejected(self):
        self.rejects(base(repo="/repo\0evil"), "repo")

    def test_repo_wrong_types_are_rejected(self):
        for value in (1, True, None, ["/repo"], {"path": "/repo"}):
            with self.subTest(value=value):
                self.rejects(base(repo=value), "repo")


class BaseTests(RequestTestCase):
    def test_absent_base_defaults_to_head(self):
        obj = base()
        obj.pop("base", None)
        self.assertEqual(self.parse(obj).base, "HEAD")

    def test_valid_bases(self):
        for value in ("HEAD", "main", "main~1", "abc123^", "v1.2.3-rc1",
                      "a" * 200):
            with self.subTest(value=value):
                self.assertEqual(self.parse(base(base=value)).base, value)

    def test_empty_base_is_rejected(self):
        self.rejects(base(base=""), "base")

    def test_too_long_base_is_rejected(self):
        self.rejects(base(base="a" * 201), "base")

    def test_base_with_space_is_rejected(self):
        self.rejects(base(base="main branch"), "base")

    def test_base_with_tab_is_rejected(self):
        self.rejects(base(base="main\tbranch"), "base")

    def test_base_with_newline_is_rejected(self):
        self.rejects(base(base="main\nbranch"), "base")

    def test_base_starting_with_dash_is_rejected(self):
        self.rejects(base(base="--upload-pack=evil"), "base")

    def test_base_wrong_types_are_rejected(self):
        for value in (1, True, None, ["HEAD"], {"ref": "HEAD"}):
            with self.subTest(value=value):
                self.rejects(base(base=value), "base")


class BaseControlCharTests(RequestTestCase):
    def test_nul_in_base_is_rejected(self):
        self.rejects(base(base="main\u0000--output=x"), "base")

    def test_control_chars_in_base_are_rejected(self):
        for ch in ("\u0001", "\u001b", "\u007f"):
            with self.subTest(ch=repr(ch)):
                self.rejects(base(base=f"main{ch}"), "base")


class DuplicateKeyTests(RequestTestCase):
    def test_duplicate_key_is_rejected(self):
        raw = '{"v": 1, "provider": "deepseek", "repo": "/r", "task": "a", "repo": "/other"}'
        self.rejects_raw(raw, "repo")


class GrantsTests(RequestTestCase):
    def test_absent_grants_defaults_to_empty(self):
        obj = base()
        obj.pop("grants", None)
        self.assertEqual(self.parse(obj).grants, ())

    def test_valid_grants(self):
        self.assertEqual(self.parse(base(grants=list(GRANTS))).grants,
                         tuple(GRANTS))

    def test_single_grant(self):
        self.assertEqual(self.parse(base(grants=["edit"])).grants, ("edit",))

    def test_unknown_grant_is_rejected(self):
        self.rejects(base(grants=["read"]), "grants", "read")

    def test_duplicate_grant_is_rejected(self):
        self.rejects(base(grants=["edit", "edit"]), "grants", "edit")

    def test_uppercase_grant_is_rejected(self):
        self.rejects(base(grants=["EDIT"]), "grants")

    def test_grants_as_string_is_rejected(self):
        self.rejects(base(grants="edit"), "grants")

    def test_grants_as_object_is_rejected(self):
        self.rejects(base(grants={"edit": True}), "grants")

    def test_grants_element_wrong_types_are_rejected(self):
        for value in (1, True, None, ["edit"], {"g": "edit"}):
            with self.subTest(value=value):
                self.rejects(base(grants=[value]), "grants")


class McpTests(RequestTestCase):
    def test_absent_mcp_defaults_to_empty(self):
        obj = base()
        obj.pop("mcp", None)
        self.assertEqual(self.parse(obj).mcp, ())

    def test_valid_mcp_ids(self):
        self.assertEqual(self.parse(base(mcp=["fs", "github-1"])).mcp,
                         ("fs", "github-1"))

    def test_mcp_id_of_32_chars_is_accepted(self):
        mcp_id = "a" + "b" * 31
        self.assertEqual(self.parse(base(mcp=[mcp_id])).mcp, (mcp_id,))

    def test_duplicate_mcp_id_is_rejected(self):
        self.rejects(base(mcp=["fs", "fs"]), "mcp", "fs")

    def test_uppercase_mcp_id_is_rejected(self):
        self.rejects(base(mcp=["Fs"]), "mcp")

    def test_leading_digit_mcp_id_is_rejected(self):
        self.rejects(base(mcp=["1fs"]), "mcp")

    def test_empty_mcp_id_is_rejected(self):
        self.rejects(base(mcp=[""]), "mcp")

    def test_too_long_mcp_id_is_rejected(self):
        self.rejects(base(mcp=["a" * 33]), "mcp")

    def test_mcp_as_string_is_rejected(self):
        self.rejects(base(mcp="fs"), "mcp")

    def test_mcp_element_wrong_types_are_rejected(self):
        for value in (1, True, None, ["fs"], {"id": "fs"}):
            with self.subTest(value=value):
                self.rejects(base(mcp=[value]), "mcp")


class TimeoutTests(RequestTestCase):
    def test_absent_timeout_defaults_to_1800(self):
        obj = base()
        obj.pop("timeout", None)
        self.assertEqual(self.parse(obj).timeout, 1800)

    def test_below_minimum_is_rejected(self):
        self.rejects(base(timeout=59), "timeout")

    def test_above_maximum_is_rejected(self):
        self.rejects(base(timeout=14401), "timeout")

    def test_zero_is_rejected(self):
        self.rejects(base(timeout=0), "timeout")

    def test_negative_is_rejected(self):
        self.rejects(base(timeout=-60), "timeout")

    def test_bool_true_is_rejected(self):
        self.rejects(base(timeout=True), "timeout")

    def test_bool_false_is_rejected(self):
        self.rejects(base(timeout=False), "timeout")

    def test_float_is_rejected(self):
        self.rejects(base(timeout=60.0), "timeout")

    def test_string_is_rejected(self):
        self.rejects(base(timeout="600"), "timeout")

    def test_null_is_rejected(self):
        self.rejects(base(timeout=None), "timeout")

    def test_list_is_rejected(self):
        self.rejects(base(timeout=[600]), "timeout")


class TaskTests(RequestTestCase):
    def test_missing_task_is_rejected(self):
        obj = base()
        obj.pop("task", None)
        self.rejects(obj, "task")

    def test_empty_task_is_rejected(self):
        self.rejects(base(task=""), "task")

    def test_too_long_task_is_rejected(self):
        self.rejects(base(task="t" * 100001), "task")

    def test_task_with_nul_is_rejected(self):
        self.rejects(base(task="borra\0todo"), "task")

    def test_task_with_newlines_is_accepted(self):
        task = "linea 1\nlinea 2\n"
        self.assertEqual(self.parse(base(task=task)).task, task)

    def test_task_wrong_types_are_rejected(self):
        for value in (1, 1.5, True, None, ["task"], {"text": "task"}):
            with self.subTest(value=value):
                self.rejects(base(task=value), "task")


class ChecksTests(RequestTestCase):
    def test_absent_checks_defaults_to_empty(self):
        obj = base()
        obj.pop("checks", None)
        self.assertEqual(self.parse(obj).checks, ())

    def test_single_check(self):
        self.assertEqual(self.parse(base(checks=[["npm", "test"]])).checks,
                         (("npm", "test"),))

    def test_multiple_checks_keep_order(self):
        req = self.parse(base(checks=[["a"], ["b", "c"]]))
        self.assertEqual(req.checks, (("a",), ("b", "c")))

    def test_check_arguments_are_not_split(self):
        req = self.parse(base(checks=[["echo", "a b"]]))
        self.assertEqual(req.checks, (("echo", "a b"),))

    def test_checks_as_string_is_rejected(self):
        self.rejects(base(checks="npm test"), "checks")

    def test_checks_as_object_is_rejected(self):
        self.rejects(base(checks={"cmd": "npm test"}), "checks")

    def test_check_as_shell_string_is_rejected(self):
        self.rejects(base(checks=["npm test"]), "checks")

    def test_check_as_number_is_rejected(self):
        self.rejects(base(checks=[3]), "checks")

    def test_empty_check_is_rejected(self):
        self.rejects(base(checks=[[]]), "checks")

    def test_empty_check_argument_is_rejected(self):
        self.rejects(base(checks=[["npm", ""]]), "checks")

    def test_check_argument_with_nul_is_rejected(self):
        self.rejects(base(checks=[["echo", "a\0b"]]), "checks")

    def test_check_argument_wrong_types_are_rejected(self):
        for value in (1, True, None, ["x"], {"a": 1}):
            with self.subTest(value=value):
                self.rejects(base(checks=[["echo", value]]), "checks")

    def test_nested_check_list_is_rejected(self):
        self.rejects(base(checks=[[["npm", "test"]]]), "checks")

    def test_too_many_checks_is_rejected(self):
        self.rejects(base(checks=[["true"]] * 21), "checks")


class ScopeTests(RequestTestCase):
    def test_absent_scope_defaults_to_empty(self):
        obj = base()
        obj.pop("scope", None)
        self.assertEqual(self.parse(obj).scope, ())

    def test_valid_scope_globs(self):
        scope = ["src/**", "src/auth/*.ts", "a/b.txt", "*.md", "a..b/c", "..x"]
        self.assertEqual(self.parse(base(scope=scope)).scope, tuple(scope))

    def test_scope_as_string_is_rejected(self):
        self.rejects(base(scope="src/**"), "scope")

    def test_scope_as_object_is_rejected(self):
        self.rejects(base(scope={"path": "src/**"}), "scope")

    def test_empty_scope_entry_is_rejected(self):
        self.rejects(base(scope=[""]), "scope")

    def test_absolute_scope_entry_is_rejected(self):
        self.rejects(base(scope=["/etc/passwd"]), "scope")

    def test_leading_slash_scope_entry_is_rejected(self):
        self.rejects(base(scope=["/src/**"]), "scope")

    def test_dotdot_entry_is_rejected(self):
        self.rejects(base(scope=[".."]), "scope")

    def test_leading_dotdot_entry_is_rejected(self):
        self.rejects(base(scope=["../secrets"]), "scope")

    def test_inner_dotdot_entry_is_rejected(self):
        self.rejects(base(scope=["src/../../etc"]), "scope")

    def test_trailing_dotdot_entry_is_rejected(self):
        self.rejects(base(scope=["src/.."]), "scope")

    def test_scope_entry_with_nul_is_rejected(self):
        self.rejects(base(scope=["src\0/**"]), "scope")

    def test_scope_entry_wrong_types_are_rejected(self):
        for value in (1, True, None, ["src/**"], {"p": "src/**"}):
            with self.subTest(value=value):
                self.rejects(base(scope=[value]), "scope")

    def test_too_many_scope_entries_is_rejected(self):
        self.rejects(base(scope=["d%d/**" % i for i in range(51)]), "scope")


class SnapshotWaitTests(RequestTestCase):
    def test_defaults(self):
        req = self.parse(base())
        self.assertFalse(req.snapshot)
        self.assertTrue(req.wait)

    def test_explicit_values(self):
        req = self.parse(base(snapshot=True, wait=False))
        self.assertTrue(req.snapshot)
        self.assertFalse(req.wait)

    def test_snapshot_wrong_types_are_rejected(self):
        for value in (0, 1, "true", None, [], {}):
            with self.subTest(value=value):
                self.rejects(base(snapshot=value), "snapshot")

    def test_wait_wrong_types_are_rejected(self):
        for value in (0, 1, "false", None, [], {}):
            with self.subTest(value=value):
                self.rejects(base(wait=value), "wait")


class MissingRequiredKeyTests(RequestTestCase):
    def test_empty_object_names_every_missing_key(self):
        self.rejects({}, "v", "provider", "repo", "task")

    def test_only_version_names_the_rest(self):
        self.rejects({"v": 1}, "provider", "repo", "task")

    def test_missing_keys_are_named_together(self):
        self.rejects({"v": 1, "provider": "deepseek"}, "repo", "task")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
