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
        for s in ["ghp_" + "a" * 36, "sk-ant-" + "b" * 40, "AKIA" + "C" * 16,
                  "-----BEGIN OPENSSH PRIVATE KEY-----",
                  "DATABASE_URL=postgres://u:p@h/db",
                  "DATABASE_PASSWORD=hunter2", "API_TOKEN: abcd1234efgh",
                  "ver https://u:p@h.com/x"]:
            with self.assertRaises(SecretInBrief) as cm:
                check_secrets("contexto\n" + s)
            self.assertNotIn(s, str(cm.exception))
            with self.assertRaises(SecretInBrief):
                build_brief("x", "Innovares/datago", "main", [], [s], "C")

    def test_mentions_without_values_pass(self):
        check_secrets("la variable DATABASE_PASSWORD no se toca; el token va en el vault")

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
