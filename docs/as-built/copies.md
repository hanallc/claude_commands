# Repository copies

`scripts/copies/copies.py` owns repository copies for builds.
The guard permits only original worktrees, the external upstream clone, and this implementation.

`CommittedSource` selects a revision.
`WorktreeSource` applies staged, unstaged, and untracked changes to a revision.
`StagedSource` selects only index content.
`NoMerge` keeps that source.
`MergeRevision` adds a merge after source preparation.
The CLI converts its flags into these policies.

Staged copies use a private source index and a synthetic commit without changing source refs.
Each gate takes one detached repository slot through `build_slots.take_slot()`.
The machine and repository select the pool; the owner does not select its path.
The caller holds the slot lease through preparation, execution, and result publication.
The lease keeps the staff id in `unit` and `gate` in `work`; `purpose` names the gated source revision's subject, including merge gates.
A `<kind>(<slug>): phase <N> — <title>` subject gives `gate · <slug> phase <N> · <title>`; other subjects give `gate · <subject>`, and unreadable subjects give `gate`.
Identical nested proofs reuse their held slot.
Other revisions take another free slot or wait in the build queue.
The configured `slots.wait_seconds` limits that wait.
The Mac uses a finite kernel-lock wait because it has no admission service.

Acquisition rescues source work before reset.
Git resets changed files and retains unchanged file times.
Default final Cargo output stays under the slot's `target/`.
Acquisition replaces a target symlink with a directory inside the slot.
Each slot uses its own Git `info/exclude` to hide `target/` from status.
`CARGO_BUILD_BUILD_DIR` defaults to the slot's separate Cargo directory.
Explicit caller output settings remain unchanged.
Nested slots replace the enclosing slot's default directory with their own default.
An acquisition above its configured size limit deletes generated output once.
A failed rescue retains the source and output.
A damaged index is saved as `.rescue-index-*` with source edits in the rescue commit.
Recovery rebuilds the index before reset.
A failed rescue restores the damaged index.
Incomplete slot recovery rescues linked worktrees before deletion.
Recovery retains a repository with a different object store.
Acquisition removes a stale worktree index lock under the slot lease.
Rust release trials retain Cargo mend output under `target/mend-target`.
Slot preparation failures raise `CopyError` with command details.
A lease release failure after use raises `PostUseError` and retains the caller error.

`workspace()` keeps a named editable checkout under `roots.workspace`.
The maker creates its folder, and the caller holds its lease during use.
Edits and the selected branch remain between uses.
Removal rescues work under the removal lease and retains the rescue in the source repository.
The rescue includes clean local branches, tags, and every stash reflog entry.
When no local source remains, removal exports the rescue into a sibling bare repository named `<workspace-root>-rescues.git`.
Removal verifies the exported rescue before it deletes the workspace.
Python callers pass slot lease descriptors to their build and test children.
`clone()` keeps its task-owned return-a-path API and uses the maker outside the gate root.
Its callers retain their existing task cleanup.

`copies.py list` reads slot leases and remote mirror rows.
`copies.py remove` removes owned editable and remote copies; it never removes a shared slot.
The nightly slot sweep removes old free slots.
`gates_root()` reads the configured machine slot root without creating it.
The legacy `copies.root` key remains in configuration, but no code reads it.

`RemoteMirror` records the owner, repository, host, mirror path, and holder session.
The `remote_registry` key in `config/copies.conf` selects the remote registry.
Cutover moves the old `.remote-mirrors` folder and its lock files to that path before new commands run.
Mac, Linux, and AWS copies use the same kind.
The Mac path uses the validated `MacMirrorRoot` setting.

The mirror lease covers the claim read and the copy-record lock.
The claim reader releases each admission lock before it returns its snapshot.
`mirror_metadata_locks` holds only the copy-record lock.
An outside mirror lease protects task parent removal.
After acquisition, it rejects a missing or replaced lease path as busy.
It deletes the outside lease file after parent removal, while both leases remain held.
The existing sibling lease still protects a live lease from the old code.
The receiver keeps `.copy-record.json`, `.copy-session`, and `.mac-test-mirror` during rsync deletion.
Remote commands inherit the mirror lease.
Process inspection also keeps mirrors with a working directory, a mapped file, or a file open for writing.
A file open only for reading does not make a mirror busy.
Unreadable process state prevents removal.

The Mac floor sweep acquires idle mirror leases before it reads claims.
It retains these leases through removal.
It reads claims again before each removal group outside the acquired leases.
The Linux floor sweep holds mirror leases while it checks and removes idle output.
Admission takes available slot leases before it selects a claim.
It releases unused slot leases after claim selection.
It skips busy mirrors and preserves their records.
Cancellation closes its acquired descriptors.
The lock files retain their inodes.

`copies.py remove` takes the owner from the caller's staff row and includes that owner's remote mirrors within the repository scope.
Retirement and production cleanup use the same removal path before deleting source trees or production state.
Busy removal names the holder session and mirror path.
A host connection failure marks the local mirror record for deferred removal, so retirement can finish.
Removal tries an unreachable host only once per call.
`register_remote` retries deferred removals after the next successful copy or remote clone on that host.
Registration replaces the current path's record first, so reuse clears its removal mark.
A failed retry keeps the mark and prints one anomaly without failing the copy.
A successful retry removes the record.
Removal requires a zero status marker from the remote command before it deletes the local record.
Authentication failures and remote removal refusals still raise `RemoteMirrorKept` and prevent retirement.
A kept local tree prints one line, and retirement continues.
The sweep removes the local tree later.
Another owner's records remain.

Local mirror rows display the machine name while their records retain `localhost`.
List and sweep reads unregister local mirrors whose checkout is gone.
A lease with `create=False` creates no parent folder.
Clone metadata staging uses a temporary context inside the checkout and disappears after a failure.
