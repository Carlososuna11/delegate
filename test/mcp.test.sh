#!/usr/bin/env bash
# Tests for lib/mcp.sh: the server entry grammar, the key rules, the tool
# allowlist and the JSON built for the child. Every rejection rule in the header
# contract has a case here that checks the exit code, not only the message: a
# rule that starts accepting bad input while still writing a warning is exactly
# the bug worth catching.
#
# Everything is written inside $TMP, and DELEGATE_MCP_DIR points there. Nothing
# here reads or writes the real ~/.config/delegate.

source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/mcp.sh"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

# A complete http entry: the required keys only.
http_lines() {
  printf '%s\n' \
    $'transport\thttp' \
    $'url\thttps://mcp.example.com/mcp' \
    $'tools_read\tget_thing,list_things'
}

# A complete stdio entry: the required keys only.
stdio_lines() {
  printf '%s\n' \
    $'transport\tstdio' \
    $'command\tnpx' \
    $'trust\tlocal-exec' \
    $'tools_read\tget_thing,list_things'
}

# The entry from <generator> with one key replaced, appended when the generator
# does not set it.
write_entry_with() {
  local path="$1" gen="$2" key="$3" value="$4" line='' k=''
  : > "$path"
  while IFS= read -r line; do
    k="${line%%$'\t'*}"
    [[ "$k" == "$key" ]] && continue
    printf '%s\n' "$line" >> "$path"
  done < <("$gen")
  printf '%s\t%s\n' "$key" "$value" >> "$path"
}

# The entry from <generator> minus one key.
write_entry_without() {
  local path="$1" gen="$2" key="$3" line='' k=''
  : > "$path"
  while IFS= read -r line; do
    k="${line%%$'\t'*}"
    [[ "$k" == "$key" ]] && continue
    printf '%s\n' "$line" >> "$path"
  done < <("$gen")
}

# The entry from <generator> plus verbatim lines, for grammar cases.
write_entry_plus_raw() {
  local path="$1" gen="$2"; shift 2
  { "$gen"; printf '%s\n' "$@"; } > "$path"
}

# The value <file> gives <key>, read the way the parser reads it and with the
# same trailing-blank trim. Lets a test compute its expected string from a
# shipped entry instead of duplicating that entry's text here.
entry_value() {
  local file="$1" key="$2" line=''
  while IFS= read -r line; do
    [[ "$line" == "$key"$'\t'* ]] || continue
    line="${line#*$'\t'}"
    printf '%s' "${line%"${line##*[![:blank:]]}"}"
    return 0
  done < "$file"
  return 1
}

# A rejection from dg_mcp_parse: exit 2, nothing on stdout, the documented
# prefix on stderr. `-`, not `:-`: an empty id is one of the rejected cases.
assert_reject() {
  local path="$1" msg="$2" id="${3-srv}"
  capture dg_mcp_parse "$path" "$id"
  assert_eq 2 "$RC" "$msg: exit code"
  assert_eq '' "$OUT" "$msg: stdout stays empty"
  assert_contains "$ERR" "delegate: mcp $id: " "$msg: stderr carries the server id"
}

# The mirror of assert_reject_value: a charset narrowed to shut a hole has to
# be shown still accepting the shapes the contract promises, or the next reader
# cannot tell a fix from an over-correction.
assert_accept_value() {
  local key="$1" value="$2" msg="$3" gen="${4-http_lines}" path="$TMP/accept"
  write_entry_with "$path" "$gen" "$key" "$value"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "$msg: exit code"
  assert_contains "$OUT" "$value" "$msg: the value survives the parse"
}

# A rejection coming from one value rule, with the rest of the entry valid.
assert_reject_value() {
  local key="$1" value="$2" msg="$3" gen="${4-http_lines}" path="$TMP/value"
  write_entry_with "$path" "$gen" "$key" "$value"
  assert_reject "$path" "$msg"
}

# A rejection coming from one raw line added to an otherwise valid entry.
assert_reject_raw() {
  local line="$1" msg="$2" gen="${3-http_lines}" path="$TMP/raw"
  write_entry_plus_raw "$path" "$gen" "$line"
  assert_reject "$path" "$msg"
}

# A DG_MCP_* global read through this accessor instead of directly. `set -u` is
# on, so a regression that leaves the global unset would abort the whole suite at
# the first direct read, printing an unbound-variable error where the assertion
# message should be, and hiding every test that had not run yet.
dg_global() {
  printf '%s' "${!1-}"
}

# dg_mcp_resolve speaks through globals, so every call starts from a clean
# slate: a test that forgot this would pass by reading the previous test's
# answer, which is the one way these results can lie.
reset_globals() {
  unset DG_MCP_JSON DG_MCP_TOOLS DG_MCP_RULES
  DG_MCP_ENV=()
  DG_MCP_NEED=()
}

# A refusal must leave nothing behind. `''` when unset, so this reads as "no
# global was touched".
assert_globals_unset() {
  local msg="$1"
  assert_eq '' "${DG_MCP_JSON+set}" "$msg: DG_MCP_JSON is not set"
  assert_eq '' "${DG_MCP_TOOLS+set}" "$msg: DG_MCP_TOOLS is not set"
  assert_eq '' "${DG_MCP_RULES+set}" "$msg: DG_MCP_RULES is not set"
  assert_eq '' "${DG_MCP_ENV+set}" "$msg: DG_MCP_ENV is not set"
  assert_eq '' "${DG_MCP_NEED+set}" "$msg: DG_MCP_NEED is not set"
}

# ── dg_mcp_dir ──────────────────────────────────────────────────────────────

test_dir_defaults_under_config_delegate() {
  local saved="${DELEGATE_MCP_DIR-}" got
  unset DELEGATE_MCP_DIR
  capture dg_mcp_dir
  got="$OUT"
  [[ -n "$saved" ]] && DELEGATE_MCP_DIR="$saved"
  assert_eq 0 "$RC" "dg_mcp_dir succeeds"
  assert_eq "$HOME/.config/delegate/mcp.d" "$got" "the default lives under ~/.config/delegate"
}

test_dir_follows_delegate_mcp_dir() {
  local saved="${DELEGATE_MCP_DIR-}"
  DELEGATE_MCP_DIR="$TMP/mcp.d"
  capture dg_mcp_dir
  assert_eq 0 "$RC" "dg_mcp_dir succeeds"
  assert_eq "$TMP/mcp.d" "$OUT" "the environment override wins"

  # An empty override is a mistake, not a directory named ''.
  DELEGATE_MCP_DIR=''
  capture dg_mcp_dir
  assert_eq "$HOME/.config/delegate/mcp.d" "$OUT" "an empty override falls back to the default"

  unset DELEGATE_MCP_DIR
  if [[ -n "$saved" ]]; then DELEGATE_MCP_DIR="$saved"; fi
}

# ── dg_mcp_servers ──────────────────────────────────────────────────────────

test_lists_ids_sorted_and_skips_what_is_not_an_id() {
  local dir="$TMP/servers" expected
  mkdir -p "$dir"
  write_kv "$dir/bravo" transport http
  write_kv "$dir/alpha" transport http
  write_kv "$dir/charlie-2" transport http
  : > "$dir/Bad-Id"        # uppercase: not an id
  : > "$dir/with.dot"      # dot: not an id
  : > "$dir/..hidden"      # not matched by the glob either
  mkdir -p "$dir/delta"    # a directory is not an entry

  capture dg_mcp_servers "$dir"
  expected="$(printf '%s\n' alpha bravo charlie-2)"
  assert_eq 0 "$RC" "listing succeeds"
  assert_eq '' "$ERR" "no stderr while listing"
  assert_eq "$expected" "$OUT" "ids sorted, invalid names skipped"
}

test_missing_or_empty_directory_is_a_success_with_no_output() {
  mkdir -p "$TMP/empty"
  capture dg_mcp_servers "$TMP/empty"
  assert_eq 0 "$RC" "an empty directory is not an error"
  assert_eq '' "$OUT" "nothing listed from an empty directory"

  capture dg_mcp_servers "$TMP/does-not-exist"
  assert_eq 0 "$RC" "a missing directory is not an error"
  assert_eq '' "$OUT" "nothing listed from a missing directory"
  assert_eq '' "$ERR" "a missing directory is quiet"
}

test_listing_does_not_validate_contents() {
  local dir="$TMP/garbage"
  mkdir -p "$dir"
  printf 'this is not an entry at all\n' > "$dir/alpha"
  capture dg_mcp_servers "$dir"
  assert_eq 0 "$RC" "listing still succeeds"
  assert_eq 'alpha' "$OUT" "a broken entry is still listed"
}

# ── dg_mcp_parse: happy path ────────────────────────────────────────────────

test_parses_an_http_entry_in_sorted_order() {
  local path="$TMP/http" expected
  write_kv "$path" \
    tools_read 'get_thing,list_things' \
    url 'https://mcp.example.com/mcp' \
    transport http \
    description 'Example server (read only)' \
    tools_write 'set_thing' \
    env_passthrough 'HOME,EDITOR'
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "a complete http entry parses"
  assert_eq '' "$ERR" "no stderr on success"
  expected="$(printf '%s\n' \
    $'description\tExample server (read only)' \
    $'env_passthrough\tHOME,EDITOR' \
    $'tools_read\tget_thing,list_things' \
    $'tools_write\tset_thing' \
    $'transport\thttp' \
    $'url\thttps://mcp.example.com/mcp')"
  assert_eq "$expected" "$OUT" "one key<TAB>value line per key, sorted by key"
}

test_parses_a_stdio_entry_and_keeps_args_order() {
  local path="$TMP/stdio" expected
  write_kv "$path" \
    args '-y @upstash/context7-mcp' \
    command npx \
    trust local-exec \
    transport stdio \
    tools_read 'get_thing,list_things,put_thing'
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "a complete stdio entry parses"
  expected="$(printf '%s\n' \
    $'args\t-y @upstash/context7-mcp' \
    $'command\tnpx' \
    $'tools_read\tget_thing,list_things,put_thing' \
    $'transport\tstdio' \
    $'trust\tlocal-exec')"
  assert_eq "$expected" "$OUT" "the argv elements are kept as written, in order"
}

test_omits_keys_the_entry_does_not_set() {
  local path="$TMP/minimal" expected
  http_lines > "$path"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "the required keys are enough"
  expected="$(printf '%s\n' \
    $'tools_read\tget_thing,list_things' \
    $'transport\thttp' \
    $'url\thttps://mcp.example.com/mcp')"
  assert_eq "$expected" "$OUT" "keys the entry does not set are omitted"
}

test_ignores_comments_and_blank_lines() {
  local path="$TMP/comments" expected
  {
    printf '\n'
    printf '# a whole-line comment\n'
    printf '   # an indented comment with\ta tab in it\n'
    http_lines
    printf '\n'
    printf '   \t  \n'
    printf '# trailing comment\n'
  } > "$path"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "comments and blank lines are ignored"
  expected="$(printf '%s\n' \
    $'tools_read\tget_thing,list_things' \
    $'transport\thttp' \
    $'url\thttps://mcp.example.com/mcp')"
  assert_eq "$expected" "$OUT" "only the configured keys are printed"
}

test_trims_blanks_around_lines_and_values() {
  local path="$TMP/trim" expected
  {
    printf '  transport\thttp\n'
    printf '\turl\thttps://mcp.example.com/mcp   \n'
    printf '  tools_read\tget_thing,list_things  \n'
  } > "$path"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "padded lines parse"
  expected="$(printf '%s\n' \
    $'tools_read\tget_thing,list_things' \
    $'transport\thttp' \
    $'url\thttps://mcp.example.com/mcp')"
  assert_eq "$expected" "$OUT" "leading and trailing blanks are trimmed"
}

test_accepts_the_whole_url_shape() {
  local path="$TMP/urls" ok
  for ok in 'https://host' 'https://host:8080' 'https://host/v1/mcp' \
            'https://mcp.example.com/a.b-c_d~e/'; do
    write_entry_with "$path" http_lines url "$ok"
    capture dg_mcp_parse "$path" srv
    assert_eq 0 "$RC" "url '$ok' is accepted"
  done
}

# ── dg_mcp_parse: grammar ───────────────────────────────────────────────────

test_rejects_an_invalid_server_id() {
  local path="$TMP/id"
  http_lines > "$path"
  assert_reject "$path" "an uppercase id" 'Bad'
  assert_reject "$path" "an id with a dot" 'a.b'
  assert_reject "$path" "an over-long id" "$(printf 'a%032d' 0)"
}

test_rejects_a_missing_or_unreadable_file() {
  capture dg_mcp_parse "$TMP/nope" srv
  assert_eq 2 "$RC" "a missing file is rejected"
  assert_eq '' "$OUT" "nothing on stdout"
  assert_contains "$ERR" 'delegate: mcp srv: ' "the message carries the id"
}

test_rejects_a_line_with_no_tab() {
  assert_reject_raw 'transport http' 'a line with no tab'
}

test_rejects_a_line_with_more_than_one_tab() {
  assert_reject_raw $'transport\thttp\thttp' 'a line with two tabs'
}

test_rejects_an_empty_value() {
  assert_reject_raw $'trust\t' 'an empty value'
}

test_rejects_an_unknown_key() {
  assert_reject_raw $'capabilities\tread' 'a key outside the vocabulary'
}

test_rejects_a_duplicate_key() {
  assert_reject_raw $'transport\thttp' 'the same key twice'
}

test_rejects_a_missing_transport() {
  local path="$TMP/no-transport"
  write_entry_without "$path" http_lines transport
  assert_reject "$path" "no transport"
}

test_rejects_an_unknown_transport() {
  assert_reject_value transport websocket 'a transport outside the vocabulary'
}

# ── dg_mcp_parse: transport decides which keys belong ───────────────────────

test_rejects_a_stdio_entry_without_trust() {
  local path="$TMP/no-trust"
  write_entry_without "$path" stdio_lines trust
  assert_reject "$path" "stdio without trust"
}

test_rejects_a_trust_value_other_than_local_exec() {
  assert_reject_value trust yes 'trust that is not local-exec' stdio_lines
  assert_reject_value trust 'Local-Exec' 'trust in the wrong case' stdio_lines
  assert_reject_value trust 'local exec' 'trust with a space instead of a dash' stdio_lines
  # NOT tested as a rejection: `local-exec ` with a trailing blank. The grammar
  # trims blanks around the line before splitting, so that value legitimately
  # arrives as `local-exec`. Asserting a rejection there would pin a behaviour
  # the contract does not ask for.
}

test_rejects_trust_on_an_http_entry() {
  assert_reject_value trust local-exec 'trust on an http entry' http_lines
}

test_rejects_a_url_on_a_stdio_entry() {
  assert_reject_value url 'https://mcp.example.com/mcp' 'url on a stdio entry' stdio_lines
}

test_rejects_a_command_on_an_http_entry() {
  assert_reject_value command npx 'command on an http entry' http_lines
}

test_rejects_args_on_an_http_entry() {
  assert_reject_value args '-y something' 'args on an http entry' http_lines
}

test_rejects_a_stdio_entry_without_command() {
  local path="$TMP/no-command"
  write_entry_without "$path" stdio_lines command
  assert_reject "$path" "stdio without command"
}

# ── dg_mcp_parse: value rules ───────────────────────────────────────────────

test_rejects_a_url_that_is_not_https() {
  assert_reject_value url 'http://mcp.example.com/mcp' 'plain http'
  assert_reject_value url 'mcp.example.com/mcp' 'a url without a scheme'
}

test_rejects_a_tool_name_outside_its_charset() {
  assert_reject_value tools_read 'getThing' 'an uppercase tool name'
  assert_reject_value tools_read 'get.thing' 'a dotted tool name'
  assert_reject_value tools_read '1thing' 'a tool name starting with a digit'
  assert_reject_value tools_read 'get thing' 'a tool name with a space'
  # A hyphen is NOT in this list any more: real servers ship hyphenated names,
  # so the charset widened and test_accepts_hyphenated_tool_names pins that.
}

test_rejects_a_tool_list_with_an_empty_or_repeated_element() {
  assert_reject_value tools_read 'get_thing,' 'a trailing comma'
  assert_reject_value tools_read ',get_thing' 'a leading comma'
  assert_reject_value tools_read 'get_thing,,list_things' 'a doubled comma'
  assert_reject_value tools_read 'get_thing,get_thing' 'the same tool twice'
}

test_rejects_a_missing_tools_read() {
  local path="$TMP/no-tools"
  write_entry_without "$path" http_lines tools_read
  assert_reject "$path" "no readable tool at all"
}

test_rejects_a_command_that_climbs_out_of_its_directory() {
  assert_reject_value command '../../bin/sh' 'a command with ..' stdio_lines
  assert_reject_value command 'npx -y' 'a command with a space' stdio_lines
  assert_reject_value command '$HOME/bin/serve' 'a command with a variable' stdio_lines
}

test_rejects_a_relative_command_path() {
  # The contract has always said "absolute path, or a bare name on the child's
  # PATH", and the charset used to accept a third shape the contract never
  # promised. A relative path resolves against the CHILD's cwd, which is the
  # working directory under review, so a `bin/mcp-server` committed into a repo
  # handed to `delegate` would run as the delegated process — outside the tool
  # allowlist entirely.
  assert_reject_value command './serve' 'a command starting with ./' stdio_lines
  assert_reject_value command 'bin/serve' 'a one-segment relative path' stdio_lines
  assert_reject_value command 'node_modules/.bin/serve' 'a deeper relative path' stdio_lines
}

test_accepts_an_absolute_command_and_a_bare_name() {
  assert_accept_value command '/usr/bin/node' 'an absolute path' stdio_lines
  assert_accept_value command 'npx' 'a bare name' stdio_lines
  assert_accept_value command 'mcp-server.js' 'a bare name with a dot and a hyphen' stdio_lines
}

test_rejects_an_args_element_outside_its_charset() {
  assert_reject_value args '--flag="a b"' 'a quoted element' stdio_lines
  assert_reject_value args '$(id)' 'a command substitution' stdio_lines
  assert_reject_value args 'a;rm -rf /' 'a shell separator' stdio_lines
}

test_rejects_an_args_value_with_a_space_inside_an_element() {
  # A space is the separator, so an argument that needs one shows up as an empty
  # element. The reason has to say what the real limit is.
  assert_reject_value args '-y  thing' 'a doubled space' stdio_lines
  assert_reject_value args ' -y thing' 'a leading space' stdio_lines
  local path="$TMP/args-space"
  write_entry_with "$path" stdio_lines args '-y  thing'
  capture dg_mcp_parse "$path" srv
  assert_contains "$ERR" 'space' "the reason names the space, not just the charset"
}

test_rejects_an_env_name_outside_its_charset() {
  assert_reject_value env_passthrough 'home' 'a lowercase variable name'
  assert_reject_value env_passthrough '1TOKEN' 'a name starting with a digit'
  assert_reject_value env_passthrough 'HOME,editor' 'one bad name in the list'
}

test_accepts_a_repeated_env_name() {
  # Not an error: resolve deduplicates, and two entries naming the same variable
  # is the normal case.
  local path="$TMP/env-dup"
  write_entry_with "$path" http_lines env_passthrough 'HOME,HOME,EDITOR'
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "a repeated env name parses"
  assert_eq '' "$ERR" "and says nothing about it"
}

test_rejects_a_tools_write_name_outside_its_charset() {
  assert_reject_value tools_write 'set thing' 'a write tool with a space'
}

test_rejects_a_description_outside_its_charset_or_length() {
  assert_reject_value description 'Note: careful' 'a colon in the description'
  assert_reject_value description "$(printf 'd%0120d' 0)" 'a description over 120 characters'
}

test_accepts_a_description_at_the_length_limit() {
  local path="$TMP/desc" d120
  printf -v d120 '%0120d' 0
  write_entry_with "$path" http_lines description "$d120"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "120 characters is still a description"
}

# ── dg_mcp_parse: hyphenated tool names ─────────────────────────────────────

test_accepts_hyphenated_tool_names() {
  # context7's real tools are resolve-library-id and query-docs, taken from its
  # own tools/list. A charset without the hyphen would make those entries
  # unwritable rather than safer, so it was widened by exactly one character.
  local path="$TMP/hyphen" expected
  write_entry_with "$path" http_lines tools_read 'resolve-library-id,query-docs'
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "tool names with hyphens parse"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" $'tools_read\tresolve-library-id,query-docs' "the names come back as written"

  local dir="$TMP/hyphendir"
  mkdir -p "$dir"
  cp "$path" "$dir/ctx"
  reset_globals
  capture dg_mcp_resolve "$dir" ctx ''
  assert_eq 0 "$RC" "an entry with hyphenated tools resolves"
  expected='mcp__ctx__resolve-library-id,mcp__ctx__query-docs'
  assert_eq "$expected" "$(dg_global DG_MCP_TOOLS)" "the prefix goes around a hyphenated name unchanged"
}

test_still_rejects_a_tool_name_that_starts_with_a_hyphen() {
  # The widened charset moved one character, it did not drop the rule that a
  # name starts with a letter. A leading hyphen would read as a flag at the
  # boundary of mcp__<id>__<tool>.
  assert_reject_value tools_read '-get_thing' 'a tool name starting with a hyphen'
  assert_reject_value tools_read '-resolve-library-id' 'a hyphenated name starting with a hyphen'
}

test_still_rejects_an_uppercase_tool_name_after_the_widening() {
  assert_reject_value tools_read 'resolve-Library-Id' 'a hyphenated name with an uppercase letter'
}

# ── dg_mcp_parse: rules ─────────────────────────────────────────────────────

test_parses_an_entry_with_rules() {
  local path="$TMP/rules" text='lee el archivo antes de escribir'
  write_entry_with "$path" http_lines rules "$text"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "an entry with rules parses"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" $'rules\t'"$text" "the guidance comes back as written"
}

test_rejects_rules_outside_its_charset() {
  # rules is the only value that reaches the child as prose, so it is the only
  # one that needs accents and Spanish punctuation. It still excludes the quote,
  # the backslash and control characters: all three would be pasted next to the
  # JSON as one argv element, and an entry file is not a place from which a line
  # break gets into the system prompt.
  assert_reject_value rules 'no hagas "eso"' 'a double quote in rules'
  assert_reject_value rules 'C:\ruta\servidor' 'a backslash in rules'
  assert_reject_value rules $'antes\ndespues' 'a newline in rules is refused, as a grammar problem'
  assert_reject_value rules $'antes\001despues' 'a control character in rules'
  assert_reject_value rules 'cuesta $5' 'a dollar sign in rules'
  assert_reject_value rules 'lee <esto>' 'an angle bracket in rules'
}

test_rules_length_limit_is_600_characters() {
  local path="$TMP/rules-len" r600 r601
  printf -v r600 '%0600d' 0
  printf -v r601 '%0601d' 0
  write_entry_with "$path" http_lines rules "$r600"
  capture dg_mcp_parse "$path" srv
  assert_eq 0 "$RC" "600 characters is still rules"
  assert_eq '' "$ERR" "and it says nothing about the length"
  assert_reject_value rules "$r601" 'a rules of 601 characters'
}

test_a_rejected_rules_refuses_the_run_and_leaves_no_global() {
  local dir="$TMP/badrules"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  printf 'rules\tno hagas "eso"\n' >> "$dir/alpha"
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 2 "$RC" "an entry with a bad rules refuses the run"
  assert_contains "$ERR" 'delegate: mcp alpha: ' "the message names the server"
  assert_contains "$ERR" 'rules' "and the key responsible"
  assert_globals_unset "a bad rules"
}

# ── the premise under the JSON assembly ─────────────────────────────────────

test_a_value_that_could_close_the_json_string_is_rejected_by_charset() {
  # The JSON is assembled by concatenation with no escaping step, which is sound
  # only while no charset admits a quote, a backslash or a control character.
  # Each of these would break out of its string if it got through.
  local path="$TMP/inject" hostile
  for hostile in '"' '\' '"}' '","evil":{"type":"http","url":"https://evil' 'a"b'; do
    write_entry_with "$path" http_lines url "https://host/mcp$hostile"
    capture dg_mcp_parse "$path" srv
    assert_eq 2 "$RC" "a url containing $(printf '%q' "$hostile") is rejected"
    assert_eq '' "$OUT" "nothing reaches stdout to be pasted into JSON"

    write_entry_with "$path" http_lines tools_read "get_thing$hostile"
    capture dg_mcp_parse "$path" srv
    assert_eq 2 "$RC" "a tool containing $(printf '%q' "$hostile") is rejected"

    write_entry_with "$path" http_lines description "a$hostile"
    capture dg_mcp_parse "$path" srv
    assert_eq 2 "$RC" "a description containing $(printf '%q' "$hostile") is rejected"
  done
}

test_a_hostile_entry_cannot_reach_a_resolved_run() {
  local dir="$TMP/hostile" path="$TMP/hostile/evil"
  mkdir -p "$dir"
  write_kv "$path" \
    transport http \
    url 'https://host/mcp","evil":{"type":"http","url":"https://evil' \
    tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" evil ''
  assert_eq 2 "$RC" "the run is refused"
  assert_eq '' "$OUT" "nothing on stdout"
  assert_globals_unset "a hostile entry"
}

# ── dg_mcp_resolve ──────────────────────────────────────────────────────────

test_no_server_requested_grants_nothing() {
  local dir="$TMP/none"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://mcp.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" '' ''
  assert_eq 0 "$RC" "asking for nothing succeeds"
  assert_eq '' "$OUT" "resolve prints nothing"
  # ${VAR+set} yields `set` when VAR is set, empty when it is not. Asserting the
  # empty string here would assert the opposite of what this test is named for.
  assert_eq 'set' "${DG_MCP_JSON+set}" "DG_MCP_JSON is SET to empty, not unset"
  assert_eq '' "$(dg_global DG_MCP_JSON)" "and it is empty"
  assert_eq 'set' "${DG_MCP_TOOLS+set}" "DG_MCP_TOOLS is SET to empty, not unset"
  assert_eq '' "$(dg_global DG_MCP_TOOLS)" "and it is empty"
  assert_eq 'set' "${DG_MCP_RULES+set}" "DG_MCP_RULES is SET to empty, not unset"
  assert_eq '' "$(dg_global DG_MCP_RULES)" "and it is empty"
  assert_eq 0 "${#DG_MCP_ENV[@]}" "DG_MCP_ENV is an empty array"
  assert_eq 0 "${#DG_MCP_NEED[@]}" "DG_MCP_NEED is an empty array"
}

test_read_only_server_builds_the_json_and_the_allowlist() {
  local dir="$TMP/httpdir"
  mkdir -p "$dir"
  write_kv "$dir/alpha" \
    transport http \
    url 'https://mcp.example.com/mcp' \
    tools_read 'get_thing,list_things'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "a known read id resolves"
  assert_eq '' "$ERR" "no stderr on success"
  assert_eq '{"mcpServers":{"alpha":{"type":"http","url":"https://mcp.example.com/mcp"}}}' \
    "$(dg_global DG_MCP_JSON)" "the http shape is type plus url"
  assert_eq 'mcp__alpha__get_thing,mcp__alpha__list_things' "$(dg_global DG_MCP_TOOLS)" \
    "the tool names carry the mcp__<id>__ prefix"
}

test_stdio_server_builds_the_command_shape() {
  local dir="$TMP/stdiodir"
  mkdir -p "$dir"
  write_kv "$dir/local" \
    transport stdio \
    command npx \
    trust local-exec \
    args '-y @upstash/context7-mcp' \
    tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" local ''
  assert_eq 0 "$RC" "a stdio entry resolves"
  assert_eq '{"mcpServers":{"local":{"command":"npx","args":["-y","@upstash/context7-mcp"]}}}' \
    "$(dg_global DG_MCP_JSON)" "the stdio shape is command plus args, one element each"
}

test_stdio_server_without_args_omits_the_key() {
  local dir="$TMP/stdio-bare"
  mkdir -p "$dir"
  write_kv "$dir/local" transport stdio command /usr/bin/serve trust local-exec tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" local ''
  assert_eq 0 "$RC" "a stdio entry with no args resolves"
  assert_eq '{"mcpServers":{"local":{"command":"/usr/bin/serve"}}}' "$(dg_global DG_MCP_JSON)" \
    "args is omitted entirely"
}

test_write_grants_the_read_tools_too() {
  local dir="$TMP/writedir"
  mkdir -p "$dir"
  write_kv "$dir/alpha" \
    transport http \
    url 'https://mcp.example.com/mcp' \
    tools_read 'get_thing,list_things' \
    tools_write 'set_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" '' alpha
  assert_eq 0 "$RC" "a write id resolves"
  assert_eq 'mcp__alpha__get_thing,mcp__alpha__list_things,mcp__alpha__set_thing' "$(dg_global DG_MCP_TOOLS)" \
    "writing without reading is not a mode anyone wants"
}

test_read_and_write_ids_together_produce_the_union_without_repeats() {
  local dir="$TMP/union"
  mkdir -p "$dir"
  # `shared` is named in both lists, and `get_thing` in both tool lists.
  write_kv "$dir/shared" \
    transport http \
    url 'https://mcp.example.com/mcp' \
    tools_read 'get_thing' \
    tools_write 'get_thing,set_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" shared shared
  assert_eq 0 "$RC" "the same id in both lists is not an error"
  assert_eq '{"mcpServers":{"shared":{"type":"http","url":"https://mcp.example.com/mcp"}}}' \
    "$(dg_global DG_MCP_JSON)" "one server, one JSON entry"
  assert_eq 'mcp__shared__get_thing,mcp__shared__set_thing' "$(dg_global DG_MCP_TOOLS)" \
    "one allowlist, each tool once"
}

test_two_servers_produce_one_object_with_both() {
  local dir="$TMP/two"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'list_things'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,beta ''
  assert_eq 0 "$RC" "two known ids resolve"
  assert_eq '{"mcpServers":{"alpha":{"type":"http","url":"https://a.example.com/mcp"},"beta":{"type":"http","url":"https://b.example.com/mcp"}}}' \
    "$(dg_global DG_MCP_JSON)" "one JSON object, both servers"
  assert_eq 'mcp__alpha__get_thing,mcp__beta__list_things' "$(dg_global DG_MCP_TOOLS)" "both allowlists"
}

test_ids_keep_the_order_they_were_requested_in() {
  local dir="$TMP/order"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" beta,alpha ''
  assert_eq '{"mcpServers":{"beta":{"type":"http","url":"https://b.example.com/mcp"},"alpha":{"type":"http","url":"https://a.example.com/mcp"}}}' \
    "$(dg_global DG_MCP_JSON)" "the ids follow the request, not the directory"
}

test_env_passthrough_is_collected_and_deduplicated() {
  local dir="$TMP/envdir"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' \
    env_passthrough 'HOME,EDITOR'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing' \
    env_passthrough 'EDITOR,DG_TEST_UNSET'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,beta ''
  assert_eq 0 "$RC" "entries with env_passthrough resolve"
  assert_eq 'HOME EDITOR DG_TEST_UNSET' "${DG_MCP_ENV[*]}" \
    "the names come in first-seen order, each one once"
  # A name that is unset right now is not this function's business.
  assert_eq '' "${DG_TEST_UNSET+set}" "the variable really is unset and it was not an error"
}

test_requires_file_is_collected_with_its_server_id() {
  local dir="$TMP/reqdir"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' \
    requires_file 'skills/alpha/SKILL.md,docs/alpha.md'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing' \
    requires_file 'skills/alpha/SKILL.md'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,beta ''
  assert_eq 0 "$RC" "entries with requires_file resolve"
  # Tab-separated, id first, in requested order, NOT deduplicated: the run is
  # missing the shared file for both servers, and the message has to be able to
  # name one the user actually asked for.
  assert_eq 3 "${#DG_MCP_NEED[@]}" "one element per path, duplicates kept"
  assert_eq "alpha"$'\t'"skills/alpha/SKILL.md" "${DG_MCP_NEED[0]}" "first path carries its id"
  assert_eq "alpha"$'\t'"docs/alpha.md" "${DG_MCP_NEED[1]}" "second path of the same entry"
  assert_eq "beta"$'\t'"skills/alpha/SKILL.md" "${DG_MCP_NEED[2]}" "the other server's copy is kept"
  # This library does not touch the filesystem for these: existence is decided
  # by bin/delegate, which is what knows the working directory.
  assert_fails "the path really does not exist and that was not an error here" \
    test -e "skills/alpha/SKILL.md"
}

test_requires_file_is_absent_when_no_entry_declares_one() {
  local dir="$TMP/noreq"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "an entry without requires_file resolves"
  assert_eq 0 "${#DG_MCP_NEED[@]}" "DG_MCP_NEED is an empty array, not unset"
}

test_rejects_a_requires_file_outside_its_charset() {
  assert_reject_value requires_file '../../etc/passwd' 'a path with ..' http_lines
  assert_reject_value requires_file 'a/../b' 'a path with .. in the middle' http_lines
  assert_reject_value requires_file '~/secrets' 'a path with a tilde' http_lines
  assert_reject_value requires_file 'a b/c' 'a path with a space' http_lines
  assert_reject_value requires_file '$HOME/x' 'a path with a variable' http_lines
  assert_reject_value requires_file 'a,,b' 'an empty element' http_lines
  assert_reject_value requires_file 'a,b,c,d,e' 'five paths' http_lines
}

test_accepts_a_relative_and_an_absolute_requires_file() {
  # Relative is the useful case and the reason the charset differs from
  # `command`: this value is only ever stat'ed, never executed.
  assert_accept_value requires_file 'skills/figma-use/SKILL.md' 'a relative path' http_lines
  assert_accept_value requires_file '/etc/hostname' 'an absolute path' http_lines
  assert_accept_value requires_file 'a.md,b/c.md,d,e/f/g.json' 'four paths' http_lines
}

# ── dg_mcp_resolve: DG_MCP_RULES ────────────────────────────────────────────

test_rules_reaches_the_run_under_its_server_prefix() {
  local dir="$TMP/rulesdir" text
  mkdir -p "$dir"
  text='lee el README antes de escribir. ¿Sabes por que? (Claro) 100% + * = - @ # / _ ñÑüÜáéíóú'
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' rules "$text"
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "resolving an entry with rules succeeds"
  assert_eq '' "$ERR" "no stderr"
  assert_eq "Sobre el servidor MCP alpha: $text" "$(dg_global DG_MCP_RULES)" \
    "the whole text, and the id of the server it came from"
}

test_two_servers_with_rules_keep_the_requested_order() {
  local dir="$TMP/rules-order" expected
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' rules 'guia de alpha'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing' rules 'guia de beta'
  reset_globals
  capture dg_mcp_resolve "$dir" beta,alpha ''
  assert_eq 0 "$RC" "two entries with rules resolve"
  expected='Sobre el servidor MCP beta: guia de beta Sobre el servidor MCP alpha: guia de alpha'
  assert_eq "$expected" "$(dg_global DG_MCP_RULES)" "both texts, in the order they were requested"
}

test_a_server_without_rules_adds_nothing() {
  # One prefix and one text: no second prefix, no separator left dangling and no
  # empty clause for the entry that declared no rules.
  local dir="$TMP/rules-mixed" expected
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' rules 'guia de alpha'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,beta ''
  assert_eq 0 "$RC" "an entry with rules and one without resolve together"
  expected='Sobre el servidor MCP alpha: guia de alpha'
  assert_eq "$expected" "$(dg_global DG_MCP_RULES)" "only the entry that declares rules contributes"
  assert_not_contains "$(dg_global DG_MCP_RULES)" 'beta' "no prefix for the entry with no rules"
}

test_rules_of_an_unrequested_server_do_not_appear() {
  # Declared in the directory, never asked for: its guidance stays out of the
  # run, exactly like its tools and its JSON do.
  local dir="$TMP/rules-unasked"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' rules 'guia de alpha'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing' rules 'guia de beta'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "asking for one of the two succeeds"
  assert_eq 'Sobre el servidor MCP alpha: guia de alpha' "$(dg_global DG_MCP_RULES)" \
    "only the requested entry speaks"
  assert_not_contains "$(dg_global DG_MCP_RULES)" 'guia de beta' "the unrequested entry says nothing"
}

test_a_requested_server_without_rules_leaves_rules_set_and_empty() {
  local dir="$TMP/rules-empty"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "an entry with no rules resolves"
  assert_eq 'set' "${DG_MCP_RULES+set}" "DG_MCP_RULES is SET, not unset"
  assert_eq '' "$(dg_global DG_MCP_RULES)" "and it is empty"
}

test_rules_are_granted_in_read_mode_too() {
  # The guidance belongs to the entry, not to the tool lattice: a read-only run
  # against an entry that declares rules still gets them.
  local dir="$TMP/rules-read"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' \
    tools_write 'set_thing' rules 'lee antes de tocar nada'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha ''
  assert_eq 0 "$RC" "a read-only request resolves"
  assert_eq 'Sobre el servidor MCP alpha: lee antes de tocar nada' "$(dg_global DG_MCP_RULES)" \
    "the guidance does not depend on --mcp-write"

  reset_globals
  capture dg_mcp_resolve "$dir" '' alpha
  assert_eq 0 "$RC" "the same entry resolves as a write request"
  assert_eq 'Sobre el servidor MCP alpha: lee antes de tocar nada' "$(dg_global DG_MCP_RULES)" \
    "and the guidance is the same one"
}

test_the_same_id_in_both_lists_speaks_once() {
  # The id appears in the JSON once, so its guidance must appear once too.
  local dir="$TMP/rules-both"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' \
    tools_write 'set_thing' rules 'una sola vez'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha alpha
  assert_eq 0 "$RC" "the same id in both lists resolves"
  assert_eq 'Sobre el servidor MCP alpha: una sola vez' "$(dg_global DG_MCP_RULES)" "one prefix, one text"
}

test_a_write_on_an_entry_without_tools_write_is_refused() {
  local dir="$TMP/nowrite"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" '' alpha
  assert_eq 2 "$RC" "write where the entry declares none is refused"
  assert_contains "$ERR" 'delegate: mcp alpha: ' "the message names the server"
  assert_contains "$ERR" 'tools_write' "and the missing key"
  assert_globals_unset "a refused write"
}

test_a_refusal_after_a_grant_leaves_no_global_from_the_grant() {
  # The resolver published its globals on success and simply returned on a
  # refusal, so a refused call kept the JSON, tools and env names of the call
  # before it. The CLI exits on a refusal today; the contract is that a refused
  # run is never readable as a granted one, whoever the caller is.
  local dir="$TMP/both"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing' \
    tools_write 'put_thing' env_passthrough 'ALPHA_TOKEN' rules 'Regla de alpha.' requires_file 'NOTAS.md'
  reset_globals
  capture dg_mcp_resolve "$dir" '' alpha
  assert_eq 0 "$RC" "control: the write grant resolves"
  assert_ne '' "$(dg_global DG_MCP_TOOLS)" "control: the grant set DG_MCP_TOOLS"
  capture dg_mcp_resolve "$dir" alpha,ghost ''
  assert_eq 2 "$RC" "the second call is refused"
  assert_globals_unset "a refusal right after a grant"
}

test_an_unknown_id_is_refused() {
  local dir="$TMP/known"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,ghost ''
  assert_eq 2 "$RC" "an id with no entry is refused"
  assert_contains "$ERR" 'delegate: mcp ghost: ' "the message names the missing server"
  assert_globals_unset "an unknown id"
}

test_an_invalid_id_is_refused() {
  local dir="$TMP/ids"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" '../alpha' ''
  assert_eq 2 "$RC" "an id that is not an id is refused"
  assert_globals_unset "an invalid id"
}

test_a_repeated_id_in_the_same_list_is_refused() {
  local dir="$TMP/dup"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" 'alpha,alpha' ''
  assert_eq 2 "$RC" "the same id twice in one list is refused"
  assert_contains "$ERR" 'twice' "the message says what happened"
  assert_globals_unset "a repeated id"
}

test_an_empty_entry_in_a_list_is_refused() {
  local dir="$TMP/emptylist"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  reset_globals
  capture dg_mcp_resolve "$dir" 'alpha,' ''
  assert_eq 2 "$RC" "a trailing comma is refused"
  assert_globals_unset "an empty list entry"
}

test_an_entry_that_fails_to_parse_refuses_the_whole_run() {
  local dir="$TMP/broken"
  mkdir -p "$dir"
  write_kv "$dir/alpha" transport http url 'https://a.example.com/mcp' tools_read 'get_thing'
  write_kv "$dir/beta" transport http url 'https://b.example.com/mcp' tools_read 'get_thing' caps 'extra'
  reset_globals
  capture dg_mcp_resolve "$dir" alpha,beta ''
  assert_eq 2 "$RC" "one broken entry refuses the run, it does not just drop the server"
  assert_contains "$ERR" 'delegate: mcp beta: ' "the message names the broken entry"
  assert_globals_unset "a broken entry"
}

# ── the entries this repo ships ─────────────────────────────────────────────

test_the_figma_example_parses() {
  local path="$repo/examples/mcp.d/figma"
  capture dg_mcp_parse "$path" figma
  assert_eq 0 "$RC" "examples/mcp.d/figma is a valid entry"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" $'transport\thttp' "it is the http entry Claude Code saved the OAuth login for"
  assert_contains "$OUT" $'url\thttps://mcp.figma.com/mcp' "pointing at the hosted endpoint"
  assert_contains "$OUT" 'get_design_context' "with the read tools"
  assert_contains "$OUT" 'use_figma' "and the write tools"
  assert_not_contains "$OUT" 'mcp__figma__' "tool names are written without the prefix"
  assert_not_contains "$OUT" 'env_passthrough' "no variable is forwarded: the OAuth login is not a token in this file"
}

test_the_shipped_examples_parse_and_figma_carries_its_rules() {
  # Both examples are exercised from a copy under $TMP, which is how a user
  # installs them: the suite never reads the real ~/.config/delegate. context7's
  # real tool names carry hyphens, so this also pins that the widened charset
  # made the entry writable rather than the entry being softened to fit.
  local dir="$TMP/mcp.d" expected
  mkdir -p "$dir"
  cp "$repo/examples/mcp.d/figma" "$dir/figma"
  cp "$repo/examples/mcp.d/context7" "$dir/context7"
  cp "$repo/examples/mcp.d/repo-notes" "$dir/repo-notes"

  capture dg_mcp_parse "$dir/repo-notes" repo-notes
  assert_eq 0 "$RC" "the repo-notes example parses"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" $'requires_file\tdocs/NOTES.md' "and carries its precondition"
  # figma deliberately does NOT, because its skill file is a precondition of
  # writing and reading a design without it is a fine run.
  capture dg_mcp_parse "$dir/figma" figma
  assert_not_contains "$OUT" 'requires_file' "figma sets no precondition"

  capture dg_mcp_parse "$dir/context7" context7
  assert_eq 0 "$RC" "the context7 example parses"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" $'tools_read\tresolve-library-id,query-docs' \
    "with the server's own hyphenated tool names"

  capture dg_mcp_parse "$dir/figma" figma
  assert_eq 0 "$RC" "the figma example parses"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$OUT" 'rules' "and it declares rules"

  reset_globals
  capture dg_mcp_resolve "$dir" figma,context7 ''
  assert_eq 0 "$RC" "both examples resolve in one run"
  assert_eq '' "$ERR" "no stderr"
  assert_contains "$(dg_global DG_MCP_JSON)" '"figma"' "the JSON carries figma"
  assert_contains "$(dg_global DG_MCP_JSON)" '"context7"' "and context7"
  assert_contains "$(dg_global DG_MCP_TOOLS)" 'mcp__context7__resolve-library-id' \
    "the hyphenated name keeps the prefix"

  expected="Sobre el servidor MCP figma: $(entry_value "$dir/figma" rules)"
  assert_eq "$expected" "$(dg_global DG_MCP_RULES)" "figma's rules arrives whole, under its prefix"
  # The comparison above is built from the file, so it would follow the entry
  # wherever it went. These pin the real content instead: if the guidance were
  # truncated, its accents dropped or its punctuation mangled, one of them would
  # go missing.
  assert_contains "$(dg_global DG_MCP_RULES)" 'skills/figma-use/SKILL.md' "including the file it points at"
  assert_contains "$(dg_global DG_MCP_RULES)" 'diseño' "and the accents"
  assert_contains "$(dg_global DG_MCP_RULES)" 'node-id=1-23' "and the punctuation a Spanish sentence uses"
  assert_not_contains "$(dg_global DG_MCP_RULES)" 'context7' "context7 declares no rules and adds nothing"
}

run_tests
