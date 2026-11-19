#!/usr/bin/env bash
# Check test starts at all times. Check cargo calls only during a hold.

IFS= read -r -d '' hook_input || true
continuation='\\\n'
command_text="${hook_input//"$continuation"/}"
test_start=0
case "$command_text" in
    *unittest*|*pytest*|*py.test*|*module_run*|*coverage_overlap*|*merge_gate*|*/gate.py*|*' gate.py'*|*'"gate.py'*|*targeted_test*|*walled_tests*)
        test_start=1
        ;;
    *python*|*/py*|*node*|*bash*|*zsh*|*dash*|*'"sh '*|*'"sh"'*|*'/sh '*|*'/sh"'*|*' sh '*)
        case "$command_text" in
            *test_*|*proof*) test_start=1 ;;
        esac
        ;;
esac

if (( ! test_start )); then
    case "$command_text" in
        *cargo*) ;;
        *) exit 0 ;;
    esac
    hold_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}"
    shopt -s nullglob dotglob
    if [[ ! -e "$hold_dir" || ( -d "$hold_dir" && -r "$hold_dir" && -x "$hold_dir" ) ]]; then
        hold_entries=("$hold_dir"/*)
        (( ${#hold_entries[@]} )) || exit 0
    fi
fi

hook_dir="${BASH_SOURCE[0]%/*}"
printf '%s' "$hook_input" | "$hook_dir/../lib/py" "$hook_dir/pre-tool-use-build-hold-guard.py"
