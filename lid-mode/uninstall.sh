#!/bin/sh
# Turn lid-closed mode off: restore normal sleep, remove the helper and the sudoers rule.
set -eu
STATE="${HERMES_AWAKE_DIR:-$HOME/.hermes/hermes-awake}"
[ -x "$STATE/lid-helper" ] && "$STATE/lid-helper" off || true
rm -f "$STATE/lid-helper"
[ -f /etc/sudoers.d/hermes-awake ] && sudo rm /etc/sudoers.d/hermes-awake
echo "lid-closed mode removed"
