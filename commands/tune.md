---
description: Publish an approved config value.
---

**Usage:** `/tune <file> <key> <value>` (file is the config stem)

**Arguments:** $ARGUMENTS

Only the user approves a value change. A session that wants a change asks the
user in one line, then runs this command only on the user's go. Moving a value
from code to config with the same value needs no go and does not use `/tune`.

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/tune.py" <file> <key> <value> --why "<one line>" --user-words "<the user's own approval words>"`.
Pass separate, quoted arguments. On the Mac, the script forwards them to natedev.
The script checks the limits, writes the live value on both machines, and starts the configured readers.
It commits the row and pushes main when no land runs.
During a land, a detached publisher waits for a gap. The command returns at once.
If the Mac is offline, the watcher sends the pull when the Mac answers.
Run no test or gate. Add no approval step.

If `<key>.<host>` also exists, the script refuses `<key>` and names both lines.
Name the exact key, such as `sweep_free_floor_gib.mac`, to change that line.

Report the script's result. Exit 0 can mean that main waits for the current land.
Exit 2 means a refusal or a publication failure. The notes timer retries publication failures.
Exit 1 means the Mac forwarding failed. An unchanged value can retry publication or a Mac pull.

For an approved shared `settings.json` edit, apply it on natedev and run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" publish-json --message "<approved reason>"`.
