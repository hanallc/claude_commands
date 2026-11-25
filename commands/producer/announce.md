---
description: Send a message to all producers.
argument-hint: "<message>"
---

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --producers "From the user (via <your session name>): <message>"`

It reaches every live producer on both machines and excludes the sender. An open review reports `held`.
`/showrunner:announce` reaches showrunners. `/unit:announce` reaches unit directors. `/announce` reaches all roster groups, including producers.
