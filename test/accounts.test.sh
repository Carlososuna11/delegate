#!/usr/bin/env bash
# Tests for lib/accounts.sh.
#
# Every case builds its own key_dir inside $TMP (created and removed per test by
# the harness) and fills it with fake keys. Nothing in this file may read, write
# or delete anything under the real ~/.config/deepseek.
source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"
# Key dirs and files are born private, as the library requires of key_dir.
umask 077
ACCOUNTS_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/accounts.sh"
source "$ACCOUNTS_LIB"

# make_key <path> [contents] — creates a 600 key file, dirs included.
make_key() {
  local path="$1" content="${2:-fake-key}"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" >"$path"
  chmod 600 "$path"
}

# Assertions about filesystem presence/absence, spelled out so a mistake here
# cannot silently invert the intent.
assert_absent() {  # assert_absent <path> <msg>
  local path="$1" msg="${2:-}"
  if [[ -e $path || -L $path ]]; then
    fail "${msg:-absent}: $path still exists"
  else
    pass_detail "${msg:-absent}"
  fi
}

assert_present() {  # assert_present <path> <msg>
  local path="$1" msg="${2:-}"
  if [[ -e $path || -L $path ]]; then
    pass_detail "${msg:-present}"
  else
    fail "${msg:-present}: $path does not exist"
  fi
}

# Like `capture`, but feeds a fixed stdin. capture() cannot do that because it
# takes a command list, not a pipeline.
run_with_stdin() {
  local input="$1"; shift
  local o e
  o="$(mktemp)"; e="$(mktemp)"
  RC=0
  printf '%s' "$input" | "$@" >"$o" 2>"$e" || RC=$?
  OUT="$(cat "$o")"; ERR="$(cat "$e")"
  rm -f "$o" "$e"
}

# ── dg_account_path ──────────────────────────────────────────────────────────

test_path_builds_path_from_pattern() {
  capture dg_account_path "$TMP/keys" 'api_key.%s' 'work'
  assert_eq 0 "$RC" "exit 0 for a valid name"
  assert_eq "$TMP/keys/api_key.work" "$OUT" "pattern expanded with the account"
  assert_eq '' "$ERR" "nothing on stderr"

  capture dg_account_path "$TMP/keys" 'key_%s.txt' 'work'
  assert_eq "$TMP/keys/key_work.txt" "$OUT" "pattern with suffix"

  # key_dir is expanded but the file is not required to exist yet.
  assert_eq 0 "$RC" "path is printed even when nothing exists"
}

test_path_expands_leading_tilde() {
  capture dg_account_path '~/delegate-test-keys' 'api_key.%s' 'a'
  assert_eq 0 "$RC" "tilde key_dir accepted"
  assert_eq "$HOME/delegate-test-keys/api_key.a" "$OUT" "~/ expanded"
  # Printing a path must not create or touch anything in $HOME.
  assert_absent "$HOME/delegate-test-keys" "nothing was created under \$HOME"
}

test_path_rejects_names_that_could_escape() {
  local bad
  for bad in '../evil' '/etc/passwd' 'a/b' '..' '.' 'x y' 'A_b' 'UPPER' '-lead' '' 'a.b' 'x'$'\n''y'; do
    capture dg_account_path "$TMP/keys" 'api_key.%s' "$bad"
    assert_eq 2 "$RC" "refuses account name $(printf '%q' "$bad")"
    assert_eq '' "$OUT" "prints no path for $(printf '%q' "$bad")"
    assert_contains "$ERR" 'delegate:' "reports a delegate error for $(printf '%q' "$bad")"
  done
  # Nothing was built below $TMP because the name never reached a path.
  assert_eq '' "$(ls -A "$TMP" 2>/dev/null)" "no path was constructed under the key dir root"
}

test_path_refuses_a_name_that_lands_on_the_metadata_files() {
  # `%s` is a legal key_pattern, and `api_key` and `default_account` are legal
  # account names. Together the account file WAS the compatibility symlink, or
  # the default-account metadata: adding the first such account wrote the key
  # there, and activating it then replaced it with a symlink to itself.
  local combo pattern account
  for combo in '%s|api_key' '%s|default_account' 'api_%s|key' 'default_%s|account' \
               'api_k%s|ey' '.%s|hidden'; do
    pattern="${combo%%|*}"; account="${combo#*|}"
    capture dg_account_path "$TMP/keys" "$pattern" "$account"
    assert_eq 2 "$RC" "refuses pattern $pattern with account $account"
    assert_eq '' "$OUT" "prints no path for $pattern + $account"
  done
  # Control: the same patterns with an ordinary name are fine.
  capture dg_account_path "$TMP/keys" '%s' 'trabajo'
  assert_eq 0 "$RC" "a %s pattern with an ordinary name is accepted"
}

test_list_never_reports_the_metadata_files_as_accounts() {
  mkdir -p "$TMP/keys"
  printf 'k\n' >"$TMP/keys/trabajo"; chmod 600 "$TMP/keys/trabajo"
  ln -s "$TMP/keys/trabajo" "$TMP/keys/api_key"
  printf 'trabajo\n' >"$TMP/keys/default_account"
  capture dg_account_list "$TMP/keys" '%s'
  assert_eq 'trabajo' "$OUT" "only the real account is listed"
}

test_path_rejects_over_long_and_accepts_32_chars() {
  local name32='a2345678901234567890123456789012'  # 32 chars, the maximum
  capture dg_account_path "$TMP/keys" 'api_key.%s' "$name32"
  assert_eq 0 "$RC" "32 characters is accepted"
  capture dg_account_path "$TMP/keys" 'api_key.%s' "${name32}1"
  assert_eq 2 "$RC" "33 characters is refused"
}

# ── dg_account_list ──────────────────────────────────────────────────────────

test_list_prints_existing_names_sorted() {
  make_key "$TMP/keys/api_key.zeta"  'k-zeta'
  make_key "$TMP/keys/api_key.alpha" 'k-alpha'
  make_key "$TMP/keys/api_key.beta"  'k-beta'
  printf 'not-a-key\n' >"$TMP/keys/random.txt"
  make_key "$TMP/keys/api_key.FOO" 'k-upper'      # not a legal account name
  mkdir -p "$TMP/keys/api_key.dirname"            # a directory is not a key
  ln -s api_key.alpha "$TMP/keys/api_key"         # the projection is not an account

  capture dg_account_list "$TMP/keys" 'api_key.%s'
  assert_eq 0 "$RC" "exit 0"
  assert_eq 'alpha
beta
zeta' "$OUT" "sorted, one per line, only real accounts"
}

test_list_is_silent_when_empty() {
  capture dg_account_list "$TMP/keys" 'api_key.%s'
  assert_eq 0 "$RC" "exit 0 for a missing key_dir"
  assert_eq '' "$OUT" "no output for a missing key_dir"

  mkdir -p "$TMP/keys"
  capture dg_account_list "$TMP/keys" 'api_key.%s'
  assert_eq 0 "$RC" "exit 0 for an empty key_dir"
  assert_eq '' "$OUT" "no output for an empty key_dir"
}

# ── dg_account_active ────────────────────────────────────────────────────────

test_active_honours_a_providers_own_key_pattern() {
  # key_pattern is a provider setting, and this function used to hardcode
  # `api_key.`, so for any provider naming its files differently `account list`
  # showed them and `active` reported that nothing was active — which is also
  # how `account add` decided a second key was the first one.
  make_key "$TMP/keys/token-work" 'fake-key-a'
  ln -s token-work "$TMP/keys/api_key"
  capture dg_account_active "$TMP/keys" 'token-%s'
  assert_eq 0 "$RC" "exit 0 with a custom pattern"
  assert_eq 'work' "$OUT" "the name comes out of the custom pattern"

  # The same file is not an account under the default pattern.
  capture dg_account_active "$TMP/keys"
  assert_eq 1 "$RC" "the default pattern does not match it"
  assert_eq '' "$OUT" "and prints nothing"
}

test_active_resolves_the_symlink() {
  make_key "$TMP/keys/api_key.work" 'fake-key-a'
  ln -s api_key.work "$TMP/keys/api_key"

  capture dg_account_active "$TMP/keys"
  assert_eq 0 "$RC" "exit 0"
  assert_eq 'work' "$OUT" "active account name"

  # Absolute target too.
  rm -f "$TMP/keys/api_key"
  ln -s "$TMP/keys/api_key.work" "$TMP/keys/api_key"
  capture dg_account_active "$TMP/keys"
  assert_eq 'work' "$OUT" "absolute symlink target"
}

test_active_refuses_unrecognisable_links() {
  make_key "$TMP/keys/api_key.work" 'fake-key-a'

  capture dg_account_active "$TMP/keys"                    # no api_key at all
  assert_eq 1 "$RC" "missing api_key"
  assert_eq '' "$OUT" "prints nothing when missing"

  printf 'plain file\n' >"$TMP/keys/api_key"               # not a symlink
  capture dg_account_active "$TMP/keys"
  assert_eq 1 "$RC" "api_key is a regular file"
  assert_eq '' "$OUT" "prints nothing for a regular file"
  rm -f "$TMP/keys/api_key"

  ln -s /etc/passwd "$TMP/keys/api_key"                    # outside key_dir
  capture dg_account_active "$TMP/keys"
  assert_eq 1 "$RC" "link pointing outside key_dir"
  assert_eq '' "$OUT" "prints nothing for an outside link"

  rm -f "$TMP/keys/api_key"
  ln -s 'random.txt' "$TMP/keys/api_key"                   # inside, wrong shape
  capture dg_account_active "$TMP/keys"
  assert_eq 1 "$RC" "link to a file that is not a key file"

  rm -f "$TMP/keys/api_key"
  make_key "$TMP/keys-otro/api_key.other" 'k-other'
  ln -s '../keys-otro/api_key.other' "$TMP/keys/api_key"   # sibling dir via ..
  capture dg_account_active "$TMP/keys"
  assert_eq 1 "$RC" "relative link escaping key_dir"
}

# ── dg_account_fingerprint ───────────────────────────────────────────────────

test_fingerprint_differs_and_never_shows_the_key() {
  local a b
  make_key "$TMP/keys/api_key.a" 'fake-key-a'
  make_key "$TMP/keys/api_key.b" 'fake-key-b'

  a=$(dg_account_fingerprint "$TMP/keys/api_key.a"); assert_eq 0 "$?" "a is readable"
  b=$(dg_account_fingerprint "$TMP/keys/api_key.b"); assert_eq 0 "$?" "b is readable"

  assert_ne "$a" "$b" "different keys, different fingerprints"
  assert_eq 8 "${#a}" "8 hex characters"
  if [[ "$a" =~ ^[0-9a-f]{8}$ ]]; then pass_detail "hex only"
  else fail "fingerprint is not 8 hex chars: $a"; fi
  assert_not_contains "$a" 'fake-key-a' "the fingerprint never contains the key"
  assert_not_contains "$b" 'fake-key-b' "the fingerprint never contains the key"

  capture dg_account_fingerprint "$TMP/keys/nope"
  assert_eq 1 "$RC" "unreadable path fails"
  assert_eq '' "$OUT" "no output when failing"
}

# ── dg_account_validate ──────────────────────────────────────────────────────

test_validate_accepts_a_good_key() {
  make_key "$TMP/keys/api_key.work" 'fake-key-a'
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.work"
  assert_eq 0 "$RC" "600 file inside key_dir"
  assert_eq '' "$ERR" "nothing on stderr"

  # Inside key_dir through a symlink that stays inside is fine too.
  ln -s api_key.work "$TMP/keys/api_key.alias"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.alias"
  assert_eq 0 "$RC" "symlink resolving inside key_dir"
}

test_validate_rejects_mode_other_than_600() {
  local mode
  for mode in 644 640 400 777 660; do
    make_key "$TMP/keys/api_key.m$mode" 'fake-key-a'
    chmod "$mode" "$TMP/keys/api_key.m$mode"
    capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.m$mode"
    assert_eq 1 "$RC" "mode $mode is refused"
    assert_contains "$ERR" '600' "mode $mode: error names the expected mode"
  done
}

test_validate_rejects_paths_outside_key_dir() {
  mkdir -p "$TMP/keys"
  make_key "$TMP/outside/api_key.a" 'fake-key-a'
  capture dg_account_validate "$TMP/keys" "$TMP/outside/api_key.a"
  assert_eq 1 "$RC" "a 600 key outside key_dir is refused"
  assert_contains "$ERR" 'outside' "error says outside"

  # Sibling directory whose name merely starts with key_dir's name: a string
  # prefix test would accept this, component comparison must not.
  make_key "$TMP/keys-otro/api_key.a" 'fake-key-a'
  capture dg_account_validate "$TMP/keys" "$TMP/keys-otro/api_key.a"
  assert_eq 1 "$RC" "keys-otro is not inside keys"
  assert_contains "$ERR" 'outside' "sibling prefix is refused"

  capture dg_account_validate "$TMP/keys" "$TMP/keys"
  assert_eq 1 "$RC" "key_dir itself is not inside key_dir"

  capture dg_account_validate "$TMP/keys" "$TMP/keys/nope/api_key.a"
  assert_eq 1 "$RC" "path under a missing subdir is refused"
}

test_validate_rejects_a_symlink_pointing_outside() {
  mkdir -p "$TMP/keys"
  ln -s /etc/passwd "$TMP/keys/api_key.evil"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.evil"
  assert_eq 1 "$RC" "symlink to /etc/passwd is refused"
  assert_contains "$ERR" 'outside' "error says outside"

  # A perfectly valid 600 key, but reached through a symlink from key_dir.
  make_key "$TMP/outside/api_key.a" 'fake-key-a'
  ln -s "$TMP/outside/api_key.a" "$TMP/keys/api_key.sneaky"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.sneaky"
  assert_eq 1 "$RC" "symlink to a 600 key outside key_dir is refused"

  ln -s /etc/nonexistent-target "$TMP/keys/api_key.dangling"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.dangling"
  assert_eq 1 "$RC" "dangling symlink is refused"
}

test_validate_rejects_non_files_and_empty_files() {
  mkdir -p "$TMP/keys/api_key.adir"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.adir"
  assert_eq 1 "$RC" "a directory is refused"

  : >"$TMP/keys/api_key.empty"; chmod 600 "$TMP/keys/api_key.empty"
  capture dg_account_validate "$TMP/keys" "$TMP/keys/api_key.empty"
  assert_eq 1 "$RC" "an empty key is refused"
  assert_contains "$ERR" 'empty' "error says empty"

  capture dg_account_validate "$TMP/nonexistent-dir" "$TMP/keys/api_key.empty"
  assert_eq 1 "$RC" "a missing key_dir is refused"
}

test_validate_rejects_a_key_dir_that_is_a_sibling_of_itself_via_symlink() {
  # key_dir reached through a symlink is fine as long as the resolved sides
  # agree; the link must not be able to make outside look like inside.
  mkdir -p "$TMP/real" "$TMP/link-away"
  ln -s "$TMP/real" "$TMP/keys-link"
  make_key "$TMP/link-away/api_key.a" 'fake-key-a'
  capture dg_account_validate "$TMP/keys-link" "$TMP/link-away/api_key.a"
  assert_eq 1 "$RC" "outside stays outside through a symlinked key_dir"

  make_key "$TMP/real/api_key.a" 'fake-key-a'
  capture dg_account_validate "$TMP/keys-link" "$TMP/real/api_key.a"
  assert_eq 0 "$RC" "inside stays inside through a symlinked key_dir"
}

# ── dg_account_read ──────────────────────────────────────────────────────────

test_read_strips_whitespace_and_cr() {
  mkdir -p "$TMP/keys"
  printf '\n   fake-key-a \r\n\n' >"$TMP/keys/api_key.pad"
  chmod 600 "$TMP/keys/api_key.pad"
  capture dg_account_read "$TMP/keys/api_key.pad"
  assert_eq 0 "$RC" "exit 0"
  assert_eq 'fake-key-a' "$OUT" "surrounding space, CR and LF stripped"

  capture dg_account_read "$TMP/keys/nope"
  assert_eq 1 "$RC" "unreadable file fails"
  assert_eq '' "$OUT" "no output when it fails"
}

# ── dg_account_activate ──────────────────────────────────────────────────────

test_activate_points_api_key_at_the_target() {
  make_key "$TMP/keys/api_key.alpha" 'k-alpha'
  make_key "$TMP/keys/api_key.beta"  'k-beta'

  capture dg_account_activate "$TMP/keys" 'api_key.alpha'
  assert_eq 0 "$RC" "activate succeeds"
  assert_present "$TMP/keys/api_key" "api_key exists as a symlink"
  assert_eq 'alpha' "$(dg_account_active "$TMP/keys")" "active is alpha"

  dg_account_activate "$TMP/keys" 'api_key.beta'
  assert_eq 'beta' "$(dg_account_active "$TMP/keys")" "active switched to beta"

  # No temporary symlink survives a successful activation.
  local leftovers
  leftovers=$(find "$TMP/keys" -maxdepth 1 -name '.api_key.tmp.*' | wc -l)
  assert_eq 0 "$leftovers" "no temporary symlink left behind"

  capture dg_account_activate "$TMP/nonexistent" 'api_key.alpha'
  assert_eq 1 "$RC" "activating in a missing key_dir fails"
}

test_activate_never_leaves_api_key_missing() {
  local dir="$TMP/keys" writer missing=0 samples=0 active rc
  mkdir -p "$dir"
  make_key "$dir/api_key.alpha" 'k-alpha'
  make_key "$dir/api_key.beta"  'k-beta'
  # Point it somewhere before sampling starts, so the only thing under test is
  # the swap and not the very first activation.
  dg_account_activate "$dir" 'api_key.alpha'
  assert_present "$dir/api_key" "api_key exists before the swapping starts"

  # One process swaps the active account as fast as it can...
  (
    local i
    for (( i = 0; i < 400; i++ )); do
      if (( i % 2 )); then
        dg_account_activate "$dir" 'api_key.alpha'
      else
        dg_account_activate "$dir" 'api_key.beta'
      fi
    done
  ) &
  writer=$!

  # ...while this one samples the path, looking for the instant where a
  # non-atomic `ln -sfn` would have removed it and not yet recreated it.
  while kill -0 "$writer" 2>/dev/null; do
    [[ -e $dir/api_key ]] || missing=$(( missing + 1 ))
    samples=$(( samples + 1 ))
  done
  wait "$writer"; rc=$?

  assert_eq 0 "$rc" "the swapping process completed all 400 activations"
  assert_eq 0 "$missing" "api_key was never absent during $samples samples"

  active=$(dg_account_active "$dir")
  assert_eq 0 "$?" "still resolves to a real account afterwards"
  [[ "$active" == 'alpha' || "$active" == 'beta' ]] &&
    pass_detail "active is one of the two accounts" ||
    fail "active is neither alpha nor beta: $(printf '%q' "$active")"
}

test_activate_cleans_up_when_the_rename_fails() {
  local dir="$TMP/keys" leftovers
  mkdir -p "$dir"
  make_key "$dir/api_key.alpha" 'k-alpha'
  mkdir -p "$dir/api_key"          # blocks rename(2) with EISDIR

  capture dg_account_activate "$dir" 'api_key.alpha'
  assert_eq 1 "$RC" "activation fails when api_key cannot be replaced"
  assert_contains "$ERR" 'delegate:' "reports the failure"

  leftovers=$(find "$dir" -maxdepth 1 -name '.api_key.tmp.*' | wc -l)
  assert_eq 0 "$leftovers" "temporary symlink removed after the failure"
}

# ── dg_account_add / dg_account_remove ───────────────────────────────────────

test_add_from_stdin_creates_a_600_key() {
  local dir="$TMP/keys"
  run_with_stdin 'fake-key-a
' dg_account_add "$dir" 'api_key.%s' 'work'
  assert_eq 0 "$RC" "exit 0"
  assert_eq '' "$OUT" "never prints the key"
  assert_not_contains "$ERR" 'fake-key-a' "never logs the key either"

  assert_eq 'fake-key-a' "$(cat "$dir/api_key.work")" "key stored"
  assert_eq '' "$(cat "$dir/api_key.work" | tr -d '\n' | sed 's/fake-key-a//')" "nothing else stored"
  assert_file_mode "$dir/api_key.work" 600 "key file is 600"
  assert_file_mode "$dir" 700 "key dir created with 700"
  assert_eq 'work' "$(dg_account_list "$dir" 'api_key.%s')" "the account is listed"
  assert_ok "the new key passes validation" dg_account_validate "$dir" "$dir/api_key.work"
  assert_eq 'fake-key-a' "$(dg_account_read "$dir/api_key.work")" "reads back"
}

test_add_refuses_duplicates_and_empty_input() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.work" 'original-key'

  run_with_stdin 'second-key
' dg_account_add "$dir" 'api_key.%s' 'work'
  assert_eq 1 "$RC" "an existing account is refused"
  assert_contains "$ERR" 'already exists' "reports the clash"
  assert_eq 'original-key' "$(cat "$dir/api_key.work")" "the existing key is untouched"

  run_with_stdin '
' dg_account_add "$dir" 'api_key.%s' 'empty'
  assert_eq 1 "$RC" "empty input is refused"
  assert_absent "$dir/api_key.empty" "no file written for empty input"

  run_with_stdin '' dg_account_add "$dir" 'api_key.%s' 'eof'
  assert_eq 1 "$RC" "EOF with nothing read is refused"
  assert_absent "$dir/api_key.eof" "no file written on EOF"

  run_with_stdin 'k
' dg_account_add "$dir" 'api_key.%s' '../escape'
  assert_eq 2 "$RC" "an illegal account name is refused with the path error code"
  assert_absent "$TMP/escape" "nothing created outside the key dir"

  # The refused key is never echoed back, in stdout or stderr.
  assert_not_contains "$OUT$ERR" 'second-key' "the rejected key is never printed"
}

test_remove_active_returns_10_and_promotes_nothing() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.alpha" 'k-alpha'
  make_key "$dir/api_key.beta"  'k-beta'
  dg_account_activate "$dir" 'api_key.alpha'

  capture dg_account_remove "$dir" 'api_key.%s' 'alpha'
  assert_eq 10 "$RC" "removing the active account returns 10"
  assert_absent "$dir/api_key.alpha" "the key file is gone"
  assert_absent "$dir/api_key" "the dangling api_key symlink is gone"
  assert_present "$dir/api_key.beta" "beta is still there"
  capture dg_account_active "$dir"
  assert_eq 1 "$RC" "no account is active any more"
  assert_absent "$dir/default_account" "no default account was written"
}

test_remove_inactive_keeps_the_active_one() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.alpha" 'k-alpha'
  make_key "$dir/api_key.beta"  'k-beta'
  dg_account_activate "$dir" 'api_key.beta'

  capture dg_account_remove "$dir" 'api_key.%s' 'alpha'
  assert_eq 0 "$RC" "removing an inactive account returns 0"
  assert_absent "$dir/api_key.alpha" "alpha is gone"
  assert_eq 'beta' "$(dg_account_active "$dir")" "beta is still active"
  assert_present "$dir/api_key" "api_key still resolves"

  capture dg_account_remove "$dir" 'api_key.%s' 'ghost'
  assert_eq 1 "$RC" "removing something that does not exist returns 1"
}

# ── default account ──────────────────────────────────────────────────────────

test_default_get_and_set_roundtrip() {
  local dir="$TMP/keys"

  capture dg_account_default_get "$dir"
  assert_eq 1 "$RC" "no default account yet"
  assert_eq '' "$OUT" "prints nothing"

  capture dg_account_default_set "$dir" 'work'
  assert_eq 0 "$RC" "set succeeds"
  assert_file_mode "$dir/default_account" 644 "metadata file is 644"
  assert_file_mode "$dir" 700 "key dir created with 700"
  assert_eq 'work' "$(dg_account_default_get "$dir")" "round trip"
  assert_eq 0 "$(find "$dir" -maxdepth 1 -name '.default_account.tmp.*' | wc -l)" "no temp file left behind"

  dg_account_default_set "$dir" 'other'
  assert_eq 'other' "$(dg_account_default_get "$dir")" "replaces the previous value"
  assert_eq 0 "$(find "$dir" -maxdepth 1 -name '.default_account.tmp.*' | wc -l)" "no temp file after replacing"
}

test_default_get_refuses_garbage() {
  local dir="$TMP/keys"
  mkdir -p "$dir"

  printf '../evil\n' >"$dir/default_account"
  capture dg_account_default_get "$dir"
  assert_eq 1 "$RC" "a name that does not validate is refused"
  assert_eq '' "$OUT" "prints nothing"

  printf '\n' >"$dir/default_account"
  capture dg_account_default_get "$dir"
  assert_eq 1 "$RC" "an empty name is refused"
}

test_default_set_refuses_an_illegal_name() {
  local dir="$TMP/keys"
  capture dg_account_default_set "$dir" '../evil'
  assert_eq 2 "$RC" "illegal name refused"
  assert_absent "$TMP/evil" "nothing written outside"
  assert_absent "$dir/default_account" "no metadata file created"
}

# ── A11: races on key creation and on key reading ────────────────────────────

# A key_dir anyone else can enter or write lets them swap a validated key for
# another file between the check and the read, or plant a link where `add` is
# about to write. It is checked on every use, not only when `add` creates it.
test_a_key_dir_that_is_not_private_is_refused_on_every_use() {
  local dir="$TMP/keys" mode
  make_key "$dir/api_key.work" 'fake-key-a'
  for mode in 750 705 770 777 755; do
    chmod "$mode" "$dir"
    capture dg_account_validate "$dir" "$dir/api_key.work"
    assert_eq 1 "$RC" "validate refuses a key_dir in mode $mode"
    assert_contains "$ERR" '700' "mode $mode: the error names the expected mode"
    capture dg_account_load "$dir" "$dir/api_key.work"
    assert_eq 1 "$RC" "load refuses a key_dir in mode $mode"
    assert_eq '' "$OUT" "mode $mode: load prints no key"
    run_with_stdin 'fake-key-b
' dg_account_add "$dir" 'api_key.%s' 'other'
    assert_eq 1 "$RC" "add refuses a key_dir in mode $mode"
    assert_absent "$dir/api_key.other" "mode $mode: add wrote nothing"
  done
  chmod 700 "$dir"
  assert_ok "control: the same key_dir in 700 is accepted" \
    dg_account_validate "$dir" "$dir/api_key.work"
}

# The existence check runs before stdin is read, so whatever lands on the path
# while `add` waits for the key is planted AFTER it. The writer below plants it
# and only then sends the key. The old `printf >"$path"` followed the link and
# wrote the key wherever it pointed.
test_add_never_writes_through_something_planted_after_its_check() {
  local dir="$TMP/keys" leftovers
  mkdir -p "$dir"
  printf 'victim-original\n' >"$TMP/victim"

  dg_account_add "$dir" 'api_key.%s' 'work' \
    < <(sleep 0.3; ln -s "$TMP/victim" "$dir/api_key.work"; printf 'fake-key-a\n') \
    >"$TMP/out" 2>"$TMP/err" && RC=0 || RC=$?
  assert_eq 1 "$RC" "a symlink planted at the destination makes add fail"
  assert_eq 'victim-original' "$(cat "$TMP/victim")" "the link target is untouched"
  [[ -L $dir/api_key.work ]] && pass_detail "the planted link is left as it was" ||
    fail "the planted link was replaced"

  dg_account_add "$dir" 'api_key.%s' 'ghost' \
    < <(sleep 0.3; ln -s "$TMP/created-outside" "$dir/api_key.ghost"; printf 'fake-key-a\n') \
    >"$TMP/out" 2>"$TMP/err" && RC=0 || RC=$?
  assert_eq 1 "$RC" "a dangling symlink planted at the destination makes add fail"
  assert_absent "$TMP/created-outside" "nothing is created through a dangling link"

  dg_account_add "$dir" 'api_key.%s' 'plain' \
    < <(sleep 0.3; printf 'planted\n' >"$dir/api_key.plain"; printf 'fake-key-a\n') \
    >"$TMP/out" 2>"$TMP/err" && RC=0 || RC=$?
  assert_eq 1 "$RC" "a file planted at the destination makes add fail"
  assert_eq 'planted' "$(cat "$dir/api_key.plain")" "the planted file is not overwritten"
  assert_not_contains "$(cat "$TMP/out" "$TMP/err")" 'fake-key-a' "the key is never printed"

  leftovers=$(find "$dir" -maxdepth 1 -name '.*' -type f | wc -l)
  assert_eq 0 "$leftovers" "no temporary file is left behind"
}

# dg_account_load opens the file once and checks THAT descriptor (where it
# points, its type, mode, owner and size) before reading from it, so the file
# read is the file checked. The cases below are the ones validate refuses.
test_load_checks_and_reads_the_same_descriptor() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.work" $'\n  fake-key-a \r'
  capture dg_account_load "$dir" "$dir/api_key.work"
  assert_eq 0 "$RC" "a good key loads"
  assert_eq 'fake-key-a' "$OUT" "trimmed like dg_account_read"
  assert_eq '' "$ERR" "nothing on stderr"

  ln -s api_key.work "$dir/api_key.alias"
  capture dg_account_load "$dir" "$dir/api_key.alias"
  assert_eq 'fake-key-a' "$OUT" "a link that stays inside key_dir loads"

  make_key "$TMP/outside/api_key.a" 'outside-key'
  ln -s "$TMP/outside/api_key.a" "$dir/api_key.sneaky"
  capture dg_account_load "$dir" "$dir/api_key.sneaky"
  assert_eq 1 "$RC" "a link leaving key_dir is refused"
  assert_contains "$ERR" 'outside' "the error says outside"
  assert_not_contains "$OUT$ERR" 'outside-key' "the outside key is never printed"

  make_key "$dir/api_key.wide" 'wide-key'; chmod 644 "$dir/api_key.wide"
  capture dg_account_load "$dir" "$dir/api_key.wide"
  assert_eq 1 "$RC" "mode 644 is refused"
  assert_not_contains "$OUT$ERR" 'wide-key' "a refused key is never printed"

  : >"$dir/api_key.empty"; chmod 600 "$dir/api_key.empty"
  capture dg_account_load "$dir" "$dir/api_key.empty"
  assert_eq 1 "$RC" "an empty key is refused"

  mkdir -m 700 "$dir/api_key.adir"
  capture dg_account_load "$dir" "$dir/api_key.adir"
  assert_eq 1 "$RC" "a directory is refused"

  mkfifo -m 600 "$dir/api_key.fifo"
  capture timeout 5 bash -c 'source "$1"; dg_account_load "$2" "$3"' _ \
    "$ACCOUNTS_LIB" "$dir" "$dir/api_key.fifo"
  assert_eq 1 "$RC" "a fifo is refused without blocking"

  capture dg_account_load "$dir" "$dir/api_key.nope"
  assert_eq 1 "$RC" "a missing file is refused"
}

# `%F` in stat is translated: on a system in another language "regular file"
# comes back in that language, and a check comparing it with the English text
# refused every valid key. Simulated by a `stat` that answers in Spanish.
test_load_does_not_depend_on_the_language_of_stat() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.work" 'fake-key-a'
  stat() {
    command stat "$@" | sed -e 's/regular empty file/fichero regular vacío/' \
                            -e 's/regular file/fichero regular/' -e 's/directory/directorio/'
  }
  capture dg_account_load "$dir" "$dir/api_key.work"
  unset -f stat
  assert_eq 0 "$RC" "a valid key loads whatever language stat answers in"
  assert_eq 'fake-key-a' "$OUT" "and it is the key"
}

# The descriptor check looked for `|600|` anywhere in "type|mode|uid|size", so
# for a user whose uid is 600 a 644 file passed on the uid field. That check is
# the one meant to catch a file changed after the path checks, so the change
# is simulated: a `stat` that reports 644 only when asked about the descriptor.
# uid 600 comes from a user namespace.
test_the_descriptor_mode_check_reads_the_mode_field_only() {
  local dir="$TMP/keys"
  make_key "$dir/api_key.work" 'fake-key-a'
  if ! unshare -U --map-user=600 --map-group=600 true 2>/dev/null; then
    pass_detail "skipped: no user namespaces on this machine"
    return 0
  fi
  local probe='
    source "$1"
    stat() {
      if [[ "$*" == */proc/*/fd/* ]]; then command stat "$@" | sed "s/|600|/|644|/"
      else command stat "$@"; fi
    }
    dg_account_load "$2" "$3"'
  capture unshare -U --map-user=600 --map-group=600 bash -c "$probe" _ \
    "$ACCOUNTS_LIB" "$dir" "$dir/api_key.work"
  assert_eq 1 "$RC" "a 644 descriptor is refused for uid 600"
  assert_eq '' "$OUT" "and no key is printed"

  capture unshare -U --map-user=600 --map-group=600 bash -c 'source "$1"; dg_account_load "$2" "$3"' _ \
    "$ACCOUNTS_LIB" "$dir" "$dir/api_key.work"
  assert_eq 'fake-key-a' "$OUT" "control: unchanged, the key loads for uid 600"
}

# `add` writes the key to a dot-named temporary and then links it into place.
# Interrupted in between, the temporary — a copy of the key — stayed behind.
# The interruption is made deterministic with an `ln` that takes a while.
test_an_interrupted_add_leaves_no_temporary_key_behind() {
  local dir="$TMP/keys" sig pid tries leftovers
  mkdir -p "$TMP/slow"
  printf '#!/bin/sh\nsleep 1\nexec %q "$@"\n' "$(type -P ln)" >"$TMP/slow/ln"
  chmod +x "$TMP/slow/ln"
  for sig in TERM INT HUP; do
    rm -rf "$dir"; mkdir -m 700 "$dir"
    ( PATH="$TMP/slow:$PATH"; dg_account_add "$dir" 'api_key.%s' work <<<'fake-key-a' ) \
      >/dev/null 2>&1 &
    pid=$!
    tries=100
    until compgen -G "$dir/.api_key.add.*" >/dev/null || (( tries-- <= 0 )); do sleep 0.02; done
    compgen -G "$dir/.api_key.add.*" >/dev/null || { fail "$sig: control: the temporary never appeared"; continue; }
    kill -s "$sig" "$pid"
    wait "$pid" 2>/dev/null
    sleep 1.2   # the slow ln outlives an untrapped shell
    leftovers=$(find "$dir" -maxdepth 1 -name '.api_key.add.*' | wc -l)
    assert_eq 0 "$leftovers" "$sig: no temporary copy of the key is left"
  done
}

run_tests
