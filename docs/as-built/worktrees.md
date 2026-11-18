# Worktree removal

Landed cleanup and creation rollback remove the worktree under the folder lock.
They keep the worktree and both files while another process holds the lock.
They remove the lease record and lock file after successful worktree removal.

Worktree creation releases its lease before it calls `write_record()`.

Shell deletion, agent shutdown, and production retirement release the pair after worktree removal.
The release requires an absent folder path and a free lock.
A symbolic link in the parent path prevents release.
A held lock leaves both files in place and reports the cause.
The release reports when it removes only part of the pair.
The route continues after a pair release warning.
