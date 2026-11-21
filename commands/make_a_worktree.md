---
description: Create a Git worktree and branch.
---

1. Get the repo word, unit name, and short name. Use the current repo word if the user omits it.
2. Find the source at `~/rust/<repo>`. For `claude`, use `~/.claude`.
3. Show the repo, unit, short name, and branch `<unit>-<short-name>`. Wait for approval.
4. Run `bash ~/.claude/scripts/worktree/make_worktree.sh <repo> <unit> <short-name>`. Keep its output path.
5. Check the branch with `git -C <path> branch --show-current`. Keep the current working directory.
6. Run `bash ~/.claude/scripts/make_a_worktree/copy_settings_local.sh <path>`.
7. Run `bash ~/.claude/scripts/make_a_worktree/direnv_allow.sh <path>`.
8. Report the path and branch. If a command fails, report the error and stop.
