#!/bin/sh
# Opt-in: let hermes-awake keep the Mac awake with the lid closed.
# Installs a sudoers rule limited to `pmset -a disablesleep 0|1` (asks for your admin password once)
# and the helper the plugin calls. Undo: lid-mode/uninstall.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd -P)"
RULE=/etc/sudoers.d/hermes-awake
STATE="${HERMES_AWAKE_DIR:-$HOME/.hermes/hermes-awake}"

TMP="$(mktemp)"
echo "$(id -un) ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, /usr/bin/pmset -a disablesleep 1" > "$TMP"
visudo -cf "$TMP" >/dev/null
sudo install -m 440 -o root -g wheel "$TMP" "$RULE"
rm -f "$TMP"

mkdir -p "$STATE"
install -m 755 "$HERE/hermes-awake-lid" "$STATE/lid-helper"
"$STATE/lid-helper" check && echo "lid-closed mode ready"
