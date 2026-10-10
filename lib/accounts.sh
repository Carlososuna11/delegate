#!/usr/bin/env bash
# Account (API key) management.
#
# The concrete file `<key_dir>/api_key.<account>` is the truth. The unsuffixed
# `<key_dir>/api_key` is only a compatibility projection: a symlink pointing at
# whichever account is active, kept because `cc-proveedor` is Python and reads
# that fixed path. Nothing in this file may print the contents of a key.
#
# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT — implement exactly this surface. bin/delegate depends on it.
# ─────────────────────────────────────────────────────────────────────────────
#
# dg_account_path <key_dir> <key_pattern> <account>
#   Prints the absolute path the account's key would live at. Expands a leading
#   `~/` in key_dir. Rejects (2) an account name that does not match
#   ^[a-z0-9][a-z0-9_-]{0,31}$ — refuse before building a path, so no caller can
#   be tricked into `../` or an absolute escape. Does not require the file to
#   exist.
#
# dg_account_list <key_dir> <key_pattern>
#   Prints existing account names, sorted, one per line. Derived from the files
#   actually present, matched against key_pattern with `%s` standing for the
#   name. Nothing on stdout and exit 0 when there are none.
#
# dg_account_active <key_dir> [key_pattern]
#   Prints the name of the active account by resolving the `api_key` symlink.
#   Returns 1 and prints nothing when `api_key` is missing, is not a symlink, or
#   points somewhere that is not a recognisable account file.
#   key_pattern defaults to `api_key.%s`. It is a parameter because a provider
#   file may set its own, and a hardcoded `api_key.*` here would report "no
#   active account" for a provider whose files are named anything else — while
#   `account list` listed them fine.
#
# dg_account_fingerprint <path>
#   Prints the first 8 hex characters of the file's sha256. This is the ONLY
#   thing any command may show about a key's contents. Returns 1 if unreadable.
#
# dg_account_validate <key_dir> <path>
#   Returns 0 only when ALL of these hold; otherwise prints
#   `delegate: <reason>` to stderr and returns 1:
#     - the fully resolved path is INSIDE the resolved key_dir (not equal to it,
#       and not merely prefix-matching a sibling directory name: resolve both
#       and compare path components)
#     - it is a regular file, after following symlinks
#     - its mode is exactly 600
#     - it is owned by the current user
#     - it is non-empty
#   Called immediately before a key is read. The containment check is what stops
#   a hand-edited symlink from turning `delegate` into a file-exfiltration tool.
#   On success it PRINTS the fully resolved path, and that is the path the caller
#   must open: every check above is made against the resolved file, so opening
#   the original argument again would re-follow the symlink and could land on a
#   different file than the one that was checked. A caller that only wants the
#   verdict discards stdout.
#
# dg_account_read <path>
#   Prints the key with surrounding whitespace, CR and LF stripped. The caller
#   captures it into a variable and passes it through the child's environment.
#   Assumes dg_account_validate already passed, and expects the path IT printed;
#   call it first.
#
# dg_account_activate <key_dir> <target>
#   Points `<key_dir>/api_key` at <target> ATOMICALLY: create a uniquely named
#   temporary symlink in key_dir, then `mv -T` it over `api_key`, so the path is
#   never absent. NEVER `ln -sfn`: with -f, coreutils unlinks the destination
#   before creating the replacement, and a concurrent reader in that window sees
#   nothing. Clean up the temp symlink on any failure. Invariant, see CLAUDE.md.
#
# dg_account_add <key_dir> <key_pattern> <account>
#   Reads the key from stdin. When stdin is a terminal, reads it with echo OFF
#   (`read -rs`) and prints a prompt to stderr; otherwise reads a line plainly,
#   so the command is scriptable. Creates key_dir if needed (mode 700). Writes
#   the file under `umask 077`. Refuses (1) if the account already exists, or if
#   what it read is empty. Must never echo, log or print the key.
#
# dg_account_remove <key_dir> <key_pattern> <account>
#   Deletes the key file. If it was the active one, also removes the now-dangling
#   `api_key` symlink and returns 10 to tell the caller no account is active any
#   more. DELIBERATELY does not promote another account: silently changing which
#   key gets billed is worse than having none active. Returns 1 if it does not
#   exist. Asks nothing — confirmation is the CLI layer's job, not this one's.
#
# dg_account_default_get <key_dir>
#   Prints the contents of `<key_dir>/default_account` (plain text, one name, not
#   a secret). Returns 1 if absent or if the name does not validate.
#
# dg_account_default_set <key_dir> <account>
#   Writes that file atomically (temp file plus `mv -T`), mode 644.
#
# ─────────────────────────────────────────────────────────────────────────────
# Tests go in test/accounts.test.sh. They must NEVER touch the real
# ~/.config/deepseek: every case builds its own key_dir under $TMP, which the
# harness creates and removes per test. At least one test has to prove the
# activation window never leaves `api_key` missing, and one has to prove a key
# whose mode is not 600, and one living outside key_dir, are both refused.
# ─────────────────────────────────────────────────────────────────────────────

# This file is a pure function library: it sets no shell options, so sourcing it
# never changes how bin/delegate (or a test suite) behaves. Every function reads
# its arguments with ${1:-} and is safe under `set -u`. Internal helpers are
# prefixed `_dg_account_` so they cannot collide with the public surface.

# Account names become a filename component and a path argument, so the rule is
# the narrowest one that is still usable: no dots, no slashes, no leading `-`,
# no uppercase. Rejecting here is what makes `../` unreachable.
_DG_ACCOUNT_NAME_RE='^[a-z0-9][a-z0-9_-]{0,31}$'

_dg_account_err() {
  printf 'delegate: %s\n' "$*" >&2
}

_dg_account_name_ok() {
  [[ ${1:-} =~ $_DG_ACCOUNT_NAME_RE ]]
}

# Expands a leading `~`/`~/`, makes the result absolute, drops trailing slashes.
# Nothing here resolves symlinks: callers that need canonical paths (validate,
# active) resolve both sides themselves.
_dg_account_dir() {
  local d="${1:-}"
  [[ -n $d ]] || return 1
  case $d in
    '~')   d="${HOME:-}" ;;
    '~/'*) d="${HOME:-}/${d#\~/}" ;;
  esac
  [[ -n $d ]] || return 1
  [[ $d == /* ]] || d="$PWD/$d"
  while [[ $d == */ && $d != / ]]; do d="${d%/}"; done
  printf '%s\n' "$d"
}

# Strips CR and surrounding whitespace from a key (never prints it).
_dg_account_trim() {
  local s="${1:-}"
  s="${s//$'\r'/}"
  while [[ $s == [[:space:]]* ]]; do s="${s#[[:space:]]}"; done
  while [[ $s == *[[:space:]] ]]; do s="${s%[[:space:]]}"; done
  printf '%s' "$s"
}

# True when <path> is strictly inside <dir>. Both must already be canonical.
# Components are compared one by one on purpose: a string prefix test would
# accept `/a/keys-otro/x` for key_dir `/a/keys`, which is the whole point of the
# containment check.
_dg_account_inside() {
  local dir="${1:-}" path="${2:-}"
  local -a dparts=() pparts=()
  local i

  [[ $dir == / ]] && { [[ $path == /* && $path != / ]]; return; }

  IFS=/ read -r -a dparts <<<"${dir#/}"
  IFS=/ read -r -a pparts <<<"${path#/}"

  # A trailing slash yields a final empty component; ignore it.
  while (( ${#dparts[@]} )) && [[ -z ${dparts[-1]} ]]; do unset 'dparts[-1]'; done
  while (( ${#pparts[@]} )) && [[ -z ${pparts[-1]} ]]; do unset 'pparts[-1]'; done

  (( ${#pparts[@]} > ${#dparts[@]} )) || return 1
  for (( i = 0; i < ${#dparts[@]}; i++ )); do
    [[ ${dparts[i]} == "${pparts[i]}" ]] || return 1
  done
  return 0
}

# 0 when <basename> is one of the files this library keeps NEXT TO the keys:
# the `api_key` compatibility symlink, the `default_account` metadata, and the
# dot-prefixed temporaries both are published through. `%s` is a legal
# key_pattern and `api_key` a legal account name, so without this the account
# file could BE the symlink — adding it wrote the key there and activating it
# replaced it with a symlink to itself — or the metadata file.
_dg_account_reserved_base() {
  case "$1" in
    api_key|default_account|.*) return 0 ;;
  esac
  return 1
}

dg_account_path() {
  local key_dir="${1:-}" pattern="${2:-}" account="${3:-}" dir
  if [[ -z $key_dir || -z $pattern || -z $account ]]; then
    _dg_account_err "dg_account_path: expects <key_dir> <key_pattern> <account>"
    return 2
  fi
  # Validate the name before it reaches a path, so `../x` and `/etc/x` are both
  # refused rather than normalised away.
  if ! _dg_account_name_ok "$account"; then
    printf 'delegate: invalid account name: %q\n' "$account" >&2
    return 2
  fi
  dir=$(_dg_account_dir "$key_dir") || {
    _dg_account_err "dg_account_path: unusable key_dir"
    return 2
  }
  local base="${pattern/\%s/$account}"
  if _dg_account_reserved_base "$base"; then
    printf 'delegate: account %s with key_pattern %s lands on %s, a reserved file in key_dir\n' \
      "$account" "$pattern" "$base" >&2
    return 2
  fi
  printf '%s/%s\n' "$dir" "$base"
}

dg_account_list() {
  local key_dir="${1:-}" pattern="${2:-}" dir prefix suffix f base name
  local -a names=()

  if [[ -z $key_dir || -z $pattern ]]; then
    _dg_account_err "dg_account_list: expects <key_dir> <key_pattern>"
    return 2
  fi
  dir=$(_dg_account_dir "$key_dir") || return 2
  [[ -d $dir ]] || return 0

  prefix="${pattern%%%s*}"
  suffix="${pattern#*%s}"

  for f in "$dir"/*; do
    [[ -f $f || -L $f ]] || continue
    base="${f##*/}"
    [[ $base == "$prefix"*"$suffix" ]] || continue
    _dg_account_reserved_base "$base" && continue
    name="$base"
    [[ -n $prefix ]] && name="${name#"$prefix"}"
    [[ -n $suffix ]] && name="${name%"$suffix"}"
    # A file that survives the pattern but whose name is not a legal account
    # name (api_key.FOO, api_key., ...) cannot be used by any other function
    # here, so it is not an account and is not listed.
    _dg_account_name_ok "$name" || continue
    names+=("$name")
  done

  (( ${#names[@]} )) || return 0
  printf '%s\n' "${names[@]}" | sort
}

dg_account_active() {
  local key_dir="${1:-}" pattern="${2:-api_key.%s}"
  local link target candidate rdir rbase name prefix suffix

  [[ -n $key_dir ]] || return 1
  # Same decomposition dg_account_list uses. Hardcoding `api_key.` here made
  # this function disagree with `list` for any provider that sets its own
  # key_pattern: the files were there, `list` showed them, and `active` said
  # nothing was active.
  prefix="${pattern%%%s*}"
  suffix="${pattern#*%s}"
  key_dir=$(_dg_account_dir "$key_dir") || return 1
  link="$key_dir/api_key"

  [[ -L $link ]] || return 1
  target=$(readlink -- "$link" 2>/dev/null) || return 1
  [[ -n $target ]] || return 1

  # A relative target is relative to key_dir, as the kernel resolves it. A
  # dangling target still counts: the name is what we are after, and `_remove`
  # relies on that to detect the active account after deleting its key.
  case $target in
    /*) candidate="$target" ;;
    *)  candidate="$key_dir/$target" ;;
  esac

  rdir=$(readlink -f -- "$key_dir" 2>/dev/null) || return 1
  candidate=$(readlink -f -- "$candidate" 2>/dev/null) || return 1
  _dg_account_inside "$rdir" "$candidate" || return 1

  rbase="${candidate##*/}"
  [[ $rbase == "$prefix"*"$suffix" ]] || return 1
  name="$rbase"
  [[ -n $prefix ]] && name="${name#"$prefix"}"
  [[ -n $suffix ]] && name="${name%"$suffix"}"
  _dg_account_name_ok "$name" || return 1
  printf '%s\n' "$name"
}

dg_account_fingerprint() {
  local path="${1:-}" sum
  [[ -n $path ]] || return 1
  sum=$(sha256sum -- "$path" 2>/dev/null) || return 1
  sum="${sum%% *}"
  [[ -n $sum ]] || return 1
  printf '%s\n' "${sum:0:8}"
}

dg_account_validate() {
  local key_dir="${1:-}" path="${2:-}" rdir rpath mode owner

  if [[ -z $key_dir || -z $path ]]; then
    _dg_account_err "dg_account_validate: expects <key_dir> <path>"
    return 1
  fi

  # The tilde first: every other entry point goes through _dg_account_dir, and
  # this one did not, so a provider file with `key_dir ~/.config/deepseek` —
  # which is how both of them are written — died here with "cannot be resolved"
  # while `account list` worked. readlink -f will not expand a `~`: to the
  # kernel it is a directory name, and it does not exist.
  key_dir=$(_dg_account_dir "$key_dir") || {
    _dg_account_err "key dir '${1}' cannot be expanded"
    return 1
  }
  path=$(_dg_account_dir "$path") || {
    _dg_account_err "key file '${2}' cannot be expanded"
    return 1
  }

  # Resolve both sides, then compare components. readlink -f also follows a
  # symlink target, so a link landing outside key_dir is refused here.
  rdir=$(readlink -f -- "$key_dir" 2>/dev/null) || {
    _dg_account_err "key dir '$key_dir' cannot be resolved"
    return 1
  }
  rpath=$(readlink -f -- "$path" 2>/dev/null) || {
    _dg_account_err "key file '$path' cannot be resolved"
    return 1
  }
  if ! _dg_account_inside "$rdir" "$rpath"; then
    _dg_account_err "key file '$rpath' is outside the key dir '$rdir'"
    return 1
  fi

  if [[ ! -f $rpath ]]; then
    _dg_account_err "key file '$rpath' is not a regular file"
    return 1
  fi

  mode=$(stat -c %a -- "$rpath" 2>/dev/null) || {
    _dg_account_err "key file '$rpath' cannot be stat'ed"
    return 1
  }
  if [[ $mode != 600 ]]; then
    _dg_account_err "key file '$rpath' must have mode 600, found $mode"
    return 1
  fi

  owner=$(stat -c %u -- "$rpath" 2>/dev/null) || {
    _dg_account_err "key file '$rpath' cannot be stat'ed"
    return 1
  }
  if [[ $owner != "$EUID" ]]; then
    _dg_account_err "key file '$rpath' is not owned by the current user"
    return 1
  fi

  if [[ ! -s $rpath ]]; then
    _dg_account_err "key file '$rpath' is empty"
    return 1
  fi

  # The resolved path, for the caller to open. Handing back the argument would
  # leave a window where the symlink is re-followed after every check passed.
  printf '%s\n' "$rpath"
  return 0
}

dg_account_read() {
  # `+x`: a local shadowing an exported variable of the same name keeps the
  # export attribute, which would put the key in the environment of every
  # command this function and its caller run afterwards.
  local +x key
  local path="${1:-}"
  [[ -n $path ]] || {
    _dg_account_err "dg_account_read: expects <path>"
    return 1
  }
  key=$(cat -- "$path" 2>/dev/null) || {
    _dg_account_err "key file '$path' cannot be read"
    return 1
  }
  key=$(_dg_account_trim "$key")
  [[ -n $key ]] || {
    _dg_account_err "key file '$path' is empty"
    return 1
  }
  printf '%s\n' "$key"
}

dg_account_activate() {
  local key_dir="${1:-}" target="${2:-}" link tmp tries=0

  if [[ -z $key_dir || -z $target ]]; then
    _dg_account_err "dg_account_activate: expects <key_dir> <target>"
    return 1
  fi
  key_dir=$(_dg_account_dir "$key_dir") || return 1
  if [[ ! -d $key_dir ]]; then
    _dg_account_err "key dir '$key_dir' does not exist"
    return 1
  fi
  link="$key_dir/api_key"

  # Unique temporary name in the same directory, created with a real `ln -s`
  # so two concurrent activations cannot agree on the same name: the loser gets
  # EEXIST and picks another. Same directory, because rename(2) is only atomic
  # within a filesystem.
  while :; do
    tmp="$key_dir/.api_key.tmp.$$.$RANDOM$tries"
    if ln -s -- "$target" "$tmp" 2>/dev/null; then break; fi
    if [[ ! -e $tmp && ! -L $tmp ]]; then
      _dg_account_err "cannot create a temporary symlink in '$key_dir'"
      return 1
    fi
    tries=$(( tries + 1 ))
    if (( tries > 32 )); then
      _dg_account_err "cannot create a unique temporary symlink in '$key_dir'"
      return 1
    fi
  done

  # mv -T is a single rename(2): `api_key` goes from pointing at the old target
  # to the new one with no instant in between where it is absent. NEVER ln -sfn,
  # which unlinks the destination first (invariant, see CLAUDE.md).
  if ! mv -T -- "$tmp" "$link" 2>/dev/null; then
    rm -f -- "$tmp"
    _dg_account_err "cannot replace '$link'"
    return 1
  fi
  return 0
}

dg_account_add() {
  # Same `+x` reason as dg_account_read: the key must not become exported.
  local +x key
  local key_dir="${1:-}" pattern="${2:-}" account="${3:-}" path

  path=$(dg_account_path "$key_dir" "$pattern" "$account") || return $?
  key_dir=$(_dg_account_dir "$key_dir") || return 2

  if [[ -e $path || -L $path ]]; then
    _dg_account_err "account '$account' already exists"
    return 1
  fi

  if [[ -t 0 ]]; then
    printf 'API key for %s: ' "$account" >&2
    IFS= read -rs -r key || true
    printf '\n' >&2
  else
    # Non-interactive: one plain line, so the command is scriptable.
    IFS= read -r key || true
  fi
  key=$(_dg_account_trim "$key")
  if [[ -z $key ]]; then
    _dg_account_err "no key given on stdin"
    return 1
  fi

  if [[ ! -d $key_dir ]]; then
    mkdir -p -m 700 -- "$key_dir" || {
      _dg_account_err "cannot create key dir '$key_dir'"
      return 1
    }
  fi

  # umask 077 in a subshell: the file is born 600, it is never world-readable
  # for even an instant.
  if ! ( umask 077; printf '%s\n' "$key" >"$path" ); then
    rm -f -- "$path"
    _dg_account_err "cannot write key file '$path'"
    return 1
  fi
  chmod 600 -- "$path" 2>/dev/null ||
    _dg_account_err "warning: cannot tighten mode of '$path'"
  return 0
}

dg_account_remove() {
  local key_dir="${1:-}" pattern="${2:-}" account="${3:-}" path active link

  path=$(dg_account_path "$key_dir" "$pattern" "$account") || return $?
  if [[ ! -e $path && ! -L $path ]]; then
    return 1
  fi

  active=$(dg_account_active "$key_dir" "$pattern" 2>/dev/null) || active=''

  rm -f -- "$path" || {
    _dg_account_err "cannot remove key file '$path'"
    return 1
  }

  if [[ $active == "$account" ]]; then
    # No promotion on purpose: silently changing which key gets billed is worse
    # than being left with nothing active. 10 tells the caller so.
    key_dir=$(_dg_account_dir "$key_dir") || key_dir=''
    link="$key_dir/api_key"
    [[ -n $key_dir ]] && rm -f -- "$link" 2>/dev/null
    return 10
  fi
  return 0
}

dg_account_default_get() {
  local key_dir="${1:-}" file name

  [[ -n $key_dir ]] || return 1
  key_dir=$(_dg_account_dir "$key_dir") || return 1
  file="$key_dir/default_account"
  [[ -f $file ]] || return 1
  name=$(cat -- "$file" 2>/dev/null) || return 1
  name=$(_dg_account_trim "$name")
  _dg_account_name_ok "$name" || return 1
  printf '%s\n' "$name"
}

dg_account_default_set() {
  local key_dir="${1:-}" account="${2:-}" file tmp

  if [[ -z $key_dir || -z $account ]]; then
    _dg_account_err "dg_account_default_set: expects <key_dir> <account>"
    return 2
  fi
  if ! _dg_account_name_ok "$account"; then
    printf 'delegate: invalid account name: %q\n' "$account" >&2
    return 2
  fi
  key_dir=$(_dg_account_dir "$key_dir") || return 2

  if [[ ! -d $key_dir ]]; then
    mkdir -p -m 700 -- "$key_dir" || {
      _dg_account_err "cannot create key dir '$key_dir'"
      return 1
    }
  fi

  file="$key_dir/default_account"
  # Not a secret, so 644; written the same way as api_key, atomically.
  tmp="$key_dir/.default_account.tmp.$$.$RANDOM"
  if ! ( umask 022; printf '%s\n' "$account" >"$tmp" ); then
    rm -f -- "$tmp"
    _dg_account_err "cannot write '$tmp'"
    return 1
  fi
  if ! mv -T -- "$tmp" "$file" 2>/dev/null; then
    rm -f -- "$tmp"
    _dg_account_err "cannot replace '$file'"
    return 1
  fi
  chmod 644 -- "$file" 2>/dev/null || {
    _dg_account_err "cannot set mode 644 on '$file'"
    return 1
  }
  return 0
}
