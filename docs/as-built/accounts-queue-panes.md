# Accounts and build queue panes

## What they show

The `unit-board@nate-local` plugin provides two panes.
`/showrunner:accounts` shows saved account readings and staff activity counts.
`/build:queue` shows the memory cap, running steps, waiting steps, and admission checks.
The plugin keeps its name because both commands use it.
`/showrunner:units` prints the Units table through `scripts/production/unit_phases.py`.

## The mod

The files in `mods/unit-board/hooks/` use the Claude Code engine through `$`.
`register.tsx` owns engine calls, pane state, timers, and command hooks.
`data.ts` owns account loading, board checks, and source checks.
`accounts.tsx` draws accounts; `queue.tsx` draws queue span lines.
Shared text helpers remain available to both panes.
The accounts and queue wire types describe their Python readers.

Install the local marketplace with `claude plugin marketplace add ~/.claude/mods`.
Install the plugin with `claude plugin install unit-board@nate-local`.
Claude Code 2.1.316 and later reads this local plugin in place.
Promoted changes apply at the next session start or `/reload-plugins`.

## Commands

`/showrunner:accounts [close]` opens the `Accounts` pane without a model turn.
It runs `accounts.py status --json --stored`.
It replies `Accounts opened.` or `Accounts closed.`.

`/build:queue [<position|session|folder>]` opens the `Build queue` pane without a model turn.
It runs `build_queue.py --json --columns <width> --you <session>` with the optional selector.
The initial width is 100 columns.
A width change requests a refresh.
A selector displays the queue and its matching cards on one page.
`mine` uses the same layout.
Selection survives refresh and reload.
The command replies `Build queue opened.` or `Build queue closed.`.
Bump remains disabled.

`queue_lines.py` defines terminal spans, widths, wrapping, clipping, and the `QueueLines` wire record.
The JSON omits default tone, background, and bold values.
A countdown span carries its deadline in `until`.
The pane updates its countdown without another script run.

`build_queue.py` uses one captured admission pass through `queue_read()`.
`unit_labels_for` supplies labels through one file read.
The memory bar shows the cap, anonymous use, keep-aside, room, safety, and other programs.
Running rows show each run and its held slot.
Waiting rows show the showrunner, unit, step, wait, pid, and memory need.
A missing pid shows `pid —`.
The first eligible row has an `↑` marker and its hold cause.
Head protection preserves room before a younger run starts.
Held slots retain priority for the same run's next step.
New starts use the recorded spacing deadline.
Service rows retain RAM and swap amounts on continuation lines.
`--you` selects matching rows through the session id and adds a blue `▸`.

Selector cards use the queue model and admission formatter.
They show the room check, start basis, head cause, and run keep-aside from that pass.
`as_of` records the admission reading's epoch.

`session.start` restores timers for open panes.
Without the mod, `/showrunner:accounts` names the required plugin, and `/build:queue` runs its script.
Use `/reload-plugins` to load changed hooks in an existing session.

## State and refresh

Plugin state keeps independent `accountsPanes` and `queuePanes` records.
A missing record means the pane is closed.
Accounts records have `loading`, `accounts`, or `failed` states.
Queue records have `loading`, `queue`, or `failed` states and retain their selection.
Each pane has its own busy guard, timer, and lifecycle counter.
Close cancels its timer and invalidates pending reads.
A forced refresh during a read requests one following read.

Accounts refreshes every ten seconds while open.
`refreshIfChanged` checks saved usage, groups, worker selection, and role-map cache paths.
It also checks reported session inputs and the peer-count cache.
A missing path uses its nearest existing parent for discovery.
A source change or failed stat requests a new reading.
Reported session inputs also request current local counts on each tick.
The board's `stale_at` is the next local midnight, when the reset text changes.
The first tick at or after `stale_at` requests a new reading.
Accounts ignores `turn.complete`.

The build queue reads live process memory every ten seconds while open.
A failed refresh retains its last successful page.
A failed first query shows `Build queue could not be read.`.
Countdown spans redraw once per second until expiry, without another script run.

## Account drawing

`accounts.status_board()` builds the board, and the console Accounts page draws the same board.
The board holds its columns, one row of cells for each table line, and the same facts for a narrow pane.
Its columns start with the access mark, account, left, and role.
The activity columns follow: busy, idle, paused, exited, and total.
The final columns show resets, credits, and enabled.
Account labels are bold, and numbers align right.
Each role has its own row and counts.
Only the first row repeats the account values.
Python combines machine counts in `role_counts` and retains separate records in `role_counts_by_machine`.
The decoder counts an older peer's starting sessions as busy before it combines machine counts.
A role without reported members has zero counts; `spare` has no count.
The totals row sums known activity counts.

Without live detached work, units and showrunners without live sessions count as exited on their selected role account.
Claude producers count under `showrunners` through the same hold and activity rules.
Without a live session, they count on the selected showrunners account.
Workers count only while live.
Busy panes count as busy. Starting panes count as starting, including with live detached work.
Other live detached work counts as waiting.
Staff exit and pause records take priority.
The access mark uses `read_ok`.
Low allowance uses error below 10 percent and warning below 25 percent.

`AccountsView(elements, columns, status)` draws the board as it is, and each span keeps its tone and weight.
Tables use the widest header or cell, with two spaces between columns.
A narrow pane splits the table before `resets`, or shows the facts of each row.
Wrapping preserves complete facts and stays inside the pane.
Problems appear as wrapped warning lines.
`validBoard` refuses a board with an unknown tone or kind, a column without an alignment, or a `stale_at` that is not a time.
The pane then shows `Stored accounts status could not be read.`.
A pane state saved before the board existed draws as loading until the next reading.
The [account design](accounts.md#status-counts-and-notes) defines count attribution and stored reads.

## Invariants and checks

1. Panes open only through their commands.
2. A closed pane performs no timer, filesystem, or process work.
3. The mod reads no credential file and requests no login.
4. Accounts uses `accounts.py status --json --stored` without account probes.
5. User-facing times use the user zone, which `accounts.py` applies.
6. The readers remain one-shot commands; the mod adds no daemon or external job.

Run `command claude plugin test mods/unit-board` for the mod tests.
Run `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for types.
The engine-generated `.claude-plugin/types/` declarations are required for the type check.
A fresh checkout without those files reports `TS2688`.
The queue tests also cover `test_build_queue`, `test_head_facts`, `test_queue_view`, and `test_queue_card`.
