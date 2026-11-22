# Removal by hand (item 152)

This procedure removes a kept folder by hand. A kept folder is one that a tool kept and listed with this line:

```
kept for a manual removal list: <path> (<tool>, <reason>)
```

Examples: a lint copy under `<target>/package/verify-lint-copy*`, a gate checkout, a scratch folder.

## When it applies

This procedure applies only after nate answers who gives the yes for a removal. Until then, the hold of 2026-11-21 09:19 EST stays: remove no kept folder, by hand or by script. Keep all kept folders on disk.

## The four checks

Do all four checks for each path. If one check fails, keep the folder and tell the showrunner.

1. **Typed path.** A session types the exact path in one command. Use no pattern, no glob, no variable, no script and no background job.
2. **Real path.** `realpath <path>` prints the same path. No part of the path is a link.
3. **Ended run.** The run that made the folder has ended. For a lint copy, the process in its `owner` file is not running.
4. **Nothing only in the folder.** The folder holds no work that is in no other place.
   - For a lint copy, this check passes only in one of three cases:
     - the `return-copy` of that copy ended with success, and the run log has no `verify.sh: not written:` line (such a fix stays only in the copy);
     - the run log proves that no fix step ran on the copy;
     - the owner of the worktree says that the worktree does not need the content of the copy.
   - When `return-copy` failed, refused or withheld a fix, the folder stays until the third case applies.

## The yes

One yes names one path. A yes for one path does not cover another path, a parent folder or a pattern.

Get the yes for the exact path before you write the record line and before you remove the folder.

## The record

Each removal adds one line to `~/.local/state/removal-lists/by-hand.log` on the machine of the folder. The line has these fields, separated by tabs:

1. the time, in ISO 8601 with its zone;
2. the path;
3. the result of check 1 (`typed`);
4. the result of check 2 (`realpath same`);
5. the result of check 3 (`owner <pid> ended`, or the run id that ended);
6. the result of check 4 (`return-copy ok`, `no fix step: <log path>`, or `owner said not needed: <who, when>`);
7. who typed the removal (the session name);
8. who gave the yes, and when.

Write the line in its own command, before the removal command. Remove the folder only when all four results pass.
