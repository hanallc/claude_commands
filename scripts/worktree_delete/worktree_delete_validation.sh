#!/usr/bin/env bash
# Preview the deletion and return its identifier with the loss counts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

if [[ "${1:-}" == --branch-only ]]; then
    if [[ $# -ne 4 ]]; then
        echo "Usage: worktree_delete_validation.sh --branch-only <worktree_path> <repository> <branch_ref>" >&2
        exit 1
    fi
    wt_command branch-only-preview "$2" "$3" "$4" "$WT_CONFIRM_DIR" "$WT_CONFIRM_TTL_SECONDS"
    exit
fi

wt_command deletion-preview "${1:-}" "$WT_CONFIRM_DIR" "$WT_CONFIRM_TTL_SECONDS"
