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
FIGMA_OLD_TOKEN='figma-older-access-not-real-11f2'
FIGMA_NOEXP_TOKEN='figma-no-expiry-not-real-4b7e'
OTHER_TOKEN='other-access-not-real-9a2b'
DECOY_TOKEN='decoy-access-not-real-2e6d'
CLAUDE_TOKEN='claude-login-not-real-5e4f'
REFRESH_TOKEN='refresh-not-real-3d8c'
CLIENT_SECRET='client-secret-not-real-8c1a'
CANARY='HOME-SECRET-CANARY-6b0a'

# Claude Code keys mcpOAuth entries as `<serverName>|<hash>`, the hash being one
# it computes from the server config. The fixtures use keys of that shape: the
# lookup must find the entry by its fields and copy this key verbatim.
FIGMA_KEY='figma|d39d3b6252bc1ac5'
FIGMA_OLD_KEY='figma|0f1e2d3c4b5a6978'
DECOY_KEY='figma|https://mcp.figma.com/mcp'
OTHER_NAME_KEY='acme|7c9d1e3f5a2b4c6d'
OTHER_KEY='other|a1b2c3d4e5f60718'

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

# A fake ~/.claude/.credentials.json with the real shape: `claudeAiOauth` next
# to `mcpOAuth`, `<name>|<hash>` keys, entries carrying the extra fields a real
# login has (refreshToken, clientId, clientSecret, scope, issuer, redirectUri,
# a nested discoveryState) and other servers. <figma-expires-ms> is a parameter
# so the same fixture covers valid, about-to-expire and expired; pass `none` to
# leave the figma entry out of the store.
write_creds() {
  local figma_exp="$1" entry body=''
  local other_exp=$(( ($(date +%s) + 7200) * 1000 ))
  local -a entries=()

  if [[ "$figma_exp" != none ]]; then
    entries+=("\"$FIGMA_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_TOKEN\",\"refreshToken\":\"$REFRESH_TOKEN\",\"expiresAt\":$figma_exp,\"clientId\":\"figma-client-not-real\",\"clientSecret\":\"$CLIENT_SECRET\",\"scope\":\"mcp:connect\",\"issuer\":\"https://auth.figma.example\",\"redirectUri\":\"http://127.0.0.1:53211/callback\",\"discoveryState\":{\"tokenEndpoint\":\"https://auth.figma.example/token\",\"nested\":{\"brace\":\"}\"}}}")
  fi
  # The key a lookup by `<name>|<url>` would have built, over fields of a
  # different server: matching on the key text would pick this one up.
  entries+=("\"$DECOY_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/other\",\"accessToken\":\"$DECOY_TOKEN\",\"expiresAt\":$other_exp,\"clientSecret\":\"$CLIENT_SECRET\"}")
  # The requested url under another name: not this server either.
  entries+=("\"$OTHER_NAME_KEY\":{\"serverName\":\"acme\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$DECOY_TOKEN\",\"expiresAt\":$other_exp}")
  entries+=("\"$OTHER_KEY\":{\"serverName\":\"other\",\"serverUrl\":\"https://other.example.test/mcp\",\"accessToken\":\"$OTHER_TOKEN\",\"refreshToken\":\"$REFRESH_TOKEN\",\"expiresAt\":$other_exp,\"clientId\":\"other-client-not-real\",\"clientSecret\":\"$CLIENT_SECRET\",\"scope\":\"mcp:connect\"}")

  for entry in "${entries[@]}"; do body="${body:+$body,}$entry"; done

  printf '{"claudeAiOauth":{"accessToken":"%s","refreshToken":"%s","expiresAt":9999999999999,"scopes":["user:inference"],"subscriptionType":"max"},"mcpOAuth":{%s}}\n' \
    "$CLAUDE_TOKEN" "$REFRESH_TOKEN" "$body" \
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
  # that looks like JSON. Only the top-level mcpOAuth member and the entry whose
  # own serverName/serverUrl match are read, and the entry keeps the hash key
  # the file used, because that is what Claude Code looks it up by.
  local file="$TMP/shape.json" exp out
  exp="$(future_ms)"
  cat > "$file" <<EOF
{
  "claudeAiOauth": {"accessToken": "$CLAUDE_TOKEN", "expiresAt": 9999999999999},
  "projects": {"a": [1, {"nested": "mcpOAuth", "brace": "}"}], "b": null},
  "note": "a string with \\"quotes\\", a brace } and mcpOAuth:{}",
  "mcpOAuth": {
    "$DECOY_KEY": {
      "serverName": "figma",
      "serverUrl": "https://mcp.figma.com/other",
      "accessToken": "$DECOY_TOKEN",
      "expiresAt": $exp
    },
    "$FIGMA_KEY": {
      "expiresAt": $exp,
      "scope": "ignored",
      "accessToken": "shape-token-not-real",
      "serverUrl": "https://mcp.figma.com/mcp",
      "serverName": "figma",
      "discoveryState": {"tokenEndpoint": "https://auth.figma.example/token"}
    },
    "$OTHER_KEY": {"accessToken": "nope", "expiresAt": $exp}
  }
}
EOF
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "the file parses"
  out="$OUT"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"shape-token-not-real","expiresAt":'"$exp"'}}}' \
    "$out" "exactly one entry, under its original hash key, from the four fields the run needs"
  assert_not_contains "$out" 'ignored' "the scope is not copied"
  assert_not_contains "$out" "$DECOY_TOKEN" "an entry keyed like the old <name>|<url> form is not it"
  assert_not_contains "$out" 'claudeAiOauth' "and nothing from the rest of the file"
  assert_not_contains "$out" 'projects' "not even the unrelated members"
}

test_the_entry_is_found_by_its_fields_not_by_the_key_text() {
  local file="$TMP/lookup.json" exp
  exp="$(future_ms)"
  # The old `<name>|<url>` key text, and the requested url under another name:
  # a lookup by key text would take either for the requested server.
  cat > "$file" <<EOF
{"mcpOAuth":{
  "$DECOY_KEY":{"serverName":"figma","serverUrl":"https://mcp.figma.com/other","accessToken":"$DECOY_TOKEN","expiresAt":$exp},
  "$OTHER_NAME_KEY":{"serverName":"acme","serverUrl":"https://mcp.figma.com/mcp","accessToken":"$DECOY_TOKEN","expiresAt":$exp}
}}
EOF
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 2 "$RC" "no entry carries both the requested name and url"
  assert_contains "$ERR" 'figma' "the message names the server"
  assert_contains "$ERR" 'https://mcp.figma.com/mcp' "and the url it matched against"
  assert_eq '' "$OUT" "nothing is printed, so no other server's token leaks"
}

test_the_store_keeps_the_original_key_and_drops_the_refresh_token() {
  local file="$TMP/verbatim.json" exp out
  exp="$(future_ms)"
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s","refreshToken":"%s","clientSecret":"%s","expiresAt":%s}}}\n' \
    "$FIGMA_KEY" "$FIGMA_TOKEN" "$REFRESH_TOKEN" "$CLIENT_SECRET" "$exp" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "the entry is found by its fields"
  out="$OUT"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-access-not-real-7c1d","expiresAt":'"$exp"'}}}' \
    "$out" "the key is copied verbatim and the extra fields are not"
  assert_not_contains "$out" "$REFRESH_TOKEN" "no refresh token"
  assert_not_contains "$out" "$CLIENT_SECRET" "no client secret"
}

test_several_requested_servers_share_the_store() {
  local file="$TMP/two-servers.json" exp
  exp="$(future_ms)"
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s","expiresAt":%s},"%s":{"serverName":"other","serverUrl":"https://other.example.test/mcp","accessToken":"%s","expiresAt":%s}}}\n' \
    "$FIGMA_KEY" "$FIGMA_TOKEN" "$exp" "$OTHER_KEY" "$OTHER_TOKEN" "$exp" > "$file"
  capture dg_oauth_minimal_json "$file" \
    "$(printf 'figma\thttps://mcp.figma.com/mcp')" \
    "$(printf 'other\thttps://other.example.test/mcp')"
  assert_eq 0 "$RC" "both pairs resolve"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-access-not-real-7c1d","expiresAt":'"$exp"'},"other|a1b2c3d4e5f60718":{"serverName":"other","serverUrl":"https://other.example.test/mcp","accessToken":"other-access-not-real-9a2b","expiresAt":'"$exp"'}}}' \
    "$OUT" "one entry per requested server, each under its own key"

  # One missing entry refuses the whole store: half a store would start the run
  # with a server that silently has no token.
  capture dg_oauth_minimal_json "$file" \
    "$(printf 'figma\thttps://mcp.figma.com/mcp')" \
    "$(printf 'other\thttps://other.example.test/nope')"
  assert_eq 2 "$RC" "a pair with no entry refuses the run"
  assert_eq '' "$OUT" "and no partial store is printed"
}

test_a_malformed_mcp_oauth_section_is_refused() {
  local file="$TMP/broken.json"
  # No comma between two members: the section cannot be walked, so nothing is
  # guessed from the part that did parse.
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s"} "%s":{"serverName":"other"}}}\n' \
    "$FIGMA_KEY" "$FIGMA_TOKEN" "$OTHER_KEY" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 2 "$RC" "a section that cannot be walked refuses the run"
  assert_contains "$ERR" 'mcpOAuth' "and the message says which section"
  assert_eq '' "$OUT" "printing nothing rather than guessing an entry"

  # A break inside an entry makes that entry unreadable, and an unreadable entry
  # is not a match: the run is refused for lack of an entry, never started with
  # a half-read one.
  printf '{"mcpOAuth":{"%s":{"serverName":"figma" "serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s"}}}\n' \
    "$FIGMA_KEY" "$FIGMA_TOKEN" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 2 "$RC" "an entry that does not parse is not taken as a match"
  assert_contains "$ERR" 'no tiene una entrada' "and the message is the missing-entry one"
  assert_eq '' "$OUT" "with no store printed"
}

test_the_entry_with_the_latest_expiry_wins() {
  local file="$TMP/two.json" exp_old exp_new out
  exp_old=$(( ($(date +%s) + 3600) * 1000 ))
  exp_new=$(( ($(date +%s) + 7200) * 1000 ))
  local first="\"$FIGMA_OLD_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_OLD_TOKEN\",\"expiresAt\":$exp_old}"
  local second="\"$FIGMA_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_TOKEN\",\"expiresAt\":$exp_new}"

  printf '{"mcpOAuth":{%s,%s}}\n' "$first" "$second" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "two entries match and one is chosen"
  out="$OUT"
  assert_contains "$out" "$FIGMA_TOKEN" "the token that expires later"
  assert_not_contains "$out" "$FIGMA_OLD_TOKEN" "not the one that expires sooner"

  # File order does not decide it: the same answer with the entries swapped.
  printf '{"mcpOAuth":{%s,%s}}\n' "$second" "$first" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "the swapped file parses too"
  assert_eq "$out" "$OUT" "and gives the same store, entry included"
}

test_an_entry_without_expires_at_is_taken_as_non_expiring() {
  local file="$TMP/noexp.json" exp out
  exp="$(( ($(date +%s) + 3600) * 1000 ))"
  # The real file has entries with no expiresAt and no clientId; a login in that
  # shape has no deadline to compare against, so it is used as it is.
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s","scope":"mcp:connect"}}}\n' \
    "$FIGMA_KEY" "$FIGMA_NOEXP_TOKEN" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "an entry with no expiry is accepted"
  out="$OUT"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-no-expiry-not-real-4b7e"}}}' \
    "$out" "kept without an expiresAt nobody wrote"
  assert_not_contains "$out" 'expiresAt' "no deadline is invented"
  assert_not_contains "$out" 'mcp:connect' "and the scope still stays behind"

  # It never expires, so it beats an entry that does.
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s","expiresAt":%s},"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s"}}}\n' \
    "$FIGMA_KEY" "$FIGMA_TOKEN" "$exp" "$FIGMA_OLD_KEY" "$FIGMA_NOEXP_TOKEN" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 0 "$RC" "a dated and a non-expiring entry both match"
  assert_contains "$OUT" "$FIGMA_NOEXP_TOKEN" "the non-expiring one is the one that never goes stale"
  assert_not_contains "$OUT" "$FIGMA_TOKEN" "not the dated one"
}

test_a_token_that_would_break_the_json_is_refused() {
  local file="$TMP/evil.json"
  printf '{"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"a\\"b","expiresAt":%s}}}\n' \
    "$FIGMA_KEY" "$(future_ms)" > "$file"
  capture dg_oauth_minimal_json "$file" "$(printf 'figma\thttps://mcp.figma.com/mcp')"
  assert_eq 2 "$RC" "a token with a quote in it is refused"
  assert_contains "$ERR" 'accessToken' "and the message names the field"
  assert_eq '' "$OUT" "nothing that could be pasted into JSON is printed"
}

# ── the minimal store ───────────────────────────────────────────────────────

test_the_store_has_only_the_requested_entry() {
  local exp
  exp="$(future_ms)"
  setup
  write_creds "$exp"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the sandboxed run succeeds"

  assert_eq 'yes' "$( [[ -f "$TMP/store.copy" ]] && echo yes || echo no )" \
    "a credentials file was mounted"
  # The entry under the key the real file used, trimmed to what the run needs.
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-access-not-real-7c1d","expiresAt":'"$exp"'}}}' \
    "$(cat "$TMP/store.copy")" \
    "the store is that one entry, under Claude Code's own key, and nothing else"
  # Every other secret in the real-shaped file stays out.
  assert_not_contains "$(cat "$TMP/store.copy")" "$DECOY_TOKEN" \
    "not the token of an entry the old key format would have matched"
  assert_not_contains "$(cat "$TMP/store.copy")" "$OTHER_TOKEN" "not another server's token"
  assert_not_contains "$(cat "$TMP/store.copy")" "$CLAUDE_TOKEN" "not the Anthropic login"
  assert_not_contains "$(cat "$TMP/store.copy")" 'claudeAiOauth' "not that block either"
  assert_not_contains "$(cat "$TMP/store.copy")" "$REFRESH_TOKEN" "and no refresh token"
  assert_not_contains "$(cat "$TMP/store.copy")" "$CLIENT_SECRET" "no client secret"
  assert_not_contains "$(cat "$TMP/store.copy")" 'mcp:connect' \
    "the scope is optional, not a reason to carry the rest of the entry"
  assert_not_contains "$(cat "$TMP/store.copy")" 'discoveryState' "nor the discovery state"
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

test_a_login_without_expiry_reaches_the_store() {
  setup
  # The real file can hold entries with no expiresAt: such a run has to start,
  # not be refused for lack of a deadline, and the store has to say the same.
  printf '{"claudeAiOauth":{"accessToken":"%s"},"mcpOAuth":{"%s":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"%s","clientId":"figma-client-not-real"}}}\n' \
    "$CLAUDE_TOKEN" "$FIGMA_KEY" "$FIGMA_NOEXP_TOKEN" \
    > "$TMP/home/.claude/.credentials.json"
  chmod 600 "$TMP/home/.claude/.credentials.json"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the sandboxed run starts"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-no-expiry-not-real-4b7e"}}}' \
    "$(cat "$TMP/store.copy")" "the store keeps the entry without a deadline"
  assert_not_contains "$(cat "$TMP/store.copy")" "$CLAUDE_TOKEN" "and still not the Anthropic login"
}

test_the_store_takes_the_most_recent_of_two_matching_entries() {
  local exp_old=$(( ($(date +%s) + 3600) * 1000 ))
  local exp_new=$(( ($(date +%s) + 7200) * 1000 ))
  setup
  printf '{"mcpOAuth":{%s,%s}}\n' \
    "\"$FIGMA_OLD_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_OLD_TOKEN\",\"expiresAt\":$exp_old}" \
    "\"$FIGMA_KEY\":{\"serverName\":\"figma\",\"serverUrl\":\"https://mcp.figma.com/mcp\",\"accessToken\":\"$FIGMA_TOKEN\",\"expiresAt\":$exp_new}" \
    > "$TMP/home/.claude/.credentials.json"
  chmod 600 "$TMP/home/.claude/.credentials.json"
  run_delegate --sandbox bwrap --mcp figma --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "the sandboxed run succeeds"
  assert_eq '{"mcpOAuth":{"figma|d39d3b6252bc1ac5":{"serverName":"figma","serverUrl":"https://mcp.figma.com/mcp","accessToken":"figma-access-not-real-7c1d","expiresAt":'"$exp_new"'}}}' \
    "$(cat "$TMP/store.copy")" "the entry that expires later, under its own key"
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
  assert_contains "$ERR" 'https://mcp.figma.com/mcp' "and the url it matched against"
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
  assert_contains "$probe" '"figma|d39d3b6252bc1ac5"' "under Claude Code's own key"
  assert_not_contains "$probe" "$DECOY_TOKEN" "not the token keyed like the old form"
  assert_not_contains "$probe" "$OTHER_TOKEN" "not another server's token"
  assert_not_contains "$probe" "$CLAUDE_TOKEN" "not the Anthropic login"
  assert_not_contains "$probe" "$REFRESH_TOKEN" "and no refresh token"
  assert_not_contains "$probe" "$CLIENT_SECRET" "no client secret"
  assert_contains "$probe" 'LISTING:.credentials.json,' "~/.claude holds that one file"
  assert_contains "$probe" 'CANARY:hidden' "a file next to it in the real ~/.claude is not visible"
  assert_not_contains "$probe" "$CANARY" "and its content never crosses"
}

run_tests
