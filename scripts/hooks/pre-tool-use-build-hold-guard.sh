#!/usr/bin/env bash
# Check test starts at all times. Check cargo calls only during a hold.

IFS= read -r -d '' hook_input || true
command_text=$hook_input

# Use the full payload if the field or its closing quote is ambiguous.
select_command() {
    local value command
    case "$hook_input" in
        *'"cmd"'*'"command"'*|*'"command"'*'"cmd"'*) return ;;
        *'"command"'*) value=${hook_input#*'"command"'} ;;
        *'"cmd"'*) value=${hook_input#*'"cmd"'} ;;
        *) return ;;
    esac
    case "$value" in
        *'"command"'*|*'"cmd"'*) return ;;
    esac
    value=${value#"${value%%[![:space:]]*}"}
    [[ $value == :* ]] || return 0
    value=${value#:}
    value=${value#"${value%%[![:space:]]*}"}
    [[ $value == \"* ]] || return 0
    value=${value#\"}
    command=${value%%\"*}
    [[ $command != "$value" && $command != *\\ ]] || return 0
    command_text="\"$command\""
}

needs_decision() {
    # The Python hook decodes JSON Unicode escapes.
    [[ $hook_input == *'\u'* ]] && return 0

    select_command
    local continuation='\\\n'
    command_text="${command_text//"$continuation"/}"
    case "$command_text" in
        *unittest*|*pytest*|*py.test*|*module_run*|*coverage_overlap*|*merge_gate*|*/gate.py*|*' gate.py'*|*'"gate.py'*|*targeted_test*|*walled_tests*)
            return 0
            ;;
        *python*|*/py*|*node*|*bash*|*zsh*|*dash*|*'"sh '*|*'"sh"'*|*'/sh '*|*'/sh"'*|*' sh '*)
            case "$command_text" in
                *test_*|*proof*) return 0 ;;
            esac
            ;;
        *test_*.sh*|*test_*.py*|*proof*.mjs*)
            [[ $hook_input == *'"Bash"'* ]] && return 0
            ;;
    esac
    case "$command_text" in
        *cargo*) ;;
        *) return 1 ;;
    esac
    local hold_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}"
    shopt -s nullglob dotglob
    if [[ ! -e "$hold_dir" || ( -d "$hold_dir" && -r "$hold_dir" && -x "$hold_dir" ) ]]; then
        local -a hold_entries=("$hold_dir"/*)
        (( ${#hold_entries[@]} )) || return 1
    fi
    return 0
}

needs_decision || exit 0
hook_dir="${BASH_SOURCE[0]%/*}"
printf '%s' "$hook_input" | "$hook_dir/../lib/py" "$hook_dir/pre-tool-use-build-hold-guard.py"
