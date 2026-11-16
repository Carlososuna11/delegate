#!/usr/bin/env bash
# test/oauth.test.sh — the minimal credentials store a sandboxed run builds for
# an MCP server that uses the OAuth login Claude Code saved.
#
# The fixtures are fake credential files written under $TMP. Nothing here reads,
# prints or copies the real ~/.claude: HOME always points into the scratch
# directory, and the sentinels below are the only "tokens" that exist.
#
# The bwrap in most cases is a fake script that records its argv and then execs
# the command after `--`, so the test can see exactly what would be mounted and
# can inspect the store while it exists. One case runs the real bwrap when this
# machine has a usable one, which is what proves the child sees the file at
# $HOME/.claude/.credentials.json and nothing else from ~/.claude.
source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/claude_oauth.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DELEGATE="$REPO_ROOT/bin/delegate"

FAKE_KEY='fake-key-oauth-suite'
FIGMA_TOKEN='figma-access-not-real-7c1d'
OTHER_TOKEN='other-access-not-real-9a2b'
CLAUDE_TOKEN='claude-login-not-real-5e4f'
REFRESH_TOKEN='refresh-not-real-3d8c'
CANARY='HOME-SECRET-CANARY-6b0a'

# ── fixtures ────────────────────────────────────────────────────────────────

write_fake_claude() {
  cat > "$TMP/bin/claude" <<FAKE
#!/bin/sh
# Fake Claude Code. Never calls a model; records what it got and probes
# ~/.claude from wherever it is running (host path for a fake bwrap, private
# HOME for the real one).
printf 'ARG:%s\n' "\$@" > "$TMP/claude.args" 2>/dev/null || true
env > "$TMP/claude.env" 2>/dev/null || true
{
  if [ -r "\$HOME/.claude/.credentials.json" ]; then
    printf 'CREDS_FILE:yes\n'
    cat "\$HOME/.claude/.credentials.json"
  else
    printf 'CREDS_FILE:no\n'
  fi
  printf 'LISTING:'
  ls -A "\$HOME/.claude" 2>/dev/null | tr '\n' ','
  printf '\n'
  if [ -e "\$HOME/.claude/HISTORY" ]; then printf 'CANARY:visible\n'; else printf 'CANARY:hidden\n'; fi
} > probe.out 2>/dev/null || true
exit "\$(cat "$TMP/claude.exit" 2>/dev/null || echo 0)"
FAKE
  chmod +x "$TMP/bin/claude"
}

write_fake_bwrap() {
  cat > "$TMP/bin/bwrap" <<FAKE
#!/bin/bash
# Stands in for bwrap: records the argv it would have run and copies the OAuth
# store while it exists, then runs the command after --. It confines nothing,
# which is why the real-bwrap case exists separately.
printf '%s\0' "\$@" > "$TMP/bwrap.argv"
: > "$TMP/bwrap.called"
args=("\$@")
i=0
while (( i < \${#args[@]} )); do
  [[ "\${args[i]}" == '--' ]] && { i=\$(( i + 1 )); break; }
  i=\$(( i + 1 ))
done
for (( j = 0; j < \${#args[@]}; j++ )); do
  if [[ "\${args[j]}" == '--ro-bind' && "\${args[j+2]}" == *'/.claude/.credentials.json' ]]; then
    src="\${args[j+1]}"
    cp -- "\$src" "$TMP/store.copy" 2>/dev/null || true
    ls -A "\$(dirname -- "\$src")" > "$TMP/store.listing" 2>/dev/null || true
    printf '%s' "\$src" > "$TMP/store.path"
    stat -c '%a' "\$src" > "$TMP/store.mode" 2>/dev/null || true
    stat -c '%a' "\$(dirname -- "\$src")" > "$TMP/store.dir.mode" 2>/dev/null || true
  fi
done
exec "\${args[@]:i}"
FAKE
  chmod +x "$TMP/bin/bwrap"
}

setup() {
  mkdir -p "$TMP/bin" "$TMP/home/.claude" "$TMP/providers.d" "$TMP/mcp.d" \
    "$TMP/work" "$TMP/keys"
  chmod 700 "$TMP/keys"
  printf '%s\n' "$FAKE_KEY" > "$TMP/keys/api_key.uno"
  chmod 600 "$TMP/keys/api_key.uno"
  ln -s api_key.uno "$TMP/keys/api_key"
  printf 'endpoint\thttps://api.example.test/anthropic\n' > "$TMP/providers.d/fake"
  printf 'auth_env\tANTHROPIC_API_KEY\n' >> "$TMP/providers.d/fake"
  printf 'key_dir\t%s\n' "$TMP/keys" >> "$TMP/providers.d/fake"
  printf 'model\tfake-model\nsmall_model\tfake-small\ntimeout\t60\ncaps\tread,mcp\n' \
    >> "$TMP/providers.d/fake"

  # figma is the shape that needs the saved login: remote and no token of its
  # own. The other two must never pull the store in.
  printf 'transport\thttp\nurl\thttps://mcp.figma.com/mcp\ntools_read\tget_design_context\n' \
    > "$TMP/mcp.d/figma"
  printf 'transport\thttp\nurl\thttps://token.example.test/mcp\ntools_read\tget_thing\nenv_passthrough\tSERVER_TOKEN\n' \
    > "$TMP/mcp.d/token"
  printf 'transport\tstdio\ntrust\tlocal-exec\ncommand\tnpx\ntools_read\tget_thing\n' \
    > "$TMP/mcp.d/local"

  write_fake_claude
  write_fake_bwrap
  export SERVER_TOKEN='server-token-not-real'

  DG_ENV=(
    env
    PATH="$TMP/bin:/usr/bin:/bin"
    HOME="$TMP/home"
    XDG_RUNTIME_DIR="$TMP"
    TMPDIR="$TMP"
    DELEGATE_PROVIDERS_DIR="$TMP/providers.d"
    DELEGATE_MCP_DIR="$TMP/mcp.d"
  )
}

# A fake ~/.claude/.credentials.json. <figma-expires-ms> is a parameter so the
# same fixture covers valid, about-to-expire and expired; pass `none` to leave
# figma out of the store.
write_creds() {
  local figma_exp="$1" body='' extra=''
  if [[ "$figma_exp" != none ]]; then
    body="\"figma|https://mcp.figma.com/mcp\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_TOKEN\",\"refreshToken\":\"$REFRESH_TOKEN\",\"expiresAt\":$figma_exp,\"scope\":\"mcp:connect\",\"tokenType\":\"Bearer\"},"
  fi
  extra="\"other|https://other.example.test/mcp\":{\"serverName\":\"other\",\"serverUrl\":\"https://other.example.test/mcp\",\"accessToken\":\"$OTHER_TOKEN\",\"expiresAt\":$(( ($(date +%s) + 7200) * 1000 ))}"
  printf '{"claudeAiOauth":{"accessToken":"%s","refreshToken":"%s","expiresAt":9999999999999},"mcpOAuth":{%s%s}}\n' \
    "$CLAUDE_TOKEN" "$REFRESH_TOKEN" "$body" "$extra" \
    > "$TMP/home/.claude/.credentials.json"
  chmod 600 "$TMP/home/.claude/.credentials.json"
  printf '%s\n' "$CANARY" > "$TMP/home/.claude/HISTORY"
  chmod 600 "$TMP/home/.claude/HISTORY"
}

future_ms() { printf '%s' "$(( ($(date +%s) + 3600) * 1000 ))"; }

run_delegate() {
  capture "${DG_ENV[@]}" "$DELEGATE" fake "$@"
}

clear_dump() {
  rm -f "$TMP"/claude.args "$TMP"/claude.env "$TMP"/bwrap.argv "$TMP"/bwrap.called \
    "$TMP"/store.copy "$TMP"/store.listing "$TMP"/store.path "$TMP"/store.mode \
    "$TMP"/store.dir.mode "$TMP/work/probe.out"
}

bwrap_argv() { tr '\0' '\n' < "$TMP/bwrap.argv" 2>/dev/null; }

# ── the scanner ─────────────────────────────────────────────────────────────

test_the_scanner_reads_a_real_shaped_file_regardless_of_order() {
  # The credentials file is another program's output: keys can be in any order,
  # there can be nested values before mcpOAuth, and a string can contain text
  # that looks like JSON. Only the top-level mcpOAuth member and its top-level
  # accessToken/expiresAt matter.
  local file="$TMP/shape.json" exp out
  exp="$(future_ms)"
  cat > "$file" <<EOF
{
  "projects": {"a": [1, {"nested": "mcpOAuth", "brace": "}"}], "b": null},
  "note": "a string with \\"quotes\\", a brace } and mcpOAuth:{}",
  "mcpOAuth": {
    "figma|https://mcp.figma.com/mcp": {
      "expiresAt": $exp,
      "scope": "ignored",
      "accessToken": "shape-token-not-real",
      "serverUrl": "https://mcp.figma.com/mcp",
      "serverName": "figma"
    },
    "other|https://other.example.test/mcp": {"accessToken": "nope", "expiresAt": $exp}
  }
}
EOF
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "the file parses"
  out="$OUT"
  assert_eq '{"mcpOAuth":{"figma|https://mcp.figma.com/mcp":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"shape-token-not-real","expiresAt":'"$exp"'}}}' \
    "$out" "exactly one entry, built from the four fields the run needs"
  assert_not_contains "$out" 'ignored' "the scope is not copied"
  assert_not_contains "$out" 'projects' "and nothing from the rest of the file"
}

test_a_token_that_would_break_the_json_is_refused() {
  local file="$TMP/evil.json"
  printf '{"mcpOAuth":{"figma|https://mcp.figma.com/mcp":{"accessToken":"a\\"b","expiresAt":%s}}}\n' \
    "$(future_ms)" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 2 "$RC" "a token with a quote in it is refused"
  assert_contains "$ERR" 'accessToken' "and the message names the field"
  assert_eq '' "$OUT" "nothing that could be pasted into JSON is printed"
}

# ── the minimal store ───────────────────────────────────────────────────────

test_the_store_has_only_the_requested_entry() {
  setup
  write_creds "$(future_ms)"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the sandboxed run succeeds"

  assert_eq 'yes' "$( [[ -f "$TMP/store.copy" ]] && echo yes || echo no )" \
    "a credentials file was mounted"
  # The key and its one entry, with the token; nothing else.
  assert_contains "$(cat "$TMP/store.copy")" '"figma|https://mcp.figma.com/mcp"' \
    "the store carries the requested entry under its name+url key"
  assert_contains "$(cat "$TMP/store.copy")" "$FIGMA_TOKEN" "with its access token"
  # Every other secret in the real-shaped file stays out.
  assert_not_contains "$(cat "$TMP/store.copy")" "$OTHER_TOKEN" "not another server's token"
  assert_not_contains "$(cat "$TMP/store.copy")" "$CLAUDE_TOKEN" "not the Anthropic login"
  assert_not_contains "$(cat "$TMP/store.copy")" "$REFRESH_TOKEN" "and no refresh token"
  assert_not_contains "$(cat "$TMP/store.copy")" 'mcp:connect' \
    "the scope is optional, not a reason to carry the rest of the entry"
  assert_eq '.credentials.json' "$(cat "$TMP/store.listing")" \
    "the store directory holds one file"
  assert_eq 600 "$(cat "$TMP/store.mode")" "the file is mode 600"
  assert_eq 700 "$(cat "$TMP/store.dir.mode")" "its directory is mode 700"

  local argv
  argv="$(bwrap_argv)"
  assert_contains "$argv" "--dir" "the child's config directory is created"
  # The mount destination is the one path Claude Code reads.
  assert_eq "$TMP/home/.claude/.credentials.json" \
    "$(printf '%s\n' "$argv" | awk '/\.claude\/\.credentials\.json$/ { print; exit }')" \
    "the store is mounted where Claude Code expects it"
}

test_nothing_is_mounted_without_an_oauth_server() {
  setup
  write_creds "$(future_ms)"
  # Unreadable on purpose: if a run that needs no OAuth touched it, it would
  # fail instead of succeeding.
  chmod 000 "$TMP/home/.claude/.credentials.json"
  chmod 000 "$TMP/home/.claude/HISTORY"

  run_delegate --sandbox bwrap --mcp local --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "a stdio server runs without reading the saved login"
  assert_eq 'yes' "$( [[ -f "$TMP/bwrap.called" ]] && echo yes || echo no )" "bwrap was used"
  assert_not_contains "$(bwrap_argv)" '.claude/.credentials.json' "no credentials file is mounted"
  assert_not_contains "$(bwrap_argv)" 'claude-oauth' "no store is built"

  clear_dump
  run_delegate --sandbox bwrap --mcp token --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "a server with env_passthrough runs without it"
  assert_not_contains "$(bwrap_argv)" '.claude/.credentials.json' "and nothing is mounted"

  clear_dump
  run_delegate --sandbox bwrap --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "a run with no MCP at all still succeeds"
  assert_not_contains "$(bwrap_argv)" '.claude/.credentials.json' "and mounts nothing"

  # Without a sandbox there is nothing to hide: the child sees the real
  # ~/.claude, so the run must not read or build anything.
  clear_dump
  run_delegate --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "a run without a sandbox does not need the store"
  assert_fails "bwrap was not used" test -f "$TMP/bwrap.called"
  assert_fails "and no store was built" test -f "$TMP/store.copy"
}

test_the_temp_store_is_removed_after_the_run() {
  setup
  write_creds "$(future_ms)"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the run succeeds"
  local store
  store="$(cat "$TMP/store.path")"
  assert_fails "the store file is gone after the run" test -e "$store"
  assert_eq 'no' "$( [[ -d "$(dirname "$(dirname "$store")")" ]] && echo yes || echo no )" \
    "and so is the run's private directory"

  # A child that fails is still an exit path.
  clear_dump
  printf '7\n' > "$TMP/claude.exit"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 7 "$RC" "the child's exit code reaches the caller"
  store="$(cat "$TMP/store.path")"
  assert_fails "the store is removed when the child fails too" test -e "$store"
  rm -f "$TMP/claude.exit"
}

test_missing_entry_fails_clearly_before_the_key_is_read() {
  setup
  write_creds none
  # A missing account proves the ordering: if the key were read first, this is
  # the error that would be printed.
  run_delegate --sandbox bwrap --mcp figma --account fantasma --dir "$TMP/work" "tarea"
  assert_eq 2 "$RC" "the run is refused with 2"
  assert_contains "$ERR" 'figma' "the message names the server"
  assert_contains "$ERR" 'figma|https://mcp.figma.com/mcp' "and the pair it looked for"
  assert_contains "$ERR" 'Claude Code' "and says where the login comes from"
  assert_not_contains "$ERR" 'api_key.fantasma' "the provider key was never resolved"
  assert_fails "no child was launched" test -f "$TMP/claude.args"

  # No credentials file at all is the same refusal, with the path it looked at.
  rm -f "$TMP/home/.claude/.credentials.json"
  clear_dump
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 2 "$RC" "a missing file is refused with 2"
  assert_contains "$ERR" "$TMP/home/.claude/.credentials.json" "naming the file"
  assert_contains "$ERR" '/mcp' "and telling the user what to run"
  assert_fails "no child was launched either" test -f "$TMP/claude.args"
}

test_expired_or_expiring_token_is_refused() {
  setup
  write_creds "$(( ($(date +%s) - 60) * 1000 ))"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 2 "$RC" "an expired token refuses the run"
  assert_contains "$ERR" 'ya venció' "says it expired"
  assert_contains "$ERR" 'Claude Code' "and how to refresh it"
  assert_fails "nothing reached the child" test -f "$TMP/claude.args"

  clear_dump
  write_creds "$(( ($(date +%s) + 60) * 1000 ))"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 2 "$RC" "a token about to expire refuses the run too"
  assert_contains "$ERR" 'vence en' "and says how long is left"
  assert_fails "nothing reached the child" test -f "$TMP/claude.args"
}

# ── the real sandbox ────────────────────────────────────────────────────────

REAL_BWRAP=''
real_bwrap_works() {
  # Resolved before the fake bwrap exists anywhere on PATH: the suite's own
  # PATH is untouched, only DG_ENV is per-run.
  REAL_BWRAP="$(type -P bwrap)" || return 1
  [[ "$REAL_BWRAP" == /* ]] || return 1
  "$REAL_BWRAP" --unshare-all --ro-bind /usr /usr --proc /proc --dev /dev \
    /bin/true >/dev/null 2>&1
}

test_the_real_sandbox_child_sees_only_the_minimal_login() {
  real_bwrap_works || return 0   # no usable bwrap here: the fake covers the argv
  setup
  write_creds "$(future_ms)"
  mkdir -p "$TMP/realbin"
  ln -s "$REAL_BWRAP" "$TMP/realbin/bwrap"
  DG_ENV=(
    env
    PATH="$TMP/realbin:$TMP/bin:/usr/bin:/bin"
    HOME="$TMP/home"
    XDG_RUNTIME_DIR="$TMP"
    TMPDIR="$TMP"
    DELEGATE_PROVIDERS_DIR="$TMP/providers.d"
    DELEGATE_MCP_DIR="$TMP/mcp.d"
  )
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the real sandboxed run succeeds"

  local probe
  probe="$(cat "$TMP/work/probe.out" 2>/dev/null)"
  assert_contains "$probe" 'CREDS_FILE:yes' "the child sees a credentials file at HOME"
  assert_contains "$probe" "$FIGMA_TOKEN" "with the requested access token"
  assert_not_contains "$probe" "$OTHER_TOKEN" "not another server's token"
  assert_not_contains "$probe" "$CLAUDE_TOKEN" "not the Anthropic login"
  assert_not_contains "$probe" "$REFRESH_TOKEN" "and no refresh token"
  assert_contains "$probe" 'LISTING:.credentials.json,' "~/.claude holds that one file"
  assert_contains "$probe" 'CANARY:hidden' "a file next to it in the real ~/.claude is not visible"
  assert_not_contains "$probe" "$CANARY" "and its content never crosses"
}

run_tests
