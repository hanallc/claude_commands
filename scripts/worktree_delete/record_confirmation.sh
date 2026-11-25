#!/usr/bin/env bash
# Confirm the exact deletion preview that the user approved.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: record_confirmation.sh <preview_id>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

wt_command confirm-deletion "$WT_CONFIRM_DIR" "$1"
