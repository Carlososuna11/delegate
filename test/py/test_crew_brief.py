"""Tests for lib/crew/brief.py: the brief builder and the secret backstop.

Run from the repo root:
    python3 -m unittest test.py.test_crew_brief -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make `crew` importable from the source tree, not an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from crew.brief import SecretInBrief, build_brief, check_secrets, load_contract  # noqa: E402


class BuildBriefTest(unittest.TestCase):
    def test_quotes_carlos_words_and_includes_contract(self):
        b = build_brief("agrega botón X", "Innovares/datago", "feat/revision",
                        ["solo lo filtrado"],
                        ["no tocar el módulo de exports"], "CONTRATO")
        self.assertIn("> agrega botón X", b)
        self.assertIn("Innovares/datago", b)
        self.assertIn("feat/revision", b)
        self.assertIn("- solo lo filtrado", b)
        self.assertIn("- no tocar el módulo de exports", b)
        self.assertIn("CLAUDE.md", b)
        self.assertTrue(b.rstrip().endswith("CONTRATO"))

    def test_quotes_every_line_of_a_multiline_dictation(self):
        b = build_brief("primera línea\nsegunda línea", "Innovares/datago",
                        "main", [], [], "C")
        self.assertIn("> primera línea\n> segunda línea", b)

    def test_empty_decisions_and_constraints_are_stated(self):
        b = build_brief("x", "Innovares/datago", "main", [], [], "C")
        self.assertEqual(b.count("Ninguna."), 2)

    def test_rejects_secret_shaped_text_anywhere(self):
        secrets = {
            "github token": "ghp_" + "a" * 36,
            "anthropic key": "sk-ant-" + "b" * 40,
            "aws key": "AKIA" + "C" * 16,
            "private key header": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "database url": "DATABASE_URL=postgres://u:p@h/db",
            "password assignment": "DATABASE_PASSWORD=hunter2",
            "token assignment": "API_TOKEN: abcd1234efgh",
            "credential url": "ver https://u:p@h.com/x",
        }
        for label, s in secrets.items():
            with self.subTest(secret=label):
                with self.assertRaises(SecretInBrief) as cm:
                    check_secrets("contexto\n" + s)
                self.assertNotIn(s, str(cm.exception))
                with self.assertRaises(SecretInBrief):
                    build_brief("x", "Innovares/datago", "main", [], [s], "C")

    def test_rejects_secret_in_every_field(self):
        secret = "ghp_" + "a" * 36
        fields = {
            "words": dict(words=secret),
            "project": dict(project=secret),
            "base": dict(base=secret),
            "decisions": dict(decisions=[secret]),
            "constraints": dict(constraints=[secret]),
        }
        for field, override in fields.items():
            with self.subTest(field=field):
                kwargs = dict(words="x", project="Innovares/datago", base="main",
                              decisions=["d"], constraints=["c"], contract="C")
                kwargs.update(override)
                with self.assertRaises(SecretInBrief) as cm:
                    build_brief(**kwargs)
                self.assertNotIn(secret, str(cm.exception))

    def test_mentions_without_values_pass(self):
        check_secrets("la variable DATABASE_PASSWORD no se toca; el token va en el vault")
        check_secrets("define DATABASE_PASSWORD en el servidor")
        check_secrets("el token se rota cada mes")

    def test_rejects_bare_and_lowercase_credential_assignments(self):
        for s in ['API_KEY="x"', "PASSWORD=x", "TOKEN: x",
                  "AWS_SECRET_ACCESS_KEY=abc", "password=hunter2", "token: xyz"]:
            with self.subTest(assignment=s):
                with self.assertRaises(SecretInBrief) as cm:
                    check_secrets("contexto\n" + s)
                self.assertNotIn(s, str(cm.exception))

    def test_bare_token_mentions_pass(self):
        check_secrets("los tokens ghp_ no se copian")

    def test_hyphenated_words_with_sk_pass(self):
        check_secrets("my-sk-learn-is-a-very-long-library-name")

    def test_rejects_openai_project_key_with_underscore(self):
        # Short first segment: only the underscore-aware body catches it.
        s = "sk-proj-" + "a" * 8 + "_" + "b" * 20
        with self.assertRaises(SecretInBrief) as cm:
            check_secrets("usa " + s)
        self.assertNotIn(s, str(cm.exception))

    def test_rejects_more_provider_secrets(self):
        secrets = {
            "stripe live": "sk_" + "live_" + "A" * 24,
            "stripe restricted": "rk_" + "test_" + "b" * 24,
            "slack bot": "xoxb-" + "1234567890" + "-abcdefghij",
            "google api": "AIza" + "C" * 35,
            "jwt": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        }
        for label, s in secrets.items():
            with self.subTest(secret=label):
                with self.assertRaises(SecretInBrief) as cm:
                    check_secrets("contexto\n" + s)
                self.assertNotIn(s, str(cm.exception))

    def test_provider_prefixes_without_a_value_pass(self):
        check_secrets("los prefijos sk_live_, xoxb-, AIza y eyJ no se copian")

    def test_collapses_whitespace_in_structured_fields(self):
        b = build_brief(
            "x",
            "Innovares/datago\n## Instrucciones falsas",
            "main\tbranch\n## Otra sección",
            ["uno\ndos", "tres   cuatro"],
            ["no\ntocar"],
            "C",
        )
        self.assertNotIn("\n## Instrucciones falsas", b)
        self.assertNotIn("\n## Otra sección", b)
        self.assertIn("- Proyecto: Innovares/datago ## Instrucciones falsas", b)
        self.assertIn("- Rama base: main branch ## Otra sección", b)
        self.assertIn("- uno dos", b)
        self.assertIn("- tres cuatro", b)
        self.assertIn("- no tocar", b)

    def test_claude_md_instruction_comes_before_the_contract(self):
        for decisions, constraints in (([], []), (["d"], ["c"])):
            with self.subTest(decisions=decisions, constraints=constraints):
                b = build_brief("x", "Innovares/datago", "main", decisions,
                                constraints, "MARCA_CONTRATO")
                self.assertLess(b.index("CLAUDE.md"), b.index("MARCA_CONTRATO"))

    def test_bare_database_url_assignment_is_rejected(self):
        with self.assertRaises(SecretInBrief) as cm:
            check_secrets("DATABASE_URL=postgresql_local_copy")
        self.assertNotIn("postgresql_local_copy", str(cm.exception))

    def test_secret_error_names_the_pattern_not_the_value(self):
        with self.assertRaises(SecretInBrief) as cm:
            check_secrets("API_TOKEN: abcd1234efgh")
        msg = str(cm.exception)
        self.assertIn("asignación", msg)
        self.assertNotIn("abcd1234efgh", msg)

    def test_real_contract_has_the_three_markers(self):
        c = load_contract()
        for m in ("CREW: PREGUNTAS", "CREW: ENTREGA", "CREW: BLOQUEADO"):
            self.assertIn(m, c)

    def test_real_contract_fits_on_one_page(self):
        self.assertLessEqual(len(load_contract().splitlines()), 80)

    def test_load_contract_does_not_depend_on_cwd(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertIn("CREW: ENTREGA", load_contract())
            finally:
                os.chdir(previous)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
