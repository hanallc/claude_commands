# shellcheck shell=sh
# Source this file from the statusLine command with statusline set to this folder.
# Read the account, colors, zone, and quota labels with shell builtins.
# Use assignment.py when the saved session fields expire.

: "${statusline:?}"

if [ -n "${CONF_READ_LOG:-}" ]; then
  # shellcheck source=/dev/null
  . "$statusline/../lib/conf_record.sh"
fi

config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
account=
{ read -r account < "$config/account-label"; } 2>/dev/null
# The cache in the config home is valid only while its legacy login remains.
home_config=/dev/null
if [ "$config" = "$HOME/.claude" ] && [ -f "$HOME/.claude.json" ] && [ -r "$HOME/.claude.json" ]; then
  home_config="$HOME/.claude.json"
fi
legacy_home=
[ "$config" != "$HOME/.claude" ] || legacy_home=1

staff="${STAFF_FILE:-$HOME/.local/state/staff/staff.json}"
[ -f "$staff" ] && [ -r "$staff" ] || staff=/dev/null

labels="$HOME/.local/state/agent-notes/by-label.json"
[ -r "$labels" ] || labels=/dev/null

config_dir="${CONF_DIR:-$statusline/../../config}"
colors="$config_dir/statusline.conf"
[ -r "$colors" ] || colors=/dev/null
[ -z "${CONF_READ_LOG:-}" ] || [ "$colors" = /dev/null ] || conf_record statusline

zone=${USER_ZONE:-}
if [ -z "$zone" ]; then
  zone_conf="$config_dir/user_zone.conf"
  zone_stem=${zone_conf##*/}
  [ -z "${CONF_READ_LOG:-}" ] || conf_record "${zone_stem%.conf}"
  {
    while IFS= read -r line; do
      case $line in zone=*) zone=${line#zone=}; break ;; esac
    done < "$zone_conf"
  } 2>/dev/null
fi
export TZ="${zone:-America/New_York}"
# NixOS's glibc finds zone files only through TZDIR; a bare environment would get UTC.
[ -n "${TZDIR:-}" ] || [ ! -d /etc/zoneinfo ] || export TZDIR=/etc/zoneinfo

set -- -r --arg pwd "$PWD" --arg account "$account" --arg legacy_home "$legacy_home" --rawfile home_config "$home_config" \
  --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"

newline='
'
tab='	'

sources_match() {
  sources=$(cat && printf '.') || return 1
  case $sources in *"$newline.") ;; *) return 1 ;; esac
  sources=${sources%"$newline."}

  expected=$(printf '%s\n' "$sources" | LC_ALL=C STATUSLINE_ASSIGNMENT="$statusline/assignment.py" \
    STATUSLINE_SOURCE_ROWS="$1" awk -F "$tab" '
    $0 !~ /^\// || NF < 2 { exit 1 }
    NF != 2 || $2 != "missing" {
      print
      if ($1 == ENVIRON["STATUSLINE_ASSIGNMENT"]) implementation = 1
    }
    END { if ("x" NR != "x" ENVIRON["STATUSLINE_SOURCE_ROWS"] || !implementation) exit 1 }
  ') || return 1
  observed=$(printf '%s\n' "$expected" | cut -f1 |
    LC_ALL=C TZ=UTC0 xargs -d '\n' stat -L --printf='%n\t%d:%i:%y:%z\n' -- 2>/dev/null) || return 1
  [ "$observed" = "$expected" ] || return 1

  missing_paths=$(printf '%s\n' "$sources" | LC_ALL=C awk -F "$tab" 'NF == 2 && $2 == "missing" { print $1 }') || return 1
  [ -n "$missing_paths" ] || return 0
  missing_count=$(printf '%s\n' "$missing_paths" | wc -l) || return 1
  # Do not follow links when checking for absent paths.
  observed=$(printf '%s\n' "$missing_paths" |
    LC_ALL=C xargs -d '\n' stat --printf='present\n' -- 2>&1) && return 1
  printf '%s\n' "$observed" | LC_ALL=C awk -v count="$missing_count" '
    !/^stat: cannot stat.*: No such file or directory$/ { exit 1 }
    END { if (NR != count) exit 1 }
  '
}

render_fresh() {
  cache=$1
  shift
  [ -f "$cache" ] || return 1
  {
    IFS= read -r key && [ "$key" = "$render_key" ] &&
      IFS= read -r deadline && IFS= read -r size && IFS= read -r fields &&
      sources_match "$size" || return 1
  } < "$cache" 2>/dev/null
  case $deadline in ''|*[!0123456789]*) return 1 ;; esac
  [ "$deadline" -lt 9223372036854775807 ] 2>/dev/null || return 1
  # Keep the final newline from jq.
  result=$(jq -n --argjson payload "$payload" --argjson render "$fields" "$@" 2>/dev/null && printf '.') || return 1
  now=$(date +%s) && [ "$now" -lt "$deadline" ] 2>/dev/null || return 1
  printf '%s' "${result%.}"
}

if [ "${STATUSLINE_SYSTEM:-$(uname -s)}" = Linux ]; then
  # Keep trailing newlines from the input for the Python fallback.
  prepared=$(jq -Rrs --arg pwd "$PWD" --arg staff "$staff" --arg runtime "$statusline" '
    . as $raw | fromjson
    | if type == "object" then . else error("Expected one status payload") end
    | (if has("session_id") then .session_id else "" end) as $id
    | ((try .workspace.current_dir catch null) // .cwd // "") as $folder
    | (if ($id | type) == "string" and ($id | test("\\A[A-Za-z0-9_-]{0,128}\\z"))
          and ($folder | type) == "string"
          and ((.workspace // {}) | type) == "object" then "s-" + $id else "" end),
      ([$id, $folder, $pwd, $staff, $runtime, $ENV.HOME, $ENV.FOR_YOU_ACTIONS_DIR, $ENV.REBUILD_NEEDS_DIR,
        $ENV.SHOWRUNNER_STATE_DIR, $ENV.NOTIFIER_STATE_DIR, $ENV.CONF_DIR, $ENV.USER_ZONE]
        | tojson),
      ($raw + ".")
  ') || exit 1
  session=${prepared%%"$newline"*}
  prepared=${prepared#*"$newline"}
  render_key=${prepared%%"$newline"*}
  payload=${prepared#*"$newline"}
  payload=${payload%.}
  state="${SHOWRUNNER_STATE_DIR:-$HOME/.local/state/showrunner}"
  if [ -n "$session" ]; then
    render_fresh "$state/counts/statusline-render-v2/$session" "$@" && exit 0
  else
    render_key=
  fi
  printf '%s' "$payload" | CONF_DIR="$config_dir" STAFF_FILE="$staff" STATUSLINE_RENDER_KEY="$render_key" \
    "$statusline/../lib/py" --isolated "$statusline/assignment.py" "$@"
  exit $?
fi

CONF_DIR="$config_dir" STAFF_FILE="$staff" exec "$statusline/../lib/py" --isolated "$statusline/assignment.py" "$@"
