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
Local gates take a detached numbered copy through `build_slots.take_copy()`.
The repo word and unit select the parent folder under the host's build root.
`copies.standing()` uses the caller's staff unit name when it finds the staff row.
The maker takes the lowest free existing number or creates the smallest missing number.
An identical nested committed proof can reuse its held copy.
A run can still wait for normal build admission.
The AWS gate uses its separate slot path.

The Hana AWS guest uses its main checkout at `~/rust/hana` for bundle fetches.
The sender stages the bundle in the source repository's common Git directory.
The guest stages the payload inside the main checkout and the bundle inside its Git directory.
The runner takes the assigned copy number under `~/build/hana/aws-hana`.
The receiver probes that numbered copy's lock before it reports lease release.
The shell cache lives at `~/build/hana/main/shells`.

The caller holds the copy lease through preparation and execution.
The lease records the source checkout, commit, Git directory identities, and holder.
Python callers pass the lock descriptor and its environment setting to build and test children.
The copy becomes free when the kernel lock is free.
A lease release failure after use raises `PostUseError` and retains the caller error.

The maker rescues source work before Git resets the copy to the requested commit.
Git retains the file times of unchanged files.
The copy keeps a real `target/` folder and excludes it from Git status.
The lease environment sets both `CARGO_TARGET_DIR` and `CARGO_BUILD_BUILD_DIR` to that folder.
A nested copy uses its own Cargo output.
The maker keeps Cargo output for the next use.
A failed rescue retains the source and output.
Recovery moves a damaged index to `.rescue-index-*` while it rebuilds the index and rescues source edits.
Recovery restores the damaged index if the rescue fails.
The maker removes a stale worktree index lock under the copy lock.
Rust release trials retain Cargo mend output under `target/mend-target`.
Copy preparation failures report the command details.

Use `worktrees.py make <repo> <unit> <short-name>` for a new editable checkout.
`workspace()` can resume an existing legacy editable copy and hold its lease.
The resumed workspace retains its edits and branch between uses.
Removal rescues work under the removal lease and retains the rescue in the source repository.
The rescue includes clean local branches, tags, and every stash reflog entry.
When no local source remains, removal exports the rescue into a sibling bare repository named `<workspace-root>-rescues.git`.
Removal verifies the exported rescue before it deletes the workspace.
`clone()` keeps its task-owned return-a-path API and uses the maker outside the numbered copy root.
Its Git children inherit the destination lease and busy lock.
The saved-work check trusts cached remote-tracking refs when they cover all local commits.
Otherwise, removal runs `git ls-remote` in the clone once per named remote.
Git uses the named remote's configured transport.
Git resolves URL rewrites before the publication check reads a remote.
The check keeps the clone if a local remote uses Git directories or alternate object directories inside the removal tree.
It keeps the clone if it cannot verify that the local remote's Git storage is independent.
The check trusts the configured transport programs to use the named remote.
Only advertised IDs that exist locally can cover commits in the second check.
This check reads current HEAD, branches, and tags after the remote commands.
Peeled tag IDs can cover commits.
Removal keeps the clone if the check cannot prove publication of every commit.
Terminal workspace cleanup can remove an unpublished workspace after it verifies a rescue outside the workspace.
A newer remote tip that is absent locally cannot supply ancestry evidence.
The remote evidence applies at the time of the advertisement.
The `removal_remote_timeout_seconds` setting gives all remote queries for one repository the same deadline.
The caller stops reading output at the deadline.
The portable runner stops the Git process group when the command completes or reaches the deadline.
The runner stops the group even if the direct command or the removal caller has exited.
The query disables credential prompts.
The Git reads discard inherited trace settings and disable configured Trace2 targets.
The query leaves the clone's objects, refs, and `FETCH_HEAD` unchanged.
Cargo recovery reuses advertised IDs for the same repository only while the removal locks remain held.
It checks current local work again when recovery requires revalidation.
Dry runs, lifetime scans, and linked worktree checks use local Git evidence.
Actual removal of a landed clone can query the remotes under the removal locks.
When a task lists a folder and holds it, one removal attempt supplies the result for both.
Removal retries take the busy lock again, even if an earlier attempt removed the mark.
Recovery ignores the timestamps of busy marks that the current attempt creates.
A removal retry checks `.git`, `commondir`, and `HEAD` at the removal path and in descendant checkouts.
An external `HEAD` must exist. An interrupted removal can leave an internal `HEAD` absent.
Both scans check the types of present Git metadata, including `index`, `packed-refs`, and loose refs.
One walker reports each entry's path, type, device, inode, and ctime before the scan applies its checks.
The walker opens the removal root directly and opens each child directory through its parent descriptor.
The walker checks each directory's identity before it reads the entries and again before it closes the descriptor.
Checkout inspection supplies the expected identities of the Git directories.
The scan reads a symbolic ref's target without following the link.
The scan accepts the link only if the target names a valid `refs/` path.
The retry checks work again if a regular file or link has a ctime newer than the intent.
Equal timestamps count as unchanged.
The retry checks directories by type and recorded evidence.
It keeps a checkout if a directory occupies the recorded path of a loose ref.
For a legacy intent, it keeps an empty ref directory outside `refs`, `refs/heads`, `refs/tags`, and `refs/remotes`.
At those paths, it keeps an empty directory if a regular reflog or `packed-refs` names the exact ref.
Without that evidence, a legacy intent cannot distinguish an empty standard namespace from a loose ref that a directory replaced.
The retry accepts the empty standard namespace in that case.
Writers must honor the removal locks through deletion, including during the remote queries.
`save_cargo_outputs` reads Cargo metadata with `--locked` to preserve source lockfiles.
`clean_cargo_outputs` deletes only build output outside the source tree.
A retry with Cargo work validates the source again after these helpers.
A first removal records source evidence after these helpers.
Linked checkout creation refuses a source with a removal mark.
Recursive removal checks the busy locks and recorded borrowers of existing descendant clones.
Busy marks and copy records must be regular files.
Task cleanup removes the clone and clears the removal marks before it tries to remove the lease file and lock file.
Cleanup holds the lock and checks the identities of those two files before it removes either file.
Recovery leaves the files if the clone is already absent when recovery starts.

The borrow check keeps sources with recorded borrowers or unreadable copy records.
It checks the copy folders in the registry and the adopted copies in the holds file.
It also checks numbered copy source records before it removes a Git source.
It skips an unregistered folder if the folder's parent is directly under the copies root and contains `.git`.
This exception applies only if the folder contains none of `.git`, `HEAD`, `objects`, or `commondir`.
The check inspects these entries without following symbolic links.

`copies.py list` reads numbered copy leases and remote mirror rows.
`copies.py remove` removes owned legacy editable copies and remote mirrors.
`gates_root()` reads the configured build root without creating it.
The two-day sweep lists idle numbered copies with expired use stamps for manual review.
See [Build folders](build-folders.md#numbered-copies-and-removal) for the lock and removal rules.

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
