#!/usr/bin/env bash
# test/runner.test.sh — the harness in test/lib/assert.sh, tested against itself.
#
# A suite is only as good as its ability to go red. These cases write a tiny
# suite into $TMP, run it in a fresh bash, and check that the harness reports
# what actually happened.
source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"

ASSERT_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

mini_suite() {
  # <body>: a suite file that sources the harness, defines <body> and runs it.
  {
    printf 'source %q\n' "$ASSERT_LIB"
    printf '%s\n' "$1"
    printf 'run_tests\n'
  } >"$TMP/mini.test.sh"
}

test_a_test_that_returns_nonzero_without_calling_fail_is_a_failure() {
  # The harness used to count a test as passing solely from `_current_failed`,
  # so a test that died half way — a `return 1`, a helper that bailed out —
  # without calling `fail` was reported green. A security test that cannot go
  # red is decoration.
  mini_suite 'test_bails_out() { return 1; }'
  capture bash "$TMP/mini.test.sh"
  assert_ne 0 "$RC" "the suite exits non-zero"
  assert_contains "$ERR" "bails_out" "and names the test that failed"
  assert_contains "$OUT$ERR" "1 failed" "the summary counts it"
}

test_a_test_that_returns_zero_and_never_fails_still_passes() {
  # Control: the change must not turn every test red.
  mini_suite 'test_fine() { assert_eq 1 1 "one is one"; }'
  capture bash "$TMP/mini.test.sh"
  assert_eq 0 "$RC" "the suite exits 0"
  assert_contains "$OUT" "1 passed" "and counts the pass"
}

test_a_failed_assertion_is_still_a_failure() {
  mini_suite 'test_wrong() { assert_eq 1 2 "one is two"; }'
  capture bash "$TMP/mini.test.sh"
  assert_ne 0 "$RC" "the suite exits non-zero"
  assert_contains "$OUT$ERR" "1 failed" "the summary counts it"
}

run_tests
