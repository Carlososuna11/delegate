#!/usr/bin/env bash
# lib/permissions.sh — the permission lattice and the provider `caps` ceiling.
#
# The load-bearing case is the rejection one: a capability the provider's caps
# does not include must fail with exit 2, never be dropped. A caller that asked
# for --bash and silently got a read-only run would report the task as
# impossible instead of prohibited (invariant 4 in CLAUDE.md).
#
# The ceiling covers edit, bash, mcp and mcp-write. Which MCP servers exist, what
# their tools are called and the JSON that reaches the child are lib/mcp.sh's
# business and are tested in test/mcp.test.sh: this file only owns the ceiling
# and the sentence that tells the model MCP tools exist and are namespaced.
source "$(dirname "${BASH_SOURCE[0]}")/lib/assert.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/permissions.sh"

# dg_perm_resolve talks through globals, so each test starts from a clean slate:
# a stale DG_ALLOWED would make an assertion pass for the wrong reason.
reset_globals() {
  unset DG_TOOLS DG_ALLOWED DG_RULES
}

# Rejections run in a subshell: only the exit code and stderr matter, and the
# globals the function might have set must not leak into the test.
expect_reject() {
  # <caps> <want_edit> <want_bash> <want_mcp> <want_mcp_write> [mcp_rules]
  REJECT_RC=0
  ( dg_perm_resolve "$@" ) >"$TMP/out" 2>"$TMP/err" || REJECT_RC=$?
  REJECT_OUT="$(cat "$TMP/out")"
  REJECT_ERR="$(cat "$TMP/err")"
}

test_floor_grants_no_write_and_no_unrestricted_bash() {
  reset_globals
  dg_perm_resolve "" 0 0 0 0 || fail "the floor must resolve"
  assert_eq "Read,Bash" "$DG_TOOLS" "floor tool list"
  assert_not_contains "$DG_TOOLS" "Edit" "no Edit without --edit"
  assert_not_contains "$DG_TOOLS" "Write" "no Write without --edit"
  assert_not_contains ",$DG_ALLOWED," ",Edit," "no Edit in allowed"
  assert_not_contains ",$DG_ALLOWED," ",Write," "no Write in allowed"
  # Delimited on purpose: DG_ALLOWED does contain `Bash(grep:*)`, which a plain
  # `contains "Bash"` would accept as unrestricted shell.
  assert_not_contains ",$DG_ALLOWED," ",Bash," "no unrestricted Bash at the floor"
}

test_floor_still_exposes_the_bash_tool() {
  reset_globals
  dg_perm_resolve "" 0 0 0 0 || fail "the floor must resolve"
  assert_contains ",$DG_TOOLS," ",Bash," "Bash exists: there is no Grep or Glob, search goes through the shell"
  assert_contains "$DG_ALLOWED" "Bash(grep:*)" "grep is allowed through Bash"
  assert_contains "$DG_ALLOWED" "Bash(git diff:*)" "git diff is allowed through Bash"
}

test_edit_adds_edit_and_write_to_tools_and_allowed() {
  reset_globals
  dg_perm_resolve "read,edit,bash" 1 0 0 0 || fail "--edit must resolve"
  assert_contains ",$DG_TOOLS," ",Edit," "Edit added to tools"
  assert_contains ",$DG_TOOLS," ",Write," "Write added to tools"
  assert_contains ",$DG_ALLOWED," ",Edit," "Edit added to allowed"
  assert_contains ",$DG_ALLOWED," ",Write," "Write added to allowed"
}

test_bash_adds_unrestricted_bash_to_allowed() {
  reset_globals
  dg_perm_resolve "read,edit,bash" 0 1 0 0 || fail "--bash must resolve"
  assert_contains ",$DG_ALLOWED," ",Bash," "unrestricted Bash allowed"
  assert_eq "Read,Bash" "$DG_TOOLS" "--bash widens the allow list, not the tool list"
}

test_find_is_never_in_the_read_only_list() {
  reset_globals
  dg_perm_resolve "" 0 0 0 0 || fail "the floor must resolve"
  assert_not_contains "$DG_ALLOWED" "find" "floor: find stays out (-delete and -exec write)"

  reset_globals
  dg_perm_resolve "read,edit,bash,mcp,mcp-write" 1 1 1 1 || fail "the widest flag set must resolve"
  assert_not_contains "$DG_ALLOWED" "find" "widest flag set: find still out"
}

test_caps_without_edit_rejects_with_exit_2() {
  reset_globals
  expect_reject "read,bash" 1 0 0 0
  assert_eq 2 "$REJECT_RC" "--edit above the ceiling exits 2"
  assert_contains "$REJECT_ERR" "el proveedor no permite edit" "names the missing cap"
  assert_eq "" "$REJECT_OUT" "nothing on stdout"
  assert_eq "" "${DG_TOOLS:-}" "no globals set on rejection"
  assert_eq "" "${DG_ALLOWED:-}" "no globals set on rejection"
}

test_caps_without_bash_rejects_with_exit_2() {
  reset_globals
  expect_reject "read,edit" 0 1 0 0
  assert_eq 2 "$REJECT_RC" "--bash above the ceiling exits 2"
  assert_contains "$REJECT_ERR" "el proveedor no permite bash" "names the missing cap"
  assert_eq "" "${DG_ALLOWED:-}" "no globals set on rejection"
}

test_caps_without_mcp_rejects_with_exit_2() {
  reset_globals
  expect_reject "read,edit,bash" 0 0 1 0
  assert_eq 2 "$REJECT_RC" "--mcp above the ceiling exits 2"
  assert_contains "$REJECT_ERR" "el proveedor no permite mcp" "names the missing cap"
  assert_eq "" "$REJECT_OUT" "nothing on stdout"
  assert_eq "" "${DG_TOOLS:-}" "no globals set on rejection"
  assert_eq "" "${DG_ALLOWED:-}" "no globals set on rejection"
  assert_eq "" "${DG_RULES:-}" "no rules set on rejection"
}

test_caps_without_mcp_write_rejects_with_exit_2() {
  reset_globals
  # `mcp` alone is not enough to write: the ceiling has to say mcp-write.
  expect_reject "read,edit,bash,mcp" 0 0 1 1
  assert_eq 2 "$REJECT_RC" "--mcp-write above the ceiling exits 2"
  assert_contains "$REJECT_ERR" "el proveedor no permite mcp-write" "names the missing cap"
  assert_eq "" "${DG_ALLOWED:-}" "no globals set on rejection"
  assert_eq "" "${DG_RULES:-}" "no rules set on rejection"
}

test_mcp_write_ceiling_also_covers_read_mode() {
  # `mcp-write` implies `mcp`: a provider allowed to write through a server can
  # certainly read through it. The reverse does not hold, which the previous test
  # pins down.
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp-write" 0 0 1 0 || fail "write ceiling covers read"
  assert_contains "$DG_RULES" "mcp__<servidor>__<herramienta>" "MCP is announced at the ceiling"
  assert_contains "$DG_RULES" "Por MCP solo puedes LEER" "read mode stays read-only over MCP"
}

test_allowed_never_carries_mcp_tools() {
  # No server is named in lib/permissions.sh: the mcp__* allowlist is built by
  # lib/mcp.sh from the entries and appended by bin/delegate. A library that
  # invented an MCP tool here would be reaching past its own contract, and a run
  # whose allowlist has tools nobody granted is exactly what the ceiling exists
  # to prevent.
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp,mcp-write" 0 0 1 1 || fail "mcp must resolve"
  assert_not_contains "$DG_ALLOWED" "mcp__" "no MCP tool in the local allowlist"
  assert_not_contains "$DG_TOOLS" "mcp__" "no MCP tool in the tool list"
  assert_eq "Read,Bash" "$DG_TOOLS" "MCP does not widen the local tool list"

  # Same when nothing was asked for: no flag, no MCP.
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp,mcp-write" 0 0 0 0 || fail "no flag must resolve"
  assert_not_contains "$DG_ALLOWED" "mcp__" "no MCP tool without a flag"
  assert_not_contains "$DG_RULES" "MCP" "no MCP guidance without a flag"
}

test_mcp_rules_explain_the_namespace_and_the_read_limit() {
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp" 0 0 1 0 || fail "mcp read must resolve"
  assert_contains "$DG_RULES" "mcp__<servidor>__<herramienta>" "says how MCP tools are named"
  assert_contains "$DG_RULES" "solo existen las que están en tu lista de permitidas" \
    "says only the granted tools exist"
  assert_contains "$DG_RULES" "Por MCP solo puedes LEER" "no write was requested, so MCP is read-only"

  reset_globals
  dg_perm_resolve "read,edit,bash,mcp,mcp-write" 0 0 1 1 || fail "mcp write must resolve"
  assert_not_contains "$DG_RULES" "Por MCP solo puedes LEER" "with write requested the limit is gone"
}

test_mcp_rules_argument_is_appended_verbatim() {
  local extra='Usa fileKey como id de archivo y pasá el node-id como 1:2.'
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp" 0 0 1 0 "$extra" || fail "mcp_rules must resolve"
  assert_contains "$DG_RULES" "$extra" "the entry's own guidance reaches the prompt"
  # Verbatim and last: removing the text as a suffix has to leave exactly it.
  assert_eq "$extra" "${DG_RULES#"${DG_RULES%"$extra"}"}" "appended verbatim, at the end"

  # A run with no MCP enabled has no server to take guidance from.
  reset_globals
  dg_perm_resolve "read,edit,bash,mcp,mcp-write" 0 0 0 0 "$extra" || fail "no MCP must resolve"
  assert_not_contains "$DG_RULES" "$extra" "guidance from an entry does not leak in without a flag"
}

test_invalid_inputs_are_rejected() {
  reset_globals
  expect_reject "read,edit,bash" 0 0 sideways 0
  assert_eq 2 "$REJECT_RC" "unknown want_mcp exits 2"
  assert_contains "$REJECT_ERR" "want_mcp inválido" "names the bad argument"

  reset_globals
  expect_reject "read,edit,bash" 0 0 1 sideways
  assert_eq 2 "$REJECT_RC" "unknown want_mcp_write exits 2"
  assert_contains "$REJECT_ERR" "want_mcp_write inválido" "names the bad argument"

  reset_globals
  expect_reject "read,edit,bash" maybe 0 0 0
  assert_eq 2 "$REJECT_RC" "non-numeric want_edit exits 2"
}

test_rules_state_the_working_rules() {
  reset_globals
  dg_perm_resolve "" 0 0 "" "" || fail "the floor must resolve"
  assert_contains "$DG_RULES" "git add -A" "no git add -A"
  assert_contains "$DG_RULES" "git add ." "no git add ."
  assert_contains "$DG_RULES" "--no-verify" "no --no-verify"
  assert_contains "$DG_RULES" "commit" "no commits"
  assert_contains "$DG_RULES" "secrets" "no secrets"
  assert_contains "$DG_RULES" "dilo explícitamente" "say when a check could not run"
  assert_contains "$DG_RULES" "grep o rg" "search through the shell"
  assert_contains "$DG_RULES" "denegado" "report a denied command"
  assert_contains "$DG_RULES" "codex" "no codex"
  assert_contains "$DG_RULES" "claude" "no claude"

  # The tool inventory must match the grant. On a read-only run the model is told
  # it has Read and Bash and nothing else, and is told so explicitly, rather than
  # discovering it by calling a tool that is not there.
  assert_contains "$DG_RULES" "Read y Bash" "read-only announces only Read and Bash"
  assert_contains "$DG_RULES" "SOLO LECTURA" "read-only says so out loud"
  assert_not_contains "$DG_RULES" "Read, Edit" "no Edit announced without --edit"
  assert_contains "$DG_RULES" "solo corren comandos de lectura" "restricted Bash is spelled out"
  # The prompt travels as one --append-system-prompt argument: a stray backslash
  # (from a line continuation written in single quotes) or a literal newline
  # would reach the model.
  assert_not_contains "$DG_RULES" '\' "no stray backslash"
  assert_eq 0 "$(printf '%s' "$DG_RULES" | wc -l)" "one single line"

  reset_globals
  dg_perm_resolve "read,edit,bash" 1 0 0 0 || fail "--edit must resolve"
  assert_contains "$DG_RULES" "Read, Edit, Write y Bash" "with --edit the inventory grows"
  assert_not_contains "$DG_RULES" "SOLO LECTURA" "with --edit it is not a read-only run"

  # --bash removes the restricted-command warning, because it stops being true.
  reset_globals
  dg_perm_resolve "read,edit,bash" 1 1 0 0 || fail "--edit --bash must resolve"
  assert_not_contains "$DG_RULES" "solo corren comandos de lectura" "no false restriction with --bash"
}

test_default_caps_ceiling() {
  assert_eq "read,edit,bash" "$(dg_perm_caps_default)" "default ceiling"
  # No caps means the default ceiling, so --edit and --bash are fine.
  reset_globals
  dg_perm_resolve "" 1 1 0 0 || fail "empty caps takes the default ceiling"
  assert_contains ",$DG_ALLOWED," ",Edit," "default ceiling allows edit"
  assert_contains ",$DG_ALLOWED," ",Bash," "default ceiling allows bash"
  # ... and it does not include mcp.
  reset_globals
  expect_reject "" 0 0 1 0
  assert_eq 2 "$REJECT_RC" "the default ceiling has no mcp"
}

run_tests
