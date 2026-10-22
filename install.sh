#!/bin/sh
# Install hermes-awake for every Hermes profile on this Mac. Safe to re-run.
#   ./install.sh              all profiles
#   ./install.sh ares node1   only these
# Lid-closed mode is a separate opt-in: lid-mode/install.sh
set -eu

ROOT="${HERMES_ROOT:-$HOME/.hermes}"
SHARED="$ROOT/plugins/hermes-awake"
SRC="$(cd "$(dirname "$0")/plugin" && pwd -P)"

[ "$(uname)" = Darwin ] || { echo "hermes-awake: macOS only" >&2; exit 1; }
command -v hermes >/dev/null || { echo "hermes-awake: 'hermes' not on PATH" >&2; exit 1; }

# Point $1 at $2. A real directory in the way is archived, never deleted.
link() {
  if [ -e "$1" ] && [ ! -L "$1" ]; then
    mkdir -p "$ROOT/archive"
    mv "$1" "$ROOT/archive/$(basename "$1")-$(date +%Y%m%d-%H%M%S)"
  fi
  mkdir -p "$(dirname "$1")"
  ln -sfn "$2" "$1"
}

# One shared copy; each profile links to it, so an update reaches all of them.
link "$SHARED" "$SRC"
if [ $# -gt 0 ]; then profiles="$*"; else
  profiles="default $(cd "$ROOT/profiles" 2>/dev/null && ls -d */ 2>/dev/null | tr -d / | grep -vx default || true)"
fi
for p in $profiles; do
  [ "$p" = default ] || link "$ROOT/profiles/$p/plugins/hermes-awake" "$SHARED"
  if hermes -p "$p" plugins enable hermes-awake >/dev/null 2>&1; then echo "✓ $p"; else echo "✗ $p (hermes -p $p plugins enable hermes-awake)"; fi
done

echo
hermes awake doctor 2>/dev/null || true
echo
echo "The agent tool 'awake' shows up in new sessions. Uninstall: ./uninstall.sh"
