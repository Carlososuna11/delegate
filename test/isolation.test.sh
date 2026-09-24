#!/usr/bin/env bash
# test/isolation.test.sh — the six invariants of CLAUDE.md, tested end to end.
#
# Every case runs the real bin/delegate with a fake `claude` first in PATH. The
# fake records its arguments, its environment, its own /proc cmdline and the
# command line of its parent, then exits 0 without calling a model.
#
# Nothing here writes outside $TMP: PATH, the providers dir, the key dir, the MCP
# entry dir and XDG_CACHE_HOME (where the blocked-CLI stubs land) all point into
# the scratch directory, and the only key in the whole suite is the sentinel
# below, written by the fixture.
#
#   invariant 1  the key never travels as an argument nor as shell text
#   invariant 2  the key is read once, after every other validation
#   invariant 3  the child runs bare: no user settings; no MCP server it was not
#                asked for by name; of a server asked for, only the tools its
#                entry lists; and none of the variables the run itself defines
#   invariant 4  permissions only widen, and they reach the child
#   invariant 5  codex, claude, delegate and dg are unreachable from the child
#   invariant 6  a provider file, and an MCP entry, are parsed, never sourced
source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"

DELEGATE_BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/delegate"

# The only key in this suite, and not a key anybody could use. Long and unique so
# searching for it cannot match by accident.
SENTINEL='SENTINEL-6f3a91c2-not-a-real-key'
SENTINEL_B='SENTINEL-b7d40e15-also-not-a-real-key'

# ─── fixture ─────────────────────────────────────────────────────────────────

write_fake_claude() {
  # The fake harness. bin/delegate resolves `claude` before it rewrites PATH, so
  # this file is what a delegated run execs. It only writes down what it got.
  cat >"$TMP/bin/claude" <<'FAKE'
#!/usr/bin/env bash
set -u
dump="${DELEGATE_TEST_DUMP:-}"
mode="${DELEGATE_TEST_MODE:-record}"
sleep_for="${DELEGATE_TEST_SLEEP:-0}"

if [[ -n "$dump" ]]; then
  mkdir -p -- "$dump" 2>/dev/null || true
  # The child is alive from here on. The account-swap test waits for this marker
  # instead of guessing how long the CLI takes to start.
  : >"$dump/started"
fi

sleep "$sleep_for"

if [[ -n "$dump" ]]; then
  # The arguments as received, in both shapes: one per line to read, and
  # NUL-separated for the raw vector.
  printf '%s\n' "$@" >"$dump/args"
  printf '%s\0' "$@" >"$dump/argv"
  env >"$dump/env"
  # /proc/$$/cmdline is what any other user of the machine can read. By explicit
  # pid, never /proc/self: a redirection is opened by the forked child, so
  # /proc/self there would name this `tr` and report its own argv.
  tr '\0' '\n' <"/proc/$$/cmdline" >"$dump/cmdline" 2>/dev/null || true
  # The direct parent is `timeout`. Nothing above it can be changed by the tool
  # (it only execs downwards), so the parent is where an argument placed one
  # level up would still be visible in `ps`.
  cat "/proc/$PPID/comm" >"$dump/parent_comm" 2>/dev/null || true
  tr '\0' '\n' <"/proc/$PPID/cmdline" >"$dump/parent_cmdline" 2>/dev/null || true
fi

if [[ "$mode" == probe-tools ]]; then
  # The child tries to reach another AI CLI by PATH; all four must be blocked.
  : >"$dump/blocked"
  for tool in codex claude delegate dg; do
    rc=0
    "$tool" >/dev/null 2>&1 || rc=$?
    printf '%s %s\n' "$tool" "$rc" >>"$dump/blocked"
  done
fi

exit 0
FAKE
  chmod +x "$TMP/bin/claude"
}

write_provider() {
  # <id> <caps>. A complete, valid provider file pointing at the fixture's key
  # dir, with the ceiling the caller asks for.
  local id="$1" caps="$2"
  {
    printf 'endpoint\thttps://api.example.invalid/v1\n'
    printf 'auth_env\tANTHROPIC_API_KEY\n'
    printf 'key_dir\t%s\n' "$TMP/keys"
    printf 'model\ttest-model-1\n'
    printf 'small_model\ttest-model-small\n'
    printf 'timeout\t30\n'
    [[ -n "$caps" ]] && printf 'caps\t%s\n' "$caps"
    printf 'residency\tDatos de prueba (local)\n'
  } >"$TMP/providers/$id"
}

provider_with() {
  # <id> <key> <value>: the standard file with one key overridden or added. Used
  # by the injection cases, which replace a single well-formed line.
  local id="$1" overridden="$2" value="$3"
  {
    [[ "$overridden" == endpoint ]]  || printf 'endpoint\thttps://api.example.invalid/v1\n'
    [[ "$overridden" == auth_env ]]  || printf 'auth_env\tANTHROPIC_API_KEY\n'
    [[ "$overridden" == key_dir ]]   || printf 'key_dir\t%s\n' "$TMP/keys"
    [[ "$overridden" == model ]]     || printf 'model\ttest-model-1\n'
    [[ "$overridden" == timeout ]]   || printf 'timeout\t30\n'
    [[ "$overridden" == caps ]]      || printf 'caps\tread,edit,bash\n'
    printf '%s\t%s\n' "$overridden" "$value"
  } >"$TMP/providers/$id"
}

mcp_entry() {
  # <id> <tools_read> [tools_write] [env_passthrough]: the standard shape of an
  # MCP entry, written into the fixture's entry directory. An http entry so the
  # JSON it produces is a single fixed string a test can compare against.
  local id="$1" t_read="$2" t_write="${3-}" env_pass="${4-}"
  {
    printf 'transport\thttp\n'
    printf 'url\thttps://mcp.example.invalid/%s\n' "$id"
    printf 'tools_read\t%s\n' "$t_read"
    [[ -n "$t_write" ]] && printf 'tools_write\t%s\n' "$t_write"
    [[ -n "$env_pass" ]] && printf 'env_passthrough\t%s\n' "$env_pass"
    printf 'description\tServidor de prueba %s\n' "$id"
  } >"$TMP/mcp.d/$id"
}

raw_mcp_entry() {
  # <id> <line>...: writes the lines verbatim, so a test can hand the parser a
  # file it is expected to reject. A standard entry is `mcp_entry`; this one
  # exists for the cases where the whole point is a malformed line.
  local id="$1"; shift
  printf '%s\n' "$@" >"$TMP/mcp.d/$id"
}

add_key() {
  # <account> <value>. Always mode 600: the fixture is the valid case.
  printf '%s\n' "$2" >"$TMP/keys/api_key.$1"
  chmod 600 "$TMP/keys/api_key.$1"
}

activate_account() {
  # Points the active-account symlink at <account> the way the tool does: a
  # temporary symlink plus rename(2). Never `ln -sfn`, which unlinks the target
  # first and leaves a window with no symlink at all.
  ln -s -- "$TMP/keys/api_key.$1" "$TMP/keys/.active.$$"
  mv -T -- "$TMP/keys/.active.$$" "$TMP/keys/api_key"
}

setup() {
  mkdir -p "$TMP/bin" "$TMP/providers" "$TMP/keys" "$TMP/work" "$TMP/dump" "$TMP/mcp.d"
  DG_ENV=()
  write_fake_claude
  # The ceiling a provider declares, in the current vocabulary. `mcp-write`
  # implies `mcp`, so one provider covers both MCP modes.
  write_provider testprov 'read,edit,bash,mcp,mcp-write'
  write_provider readmcp 'read,mcp'
  write_provider readonly 'read'
  add_key testacct "$SENTINEL"
  activate_account testacct
}

# Extra `VAR=value` entries for the next delegated run, as an array so a test can
# add to the child's environment without exporting anything into the suite's own.
DG_ENV=()

dg_run() {
  # The real CLI, with the fake world on PATH. It runs in a subshell so PATH,
  # XDG_CACHE_HOME, DELEGATE_PROVIDERS_DIR and DELEGATE_MCP_DIR never leak into
  # other tests. DELEGATE_MCP_DIR is set unconditionally: without it the CLI
  # would read the real ~/.config/delegate/mcp.d, and this suite must never look
  # at the machine's own configuration.
  (
    export PATH="$TMP/bin:$PATH"
    export XDG_CACHE_HOME="$TMP/cache"
    export DELEGATE_PROVIDERS_DIR="$TMP/providers"
    export DELEGATE_MCP_DIR="$TMP/mcp.d"
    export DELEGATE_TEST_DUMP="$TMP/dump"
    local kv
    for kv in "${DG_ENV[@]+"${DG_ENV[@]}"}"; do export "$kv"; done
    bash "$DELEGATE_BIN" "$@"
  )
}

dg_ok() {
  # A run of the default provider with no permission flags, from a directory
  # inside $TMP so --add-dir is deterministic.
  dg_run testprov --dir "$TMP/work" "$@"
}

# ─── reading what the child got ──────────────────────────────────────────────

child_ran()      { [[ -s "$TMP/dump/args" ]]; }
child_args()     { cat "$TMP/dump/args"; }
child_env()      { cat "$TMP/dump/env"; }
child_cmdline()  { tr '\0' '\n' <"$TMP/dump/cmdline"; }
child_argv()     { tr '\0' '\n' <"$TMP/dump/argv"; }

arg_after() {
  # arg_after <file> <flag>: the single argument that follows <flag>.
  awk -v want="$2" 'seen { print; exit } $0 == want { seen = 1 }' "$1"
}

arg_count() {
  # Exact-line count: `--mcp-config` must not be matched inside another value.
  grep -c -x -F -- "$2" "$1" || true
}

env_value() {
  sed -n "s/^$1=//p" "$TMP/dump/env" | head -n 1
}

require_child_ran() {
  # Every test that reads the dump proves the child ran first. Without this a
  # crash inside the CLI would make each `assert_not_contains` pass against an
  # empty file, and the suite would be green for the wrong reason.
  if ! child_ran; then
    fail "the fake claude never ran: the CLI died before exec"
    printf '      stderr: %s\n' "$(head -n 3 "$TMP/err" 2>/dev/null)" >&2
    return 1
  fi
  return 0
}

clear_dump() {
  # Wipes the record of the previous run, so a test that runs the CLI twice can
  # still tell "this run never reached the child" from "an earlier run did".
  rm -f "$TMP/dump/args" "$TMP/dump/argv" "$TMP/dump/env" "$TMP/dump/cmdline" \
        "$TMP/dump/parent_comm" "$TMP/dump/parent_cmdline" "$TMP/dump/blocked" \
        "$TMP/dump/started"
}

wait_for() {
  # wait_for <path> [tries of 50 ms]
  local path="$1" tries="${2:-200}"
  while (( tries > 0 )); do
    [[ -e "$path" ]] && return 0
    sleep 0.05
    tries=$((tries - 1))
  done
  return 1
}

# ─── the run has to start ────────────────────────────────────────────────────

test_the_cli_starts_and_reaches_the_child() {
  # Not one of the six invariants. It exists so that "the CLI cannot even start"
  # has a single, readable name instead of showing up as twenty red assertions
  # with no visible common cause.
  setup
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "a delegated run exits 0"
  assert_eq "" "$ERR" "and says nothing on stderr"
  require_child_ran || return 0
  assert_contains "$(child_args)" "-p" "the harness really was invoked"
}

# ─── invariant 1: the key never travels as an argument ───────────────────────

test_key_is_not_in_the_arguments_the_child_received() {
  setup
  capture dg_ok "lista los error paths"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  local argv cmdline
  argv="$(child_argv)"
  cmdline="$(child_cmdline)"

  # Positive controls first: if these files were empty or truncated, the
  # assertions below would pass while proving nothing.
  assert_contains "$argv" "lista los error paths" "the dump really holds the arguments"
  assert_contains "$cmdline" "--strict-mcp-config" "the cmdline really holds the exec vector"

  assert_not_contains "$argv" "$SENTINEL" "the key is not one of the arguments"
  assert_not_contains "$cmdline" "$SENTINEL" "and /proc does not expose it either"
  assert_not_contains "$(child_args)" "$SENTINEL" "nor in the line-per-argument view"
}

test_key_is_not_in_the_command_line_of_timeout() {
  setup
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  # delegate execs `env ... timeout ... claude ...`: an argument placed one level
  # up would still be visible in `ps` to every user of the machine. Nothing above
  # `timeout` is asserted on: the tool only execs downwards, so it cannot change
  # the command line of the shell that called it.
  assert_eq "timeout" "$(cat "$TMP/dump/parent_comm")" "the direct parent is timeout, as designed"
  local parent
  parent="$(cat "$TMP/dump/parent_cmdline")"
  assert_contains "$parent" "--strict-mcp-config" "the parent dump really holds arguments"
  assert_not_contains "$parent" "$SENTINEL" "the key is not in timeout's command line"
}

test_key_arrives_through_the_environment() {
  setup
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  # The other half of the invariant: the key IS delivered, in the variable the
  # provider names in auth_env. Without this the isolation would be trivial and
  # the tool would not work.
  assert_eq "$SENTINEL" "$(env_value ANTHROPIC_API_KEY)" "the key arrives in ANTHROPIC_API_KEY"
  assert_eq "https://api.example.invalid/v1" "$(env_value ANTHROPIC_BASE_URL)" "the endpoint travels with it"
  assert_eq "test-model-1" "$(env_value ANTHROPIC_MODEL)" "the provider's model"
  assert_eq "test-model-small" "$(env_value CLAUDE_CODE_SUBAGENT_MODEL)" "the small model"
  # One carrier only: the key must not be duplicated into some second variable.
  assert_eq 1 "$(grep -c -F -- "$SENTINEL" "$TMP/dump/env" || true)" \
    "the key appears exactly once in the child's environment"
}

# ─── invariant 2: the key is read once, after everything else ────────────────

test_a_permission_rejection_happens_before_the_key_is_read() {
  setup
  # The key is left in a mode the validator must refuse. If the ceiling were
  # enforced after reading the key, this run would fail with the key's error.
  chmod 644 "$TMP/keys/api_key.testacct"

  capture dg_run readonly --dir "$TMP/work" --bash "tarea"
  assert_eq 2 "$RC" "--bash above the ceiling exits 2"
  assert_contains "$ERR" "el proveedor no permite bash" "the message names the missing cap"
  assert_not_contains "$ERR" "mode 600" "it never got as far as the key"
  assert_fails "no child was launched" child_ran

  capture dg_run readonly --dir "$TMP/work" --edit "tarea"
  assert_eq 2 "$RC" "--edit above the ceiling exits 2"
  assert_not_contains "$ERR" "mode 600" "same ordering for --edit"
  assert_fails "no child was launched" child_ran

  # MCP joins that ordering, and in two shapes: a server the entry directory
  # does not declare, and one it does but the provider's ceiling forbids. Both
  # must be refused before the key is looked at, so the key's error never wins.
  capture dg_run testprov --dir "$TMP/work" --mcp nosuchserver "tarea"
  assert_eq 2 "$RC" "an undeclared server exits 2"
  assert_contains "$ERR" "delegate: mcp nosuchserver:" "the message comes from the entry resolver"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"
  assert_fails "no child was launched" child_ran

  mcp_entry srvone read_one
  capture dg_run readonly --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 2 "$RC" "--mcp above the ceiling exits 2"
  assert_contains "$ERR" "el proveedor no permite mcp" "the message names the missing cap"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"
  assert_fails "no child was launched" child_ran

  # A server the entry allows writing through, so the ceiling is the only thing
  # that can refuse this one.
  mcp_entry srvwrite read_one write_one
  capture dg_run readmcp --dir "$TMP/work" --mcp-write srvwrite "tarea"
  assert_eq 2 "$RC" "--mcp-write above the ceiling exits 2"
  assert_contains "$ERR" "el proveedor no permite mcp-write" "the ceiling is checked per capability"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"
  assert_fails "no child was launched" child_ran
}

test_argument_and_config_errors_happen_before_the_key_is_read() {
  setup
  chmod 644 "$TMP/keys/api_key.testacct"   # would fail, but must never be reached

  capture dg_ok --timeout abc "tarea"
  assert_eq 1 "$RC" "a --timeout that is not a number is refused"
  assert_contains "$ERR" "--timeout" "the message is about the argument"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"

  capture dg_run testprov --dir "$TMP/work"
  assert_eq 1 "$RC" "a missing task is refused"
  assert_contains "$ERR" "falta la tarea" "the message is about the task"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"

  capture dg_run testprov --dir "$TMP/nope" "tarea"
  assert_eq 1 "$RC" "a missing working directory is refused"
  assert_contains "$ERR" "no existe el directorio" "the message is about the directory"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"

  # And a provider file the parser rejects comes before the key too.
  printf 'endpoint\thttps://api.example.invalid/v1\nauth_env\tANTHROPIC_API_KEY\nkey_dir\t%s\nmodel\ttest-model-1\nunknown_key\tx\n' \
    "$TMP/keys" >"$TMP/providers/badcfg"
  capture dg_run badcfg --dir "$TMP/work" "tarea"
  assert_eq 2 "$RC" "a config the parser rejects exits 2"
  assert_contains "$ERR" "delegate: badcfg:" "the message is the parser's"
  assert_contains "$ERR" "unknown key" "it says what is wrong"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"
  assert_fails "no child was launched" child_ran

  # Same ordering for an MCP entry the parser rejects: it is resolved before the
  # permissions and long before any key.
  raw_mcp_entry srvone $'transport\thttp' $'url\thttps://mcp.example.invalid/mcp' \
    $'tools_read\tread_one' $'unknown_key\tx'
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 2 "$RC" "an entry the parser rejects exits 2"
  assert_contains "$ERR" "delegate: mcp srvone:" "the message is the entry parser's"
  assert_contains "$ERR" "unknown key" "it says what is wrong"
  assert_not_contains "$ERR" "mode 600" "the key was never validated"
  assert_fails "no child was launched" child_ran

  # `caps` is a closed vocabulary, and the one the tool used to hard-code is
  # gone from it: `figma` and `figma-write` are now MCP entries, reached by name
  # like any other. A file still written with them has to be refused rather than
  # read as a narrower ceiling, which would reach the child with fewer abilities
  # than the file declares and look like a provider that simply cannot do it.
  local retired
  for retired in 'read,edit,bash,figma' 'read,edit,bash,figma-write'; do
    provider_with figcaps caps "$retired"
    capture dg_run figcaps --dir "$TMP/work" "tarea"
    assert_eq 2 "$RC" "the retired caps vocabulary is refused: $retired"
    assert_contains "$ERR" "delegate: figcaps:" "the parser owns that error: $retired"
    assert_contains "$ERR" "unknown caps element" "and says which element: $retired"
    assert_not_contains "$ERR" "mode 600" "the key was never validated: $retired"
    assert_fails "no child was launched: $retired" child_ran
  done
}

test_a_key_that_is_not_600_is_refused_when_the_permissions_are_fine() {
  setup
  chmod 644 "$TMP/keys/api_key.testacct"

  capture dg_ok "tarea"
  assert_eq 1 "$RC" "a key whose mode is not 600 is refused"
  assert_contains "$ERR" "mode 600" "the message is the key's"
  assert_not_contains "$ERR" "no permite" "and not a permission problem"
  assert_not_contains "$ERR" "$SENTINEL" "the message never echoes the key"
  assert_fails "no child was launched" child_ran
}

test_swapping_the_active_account_mid_run_does_not_affect_the_run() {
  setup
  add_key other "$SENTINEL_B"

  DG_ENV=(DELEGATE_TEST_SLEEP=1)
  dg_run testprov --dir "$TMP/work" "tarea" >"$TMP/out" 2>"$TMP/err" &
  local runner=$! rc=0

  if ! wait_for "$TMP/dump/started"; then
    kill "$runner" 2>/dev/null || true
    fail "the child never started"
    return 0
  fi

  # The run is in flight. Point the active account at the other key, which is
  # exactly what another session running `delegate account ... use` would do.
  activate_account other
  wait "$runner" || rc=$?

  assert_eq 0 "$rc" "the run in flight finishes fine"
  assert_contains "$(readlink -f "$TMP/keys/api_key")" "api_key.other" \
    "the swap really happened: not a no-op test"
  assert_eq "$SENTINEL" "$(env_value ANTHROPIC_API_KEY)" \
    "the run kept the key it had already read"
  assert_not_contains "$(child_env)" "$SENTINEL_B" "the new key never reached the run in flight"

  # Control: a run started after the swap does use the new account, so the
  # assertion above cannot pass just because the swap was ignored somewhere.
  DG_ENV=()
  capture dg_ok "otra tarea"
  assert_eq 0 "$RC" "the later run exits 0"
  require_child_ran || return 0
  assert_eq "$SENTINEL_B" "$(env_value ANTHROPIC_API_KEY)" "a later run picks up the new active account"
}

# ─── invariant 3: isolation ──────────────────────────────────────────────────

test_child_runs_bare_with_no_user_settings_and_no_mcp() {
  setup
  # A server exists in the entry directory and this run does not ask for it.
  # Declaring a server and reaching it are two different acts: existing is not
  # being enabled, so this is the fixture that gives the assertions below teeth.
  # Its passthrough names a variable this shell does NOT set: forwarding it would
  # have to say so on stderr, which is the observable an unrequested entry must
  # not produce.
  mcp_entry ghost read_ghost '' DELEGATE_TEST_GHOST_TOKEN
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  local args
  args="$(child_args)"
  assert_contains "$args" "--bare" "no CLAUDE.md, no memory, no user config"
  assert_contains "$args" "--strict-mcp-config" "only the MCP servers passed explicitly"
  assert_eq "project,local" "$(arg_after "$TMP/dump/args" --setting-sources)" "no user settings"
  assert_eq "dontAsk" "$(arg_after "$TMP/dump/args" --permission-mode)" "no interactive prompt to hang on"
  assert_eq "$TMP/work" "$(arg_after "$TMP/dump/args" --add-dir)" "the session is scoped to the requested dir"
  # With no MCP flag the child gets no --mcp-config argument at all, not an empty
  # one: under the strict flag the difference is between "no server" and "a
  # server whose config failed to parse".
  assert_eq 0 "$(arg_count "$TMP/dump/args" --mcp-config)" "no --mcp-config argument without an MCP flag"
  assert_not_contains "$args" "mcp__" "no MCP tool is allowed either"
  assert_not_contains "$args" "ghost" "a declared but unrequested server does not reach the child"
  assert_not_contains "$ERR" "no está definida en este shell" \
    "nor is its env_passthrough even looked at"
}

test_mcp_reaches_exactly_the_requested_servers() {
  setup
  mcp_entry srvone read_one write_one
  mcp_entry srvtwo read_two
  mcp_entry ghost read_ghost

  # ── one server ──
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  local args json other
  args="$(child_args)"
  assert_eq 1 "$(arg_count "$TMP/dump/args" --mcp-config)" "exactly one --mcp-config"
  assert_contains "$args" "--strict-mcp-config" "still strict: nothing else can be added"

  json="$(arg_after "$TMP/dump/args" --mcp-config)"
  # The whole argument, compared as one string: any extra server would show up
  # here, not only in the auxiliary counts below.
  assert_eq '{"mcpServers":{"srvone":{"type":"http","url":"https://mcp.example.invalid/srvone"}}}' "$json" \
    "the config is exactly the requested server"
  assert_eq 1 "$(printf '%s' "$json" | grep -o 'mcpServers' | wc -l)" "a single mcpServers key"
  for other in srvtwo ghost linear datadog sentry retool github slack notion airtable; do
    assert_not_contains "$json" "$other" "$other is not reachable"
  done
  assert_contains "$args" "mcp__srvone__read_one" "the requested server's read tool is allowed"
  assert_not_contains "$args" "mcp__srvone__write_one" "--mcp does not grant writing through it"

  # ── two servers, accumulated by repeating the flag ──
  clear_dump
  capture dg_run testprov --dir "$TMP/work" --mcp srvone --mcp srvtwo "tarea"
  assert_eq 0 "$RC" "the two-server run exits 0"
  require_child_ran || return 0

  args="$(child_args)"
  json="$(arg_after "$TMP/dump/args" --mcp-config)"
  assert_eq 1 "$(arg_count "$TMP/dump/args" --mcp-config)" "still exactly one --mcp-config"
  # The whole argument again, as one string. Counting server objects would still
  # pass with a third one swapped in for one of these, and `assert_contains` for
  # each id would pass with every entry in the directory added next to them.
  assert_eq '{"mcpServers":{"srvone":{"type":"http","url":"https://mcp.example.invalid/srvone"},"srvtwo":{"type":"http","url":"https://mcp.example.invalid/srvtwo"}}}' "$json" \
    "the config is exactly the two requested servers, in the order they were asked for"
  assert_eq 1 "$(printf '%s' "$json" | grep -o 'mcpServers' | wc -l)" "still a single mcpServers key"
  assert_contains "$args" "mcp__srvone__read_one" "the first server's tools are allowed"
  assert_contains "$args" "mcp__srvtwo__read_two" "and the second server's"
  assert_not_contains "$args" "mcp__ghost__read_ghost" "not the unrequested server's"
}

test_mcp_grants_only_the_tools_the_entry_lists() {
  setup
  # Five read tools and two write tools in the entry; one of the read tools is
  # named again in tools_write, to pin the "each tool once" behaviour.
  mcp_entry srvone read_one,read_two,read_three read_one,write_one

  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "the --mcp run exits 0"
  require_child_ran || return 0

  local allowed
  allowed="$(arg_after "$TMP/dump/args" --allowedTools)"
  assert_contains ",$allowed," ",mcp__srvone__read_one," "a read tool is allowed"
  assert_contains ",$allowed," ",mcp__srvone__read_two," "and the other one"
  assert_contains ",$allowed," ",mcp__srvone__read_three," "and the third"
  # --mcp is read access to that server, never to everything the server offers.
  assert_not_contains ",$allowed," ",mcp__srvone__write_one," "the entry's write tool is NOT allowed"
  assert_not_contains "$(child_args)" "mcp__srvone__write_one" "nor is it named anywhere in the arguments"
  assert_eq 1 "$(printf '%s' "$allowed" | grep -o 'mcp__srvone__read_one' | wc -l)" \
    "a tool named in both lists is allowed exactly once"

  # --mcp-write widens to exactly the entry's write tools, and no others.
  clear_dump
  capture dg_run testprov --dir "$TMP/work" --mcp-write srvone "tarea"
  assert_eq 0 "$RC" "the --mcp-write run exits 0"
  require_child_ran || return 0

  allowed="$(arg_after "$TMP/dump/args" --allowedTools)"
  assert_contains ",$allowed," ",mcp__srvone__read_one," "--mcp-write still reads through the server"
  assert_contains ",$allowed," ",mcp__srvone__read_three," "all of the entry's read tools are there"
  assert_contains ",$allowed," ",mcp__srvone__write_one," "--mcp-write grants the entry's write tool"
  # A tool the entry never mentions does not exist, whatever the server offers.
  assert_not_contains "$(child_args)" "mcp__srvone__delete_everything" "an undeclared tool is not granted"
}

test_mcp_env_passthrough_forwards_the_name_not_a_value_from_the_file() {
  setup
  mcp_entry srvone read_one '' DELEGATE_TEST_FORWARDED
  DG_ENV=(DELEGATE_TEST_FORWARDED=parent-value-3c1f)
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  # The entry carries the name. The value is whatever the parent shell has under
  # it, which is the only thing that can reach the child: a file cannot put a
  # value of its own into the child's environment.
  assert_eq "parent-value-3c1f" "$(env_value DELEGATE_TEST_FORWARDED)" \
    "the child gets the parent's value under the name the entry asked for"
  assert_eq 1 "$(grep -c '^DELEGATE_TEST_FORWARDED=' "$TMP/dump/env" || true)" \
    "and the variable appears exactly once"

  # A name the parent does not have is not an error, and forwards nothing.
  clear_dump
  DG_ENV=()
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "an undefined name is not a failure"
  assert_contains "$ERR" "no está definida en este shell" "but it is said out loud"
  require_child_ran || return 0
  assert_not_contains "$(child_env)" "DELEGATE_TEST_FORWARDED" "and nothing is invented for it"

  # A value smuggled into the entry file instead of a name is refused by the
  # charset, so it never becomes a variable in the child either.
  mcp_entry srvone read_one '' 'DELEGATE_TEST_SMUGGLED=injected-9f2c'
  clear_dump
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 2 "$RC" "a name carrying a value is refused"
  assert_contains "$ERR" "delegate: mcp srvone:" "by the entry parser"
  assert_not_contains "$ERR" "no está definida" "and not quietly read as a bare name"
  assert_fails "no child was launched, so the value reaches nothing" child_ran
}

test_env_passthrough_cannot_carry_a_variable_the_run_owns() {
  setup
  # Values never come from the entry, but the NAMES are a different matter: the
  # passthrough is handed to `env` after the run's own assignments, and `env`
  # lets the last one win. An entry naming ANTHROPIC_API_KEY would therefore
  # send this shell's key to the provider's endpoint — the very key `-u
  # ANTHROPIC_API_KEY` exists to keep out — and one naming ANTHROPIC_BASE_URL
  # would point the run at another endpoint with that key in hand. These names
  # belong to the run, so they are refused rather than reordered.
  DG_ENV=(
    ANTHROPIC_API_KEY=parent-api-key-must-lose
    CLAUDE_CODE_OAUTH_TOKEN=oauth-token-not-real-4b2e
  )
  # The key is left unreadable on purpose: refusing a name has to happen before
  # the key is even opened, or a misread entry would decide what the run reads.
  chmod 644 "$TMP/keys/api_key.testacct"

  local name
  for name in ANTHROPIC_API_KEY ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN; do
    mcp_entry srvone read_one '' "$name"
    clear_dump
    capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
    assert_eq 2 "$RC" "an entry asking to forward $name is refused"
    assert_contains "$ERR" "$name" "the refusal names the variable: $name"
    assert_not_contains "$ERR" "mode 600" "and comes before the key is read: $name"
    assert_fails "no child was launched, so nothing was forwarded: $name" child_ran
  done

  # Control, so a guard that refused every name at all could not pass as this
  # one: the same shape of entry, naming a variable the run does not define,
  # runs and forwards the parent's value.
  add_key testacct "$SENTINEL"   # the key back to mode 600
  DG_ENV=(DELEGATE_TEST_FORWARDED=parent-value-3c1f)
  mcp_entry srvone read_one '' DELEGATE_TEST_FORWARDED
  clear_dump
  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "a name the run does not define is still forwarded"
  require_child_ran || return 0
  assert_eq "parent-value-3c1f" "$(env_value DELEGATE_TEST_FORWARDED)" \
    "the child gets the parent's value, under the name the entry asked for"
}

test_no_oauth_token_or_parent_key_reaches_the_child() {
  setup
  # None of these may survive into the child: the delegated run authenticates
  # with the provider's key or with nothing.
  DG_ENV=(
    ANTHROPIC_AUTH_TOKEN=oauth-token-not-real-4b2e
    CLAUDE_CODE_OAUTH_TOKEN=oauth-token-not-real-9d7c
    ANTHROPIC_API_KEY=parent-api-key-must-lose
  )
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  local child
  child="$(child_env)"
  assert_not_contains "$child" "ANTHROPIC_AUTH_TOKEN" "the variable is gone, not merely emptied"
  assert_not_contains "$child" "CLAUDE_CODE_OAUTH_TOKEN" "the variable is gone, not merely emptied"
  assert_not_contains "$child" "oauth-token-not-real" "no OAuth token of the parent's"
  assert_not_contains "$child" "parent-api-key-must-lose" "an API key exported by the caller is discarded"
  assert_eq "$SENTINEL" "$(env_value ANTHROPIC_API_KEY)" "the provider's key is the one that travels"
}

# ─── invariant 4: permissions only widen, and they reach the child ───────────

test_the_floor_reaches_the_child_without_unrestricted_bash() {
  setup
  capture dg_ok "tarea"   # no permission flag at all
  assert_eq 0 "$RC" "the run exits 0"
  require_child_ran || return 0

  local allowed
  allowed="$(arg_after "$TMP/dump/args" --allowedTools)"
  assert_contains ",$allowed," ",Read," "Read is allowed"
  assert_contains "$allowed" "Bash(grep:*)" "search goes through the shell, restricted"
  # Delimited on purpose: --allowedTools always contains `Bash(grep:*)`, so a
  # plain search for `Bash` would pass while unrestricted shell was granted.
  assert_not_contains ",$allowed," ",Bash," "no unrestricted shell at the floor"
  assert_not_contains ",$allowed," ",Edit," "no writing at the floor"
  assert_not_contains ",$allowed," ",Write," "no writing at the floor"
  assert_not_contains "$allowed" "mcp__" "and no MCP tool at the floor either"
  assert_eq "Read,Bash" "$(arg_after "$TMP/dump/args" --tools)" "the floor tool list"
}

test_edit_and_bash_widen_the_allowed_tools_the_child_gets() {
  setup
  capture dg_run testprov --dir "$TMP/work" --edit "tarea"
  assert_eq 0 "$RC" "the --edit run exits 0"
  require_child_ran || return 0
  local allowed
  allowed="$(arg_after "$TMP/dump/args" --allowedTools)"
  assert_contains ",$allowed," ",Edit," "--edit reaches the child in allowedTools"
  assert_contains ",$allowed," ",Write," "--edit reaches the child in allowedTools"
  assert_not_contains ",$allowed," ",Bash," "--edit does not hand over unrestricted shell"
  assert_eq "Read,Bash,Edit,Write" "$(arg_after "$TMP/dump/args" --tools)" "--edit widens the tool list"

  # Second run, so the assertions do not depend on the flags accumulating.
  capture dg_run testprov --dir "$TMP/work" --bash "tarea"
  assert_eq 0 "$RC" "the --bash run exits 0"
  require_child_ran || return 0
  allowed="$(arg_after "$TMP/dump/args" --allowedTools)"
  assert_contains ",$allowed," ",Bash," "--bash reaches the child in allowedTools"
  assert_not_contains ",$allowed," ",Edit," "no edit was asked for here"
}

# ─── invariant 5: other AI CLIs are blocked in the child's PATH ──────────────

test_codex_claude_delegate_and_dg_are_blocked_from_the_child() {
  setup
  DG_ENV=(DELEGATE_TEST_MODE=probe-tools)
  capture dg_ok "tarea"
  assert_eq 0 "$RC" "the delegated run itself succeeds"
  require_child_ran || return 0

  local results
  results="$(cat "$TMP/dump/blocked")"
  assert_eq 4 "$(printf '%s\n' "$results" | grep -c .)" "all four were probed"
  local tool
  for tool in codex claude delegate dg; do
    assert_eq "$tool 126" "$(grep "^$tool " "$TMP/dump/blocked")" "$tool exits 126 in the child"
  done
}

# ─── invariant 6: a provider file is parsed, never sourced ───────────────────

test_shell_payloads_in_a_provider_file_never_execute() {
  setup
  local victim="$TMP/pwned"
  local case key value

  for case in \
    'model|$(touch PAYLOAD)' \
    'model|`touch PAYLOAD`' \
    'small_model|$(touch PAYLOAD)' \
    'endpoint|https://api.example.invalid/;touch PAYLOAD' \
    'endpoint|https://api.example.invalid/v1 && touch PAYLOAD' \
    'key_dir|$TMP/keys; touch PAYLOAD' \
    'key_pattern|api_key.%s; touch PAYLOAD' \
    'timeout|30; touch PAYLOAD' \
    'auth_env|$(touch PAYLOAD)' \
    'residency|nota $(touch PAYLOAD)' \
    'evil$(touch PAYLOAD)|x'; do
    key="${case%%|*}"
    value="${case#*|}"
    value="${value//PAYLOAD/$victim}"
    provider_with inject "$key" "$value"

    capture dg_run inject --dir "$TMP/work" "tarea"
    # Either the parser rejects the value or the value travels as inert data.
    # What is asserted here is that nothing ran: neither branch may execute it.
    assert_not_contains "$OUT" "$SENTINEL" "no key is read for a rejected file: $key"
    assert_fails "nothing was executed by the payload in: $key" test -e "$victim"
    assert_fails "no child was launched for: $key" child_ran
    if (( RC == 0 )); then
      fail "the payload [$key] was accepted as config: $value"
    else
      assert_contains "$ERR" "delegate: inject:" "rejected as config, not as something else: $key"
    fi
  done
}

test_values_with_shell_metacharacters_cross_the_boundary_as_data() {
  setup
  # Both values are legal for the parser and would be a glob, a syntax error or
  # a command substitution if the file were sourced or eval'd instead.
  {
    printf 'endpoint\thttps://api.example.invalid/v1\n'
    printf 'auth_env\tANTHROPIC_API_KEY\n'
    printf 'key_dir\t%s\n' "$TMP/keys"
    printf 'model\ttest-model[1m]\n'
    printf 'timeout\t30\n'
    printf 'residency\tDatos (VE) / nota - x\n'
  } >"$TMP/providers/inert"

  capture dg_run providers
  assert_eq 0 "$RC" "the provider list parses"
  assert_contains "$OUT" "test-model[1m]" "the model id survives verbatim"
  assert_contains "$OUT" "Datos (VE) / nota - x" "the residency note survives verbatim"

  capture dg_run inert --dir "$TMP/work" "tarea"
  assert_eq 0 "$RC" "a legal file with metacharacters runs"
  require_child_ran || return 0
  assert_eq "test-model[1m]" "$(env_value ANTHROPIC_MODEL)" "the brackets reach the child as data"
  assert_fails "nothing was executed" test -e "$TMP/pwned"
}

test_an_unreadable_provider_file_is_a_config_error_not_a_shell_error() {
  setup
  # 000: the parser must report that it cannot read the file. If the file were
  # sourced instead of parsed, the shell would run it anyway.
  printf 'endpoint\thttps://api.example.invalid/v1\n' >"$TMP/providers/unreadable"
  printf 'touch %s/pwned\n' "$TMP" >>"$TMP/providers/unreadable"
  chmod 000 "$TMP/providers/unreadable"

  # Root reads anything, so the premise only holds for an unprivileged user.
  if [[ "$(id -u)" == 0 ]]; then
    pass_detail "running as root: an unreadable file cannot be set up, skipped"
    return 0
  fi

  capture dg_run unreadable --dir "$TMP/work" "tarea"
  assert_ne 0 "$RC" "the run fails"
  assert_contains "$ERR" "delegate: unreadable:" "the parser owns the error"
  assert_contains "$ERR" "cannot read provider file" "and says why"
  assert_fails "nothing was executed" test -e "$TMP/pwned"
  assert_fails "no child was launched" child_ran
}

test_shell_payloads_in_an_mcp_entry_never_execute() {
  setup
  local victim="$TMP/pwned"
  local case spec transport key value lines=()

  # An MCP entry is the other file a delegated run reads before it execs, so the
  # same rule applies: parsed, never sourced. Each case is a complete, valid
  # entry except for the one key carrying the payload, so a rejection below is
  # the payload's doing and not a missing line's.
  for case in \
    'http:url|https://mcp.example.invalid/mcp;touch PAYLOAD' \
    'http:url|https://mcp.example.invalid/$(touch PAYLOAD)' \
    'http:tools_read|read$(touch PAYLOAD)' \
    'http:tools_write|write`touch PAYLOAD`' \
    'http:env_passthrough|$(touch PAYLOAD)' \
    'http:description|nota $(touch PAYLOAD)' \
    'stdio:command|$(touch PAYLOAD)' \
    'stdio:command|/bin/sh -c "touch PAYLOAD"' \
    'stdio:args|-y;touch PAYLOAD' \
    'stdio:trust|local-exec;touch PAYLOAD'; do
    spec="${case%%|*}"
    transport="${spec%%:*}"
    key="${spec#*:}"
    value="${case#*|}"
    value="${value//PAYLOAD/$victim}"

    lines=($'transport\t'"$transport")
    if [[ "$transport" == http ]]; then
      [[ "$key" == url ]] || lines+=($'url\thttps://mcp.example.invalid/mcp')
    else
      [[ "$key" == command ]] || lines+=($'command\tnpx')
      [[ "$key" == trust ]]   || lines+=($'trust\tlocal-exec')
    fi
    [[ "$key" == tools_read ]] || lines+=($'tools_read\tread_thing')
    lines+=("$key"$'\t'"$value")
    raw_mcp_entry srvone "${lines[@]}"

    clear_dump
    capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
    # Either the charset rejects the value or the value travels as inert data.
    # What is asserted is that nothing ran: neither branch may execute it.
    assert_fails "nothing was executed by the payload in: $spec" test -e "$victim"
    assert_fails "no child was launched for: $spec" child_ran
    if (( RC == 0 )); then
      fail "the payload [$spec] was accepted as an entry: $value"
    else
      assert_contains "$ERR" "delegate: mcp srvone:" "rejected as an entry, not as something else: $spec"
    fi
  done
}

test_an_mcp_entry_with_legal_metacharacters_crosses_as_data() {
  setup
  # All of these are legal for the entry grammar and would be a glob, a
  # substitution or a redirection if the file were sourced or eval'd instead.
  raw_mcp_entry srvone \
    $'transport\thttp' \
    $'url\thttps://mcp.example.invalid/a.b-c_d~e/' \
    $'tools_read\tread_thing' \
    $'description\tServidor (VE) / nota - x, y'

  capture dg_run testprov --dir "$TMP/work" --mcp srvone "tarea"
  assert_eq 0 "$RC" "a legal entry with metacharacters runs"
  require_child_ran || return 0

  local json
  json="$(arg_after "$TMP/dump/args" --mcp-config)"
  assert_eq '{"mcpServers":{"srvone":{"type":"http","url":"https://mcp.example.invalid/a.b-c_d~e/"}}}' "$json" \
    "the url reaches the child verbatim, as data inside the JSON"
  assert_fails "nothing was executed" test -e "$TMP/pwned"
}

test_an_unreadable_mcp_entry_is_a_config_error_not_a_shell_error() {
  setup
  # Same rule as the provider file: an entry that cannot be read is refused as
  # config. If it were sourced instead, the shell would run it anyway.
  printf 'transport\thttp\ntools_read\tread_thing\n' >"$TMP/mcp.d/unreadable"
  printf 'touch %s/pwned\n' "$TMP" >>"$TMP/mcp.d/unreadable"
  chmod 000 "$TMP/mcp.d/unreadable"

  if [[ "$(id -u)" == 0 ]]; then
    pass_detail "running as root: an unreadable file cannot be set up, skipped"
    return 0
  fi

  capture dg_run testprov --dir "$TMP/work" --mcp unreadable "tarea"
  assert_eq 2 "$RC" "the run is refused"
  assert_contains "$ERR" "delegate: mcp unreadable:" "the entry parser owns the error"
  assert_contains "$ERR" "cannot read server file" "and says why"
  assert_fails "nothing was executed" test -e "$TMP/pwned"
  assert_fails "no child was launched" child_ran
}

run_tests
