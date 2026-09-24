#!/usr/bin/env bash
# The permission lattice: what a delegated run is allowed to do.
#
# Permissions are graduated and only ever widen from read-only. The provider's
# `caps` is a CEILING: a flag asking for more than the provider allows is
# REJECTED, never silently dropped. A user who asks for `--bash` and gets a
# read-only run would conclude the task was impossible rather than that it was
# not permitted. That is invariant 4 in CLAUDE.md.
#
# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT — implement exactly this surface. bin/delegate depends on it.
# ─────────────────────────────────────────────────────────────────────────────
#
# dg_perm_resolve <caps> <want_edit> <want_bash> <want_mcp> <want_mcp_write>
#                 [mcp_rules]
#   caps            comma-separated, already validated by lib/config.sh; empty
#                   means the default ceiling `read,edit,bash`
#   want_edit       1 or 0
#   want_bash       1 or 0
#   want_mcp        1 or 0 — any MCP server was requested for this run
#   want_mcp_write  1 or 0 — at least one was requested for writing. Implies
#                   want_mcp: writing through a server without reading through
#                   it is not a mode anyone wants.
#   mcp_rules       optional guidance contributed by the enabled entries, which
#                   lib/mcp.sh collects. Appended verbatim. This file never
#                   knows which servers those are.
#
#   On success returns 0 and sets these globals, and nothing else:
#     DG_TOOLS        value for --tools
#     DG_ALLOWED      value for --allowedTools, LOCAL tools only. bin/delegate
#                     appends the MCP tool allowlist from lib/mcp.sh.
#     DG_RULES        the full --append-system-prompt text
#
#   On a request above the ceiling, prints
#   `delegate: el proveedor no permite <cap>` to stderr and returns 2, without
#   setting any global: a refused run must not be readable as a narrower one.
#
# dg_perm_caps_default
#   Prints the default ceiling, `read,edit,bash`.
#
# ─────────────────────────────────────────────────────────────────────────────
# THE LATTICE
# ─────────────────────────────────────────────────────────────────────────────
#
# Read-only (no flags) — the floor, always available:
#   tools:   Read,Bash
#   allowed: Read plus read-only shell only:
#     Bash(grep:*) Bash(rg:*) Bash(ls:*) Bash(wc:*) Bash(head:*) Bash(tail:*)
#     Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(git status:*)
#     Bash(git blame:*)
#   `find` stays out deliberately: -delete and -exec write.
#   Bash EXISTS in read-only mode because there is no Grep or Glob tool here;
#   searching is done through the shell. So the tool list is not the boundary,
#   the allow list is.
#
# --edit  adds Edit and Write, to both tools and allowed.
# --bash  adds unrestricted `Bash` to allowed. Note it widens the allow list,
#         not the tool list — Bash is already present.
# --mcp <id> / --mcp-write <id>  reach a declared MCP server. No server is named
#         in this file, and none is reachable unless the invocation asks for it
#         by name. The server list is the machine owner's declaration, read by
#         lib/mcp.sh from ~/.config/delegate/mcp.d; the tool allowlist comes from
#         each entry, so enabling a server is not enabling everything it offers.
#         What this file contributes is the ceiling (`mcp`, `mcp-write`) and the
#         sentence in DG_RULES telling the model that MCP tools exist, are
#         namespaced, and are limited to what it was given.
#
# ─────────────────────────────────────────────────────────────────────────────
# DG_RULES
# ─────────────────────────────────────────────────────────────────────────────
#
# In --bare there is no CLAUDE.md, so the working rules have to be passed in.
# Spanish, addressing the model as `tú`. It must state:
#   - Spanish for prose, English for code comments and test descriptions.
#   - Never `git add -A` or `git add .`; never --no-verify. No commits, no push,
#     no PRs: leave changes in the working tree and summarise them.
#   - Never print or read secrets (.env, credentials, api keys).
#   - Say so explicitly when a verification could not be run, instead of
#     assuming the result.
#   - Which tools exist, matching the grant exactly: Read and Bash on the floor,
#     plus Edit and Write with --edit. There is never Grep or Glob, so searching
#     goes through grep or rg under Bash.
#   - A denied Bash command is to be reported, not worked around.
#   - Never invoke codex, claude or any other AI CLI: they spend the expensive
#     quota. If a second opinion seems needed, say so in the answer instead.
# The tool inventory is DERIVED from the grant, never a fixed sentence: on a
# read-only run the model is told it has Read and Bash and nothing else, and is
# told the run cannot write, rather than finding out by calling a tool that is
# not there. Same for the restricted shell.
#
# Plus, when any MCP server is enabled, that MCP tools are namespaced
# mcp__<server>__<tool>, that only the allowed ones exist, and — when no server
# was requested for writing — that MCP is read-only for this run. Anything
# server-specific arrives in `mcp_rules` from the entry itself; this file has no
# knowledge of any particular server.
#
# ─────────────────────────────────────────────────────────────────────────────
# Tests go in test/permissions.test.sh. Required cases: the floor grants no
# write and no unrestricted Bash; --edit adds Edit/Write; --bash adds Bash to
# the allow list; a cap the provider lacks is rejected with exit 2 for each of
# edit, bash, mcp and mcp-write; mcp-write implies mcp; DG_ALLOWED carries no
# mcp__ tool, since those come from lib/mcp.sh.
#
# ─────────────────────────────────────────────────────────────────────────────
# IMPLEMENTATION
# ─────────────────────────────────────────────────────────────────────────────

dg_perm_caps_default() {
  printf 'read,edit,bash\n'
}

# 0 when <caps> (comma-separated) contains <cap>, 1 otherwise. `mcp-write`
# implies `mcp`: a provider allowed to write through a server can certainly read
# through it, and reading is all the read mode asks for. The reverse does not
# hold.
_dg_perm_has_cap() {
  local caps="${1// /}" cap="$2"
  case ",$caps," in
    *",$cap,"*) return 0 ;;
  esac
  [[ "$cap" == mcp && ",$caps," == *",mcp-write,"* ]]
}

dg_perm_resolve() {
  local caps="${1-}" want_edit="${2-}" want_bash="${3-}"
  local want_mcp="${4-}" want_mcp_write="${5-}" mcp_rules="${6-}"

  # caps arrives already validated by lib/config.sh. Empty means the default
  # ceiling, which is what a provider file without a `caps` line gets.
  [[ -n "${caps// /}" ]] || caps="$(dg_perm_caps_default)"

  # The callers are trusted to pass 0/1 and '', 'read' or 'write', but a value
  # nobody understands must not be read as "no": that would be the same silent
  # downgrade the ceiling check exists to prevent.
  local we wb
  case "${want_edit:-0}" in
    1) we=1 ;;
    0) we=0 ;;
    *) printf 'delegate: want_edit inválido: %s\n' "$want_edit" >&2; return 2 ;;
  esac
  case "${want_bash:-0}" in
    1) wb=1 ;;
    0) wb=0 ;;
    *) printf 'delegate: want_bash inválido: %s\n' "$want_bash" >&2; return 2 ;;
  esac
  local wm wmw
  case "${want_mcp:-0}" in
    1) wm=1 ;;
    0) wm=0 ;;
    *) printf 'delegate: want_mcp inválido: %s\n' "$want_mcp" >&2; return 2 ;;
  esac
  case "${want_mcp_write:-0}" in
    1) wmw=1 ;;
    0) wmw=0 ;;
    *) printf 'delegate: want_mcp_write inválido: %s\n' "$want_mcp_write" >&2; return 2 ;;
  esac
  # Writing through a server without reading through it is not a mode: the
  # caller asking for one implies the other, and saying so here keeps every
  # caller from having to remember it.
  (( wmw )) && wm=1

  # The ceiling wins over the flags. Asking for a capability the provider lacks
  # is an error, never a downgrade: a read-only run handed to someone who asked
  # for --bash reads as "the task was impossible", not as "it was not allowed".
  if (( we )) && ! _dg_perm_has_cap "$caps" edit; then
    printf 'delegate: el proveedor no permite edit\n' >&2
    return 2
  fi
  if (( wb )) && ! _dg_perm_has_cap "$caps" bash; then
    printf 'delegate: el proveedor no permite bash\n' >&2
    return 2
  fi
  if (( wm )) && ! _dg_perm_has_cap "$caps" mcp; then
    printf 'delegate: el proveedor no permite mcp\n' >&2
    return 2
  fi
  if (( wmw )) && ! _dg_perm_has_cap "$caps" mcp-write; then
    printf 'delegate: el proveedor no permite mcp-write\n' >&2
    return 2
  fi

  # Read-only floor. Bash is a TOOL here even in read-only mode, because this
  # environment has no Grep and no Glob: searching goes through the shell. The
  # tool list is therefore not the boundary, the allow list is. `find` stays
  # out of it: -delete and -exec write.
  local tools='Read,Bash'
  local allowed='Read,Bash(grep:*),Bash(rg:*),Bash(ls:*),Bash(wc:*),Bash(head:*),Bash(tail:*),Bash(git log:*),Bash(git diff:*),Bash(git show:*),Bash(git status:*),Bash(git blame:*)'

  if (( we )); then
    tools="$tools,Edit,Write"
    allowed="$allowed,Edit,Write"
  fi
  if (( wb )); then
    # Widens the allow list, not the tool list: Bash is already a tool, it was
    # just restricted to the read-only commands above.
    allowed="$allowed,Bash"
  fi

  # In --bare there is no CLAUDE.md, so the working rules travel in the prompt.
  # Ported from the script this replaces (deepseek-agent), which had them inline.
  # Double quotes on purpose: inside single quotes a trailing backslash is
  # literal, so the line continuations would end up in the prompt. This text has
  # no $, no backtick, no " and no backslash, so double quotes are inert here.
  local rules="Responde en español venezolano neutro (tú, nunca voseo). Comentarios de código y descripciones de tests en inglés. \
Nunca uses git add -A ni git add . ; nunca --no-verify. No hagas commit, push ni abras PRs: deja los cambios en el working tree y resume qué cambiaste. \
Nunca imprimas ni leas secrets (.env, credenciales, api keys). Si una verificación depende de algo que no puedes correr, dilo explícitamente en vez de suponer el resultado. \
Si un comando de Bash es denegado, no intentes rodear la denegación: repórtalo. No invoques codex, claude ni ningún otro agente o CLI de IA: gastan cuota cara; si crees que hace falta una segunda opinión, dilo en tu respuesta."

  # The tool announcement is DERIVED from what was actually granted, never a
  # fixed sentence. The script this replaces always claimed "Read, Edit y Bash"
  # even on a read-only run, where Edit is not in the tool list at all: the model
  # was told it had a tool it did not have, and spent turns discovering that by
  # failing. An inventory that does not match the grant is worse than none.
  if (( we )); then
    rules="$rules Herramientas en este entorno: Read, Edit, Write y Bash; no existen Grep ni Glob."
  else
    rules="$rules Herramientas en este entorno: Read y Bash; no existen Edit, Write, Grep ni Glob. Esta corrida es de SOLO LECTURA: no puedes modificar ni crear archivos. Si la tarea requiere cambiarlos, dilo en tu respuesta en vez de intentarlo."
  fi
  rules="$rules Para buscar en el código usa grep o rg con Bash."
  if (( ! wb )); then
    rules="$rules Por Bash solo corren comandos de lectura (grep, rg, ls, wc, head, tail, y git log/diff/show/status/blame); cualquier otro va a ser denegado, así que no lo intentes."
  fi

  # No MCP server is named in this file. Which servers exist is the machine
  # owner's declaration (lib/mcp.sh reads it from ~/.config/delegate/mcp.d), and
  # which ones a run reaches is the invocation's. This file owns only the local
  # tool lattice and the ceiling; it appends whatever guidance the enabled
  # entries contributed, without knowing what any of them is.
  if (( wm )); then
    rules="$rules Esta corrida tiene servidores MCP habilitados: sus herramientas se llaman mcp__<servidor>__<herramienta> y solo existen las que están en tu lista de permitidas. Cualquier otro servidor MCP no existe acá, así que no lo intentes."
    if (( ! wmw )); then
      rules="$rules Por MCP solo puedes LEER: las herramientas de escritura no están habilitadas."
    fi
    [[ -n "$mcp_rules" ]] && rules="$rules $mcp_rules"
  fi

  DG_TOOLS="$tools"
  DG_ALLOWED="$allowed"
  DG_RULES="$rules"
  return 0
}
