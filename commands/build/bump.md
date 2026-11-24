---
description: Change a waiting unit's build priority.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`.
Show its output in a `text` block.
Use `/build:page` for the Builds page.

`<position|session|folder>` selects a waiting step and records a bump for its unit.
`clear` records a request to empty the priority list.
`clear <unit>` records a request to remove one unit.
The command prints `recorded` with an id. The queue applies the record at its next pass.
A unit already in the list keeps its place.
The Builds page shows records that wait or could not apply.
An unmatched selector or unknown unit exits 3 with a refusal.
A bare `/build:bump` prints the usage and the Builds page link.
These controls work from any session or a plain shell.
