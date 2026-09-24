#!/usr/bin/env bash
# Runs every test/*.test.sh and reports per-file results.
#   test/run.sh              all suites
#   test/run.sh config       only test/config.test.sh
#   VERBOSE=1 test/run.sh    print each passing assertion too
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=''; G=''; R=''; D=''; N=''
fi

suites=()
if [[ $# -gt 0 ]]; then
  for arg in "$@"; do
    f="test/${arg%.test.sh}.test.sh"
    [[ -f "$f" ]] || { printf '%sno existe%s %s\n' "$R" "$N" "$f" >&2; exit 2; }
    suites+=("$f")
  done
else
  while IFS= read -r f; do suites+=("$f"); done < <(find test -maxdepth 1 -name '*.test.sh' | sort)
fi

(( ${#suites[@]} )) || { printf '%sno hay suites en test/%s\n' "$R" "$N" >&2; exit 2; }

failed_suites=()
for suite in "${suites[@]}"; do
  printf '\n%s%s%s\n' "$B" "$suite" "$N"
  if bash "$suite"; then :; else failed_suites+=("$suite"); fi
done

printf '\n%s──────────────────────────────%s\n' "$D" "$N"
if (( ${#failed_suites[@]} )); then
  printf '%s✗ fallaron:%s %s\n' "$R" "$N" "${failed_suites[*]}"
  exit 1
fi
printf '%s✓ todas las suites en verde%s (%d)\n' "$G" "$N" "${#suites[@]}"
