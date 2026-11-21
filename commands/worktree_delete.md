---
description: Delete a worktree and its branch.
---

PROTECTED_BRANCHES = main
DEFAULT_REMOTE = origin

Track discovery, validation, change checks, confirmation, and deletion with TodoWrite.
Set each step to `in_progress` before execution and `completed` after execution.

<ExecutionSteps>
    1. Execute <DiscoverWorktrees/>.
    2. Execute <ValidateDeletionTarget/>.
    3. If `cleanup` is true, say: "This tree is cleanup; all work is on the target branch."
       Execute <PerformDeletion/> with the preview identifier and `--automatic`. Stop after deletion.
    4. Execute <CheckUncommittedChanges/>.
    5. Execute <CheckUnpushedCommits/>.
    6. Execute <GetFinalConfirmation/>.
    7. Execute <PerformDeletion/>.
</ExecutionSteps>

<DiscoverWorktrees>
    1. Run `bash ~/.claude/scripts/worktree_delete/discover_worktrees.sh`.
    2. Exclude the current worktree and `${PROTECTED_BRANCHES}` from the deletion options.
    3. Display the current worktree and the deletion options.

    If one option exists, select it and announce its path without a question.
    If no option exists, inform the user and stop.
    If multiple options exist, ask the user to select a number or enter `cancel`.
    Wait for the selection before validation.
</DiscoverWorktrees>

<ValidateDeletionTarget>
    1. Run `bash ~/.claude/scripts/worktree_delete/worktree_delete_validation.sh "$SELECTED_WORKTREE"`.
    2. Read the JSON status.
    3. If the status is `error`, display the message and stop.
    4. Store the validation fields, including `preview_id` as `PREVIEW_ID`.
</ValidateDeletionTarget>

<CheckUncommittedChanges>
    If `has_uncommitted` is true, stop and tell the user that deletion would lose the changes.
</CheckUncommittedChanges>

<CheckUnpushedCommits>
    If `has_unpushed` is true, show the count of commits without a known copy on origin.
    If `has_remote_only` is true, show the count of commits without a known copy on another origin branch.
</CheckUnpushedCommits>

<GetFinalConfirmation>
    Display these deletion details:

    1. The worktree path.
    2. The local branch name.
    3. The `origin_action`. For `delete`, show the origin branch and `origin_commit`.
    4. The `unpushed_count` and `remote_only_count` from the same preview.

    Ask the user to enter `confirm` for deletion or `cancel` to stop.
    Wait for the response.
    If the user enters `cancel`, stop.
    If the user enters `confirm`, run:
    `bash ~/.claude/scripts/worktree_delete/record_confirmation.sh "$PREVIEW_ID"`.
    Continue to <PerformDeletion/> only after confirmation succeeds.
</GetFinalConfirmation>

<PerformDeletion>
    1. Use `dangerouslyDisableSandbox: true` when sandbox limits prevent deletion outside the permitted paths.
    2. Run `bash ~/.claude/scripts/worktree_delete/perform_deletion.sh "$SELECTED_WORKTREE" "$TARGET_BRANCH" "$PREVIEW_ID"`.
       Append `--automatic` for a preview that permits cleanup.
    3. Report the result.

    Each preview permits one execution before it expires. If it expires or the files or refs change, create a new preview.
    A `keep` or `absent` preview requires a new preview and confirmation before any later origin deletion.
</PerformDeletion>
