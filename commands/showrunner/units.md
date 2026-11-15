---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session:
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`.
The caller's staff row selects the production.
Print the full output as Markdown, without code fences or added text.
Include the summary, unit blocks, style table, totals, and anomaly lines when present.

The phase board prints one block per staff unit, sorted by name without case distinctions.
It includes paused units, stopped units, and units without a session.
The first line counts each unit once:
`working N · waiting to merge N · waiting on other work N · idle N · <total> units`.
It inserts `unknown N` before the total only when that count exceeds zero.
Counts include only units in these blocks.

Each title is `**<unit>** — <bucket>`.
When any unit uses another machine, each title includes its machine in parentheses.
The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.
Each field is a list item with a bold label.
`Now` and `Next` always appear.
`Also`, `Wait`, and `Merge` appear only with facts.
Multiple items use an indented numbered list.
Queue items always use a numbered list.
Native Markdown wrapping preserves full titles, reasons, and queue text inside their blocks.
The phase board reads no pane width and uses no continuation rows.

The summary uses the first matching rule:

1. `working`: a matched worker, command, director activity, or busy director with an unsent open phase executes.
2. `idle`: the unit has a pause or stop, or readable records show no remaining work and no busy director.
3. `waiting on other work`: an open phase or director command has a recorded wait.
4. `waiting to merge`: sent work waits, merges, or failed; a unit wait can also keep done work from the merge.
5. `waiting on other work`: the first next item has a recorded wait, or a unit wait has no matching target.
6. `unknown`: the available facts explain no earlier state.

`Now` names each open phase as `<title> (<phase id>)`.
It removes a repeated phase id from the title's start.
Inside a sentence, a title's leading article or number word is lowercase; other first words keep their capital.
Tasks follow plan order, then start order for work outside the plan.
Executing tasks appear first.
When a task executes, other open tasks appear under `Also`.
A recorded pass role supplies the worker's action, such as `The implementation worker writes <work>.`
The pass's worker id joins the census first; its phase assignment supplies the fallback.
Finished passes supply no current worker.
A running detached command says `The <role> runs <label> for <work> and waits for its result.`
An unread result says `the worker finished; the director has not read its result`.
A director activity says `No worker is needed for this step.`
A busy director without a recorded step says `the director's step is not recorded`.
An unexplained open task says `is open, but nothing runs; reason not recorded`.

A memory wait says `<work> needs build memory in use by <holder>; since <time>.`
A slot wait says `<work> waits for a build slot used by <holder>; since <time>.`
Other named resource waits say `waits for <reason>, in use by <holder>`.
An unnamed holder says `holder not recorded` and preserves the recorded reason.
A holder change preserves the wait's original start.
A wait belongs to its task; it does not hide another worker's work.
Director commands retain their recorded resource waits.
A promise appears once per phase, on its first displayed task.
An overdue promise says `Estimated finish <time>, late <duration>.`
Times use the production zone, with `HH:MM` today and a date on other days.

Without a current task, `Now` states the recorded pause, stop, first next wait, or unexplained delay.
A missing pause actor uses `The unit is paused`; a missing stop actor uses `The unit is stopped`.
A missing pause or stop reason uses `reason not recorded`.
Empty work says `Nothing to do: the plan is complete and the queue is empty.`
Without a plan, it says `Nothing to do: no plan or queued work.`
A failed census says `Workers unavailable; who runs cannot be determined.`
No-worker mode adds `Workers cannot start because no-worker mode is on.`

`Merge` prints each merge group once, with active merges before merge line order.
A single phase says `<id> is done; waiting to merge, 1st in line.`
A shared group says `<a> and <b> are done; merge together, 2nd in line.`
An active merge says `merging now`.
A carried commit names its carrier unit and phase.
A failure names its cause and time.
Submitted work without a position says `absent from the merge line; reason not recorded`.
A line that has no record says `merge position not recorded`.
Sent open phases appear only under `Merge`.
The board has no daily merge count.

Directors record a unit wait with `progress_history.py start-wait --target WORK --by WHO --reason CONDITION --kind KIND`.
The target can name a phase or a queue item's exact leading ID.
The command requires `--by` and `--reason` with `--target`.
The record belongs to the run, with no phase or pass attribution.
`finish-wait --wait-id ID`, target phase start, or run finish closes the wait.
The newest open reason for a target replaces its earlier reason.

Done work outside the merge line uses `<id> is done; <by> keeps it from the merge until <reason>; since <time>.`
This text replaces the missing-reason sentence under `Merge`.
A merge line entry, running merge, result, or presence on local main suppresses the hold text.
Queue targets append `<By> keeps it waiting until <reason>; since <time>.`
Pending plan targets append the same text after `Plan next`.
An unmatched target appears under `Now`, or under `Also` when another task runs.
Its text is `<By> keeps <target> waiting until <reason>; since <time>.`
With no open phase, the first next item's unit wait supplies `Now`:
`No task runs. <By> keeps <target> waiting until <reason>.`
An open unit wait prevents an otherwise empty unit from counting as idle.
Stored actor text remains unchanged; sentence starts use a capital letter.

`Next` preserves every queue item in stored order.
An exact leading open or sent phase ID followed by `:` identifies started work.
`Next` leaves out that item, because `Now` or `Merge` already shows the work.
The first unstarted queue item determines the queue wait.
A satisfied dependency appends `Ready: <after> is on the local copy of main.`
An unmet dependency appends `After <after>: <state>.`
States distinguish absent local work, started work, submitted work, unstarted phases, and dependencies not found locally.
A sent dependency outside the merge line can use its unit wait: `After <after>: done; <by> keeps it from the merge until <reason>.`
The dependency reader returns that wait from the run it already reads.
The board checks local `origin/main` without a fetch.
Pending plan work follows the queue: `Plan next: <title> (<id>); then <remaining ids>.`
The first pending phase preserves its `Waits for:` text and names unmet `Depends on:` phases.
The configured plan takes priority when it has phases; otherwise, the selected run supplies the plan.
Empty work distinguishes a complete plan, no assigned plan, and `Nothing queued after the current work.`
Open phases without plan rows say `Nothing queued after the current work.`
An unreadable queue says `Queue unavailable; next work cannot be determined.`

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
