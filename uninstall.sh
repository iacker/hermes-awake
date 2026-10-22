#!/bin/sh
# Remove hermes-awake from every profile. Running keepers notice within a tick, give the Mac
# back and stop. Settings and journal stay in ~/.hermes/hermes-awake.
set -eu

ROOT="${HERMES_ROOT:-$HOME/.hermes}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"

for p in default $(cd "$ROOT/profiles" 2>/dev/null && ls -d */ 2>/dev/null | tr -d / | grep -vx default || true); do
  hermes -p "$p" plugins disable hermes-awake >/dev/null 2>&1 || true
  link="$ROOT/profiles/$p/plugins/hermes-awake"
  if [ -L "$link" ]; then rm "$link"; echo "✓ $p"; fi
done
[ -L "$ROOT/plugins/hermes-awake" ] && rm "$ROOT/plugins/hermes-awake"
[ -e "${HERMES_AWAKE_DIR:-$ROOT/hermes-awake}/lid-helper" ] && "$HERE/lid-mode/uninstall.sh"
echo "done"
