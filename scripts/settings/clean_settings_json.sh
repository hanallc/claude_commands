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
# Use the stage 0 order so Git can compare the clean result with the index.
order_reference="$(jq -c 'if .head == null then null else .stage0 // .head end' <<<"$references")" || exit 1
if [[ "$order_reference" == null ]]; then
    snapshot_baseline="$(jq 'keys' "$input")" || exit 1
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    if [[ -s "$sidecar" ]]; then
        # Keep saved keys local when HEAD is unavailable.
        # shellcheck disable=SC2016
        snapshot_baseline="$(jq --argjson baseline "$snapshot_baseline" '$baseline - keys' "$sidecar")" || exit 1
    fi
else
    snapshot_baseline="$(jq -c '.head | keys' <<<"$references")" || exit 1
fi
# If an unlisted local key matches an incoming key, a merge snapshot can overwrite its saved value. During a conflict, git checkout --ours then git add can stage the local value.
# shellcheck disable=SC2016
strip_baseline="$(jq -c --argjson baseline "$snapshot_baseline" '
    ($baseline + [.stage0, .stage1, .stage2, .stage3 | objects | keys[]]) | unique' <<<"$references")" || exit 1

# Keep snapshot errors even when Git discards filter stderr.
log="$_SETTINGS_LOCAL_ROOT/settings.local-keys.errors.log"
if ! errors="$(settings_local_keys_snapshot "$input" "$snapshot_baseline" 2>&1)"; then
    printf '%s\n' "$errors" >>"$log"
    echo "settings: cannot preserve local settings values; see $log" >&2
    exit 1
fi
jq --argjson baseline "$strip_baseline" --argjson order_reference "$order_reference" "$SETTINGS_LOCAL_KEYS_STRIP" "$input"
