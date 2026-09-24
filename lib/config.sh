#!/usr/bin/env bash
# Provider configuration: parsing and validation.
#
# A provider is data, not code. Each one is a file in providers.d/ with one
# `key<TAB>value` pair per line. The file is PARSED, never sourced and never
# eval'd: a provider file must not be able to inject shell, environment
# variables or MCP servers. That is invariant 6 in CLAUDE.md.
#
# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT — implement exactly this surface. bin/delegate depends on it.
# ─────────────────────────────────────────────────────────────────────────────
#
# dg_config_parse <file> <provider_id>
#   Validates the file and prints one `key<TAB>value` line per recognised key
#   to stdout, sorted by key. Omits keys the file does not set: defaults are
#   the caller's business, not the parser's.
#   Returns 0 on success. On ANY validation problem, prints
#   `delegate: <provider_id>: <reason>` to stderr and returns 2, having
#   printed nothing to stdout.
#
# dg_config_providers <dir>
#   Prints the provider ids found in <dir>, sorted, one per line. A file whose
#   name is not a valid provider id is skipped silently — it is not config.
#   Does not validate the contents.
#
# ─────────────────────────────────────────────────────────────────────────────
# GRAMMAR
# ─────────────────────────────────────────────────────────────────────────────
#
#   - Blank lines, and lines whose first non-blank character is `#`, are ignored.
#   - Every other line is `key`, one TAB, `value`. Leading and trailing spaces
#     and tabs around the whole line are trimmed before splitting; the value
#     keeps any interior spaces.
#   - A line with no TAB, or with more than one TAB, is an error. This is what
#     keeps a value from smuggling a second field in.
#   - An empty value is an error. To leave a key unset, omit the line.
#   - An unknown key is an error. So is a duplicate key. Silently ignoring
#     either would let a typo disable a security-relevant setting.
#
# KEYS — the complete set. Anything else is rejected.
#
#   required:
#     endpoint         Anthropic-compatible base URL. Must match
#                      ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$
#                      Plain http is refused: the key travels on this connection.
#     auth_env         Name of the env var the key is passed in. Either
#                      ANTHROPIC_API_KEY (sent as `x-api-key`) or
#                      ANTHROPIC_AUTH_TOKEN (sent as `Authorization: Bearer`),
#                      whichever the endpoint accepts. An allowlist of those two
#                      names, not a pattern: a config file must not be able to
#                      aim the key at an arbitrary variable name.
#     key_dir          Directory holding the key files. Absolute, or starting
#                      with `~/`. Must not contain `..` or a newline.
#     model            Main model id. ^[A-Za-z0-9._\[\]:@-]+$ (brackets matter:
#                      `deepseek-flash[1m]` is a real model id).
#
#   optional:
#     small_model      Model for cheap internal calls. Same charset as `model`.
#     key_pattern      Basename template for a per-account key file. Exactly one
#                      `%s`, and otherwise only [A-Za-z0-9._-]. Caller's default
#                      is `api_key.%s`.
#     default_account  ^[a-z0-9][a-z0-9_-]{0,31}$
#     timeout          Seconds, integer, 1..86400.
#     caps             Comma-separated subset of: read,edit,bash,mcp,mcp-write
#                      No duplicates, no empty elements. `read` is implied but
#                      may be listed. This is the provider's CEILING, enforced in
#                      lib/permissions.sh.
#     residency        Free text, informational only, shown by `delegate
#                      providers`. Charset ^[A-Za-z0-9 ,.()/_-]+$, max 120 chars.
#                      Nothing is enforced from it — see the note on security
#                      theatre in CLAUDE.md.
#
# The provider id itself (the filename, and the subcommand the user types) must
# match ^[a-z][a-z0-9-]{0,31}$.
#
# ─────────────────────────────────────────────────────────────────────────────
# Tests go in test/config.test.sh. Every rejection rule above needs a case that
# proves it rejects, with the exit code checked, not just the message.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# IMPLEMENTATION
# ─────────────────────────────────────────────────────────────────────────────
#
# This file is sourced by bin/delegate, so it sets no shell options: a `set -e`
# or `set -u` from here would change how the caller behaves. It owns only the
# `_dg_config_*` and `_DG_CONFIG_*` names.
#
# Nothing in here evaluates a config file. Every value travels as data: braces,
# dollar signs and quotes are just bytes that the character classes below accept
# or reject.

# Checked once the whole file has been read, so a missing key is reported the
# same way whether the file is empty or simply omits it.
_DG_CONFIG_REQUIRED=(endpoint auth_env key_dir model)

# Vocabulary of a `caps` value. lib/permissions.sh enforces the ceiling; the
# parser only checks that the value is built out of these words.
_DG_CONFIG_CAPS=(read edit bash mcp mcp-write)

# Error channel of the value validator: set on rejection, printed by the caller
# that knows the provider id. Keeps the `delegate: <id>: <reason>` format in one
# place instead of repeated in every rule.
_dg_config_reason=''

_dg_config_err() {
  # <provider_id> <reason>
  printf 'delegate: %s: %s\n' "$1" "$2" >&2
}

_dg_config_usage() {
  # <function> <signature>
  printf 'delegate: config: usage: %s %s\n' "$1" "$2" >&2
}

_dg_config_valid_id() {
  # Provider ids are filenames, subcommands and error-message prefixes, so they
  # stay in the one shape that needs no quoting anywhere.
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]]
}

_dg_config_ltrim() {
  # Leading spaces and tabs only, as the grammar says. Not `[:space:]`: a `\r`
  # from a CRLF file is left in the value so the key's own rule rejects it
  # loudly, instead of a mangled value sliding through.
  local s="$1"
  printf '%s' "${s#"${s%%[![:blank:]]*}"}"
}

_dg_config_rtrim() {
  local s="$1"
  printf '%s' "${s%"${s##*[![:blank:]]}"}"
}

_dg_config_validate() {
  # <lineno> <key> <value>: 0 when the value is valid; 1 after setting
  # _dg_config_reason. Never prints, so the caller owns the message prefix.
  # Values are never echoed back: a provider file must not be able to leak
  # whatever the user pasted into it.
  local lineno="$1" key="$2" value="$3"
  local re='' rest='' part='' seen_caps=' '
  _dg_config_reason=''

  case "$key" in
    endpoint)
      # https only: the key travels on this connection.
      re='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$'
      [[ "$value" =~ $re ]] ||
        { _dg_config_reason="line $lineno: endpoint must be an https:// URL"; return 1; }
      ;;
    auth_env)
      # An allowlist of exactly two names, not a pattern: a config file must not
      # be able to aim the key at an arbitrary environment variable. Two rather
      # than one because the harness sends them differently and the endpoint
      # decides which it accepts — ANTHROPIC_API_KEY goes out as `x-api-key`,
      # ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer`. DeepSeek takes the
      # first, Qwen Cloud the second; pinning this to one name left qwen
      # unusable with a message that blamed the config file.
      case "$value" in
        ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN) ;;
        *) _dg_config_reason="line $lineno: auth_env must be ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
           return 1 ;;
      esac
      ;;
    key_dir)
      [[ "$value" == /* || "$value" == '~/'* ]] ||
        { _dg_config_reason="line $lineno: key_dir must be absolute or start with ~/"; return 1; }
      [[ "$value" != *'..'* ]] ||
        { _dg_config_reason="line $lineno: key_dir must not contain '..'"; return 1; }
      [[ "$value" != *$'\n'* ]] ||
        { _dg_config_reason="line $lineno: key_dir must not contain a newline"; return 1; }
      ;;
    model|small_model)
      # Brackets are part of a real model id (`deepseek-flash[1m]`), hence the
      # bracket expression that starts with a literal `]` and ends with `-`.
      re='^[][A-Za-z0-9._:@-]+$'
      [[ "$value" =~ $re ]] ||
        { _dg_config_reason="line $lineno: $key allows only A-Za-z0-9 and . _ : @ - [ ]"; return 1; }
      ;;
    key_pattern)
      # Exactly one %s: drop every occurrence and see how much disappeared.
      rest="${value//%s/}"
      (( ${#value} - ${#rest} == 2 )) ||
        { _dg_config_reason="line $lineno: key_pattern must contain exactly one %s"; return 1; }
      [[ -z "$rest" || "$rest" != *[!A-Za-z0-9._-]* ]] ||
        { _dg_config_reason="line $lineno: key_pattern allows only A-Za-z0-9 and . _ - besides its %s"; return 1; }
      ;;
    default_account)
      re='^[a-z0-9][a-z0-9_-]{0,31}$'
      [[ "$value" =~ $re ]] ||
        { _dg_config_reason="line $lineno: default_account allows only lowercase, digits, _ and - (32 chars max)"; return 1; }
      ;;
    timeout)
      [[ "$value" =~ ^[0-9]+$ ]] ||
        { _dg_config_reason="line $lineno: timeout must be a whole number of seconds"; return 1; }
      rest="${value#"${value%%[!0]*}"}"   # leading zeros off, for the range test
      if [[ -z "$rest" || ${#rest} -gt 5 ]] || (( 10#$rest < 1 || 10#$rest > 86400 )); then
        _dg_config_reason="line $lineno: timeout must be between 1 and 86400"
        return 1
      fi
      ;;
    caps)
      # Comma-separated subset, no duplicates, no empty elements. Elements are
      # matched whole, so `edit` cannot be satisfied by `mcp-write`.
      rest="$value"
      while :; do
        part="${rest%%,*}"
        [[ -n "$part" ]] ||
          { _dg_config_reason="line $lineno: caps has an empty element"; return 1; }
        case " ${_DG_CONFIG_CAPS[*]} " in
          *" $part "*) ;;
          *) _dg_config_reason="line $lineno: unknown caps element '$part'"; return 1 ;;
        esac
        case " $seen_caps " in
          *" $part "*) _dg_config_reason="line $lineno: duplicate caps element '$part'"; return 1 ;;
        esac
        seen_caps="$seen_caps$part "
        [[ "$rest" == *,* ]] || break
        rest="${rest#*,}"
      done
      ;;
    residency)
      # Informational only: shown by `delegate providers`, enforced nowhere.
      re='^[A-Za-z0-9 ,.()/_-]+$'
      [[ "$value" =~ $re ]] ||
        { _dg_config_reason="line $lineno: residency allows only letters, digits and space , . ( ) / _ -"; return 1; }
      (( ${#value} <= 120 )) ||
        { _dg_config_reason="line $lineno: residency must be at most 120 characters"; return 1; }
      ;;
  esac
  return 0
}

dg_config_parse() {
  if (( $# != 2 )); then
    _dg_config_usage dg_config_parse '<file> <provider_id>'
    return 2
  fi

  local file="$1" provider="$2"
  local line='' key='' value='' stripped=''
  local lineno=0 ntabs=0 req=''
  local -a out=()
  local -A seen=() values=()

  if ! _dg_config_valid_id "$provider"; then
    _dg_config_err "$provider" 'invalid provider id'
    return 2
  fi
  if [[ ! -f "$file" || ! -r "$file" ]]; then
    _dg_config_err "$provider" "cannot read provider file: $file"
    return 2
  fi

  # Line by line, never sourced and never eval'd. Everything is validated before
  # a single byte reaches stdout, so a rejected file prints nothing on stdout.
  while IFS= read -r line || [[ -n "$line" ]]; do
    # `lineno=$(( lineno + 1 ))`, not `(( lineno++ ))`: a bare arithmetic
    # command returns 1 when the result is 0, and bin/delegate runs with `set -e`.
    lineno=$(( lineno + 1 ))

    line="$(_dg_config_ltrim "$line")"
    [[ -z "$line" || "$line" == '#'* ]] && continue

    stripped="${line//$'\t'/}"
    ntabs=$(( ${#line} - ${#stripped} ))
    if (( ntabs == 0 )); then
      _dg_config_err "$provider" "line $lineno: expected key<TAB>value, found no tab"
      return 2
    fi
    if (( ntabs > 1 )); then
      _dg_config_err "$provider" "line $lineno: expected key<TAB>value, found more than one tab"
      return 2
    fi

    key="${line%%$'\t'*}"
    # Trailing blanks come off the value rather than off the line: that keeps
    # `key<TAB>` an empty value (a rejection) instead of a tab-less line, and
    # still ignores whitespace a user left dangling at the end of the line.
    value="$(_dg_config_rtrim "${line#*$'\t'}")"
    if [[ -z "$value" ]]; then
      _dg_config_err "$provider" "line $lineno: empty value for key '$key'"
      return 2
    fi

    case "$key" in
      endpoint|auth_env|key_dir|model|small_model|key_pattern|default_account|timeout|caps|residency) ;;
      *) _dg_config_err "$provider" "line $lineno: unknown key '$key'"; return 2 ;;
    esac

    if [[ -n "${seen[$key]:-}" ]]; then
      _dg_config_err "$provider" "line $lineno: duplicate key '$key'"
      return 2
    fi
    seen[$key]=1

    if ! _dg_config_validate "$lineno" "$key" "$value"; then
      _dg_config_err "$provider" "$_dg_config_reason"
      return 2
    fi
    values[$key]="$value"
  done < "$file"

  for req in "${_DG_CONFIG_REQUIRED[@]}"; do
    if [[ -z "${seen[$req]:-}" ]]; then
      _dg_config_err "$provider" "missing required key: $req"
      return 2
    fi
  done

  # Only the keys the file set: defaults belong to the caller. Sorted so the
  # output does not depend on the order of the file or on bash's hash order.
  for key in "${!values[@]}"; do
    out+=("$key"$'\t'"${values[$key]}")
  done
  (( ${#out[@]} )) || return 0
  printf '%s\n' "${out[@]}" | LC_ALL=C sort
  return 0
}

dg_config_providers() {
  if (( $# != 1 )); then
    _dg_config_usage dg_config_providers '<dir>'
    return 2
  fi

  local dir="$1" path='' name=''
  # A directory that is not there holds no providers; the caller decides whether
  # that is worth complaining about.
  [[ -d "$dir" ]] || return 0

  for path in "$dir"/*; do
    [[ -f "$path" ]] || continue
    name="${path##*/}"
    _dg_config_valid_id "$name" || continue
    printf '%s\n' "$name"
  done | LC_ALL=C sort
  return 0
}
