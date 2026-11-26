# shellcheck shell=bash
# Live configuration reader for bash 3.2 and zsh. Valid integer and text reads use builtins.
if [[ -n "${ZSH_VERSION:-}" ]]; then
    # shellcheck disable=SC2296 # zsh reads its source path.
    _CONF_SOURCE="${(%):-%x}"
else
    _CONF_SOURCE="${BASH_SOURCE[0]}"
fi
case "$_CONF_SOURCE" in
    */*) _CONF_LIB="${_CONF_SOURCE%/*}" ;;
    *) _CONF_LIB="." ;;
esac
unset _CONF_SOURCE
# shellcheck source=/dev/null
source "$_CONF_LIB/conf_record.sh"
if [[ -r /proc/sys/kernel/hostname ]]; then
    IFS= read -r _CONF_HOST < /proc/sys/kernel/hostname
else
    _CONF_HOST="$(uname -n)" || return
fi
_CONF_HOST="${_CONF_HOST%%.*}"
if [[ "$_CONF_HOST" == *[[:upper:]]* ]]; then
    if [[ -n "${ZSH_VERSION:-}" ]]; then
        # shellcheck disable=SC2296 # zsh converts the host name to lower case.
        _CONF_HOST="${(L)_CONF_HOST}"
    elif (( BASH_VERSINFO[0] >= 4 )); then
        _CONF_HOST="${_CONF_HOST,,}"
    else
        _CONF_HOST="$(printf '%s' "$_CONF_HOST" | LC_ALL=C tr '[:upper:]' '[:lower:]')" || return
    fi
fi

conf_value() {
    local file="$1" key="$2" folder="${CONF_DIR:-$HOME/.claude/config}"
    if [[ "$key" == *[[:upper:]]* ]]; then
        printf 'conf_value: configuration key %q must be lower case\n' "$key" >&2
        return 2
    fi
    conf_record "$file"
    local raw line name value previous="" limit=""
    local base="" base_limit="" host_value="" host_limit="" base_seen=0 host_seen=0 found=0
    local low high choices choice numeric valid=1
    if [[ -r "$folder/$file.conf" ]]; then
        while IFS= read -r raw || [[ -n "$raw" ]]; do
            raw="${raw%$'\r'}"
            if [[ "$raw" == *$'\r'* ]]; then valid=0; break; fi
            if [[ "$raw" == \#* ]]; then
                line=""
            else
                line="${raw%%[[:space:]]\#*}"
            fi
            line="${line#"${line%%[![:space:]]*}"}"
            line="${line%"${line##*[![:space:]]}"}"
            if [[ "$line" != \[* && "$line" == *=* ]]; then
                name="${line%%=*}"; name="${name%"${name##*[![:space:]]}"}"
                value="${line#*=}"; value="${value#"${value%%[![:space:]]*}"}"
                if [[ "$name" == "$key" && "$base_seen" == 0 ]]; then
                    base="$value"; base_limit="$previous"; base_seen=1
                elif [[ "$name" == "$key.$_CONF_HOST" && "$host_seen" == 0 ]]; then
                    host_value="$value"; host_limit="$previous"; host_seen=1
                fi
            fi
            previous="${raw#"${raw%%[![:space:]]*}"}"
        done < "$folder/$file.conf"
    fi
    if [[ "$host_seen" == 1 ]]; then
        value="$host_value"; limit="$host_limit"; found=1
    elif [[ "$base_seen" == 1 ]]; then
        value="$base"; limit="$base_limit"; found=1
    fi
    if [[ "$valid" == 0 || "$found" == 0 || -z "$value" ]]; then valid=0
    elif [[ "$limit" == '# one of:'* ]]; then
        choices="${limit#\# one of:}"; valid=0
        while :; do
            choice="${choices%%,*}"
            choice="${choice#"${choice%%[![:space:]]*}"}"
            choice="${choice%"${choice##*[![:space:]]}"}"
            [[ "$choice" != "$value" ]] || valid=1
            [[ "$choices" == *,* ]] || break
            choices="${choices#*,}"
        done
    elif [[ "$limit" == '# range:'* ]]; then
        valid=0
    elif [[ "$limit" == '# whole:'* ]]; then
        line="${limit#\# whole:}"; line="${line//[[:space:]]/}"
        low="${line%%..*}"; high="${line#*..}"
        if [[ "$line" != *..* || ! "$value" =~ ^[+-]?[0-9]+$ || ( -n "$low" && ! "$low" =~ ^[+-]?[0-9]+$ ) || ( -n "$high" && ! "$high" =~ ^[+-]?[0-9]+$ ) ]]; then
            valid=0
        elif [[ "${#value}" -gt 15 || "${#low}" -gt 15 || "${#high}" -gt 15 ]]; then
            valid=0
        else
            numeric="${value#[+-]}"; numeric=$((10#$numeric))
            [[ "$value" != -* ]] || numeric=$((-numeric))
            if [[ -n "$low" ]]; then
                choice="${low#[+-]}"; choice=$((10#$choice))
                [[ "$low" != -* ]] || choice=$((-choice))
                [[ "$numeric" -ge "$choice" ]] || valid=0
            fi
            if [[ -n "$high" ]]; then
                choice="${high#[+-]}"; choice=$((10#$choice))
                [[ "$high" != -* ]] || choice=$((-choice))
                [[ "$numeric" -le "$choice" ]] || valid=0
            fi
        fi
    fi
    if [[ "$valid" == 1 ]]; then
        if [[ -n "${CONF_SHIFT_READ_LOG:-}" ]]; then
            name="$key"
            [[ "$host_seen" != 1 ]] || name="$key.$_CONF_HOST"
            conf_record_shift "$folder/$file.conf" "$name" "$value"
        fi
        printf '%s\n' "$value"
    else "$_CONF_LIB/py" "$_CONF_LIB/conf.py" get "$file" "$key"
    fi
}


# Scan the selected key only when the caller traces config reads.
conf_record_shift() {
    local path="$1" selected="$2" value="$3"
    local raw line key previous="" before_limit="" mark
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        line="${raw%%[[:space:]]\#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        if [[ "$line" != \#* && "$line" == *=* ]]; then
            key="${line%%=*}"
            key="${key%"${key##*[![:space:]]}"}"
            if [[ "$key" == "$selected" ]]; then
                if [[ ( "$previous" == '# whole:'* || "$previous" == '# range:'* ) &&
                      "$before_limit" == '# test config shift: '*" $value" ]]; then
                    mark="${before_limit#'# test config shift: '}"
                    { printf '%s:%s: %s -> %s\n' "${path##*/}" "$key" "${mark%% *}" "$value" >> "$CONF_SHIFT_READ_LOG"; } 2>/dev/null || :
                fi
                return 0
            fi
        fi
        before_limit="$previous"
        previous="${raw#"${raw%%[![:space:]]*}"}"
        previous="${previous%"${previous##*[![:space:]]}"}"
    done 2>/dev/null < "$path" || :
}
