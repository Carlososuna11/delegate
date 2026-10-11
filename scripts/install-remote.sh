#!/usr/bin/env bash
# Install (or update) delegate and delegate-run on a remote host, for Fase C.
#
#   scripts/install-remote.sh [host] [rev]      # defaults: caracas, HEAD
#
# What crosses: a `git bundle` of <rev> (committed history only: no working
# tree, no untracked files, no config) copied with scp, and a fixed install
# script on ssh's stdin. Nothing else: no key, no ~/.config/delegate, no
# environment. The host keeps its own delegation keys (`delegate account
# <provider> add <name>` there) and has no production or Claude credentials.
#
# On the host:
#   ~/delegate/src         the code, checked out detached at <rev>
#   ~/delegate/repos       bare mirrors, created by `delegate-run mirror`
#   ~/.local/bin/{delegate,dlg,delegate-run} -> ~/delegate/src/bin/...
# A file (not a symlink) already at one of those names is left alone and
# reported: the install never overwrites something it did not create.
set -euo pipefail

host="${1:-caracas}"
rev="${2:-HEAD}"
[[ "$host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*$ ]] || { echo "host inválido: $host" >&2; exit 2; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."
sha="$(git rev-parse --verify --quiet "${rev}^{commit}")" || { echo "no existe el commit $rev" >&2; exit 2; }
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "aviso: hay cambios sin commit; se instala $sha, no tu árbol" >&2
fi

ssh_opts=(-T -o BatchMode=yes -o ConnectTimeout=15 -o ForwardAgent=no -o ForwardX11=no
          -o ClearAllForwardings=yes)
tmp="$(mktemp -d)"
tmpref="refs/delegate-install/$$"
trap 'rm -rf "$tmp"; git update-ref -d "$tmpref" 2>/dev/null || true' EXIT
bundle="$tmp/delegate.bundle"
# A bundle carries refs, not bare commits: a throwaway ref names the commit.
git update-ref "$tmpref" "$sha"
git bundle create --quiet "$bundle" "$tmpref"

remote_bundle=".delegate-install-$sha.bundle"
scp -q -o BatchMode=yes -o ConnectTimeout=15 "$bundle" "$host:$remote_bundle"

# The sha is a validated hex string; the rest of the script is fixed text.
ssh "${ssh_opts[@]}" -- "$host" "bash -s -- $sha" <<'REMOTE'
set -euo pipefail
sha="$1"
[[ "$sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || { echo "sha inválido" >&2; exit 2; }
bundle="$HOME/.delegate-install-$sha.bundle"
trap 'rm -f "$bundle"' EXIT
umask 077
mkdir -p "$HOME/delegate/repos" "$HOME/.local/bin"
chmod 700 "$HOME/delegate" "$HOME/delegate/repos"
src="$HOME/delegate/src"
if [[ ! -d "$src/.git" ]]; then
  git init --quiet "$src"
fi
git -C "$src" fetch --quiet "$bundle" '+refs/*:refs/delegate-install/*'
git -C "$src" checkout --quiet --detach "$sha"
git -C "$src" for-each-ref --format='%(refname)' refs/delegate-install/ \
  | while read -r ref; do git -C "$src" update-ref -d "$ref"; done
for name in delegate dlg delegate-run; do
  link="$HOME/.local/bin/$name"
  target="$src/bin/$name"
  if [[ -e "$link" && ! -L "$link" ]]; then
    echo "aviso: $link existe y no es un enlace; no se toca" >&2
    continue
  fi
  ln -sfn "$target" "$link"
done
printf 'instalado %s en %s\n' "$sha" "$src"
REMOTE

# The installed version answers through the same non-interactive PATH that
# `delegate-run --remote` uses.
ssh "${ssh_opts[@]}" -- "$host" 'command -v delegate-run delegate && delegate-run --help >/dev/null && echo ok'
