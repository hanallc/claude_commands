---
description: Send a message to all showrunners.
argument-hint: "<message>"
---

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --showrunners "From the user (via <your session name>): <message>"`

It reaches every showrunner on both machines. Use `broadcast.py --production <your name> --units` for your production's unit directors.
`/producer:announce` reaches producers. `/unit:announce` reaches unit directors. `/announce` reaches all roster groups, including producers.
