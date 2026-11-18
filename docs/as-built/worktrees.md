# Worktree removal and leases

`build_folders.py` writes `<worktree>.lease.json` and `<worktree>.lock` beside each managed worktree.
The lock file uses `flock`. The lease record describes the allocation.

`remove_folder_with_lease` takes the exclusive folder lock before it calls the removal code.
Collectors that already hold the lock use the same removal code through `HeldFolderRemovalLease.remove`.
A live holder causes deletion to stop with a lock diagnostic.
The removal subprocess inherits the lock descriptor.
Removal steps check that the visible parent still matches the locked parent.
The helper removes unfinished lease records first, then the canonical record, then the lock.
An unlink failure stops cleanup and reports the file that remains.
It does this only after the removal succeeds and the path is absent.
If removal fails or leaves the path in place, the helper keeps the files.

The merge uses the helper in `worktrees.py end_record`.
`worktrees.py make` also uses it to undo creation if the worktree record cannot be written.
`perform_deletion.sh` uses the same helper for normal removal and the confirmed force and residual cleanup paths.
Both the merge and the deletion command acquire the lock before deleting the origin branch.
The production wrap acquires the lock before it parks the unit configuration and removes the worktree.
The shutdown retire command acquires the lock before Git removes the worktree.
Both retirement routes check the lease files on a retry when the worktree is already absent.

The slot sweep holds the folder lock during rescue and deletion of an expired slot.
If Cargo cleanup fails after the worktree is gone, a later sweep can finish cleanup under the lock.
The slot sweep also finishes removal when only lease files remain.
Managed gate deletion holds the gate lock, child lease locks, and busy locks during rescue, deletion, and Git prune.
A canonical lease record must identify the gate before cleanup can remove its sidecars.
Each sidecar must be a regular file with one link.
Cleanup checks the record and file identities again while it holds the lock.
A live holder or a sidecar conflict keeps the group.
The dry run and real run use the same preparation.
The dry run predicts repository emptiness from the prepared entries.
The real run checks that the repository is empty before it removes it.
The folder audit checks the visible path before it passes the path to source deletion.
The audit removes the lease files after the clone is gone.
Allocation checks the final folder name for control characters and reserved sidecar names.
An ancestor cleanup keeps the parent if a child directory has a reserved sidecar name.
The temporary sweep uses the folder decision and the age of every sidecar to judge the group.
It checks the lease record and removes the verified group under the same lock as the folder.
The hand cleanup plan lists each removal with its device and inode.
Applying the plan keeps a group if any current entry has no matching identity in the plan.
Task clone removal records the verified file identities before removal so a retry can check the remaining sidecars.

See [Original worktrees and landing](gate-checkouts.md#original-worktrees-and-landing) for the checks that permit deletion.
