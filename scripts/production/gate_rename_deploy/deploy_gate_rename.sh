#!/usr/bin/env bash
# Gate rename chunk 6 deletes this folder and its configuration after deployment.
set -eu
command_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
checkout=$(CDPATH='' cd -- "$command_dir/../../.." && pwd)
for name in PYTHONPATH CLAUDE_PYTHON XDG_CACHE_HOME; do
    if [[ -v "$name" ]]; then
        printf 'Refused: Unset %s before deployment.\n' "$name" >&2
        exit 2
    fi
done
exec "$checkout/scripts/lib/py" --isolated "$command_dir/deploy_gate_rename.py" "$@"
