#!/usr/bin/env bash
# Tests for lib/config.sh: the provider file grammar, the key rules and the
# provider listing. Every rejection rule in the header contract has a case here
# that checks the exit code, not only the message: a rule that starts accepting
# bad input while still writing a warning is exactly the bug worth catching.
#
# Everything is written inside $TMP. Nothing here reads or writes ~/.config.

source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/config.sh"

# ── helpers ─────────────────────────────────────────────────────────────────

# Writes alternating key/value arguments, one <TAB> between them.
write_kv() {
  local path="$1"; shift
  : > "$path"
  while (( $# )); do
    printf '%s\t%s\n' "$1" "$2" >> "$path"
    shift 2
  done
}

# The four required keys, one `key<TAB>value` line each.
required_lines() {
  printf '%s\n' \
    $'endpoint\thttps://api.deepseek.com/anthropic' \
    $'auth_env\tANTHROPIC_API_KEY' \
    $'key_dir\t~/.config/deepseek' \
    $'model\tdeepseek-flash[1m]'
}

# The required keys plus the pair, which replaces the key it names and is
# appended when it names an optional one.
write_config_with() {
  local path="$1" key="$2" value="$3" line='' k=''
  : > "$path"
  while IFS= read -r line; do
    k="${line%%$'\t'*}"
    [[ "$k" == "$key" ]] && continue
    printf '%s\n' "$line" >> "$path"
  done < <(required_lines)
  printf '%s\t%s\n' "$key" "$value" >> "$path"
}

# The required keys minus one.
write_config_without() {
  local path="$1" key="$2" line='' k=''
  : > "$path"
  while IFS= read -r line; do
    k="${line%%$'\t'*}"
    [[ "$k" == "$key" ]] && continue
    printf '%s\n' "$line" >> "$path"
  done < <(required_lines)
}

# The required keys plus verbatim lines, for grammar cases.
write_config_plus_raw() {
  local path="$1"; shift
  { required_lines; printf '%s\n' "$@"; } > "$path"
}

# Every recognised key, each one valid, in scrambled order.
write_full_config() {
  local path="$1"
  write_kv "$path" \
    model 'deepseek-flash[1m]' \
    timeout 1800 \
    endpoint 'https://api.deepseek.com/anthropic' \
    caps 'read,edit,bash,mcp,mcp-write' \
    auth_env ANTHROPIC_API_KEY \
    residency 'US East (Virginia), tier 1' \
    key_dir '~/.config/deepseek' \
    default_account kontigo \
    small_model deepseek-flash \
    key_pattern 'api_key.%s'
}

# A rejection: exit 2, nothing on stdout, the documented prefix on stderr.
assert_reject() {
  # `-`, not `:-`: the empty provider id is one of the rejected cases.
  local path="$1" msg="$2" provider="${3-deepseek}"
  capture dg_config_parse "$path" "$provider"
  assert_eq 2 "$RC" "$msg: exit code"
  assert_eq '' "$OUT" "$msg: stdout stays empty"
  assert_contains "$ERR" "delegate: $provider: " "$msg: stderr carries the provider id"
}

# A rejection coming from one value rule, with the rest of the config valid.
assert_reject_value() {
  local key="$1" value="$2" msg="$3" path="$TMP/value"
  write_config_with "$path" "$key" "$value"
  assert_reject "$path" "$msg"
}

# A rejection coming from one raw line added to an otherwise valid config.
assert_reject_raw() {
  local line="$1" msg="$2" path="$TMP/raw"
  write_config_plus_raw "$path" "$line"
  assert_reject "$path" "$msg"
}

# ── happy path ──────────────────────────────────────────────────────────────

test_parses_a_complete_config_in_sorted_order() {
  local path="$TMP/full" expected
  write_full_config "$path"
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "a complete config parses"
  assert_eq '' "$ERR" "no stderr on success"
  expected="$(printf '%s\n' \
    $'auth_env\tANTHROPIC_API_KEY' \
    $'caps\tread,edit,bash,mcp,mcp-write' \
    $'default_account\tkontigo' \
    $'endpoint\thttps://api.deepseek.com/anthropic' \
    $'key_dir\t~/.config/deepseek' \
    $'key_pattern\tapi_key.%s' \
    $'model\tdeepseek-flash[1m]' \
    $'residency\tUS East (Virginia), tier 1' \
    $'small_model\tdeepseek-flash' \
    $'timeout\t1800')"
  assert_eq "$expected" "$OUT" "one key<TAB>value line per key, sorted by key"
}

test_parses_a_minimal_config_and_adds_no_defaults() {
  local path="$TMP/minimal" expected
  write_kv "$path" \
    model 'deepseek-flash[1m]' \
    auth_env ANTHROPIC_API_KEY \
    key_dir '~/.config/deepseek' \
    endpoint 'https://api.deepseek.com/anthropic'
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "the four required keys are enough"
  expected="$(printf '%s\n' \
    $'auth_env\tANTHROPIC_API_KEY' \
    $'endpoint\thttps://api.deepseek.com/anthropic' \
    $'key_dir\t~/.config/deepseek' \
    $'model\tdeepseek-flash[1m]')"
  assert_eq "$expected" "$OUT" "keys the file does not set are omitted"
}

test_ignores_comments_and_blank_lines() {
  local path="$TMP/comments" expected
  {
    printf '\n'
    printf '# a whole-line comment\n'
    printf '   # an indented comment with\ta tab in it\n'
    required_lines
    printf '\n'
    printf '   \t  \n'
    printf '# trailing comment\n'
  } > "$path"
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "comments and blank lines are ignored"
  expected="$(printf '%s\n' \
    $'auth_env\tANTHROPIC_API_KEY' \
    $'endpoint\thttps://api.deepseek.com/anthropic' \
    $'key_dir\t~/.config/deepseek' \
    $'model\tdeepseek-flash[1m]')"
  assert_eq "$expected" "$OUT" "only the configured keys are printed"
}

test_trims_blanks_around_lines_and_values() {
  local path="$TMP/trim" expected
  {
    printf '  endpoint\thttps://api.deepseek.com/anthropic\n'
    printf '\tauth_env\tANTHROPIC_API_KEY   \n'
    printf '  key_dir\t~/.config/deepseek  \n'
    printf 'model\tdeepseek-flash[1m]  \n'
  } > "$path"
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "padded lines parse"
  expected="$(printf '%s\n' \
    $'auth_env\tANTHROPIC_API_KEY' \
    $'endpoint\thttps://api.deepseek.com/anthropic' \
    $'key_dir\t~/.config/deepseek' \
    $'model\tdeepseek-flash[1m]')"
  assert_eq "$expected" "$OUT" "leading blanks and trailing blanks are trimmed"
}

test_accepts_boundary_values() {
  local path="$TMP/boundary" k32 r120
  printf -v k32 'k%031d' 0    # 32 characters: the longest account name
  printf -v r120 '%0120d' 0   # 120 characters: the longest residency note
  write_kv "$path" \
    endpoint 'https://host:8080/v1' \
    auth_env ANTHROPIC_API_KEY \
    key_dir '/' \
    model 'a' \
    small_model 'x:y@z' \
    key_pattern 'k.%s' \
    default_account "$k32" \
    timeout 86400 \
    caps 'mcp-write' \
    residency "$r120"
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "values at the top of their range are accepted"
  assert_contains "$OUT" $'timeout\t86400' "the boundary timeout is kept as written"
  assert_contains "$OUT" $'key_dir\t/' "a root key_dir is accepted"

  write_config_with "$path" timeout 1
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "the smallest timeout is accepted"

  write_config_with "$path" caps 'read,mcp-write'
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "a two-word caps value is accepted"
}

test_parses_a_file_without_a_trailing_newline() {
  local path="$TMP/nonewline"
  required_lines > "$path"
  printf '%s' "$(< "$path")" > "$path.2"   # same content, no final newline
  mv "$path.2" "$path"
  capture dg_config_parse "$path" deepseek
  assert_eq 0 "$RC" "the last line is read even without a trailing newline"
  assert_contains "$OUT" $'model\tdeepseek-flash[1m]' "the last key is parsed"
}

test_survives_set_euo_pipefail() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  write_full_config "$TMP/full"
  # bin/delegate runs with `set -euo pipefail`: a bare arithmetic command that
  # returns 1, or an unbound name, would abort it. Reaching the last line is the
  # assertion.
  capture bash -euo pipefail -c "
    source '$root/lib/config.sh'
    dg_config_parse '$TMP/full' deepseek > /dev/null
    dg_config_parse '$TMP/full' qwen > /dev/null
    dg_config_providers '$root/providers.d' > /dev/null
    printf 'reached'"
  assert_eq 0 "$RC" "no abort under set -euo pipefail"
  assert_eq 'reached' "$OUT" "every function runs under set -euo pipefail"
}

# ── grammar rejections ──────────────────────────────────────────────────────

test_rejects_an_unknown_key() {
  local k
  for k in models Endpoint auth-env capss residencia 'model '; do
    assert_reject_value "$k" 'deepseek-flash' "unknown key '$k' is rejected"
  done
}

test_rejects_a_duplicate_key() {
  local path="$TMP/duplicate"
  write_kv "$path" \
    endpoint 'https://api.deepseek.com/anthropic' \
    auth_env ANTHROPIC_API_KEY \
    key_dir '~/.config/deepseek' \
    model 'deepseek-flash[1m]' \
    timeout 1800 \
    timeout 900
  assert_reject "$path" "a key set twice is rejected"

  write_kv "$path" \
    endpoint 'https://api.deepseek.com/anthropic' \
    auth_env ANTHROPIC_API_KEY \
    key_dir '~/.config/deepseek' \
    model 'deepseek-flash[1m]' \
    timeout 1800 \
    timeout 1800
  assert_reject "$path" "a key repeated with the same value is rejected too"
}

test_rejects_a_line_without_a_tab() {
  assert_reject_raw 'auth_env ANTHROPIC_API_KEY' 'a space instead of a tab'
  assert_reject_raw 'deepseek-flash[1m]' 'a bare word'
  assert_reject_raw 'model=deepseek-flash' 'a shell assignment'
  assert_reject_raw 'auth_env:ANTHROPIC_API_KEY' 'a colon instead of a tab'
}

test_rejects_a_line_with_more_than_one_tab() {
  assert_reject_raw $'small_model\ta\tb' 'two tabs in the middle'
  assert_reject_raw $'residency\tUS\tEast' 'a second tab inside the value'
  assert_reject_raw $'small_model\tdeepseek-flash\t' 'a tab dangling at the end'
  assert_reject_raw $'small_model\t\t' 'two tabs and nothing else'
}

test_rejects_an_empty_value() {
  local path="$TMP/empty" line
  for line in $'small_model\t' $'small_model\t   ' $'small_model\t '; do
    write_config_plus_raw "$path" "$line"
    assert_reject "$path" "an empty value in $(printf '%q' "$line")"
  done

  write_config_plus_raw "$path" $'small_model\t'
  capture dg_config_parse "$path" deepseek
  assert_contains "$ERR" 'empty value' "the reason names the empty value"
  assert_contains "$ERR" 'line 5' "the reason names the line"
}

test_rejects_a_missing_required_key() {
  local k path="$TMP/required"
  for k in endpoint auth_env key_dir model; do
    write_config_without "$path" "$k"
    assert_reject "$path" "a config without $k"
  done

  : > "$path"
  assert_reject "$path" "an empty file"
}

test_rejects_a_missing_or_unreadable_file() {
  assert_reject "$TMP/nope" "a provider file that is not there"
  mkdir -p "$TMP/adir"
  assert_reject "$TMP/adir" "a directory instead of a file"
}

test_rejects_an_invalid_provider_id() {
  local path="$TMP/pid" p p32 p33
  printf -v p32 'a%031d' 0   # 32 characters: the longest valid id
  printf -v p33 'a%032d' 0   # 33 characters: one past the limit
  write_config_with "$path" timeout 1800
  for p in '' 'Deepseek' '9deepseek' 'deep.seek' 'deep_seek' '-deepseek' 'deep seek' "$p33"; do
    assert_reject "$path" "provider id '$p' is rejected" "$p"
  done
  capture dg_config_parse "$path" "$p32"
  assert_eq 0 "$RC" "a 32-character provider id is accepted"
}

test_rejects_wrong_argument_count() {
  capture dg_config_parse
  assert_eq 2 "$RC" "dg_config_parse with no arguments"
  assert_eq '' "$OUT" "no output without arguments"
  capture dg_config_parse "$TMP/x"
  assert_eq 2 "$RC" "dg_config_parse with one argument"
  capture dg_config_providers
  assert_eq 2 "$RC" "dg_config_providers with no arguments"
  capture dg_config_providers a b
  assert_eq 2 "$RC" "dg_config_providers with two arguments"
}

# ── value rejections ────────────────────────────────────────────────────────

test_rejects_a_non_https_endpoint() {
  local v
  for v in 'http://api.deepseek.com/anthropic' 'ftp://api.deepseek.com' \
           'api.deepseek.com' 'https://' 'HTTPS://api.deepseek.com' \
           'https://api.deepseek.com/anthropic?x=1' 'https://api deepseek.com'; do
    assert_reject_value endpoint "$v" "endpoint '$v'"
  done
  write_config_with "$TMP/value" endpoint 'https://api.deepseek.com/anthropic'
  capture dg_config_parse "$TMP/value" deepseek
  assert_eq 0 "$RC" "the https endpoint the providers.d files use is accepted"
}

test_rejects_an_auth_env_other_than_anthropic_api_key() {
  local v
  for v in ANTHROPIC_AUTH_TOKEN OPENAI_API_KEY anthropic_api_key \
           ANTHROPIC_API_KEY_2 ' ANTHROPIC_API_KEY' '$ANTHROPIC_API_KEY' '*'; do
    assert_reject_value auth_env "$v" "auth_env '$v'"
  done
}

test_rejects_a_relative_or_dotted_key_dir() {
  local v
  for v in 'config/keys' 'deepseek' '~' '~keys' '.config/deepseek' '${HOME}/.config'; do
    assert_reject_value key_dir "$v" "relative key_dir '$v'"
  done
  for v in '~/.config/../deepseek' '/tmp/../etc' '/home/u/.config/x/..' '..'; do
    assert_reject_value key_dir "$v" "key_dir with '..' '$v'"
  done
  for v in '~/.config/deepseek' '/home/u/.config/deepseek'; do
    write_config_with "$TMP/value" key_dir "$v"
    capture dg_config_parse "$TMP/value" deepseek
    assert_eq 0 "$RC" "key_dir '$v' is accepted"
  done
}

test_rejects_a_timeout_that_is_not_a_number() {
  local v
  for v in '1800s' '1800.5' '-60' '1e3' '18 00' 'un rato' '1,800' '0x10'; do
    assert_reject_value timeout "$v" "timeout '$v'"
  done
}

test_rejects_a_timeout_out_of_range() {
  local v
  for v in '0' '0000' '86401' '999999' '99999999'; do
    assert_reject_value timeout "$v" "timeout '$v'"
  done
}

test_rejects_an_invalid_caps_element() {
  local v
  for v in 'nope' 'Read' 'read,nope' 'read, bash' 'edit,bash,all' 'read,edit,write' 'rwx'; do
    assert_reject_value caps "$v" "caps '$v'"
  done
}

test_rejects_a_duplicate_caps_element() {
  local v
  for v in 'read,read' 'bash,bash' 'mcp-write,mcp-write' 'read,bash,read'; do
    assert_reject_value caps "$v" "caps '$v'"
  done
}

test_rejects_an_empty_caps_element() {
  local v
  for v in 'read,,bash' 'read,' ',read' ',' 'read,bash,'; do
    assert_reject_value caps "$v" "caps '$v'"
  done
}

test_rejects_an_invalid_key_pattern() {
  local v
  for v in 'api_key' 'api_key.%' 'api_key.%s.%s' '%s%s' 'api_%sx%s' \
           'api_%s/key' 'api_%1$s' 'api key.%s'; do
    assert_reject_value key_pattern "$v" "key_pattern '$v'"
  done
  write_config_with "$TMP/value" key_pattern 'api_key.%s'
  capture dg_config_parse "$TMP/value" deepseek
  assert_eq 0 "$RC" "key_pattern 'api_key.%s' is accepted"
}

test_rejects_an_invalid_default_account() {
  local v k33
  printf -v k33 'k%032d' 0   # 33 characters: one past the limit
  for v in 'Kontigo' '_kontigo' 'kontigo.' '.kontigo' 'kontigo!' 'kon tigo' 'kon.tigo' \
           'kon:tigo' 'kon/tigo' "$k33"; do
    assert_reject_value default_account "$v" "default_account '$v'"
  done
  write_config_with "$TMP/value" default_account 'kontigo-2'
  capture dg_config_parse "$TMP/value" deepseek
  assert_eq 0 "$RC" "default_account 'kontigo-2' is accepted"
}

test_rejects_a_model_outside_the_character_class() {
  local key v
  for key in model small_model; do
    for v in 'deepseek flash' ' deepseek-flash' 'deepseek/flash' 'deep$seek' \
             'deepseek;flash' 'deep"seek' 'deepseek{1m}' 'deepseek*'; do
      assert_reject_value "$key" "$v" "$key '$v'"
    done
  done
}

test_rejects_an_invalid_residency() {
  local v r121
  printf -v r121 '%0121d' 0   # 121 characters: one past the limit
  for v in 'US East #1' 'US$' 'US East & West' 'US{1}' "$r121"; do
    assert_reject_value residency "$v" "residency '$v'"
  done
  write_config_with "$TMP/value" residency 'US East (Virginia), tier 1 / eu-west_1'
  capture dg_config_parse "$TMP/value" deepseek
  assert_eq 0 "$RC" "a plain residency note is accepted"
}

# ── the file is data, never code ────────────────────────────────────────────

test_never_evaluates_a_config_value() {
  local path="$TMP/inject" payload
  payload='$(touch '"$TMP"'/pwned)'
  write_kv "$path" \
    endpoint 'https://api.deepseek.com/anthropic' \
    auth_env ANTHROPIC_API_KEY \
    key_dir '~/.config/deepseek' \
    model 'deepseek-flash[1m]' \
    residency "$payload"
  capture dg_config_parse "$path" deepseek
  assert_eq 2 "$RC" "a value with a command substitution is rejected"
  assert_fails "the payload never ran" test -e "$TMP/pwned"

  write_config_plus_raw "$path" 'prompt=$(touch '"$TMP"'/pwned2)'
  capture dg_config_parse "$path" deepseek
  assert_eq 2 "$RC" "a shell assignment line is rejected"
  assert_fails "the second payload never ran" test -e "$TMP/pwned2"
}

# ── listing providers ───────────────────────────────────────────────────────

test_lists_provider_ids_sorted_and_skips_everything_else() {
  local dir="$TMP/providers.d" expected
  mkdir -p "$dir"
  write_kv "$dir/qwen" timeout 1800      # contents are not validated here
  : > "$dir/deepseek"
  : > "$dir/.hidden"
  : > "$dir/UPPER"
  : > "$dir/with.dot"
  : > "$dir/-leading-dash"
  : > "$dir/deep seek"
  mkdir -p "$dir/nested"
  expected="$(printf '%s\n' deepseek qwen)"
  capture dg_config_providers "$dir"
  assert_eq 0 "$RC" "listing succeeds"
  assert_eq "$expected" "$OUT" "only valid provider ids, sorted"
}

test_lists_nothing_for_an_empty_or_missing_directory() {
  mkdir -p "$TMP/empty.d"
  capture dg_config_providers "$TMP/empty.d"
  assert_eq 0 "$RC" "an empty directory is not an error"
  assert_eq '' "$OUT" "an empty directory lists nothing"

  capture dg_config_providers "$TMP/missing.d"
  assert_eq 0 "$RC" "a missing directory is not an error"
  assert_eq '' "$OUT" "a missing directory lists nothing"
}

test_reads_the_provider_files_that_ship_with_the_tool() {
  local root path expected
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  expected="$(printf '%s\n' deepseek qwen)"
  capture dg_config_providers "$root/providers.d"
  assert_eq "$expected" "$OUT" "providers.d holds deepseek and qwen"

  for path in deepseek qwen; do
    capture dg_config_parse "$root/providers.d/$path" "$path"
    assert_eq 0 "$RC" "providers.d/$path parses"
    assert_contains "$OUT" $'auth_env\tANTHROPIC_API_KEY' "providers.d/$path sets auth_env"
    assert_contains "$OUT" $'key_dir\t~/.' "providers.d/$path points key_dir at ~/"
  done
}

run_tests
