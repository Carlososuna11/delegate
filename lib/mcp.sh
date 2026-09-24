#!/usr/bin/env bash
# MCP servers: which ones a delegated run may reach, and with which tools.
#
# Isolation is still the floor. A delegated run reaches NO MCP server unless the
# invocation asks for one by name, and asking for a server grants only the tools
# that server's own entry lists. The point is not that some servers are safe and
# others are not: it is that the machine's owner declares what exists, and each
# run declares what it needs. A run that needs nothing gets nothing.
#
# Entries live OUTSIDE this repo, in ${DELEGATE_MCP_DIR:-~/.config/delegate/mcp.d},
# one file per server, named after its id. Same `key<TAB>value` format as a
# provider, and the same discipline: PARSED, never sourced and never eval'd.
#
# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT — implement exactly this surface. bin/delegate depends on it.
# ─────────────────────────────────────────────────────────────────────────────
#
# dg_mcp_dir
#   Prints the entry directory: $DELEGATE_MCP_DIR if set and non-empty,
#   otherwise $HOME/.config/delegate/mcp.d. Does not require it to exist.
#
# dg_mcp_servers <dir>
#   Prints the server ids found in <dir>, sorted, one per line. A filename that
#   is not a valid id is skipped silently — it is not an entry. Contents are not
#   validated. Nothing on stdout and exit 0 when the directory is missing or
#   empty: having no MCP configured is the normal case, not an error.
#
# dg_mcp_parse <file> <id>
#   Validates the entry and prints one `key<TAB>value` line per recognised key,
#   sorted by key, omitting keys the file does not set. Returns 0 on success. On
#   ANY validation problem, prints `delegate: mcp <id>: <reason>` to stderr and
#   returns 2, having printed nothing to stdout.
#
# dg_mcp_resolve <dir> <read_ids> <write_ids>
#   read_ids and write_ids are comma-separated lists of server ids, either of
#   which may be empty. An id in write_ids also grants that server's read tools:
#   writing without reading is not a mode anyone wants.
#
#   On success returns 0 and sets exactly these globals:
#     DG_MCP_JSON   the complete --mcp-config argument (a JSON object with an
#                   "mcpServers" key), or '' when no server was requested
#     DG_MCP_TOOLS  comma-separated allowlist of fully prefixed tool names
#                   (mcp__<id>__<tool>), or '' when no server was requested
#     DG_MCP_RULES  the enabled entries' own `rules` text, concatenated in the
#                   requested order and each prefixed with its server id, for
#                   bin/delegate to hand to dg_perm_resolve; '' when no server
#                   was requested or none of them declares rules
#     DG_MCP_ENV    array of environment variable NAMES to forward to the child,
#                   collected from every requested server's env_passthrough;
#                   deduplicated; empty array when there are none
#     DG_MCP_NEED   array of `<id>\t<path>` entries, one per path in every
#                   requested server's requires_file, in the requested order and
#                   NOT deduplicated: the id travels with the path so the error
#                   can say which server needs it. Empty array when there are
#                   none. Whether a path exists is not decided here — this
#                   library never touches the filesystem outside the entry
#                   directory, and the working directory belongs to bin/delegate.
#
#   On an unknown id, an id that appears twice in the same list, an entry that
#   fails to parse, or a write request against an entry with no tools_write,
#   prints the reason to stderr and returns 2 WITHOUT setting any global. A
#   refused run must not be readable as a run with fewer servers.
#
# ─────────────────────────────────────────────────────────────────────────────
# KEYS — the complete set. Anything else is rejected.
# ─────────────────────────────────────────────────────────────────────────────
#
#   required, always:
#     transport    http | sse | stdio
#     tools_read   Comma-separated tool names WITHOUT the mcp__<id>__ prefix,
#                  which this library adds. Each ^[a-z][a-z0-9_-]*$, no
#                  duplicates, no empty elements, at least one. A server with no
#                  readable tool has nothing to offer, so an empty value is an
#                  error rather than a quiet no-op.
#
#   required for transport http and sse:
#     url          ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$
#                  Plain http is refused. Rejected outright for stdio.
#
#   required for transport stdio:
#     command      Absolute path, or a bare name to resolve on the child's PATH.
#                  ^[A-Za-z0-9._/-]+$ and must not contain '..'.
#     trust        Must be exactly `local-exec`. An http entry is a URL; a stdio
#                  entry is a binary running on this machine with the child's
#                  privileges. That deserves a second, explicit line in the file
#                  rather than being implied by `transport`. Rejected outright
#                  for http and sse.
#
#   optional:
#     args             Only for stdio. Space-separated argv elements, each
#                      ^[A-Za-z0-9._@:/=+-]+$. An element cannot contain a space:
#                      say so in the reason when one does, because the limit is
#                      real and the workaround is a wrapper script.
#     rules            Guidance for the delegated run, appended to its system
#                      prompt when this entry is enabled. Letters with accents,
#                      digits, space and , . ; : ( ) ? ! / _ @ # % & + * = -,
#                      600 bytes max, so an accented character costs two.
#                      Spanish, `tú`, no voseo, like every
#                      other rule the run is given. Reached only when the entry
#                      is, so a declared-but-unrequested server says nothing.
#     env_passthrough  Comma-separated variable NAMES, each ^[A-Z][A-Z0-9_]*$,
#                      forwarded from the parent environment. NAMES ONLY: a
#                      token must never be written into this file. A name whose
#                      variable is unset at resolve time is not an error here —
#                      bin/delegate decides what to do about that.
#     requires_file    Comma-separated paths that must EXIST for this entry to
#                      be usable. A relative path is read from the run's working
#                      directory (`--dir`); an absolute one is a precondition of
#                      the machine. Each ^[A-Za-z0-9._/-]+$, no '..', 4 paths
#                      max. This generalises what used to be a hard-coded check
#                      for the Figma skill file: an entry whose `rules` say "read
#                      X first" is broken in a tree that has no X, and failing
#                      with the name of the missing file beats a delegated run
#                      inventing its way around it. The check lives in
#                      bin/delegate, which is what knows the working directory;
#                      this library only carries the list.
#     tools_write      Same shape as tools_read. Tools that change something in
#                      the server. Reached only via write_ids.
#     description      Free text for `delegate mcp list`. ^[A-Za-z0-9 ,.()/_-]+$,
#                      120 characters max.
#
# The server id (the filename, and what the user types after --mcp) must match
# ^[a-z][a-z0-9-]{0,31}$.
#
# ─────────────────────────────────────────────────────────────────────────────
# BUILDING THE JSON
# ─────────────────────────────────────────────────────────────────────────────
#
# DG_MCP_JSON is assembled by concatenation, with no escaping step, and that is
# safe ONLY because every character class above excludes the quote, the
# backslash and control characters. Validation is what makes the assembly sound,
# so a new key must not widen a charset without revisiting this.
#
#   http and sse:  {"type":"<transport>","url":"<url>"}
#   stdio:         {"command":"<command>","args":["<a1>","<a2>",...]}
#                  args omitted entirely when the entry sets none.
#
# and the whole argument is {"mcpServers":{"<id>":{...},"<id2>":{...}}} with the
# ids in the order requested.
#
# The Figma entry that used to be hard-coded is now just an example entry, with
# no privileges no other entry has. Ship it in examples/mcp.d/figma. Its OAuth
# login is keyed by Claude Code to the server name and config, so an entry named
# `figma` pointing at https://mcp.figma.com/mcp reuses the saved login and no
# token passes through here — which is exactly why env_passthrough is empty for
# it, and worth a comment in the example.
#
# ─────────────────────────────────────────────────────────────────────────────
# Tests go in test/mcp.test.sh. Every rejection rule needs a case proving it
# rejects, with the exit code checked. Required beyond those: a stdio entry
# without `trust` is refused; a `url` on a stdio entry and a `command` on an http
# entry are both refused; requesting write on an entry with no tools_write is
# refused; read and write ids together produce the union of the tool lists with
# no duplicates; two servers at once produce one JSON object with both; and the
# no-server case leaves DG_MCP_JSON and DG_MCP_TOOLS empty rather than unset.
# Entries are built under $TMP; the suite must never read the real
# ~/.config/delegate.

# ─────────────────────────────────────────────────────────────────────────────
# IMPLEMENTATION
# ─────────────────────────────────────────────────────────────────────────────
#
# Sourced by bin/delegate next to lib/config.sh, so this file sets no shell
# options: a `set -e` or `set -u` from here would change how the caller behaves.
# It owns only the `_dg_mcp_*` and `_DG_MCP_*` names.
#
# Nothing here evaluates an entry file. Same grammar as a provider file, with one
# difference worth naming: `transport` is not just another value, it decides
# which of the other keys are required and which are refused, so the
# transport-dependent checks run after the whole file is read and the answer
# does not depend on the order of the lines.

# The complete key set. Anything else is a typo, and in a file that decides what
# a delegated run can reach a typo is a rejection, not a warning.
_DG_MCP_KEYS=(transport url command args trust tools_read tools_write env_passthrough description rules requires_file)

# What each transport requires, and what it refuses. A stdio entry is a binary
# running on this machine with the child's privileges; an http entry is a URL.
# Mixing the two vocabularies is always a mistake, never a harmless extra line.
# tools_read is required regardless of transport: a server with no readable tool
# has nothing to offer, so omitting it is an error rather than an entry that
# resolves to an empty allowlist and silently grants nothing.
_DG_MCP_REQUIRED_ALWAYS=(transport tools_read)
_DG_MCP_REQUIRED_HTTP=(url)
_DG_MCP_REQUIRED_STDIO=(command trust)
_DG_MCP_ONLY_STDIO=(command trust args)
_DG_MCP_ONLY_HTTP=(url)

# Character classes, named once. Every one of them is the reason the JSON can be
# assembled by concatenation, so none of them contains a quote, a backslash or a
# control character. Widening one means re-reading the note on the JSON above.
# Hyphens are allowed: real servers ship them (context7 has resolve-library-id),
# and a charset that refused them would make those entries unwritable rather
# than safer. A hyphen needs no quoting inside mcp__<id>__<tool> either.
_DG_MCP_RE_TOOL='^[a-z][a-z0-9_-]*$'
_DG_MCP_RE_ENVNAME='^[A-Z][A-Z0-9_]*$'
_DG_MCP_RE_URL='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$'
# Either an absolute path or a bare name, and nothing in between. The old
# charset also accepted `./x`, `bin/x` and `sub/dir/x`, which the header
# contract never promised and which are read relative to the CHILD's cwd — and
# that cwd is the working directory under review. A `bin/mcp-server` committed
# into a repo that gets handed to `delegate` would then run as the delegated
# process, outside the tool allowlist entirely. A bare name resolves on the
# child's PATH, whose first entry is the stub directory this tool controls.
_DG_MCP_RE_COMMAND='^(/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*|[A-Za-z0-9][A-Za-z0-9._-]*)$'
_DG_MCP_RE_ARG='^[A-Za-z0-9._@:/=+-]+$'
_DG_MCP_RE_DESCRIPTION='^[A-Za-z0-9 ,.()/_-]+$'
# A path this entry needs to exist. Relative paths ARE allowed here, unlike in
# `command`: this one is only ever stat'ed, never executed, and the useful case
# is exactly a file inside the tree under review (`skills/figma-use/SKILL.md`).
# No `..` — see the check next to the charset — and no leading `~`, which
# nothing expands at this layer and would be stat'ed as a literal directory name.
_DG_MCP_RE_REQFILE='^[A-Za-z0-9._/-]+$'
# `rules` is the only value that reaches the child as prose rather than as
# config, so it needs the accents and the punctuation a Spanish sentence uses.
# It still excludes the quote, the backslash and control characters: it travels
# as one argv element next to the JSON, and an entry file is not a place where
# anything gets to inject a line break into the system prompt.
_DG_MCP_RE_RULES='^[A-Za-z0-9áéíóúÁÉÍÓÚñÑüÜ ,.;:()¿?¡!/_@#%&+*=-]+$'
_DG_MCP_RE_ID='^[a-z][a-z0-9-]{0,31}$'

# Error channel of the value validator: set on rejection, printed by the caller
# that knows the entry name. Keeps the `delegate: mcp <id>: <reason>` format in
# one place instead of repeated in every rule.
_dg_mcp_reason=''

_dg_mcp_err() {
  # <id> <reason>
  printf 'delegate: mcp %s: %s\n' "$1" "$2" >&2
}

_dg_mcp_usage() {
  # <function> <signature>
  printf 'delegate: mcp: usage: %s %s\n' "$1" "$2" >&2
}

_dg_mcp_valid_id() {
  # An id is a filename, what the user types after --mcp, and part of every tool
  # name (mcp__<id>__<tool>), so it stays in the one shape that needs no quoting
  # anywhere. Narrower than a tool name: no underscore, because `__` is the
  # separator inside mcp__<id>__<tool> and an id carrying one would make that
  # name ambiguous to read.
  [[ "$1" =~ $_DG_MCP_RE_ID ]]
}

_dg_mcp_ltrim() {
  # Leading spaces and tabs only, as the grammar says. Not `[:space:]`: a `\r`
  # from a CRLF file is left in the value so the key's own rule rejects it
  # loudly, instead of a mangled value sliding through.
  local s="$1"
  printf '%s' "${s#"${s%%[![:blank:]]*}"}"
}

_dg_mcp_rtrim() {
  local s="$1"
  printf '%s' "${s%"${s##*[![:blank:]]}"}"
}

_dg_mcp_validate_list() {
  # <lineno> <key> <value> <regex> <allowed> <reject-dup|allow-dup>: 0 when every
  # comma-separated element is a whole element of the list and matches <regex>;
  # 1 after setting _dg_mcp_reason. Elements are matched whole, so `read` cannot
  # be satisfied by `read_write`.
  local lineno="$1" key="$2" value="$3" re="$4" allowed="$5" dup="$6"
  local rest="$value" part='' seen=' '

  while :; do
    part="${rest%%,*}"
    if [[ -z "$part" ]]; then
      _dg_mcp_reason="line $lineno: $key has an empty element"
      return 1
    fi
    if [[ ! "$part" =~ $re ]]; then
      _dg_mcp_reason="line $lineno: $key element '$part' is not allowed, only $allowed"
      return 1
    fi
    if [[ "$dup" == reject-dup ]]; then
      case " $seen " in
        *" $part "*) _dg_mcp_reason="line $lineno: duplicate $key element '$part'"; return 1 ;;
      esac
      seen="$seen$part "
    fi
    [[ "$rest" == *,* ]] || break
    rest="${rest#*,}"
  done
  return 0
}

_dg_mcp_validate() {
  # <lineno> <key> <value>: 0 when the value is valid; 1 after setting
  # _dg_mcp_reason. Never prints, so the caller owns the message prefix, and
  # never checks the transport: that is a cross-key matter, done after the file
  # has been read.
  local lineno="$1" key="$2" value="$3"
  local rest='' part=''

  case "$key" in
    transport)
      case "$value" in
        http|sse|stdio) ;;
        *) _dg_mcp_reason="line $lineno: transport must be http, sse or stdio"; return 1 ;;
      esac
      ;;
    url)
      # https only, as in a provider endpoint.
      [[ "$value" =~ $_DG_MCP_RE_URL ]] ||
        { _dg_mcp_reason="line $lineno: url must be an https:// URL"; return 1; }
      ;;
    command)
      [[ "$value" =~ $_DG_MCP_RE_COMMAND ]] ||
        { _dg_mcp_reason="line $lineno: command must be an absolute path or a bare name (A-Za-z0-9 and . _ -), not a relative path"; return 1; }
      [[ "$value" != *'..'* ]] ||
        { _dg_mcp_reason="line $lineno: command must not contain '..'"; return 1; }
      ;;
    args)
      # Space-separated argv elements, and an element cannot contain a space:
      # there is no quoting in this format. The only way a space shows up inside
      # one is as an empty element (a leading, trailing or doubled space), and
      # the reason says so, because the limit is real and the workaround is a
      # wrapper script on this machine.
      rest="$value"
      while :; do
        part="${rest%% *}"
        if [[ -z "$part" ]]; then
          _dg_mcp_reason="line $lineno: args has an empty element, and an argument containing a space cannot be written here; use a wrapper script"
          return 1
        fi
        [[ "$part" =~ $_DG_MCP_RE_ARG ]] ||
          { _dg_mcp_reason="line $lineno: args element '$part' is not allowed, only A-Za-z0-9 and . _ @ : / = + - (no spaces: an argument containing one needs a wrapper script)"; return 1; }
        [[ "$rest" == *' '* ]] || break
        rest="${rest#* }"
      done
      ;;
    trust)
      # A second, explicit line rather than something implied by `transport`:
      # this is a binary that will run with the child's privileges.
      [[ "$value" == local-exec ]] ||
        { _dg_mcp_reason="line $lineno: trust must be local-exec"; return 1; }
      ;;
    tools_read|tools_write)
      _dg_mcp_validate_list "$lineno" "$key" "$value" "$_DG_MCP_RE_TOOL" \
        'lowercase letters, digits, _ and - , starting with a letter' reject-dup ||
        return 1
      ;;
    env_passthrough)
      # NAMES ONLY: a token must never be written into this file. Duplicates are
      # not an error here — resolve deduplicates, and a name repeated across two
      # entries is the normal case, not a mistake.
      _dg_mcp_validate_list "$lineno" "$key" "$value" "$_DG_MCP_RE_ENVNAME" \
        'uppercase letters, digits and _ , starting with a letter' allow-dup ||
        return 1
      ;;
    description)
      # Free text for `delegate mcp list`. Shown, never acted on.
      [[ "$value" =~ $_DG_MCP_RE_DESCRIPTION ]] ||
        { _dg_mcp_reason="line $lineno: description allows only letters, digits and space , . ( ) / _ -"; return 1; }
      (( ${#value} <= 120 )) ||
        { _dg_mcp_reason="line $lineno: description must be at most 120 characters"; return 1; }
      ;;
    rules)
      # Guidance appended to the delegated run's system prompt when this entry
      # is enabled. It is the one key that changes what the model is told, so
      # the cap is there to keep an entry from crowding out the rest of the
      # prompt: guidance that does not fit belongs in a file the run reads.
      [[ "$value" =~ $_DG_MCP_RE_RULES ]] ||
        { _dg_mcp_reason="line $lineno: rules allows only letters (with accents), digits, space and , . ; : ( ) ? ! / _ @ # % & + * = -"; return 1; }
      # Bytes, and said so. `${#value}` counts characters in a UTF-8 locale and
      # bytes under LC_ALL=C, so the same file was accepted or rejected
      # depending on the caller's locale. Fixing the unit to bytes is the choice
      # that does not move: an accented character costs two, which still leaves
      # room for several sentences.
      (( $(LC_ALL=C printf '%s' "$value" | wc -c) <= 600 )) ||
        { _dg_mcp_reason="line $lineno: rules must be at most 600 bytes (an accented character counts as two)"; return 1; }
      ;;
    requires_file)
      # Paths that must exist for this entry to be usable. Duplicates are
      # allowed for the same reason env_passthrough allows them: two entries
      # needing the same file is the normal case.
      _dg_mcp_validate_list "$lineno" "$key" "$value" "$_DG_MCP_RE_REQFILE" \
        'letters, digits and . _ / -' allow-dup ||
        return 1
      # `..` on any element. The path is resolved against the run's working
      # directory, and a precondition that reaches outside the tree under review
      # is not a precondition of that tree.
      [[ ",$value," != *'..'* ]] ||
        { _dg_mcp_reason="line $lineno: requires_file must not contain '..'"; return 1; }
      # The cap is small on purpose: this is a precondition, not a manifest. An
      # entry that needs five files needs a setup step instead.
      local nreq
      nreq=$(printf '%s' "$value" | tr ',' '\n' | grep -c .)
      (( nreq <= 4 )) ||
        { _dg_mcp_reason="line $lineno: requires_file allows at most 4 paths, found $nreq"; return 1; }
      ;;
  esac
  return 0
}

_dg_mcp_check_list() {
  # <list> <read|write>: 0 when every id in the comma-separated list is a valid
  # id and no id repeats. Sets no global: the caller splits the list itself once
  # this says the split is safe.
  local list="$1" what="$2"
  local rest="$list" part='' seen=' '

  [[ -n "$list" ]] || return 0
  while :; do
    part="${rest%%,*}"
    if [[ -z "$part" ]]; then
      _dg_mcp_err '' "empty entry in the $what list"
      return 2
    fi
    if ! _dg_mcp_valid_id "$part"; then
      _dg_mcp_err "$part" "not a valid server id (in the $what list)"
      return 2
    fi
    case " $seen " in
      *" $part "*) _dg_mcp_err "$part" "listed twice in the $what list"; return 2 ;;
    esac
    seen="$seen$part "
    [[ "$rest" == *,* ]] || break
    rest="${rest#*,}"
  done
  return 0
}

_dg_mcp_json_fragment() {
  # <transport> <url> <command> <args-string>: the server object, built by
  # concatenation. NO ESCAPING happens here or anywhere, and that is sound ONLY
  # because every charset above excludes the quote, the backslash and control
  # characters: a value that reached this point cannot close the string it is
  # being pasted into. The validation is what makes this line safe, so a new key
  # must not widen a charset without coming back here first.
  local transport="$1" url="$2" command="$3" args="$4"
  local rest='' part='' items='' sep=''

  case "$transport" in
    http|sse)
      printf '{"type":"%s","url":"%s"}' "$transport" "$url"
      ;;
    stdio)
      if [[ -z "$args" ]]; then
        # No args key at all rather than an empty array: the entry did not set
        # one, and the caller's config shape should say so.
        printf '{"command":"%s"}' "$command"
      else
        rest="$args"
        while :; do
          part="${rest%% *}"
          items="$items$sep\"$part\""
          sep=','
          [[ "$rest" == *' '* ]] || break
          rest="${rest#* }"
        done
        printf '{"command":"%s","args":[%s]}' "$command" "$items"
      fi
      ;;
  esac
}

dg_mcp_dir() {
  if (( $# != 0 )); then
    _dg_mcp_usage dg_mcp_dir ''
    return 2
  fi

  # The override exists so the tests never touch the real $HOME. `:-` and not
  # `-`: an empty DELEGATE_MCP_DIR is a mistake, not a directory named ''.
  printf '%s\n' "${DELEGATE_MCP_DIR:-${HOME:-}/.config/delegate/mcp.d}"
  return 0
}

dg_mcp_servers() {
  if (( $# != 1 )); then
    _dg_mcp_usage dg_mcp_servers '<dir>'
    return 2
  fi

  local dir="$1" path='' name=''
  # No MCP configured is the normal case, not an error.
  [[ -d "$dir" ]] || return 0

  for path in "$dir"/*; do
    [[ -f "$path" ]] || continue
    name="${path##*/}"
    _dg_mcp_valid_id "$name" || continue
    printf '%s\n' "$name"
  done | LC_ALL=C sort
  return 0
}

dg_mcp_parse() {
  if (( $# != 2 )); then
    _dg_mcp_usage dg_mcp_parse '<file> <id>'
    return 2
  fi

  local file="$1" id="$2"
  local line='' key='' value='' stripped=''
  local lineno=0 ntabs=0 transport='' k=''
  local -a out=()
  local -A seen=() values=() linenos=()

  if ! _dg_mcp_valid_id "$id"; then
    _dg_mcp_err "$id" 'not a valid server id'
    return 2
  fi
  if [[ ! -f "$file" || ! -r "$file" ]]; then
    _dg_mcp_err "$id" "cannot read server file: $file"
    return 2
  fi

  # Line by line, never sourced and never eval'd. Everything is validated before
  # a single byte reaches stdout, so a rejected file prints nothing on stdout.
  while IFS= read -r line || [[ -n "$line" ]]; do
    # `lineno=$(( lineno + 1 ))`, not `(( lineno++ ))`: a bare arithmetic command
    # returns 1 when the result is 0, and bin/delegate runs with `set -e`.
    lineno=$(( lineno + 1 ))

    line="$(_dg_mcp_ltrim "$line")"
    [[ -z "$line" || "$line" == '#'* ]] && continue

    stripped="${line//$'\t'/}"
    ntabs=$(( ${#line} - ${#stripped} ))
    if (( ntabs == 0 )); then
      _dg_mcp_err "$id" "line $lineno: expected key<TAB>value, found no tab"
      return 2
    fi
    if (( ntabs > 1 )); then
      _dg_mcp_err "$id" "line $lineno: expected key<TAB>value, found more than one tab"
      return 2
    fi

    key="${line%%$'\t'*}"
    # Trailing blanks come off the value rather than off the line: that keeps
    # `key<TAB>` an empty value (a rejection) instead of a tab-less line.
    value="$(_dg_mcp_rtrim "${line#*$'\t'}")"
    if [[ -z "$value" ]]; then
      _dg_mcp_err "$id" "line $lineno: empty value for key '$key'"
      return 2
    fi

    case "$key" in
      transport|url|command|args|trust|tools_read|tools_write|env_passthrough|description|rules|requires_file) ;;
      *) _dg_mcp_err "$id" "line $lineno: unknown key '$key'"; return 2 ;;
    esac

    if [[ -n "${seen[$key]:-}" ]]; then
      _dg_mcp_err "$id" "line $lineno: duplicate key '$key'"
      return 2
    fi
    seen[$key]=1

    if ! _dg_mcp_validate "$lineno" "$key" "$value"; then
      _dg_mcp_err "$id" "$_dg_mcp_reason"
      return 2
    fi
    values[$key]="$value"
    linenos[$key]="$lineno"
  done < "$file"

  for k in "${_DG_MCP_REQUIRED_ALWAYS[@]}"; do
    if [[ -z "${seen[$k]:-}" ]]; then
      _dg_mcp_err "$id" "missing required key: $k"
      return 2
    fi
  done

  # The transport decides the rest, now that every line has been read. Without
  # this a `url` on a stdio entry would reach the JSON and Claude Code would run
  # a local binary that the file described as something else entirely.
  transport="${values[transport]}"
  case "$transport" in
    http|sse)
      for k in "${_DG_MCP_REQUIRED_HTTP[@]}"; do
        if [[ -z "${seen[$k]:-}" ]]; then
          _dg_mcp_err "$id" "missing required key: $k"
          return 2
        fi
      done
      for k in "${_DG_MCP_ONLY_STDIO[@]}"; do
        if [[ -n "${seen[$k]:-}" ]]; then
          _dg_mcp_err "$id" "line ${linenos[$k]}: $k is only for transport stdio"
          return 2
        fi
      done
      ;;
    stdio)
      for k in "${_DG_MCP_REQUIRED_STDIO[@]}"; do
        if [[ -z "${seen[$k]:-}" ]]; then
          _dg_mcp_err "$id" "missing required key: $k"
          return 2
        fi
      done
      for k in "${_DG_MCP_ONLY_HTTP[@]}"; do
        if [[ -n "${seen[$k]:-}" ]]; then
          _dg_mcp_err "$id" "line ${linenos[$k]}: $k is only for transport http or sse"
          return 2
        fi
      done
      ;;
  esac

  # Only the keys the file set: defaults belong to the caller. Sorted so the
  # output does not depend on the order of the file or on the order in which the
  # key vocabulary happens to be listed above.
  for key in "${_DG_MCP_KEYS[@]}"; do
    [[ -n "${seen[$key]:-}" ]] || continue
    out+=("$key"$'\t'"${values[$key]}")
  done
  printf '%s\n' "${out[@]}" | LC_ALL=C sort
  return 0
}

dg_mcp_resolve() {
  if (( $# != 3 )); then
    _dg_mcp_usage dg_mcp_resolve '<dir> <read_ids> <write_ids>'
    return 2
  fi

  local dir="$1" read_ids="$2" write_ids="$3"
  local -a reads=() writes=() ids=()
  local -a env_names=() tools=() json_parts=() needs=()
  local id='' srv='' file='' parsed='' line='' key='' value='' fragment=''
  local in_write='' transport='' url='' command='' args='' entry_env='' entry_req=''
  local t_read='' t_write='' tool='' name='' list='' rest='' seen_tools=' '
  # Everything is accumulated in locals and assigned to the DG_MCP_* globals at
  # the very end, so a refusal cannot leave half a run readable in them. The exit
  # code is the answer; the globals are only meaningful after a 0.
  local tools_acc='' json_acc='' rules_acc='' entry_rules=''

  _dg_mcp_check_list "$read_ids" read || return 2
  _dg_mcp_check_list "$write_ids" write || return 2
  # Safe to split here: _dg_mcp_check_list already refused empty entries and
  # anything that is not a plain id, so every element is a bare word.
  if [[ -n "$read_ids" ]]; then IFS=',' read -r -a reads <<< "$read_ids"; fi
  if [[ -n "$write_ids" ]]; then IFS=',' read -r -a writes <<< "$write_ids"; fi

  # Requested order, reads first, each server once: a server named in both lists
  # is one entry in the JSON, not two.
  for srv in ${reads[@]+"${reads[@]}"}; do ids+=("$srv"); done
  for srv in ${writes[@]+"${writes[@]}"}; do
    for id in ${ids[@]+"${ids[@]}"}; do
      [[ "$id" == "$srv" ]] && continue 2
    done
    ids+=("$srv")
  done

  # Nothing requested is a success with nothing granted: the floor is no MCPs.
  # Set, and set to empty: the caller distinguishes "no MCP asked for" from "the
  # resolver never ran" by the exit code, not by the globals being unset.
  if (( ${#ids[@]} == 0 )); then
    DG_MCP_JSON=''
    DG_MCP_TOOLS=''
    DG_MCP_RULES=''
    DG_MCP_ENV=()
    DG_MCP_NEED=()
    return 0
  fi

  for id in "${ids[@]}"; do
    in_write=''
    for srv in ${writes[@]+"${writes[@]}"}; do
      [[ "$srv" == "$id" ]] && in_write=1
    done

    file="$dir/$id"
    if [[ ! -f "$file" ]]; then
      _dg_mcp_err "$id" "unknown server, no entry at $file"
      return 2
    fi

    # The entry owns its own validation and its own message; a parse failure
    # propagates unchanged, and nothing has been assigned yet.
    if ! parsed="$(dg_mcp_parse "$file" "$id")"; then
      return 2
    fi

    transport='' url='' command='' args='' entry_env='' entry_req='' t_read='' t_write=''
    entry_rules=''
    while IFS= read -r line; do
      key="${line%%$'\t'*}"
      value="${line#*$'\t'}"
      case "$key" in
        transport) transport="$value" ;;
        url) url="$value" ;;
        command) command="$value" ;;
        args) args="$value" ;;
        env_passthrough) entry_env="$value" ;;
        requires_file) entry_req="$value" ;;
        tools_read) t_read="$value" ;;
        tools_write) t_write="$value" ;;
        rules) entry_rules="$value" ;;
      esac
    done <<< "$parsed"

    if [[ -n "$in_write" && -z "$t_write" ]]; then
      # Writing where the entry never declared a writing tool is a request the
      # file refuses. Accepting it while granting nothing would look like a run
      # that merely had fewer tools.
      _dg_mcp_err "$id" 'no tools_write in this entry, write cannot be requested'
      return 2
    fi

    # A write request grants the read tools too: the entry's own tools_read, plus
    # its tools_write, each tool once. A tool named in both lists is still one
    # entry in the allowlist.
    list="$t_read"
    [[ -n "$in_write" ]] && list="$t_read,$t_write"
    tools=()
    seen_tools=' '
    rest="$list"
    while :; do
      tool="${rest%%,*}"
      case " $seen_tools " in
        *" $tool "*) ;;
        *) seen_tools="$seen_tools$tool "; tools+=("mcp__${id}__${tool}") ;;
      esac
      [[ "$rest" == *,* ]] || break
      rest="${rest#*,}"
    done
    printf -v fragment '%s,' "${tools[@]}"
    tools_acc="$tools_acc${fragment%,},"

    # Names only, never values: the child inherits whatever the parent has (or
    # does not have) under that name. A name that is unset right now is not an
    # error here; bin/delegate decides what to do about it.
    if [[ -n "$entry_env" ]]; then
      local dup seen
      rest="$entry_env"
      while :; do
        name="${rest%%,*}"
        # The cursor must advance whether the name is new or a repeat. A
        # `continue 2` out of the inner loop skipped the advance below and spun
        # forever on the first name that two entries shared.
        dup=0
        for seen in ${env_names[@]+"${env_names[@]}"}; do
          if [[ "$seen" == "$name" ]]; then dup=1; break; fi
        done
        (( dup )) || env_names+=("$name")
        [[ "$rest" == *,* ]] || break
        rest="${rest#*,}"
      done
    fi

    # Preconditions, paired with the id that needs them. Not deduplicated: if
    # two servers need the same file, the run is missing it for both, and
    # whichever message the user reads should name a server they actually asked
    # for. Whether the path exists is bin/delegate's question — it owns the
    # working directory, and this library does not stat outside the entry dir.
    if [[ -n "$entry_req" ]]; then
      rest="$entry_req"
      while :; do
        name="${rest%%,*}"
        [[ -n "$name" ]] && needs+=("$id"$'\t'"$name")
        [[ "$rest" == *,* ]] || break
        rest="${rest#*,}"
      done
    fi

    # The id is the KEY the server object hangs off inside "mcpServers", so it
    # belongs to the assembly, not to the fragment: a bare object with no key
    # produces `{"mcpServers":{{...}}}`, which is not JSON at all. The id passed
    # the ^[a-z][a-z0-9-]+$ check, so it needs no escaping either.
    # The entry's own guidance, prefixed with the server id so the model can
    # tell whose rule it is when two servers are enabled at once. Collected
    # here rather than handed to lib/permissions.sh per entry: that file owns
    # the local tool lattice and stays ignorant of which servers exist.
    if [[ -n "$entry_rules" ]]; then
      rules_acc="$rules_acc Sobre el servidor MCP ${id}: ${entry_rules}"
    fi

    fragment="$(_dg_mcp_json_fragment "$transport" "$url" "$command" "$args")"
    json_parts+=("\"$id\":$fragment")
  done

  printf -v json_acc '%s,' "${json_parts[@]}"
  DG_MCP_JSON="{\"mcpServers\":{${json_acc%,}}}"
  DG_MCP_TOOLS="${tools_acc%,}"
  DG_MCP_RULES="${rules_acc# }"
  DG_MCP_ENV=()
  if (( ${#env_names[@]} )); then DG_MCP_ENV=("${env_names[@]}"); fi
  DG_MCP_NEED=()
  if (( ${#needs[@]} )); then DG_MCP_NEED=("${needs[@]}"); fi
  return 0
}
