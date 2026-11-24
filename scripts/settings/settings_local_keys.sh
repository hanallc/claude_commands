#!/usr/bin/env bash

# jq variables must remain literal inside the programs.
# shellcheck disable=SC2016

# Keep machine settings in a sidecar before checkout replaces settings.json.
# extraKnownMarketplaces stores paths on this machine.
# theme stores the theme for this machine.
# voice stores the voice setting for this machine.
SETTINGS_LOCAL_KEYS_JSON='["model","effortLevel","modelSettings","extraKnownMarketplaces","theme","voice"]'
SETTINGS_LOCAL_KEYS_SIDECAR="settings.local-keys.json"
SETTINGS_LOCAL_KEYS_NOTICES="settings.local-key-notices.json"
_SETTINGS_LOCAL_ROOT="$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)"

# Read the settings objects from HEAD and the index.
_settings_local_keys_references() {
    local objects name oid kind reference head_oid="" head=null
    local -a references=()
    objects="$(git -C "$_SETTINGS_LOCAL_ROOT" cat-file --batch-check='%(objectname) %(objecttype)' 2>/dev/null <<'EOF'
HEAD:settings.json
:0:settings.json
:1:settings.json
:2:settings.json
:3:settings.json
EOF
    )" || { printf '{}\n'; return; }
    for name in head stage0 stage1 stage2 stage3; do
        read -r oid kind || break
        reference=null
        if [[ "$kind" == blob ]]; then
            if [[ "$oid" == "$head_oid" ]]; then
                reference="$head"
            else
                reference="$(git -C "$_SETTINGS_LOCAL_ROOT" cat-file blob "$oid" 2>/dev/null |
                    jq -cs 'if length == 1 and (.[0] | type) == "object" then .[0] else null end' 2>/dev/null)" ||
                    reference=null
            fi
        fi
        references+=("\"$name\":$reference")
        if [[ "$name" == head ]]; then
            head_oid="$oid"
            head="$reference"
        fi
    done <<<"$objects"
    (IFS=,; printf '{%s}\n' "${references[*]}")
}

_SETTINGS_LOCAL_REFERENCE_DEFS='
def head_keys: (.head // {}) | keys;
def conflict_keys: [.stage1, .stage2, .stage3 | objects | keys[]] | unique;
def index_keys: ((.stage0 // {} | keys) + conflict_keys) | unique;
def shared_keys: (head_keys + index_keys) | unique;'

settings_local_keys_baseline() {
    jq -ce "$_SETTINGS_LOCAL_REFERENCE_DEFS"'
        if .head == null then error("settings: cannot read committed HEAD:settings.json")
        else shared_keys end' <<<"$1"
}

# Preserve hook groups that run commands on one machine.
SETTINGS_LOCAL_HOOK_PATTERNS_JSON='["/.config/iterm2/cc-status","/.claude/hooks/herdr-agent-state.sh"]'

# Preserve entries from local plugin marketplaces.
SETTINGS_LOCAL_MARKETPLACES_JSON='["nate-local"]'

# jq definitions every program below starts with.
SETTINGS_LOCAL_DEFS="def merged_key: . == \"hooks\" or . == \"enabledPlugins\";
def listed_key: . as \$k | (${SETTINGS_LOCAL_KEYS_JSON} | index(\$k)) != null;
def local_key: listed_key or ((merged_key | not) and (. as \$k | (\$baseline | index(\$k)) == null));
def saved_entries: with_entries(select(.key | merged_key or local_key));
def local_hook: any(.hooks[]?; (.command // \"\") as \$c | ${SETTINGS_LOCAL_HOOK_PATTERNS_JSON} | any(. as \$p | \$c | contains(\$p)));
def local_hooks: with_entries(.value |= map(select(local_hook))) | with_entries(select(.value | length > 0));
def shared_hooks: with_entries(.value |= map(select(local_hook | not))) | with_entries(select(.value | length > 0));
def local_plugin: .key as \$k | ${SETTINGS_LOCAL_MARKETPLACES_JSON} | any(. as \$m | \$k | endswith(\"@\" + \$m));"

# Remove local values and retain the reference object's key order.
# The clean filter uses this program.
# shellcheck disable=SC2034
SETTINGS_LOCAL_KEYS_STRIP='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'
def committed_order($reference):
  if type == "object" then
    . as $live
    | (if ($reference | type) == "object" then $reference else {} end) as $ordered
    | reduce (($ordered | keys_unsorted[]),
              ($live | keys_unsorted[] | select(. as $key | $ordered | has($key) | not))) as $key
        ({}; if $live | has($key) then .[$key] = ($live[$key] | committed_order($ordered[$key])) else . end)
  elif type == "array" then
    if ($reference | type) == "array" then
      to_entries | map(. as $entry | $entry.value | committed_order($reference[$entry.key]))
    else map(committed_order(null)) end
  else . end;
with_entries(select(.key | local_key | not))
| if has("hooks") then
    (.hooks | length) as $before
    | .hooks |= shared_hooks
    | if $before > 0 and (.hooks | length) == 0 then del(.hooks) else . end
  else . end
| if has("enabledPlugins") then
    (.enabledPlugins | length) as $before
    | .enabledPlugins |= with_entries(select(local_plugin | not))
    | if $before > 0 and (.enabledPlugins | length) == 0 then del(.enabledPlugins) else . end
  else . end
| if $ARGS.named.order_reference == null then . else committed_order($ARGS.named.order_reference) end'

# jq: keep only the local-only keys of an object, plus its local hook groups.
SETTINGS_LOCAL_KEYS_PICK='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'
. as $s
| ($s | with_entries(select(.key | local_key)))
+ ($s.hooks // {} | local_hooks | if length == 0 then {} else {hooks: .} end)
+ ($s.enabledPlugins // {} | with_entries(select(local_plugin)) | if length == 0 then {} else {enabledPlugins: .} end)'

# Smudge uses keys from the incoming blob and unresolved index stages.
# Restore supplies the shared baseline and keeps existing values.
SETTINGS_LOCAL_KEYS_MERGE='
(($ARGS.named.baseline // keys) + ($ARGS.named.conflict_keys // [])) as $baseline
| '"$SETTINGS_LOCAL_DEFS"'
. as $cur | ($local[0] // {} | saved_entries) as $l
| . + ($l | del(.hooks, .enabledPlugins) | with_entries(.key as $k | select(($ARGS.named.restore != true) or (($cur | has($k)) | not))))
| reduce (($l.enabledPlugins // {}) | to_entries[]) as $p (.;
    if (.enabledPlugins // {} | has($p.key)) then . else .enabledPlugins[$p.key] = $p.value end)
| reduce (($l.hooks // {}) | to_entries[]) as $e (.;
    reduce $e.value[] as $g (.;
      if ((.hooks[$e.key] // []) | index([$g])) != null then .
      else .hooks[$e.key] += [$g] end))'

# jq: how many sidecar keys and hook groups the input lacks.
SETTINGS_LOCAL_KEYS_MISSING='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'. as $cur | ($local[0] // {} | saved_entries) as $l
| ([$l | del(.hooks, .enabledPlugins) | keys[] | . as $k | select(($cur | has($k)) | not)] | length)
+ ([($l.enabledPlugins // {}) | keys[] as $k | select(($cur.enabledPlugins // {} | has($k)) | not)] | length)
+ ([($l.hooks // {}) | to_entries[] | .key as $k | .value[] | . as $g
    | select((($cur.hooks[$k] // []) | index([$g])) == null)] | length)'

# Use one atomic replacement for snapshots and restored settings.
_settings_local_write() {
    local path="$1" text="$2" temporary
    [[ ! -d "$path" ]] || { echo "settings: destination is a directory: $path" >&2; return 1; }
    temporary="$(mktemp "$path.XXXXXX")" || return 1
    if ! { printf '%s\n' "$text" > "$temporary" && mv "$temporary" "$path"; }; then
        rm -f "$temporary"
        return 1
    fi
}

# Avoid the interpreter when the ledger needs no change or delivery.
settings_local_keys_notices() {
    local mode="$1" unknown="${2:-[]}" ledger machine work
    ledger="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_NOTICES"
    machine="$(hostname -s)" || return 1
    if [[ -s "$ledger" ]]; then
        work="$(jq --arg machine "$machine" --arg mode "$mode" --argjson keys "$unknown" '
            .[$machine] // {} | . as $history
            | if $mode == "record" then any($keys[]; . as $key | $history[$key] == null)
              else any(.[]; . == "pending") end' "$ledger")" || return 1
        [[ "$work" == true ]] || return 0
    elif [[ "$mode" == deliver || "$unknown" == "[]" ]]; then
        return 0
    fi
    "$_SETTINGS_LOCAL_ROOT/scripts/lib/py" "$_SETTINGS_LOCAL_ROOT/scripts/settings/local_key_notices.py" \
        "$mode" "$_SETTINGS_LOCAL_ROOT" "$machine" "$unknown"
}

# Keep an empty backup only when the previous values are still local.
settings_local_keys_snapshot() {
    local settings sidecar snapshot references baseline staged_only_keys unknown
    settings="$1"
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" ]] || return 0
    references="${3:-$(_settings_local_keys_references)}"
    if [[ $# -ge 2 ]]; then
        baseline="$2"
    else
        baseline="$(settings_local_keys_baseline "$references")" || return 1
    fi
    snapshot="$(jq --argjson baseline "$baseline" "$SETTINGS_LOCAL_KEYS_PICK" "$settings")" || return 1
    if [[ "$snapshot" == "{}" && ! -e "$sidecar" ]]; then
        return 0
    fi
    if [[ "$snapshot" == "{}" && -s "$sidecar" ]]; then
        snapshot="$(jq --argjson baseline "$baseline" "$SETTINGS_LOCAL_KEYS_PICK" "$sidecar")" || return 1
    fi
    if [[ -f "$sidecar" && -s "$sidecar" ]]; then
        # Keep the prior local value until HEAD holds the staged key.
        staged_only_keys="$(jq -c "$_SETTINGS_LOCAL_REFERENCE_DEFS"'index_keys - head_keys' <<<"$references")" || return 1
        if [[ "$staged_only_keys" != "[]" ]]; then
            snapshot="$(jq --argjson baseline "$baseline" --argjson snapshot "$snapshot" --argjson staged_keys "$staged_only_keys" \
                '$ARGS.named.baseline as $baseline | '"$SETTINGS_LOCAL_DEFS"'
                $snapshot + with_entries(select(.key | (listed_key or merged_key | not)
                    and (. as $key | $staged_keys | index($key) != null)))' "$sidecar")" || return 1
        fi
    fi
    if [[ ! -f "$sidecar" || "$(<"$sidecar")" != "$snapshot" ]]; then
        _settings_local_write "$sidecar" "$snapshot" || return 1
    fi
    unknown="$(jq --argjson baseline "$baseline" -c '$ARGS.named.baseline as $baseline | '"$SETTINGS_LOCAL_DEFS"'
        keys | map(select(local_key and (listed_key | not)))' <<<"$snapshot")" || return 1
    settings_local_keys_notices record "$unknown"
}

# Restore missing values without replacing a recent user edit.
settings_local_keys_restore() {
    local settings sidecar missing merged references baseline
    settings="$_SETTINGS_LOCAL_ROOT/settings.json"
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" && -s "$sidecar" ]] || return 0
    references="$(_settings_local_keys_references)"
    baseline="$(settings_local_keys_baseline "$references")" || return 1
    missing="$(jq --argjson baseline "$baseline" --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MISSING" "$settings")" || return 1
    [[ "$missing" -gt 0 ]] || return 0
    merged="$(jq --argjson baseline "$baseline" --argjson restore true --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MERGE" "$settings")" || return 1
    _settings_local_write "$settings" "$merged" || return 1
    echo "ensure_git_filters: restored local-only keys into settings.json from $SETTINGS_LOCAL_KEYS_SIDECAR"
}
