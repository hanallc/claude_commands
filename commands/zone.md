---
description: Show or set the user's time zone.
---

**Usage:** `/zone [place, IANA zone or label]`

**Arguments**: $ARGUMENTS

Every time shown to the user is written in one zone, `config/user_zone.conf`.
This command shows it or changes it, on both machines.

## Steps

1. With no argument, run
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/zone.py"` and
   show its output. It names the zone, its label, its UTC offset and the time
   now. If a change waits for the Mac, it sends it.
2. With an argument, run
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/zone.py" set "<argument>"`
   on natedev, with the sandbox off. It takes an IANA zone (`America/Curacao`),
   the city of one (`Curacao`, `New York`), or a label only one zone uses. Then it:
   - writes the zone live in `config/user_zone.conf` on natedev and on the Mac, at once;
   - tells every session on both machines with `broadcast.py --all`;
   - commits that file on `~/.claude` main, pushes it and pulls the Mac's `~/.claude`;
   - prints the new zone and the time now.
   The write comes first, so the new zone is in effect before main has it.
3. Exit 2 with a `zone:` line before any `live now` line means nothing changed. Read the message:
   - **No zone has that name** (a city such as `Seattle`): find the IANA zone
     of the place (`America/Los_Angeles`) and run step 2 again with it.
   - **The name fits more than one zone** (a label such as `AST`, or a city two
     zones share): pick the zone of the place the user named, if they named one,
     and run step 2 again. If they did not, ask in one line and name two or
     three of the zones it printed.
4. Exit 2 after `live now` means the zone is live on both machines, but main
   does not have it yet (for example, main is not ready). The notes timer tries
   again. Report the line. Do not repair the checkout.
5. Exit 1 means the zone changed, but some deliveries failed. Report the lines
   that say so.
6. Report in one or two lines: the new zone with its label, the time now, and
   whether the Mac has it now or waits. Write that time in the new zone.

When the Mac write fails, the Mac pull brings the zone, and the sessions'
message goes out at the next bare `/zone` or the next promotion's Mac pull.
