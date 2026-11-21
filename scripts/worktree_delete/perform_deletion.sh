#!/usr/bin/env bash
# Verify the checkout and report it for manual removal.
# Usage: perform_deletion.sh <worktree_path> <branch_name> [<preview_id> [--automatic]]
#        perform_deletion.sh --branch-only <worktree_path> <repository> <branch_ref> <preview_id>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

remove_branch_config() {
    local branch=$1 error status
    shift
    if error=$(LC_ALL=C git "$@" config --local --remove-section "branch.$branch" 2>&1); then
        return 0
    else
        status=$?
        if [[ "$status" -ne 128 || "$error" != "fatal: no such section: branch.$branch" ]]; then
            printf '%s\n' "$error" >&2
            return "$status"
        fi
    fi
}

if [[ "${1:-}" == --branch-only ]]; then
    if [[ $# -ne 5 ]]; then
        echo "Usage: perform_deletion.sh --branch-only <worktree_path> <repository> <branch_ref> <preview_id>" >&2
        exit 1
    fi
    REPOSITORY=$3
    if [[ "$REPOSITORY" != /* ]]; then
        REPOSITORY=./$REPOSITORY
    fi
    REPOSITORY=$(cd -P -- "$REPOSITORY" && pwd -P)
    wt_command delete-branch-only "$WT_CONFIRM_DIR" "$5" "$2" "$REPOSITORY" "$4"
    remove_branch_config "${4#refs/heads/}" -C "$REPOSITORY"
    exit
fi

WORKTREE_PATH="${1:-}"
BRANCH_NAME="${2:-}"
PREVIEW_ID="${3:-}"
MODE="${4:-}"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" || $# -gt 4 ]] ||
   [[ -n "$MODE" && ( "$MODE" != --automatic || -z "$PREVIEW_ID" ) ]]; then
    echo "Usage: perform_deletion.sh <worktree_path> <branch_name> [<preview_id> [--automatic]]" >&2
    exit 1
fi

BRANCH_REF="refs/heads/$BRANCH_NAME"
git check-ref-format "$BRANCH_REF"

CALLER=$(pwd -P)
if [[ "$WORKTREE_PATH" != /* ]]; then
    WORKTREE_PATH="$CALLER/$WORKTREE_PATH"
fi
if [[ -d "$WORKTREE_PATH" ]]; then
    WORKTREE_PATH=$(cd "$WORKTREE_PATH" && pwd -P)
fi
IFS= read -r -d '' MAIN_ENTRY < <(git worktree list --porcelain -z)
DELETION_REPOSITORY=${MAIN_ENTRY#worktree }
if [[ "$CALLER/" == "$WORKTREE_PATH/"* || "$WORKTREE_PATH" == "$DELETION_REPOSITORY" ]]; then
    echo "Error: cannot delete the main or current worktree."
    exit 1
fi
wt_command protect-deletion "$WORKTREE_PATH" "$BRANCH_NAME" "$DELETION_REPOSITORY"

ORIGIN_DEFAULT="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
if [[ "$BRANCH_NAME" == main || "$BRANCH_NAME" == "${ORIGIN_DEFAULT#origin/}" ]] ||
   [[ "$BRANCH_NAME" == "$(git branch --show-current)" ]]; then
    echo "Error: refusing to delete protected or current branch: $BRANCH_NAME"
    exit 1
fi
cd "$DELETION_REPOSITORY"

if git show-ref --verify --quiet "$BRANCH_REF"; then
    :
else
    status=$?
    if [[ "$status" -eq 1 && -z "$PREVIEW_ID" && ! -e "$WORKTREE_PATH" && ! -L "$WORKTREE_PATH" ]]; then
        remove_branch_config "$BRANCH_NAME"
        exit 0
    fi
    exit "$status"
fi

if [[ -n "$PREVIEW_ID" ]]; then
    options=()
    if [[ -n "$MODE" ]]; then
        options+=("$MODE")
    fi
    CHECKED_TIPS=$(wt_command consume-deletion "$WT_CONFIRM_DIR" "$PREVIEW_ID" "$WORKTREE_PATH" \
        "$DELETION_REPOSITORY" "$BRANCH_NAME" "${options[@]}")
else
    if ! CHECKED_TIPS=$(wt_command cleanup "$WORKTREE_PATH" --checked-tips 2>&1); then
        echo "Error: no confirmation nonce for $WORKTREE_PATH."
        printf '%s\n' "$CHECKED_TIPS"
        exit 1
    fi
fi

{
    IFS= read -r CHECKED_LOCAL_TIP
    IFS= read -r ORIGIN_ACTION
    IFS= read -r ORIGIN_VALUE || true
} <<< "$CHECKED_TIPS"

check_local_state() {
    local head common expected_common status toplevel
    head=$(git -C "$WORKTREE_PATH" symbolic-ref -q HEAD) || return
    common=$(git -C "$WORKTREE_PATH" rev-parse --path-format=absolute --git-common-dir) || return
    expected_common=$(git rev-parse --path-format=absolute --git-common-dir) || return
    toplevel=$(git -C "$WORKTREE_PATH" rev-parse --show-toplevel) || return
    if [[ "$common" != "$expected_common" || "$head" != "$BRANCH_REF" || "$toplevel" != "$WORKTREE_PATH" ]]; then
        echo "Error: the local worktree identity changed after the deletion check."
        return 1
    fi
    if [[ "$(git rev-parse --verify "$BRANCH_REF")" != "$CHECKED_LOCAL_TIP" ]]; then
        echo "Error: the local branch changed after the deletion check; the worktree stays."
        return 1
    fi
    status=$(git -C "$WORKTREE_PATH" status --porcelain --untracked-files=all) || return
    if [[ -n "$status" ]]; then
        echo "Error: the worktree has uncommitted changes; the worktree stays."
        return 1
    fi
}

wt_command protect-deletion "$WORKTREE_PATH" "$BRANCH_NAME" "$DELETION_REPOSITORY"
check_local_state
case "$ORIGIN_ACTION" in
    keep)
        printf 'Kept origin ref %s: %s\n' "$BRANCH_REF" "$ORIGIN_VALUE"
        ;;
    absent)
        printf 'No origin deletion was selected for %s.\n' "$BRANCH_REF"
        ;;
    delete) ;;
    *) exit 1 ;;
esac

echo "Reviewing worktree: $WORKTREE_PATH"
wt_command remove -- "$WORKTREE_PATH"
