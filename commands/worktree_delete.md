---
description: Delete a worktree and its branch.
---

PROTECTED_BRANCHES = main
DEFAULT_REMOTE = origin

Track discovery, validation, change checks, confirmation, and deletion with TodoWrite.
Set each step to `in_progress` before execution and `completed` after execution.

<ExecutionSteps>
    If the user asks to retry a local branch deletion, execute <RetryBranchDeletion/> and stop.

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
    Display the deletion details.

    1. The worktree path.
    2. The local `branch_ref` and `local_commit`.
    3. The `origin_action`. For `delete`, show the origin ref and `origin_commit`.
    4. The `unpushed_count` and `remote_only_count` from the same preview.

    Say "Deletion removes these refs at these commits. If no other branch holds a commit, that commit is lost."
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

<RetryBranchDeletion>
    Use this route if deletion stopped after the worktree was removed and the local branch remains.

    1. Ask for the removed worktree path, the repository path, and the full branch ref under `refs/heads/`.
    2. Run `bash ~/.claude/scripts/worktree_delete/worktree_delete_validation.sh --branch-only "$SELECTED_WORKTREE" "$REPOSITORY" "$BRANCH_REF"`.
    3. If validation fails, display the message and stop.
    4. Display the repository, the local ref, its commit, and the `local_only_count` from the preview.
    5. Give the warning in <GetFinalConfirmation/>.
    6. Ask the user to enter `confirm` for deletion or `cancel` to stop.
    7. Wait for the response. If the user enters `cancel`, stop.
    8. Record the new `PREVIEW_ID` with `bash ~/.claude/scripts/worktree_delete/record_confirmation.sh "$PREVIEW_ID"`.
    9. If confirmation fails, display the message and stop.
    10. Run `bash ~/.claude/scripts/worktree_delete/perform_deletion.sh --branch-only "$SELECTED_WORKTREE" "$REPOSITORY" "$BRANCH_REF" "$PREVIEW_ID"`.
    11. Report the result.
</RetryBranchDeletion>
