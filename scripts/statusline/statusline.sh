# shellcheck shell=sh
# Source this file from the statusLine command with statusline set to this folder.
# Read the account, colors, zone, and quota labels with shell builtins.
# assignment.py reads the action count and renders the status line.

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

# shellcheck disable=SC2154 # statusline is set by the command that sources this.
CONF_DIR="$config_dir" STAFF_FILE="$staff" exec "$statusline/../lib/py" --isolated "$statusline/assignment.py" \
  -r --arg pwd "$PWD" --arg account "$account" --arg legacy_home "$legacy_home" --rawfile home_config "$home_config" \
  --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"
