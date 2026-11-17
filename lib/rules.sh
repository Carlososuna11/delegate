#!/usr/bin/env bash
# Code rules: what a delegated run is told about HOW to write code.
#
# Three layers, and only the first two ever reach the system prompt:
#
#   core   rules/core.md in this repo. Neutral procedure (search before
#          creating, one definition per value, copy the neighbour's shape, a
#          closing "Reutilización" section). Only on runs that can edit.
#   pack   ${DELEGATE_RULES_DIR:-~/.config/delegate/rules.d}/<id>/, the machine
#          owner's data, like mcp.d: `rules.md` (the text) and `pack`
#          (key<TAB>value: accounts, remotes, source, sha256). Pinned by sha,
#          limited to the accounts it lists, and only when a run asks for it
#          by name (--rules <id>).
#   repo   the tree's own CLAUDE.md or AGENTS.md. Its TEXT never enters the
#          prompt: step 1 of the core only names the file, and the model reads
#          it with Read, like any other file of the tree. A repository is not
#          the machine owner, and the system prompt is the strongest position
#          there is.
#
# Same discipline as lib/mcp.sh: parsed, never sourced; every refusal is a
# `delegate: rules ...` line on stderr and return 2, before the key is read.
#
# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT — bin/delegate depends on it.
# ─────────────────────────────────────────────────────────────────────────────
#
# dg_rules_dir
#   Prints $DELEGATE_RULES_DIR if set and non-empty, else
#   $HOME/.config/delegate/rules.d. Does not require it to exist.
#
# dg_rules_packs <dir>
#   Prints the pack ids in <dir> (directories whose name is a valid id), sorted.
#
# dg_rules_resolve <core_file|''> <account> <ids> <root>
#   <core_file> empty means no core (a run without --edit). <ids> is a comma
#   separated list of pack ids, possibly empty. <account> is the account the
#   run will read the key of. <root> is the resolved working directory.
#   On success returns 0 and sets DG_CODE_RULES (possibly empty). On any
#   refusal returns 2 and leaves DG_CODE_RULES unset.
#
# dg_rules_load_pack <dir> <id>
#   Validates one pack completely (files, metadata, text, sha) and leaves its
#   text in _dg_rules_text and its metadata in _DG_PACK_*. Returns 2 on refusal.
#
# dg_rules_add <dir> <id> <from> <accounts> <remotes>
#   Validates <from> like a run would, then installs it as pack <id> with its
#   sha pinned, replacing an existing pack of that id. Returns 2 on refusal.
#
# Depends on lib/accounts.sh (_DG_ACCOUNT_NAME_RE) and lib/mcp.sh
# (_DG_MCP_RE_ID, the id shape both kinds of entry share), sourced before.

# Bytes, not characters: `${#v}` depends on the caller's locale, and the same
# file was accepted or rejected by locale in mcp.sh before it fixed the unit.
_DG_RULES_MAX_BYTES=2000
# Above this, step 1 asks for the file's headings instead of the whole file.
_DG_RULES_POINTER_MAX_LINES=300
_DG_RULES_PLACEHOLDER='@REPO_RULES@'
# The pointer counts lines in at most this many bytes of the file.
_DG_RULES_POINTER_MAX_SCAN=1048576
# What the pointer says the repository's file may decide, and which of its
# @includes to follow. Step numbers are the core's with step 1 present.
_DG_RULES_POINTER_SCOPE='sus convenciones mandan sobre los pasos 2 a 6. Si tiene líneas @archivo, lee solo las de ruta relativa dentro del repositorio, nunca una ruta absoluta ni una que empiece con ~.'
# A remote pattern is a regular expression matched by delegate-run (Python
# re.search) against `remote.origin.url`. The charset is the subset where
# POSIX ERE and Python agree, and it excludes the comma that separates them.
_DG_RULES_RE_REMOTE='^[]A-Za-z0-9._:/@^$|()?+*[-]+$'
_DG_RULES_RE_SHA='^[0-9a-f]{64}$'
# `[:name:]` or `[.name.]`: the POSIX bracket classes Python does not have.
_DG_RULES_RE_POSIX_CLASS='\[:[^]]*:\]|\[\.[^]]*\.\]'

# Every C0 control character except the line feed, plus DEL. Built once.
_dg_rules_ctl=''
for _dg_rules_i in {1..31} 127; do
  (( _dg_rules_i == 10 )) && continue
  printf -v _dg_rules_c "\\$(printf '%03o' "$_dg_rules_i")"
  _dg_rules_ctl+="$_dg_rules_c"
done
unset -v _dg_rules_i _dg_rules_c

_dg_rules_err() {
  printf 'delegate: rules %s\n' "$*" >&2
}

dg_rules_dir() {
  printf '%s\n' "${DELEGATE_RULES_DIR:-${HOME:-}/.config/delegate/rules.d}"
}

dg_rules_packs() {
  local dir="$1" path name
  [[ -d "$dir" ]] || return 0
  for path in "$dir"/*; do
    [[ -d "$path" ]] || continue
    name="${path##*/}"
    [[ "$name" =~ $_DG_MCP_RE_ID ]] || continue
    printf '%s\n' "$name"
  done | LC_ALL=C sort
  return 0
}

# _dg_rules_private <path> <f|d> <what>: a regular file or a directory, not a
# symlink, owned by this user and writable by nobody else. Whoever can write a
# pack decides what the delegated model is told.
_dg_rules_private() {
  local path="$1" kind="$2" what="$3" st owner mode
  if [[ -L "$path" ]]; then
    _dg_rules_err "$what: $path is a symlink"; return 2
  fi
  if [[ "$kind" == f && ! -f "$path" ]] || [[ "$kind" == d && ! -d "$path" ]]; then
    _dg_rules_err "$what: $path does not exist or is not a $([[ $kind == f ]] && echo file || echo directory)"
    return 2
  fi
  st="$(stat -c '%u %a' -- "$path" 2>/dev/null)" || { _dg_rules_err "$what: cannot stat $path"; return 2; }
  read -r owner mode <<<"$st"
  if [[ "$owner" != "$EUID" ]]; then
    _dg_rules_err "$what: $path is not owned by you"; return 2
  fi
  if (( 8#$mode & 8#022 )); then
    _dg_rules_err "$what: $path is writable by group or others (mode $mode)"; return 2
  fi
  return 0
}

# _dg_rules_read <path> <what>: the file's exact bytes into _dg_rules_text,
# trailing newlines included, after the size cap. A NUL (which bash cannot
# hold) shows up as fewer bytes read than the file has, and is refused.
_dg_rules_read() {
  local path="$1" what="$2" disk got
  _dg_rules_text=''
  disk="$(LC_ALL=C wc -c <"$path" 2>/dev/null)" || { _dg_rules_err "$what: cannot read $path"; return 2; }
  if (( disk > _DG_RULES_MAX_BYTES )); then
    _dg_rules_err "$what: $path has $disk bytes, the cap is $_DG_RULES_MAX_BYTES"
    return 2
  fi
  IFS= read -r -d '' _dg_rules_text <"$path" || true
  got="$(printf '%s' "$_dg_rules_text" | LC_ALL=C wc -c)"
  if (( got != disk )); then
    _dg_rules_err "$what: $path contains a NUL byte or changed while it was read"
    return 2
  fi
  _dg_rules_check_text "$what"
}

# _dg_rules_check_text <what>: _dg_rules_text is UTF-8, has no control
# character but the line feed, and fits the cap.
_dg_rules_check_text() {
  local what="$1" n
  n="$(printf '%s' "$_dg_rules_text" | LC_ALL=C wc -c)"
  if (( n > _DG_RULES_MAX_BYTES )); then
    _dg_rules_err "$what: $n bytes, the cap is $_DG_RULES_MAX_BYTES"; return 2
  fi
  if [[ -z "${_dg_rules_text//[[:space:]]/}" ]]; then
    _dg_rules_err "$what: the text is empty"; return 2
  fi
  if ! printf '%s' "$_dg_rules_text" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
    _dg_rules_err "$what: the text is not valid UTF-8"; return 2
  fi
  if [[ "$_dg_rules_text" == *["$_dg_rules_ctl"]* ]]; then
    _dg_rules_err "$what: the text has a control character (only line breaks are allowed)"
    return 2
  fi
  return 0
}

_dg_rules_sha() {
  local s
  s="$(printf '%s' "$1" | sha256sum)" || return 1
  printf '%s\n' "${s%% *}"
}

# _dg_rules_valid_list <value> <regex>: a comma separated list whose every
# element matches, with no empty element.
_dg_rules_valid_list() {
  local value="$1" re="$2" item
  local -a items=()
  [[ -n "$value" && "$value" != ,* && "$value" != *, && "$value" != *,,* ]] || return 1
  IFS=, read -r -a items <<<"$value"
  for item in "${items[@]}"; do
    [[ "$item" =~ $re ]] || return 1
  done
  return 0
}

# _dg_rules_valid_remotes <value>: the charset, no POSIX bracket class, and
# every pattern compiles as an ERE. Cheap: it runs on every pack load.
_dg_rules_valid_remotes() {
  local item rc
  local -a items=()
  _dg_rules_valid_list "$1" "$_DG_RULES_RE_REMOTE" || return 1
  # `[:alpha:]` and `[.a.]` inside a bracket are classes to ERE and plain
  # characters to Python. `[:/]` alone is fine: both read it as `:` or `/`.
  [[ ! "$1" =~ $_DG_RULES_RE_POSIX_CLASS ]] || return 1
  IFS=, read -r -a items <<<"$1"
  for item in "${items[@]}"; do
    rc=0
    [[ '' =~ $item ]] || rc=$?
    (( rc != 2 )) || return 1
  done
  return 0
}

# _dg_rules_remotes_agree <value>: every pattern also compiles in Python's re,
# warnings as errors, which is what delegate-run matches with (`a**` and `x)`
# are ERE but not Python). Only `rules add` and `rules check` call it: a run
# never uses `remotes`, so a run does not need python3.
_dg_rules_remotes_agree() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -I -W error -c '
import re, sys
for p in sys.argv[1].split(","):
    re.compile(p)
' "$1" >/dev/null 2>&1
}

# _dg_rules_parse_pack <file> <id>: the metadata, strictly, into _DG_PACK_*.
_dg_rules_parse_pack() {
  local file="$1" id="$2" line key value lineno=0 stripped
  local -A seen=()
  _DG_PACK_ACCOUNTS='' _DG_PACK_REMOTES='' _DG_PACK_SOURCE='' _DG_PACK_SHA=''
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$(( lineno + 1 ))
    [[ -z "$line" || "$line" == '#'* ]] && continue
    stripped="${line//$'\t'/}"
    if (( ${#line} - ${#stripped} != 1 )); then
      _dg_rules_err "$id: pack line $lineno: expected key<TAB>value"; return 2
    fi
    key="${line%%$'\t'*}"
    value="${line#*$'\t'}"
    if [[ -z "$value" || "$value" == *["$_dg_rules_ctl"]* ]]; then
      _dg_rules_err "$id: pack line $lineno: empty value or a control character"; return 2
    fi
    if [[ -n "${seen[$key]:-}" ]]; then
      _dg_rules_err "$id: pack line $lineno: duplicate key '$key'"; return 2
    fi
    seen[$key]=1
    case "$key" in
      accounts)
        _dg_rules_valid_list "$value" "$_DG_ACCOUNT_NAME_RE" ||
          { _dg_rules_err "$id: pack line $lineno: accounts must be account names separated by commas"; return 2; }
        _DG_PACK_ACCOUNTS="$value" ;;
      remotes)
        _dg_rules_valid_remotes "$value" ||
          { _dg_rules_err "$id: pack line $lineno: remotes must be patterns separated by commas"; return 2; }
        _DG_PACK_REMOTES="$value" ;;
      source)
        _DG_PACK_SOURCE="$value" ;;
      sha256)
        [[ "$value" =~ $_DG_RULES_RE_SHA ]] ||
          { _dg_rules_err "$id: pack line $lineno: sha256 must be 64 lowercase hex digits"; return 2; }
        _DG_PACK_SHA="$value" ;;
      *)
        _dg_rules_err "$id: pack line $lineno: unknown key '$key'"; return 2 ;;
    esac
  done <"$file"
  if [[ -z "$_DG_PACK_ACCOUNTS" ]]; then
    _dg_rules_err "$id: pack has no accounts line: no account may use it"; return 2
  fi
  if [[ -z "$_DG_PACK_SHA" ]]; then
    _dg_rules_err "$id: pack has no sha256 line"; return 2
  fi
  return 0
}

dg_rules_load_pack() {
  local dir="$1" id="$2" sha
  if [[ ! "$id" =~ $_DG_MCP_RE_ID ]]; then
    _dg_rules_err "'$id': not a valid pack id"; return 2
  fi
  if [[ ! -e "$dir/$id" && ! -L "$dir/$id" ]]; then
    _dg_rules_err "$id: no such pack in $dir (delegate rules list)"; return 2
  fi
  _dg_rules_private "$dir" d "$id" || return 2
  _dg_rules_private "$dir/$id" d "$id" || return 2
  _dg_rules_private "$dir/$id/pack" f "$id" || return 2
  _dg_rules_private "$dir/$id/rules.md" f "$id" || return 2
  _dg_rules_parse_pack "$dir/$id/pack" "$id" || return 2
  _dg_rules_read "$dir/$id/rules.md" "$id" || return 2
  # The sha of the bytes that were READ, not of the file: what is checked is
  # what reaches the prompt.
  sha="$(_dg_rules_sha "$_dg_rules_text")" || { _dg_rules_err "$id: sha256sum failed"; return 2; }
  if [[ "$sha" != "$_DG_PACK_SHA" ]]; then
    _dg_rules_err "$id: rules.md does not match its pinned sha256; review it and install it again with delegate rules add"
    return 2
  fi
  return 0
}

# dg_rules_pointer <root>: step 1 of the core, or nothing. Only stat and a line
# count: the file's text is for the model to read, never for this script to
# paste. A regular file that resolves inside the tree; a link out of the tree,
# a FIFO or a directory yields no pointer.
dg_rules_pointer() {
  local root="$1" name real n
  for name in CLAUDE.md AGENTS.md; do
    [[ -f "$root/$name" ]] || continue
    real="$(readlink -f -- "$root/$name")" || continue
    [[ "$real" == "$root"/* && -f "$real" ]] || continue
    # Bounded in time and bytes: the file can be swapped for a FIFO after the
    # -f check, or be huge. A failed count is no pointer.
    n="$(timeout 5 head -c "$_DG_RULES_POINTER_MAX_SCAN" -- "$real" | wc -l)" || continue
    # The repository's file rules its own conventions (steps 2 to 6), never
    # the rest of the system prompt; and an @include out of the tree (the
    # owner's ~/.config, an absolute path) is not the repository's to give.
    if (( n <= _DG_RULES_POINTER_MAX_LINES )); then
      printf 'Lee %s completo antes de nada; %s' "$name" "$_DG_RULES_POINTER_SCOPE"
    else
      printf "%s tiene %d líneas: corre grep -n '^#' %s y lee solo las secciones de lo que vas a tocar; %s" \
        "$name" "$n" "$name" "$_DG_RULES_POINTER_SCOPE"
    fi
    return 0
  done
  return 0
}

# _dg_rules_render_core <core_file> <root>: the core with step 1 filled in, or
# dropped and the steps renumbered, into _dg_rules_core.
_dg_rules_render_core() {
  local core="$1" root="$2" pointer line out='' n=0
  _dg_rules_read "$core" core || return 2
  pointer="$(dg_rules_pointer "$root")"
  while IFS= read -r line; do
    if [[ "$line" == *"$_DG_RULES_PLACEHOLDER"* ]]; then
      [[ -n "$pointer" ]] || continue
      # Quoted replacement: bash 5.2 expands `&` in an unquoted one.
      line="${line//"$_DG_RULES_PLACEHOLDER"/"$pointer"}"
    fi
    if [[ "$line" =~ ^[0-9]+\.\ (.*)$ ]]; then
      n=$(( n + 1 ))
      line="$n. ${BASH_REMATCH[1]}"
    fi
    out+="$line"$'\n'
  done <<<"${_dg_rules_text%$'\n'}"
  _dg_rules_core="${out%$'\n'}"
  return 0
}

dg_rules_resolve() {
  local core="$1" account="$2" ids="$3" root="$4" dir id out=''
  local -a list=()
  local -A done_ids=()
  unset DG_CODE_RULES
  if [[ -n "$core" ]]; then
    _dg_rules_render_core "$core" "$root" || return 2
    out="$_dg_rules_core"
  fi
  dir="$(dg_rules_dir)"
  IFS=, read -r -a list <<<"$ids"
  for id in ${list[@]+"${list[@]}"}; do
    [[ -n "$id" && -z "${done_ids[$id]:-}" ]] || continue
    done_ids[$id]=1
    dg_rules_load_pack "$dir" "$id" || return 2
    if [[ ",$_DG_PACK_ACCOUNTS," != *",$account,"* ]]; then
      _dg_rules_err "$id: the account '$account' may not use this pack (accounts: $_DG_PACK_ACCOUNTS)"
      return 2
    fi
    out="${out:+$out$'\n\n'}${_dg_rules_text%$'\n'}"
  done
  DG_CODE_RULES="$out"
  return 0
}

dg_rules_add() {
  local dir="$1" id="$2" from="$3" accounts="$4" remotes="$5" src sha tmp
  if [[ ! "$id" =~ $_DG_MCP_RE_ID ]]; then
    _dg_rules_err "'$id': not a valid pack id (^[a-z][a-z0-9-]{0,31}$)"; return 2
  fi
  _dg_rules_valid_list "$accounts" "$_DG_ACCOUNT_NAME_RE" ||
    { _dg_rules_err "$id: --accounts must list at least one valid account name, separated by commas"; return 2; }
  if [[ -n "$remotes" ]] && ! { _dg_rules_valid_remotes "$remotes" && _dg_rules_remotes_agree "$remotes"; }; then
    _dg_rules_err "$id: --remotes must be patterns separated by commas, from A-Z a-z 0-9 and . _ : / @ ^ \$ | ( ) ? + * [ ] -, valid as a regex the same way in POSIX ERE and in Python (no [: or [. classes; python3 checks it)"
    return 2
  fi
  [[ -f "$from" ]] || { _dg_rules_err "$id: $from is not a file"; return 2; }
  src="$(readlink -f -- "$from")" || { _dg_rules_err "$id: cannot resolve $from"; return 2; }
  if [[ "$src" == *["$_dg_rules_ctl"]* || "$src" == *$'\n'* ]]; then
    _dg_rules_err "$id: the source path has a control character"; return 2
  fi
  _dg_rules_read "$src" "$id" || return 2
  sha="$(_dg_rules_sha "$_dg_rules_text")" || { _dg_rules_err "$id: sha256sum failed"; return 2; }

  # The directory is created private and checked as a run would check it; a
  # symlink in place of the pack directory is refused, never written through.
  ( umask 077; mkdir -p -- "$dir" ) || { _dg_rules_err "$id: cannot create $dir"; return 2; }
  _dg_rules_private "$dir" d "$id" || return 2
  if [[ -L "$dir/$id" ]]; then
    _dg_rules_err "$id: $dir/$id is a symlink"; return 2
  fi
  [[ -d "$dir/$id" ]] || ( umask 077; mkdir -- "$dir/$id" ) ||
    { _dg_rules_err "$id: cannot create $dir/$id"; return 2; }
  _dg_rules_private "$dir/$id" d "$id" || return 2

  # Each file through a temporary and rename(2), which replaces a symlink
  # rather than following it. rules.md first: until `pack` is replaced the old
  # sha no longer matches, and a run in between is refused, not misled.
  tmp="$(umask 077; mktemp -p "$dir/$id" .rules.XXXXXXXX)" || { _dg_rules_err "$id: mktemp failed"; return 2; }
  printf '%s' "$_dg_rules_text" >"$tmp" && mv -T -- "$tmp" "$dir/$id/rules.md" ||
    { rm -f -- "$tmp"; _dg_rules_err "$id: cannot write rules.md"; return 2; }
  tmp="$(umask 077; mktemp -p "$dir/$id" .pack.XXXXXXXX)" || { _dg_rules_err "$id: mktemp failed"; return 2; }
  {
    printf 'accounts\t%s\n' "$accounts"
    [[ -z "$remotes" ]] || printf 'remotes\t%s\n' "$remotes"
    printf 'source\t%s\n' "$src"
    printf 'sha256\t%s\n' "$sha"
  } >"$tmp" && mv -T -- "$tmp" "$dir/$id/pack" ||
    { rm -f -- "$tmp"; _dg_rules_err "$id: cannot write pack"; return 2; }
  _DG_PACK_SHA="$sha"
  return 0
}
