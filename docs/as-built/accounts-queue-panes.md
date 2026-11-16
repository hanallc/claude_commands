# The accounts pane and the Builds page

## Views

The `unit-board@nate-local` plugin provides the accounts pane.
`/showrunner:accounts` shows saved account readings and staff activity counts.
Use `/build:page` for running steps, waiting steps, memory use, and Mac idle alerts.
Use `/build:bump <position|session|folder>` to set a waiting unit's priority.
Use `/build:bump clear [unit]` to remove priority.
`/showrunner:units` prints the Units table through `scripts/production/unit_phases.py`.

## The plugin

The files in `mods/unit-board/hooks/` use the Claude Code engine through `$`.
`register.tsx` owns engine calls, pane state, timers, and command hooks.
`data.ts` owns account loading, board checks, and source checks.
`accounts.tsx` draws the Accounts board.

Install the local marketplace with `claude plugin marketplace add ~/.claude/mods`.
Install the plugin with `claude plugin install unit-board@nate-local`.
Claude Code 2.1.316 and later reads this local plugin in place.
Promoted changes apply at the next session start or `/reload-plugins`.
Use `/reload-plugins` to load changed hooks in an existing session.

## Commands and refresh

`/showrunner:accounts [close]` opens or closes the accounts pane without a model turn.
It runs `accounts.py status --json --stored`.
The command replies `Accounts opened.` or `Accounts closed.`.
`session.start` restores the timer for an open accounts pane.

Plugin state keeps the `accountsPanes` record.
A missing record means the pane is closed.
Records have `loading`, `accounts`, or `failed` states.
Close cancels the timer and invalidates pending reads.
A forced refresh during a read requests one subsequent read.

Accounts refreshes every ten seconds while open.
`refreshIfChanged` checks saved usage, groups, worker selection, and role-map cache paths.
It also checks reported session inputs and the peer-count cache.
A missing path uses its nearest existing parent for discovery.
A source change or failed stat requests a new reading.
Reported session inputs also request current local counts on each tick.
The board's `stale_at` is the next local midnight, when the reset text changes.
The first tick at or after `stale_at` requests a new reading.
Accounts ignores `turn.complete`.

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
`validBoard` refuses unknown tones and kinds.
It also refuses columns without alignment and invalid `stale_at` times.
The pane then shows `Stored accounts status could not be read.`.
A pane state saved before the board existed draws as loading until the next reading.
The [account design](accounts.md#status-counts-and-notes) defines count attribution and stored reads.

## Invariants and checks

1. Open the accounts pane through `/showrunner:accounts`.
2. Keep a closed pane free of timer, filesystem, and process work.
3. Read accounts through `accounts.py status --json --stored` without account probes.
4. Show times in the user zone, as `accounts.py` specifies.
5. Read no credential file and request no login.
6. Keep the reader as a single command without a daemon or an external job.

Run `command claude plugin test mods/unit-board` for the plugin tests.
Run `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for types.
The engine-generated `.claude-plugin/types/` declarations are required for the type check.
A fresh checkout without those files reports `TS2688`.
