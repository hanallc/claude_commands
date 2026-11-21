---
description: Check a worktree and report it for manual removal.
---

PROTECTED_BRANCHES = main

Track discovery, validation, change checks, and reporting with TodoWrite.
Set each step to `in_progress` before execution and `completed` after execution.

<ExecutionSteps>
    1. Execute <DiscoverWorktrees/>.
    2. Execute <ValidateTarget/>.
    3. Execute <ReportTarget/>.
</ExecutionSteps>

<DiscoverWorktrees>
    1. Run `bash ~/.claude/scripts/worktree_delete/discover_worktrees.sh`.
    2. Exclude the current worktree and `${PROTECTED_BRANCHES}` from the options.
    3. Display the current worktree and the options.

    If one option exists, select it and announce its path.
    If no option exists, tell the user and stop.
    If multiple options exist, ask the user to select a number or enter `cancel`.
    Wait for the selection before validation.
</DiscoverWorktrees>

<ValidateTarget>
    1. Run `bash ~/.claude/scripts/worktree_delete/worktree_delete_validation.sh "$SELECTED_WORKTREE"`.
    2. Read the JSON status.
    3. If the status is `error`, display the message and stop.
    4. Store `target_branch` as `TARGET_BRANCH` and `preview_id` as `PREVIEW_ID`.
</ValidateTarget>

<ReportTarget>
    Display the worktree path, `branch_ref`, and `local_commit`.
    Display the `origin_action` from the preview and the observed `origin_commit` when present.
    Display `retention_reason` if the preview marks the origin ref for retention.
    For `has_uncommitted`, report the changes and stop.
    For `has_unpushed`, report `unpushed_count` as the commits without a known copy on origin.
    For `has_remote_only`, report `remote_only_count` as the commits without a known copy on another origin branch.

    If `cleanup` is true, complete these steps.

    1. Run `bash ~/.claude/scripts/worktree_delete/perform_deletion.sh "$SELECTED_WORKTREE" "$TARGET_BRANCH" "$PREVIEW_ID" --automatic`.
    2. Report the checkout and output paths from the script.

    If `cleanup` is false, report the preview and its reason.
    If the preview expires or the files or refs change, validate the target again.
    Tell the user that the checkout and refs stay in place.
</ReportTarget>
