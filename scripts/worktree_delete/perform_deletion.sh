#!/usr/bin/env bash
# Removes a worktree and deletes its branch, locally and on origin.
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion
#
# Cleanup deletion requires all work to satisfy the landing checks.
# Other deletion requires the confirmation nonce from record_confirmation.sh.

set -euo pipefail

WORKTREE_PATH="${1:-}"
BRANCH_NAME="${2:-}"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: perform_deletion.sh <worktree_path> <branch_name>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

HAS_CONFIRMATION=false
CHECKED_LOCAL_TIP=""
if [[ -f "$(wt_confirm_file "$WORKTREE_PATH")" ]]; then
    if ! wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME"; then
        exit 1
    fi
    HAS_CONFIRMATION=true
else
    if ! CHECKED_TIPS=$("$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../worktree/worktrees.py" cleanup "$WORKTREE_PATH" --checked-tips 2>&1); then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        printf '%s\n' "$CHECKED_TIPS"
        exit 1
    fi
    read -r CHECKED_LOCAL_TIP _ <<< "$CHECKED_TIPS"
    if [[ "$(git -C "$WORKTREE_PATH" branch --show-current)" != "$BRANCH_NAME" ]]; then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        echo "Error: the worktree branch does not match $BRANCH_NAME."
        exit 1
    fi
    if [[ "$(git -C "$WORKTREE_PATH" rev-parse --path-format=absolute --git-common-dir)" != \
          "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        echo "Error: the worktree belongs to a different repository."
        exit 1
    fi
fi

# Refuse a branch lookup fault before origin deletion.
if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    if [[ "$HAS_CONFIRMATION" == true ]]; then
        CHECKED_LOCAL_TIP=$(git rev-parse --verify "refs/heads/$BRANCH_NAME")
    fi
else
    BRANCH_LOOKUP_STATUS=$?
    if [[ "$BRANCH_LOOKUP_STATUS" -ne 1 ]]; then
        exit "$BRANCH_LOOKUP_STATUS"
    fi
fi

ORIGIN_DEFAULT="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
CURRENT_BRANCH="$(git branch --show-current)"
if [[ "$BRANCH_NAME" == "main" || "$BRANCH_NAME" == "${ORIGIN_DEFAULT#origin/}" || "$BRANCH_NAME" == "$CURRENT_BRANCH" ]]; then
    echo "Error: refusing to delete protected or current branch: $BRANCH_NAME"
    exit 1
fi

if [[ "$WORKTREE_PATH" != /* ]]; then
    WORKTREE_PATH="$PWD/$WORKTREE_PATH"
fi

# Run the review from the repository's main checkout.
MAIN_WORKTREE="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
if [[ -n "$MAIN_WORKTREE" && -d "$MAIN_WORKTREE" ]]; then
    cd "$MAIN_WORKTREE"
else
    echo "Warning: could not resolve the main worktree; staying in $PWD"
fi

echo "Reviewing worktree: $WORKTREE_PATH"
if [[ -n "$CHECKED_LOCAL_TIP" ]] && [[ "$(git rev-parse --verify "refs/heads/$BRANCH_NAME")" != "$CHECKED_LOCAL_TIP" ]]; then
    echo "Error: the local branch changed after the cleanup check; the worktree stays."
    exit 1
fi

remove_options=()
if [[ "$HAS_CONFIRMATION" == true ]]; then
    remove_options+=(--force)
fi
exec "$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../worktree/worktrees.py" remove "${remove_options[@]}" -- "$WORKTREE_PATH"
