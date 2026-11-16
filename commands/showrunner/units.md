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
Its columns are `Unit | Status | Machine | Progress | Phase | ETA Next | ETA Plan`.
- `Status` is the census status of the unit's session.
- `Machine` appears only when a unit uses another machine.
- `Progress` shows one square per plan phase. The `Progress:` legend follows the table.
- `Phase` names each open phase on its own row. With no open phase, it shows done and total phases.
- `ETA Next` is the stated ETA of the phase, with any delay.
- `ETA Plan` appears only while a plan runs.
The table omits a column whose cells are all `—`.
It wraps cells to the pane width, or to `units_table_floor_columns` when the width is unknown.

Each title is `**<unit>** — <bucket>`.
When any unit uses another machine, each title includes its machine in parentheses.
The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.
One blank line separates each title from its `Item | Work | State` table.
Each item uses one row before wrapping.
`Now`, `Also`, and `Merge` use their label alone for one item; multiple items use `Now 1`, `Now 2`, and equivalent labels.
Queue items use `Next 1`, `Next 2`, and later numbers, including `Next 1` for a single item.
`Plan next` follows the queue rows.
Without queue or plan work, a `Next` row has Work `—`.
State uses short phrases separated by semicolons, with an initial capital and no final period.
Work contains a phase title and ID, a shell name, comma-separated merge IDs, or a free wait target.
A queue item's Work is its text before the first colon, or its full text without a colon.
The table omits queue step text after the colon.
Phase titles keep their capital letters.
`display_cells.escaped` escapes cells.
`display_cells.wrapped(..., keep=(0, 1, 2))` preserves all three columns and uses the status table's width.
Continuation rows preserve complete cell text without an ellipsis.

The summary uses the first matching rule:

1. `working`: a matched worker, command, director activity, or busy director with an unsent open phase executes.
2. `idle`: the unit has a pause or stop, or readable records show no remaining work and no busy director.
3. `waiting on other work`: an open phase or director command has a recorded wait.
4. `waiting to merge`: sent work waits, merges, or failed; a unit wait can also keep done work from the merge.
5. `waiting on other work`: the first next item has a recorded wait, or a unit wait has no matching target.
6. `unknown`: the available facts explain no earlier state.

`Now` names each open phase as `<title> (<phase id>)`.
It removes a repeated phase id from the title's start.
Tasks follow plan order, then start order for work outside the plan.
Executing tasks appear first.
When a task executes, other open tasks appear under `Also`.
A recorded pass role supplies State, such as `Implementation worker runs it`.
The pass's worker id joins the census first; its phase assignment supplies the fallback.
Finished passes supply no current worker.
A running detached command says `<Role> runs <label>; waits for the result`.
An unread result says `Worker finished; director has not read the result`.
A director activity says `Director <activity>; no worker needed`.
A busy director without a recorded step says `Director works on it; no worker runs; step not recorded`.
An unexplained open task says `Open; nothing runs; reason not recorded`.

A memory wait's State says `Needs build memory in use by <holder>; since <time>`.
A slot wait's State says `Waits for a build slot used by <holder>; since <time>`.
Other named resource waits say `waits for <reason>, in use by <holder>`.
An unnamed holder says `holder not recorded` and preserves the recorded reason.
A holder change preserves the wait's original start.
A wait belongs to its task; it does not hide another worker's work.
Director commands retain their recorded resource waits.
A promise appears once per phase, on its first displayed task.
An overdue promise appends `ETA <time>, late <duration>` to State.
Times use the production zone, with `HH:MM` today and a date on other days.

Without a current task, `Now` states the recorded pause, stop, first next wait, or unexplained delay.
A missing pause actor uses `The unit is paused`; a missing stop actor uses `The unit is stopped`.
A missing pause or stop reason uses `reason not recorded`.
Fallback rows use Work `—`.
Empty work says `Nothing to do; the plan is complete; queue empty`.
Without a plan, it says `Nothing to do; no plan or queued work`.
A failed census adds a separate Now row: `Workers unavailable; who runs cannot be determined`.
No-worker mode adds a separate Now row: `Workers cannot start because no-worker mode is on`.

`Merge` prints each merge group once, with active merges before merge line order.
A single phase's State says `Done; 1st in the merge line`.
A shared group's State says `Done; merge together; 2nd in the merge line`.
An active merge says `Done; merging now`.
A carried commit names its carrier unit and phase.
A failure names its cause and time.
Submitted work without a position says `Submitted; not in the merge line; reason not recorded`.
A line that has no record says `merge position not recorded`.
Sent open phases appear only under `Merge`.
The board has no daily merge count.

Directors record a unit wait with `progress_history.py start-wait --target WORK --by WHO --reason CONDITION --kind KIND`.
The target can name a phase or a queue item's exact leading ID.
The command requires `--by` and `--reason` with `--target`.
The record belongs to the run, with no phase or pass attribution.
`finish-wait --wait-id ID`, target phase start, or run finish closes the wait.
The newest open reason for a target replaces its earlier reason.

Done work outside the merge line uses a Merge row with its ID in Work.
State says `Done; <by> keeps it from the merge until <reason>; since <time>`.
A merge line entry, running merge, result, or presence on local main suppresses the hold text.
Queue targets append `<By> keeps it waiting until <reason>; since <time>` to State.
Pending plan targets append the same text to the Plan next row's State.
An unmatched target appears under `Now`, or under `Also` when another task runs.
Work holds its target; State says `<By> keeps it waiting until <reason>; since <time>`.
With no open phase, the first next item's unit wait supplies `Now`:
`Nothing runs; <By> keeps <target> waiting until <reason>`.
An open unit wait prevents an otherwise empty unit from counting as idle.
Stored actor text remains unchanged; sentence starts use a capital letter.

`Next` preserves the order of unstarted queue items.
An exact leading open or sent phase ID followed by `:` identifies started work.
`Next` leaves out that item, because `Now` or `Merge` already shows the work.
The first unstarted queue item determines the queue wait.
A satisfied or absent dependency uses State `Ready`.
An unmet dependency uses `After <dependency>; <short state>`.
Another unit's phase uses `handler phase 23`; this unit's own `devtools/acctcolor` uses `acctcolor`.
A merge rank uses `25th in the merge line`.
A sent dependency without a merge position uses `not in the merge line; reason not recorded`.
States distinguish absent local work, started work, submitted work, unstarted phases, and dependencies not found locally.
A sent dependency outside the merge line can use its unit wait:
`After <dependency>; done; <by> keeps it from the merge until <reason>`.
The dependency reader returns that wait from the run it already reads.
The board checks local `origin/main` without a fetch.
Pending plan work follows the queue in a Plan next row.
Work holds `<title> (<id>)`; State holds remaining IDs, plan waits, and unit waits.
The first pending phase preserves its `Waits for:` text and names unmet `Depends on:` phases.
The configured plan takes priority when it has phases; otherwise, the selected run supplies the plan.
Current work with nothing next says `Nothing queued`.
Other empty states say `Nothing queued; the plan is complete` or `Nothing queued; no plan is assigned`.
An unreadable queue says `Queue unavailable; next work cannot be determined`.

One census supplies worker and director evidence.
Staff, run, queue, dependency, merge, and plan reads share caches within the command.
The staff current run takes priority over newer unrelated runs.
A reopened run remains open until its next finish event.

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
