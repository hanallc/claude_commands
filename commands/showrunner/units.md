---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session:
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`.
The caller's staff row selects the production.
Print the full output as Markdown, without code fences or added text.
Include the summary, status table, unit blocks, style table, totals, and anomaly lines when present.

The phase board prints one block per staff unit, sorted by name without case distinctions.
It includes paused units, stopped units, and units without a session.
The first line counts each unit once:
`working N · waiting to merge N · waiting on other work N · idle N · <total> units`.
It inserts `unknown N` before the total only when that count exceeds zero.
Counts include only units in these blocks.

The status table follows the count line, above the blocks.
Its columns are `Unit | Director | Machine | Progress | Phase | ETA Next | ETA Plan`.
- `Director` is the census status of the director's session; its values remain `busy`, `idle`, or the recorded session state.
- `Machine` appears only when a unit uses another machine.
- `Progress` shows one square per plan phase. The `Progress:` legend follows the table.
- `Phase` names each open phase on its own row. With no open phase, it shows done and total phases.
- `ETA Next` is the stated ETA of the phase, with any delay.
- `ETA Plan` appears only while a plan runs.
The table omits a column whose cells are all `—`.
It wraps cells to the pane width, or to `units_table_floor_columns` when the width is unknown.

Each unit has a title, a blank line, and an `Item | Work | State` table.
The table preserves titles, phase IDs, and all wrapped cell text.
Its rows use the same cached plan and run records as the Progress bar.

1. `Now` names the current task and its recorded action, resource wait, or live merge state.
2. `Also` names each additional current task.
3. `Next` names the first queue item, or the first unfinished plan phase.
4. `Later` names every remaining queue item and unfinished plan phase with its own state.
5. `Stopped` names each unresolved stopped task and its recorded reason.
6. `Done` explains a stopped task that a later attempt finished while the bar still includes that attempt.

A busy session does not prove a task.
Each current task has one row.
Several actors share its State cell, joined by semicolons; equal states appear once.
No task row has an empty Work cell; a cell longer than the line limit continues on the next row.
The open phase supplies Now before any merge task.
Without current activity, State names the missing step and the latest finished step with its recorded time.
A queue entry uses `<title after the colon> (<id before the colon>)`.
With or without a colon, title cleanup removes balanced notes, including nested notes, and clauses after the first semicolon.
If cleanup leaves no title, the board uses the entry text with single spaces.
A title that starts with a plain lowercase word gets a capital; file names and IDs stay as written.
A finished activity that matches the queue ID produces a conflict row; the board does not remove the entry.
An unknown next task uses `Next | Not recorded | The director has not chosen the next task`.
Only a readable plan with every phase done permits `Nothing queued; the plan is complete`.
Absent, unreadable, and readable empty plans remain separate facts.

Merge rows name each phase title and its live rank, active merge, or short failure cause.
A checkpoint outside the line uses `Checkpoint <9 hash characters> at <time>; not in <production>'s merge line`.
Without a production name, the text ends with `not in the merge line`.
A finished phase counts as merged when its checkpoint reaches main, its unit and phase have a successful result, or main records its merge.
A finished phase also counts as merged when main has a non-merge commit with the same author time and subject as its checkpoint.
The board reads main's non-merge commits once per render and caches each unresolved checkpoint's identity check.
An unreadable checkpoint stays unmerged.
A successful result for the exact checkpoint hash counts, with or without a valid result time.
Other evidence needs a result time or merge commit time at or after the latest checkpoint time, when recorded.
Without a checkpoint time, the merge subject must name a prefix of the checkpoint hash, in any letter case.
The board ignores an invalid result time.
Merge subjects must start with `Merge <unit> phase <phase> (<hash>)`.
The board reads these subjects once per render and omits older failures for merged phases.
The row uses its normal merge state when the checkpoint is in the merge line or currently merges.
Carried work uses `merges with <owner> <name>`; queue work uses the production from its merge-line label.
Without a recorded owner, carried work uses `merges with <name>`.
A second phase in the same merge uses `Merges with <id>`, or `Merges with phase <id>` for a numeric ID.
The board omits a phase's shrink and repair merge items.
The phase's live state takes priority over older failures from those items.
Queue and plan dependencies use `After <dep>; <short state>`.
A queue dependency on main uses `Ready`; a plan dependency on main or a completed local phase has no wait.
Dependencies outside the merge line say `not in the merge line`.
A local dependency uses its ID, with `phase` before a numeric ID.
An external dependency uses `<unit> phase <id>` without its title.
A bare commit dependency uses `After commit <first 9 hash characters>; <short state>`.
A missing commit, or a commit or branch not on main, uses `not on main yet`; other missing dependencies retain `dependency not found locally`.
A branch dependency uses `After branch <name>; <short state>`.
A recorded phase ID keeps its phase form, even when it contains only hexadecimal characters.
Recorded unit waits and plan `waits_for` conditions retain their text.
With no current task, the first blocked pending task supplies Now with its own Work and State.
Next and Later contain only the remaining tasks, so they do not repeat that wait.
Stopped attempts resolve only through a later completed attempt with the same plan scope and phase ID.
An explicit completion title can name the unit that finished the work.
The board does not infer replacements from equal titles.

Recorded unit waits retain their actor, condition, and target.
Resource waits retain the resource and holder.
A resource wait held by the same unit counts as working.
A resource wait held by another unit, or without a recorded holder, counts as waiting on other work.
The board preserves phase ETA text and uses the production time zone.
Staff, run, plan, queue, dependency, ancestry, and merge reads share command caches.
The board adds no reads to obtain task titles or stopped outcomes.
The staff current run takes priority over newer unrelated runs.

For `Kind: style` or units named in the production's `Style units` table, the table is
`| Unit | Status | Ticket | Step | ETA | Findings built | Last 24 h | Findings left |`.

The style table cuts Step before Ticket to fit the width.
The number columns keep their right alignment when the table omits a column.

- **Ticket**: `#<n> · <rule> · <crate> · <work>`. The number drops the unit
  prefix. The rule is its file stem; an empty rule uses the first stem from
  the rules list, plus `+<k>` for the rest. Work is `<n> findings` for a
  proposal, `<n> sites` for listed work, or `<n> files` for read or structure
  work; 1 uses the singular. The oldest assigned or checkpointed ticket wins.
  With no ticket: `idle (<HH:MM>)` from the last refill, else `idle`.
- **Step**: in words, what the unit does now. A specific activity text from
  the newest run shows as it is (`testing on the Mac`). Otherwise `impl` →
  `writing <work>`, `fix` → `fixing review findings`, `review` → `reviewing`,
  `test` → `testing`, `checkpoint sent` → `waiting for the showrunner to merge`.
  With no open step: `starting <work>` for an assigned ticket, `waiting for the
  showrunner to merge` for a checkpointed one, else `idle — <outcome>` or `idle`.
- **ETA**: the current phase ETA as `<HH:MM>`, else `—`.
- **Findings built**: the all-time built count across the unit's configured families.
- **Last 24 h**: findings first built in the last 24 hours across those families.
- **Findings left**: filed findings with an approved decision across those families:
  `<approved>`, or `<approved> approved of <filed> filed` when not all filed
  findings are approved. A recorded split adds `: ready <a> · changed <b> ·
  structure <c> · other <d> (<age>)`. Age is whole minutes below 60 (`<n> min`),
  whole hours below 24 (`<n> h`), else whole days (`<n> d`), since the scan
  started. A split alone leaves the ticket cell as `idle`.

The total line is `All families: <built> findings built · <approved> approved
findings left[ of <filed> filed] · <r> built in the last 24 h · ETA at that rate
<YYYY-MM-DD>`. `<filed>` counts every filed finding, also one with no family,
and shows only when it differs. The date is now plus `approved / r` days.
When `r` is zero, it ends with `no ETA: nothing built in 24 h`. Numbers use
thousands commas. Times and the date use the production zone.

Read failures that make the table wrong or incomplete appear as `anomaly:` lines below the table.
Staff name conflicts also produce lines. Worker run checks share one process snapshot when needed.
A process with an identity but no staff row produces no line and cannot use a unit's pane placement.
A process without an identity reports an unreadable thread when its pane holds staff.
One staff candidate retains its pane placement with unknown status.
Morty produces no anomaly when its native ID is on the producer staff row named `morty`.
The command sends one message with all anomaly lines to `UNIT_ANOMALIES_TO` in `config/production.conf` (`sdlc`).
A failed send adds one anomaly line. It changes neither the table nor the exit code.

Use `agents` to pause, exit, or resume units:

1. Run `agents pause <unit> --why "<why>"` to stop its workers and hold its director at the prompt.
2. Run `agents exit <unit> --why "<why>"` to stop the director and its workers.
3. Add `--finish-step` to wait for the workers' current tasks before either action.
4. Run `agents pause --production <showrunner> [--except <unit> ...] --why "<why>"` to pause selected units.
5. Run `agents resume <unit>` or `agents resume --production <showrunner>` to resume units.

Each `--except` accepts a staff name, its `-unit` form, or a staff id within that production.
An unmatched value refuses the command before any row changes.
The refusal names the value and the production.
Pause and exit record the time, caller, and reason in the staff row.
A pause lets the director finish its current turn. It starts no new work.
An exit preserves the staff rows, tabs, and shells.
Resume removes the record. A paused director receives one continuation message.
An exited director starts on its configured role account.
Stop does not save or restore composer drafts.

The command reads process identities, terminal metadata, run records, decision files and read-only style ledgers.
It never reads pane contents. Missing ledgers or tables supply no data. Report a non-zero exit as one error line.
