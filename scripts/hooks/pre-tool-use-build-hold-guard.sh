#!/usr/bin/env bash
# Check test starts at all times. Check cargo calls only during a hold.

IFS= read -r -d '' hook_input || true
# Match the test start markers in GUARD_CANDIDATE.
continuation='\\\n'
case "${hook_input//"$continuation"/}" in
    *unittest*|*pytest*|*py.test*|*test_*|*proof*|*module_run*|*coverage_overlap*|*merge_gate*|*gate.py*|*targeted_test*|*walled_tests*) ;;
    *cargo*)
        hold_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}"
        shopt -s nullglob dotglob
        if [[ ! -e "$hold_dir" || ( -d "$hold_dir" && -r "$hold_dir" && -x "$hold_dir" ) ]]; then
            hold_entries=("$hold_dir"/*)
            (( ${#hold_entries[@]} )) || exit 0
        fi
        ;;
    *) exit 0 ;;
esac

hook_dir="${BASH_SOURCE[0]%/*}"
printf '%s' "$hook_input" | "$hook_dir/../lib/py" "$hook_dir/pre-tool-use-build-hold-guard.py"
