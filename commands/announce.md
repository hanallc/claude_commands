---
description: Send a message to all agents.
argument-hint: "<message>"
---

## Message text

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

## Delivery

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --all "From the user (via <your session name>): <message>"`

It reaches all roster groups on both machines, including producers.
The footer names the recipient groups and asks them to keep the message within those groups.

Put a role flag after `--all` to give a group its own version. Omit only the part that does not concern that group.
Use `--showrunners "<text>"`, `--units "<text>"`, or `--agents "<text>"`.

One session: `send.py --to <name>`, with `--machine <other machine>` on the other machine (/message). The user: /alert_user.

`/producer:announce` reaches producers. `/showrunner:announce` reaches showrunners. `/unit:announce` reaches unit directors.
