---
description: Change a waiting unit's build priority.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_bump.py $ARGUMENTS`.
Show its output in a `text` block.
Use `/build:page` for the Builds page.

`<position|session|folder>` selects a waiting step and adds its unit to the priority list.
A unit already in the list keeps its place.
`clear` empties the priority list.
`clear <unit>` removes one unit.
An unmatched selector or unknown unit exits 3 with a refusal.
A bare `/build:bump` prints the usage and the Builds page link.
Only the user and current showrunner sessions may change priority.
Use `build_queue.py [position|session|folder]` to read the queue.
The reader treats `clear` as a filter.
