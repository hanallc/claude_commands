#!/usr/bin/env bash

set -euo pipefail

# Git clean filter for settings.json: the local-only keys and hook groups never
# reach a commit.
# The key list, and the smudge that puts them back, live in
# settings_local_keys.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/settings_local_keys.sh"

input="$(mktemp)" || exit 1
trap 'rm -f "$input"' EXIT
cat >"$input" || exit 1

references="$(_settings_local_keys_references)"
baseline="$(jq -cs "$_SETTINGS_LOCAL_SHARED_KEYS" <<<"$references")" || exit 1
# Use the stage 0 order so Git can compare the clean result with the index.
ordered="$(jq -cs 'if .[0] == null then null else .[1] // .[0] end' <<<"$references")" || exit 1
if [[ "$ordered" == null ]]; then
    live_keys="$(jq 'keys' "$input")" || exit 1
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    if [[ -s "$sidecar" ]]; then
        # Keep saved local keys local when no Git object holds them.
        # shellcheck disable=SC2016
        live_keys="$(jq --argjson live "$live_keys" '$live - keys' "$sidecar")" || exit 1
    fi
    # shellcheck disable=SC2016
    baseline="$(jq --argjson shared "$baseline" '(. + $shared) | unique' <<<"$live_keys")" || exit 1
fi

# Keep snapshot errors even when Git discards filter stderr.
log="$_SETTINGS_LOCAL_ROOT/settings.local-keys.errors.log"
if ! errors="$(settings_local_keys_snapshot "$input" "$baseline" 2>&1)"; then
    printf '%s\n' "$errors" >>"$log"
    echo "settings: cannot preserve local settings values; see $log" >&2
    exit 1
fi
jq --argjson baseline "$baseline" --argjson committed "$ordered" "$SETTINGS_LOCAL_KEYS_STRIP" "$input"
