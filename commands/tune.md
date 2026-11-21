---
description: Publish an approved config value.
---

**Usage:** `/tune <file> <key> <value>` (file is the config stem)

**Arguments:** $ARGUMENTS

Only the user approves a value change. A session that wants a change asks the
user in one line, then runs this command only on the user's go. Moving a value
from code to config with the same value needs no go and does not use `/tune`.

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/tune.py" <file> <key> <value> --why "<one line>" --user-words "<the user's own approval words>"`.
Pass arguments as separate, quoted shell arguments. On the Mac the script
forwards them to natedev. It checks the key and limits, then writes the value
live on natedev and on the Mac at once. It starts each reader that
`config/live_settings.conf` names for the file. Then it commits only that row on
main, pushes, and pulls the Mac. Git fetch, Git push, the Mac write and the Mac
pull have time limits. A stalled or asleep Mac keeps a queued pull. A watcher
on natedev sends it as soon as the Mac answers. Run no test or gate. Add no approval step after
the user's go.

If `<key>.<host>` also exists, the script refuses `<key>` and names both lines.
Name the exact key, such as `sweep_free_floor_gib.mac`, to change that line.

Report the script's result. Exit 2 means a refusal, or a value that is live but
not on main yet: the notes timer commits it when main is free. Exit 1 means the
Mac forwarding failed. `already <key>=<value>` means the value needs no edit.
The script still completes publication and retries the Mac pull.
Exit 0 requires main on origin and a completed or queued Mac pull.
