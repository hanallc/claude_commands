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
`data.ts` owns account loading, source checks, and the user zone.
`accounts.tsx` draws accounts.

Install the local marketplace with `claude plugin marketplace add ~/.claude/mods`.
Install the plugin with `claude plugin install unit-board@nate-local`.
Claude Code 2.1.316 and later reads this local plugin in place.
Use `/reload-plugins` to load changed hooks in an existing session.

## Commands and refresh

`/showrunner:accounts [close]` opens or closes the accounts pane without a model turn.
It runs `accounts.py status --json --stored` and resolves the user zone.
The command replies `Accounts opened.` or `Accounts closed.`.
`session.start` restores the timer for an open accounts pane.

Plugin state keeps the `accountsPanes` record.
A missing record means the pane is closed.
Records have `loading`, `accounts`, or `failed` states.
Close cancels the timer and invalidates pending reads and zone requests.
A forced refresh during a read requests one subsequent read.

Accounts refreshes every ten seconds while open.
`refreshIfChanged` checks saved usage, groups, worker selection, and role-map cache paths.
It also checks reported session inputs and the peer-count cache.
A missing path uses its nearest existing parent for discovery.
A source change or failed stat requests a new reading.
Reported session inputs also request current local counts on each tick.
Accounts ignores `turn.complete`.

## Account drawing

`AccountsView(elements, columns, status, zone, now)` fits the pane width.
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
The pane adds the starting and waiting counts of an older stored status to busy.

Without live detached work, units and showrunners without live sessions count as exited on their selected role account.
Claude producers count under `showrunners` through the same hold and activity rules.
Without a live session, they count on the selected showrunners account.
Workers count only while live.
Busy panes count as busy. Starting panes count as starting, including with live detached work.
Other live detached work counts as waiting.
Staff exit and pause records take priority.
The access mark uses `read_ok`.
Low allowance uses error below 10 percent and warning below 25 percent.

Tables use the widest header or cell, with two spaces between columns.
Narrow panes split columns or show labeled facts.
Wrapping preserves complete facts and stays inside the pane.
Problems appear as wrapped warning lines.
`accountDisplay` converts the wire contract to `AccountReading<T>` and `CreditBalance`.
It converts `StoredRoleSessionCounts` to `RoleSessionCounts` with all activity counts present.
The [account design](accounts.md#status-counts-and-notes) defines count attribution and stored reads.

`resolveUserZone` follows `scripts/lib/user_zone.py`.
It checks `USER_ZONE`, then `USER_ZONE_CONF` or `~/.claude/config/user_zone.conf`, then `America/New_York`.
It validates the zone through `Intl.DateTimeFormat`.
A changed lifecycle cancels a pending lookup.

## Invariants and checks

1. Open the accounts pane through `/showrunner:accounts`.
2. Keep a closed pane free of timer, filesystem, and process work.
3. Read accounts through `accounts.py status --json --stored` without account probes.
4. Show times in the user zone.

Run `command claude plugin test mods/unit-board` for the plugin tests.
Run `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for types.
The engine-generated `.claude-plugin/types/` declarations are required for the type check.
A fresh checkout without those files reports `TS2688`.
