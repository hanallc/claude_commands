---
description: Change a waiting unit's build priority.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS --words "/build:bump $ARGUMENTS"`.
Show its output in a `text` block.
Use `/build:page` for the Builds page.

`--words` carries the user's command as the reason for the bump, so the user types nothing more.
When natemerge bumps on nate's standing permission, it passes `--words "natemerge can bump at will"` in place of the command text.
The service refuses a bump or clear from a session without `--words`; a plain shell without `--words` records "typed by nate in a plain shell".

`<position|session|folder>` selects a waiting step and adds its unit to the priority list.
`--run <position|session|folder>` raises only the run of the selected step; other runs of the unit keep their place.
A run entry ends when its run ends.
A unit already in the list keeps its place.
`clear` empties the priority list.
`clear <unit>` removes the unit and its one-run entries.
The output and the Builds page name each entry with the staff name that asked and its words.
An unmatched selector, unknown unit, or step with no run for `--run` exits 3 with a refusal.
A bare `/build:bump` prints the usage and the Builds page link.
These controls work from any session or a plain shell.
