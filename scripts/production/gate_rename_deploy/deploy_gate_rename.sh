#!/usr/bin/env bash
# Gate rename chunk 6 deletes this folder and its configuration after deployment.
set -eu
command_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
checkout=$(CDPATH='' cd -- "$command_dir/../../.." && pwd)
export PYTHONPATH="$checkout${PYTHONPATH:+:$PYTHONPATH}"
exec "$checkout/scripts/lib/py" "$command_dir/deploy_gate_rename.py" "$@"
