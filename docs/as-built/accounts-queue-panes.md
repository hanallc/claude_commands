# Build queue pane

## What it shows

The `unit-board@nate-local` plugin provides the Build queue pane.
It shows the memory cap, running steps, waiting steps, and admission checks.
Use Hana console, Accounts to see the accounts board.

## The plugin

The files in `mods/unit-board/hooks/` use the Claude Code engine through `$`.
`register.tsx` owns engine calls, pane state, timers, and command hooks.
`queue.tsx` draws the queue lines that Python supplies.
`lines.tsx` defines the engine element type that the queue uses.
`types/queue.d.ts` describes the queue reader's output and the pane state.

1. Install the local marketplace with `claude plugin marketplace add ~/.claude/mods`.
2. Install the plugin with `claude plugin install unit-board@nate-local`.
3. Use `/reload-plugins` to load changed hooks in an existing session.

Claude Code 2.1.316 and later reads this local plugin in place.
Promoted changes also apply at the next session start.

## Commands

`/build:queue [<position|session|folder>]` opens the `Build queue` pane without a model turn.
It runs `build_queue.py --json --columns <width> --you <session>` with the optional selector.
The initial width is 100 columns.
A width change requests a refresh.
A selector displays the queue and its matching cards on one page.
`mine` uses the same layout.
Selection survives refresh and reload.
The command replies `Build queue opened.` or `Build queue closed.`.
`/build:queue bump <position|session|folder>` changes the queue priority.
The pane keeps its selection after a bump.

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

`session.start` restores the timer for an open Build queue pane.
Without the plugin, `/build:queue` runs its script.

## State and refresh

Plugin state keeps the `queuePanes` record.
A missing record means the pane is closed.
A saved `accountsPanes` record remains unused.
Startup restores only the Build queue pane.
Queue records have `loading`, `queue`, or `failed` states and retain their selection.
The pane has a busy guard, a timer, and a lifecycle counter.
Close cancels the timer and invalidates pending reads.
A forced refresh during a read requests one following read.

The build queue reads live process memory every ten seconds while open.
A failed refresh retains its last successful page.
A failed first query shows `Build queue could not be read.`.
Countdown spans redraw once per second until expiry, without another script run.

## Invariants and checks

1. The pane opens only through its command.
2. A closed pane performs no timer, filesystem, or process work.
3. The plugin reads no credential file and requests no login.
4. The queue reader remains a single command; the plugin adds no daemon or external job.

Run `command claude plugin test mods/unit-board` for the plugin tests.
Run `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for types.
The type check needs the engine declarations in `.claude-plugin/types/`.
A fresh checkout without those files reports `TS2688`.
The queue tests also cover `test_build_queue`, `test_head_facts`, `test_queue_view`, and `test_queue_card`.
