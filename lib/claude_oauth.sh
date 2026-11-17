#!/usr/bin/env bash
# The OAuth login Claude Code saved for an MCP server, reduced to ONE entry.
#
# An MCP entry with transport http or sse and no env_passthrough has no token
# of its own: the only login it can use is the one Claude Code saved, on Linux
# in ~/.claude/.credentials.json, under `mcpOAuth`. Claude Code keys those
# entries as `<serverName>|<16 hex>`, the hash being one it computes from the
# server's configuration; the key text is NOT `<name>|<url>` and cannot be
# rebuilt from the pair a run asks for. So the lookup matches on the entry's
# own `serverName` and `serverUrl` fields and then keeps the key it found under,
# because that key is what Claude Code looks the entry up by. The sandbox hides
# HOME, so without a copy the child starts with zero tools and nothing says why.
#
# What crosses into the sandbox is a MINIMAL store: one object under that
# original key, with the server's identity, the access token and its expiry.
# Nothing else from ~/.claude or ~/.claude.json — no Anthropic token, no other
# server's token, no history, no settings — and, deliberately, NO refresh token:
#
#   A refresh token may be rotated by the authorization server on use. If the
#   child refreshed with the user's refresh token and this store is thrown away
#   at the end of the run, the user's ORIGINAL login would be invalidated, and
#   the next interactive Claude Code session would have to log in again. So
#   this module passes an access token that is still valid and refuses, before
#   the run and before the provider key is read, when it is expired or close to
#   expiring. The message tells the user to open Claude Code once, which
#   refreshes it through the flow that is allowed to write the real file.
#
# Which fields are kept, and why only those: `serverName`, `serverUrl` (the
# identity the rest of the store is checked against), `accessToken`, and
# `expiresAt` when the file has one. That is all Claude Code needs to send the
# bearer token without refreshing: with an `expiresAt` in the future there is
# nothing to renew, and an entry written without `expiresAt` has no deadline to
# compare against, so the token is used as it is — the real file already holds
# such entries, so that is a shape Claude Code accepts. The rest of the entry
# only feeds a refresh or a new authorization: `refreshToken`, `clientSecret`,
# `clientId`, `scope`, `issuer`, `redirectUri` and `discoveryState` stay behind.
# (`clientId` is left out too: the real file has entries without one, so its
# absence is something Claude Code already handles.)
#
# The store is written by bin/delegate inside the run's private stub directory
# (a fresh mktemp directory, mode 700, removed on every exit path; the store
# subdirectory is 700 and the file 600) and mounted read-only at
# $HOME/.claude/.credentials.json inside the sandbox. Nothing ever writes back.
#
# Contract, implemented here and used by bin/delegate:
#
#   dg_oauth_source_file
#     Prints $HOME/.claude/.credentials.json. No other location is consulted.
#
#   dg_oauth_minimal_json <credentials-file> <id<TAB>url>...
#     Prints `{"mcpOAuth":{"<key>":{...}}}` on stdout with only the entry each
#     pair names, under the ORIGINAL key it had in <credentials-file>: Claude
#     Code looks the entry up by that key, so it is copied and never rebuilt.
#     An entry matches when its `serverName` equals the id and its `serverUrl`
#     equals the pair's url — never by the key text. If several match, the one
#     with the latest `expiresAt` wins; an entry without `expiresAt` never
#     expires, so it wins over any dated one. Returns 2 with a `delegate: ...`
#     message on stderr when the file, the section, an entry, or a still-valid
#     access token is missing. Never prints a token anywhere but the returned
#     JSON.
#
#   dg_oauth_write_store <dir> <json>
#     Creates <dir> (mode 700) and <dir>/.credentials.json (mode 600) with
#     <json>. Returns non-zero on any filesystem error.
#
# The parser is a small JSON scanner, not a permissive grep: it walks the top
# level object and each entry object, skipping strings and nested containers.
# That is enough for a file another program generates, and it keeps this out of
# a runtime dependency on jq or Python (delegate itself has none).
#
# Everything this file copies into the store is checked to be a string with no
# quote, backslash or control character. The output JSON is assembled by
# concatenation, the same way lib/mcp.sh assembles --mcp-config, and that is
# sound only because of this check.

_dg_oauth_pos=0
_dg_oauth_value=''
_dg_oauth_string=''
_dg_oauth_walk_pos=0
_dg_oauth_walk_key=''
_dg_oauth_walk_value=''
_dg_oauth_found_key=''
_dg_oauth_found_entry=''
_dg_oauth_found_expires=''

# Five minutes. A token that expires earlier than this is treated as expired:
# the run is about to start, and a server that disconnects mid-run is the
# failure this check exists to prevent.
_DG_OAUTH_MIN_TTL_MS=$(( 5 * 60 * 1000 ))

# ─── a JSON scanner, just enough for the credentials file ───────────────────

_dg_oauth_skip_ws() {
  # <string> <index>: sets _dg_oauth_pos after the whitespace.
  local s="$1" i="$2"
  while (( i < ${#s} )); do
    case "${s:i:1}" in
      ' '|$'\t'|$'\n'|$'\r') i=$(( i + 1 )) ;;
      *) break ;;
    esac
  done
  _dg_oauth_pos=$i
}

_dg_oauth_skip_string() {
  # <string> <index of the opening quote>: sets _dg_oauth_pos after the closing
  # quote. A backslash escapes the next character, so an escaped quote does not
  # end the string.
  local s="$1" i="$2" c
  i=$(( i + 1 ))
  while (( i < ${#s} )); do
    c="${s:i:1}"
    if [[ "$c" == '\' ]]; then
      i=$(( i + 2 ))
      continue
    fi
    if [[ "$c" == '"' ]]; then
      _dg_oauth_pos=$(( i + 1 ))
      return 0
    fi
    i=$(( i + 1 ))
  done
  return 1
}

_dg_oauth_skip_container() {
  # <string> <index of { or [>: sets _dg_oauth_pos after the matching close.
  local s="$1" i="$2" open='' close='' depth=1 c j
  open="${s:i:1}"
  case "$open" in
    '{') close='}' ;;
    '[') close=']' ;;
    *) return 1 ;;
  esac
  j=$(( i + 1 ))
  while (( j < ${#s} )); do
    c="${s:j:1}"
    case "$c" in
      '"') _dg_oauth_skip_string "$s" "$j" || return 1; j=$_dg_oauth_pos ;;
      "$open") depth=$(( depth + 1 )); j=$(( j + 1 )) ;;
      "$close")
        depth=$(( depth - 1 ))
        j=$(( j + 1 ))
        if (( depth == 0 )); then
          _dg_oauth_pos=$j
          return 0
        fi
        ;;
      *) j=$(( j + 1 )) ;;
    esac
  done
  return 1
}

_dg_oauth_skip_value() {
  # <string> <index at the first byte of a JSON value>: sets _dg_oauth_pos after
  # it. The scalar branch stops at a delimiter without consuming it.
  local s="$1" i="$2" c
  case "${s:i:1}" in
    '"') _dg_oauth_skip_string "$s" "$i" ;;
    '{'|'[') _dg_oauth_skip_container "$s" "$i" ;;
    *)
      while (( i < ${#s} )); do
        c="${s:i:1}"
        case "$c" in
          ','|'}'|']'|[[:space:]]) break ;;
        esac
        i=$(( i + 1 ))
      done
      _dg_oauth_pos=$i
      return 0
      ;;
  esac
}

_dg_oauth_member() {
  # <object-string> <key>: 0 when the key is a TOP-LEVEL member, with its raw
  # value (quotes included for a string) in _dg_oauth_value. A key inside a
  # nested object does not match.
  local s="$1" want="$2" i=0 c kstart kend key vstart
  _dg_oauth_skip_ws "$s" 0
  i=$_dg_oauth_pos
  [[ "${s:i:1}" == '{' ]] || return 1
  i=$(( i + 1 ))
  while :; do
    _dg_oauth_skip_ws "$s" "$i"
    i=$_dg_oauth_pos
    c="${s:i:1}"
    [[ "$c" == '}' ]] && return 1
    [[ "$c" == '"' ]] || return 1
    kstart=$(( i + 1 ))
    _dg_oauth_skip_string "$s" "$i" || return 1
    kend=$(( _dg_oauth_pos - 1 ))
    key="${s:kstart:kend-kstart}"
    i=$_dg_oauth_pos
    _dg_oauth_skip_ws "$s" "$i"
    i=$_dg_oauth_pos
    [[ "${s:i:1}" == ':' ]] || return 1
    i=$(( i + 1 ))
    _dg_oauth_skip_ws "$s" "$i"
    i=$_dg_oauth_pos
    vstart=$i
    _dg_oauth_skip_value "$s" "$i" || return 1
    i=$_dg_oauth_pos
    if [[ "$key" == "$want" ]]; then
      _dg_oauth_value="${s:vstart:i-vstart}"
      return 0
    fi
    _dg_oauth_skip_ws "$s" "$i"
    i=$_dg_oauth_pos
    [[ "${s:i:1}" == ',' ]] || return 1
    i=$(( i + 1 ))
  done
}

_dg_oauth_walk_start() {
  # <object-string>: points the member cursor at the first member. 1 when the
  # value is not an object.
  local s="$1"
  _dg_oauth_skip_ws "$s" 0
  _dg_oauth_walk_pos=$_dg_oauth_pos
  [[ "${s:_dg_oauth_walk_pos:1}" == '{' ]] || return 1
  _dg_oauth_walk_pos=$(( _dg_oauth_walk_pos + 1 ))
  return 0
}

_dg_oauth_walk_next() {
  # <object-string>: 0 with the next top-level member in _dg_oauth_walk_key and
  # its raw value in _dg_oauth_walk_value; 1 at the closing brace; 2 when the
  # object is malformed. Iterating the members, not asking for one known key, is
  # what lets the lookup go by the entry's fields: the key is Claude Code's own
  # hash and the pair a run asks for cannot produce it.
  local s="$1" i c kstart kend vstart
  i=$_dg_oauth_walk_pos
  _dg_oauth_skip_ws "$s" "$i"
  i=$_dg_oauth_pos
  c="${s:i:1}"
  if [[ "$c" == '}' ]]; then
    _dg_oauth_walk_pos=$(( i + 1 ))
    return 1
  fi
  [[ "$c" == '"' ]] || return 2
  kstart=$(( i + 1 ))
  _dg_oauth_skip_string "$s" "$i" || return 2
  kend=$(( _dg_oauth_pos - 1 ))
  _dg_oauth_walk_key="${s:kstart:kend-kstart}"
  i=$_dg_oauth_pos
  _dg_oauth_skip_ws "$s" "$i"
  i=$_dg_oauth_pos
  [[ "${s:i:1}" == ':' ]] || return 2
  i=$(( i + 1 ))
  _dg_oauth_skip_ws "$s" "$i"
  i=$_dg_oauth_pos
  vstart=$i
  _dg_oauth_skip_value "$s" "$i" || return 2
  i=$_dg_oauth_pos
  _dg_oauth_walk_value="${s:vstart:i-vstart}"
  _dg_oauth_skip_ws "$s" "$i"
  i=$_dg_oauth_pos
  case "${s:i:1}" in
    ',') _dg_oauth_walk_pos=$(( i + 1 )) ;;
    '}') _dg_oauth_walk_pos=$i ;;   # the next call reports the end
    *) return 2 ;;
  esac
  return 0
}

_dg_oauth_find_entry() {
  # <section> <id> <url>: 0 when an entry of the section has serverName == id
  # and serverUrl == url, with its original key in _dg_oauth_found_key, its raw
  # object in _dg_oauth_found_entry and its raw expiresAt (empty when absent) in
  # _dg_oauth_found_expires. 1 when none matches; 2 when the section cannot be
  # walked; 3 when the only matches are entries whose key cannot be pasted into
  # JSON. The key is copied verbatim and never rebuilt: it is Claude Code's
  # `<serverName>|<hash>`, and Claude Code looks the entry up by it.
  local s="$1" want_id="$2" want_url="$3"
  local wrc=0 key entry name url exp norm best_norm='' best_set='' bad=0
  _dg_oauth_found_key=''
  _dg_oauth_found_entry=''
  _dg_oauth_found_expires=''
  _dg_oauth_walk_start "$s" || return 2
  while :; do
    _dg_oauth_walk_next "$s"
    wrc=$?
    (( wrc == 0 )) || break
    key="$_dg_oauth_walk_key"
    entry="$_dg_oauth_walk_value"
    [[ "${entry:0:1}" == '{' ]] || continue
    _dg_oauth_member "$entry" serverName || continue
    _dg_oauth_raw_string "$_dg_oauth_value" || continue
    name="$_dg_oauth_string"
    [[ "$name" == "$want_id" ]] || continue
    _dg_oauth_member "$entry" serverUrl || continue
    _dg_oauth_raw_string "$_dg_oauth_value" || continue
    url="$_dg_oauth_string"
    [[ "$url" == "$want_url" ]] || continue

    exp=''
    if _dg_oauth_member "$entry" expiresAt; then
      exp="$_dg_oauth_value"
    fi
    # Ranking: a non-expiring entry (no expiresAt) beats every dated one; among
    # dated ones the largest value wins; a value that is not a number ranks as
    # zero, so a usable entry is preferred and the caller still refuses if it is
    # the only match. The first entry wins a tie, which keeps the choice stable.
    if [[ -z "$exp" ]]; then
      norm="$(( 2 ** 62 ))"
    elif [[ "$exp" =~ ^[0-9]{1,15}$ ]]; then
      norm="$(( 10#$exp ))"
      # The same read the caller makes: a value too small to be milliseconds is
      # read as seconds, so both rank against the same clock.
      (( norm < 100000000000 )) && norm=$(( norm * 1000 ))
    else
      norm=0
    fi
    if [[ -z "$best_set" ]] || (( norm > best_norm )); then
      # Only a key made of characters that need no escaping can be concatenated
      # into the store; anything else is skipped, not copied.
      if [[ "$key" =~ ^[A-Za-z0-9._~+|=-]+$ ]]; then
        best_set=1
        best_norm="$norm"
        _dg_oauth_found_key="$key"
        _dg_oauth_found_entry="$entry"
        _dg_oauth_found_expires="$exp"
      else
        bad=1
      fi
    fi
  done
  (( wrc == 1 )) || return 2
  [[ -n "$best_set" ]] && return 0
  (( bad )) && return 3
  return 1
}

_dg_oauth_raw_string() {
  # <value token>: 0 when it is a JSON string whose body needs no escaping, with
  # the body in _dg_oauth_string. Refusing the quote, the backslash and control
  # characters is what keeps the concatenated output valid JSON.
  local v="$1" body='' i c
  [[ "$v" == \"*\" && ${#v} -ge 2 ]] || return 1
  body="${v:1:${#v}-2}"
  for (( i = 0; i < ${#body}; i++ )); do
    c="${body:i:1}"
    case "$c" in
      '\'|'"') return 1 ;;
    esac
    if [[ "$c" == [[:cntrl:]] ]]; then
      return 1
    fi
  done
  _dg_oauth_string="$body"
  return 0
}

# ─── the contract ───────────────────────────────────────────────────────────

dg_oauth_source_file() {
  printf '%s/.claude/.credentials.json\n' "${HOME:-/nonexistent}"
}

_dg_oauth_err() {
  printf 'delegate: %s\n' "$*" >&2
}

dg_oauth_minimal_json() {
  if (( $# < 2 )); then
    _dg_oauth_err 'dg_oauth_minimal_json: usage: <credentials-file> <id<TAB>url>...'
    return 2
  fi

  local file="$1"; shift
  local s='' section='' entry='' pair='' id='' url='' key='' expires_raw=''
  local access='' expires_ms=0 has_expiry=0 now_ms=0 ttl_s=0 frc=0
  local fields='' items='' isep=''
  local -a pairs=("$@")

  if [[ ! -f "$file" || ! -r "$file" ]]; then
    _dg_oauth_err "no encuentro el login OAuth que Claude Code guardó en $file;" \
      "un servidor MCP pedido lo necesita. Abre Claude Code una vez y autentícalo con /mcp"
    return 2
  fi
  # Not `<`: under `set -e` a read failure would kill the caller with no
  # message. The file is the user's own, not hostile input, but the failure
  # still has to be explained.
  s="$(<"$file")" || {
    _dg_oauth_err "no pude leer el login OAuth de Claude Code en $file"
    return 2
  }

  if ! _dg_oauth_member "$s" mcpOAuth; then
    _dg_oauth_err "el login OAuth de Claude Code en $file no tiene la sección mcpOAuth;" \
      "abre Claude Code una vez y autentica el servidor con /mcp"
    return 2
  fi
  section="$_dg_oauth_value"
  if [[ "${section:0:1}" != '{' ]]; then
    _dg_oauth_err "la sección mcpOAuth del login OAuth de Claude Code no es un objeto"
    return 2
  fi

  now_ms=$(( $(date +%s) * 1000 ))
  for pair in "${pairs[@]}"; do
    id="${pair%%$'\t'*}"
    url="${pair#*$'\t'}"
    # The same charsets lib/mcp.sh validates. They are repeated here so this
    # file can be read on its own, and because the id and the url are pasted
    # into JSON and into the key without escaping.
    [[ "$id" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || {
      _dg_oauth_err "el servidor MCP '$id' no tiene un id que pueda usar en el store OAuth"
      return 2
    }
    [[ "$url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] || {
      _dg_oauth_err "el servidor MCP $id no tiene una url https que pueda usar en el store OAuth"
      return 2
    }
    _dg_oauth_find_entry "$section" "$id" "$url"
    frc=$?
    case "$frc" in
      0) ;;
      1)
        _dg_oauth_err "el login OAuth de Claude Code no tiene una entrada para el servidor MCP $id ($url);" \
          "abre Claude Code una vez y autentícalo con /mcp"
        return 2
        ;;
      3)
        _dg_oauth_err "la entrada OAuth del servidor MCP $id tiene una clave que no puedo copiar al store;" \
          "abre Claude Code una vez y autentícalo con /mcp"
        return 2
        ;;
      *)
        _dg_oauth_err "no pude recorrer la sección mcpOAuth del login OAuth de Claude Code en $file"
        return 2
        ;;
    esac
    key="$_dg_oauth_found_key"
    entry="$_dg_oauth_found_entry"
    # The entry was found by its fields, so the identity is the requested pair;
    # the expiry is the one the file gave for this entry.
    expires_raw="$_dg_oauth_found_expires"

    if ! _dg_oauth_member "$entry" accessToken || ! _dg_oauth_raw_string "$_dg_oauth_value"; then
      _dg_oauth_err "la entrada OAuth del servidor MCP $id no tiene un accessToken utilizable;" \
        "abre Claude Code una vez para renovar el login y vuelve a pedir la corrida"
      return 2
    fi
    access="$_dg_oauth_string"
    if [[ -z "$access" ]]; then
      _dg_oauth_err "la entrada OAuth del servidor MCP $id tiene un accessToken vacío;" \
        "abre Claude Code una vez para renovar el login y vuelve a pedir la corrida"
      return 2
    fi

    has_expiry=0
    expires_ms=0
    if [[ -n "$expires_raw" ]]; then
      if [[ ! "$expires_raw" =~ ^[0-9]{1,15}$ ]]; then
        _dg_oauth_err "la entrada OAuth del servidor MCP $id tiene un expiresAt que no puedo leer;" \
          "abre Claude Code una vez para renovar el login y vuelve a pedir la corrida"
        return 2
      fi
      has_expiry=1
      expires_ms="$(( 10#${expires_raw} ))"
      # Claude Code writes milliseconds. A value too small to be one is read as
      # seconds so an older shape still works; nothing legitimate falls in
      # between.
      if (( expires_ms < 100000000000 )); then
        expires_ms=$(( expires_ms * 1000 ))
      fi
    fi
    # No expiresAt means nothing to expire against: the token is used as it is,
    # which is how Claude Code reads an entry it saved without a deadline.
    if (( has_expiry )) && (( expires_ms <= now_ms + _DG_OAUTH_MIN_TTL_MS )); then
      ttl_s=$(( (expires_ms - now_ms) / 1000 ))
      if (( ttl_s <= 0 )); then
        _dg_oauth_err "el access token OAuth del servidor MCP $id ya venció;" \
          "abre Claude Code una vez para renovarlo y vuelve a pedir la corrida"
      else
        _dg_oauth_err "el access token OAuth del servidor MCP $id vence en $ttl_s segundos;" \
          "abre Claude Code una vez para renovarlo y vuelve a pedir la corrida"
      fi
      return 2
    fi

    # Only what is needed to send the bearer token and to keep the identity the
    # rest of the store is keyed by. Nothing else from the entry crosses: not
    # the refresh token, not the client registration, not the scope. The key is
    # the entry's ORIGINAL one (`<serverName>|<hash>`): Claude Code looks the
    # entry up by that key, so it is copied verbatim, never rebuilt.
    fields="\"serverName\":\"$id\",\"serverUrl\":\"$url\",\"accessToken\":\"$access\""
    (( has_expiry )) && fields="$fields,\"expiresAt\":$expires_ms"
    items="$items$isep\"$key\":{$fields}"
    isep=','
  done

  printf '{"mcpOAuth":{%s}}\n' "$items"
  return 0
}

dg_oauth_write_store() {
  # <dir> <json>: the directory is private and the file is 600 from creation,
  # not after it: a token on disk readable by others for even an instant is the
  # failure this mode exists to prevent.
  if (( $# != 2 )); then
    _dg_oauth_err 'dg_oauth_write_store: usage: <dir> <json>'
    return 2
  fi
  local dir="$1" json="$2"
  (
    umask 077
    set -C   # never write through, or over, a file already at the path
    mkdir -m 700 -- "$dir" || exit 1
    printf '%s\n' "$json" >"$dir/.credentials.json" || exit 1
    chmod 600 -- "$dir/.credentials.json" || exit 1
  ) || {
    _dg_oauth_err "no pude escribir el login OAuth temporal del sandbox en $dir"
    return 1
  }
  return 0
}
