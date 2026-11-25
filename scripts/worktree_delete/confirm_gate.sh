#!/usr/bin/env bash
# Send the deletion preview identifier to the worktree command.

WT_CONFIRM_DIR="/tmp/claude/worktree_delete"
WT_CONFIRM_TTL_SECONDS=600

wt_command() {
    "$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../worktree/worktrees.py" "$@"
}
