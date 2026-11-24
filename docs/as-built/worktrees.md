# Worktrees

`worktrees.py make <repo word> <unit> <short-name>` creates a checkout, a branch, and an assigned Cargo output folder.
The checkout's `target` link points to that output folder.
The worktree record stores the unit, short name, and output path.
See [Build folders](build-folders.md) for the paths, name rules, and move commands.

A managed unit move requires a branch and records it from the destination checkout's HEAD.
The move journal stores this requirement for recovery.
If a managed move stops before relocation, resume it with `worktrees.py move`.
Migration also supports detached checkouts.
`worktrees.py record-branch <path>` updates an existing record from the checkout's current branch.
The command prints the old and new branch names.
It verifies that the checkout belongs to the recorded repository.
It refuses an unrecorded checkout or a detached HEAD.

Landed cleanup reports the checkout and its assigned output for manual review.
It keeps the worktree record until the checkout and its Git registration are absent.
If worktree setup fails, the checkout stays for review.

Worktree creation releases its source lease before it prepares the target link and calls `write_record()`.

`worktrees.py remove <path>` reports the checkout and its assigned output.
Agent shutdown uses the same review helper for a unit checkout.

A change review row's try folder is the worktree `worktrees.py make <repo> <team> try-C<n>`.
The row's verdict or withdraw calls `remove_named(<team>, "try-C<n>")`.
An empty Producer cell names the list's showrunner. Only a filled cell reads staff, through `change_routing.producer_row`.
It finds the try folder through the worktree records on the local machine and sends it to the same review helper.
A try folder on another machine stays, and its team removes it there.
It keeps a checkout with uncommitted changes or with commits that are on no remote.
For a kept checkout it returns one line with the path, the reason and the `worktrees.py remove` command.
For an error it returns `<path> not removed: <first line of the error>`.
The removal by hand of a try folder also deletes its branch `<team>-try-C<n>`.
`worktrees.py retire <unit>` reports each build folder for the unit.

The removal helper releases the source pair after the checkout is absent.
Production cleanup can also release a pair after removal.
The release requires an absent folder path and a free lock.
A symbolic link in the parent path prevents release.
A held lock leaves both files in place and reports the cause.
The release reports when it removes only part of the pair.
The route continues after a pair release warning.
