#!/usr/bin/env bash
# Minimal assertion harness. No runtime dependencies, on purpose: the tool it
# tests has none either, and a test suite that needs installing is a test suite
# that stops being run.
#
# A test file sources this, defines functions named `test_*`, and ends with
# `run_tests`. Each test runs in the same shell but gets a fresh scratch dir in
# $TMP, exported before it runs and removed after.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/../lib/assert.sh"
#   test_two_plus_two() { assert_eq 4 "$((2 + 2))" "arithmetic still works"; }
#   run_tests

set -uo pipefail   # NOT -e: a failing assertion must let the test keep going

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  _G=$'\033[32m'; _R=$'\033[31m'; _D=$'\033[2m'; _B=$'\033[1m'; _N=$'\033[0m'
else
  _G=''; _R=''; _D=''; _B=''; _N=''
fi

_passed=0
_failed=0
_current=''
_current_failed=0

fail() {
  _current_failed=1
  printf '  %s✗%s %s\n' "$_R" "$_N" "$*" >&2
}

pass_detail() {
  [[ -n "${VERBOSE:-}" ]] || return 0
  printf '  %s·%s %s\n' "$_D" "$_N" "$*"
}

assert_eq() {
  local expected="$1" actual="$2" msg="${3:-}"
  if [[ "$expected" == "$actual" ]]; then
    pass_detail "${msg:-equal}"
  else
    fail "${msg:-values differ}"
    printf '      expected: %q\n      actual:   %q\n' "$expected" "$actual" >&2
  fi
}

assert_ne() {
  local a="$1" b="$2" msg="${3:-}"
  if [[ "$a" != "$b" ]]; then pass_detail "${msg:-differ}"
  else fail "${msg:-values are equal but should not be}: $(printf '%q' "$a")"; fi
}

assert_contains() {
  local haystack="$1" needle="$2" msg="${3:-}"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass_detail "${msg:-contains}"
  else
    fail "${msg:-substring missing}"
    printf '      looking for: %q\n      in:          %q\n' "$needle" "$haystack" >&2
  fi
}

assert_not_contains() {
  local haystack="$1" needle="$2" msg="${3:-}"
  if [[ "$haystack" != *"$needle"* ]]; then
    pass_detail "${msg:-does not contain}"
  else
    fail "${msg:-substring present but should not be}: $(printf '%q' "$needle")"
  fi
}

assert_ok() {
  # assert_ok <msg> <command...>
  local msg="$1"; shift
  if "$@" >/dev/null 2>&1; then pass_detail "$msg"
  else fail "$msg (expected success, got exit $?)"; fi
}

assert_fails() {
  # assert_fails <msg> <command...> — any non-zero exit
  local msg="$1"; shift
  if "$@" >/dev/null 2>&1; then fail "$msg (expected failure, got success)"
  else pass_detail "$msg"; fi
}

assert_exit() {
  # assert_exit <expected_code> <msg> <command...>
  local want="$1" msg="$2"; shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  assert_eq "$want" "$got" "$msg (exit code)"
}

assert_file_mode() {
  local path="$1" want="$2" msg="${3:-}"
  local got
  got="$(stat -c %a "$path" 2>/dev/null)" || { fail "${msg:-mode}: $path does not exist"; return; }
  assert_eq "$want" "$got" "${msg:-mode of $path}"
}

# Runs a command and captures stdout, stderr and exit code into the globals
# OUT, ERR and RC. Keeps assertions readable when all three matter.
capture() {
  local _o _e
  _o="$(mktemp)"; _e="$(mktemp)"
  RC=0
  "$@" >"$_o" 2>"$_e" || RC=$?
  OUT="$(cat "$_o")"; ERR="$(cat "$_e")"
  rm -f "$_o" "$_e"
}

run_tests() {
  local name
  # `declare -F` lists them in definition order, which keeps output stable.
  for name in $(declare -F | awk '{print $3}' | grep '^test_'); do
    _current="$name"
    _current_failed=0
    TMP="$(mktemp -d)"
    export TMP
    # The exit status counts too. A test that bailed out half way — a
    # `return 1`, a helper that gave up — without calling `fail` used to be
    # reported as passing, so a security test could not go red by dying.
    local rc=0
    "$name" || rc=$?
    if (( rc != 0 )); then
      fail "the test function itself returned $rc"
    fi
    rm -rf "$TMP"
    if (( _current_failed )); then
      (( _failed++ ))
      printf '%s✗%s %s\n' "$_R" "$_N" "${name#test_}" >&2
    else
      (( _passed++ ))
      printf '%s✓%s %s\n' "$_G" "$_N" "${name#test_}"
    fi
  done

  printf '\n%s%d passed%s' "$_G" "$_passed" "$_N"
  (( _failed )) && printf ', %s%s%d failed%s' "$_B" "$_R" "$_failed" "$_N"
  printf '\n'
  (( _failed == 0 ))
}
