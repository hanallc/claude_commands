# Shutdown and restart

## Process-group signals

`scripts/lib/process_group.py` owns process-group signal validation.
`check_process_group(pid)` requires an integer greater than one and refuses the caller's own group.
It also refuses booleans and mock values.
`send_process_group(pid, signum)` validates before the sole raw `os.killpg` call.
Callers handle `ProcessLookupError` when the group has ended.
Shell callers use their checkout's helper through `scripts/lib/py`.
The helper returns exit 3 for a missing group and exit 2 for refusal or failure.
`test_process_group.unguarded_group_signals` checks Python references, embedded scripts, and shell commands for unguarded signals.
`run_process_group(command, timeout=..., env=...)` runs a command in its own group and returns its text output.
It waits up to the timeout for the command to exit, then stops the whole group.
After a timeout it stops the group and raises `subprocess.TimeoutExpired` with the output.
Output goes to temporary files, so a helper process that keeps the output open cannot delay the result.
Bytes that are not UTF-8 decode as replacement characters.
`stop_process_group(process)` sends TERM, then KILL, to the group of a `start_new_session` child and closes its pipes.
Every headless Firefox and Chrome run starts with `run_process_group`, or with `Popen(start_new_session=True)` and ends with `stop_process_group`.
On macOS the gate has no subreaper, and its group kill does not reach a browser group when it stops a module during a browser run.
The Node proof `memory_page_proof.mjs` starts Chrome, so its tests start Node with `run_process_group`.
A browser leaves helper processes after its own exit or a timeout, and a plain `subprocess.run` stops only the leader.

## Session commands

The `/agents` skill uses the `agents` program.
`agents` and `agents help` print the same usage and return status 0.
The public commands include `help`, `status`, `pause`, `exit`, `resume`, `clone`, `restart`, and `stop`.
`agents status [ACCOUNT] [--json] [--here]` reports live sessions.
`agents stop SESSION` stops the selected session and its workers.
SESSION is a staff id, a session id, a unique name, or a staff path.
A staff path is the full path, such as `hana/assembly/fix2`, or a path under a showrunner, such as `assembly/fix2`.
A unit path stops the unit and its workers.
A path that matches no staff row goes to the native session stop, which can match a saved session name such as `review/x`.
A path that matches more than one staff row stops nothing and returns status 2 with the matching paths.
Add `--dry-run` to print their names without stopping them.
`agents` runs `scripts/agents/shutdown.py`.
All lifecycle scripts use `scripts/agents/`.

`agents clone NAME --as NEW` forks a live conversation on the source machine.
Use a staff name, saved launch name, staff ID, or native session ID.
The command refuses a saved launch name that matches multiple sessions on this machine.
The command requires one proved pane and opens the clone after its source tab on the same Herdr server.
The live pane must show the current model and effort.
For Codex, show Model with Reasoning in the status line.
Claude uses `--resume --fork-session`.
Codex uses `codex fork`.
The first prompt names both sessions and asks the clone to wait for a request addressed to it.
The command refuses occupied names and reserved message addresses.
The fork uses the source account environment.
An absent `CLAUDE_CONFIG_DIR` stays absent.
Resume and restart keep the saved implicit Claude home unless an explicit account is selected.
The command verifies the fork before it saves the launch or adds a root `session` row.
It saves the launch before the staff write.
A definite refusal closes only the fork terminal.
The staff owner checks the name and ID together before it creates the row.
The row stores the launch folder and account for ordinary restart and resume.
Messages to each name resolve to that session's native ID.

`agents pause NAME [--keep-workers] --why TEXT` pauses a director.
The director finishes its current turn and holds at the prompt.
By default, pause stops the workers.
With `--keep-workers`, the workers continue their current work.
The command refuses `--keep-workers` for Codex directors and offers the default pause.
Codex support needs a runtime check that proves the hook through the worker result route.
It reports each refusal and pauses the other selected directors.
`agents exit NAME --why TEXT` stops the director and its workers.
Both commands preserve the staff rows, tabs, and shells.
Add `--finish-step` to wait for the workers' current tasks before stopping them.
The staff row records the time, caller, and reason under `paused` or `exited`.
The board shows that state.
The pause record includes `keep_workers` when pause keeps the workers.
`agents status` names the kept workers with open agent panes.
JSON status lists the names under `kept_workers`.
`agents pause --production SHOWRUNNER [--except UNIT ...] [--keep-workers] --why TEXT` selects units within one production.
`agents resume --production SHOWRUNNER` resumes its units.
It reports each held row and its holder when known.
It resumes the available rows and returns exit 2 if a row lock blocks a resume.
Resume sends a running director one continuation per pause.
Resume saves the pending message and clears the pause in one staff write before calling the sender.
A retry recovers the pending continuation even when the director is active.
A durable send, queue entry, or review entry completes the pending resume.
A remote send saves its destination before delivery.
A retry checks the remote receipt before it looks for a live director.
A lost reply keeps the resume pending until the sender can confirm acceptance.
A retry can reclaim a relay if its owner died before the socket write.
An unconfirmed socket write keeps the resume pending.
Review release publishes the continuation once and can retry the notice.
Resume skips worker restoration for a director paused with `--keep-workers`.
A worker message to a paused director waits in the director's transcript until resume.
A paused director on the other machine gets the continuation message without a local liveness read.
The machine comes from its worktree or its showrunner document.
A stopped paused director starts through the shared resume path, including its requested `--to` account.
With `--keep-workers`, resume requires a live staff recipient for its continuation.
If its socket is unavailable, the send fails and leaves no queued continuation.
Definite delivery failure restores the same pause record.
Uncertain delivery keeps the pending resume for a retry.
A failed start or a start exception also restores the pause record.

#### Continuation crash limits

The rule is visible pending over double send.
Claude Code has no atomic keyed input write, so a sender cannot prove whether a killed attempt was accepted.
Three crash windows remain.

1. A `send.py pending` drain killed after it flushes a queued continuation and before it rewrites the queue sends it again on the next drain. Every queued message has this window.
2. The resume stays pending if the sender dies after it saves `relay-unconfirmed` and before `record_once` saves acceptance.
3. A sender stopped after it saves the Mac destination and before SSH, followed by a director move, leaves the resume pending. A retry does not send.

A pending resume exits non-zero and keeps its record.
Resume starts an exited director through the shared role-account path and removes its exit record after success.
The Claude prompt hook reads the director hold for every prompt source.
It adds the hold instruction and preserves the message in the transcript.
An own-pane resume resolves the agent executable before it removes the exit record.
It executes that path directly. A missing executable retains the exit record.
The stop hook skips the automatic continuation for that director.
Stop does not save or restore composer drafts.

Stop, resume and account move use `scripts/agents/stop_resume.py`.

`stop(session)` ends only the selected local session's process.
It reads the session identity, pid, and start time before TERM.
It checks the current conversation before each signal.
It checks exit every 0.1 seconds and sends KILL after two seconds when the conversation proof remains valid.
Registry loss after TERM is quiet.
Stop waits up to five more seconds with the saved pid and birth proof.
Other read errors print an anomaly.
It waits up to five seconds after KILL.
Registry removal alone does not prove exit.
A surviving process returns `StopFailed` and does not resume.
After exit, `stall_watch.retire_session_prompt(session_id)` publishes only `{session_id, at}`.
It takes no watcher lock and sends no message.
Stall-watch retires the saved prompt keys on its next pass, even after launch deletion.
A cancellation error prints a separate warning and does not change the stop result.
Workers keep their threads.
Staff membership selects worker children for stop and restart.
A retained worker pane at its shell or a closed pane still needs proof that the conversation stopped.
An incomplete conversation read returns `StopUnproved`.
These operations do not read a transport pin.

`release_source(row, backend)` uses the current native conversation evidence.
A confirmed exit or `NotRunning` gives `SourceReleased`.
An unresolved staff reference gives `SourceUnresolved`.
Unknown process evidence or an unproved stop gives `SourceReleaseUnknown`.
A family switch preserves its source when release remains unproved.

`agents resume NAME [--why TEXT] [--to ACCOUNT]` works from any directory.
The short form is `resume NAME`, through `scripts/agents/resume`.
The wrapper uses the shared Python launcher and locates `shutdown.py` beside the wrapper.

The lookup accepts a staff name, a staff id, a launch reference, or a native session id.
A Codex thread name is display text and selects no conversation.
Unknown names return status 1.
They list up to five close names from local units and showrunners when available.
Ambiguous names return status 1 with up to five matching members.
The command forwards a remote staff row to its configured workstation over SSH.
The command returns status 1 when the row's machine is unknown or unreachable.
A running session returns status 0 with an `unchanged` line that names its current account.
Resume does not stop or move a running session.
Unknown Codex process evidence prints an anomaly and does not block resume.
A pane with the session id and a foreground program counts as already running.
Without process evidence, its account remains unknown.
Unknown process evidence without a pane that reports the id can allow a second process on the same conversation.
A unit pane at its shell permits resume when no remaining process blocks it.
Direct resume and watchdog recovery use the same wait while non-agent processes block the pane.
`holder_exit_seconds` in `config/shutdown.conf` allows 5 seconds by default.
A live agent in the holder list causes an immediate refusal.
The `no foreground agent` refusal lists each blocking command and its position relative to the shell tree.
The default reason is `resumed by name`.

The shared resume path reads the saved environment, arguments, and folder.
`member_account.resume_account` selects the account for both families at invocation.
The source account does not select the target account.
A named Codex resume uses the configured `workers` account, with or without a staff row.
Claude units and showrunners use their configured role account, even without a pane launch record.
Claude producers use the configured producers account, or the showrunners account while the producers group has no selection.
Other Claude sessions use the configured units account.
A configured producer without a staff row uses the producer first-start account rule.
When the account does not change, the resume message names no move.
`--to ACCOUNT` overrides the account for either family.
Restart without ACCOUNT and folder moves use the role account.
Explicit pane restore keeps the saved account for both families.
It passes the saved home to `restart.start_row` as an `AccountSelection`.
Explicit pane restore restores a closed tab when a saved launch exists.
Producer upkeep uses shared resume and selects the current family account.
An explicit CLI resume can restore an intentionally stopped session.
Unit watchdog recovery preserves an intentional stop.
Producer upkeep excludes paused and exited rows, but it can resume an otherwise active producer after a process stop.
A separate terminal starts the account command in the target pane.
The action check confirms the session before signals or pane input.
The command changes to the saved folder before the account launch.
The pane receives one opening message after identity confirmation.

Resume, restart and account moves start a session in its own pane.
The own pane is a pane that runs the member, else the newest pane whose launch record names the member and no other member runs in.
`herdr.saved_row_tab` waits within `own_pane_shell_seconds` (`config/shutdown.conf`) for that pane to reach the shell, then reuses it.
When the wait ends first, the start is refused with one line.
The line names the session, its own pane and `agents resume <name>`, and the session stays stopped.
A new tab opens only for a session with no own pane.
A producer whose own pane is outside `producer_workspace`, or shares its tab, gets a new tab in that workspace with no wait.
A restart never splits a pane beside a worker.
`stop_resume._resume_once` and `restart.start_row` use `herdr.row_workspace` for new tabs.
Producers use `producer_workspace` from `config/staff.conf`, normally `producers`.
Other rows use their parent showrunner's workspace, the entry name, or the shared session.
A role account change does not update an existing pane shell's `resume` alias.
Each agent launch prepares the pane aliases through `herdr.agent_alias_script(kind)` for its selected family.

`resume_message.py` builds every opening message.
The restore paths use these causes:

1. A named resume uses `resumed by name` unless `--why` supplies another reason.
2. An account move uses its supplied reason and the source and target accounts.
3. A refusal-watch move uses the refusal-watch reason through the shared account move.
4. A folder move uses `the session folder moved`.
5. A herdr server restore resumes the saved conversation without a generated prompt.
6. A relaunch with no known cause uses `you were stopped and resumed`.

The relaunch supervisor preserves explicit prompts.
The pane path marks prompt delivery as `pane` to prevent a second automatic prompt.
Before launch, the prompt cursor skips complete old rows without parsing JSON.
Owner scans retain runs with idle workers.
Owner scans print no missing-owner anomaly for ended runs or runs without an active dispatch.

`ClaudeUp` means the registry reports a live Claude within 20 seconds.
Resume makes one attempt.
A detached check watches its pid and start generation for ten seconds.
The watchdog owns later recovery.
A missing tab or unreadable liveness returns a named error.
All batch stops finish before any resume.
Pane input waits up to ten seconds for the target shell to finish its startup work.
Launch seeding reads process information only for selected panes.

The shared session registry reader skips a file that vanishes during the scan. It retries a transient unreadable file up to three times, 50 milliseconds apart. Other unreadable evidence remains visible.
Worker stop uses the backend in the staff snapshot for both native checks when that backend is available.
Full restart and full stop require complete Claude registry discovery before they act.

`agents move SESSION_OR_GROUP ACCOUNT [--dry-run] [--why TEXT]` delegates to the accounts move command.
It normalizes the account label and preserves the result and output.

`move_sessions` writes each session request before the stops, then records its confirmed or failed outcome in the accounts moves log.
Each attempt has its own move ID and cause.
Delegated callers and sessions proved live on the target write nothing.

`move_sessions` uses the same stop and resume functions. All stops and prompt cancellation requests finish before any resumes. A retained request compares its wall-clock cutoff with the prompt's immutable observation-start time. An observation that spans stop/resume can be retired; the next watcher pass observes the resumed prompt afresh. A prompt first observed after the stop survives the older request, and failed retirement is retried by stall-watch. Each member resumes in its own pane on the target account; failures are independent. A resumed session retries failed native identity reads within the start deadline before it submits the resume message. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts for the requested duration. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

A resume in the same pane skips the source close check.
A resume in a different pane checks that the source contains no other work before it closes the source.
A layout scan retains a pane without a notice when a new Claude process in its foreground group lacks arguments during `start_seconds`.
If the arguments stay unreadable after that period, the next scan reports unknown evidence once per scan run.

The notes timer delegates account moves to this detached batch before any stop.
A move stops each session at once, even during a build step, a queued run, a verify call, or a Bash tool shell.
The resumed session must do its interrupted work again.
A later group account selection applies to the next stop attempt.
The batch writes its output to its move log.
The supervisor needs a restart request; an exited child alone does not cause a restart.
A saved launch keeps its conversation marks but cannot replace an explicit destination account.
The batch includes the existing stay observation before it records a confirmed or failed move outcome.

Each move and unit attachment acquires the native session lock first.
If the session has a staff row, the operation also acquires that row lock.
The move holds both locks through its stop and resume.
Attachment refuses while a move holds either lock.
Retired unit recovery holds both locks in the same order before it starts `add_unit --resume`.
The child verifies both inherited lock descriptors in native-first order.
The batch verifies each reference and reads the current conversation while it holds the locks.
References to the same member share one move and receive the same result.
Only a user command sets the user-command flag:

- `accounts.py move <session>`
- `accounts.py move <group> <claudeN>`
- `accounts.py move workers <label>` without `--follow-role`
- `accounts.py swap`
- `stop_resume.py retry`, the retry command of a pending move
- the batch command that a user command gives at its limit; it carries `--user-command`

A detached `move workers --follow-role` run gets the cause and the flag of the swap that started it.
The move cause is only a label for the move journal; it does not set the flag.
A user command takes a row that another operation holds:

1. The command moves the free rows at once, and keeps no lock on a held row.
2. A row is held when another process holds its row lock, or when it is in the restart record while a restart is active.
3. The command joins the row's queue at its first try. The queue records its PID and process start.
4. The first hand move in the queue can claim a held row. A later hand move waits for it.
5. An automatic move checks its claim before the stop. If a hand move replaced it, the automatic move skips the row.
6. The command tries the held rows after each poll and moves a row when it becomes free.

A user claim is live while the process that wrote it lives, or while that process cannot be read.
A command that cannot read its own process start after two reads records no start.
Such a claim is live while its pid exists.
An automatic move never takes a row while a live user claim is current.
It drops that row as `not moved: a newer move ran` at once and does not wait.
The claim of a killed command does not stop an automatic move.
Each check and write of a request file holds the `latest-requests.lock` file beside the request folder.
Plain reads, such as the ownership check of the detached child and the list of pending moves, do not take the lock.
The `latest-requests.lock` file also protects changes to the hand move queue.
An automatic move cannot replace a live hand move claim or pass a live hand move in the queue.

The restart record check keeps the move out of the gap between the stop and the start of a restart.
The restart record is the shared list of rows to restart, and it can keep rows that an earlier stop saved.
While any restart is active, a user command waits for each row in the record, and an automatic move reports it as busy.
A hand move starts its `move.stuck_seconds` timer when it first records a held row. The default is 60 seconds.
A detached hand move requires the parent's monotonic deadline from `--move-deadline`.
If the parent finds no held row, it sets the deadline at handoff.
A detached automatic move takes no deadline.
The child takes every claim before it confirms readiness. The parent cleans up untaken claims if the handoff fails.
On the Mac, the child waits for the parent to acknowledge readiness before it starts a move.
If a hand move includes a held row and the caller, the parent transfers the batch before it reads source accounts.
The child starts the watch when it records a held row and keeps the parent's deadline.
A separate deadline check can prove and signal holders while the move prepares or starts a free row.
The watch drops a row as soon as the move takes it.
Before each signal, the watch checks that the move still waits for the row.
The row lock record and the restart holder record include the PID, command, and process start.
The move can signal a restart only if it has a valid holder record and still holds the selected row.
Between stop and start, the restart holds a separate lock for each row it stopped. It releases that lock when it takes or refuses the row for start.
A holder record error reports one anomaly. The restart continues.
On Linux, the move checks the lock file's device and inode, write access, and the `flock` entry in `/proc`.
Linux sends each signal through a pidfd.
On the Mac, the move checks the descriptor with `lsof`. It needs a matching live record when `lsof` gives no lock state.
On the Mac, a short gap remains between the process start check and the signal.
The move excludes its own process and descendants. Readers and processes that only wait for the lock do not qualify.
The move sends `SIGTERM` to all proved holders before one wait of `kill_grace_seconds`.
After the wait, the move sends `SIGKILL` only to a process that still holds a proved lock.
If it sends no `SIGKILL`, the move checks the same process for exit again.
Each confirmed holder exit lets the move retry that row for up to `upkeep_row_wait_seconds`.
The move retries recovered rows one at a time. The holder check can continue while a recovered row starts.
Once the move confirms the target account, it removes the recovered restart entry under the row lock it holds.
This includes a row that was already on the target account.
If removal fails, the move reports the row and the restart record path.
If the move cannot end a proved holder or take the row, it reports the holder when known and gives a retry command.
A retry keeps the pending request and gives the retry command.
When the command ends, it ends each of its claims, also after an interrupt in the first pass:

- It removes the claim of each row that it never took.
- A successful move removes its request.
- It keeps any other request as a plain request for a retry, for example after a stop failure or a refused destination.
  A plain request does not stop an automatic move.
- After a failed handoff, it keeps each request as a plain request for a retry.

When the caller is in the batch, the detached child takes the held rows with the same rules.
The parent waits up to 15 seconds for the child to confirm its start.
Before the child confirms, it writes its own pid and process start into each claim of its parent.
So each claim stays live through the handoff.
An automatic move never waits for a busy row and never writes a request for it, also when its cause is "by hand".
It reports a move to the same account as in progress.
Other busy rows remain pending while the batch moves available members, and the pending result gives a retry command.
The batch omits busy members from the self-move handoff.
Each attempt reads the member's live account while it holds the row lock.
If the read fails, the batch leaves the request file and writes no journal request.
For a live source, the stop must prove the same PID and start time before it records the request.
If the request append fails, the batch refuses that member before sending a stop signal.
The batch reads the current role map for a group move.
A single-session move uses the explicit account.
The batch keeps a member running when its live account matches the target.
The batch validates the Claude destination again after the warning and before launch preparation.
A Claude destination needs a real account directory and a matching local sign-in.
The sign-in needs an unexpired refresh token or an unexpired access token if only that token is present.
A refused destination leaves the members running and keeps their request files.
A user command keeps them as plain requests.
The handoff carries the request ID, target, and native session references.
Before a stop or handoff, the parent writes the new request ID and target under the session locks.
It writes one file per native session in `latest-requests` beside the move journal.
The key stays the same when a session gains or loses a staff row.
The mover checks the request ID under the locks before it stops the member.
A detached child checks that it owns the request before it reports a member outcome.
A superseded move reports `not moved: a newer move ran`.
Success removes the move's own file under the locks.
Failure leaves the file for retry.
Each explicit group request checks all members, including members that appear to be on the target.
If the live account matches the target, the request clears the earlier file under the native lock.
A busy lock makes an automatic move report the row as pending.
A user command waits for the row, and at its limit it prints a fresh `batch --target` command for an explicit target.
For a group request, it prints `batch --group` so the batch reads the current group target under the session lock.
The batch creates the default `move.log` directory and its parents before it opens the log.
`retry` can run only a saved request.
`result` and `retry` check live accounts under the session locks.
When another operation holds the row, `result` reports the busy row, and `retry` waits for the row like any user command.
They remove requests for members already on their target, including after `shutdown resume --to`.
They remove a missing session's request only when a fresh read proves absence.
Unknown liveness keeps the request and reports the cause.
Both commands name each file with a decode error and continue with valid files.
Legacy reports supply display data only.
Both commands show a fresh batch command for an off-target legacy member.
The command uses `--group` when the legacy report names a group.
`retry` starts a new move for each saved request that still needs a move.
The journal writer rolls back an incomplete append before it releases the journal lock.
The refusal watcher reads the row lock, and the unit watchdog holds it through recovery.

The batch keeps the showrunner notices and sends one phone push for the members that still need help.
The push shows qualified names and gives a short command to retry pending moves.
If the names and the phone footer exceed the push limit, the message gives a command to read the full list.
The group and target identify the phone push for repeat suppression.
`move_alert_repeat_minutes` sets the repeat window to 1,440 minutes by default.
A standalone resume failure uses a keyed `shutdown.restart_failed` push.
For a unit under watchdog recovery, the watchdog keeps the pending push with the outage.
A failed detached start sends the push with its original key.
A confirmed start clears the pending push.
Each completed member records its outcome without waiting for other resumes.
An early exit or failed resume uses the existing showrunner failure report.

Retirement, checkout moves, running-session attach and nix-shell healing use this API.
Retirement reads process facts with the reader for the host that owns the worktree.
Retirement keeps the recorded pane address and its terminal identity before cleanup.
The remote retirement journal keeps both identities for completion retries.
Cleanup excludes only that pane's proved idle shell from the remaining processes.
After successful retirement, it closes that pane as its last step on the pane's machine.
It sends no signal to that shell.
A pane already gone needs no second close.
Foreground work inside the worktree keeps the process refusal.
Foreground work outside the worktree keeps the pane open and produces one anomaly line.
A failed retirement closes no pane.
Herdr refuses a changed terminal and keeps the new pane and its launch record.
Retirement reports that mismatch without changing its result.
The confirmed close retires the pane launch record.
Unit rename closes the unit's own pane after its ownership checks.
A pane already gone counts as closed.
Checkout moves stop before moving files and resume afterward.
The checkout coordinator excludes only its own pid from cwd holders.
Other holders still refuse.
Attach keeps its process and prompt-submission proofs.

Watchdog recovery uses the retained pane launch and staff row.
An ended pane launch keeps an intentionally stopped session down.
A unit with no pane launch record stays down.
The watchdog does not read shutdown session records.
Unit recovery writes its completion receipt and discards the parked facts before it restores workers.
It returns the worker restore exit code.
A completed recovery rerun restores the workers again.

## Producer launch and admission

`resume morty`, `resume brain`, and `resume dwight` use `stop_resume.resume`.
Account moves and restart share the same resume arguments, account selection, and pane launch code.
Each producer uses its own tab in the `producers` workspace of the shared herdr session.
`config/staff.conf` supplies the workspace through `producer_workspace`.
`restart.producer_launch(row)` resumes `row.current_session` with the producer's configured settings.
`restart.producer_resume(row, entry)` preserves saved launch options through `pane_record.configured_resume_arguments`.
It takes the family, model, and effort from the current producer settings.
`pane_launch` reads `--effort` and `--autocompact` as options with a value.

Claude launches use the auto-compact window for producer, showrunner, unit, or worker from `[compact_windows]` in `config/models.conf`.
The role window applies on start, restart, account move, relaunch, transfer, and resume.
The unit session's recorded window takes precedence over its role setting.
An absent key or the value `auto` leaves the window to Claude.
The context hook reads the nearest Claude ancestor's window first.
It then checks `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `autoCompactWindow` in `settings.json`, in that order.

When the configured family matches the current session, a named resume uses that session.
When the family changes, `stop_resume._resume_once` uses the first-start path.
It stops the old conversation and closes its pane before it starts a fresh conversation in a new tab.
If the old process exits before the `ended` write, a retry can use the saved binding to identify its family.
The retry checks that the old process has stopped before it writes `ended`.
First admission binds the new conversation to the existing producer row.
A retry uses the saved admission record to complete the launch or bind the live conversation.
If an interruption occurs after tab creation and before the admission write, a retry can leave an extra shell tab open.

`models_registry.launch_settings("producers", name)` validates each `family:model:effort` value in `config/models.conf`.
The configured lines are:

```ini
[producers]
morty=claude:fable:high
brain=claude:fable:high
dwight=claude:fable:high
```

These model names are aliases that the shared model registry resolves.
`member_account.producer_account(kind, *, to="")` selects the account before admission.
An admitted producer's same-family resume uses `member_account.resume_account`.
Both functions select the workers account for Codex.
Claude uses the producers account, or the showrunners account while the producers group has no selection.
An explicit `--to` overrides that selection.

`accounts.eligible_producers(membership, family)` selects active, unpaused, local producers of the configured family.
`accounts.producer_settings(member)` returns `LaunchSettings` or `InvalidProducerSettings`.
Invalid settings exclude that producer.
`settings_report_scope` limits its warning to one per operation.
A workers swap includes eligible Codex producers.
A producers group move includes eligible Claude producers.
Units and showrunners retain their existing group selection.

A repeated move selects a stopped producer even when its recorded home already names the target.
Only proof that the producer runs on the target permits the move to skip it.
`LiveAccountUnproved`, a `ResumeFailed`, identifies a live process with unreadable account evidence.
A move returns `StopFailed` with the unreadable account reason.
It sends no stop or start and makes no home change.
Producers, units, and showrunners share this move check and `member_account.repair_home`.
The move writes `home` only when it changes.
The home records an account; it does not prove that a start finished.

`pane_launch.write` owns retained producer bindings.
`pane_launch.producer_start(directory, expected)` writes the start contract before each retained launch, including an own-pane exec.
`ProducerStartExpectation(member, session, kind, folder, home)` defines the expected launch.
Publication checks the native identity, process generation, family, folder, and account.
The shared `.publication.lock` protects every pane record writer.
Publication replaces earlier bindings through the recoverable `.producer-publication` record without closing their panes.
`pane_launch.read_entries` returns no entries or lock file when the record folder is absent.
Resume compares source and destination pane locations before it lists panes.
An own-pane resume therefore needs no pane list call.

### Catch-up reader

`scripts/agents/catch_up.py NATIVE_ID [--blocks N] [--chars N]` prints the last blocks of a saved conversation.
A session that takes a producer role reads the earlier conversation of that role with it.
`NATIVE_ID` is `session:<id>`, `codex:<id>` or an ID alone. An ID alone selects the newest transcript of either family.
A block is one user message, assistant message, tool call or tool result. A tool block is one shortened line.
A Claude compaction summary is one block. A Codex compaction summary is encrypted, so only its time shows.
The reader finds transcripts through `scripts/lib/transcripts.py` and reads from the end of the file in windows that double.
`config/catch_up.conf` sets the default block count, the characters kept from one message, the tool line length and the first read window. Times use the user's zone.

`scripts/agents/catch_up.py NATIVE_ID --user-prompts --near ISO_TIME [--minutes N]` reads typed prompts from the saved transcript.
`near_minutes` in `config/catch_up.conf` sets the default window on each side of `ISO_TIME` to 10 minutes.
If `ISO_TIME` has no offset, the reader uses the user's time zone.
The reader prints each prompt in full with its time in the user's zone, in `%m-%d %H:%M:%S` format.
It skips tool results, hook text, system reminders and messages from other sessions.

### Create a producer

`agents add-producer NAME` reads `producer_defaults.model` from `config/models.conf`.
The create command validates the name and records its profile and README text before it writes them.
The staff owner publishes the creation machine and creation ID under `[producer_creations]` with the profile.
The staff owner compares names without case distinctions and refuses a claim when a marker or staff row exists.
The staff owner checks the current marker before removal.
A pending creation requires the local creation record on that machine.
Resume on another machine names the machine that holds the record and gives the `add-producer` command.
It creates the README from `templates/producer/README.md` when the README is absent.
The first conversation receives a prompt to read the README and run `/producer:produce`.
The creation record keeps the time for prompt recovery after a failed start.
Resume accepts a matching Codex user turn even when the turn has no timestamp.
A malformed Codex rollout line blocks automatic retries of the opening prompt.
Check the reserved pane for the opening prompt.
If the prompt is absent, run `agents resume NAME --resend-opening-prompt` on the creation machine.
The command sends the prompt once when the pane is idle and its input is empty.
The create or resume command removes the shared creation entry after prompt confirmation, staff admission, and pane publication.
The command reports the staff id, account and tab.

### Rename or retire a producer

`agents rename OLD NEW` moves the registry key and retains the staff id.
If the old name no longer belongs to the saved staff id, the retry keeps the old key.
It updates the session label and tab title.
The staff document path still names the original folder.
Rename and retire refuse while the producer has a pending admission.

`agents retire NAME [--to PRODUCER]` requires a handoff file and no open questions.
Without `handoff.md`, it asks the producer for the file and returns.
The command stops the conversation and closes its tab before it reads the open List items.
It moves those items with log source `agents retire` and tells each item's showrunner.
The retirement receipt keeps the profile, session id, and notices.
The command records notice delivery after the send succeeds.
A retry refuses if the producer has a different session id.
The command removes the producer key after a final read confirms that no open List items remain assigned to the producer.
The staff row keeps the profile in `retired_producer`.
Active staff lookups omit retired producers.
The fixed-id lookup supplies their names for closed List items.
`agents resume NAME --retired` restores the profile, clears the mark and resumes the same conversation.

### First start

`ProducerFirstStart(name, folder)` represents a configured producer before staff admission.
It is separate from an admitted `StaffRow`.
`producer_folder_root` in `config/staff.conf` defaults to `~/rust/hanadocs`.
The first start requires `<root>/<name>/README.md`.
Dwight uses `~/rust/hanadocs/dwight/README.md`.

The first-start path uses this order:

1. Lock the producer's first admission through `staff.slot_operation`.
2. Adopt one proved live session with the producer name before a new reservation.
3. Reserve the producer's tab in the configured workspace.
4. Save `PendingProducerAdmission(pane, terminal, kind, home)` at `_producer_admission_path(name)`.
5. Start without a resume argument through `restart.producer_first_arguments(name)`.
6. Prove the native identity through `Herdr.producer_admission_launch`.
7. Admit the row through `staff.ensure("producer", ..., runtime_id=identity)`.
8. Publish the retained binding through `pane_launch.staff_admitted` under `producer_start`.
9. Remove the pending admission record after publication succeeds.

`producer_conflicts` uses `sessions.running_sessions`, the Codex index, and loaded daemon threads.
It checks live agents in producer workspace tabs and panes with the producer label.
An empty labeled shell does not count as a live session.
`adopt_named_producer` adopts exactly one proved native conversation without a launch.
It records that session as `current_session` and preserves its account and working folder.
It records the configured producer folder as the document location.
It publishes a retained binding when the live session has a proved pane.
Two live sessions cause refusal before any staff write or launch.
An unproved live session also causes refusal.
The refusal names each conflicting session ID and pane on one line.
An unreadable Claude registry or daemon registry refuses producer admission.
Recovery checks live sessions before it resumes a retained conversation.
Recovery adopts a different proved live session and removes the pending admission record.
Recovery resumes the retained conversation only when no other live session carries the producer name.

`agents stop NAME` resolves a producer name to its row's `current_session` before tab label selection.
Several tabs with the producer label therefore do not select a different conversation.
The shared native process reader uses Claude's session registry to prove a plain `claude` start.
The process arguments need no session ID.

`agents transfer NAME [--model MODEL --effort EFFORT]` gives a producer role to the conversation that runs it.
`producer_transfer.transfer` checks for a pending admission while it holds the `producer-first-NAME` slot.
If an admission is pending, transfer asks the user to run `agents resume NAME` first.
`producer_transfer.transfer` holds the `producer-first-NAME` slot and the row lock for these steps:

1. Refuse a caller that another staff row records. Refuse a producer on another machine.
2. Prove the caller through `stop_resume.producer_adoption_launch`: native id, family, account home, folder, and pane.
3. Refuse a Claude caller outside the selected producer account. Print the `agents move` command.
4. Validate the settings through `models_registry.producer_settings`. Without options, the configured family must match the caller.
5. Stop the old conversation through `restart.stop_row`, print its `catch_up.py` command, and close its panes.
6. Publish `[producers].NAME` through `models_registry.store_producer`.
7. Write the row through `staff.transfer_producer`, then publish the pane record as the producer binding.
8. Give the tab the producer name through `retitle_row`; the saved layout supplies the position of the old tab.

`restart.restart(stop_all=True, only=(row id,))` then restarts the caller outside the row lock.
The restart runs in a detached process and sends no opening prompt.
The row id, the document, and the `staff:<row id>` message queue do not change.
Each step is safe to run again; a caller that already holds the row skips step 5.
A failure between steps 6 and 7 leaves the new settings on the old session until the next run.

`agents remove-stray NAME SESSION` removes a local stray producer conversation.
The command uses this order:

1. Refuse a session that any staff row records as its `current_session`.
2. Prove the producer name and native process identity before the stop.
3. Save `PendingProducerCleanup` before the first change.
4. Stop the selected conversation through `stop_resume.stop`.
5. Delete the admission record if its saved content still matches the proved admission record.
6. Close its proved terminal through `Herdr.close_retained_pane`.
7. Delete its transcripts across Claude account projects folders through `Path.unlink`.
8. Delete the cleanup record after every step completes.

The pane and terminal match also applies when no retained launch entry exists.
The cleanup record path is `<pane record directory>/.producer-cleanup-<name>-<session>`.
The record holds the name, session ID, process IDs, process start times, agent kinds, and herdr session.
It also holds the pane IDs, tab IDs, terminal IDs, admission match, admission content, and transcript paths.
`ProducerCleanupStep` stores a progress hint.
Each command checks every saved target, regardless of that hint.
A process stop is complete when the saved PID is absent or has a different process start time.
An absent saved pane needs no closure.
The saved tab and terminal IDs protect other targets when the pane still exists.
An absent admission record or transcript needs no deletion.
Each completed step atomically saves its progress before the next step starts.
A repeated command reads the saved targets without native identity discovery.
Each step permits repetition after a failure between its action and its progress write.
A lost stop progress write cannot require a removed registry entry or absent pane metadata.
Cleanup also completes when only the saved transcripts remain.
A failed pane closure retains the terminal target even without herdr session metadata or a retained launch entry.
A failed transcript deletion retains the transcript targets.

Pane closure preserves other panes in the same tab.
The command adds no permission rule and uses no shell deletion command.
A repeated command prints `unchanged` when no cleanup record or selected resource remains.

The record path is `<pane record directory>/.producer-admission-<name>`.
The saved record also holds the folder.
A repeated resume uses the saved record to complete the admission.
Later resumes use the admitted row's `current_session`.
The pending record retains the original account.
A conflicting `--to`, family, or folder returns failure and preserves that record.

A lost or replaced reserved pane requires proof that its retained conversation stopped.
Recovery reserves another pane and transfers the retained entry without `pid` or `proc_start`.
It then retires the lost pane's launch.
A shell is eligible only without a retained entry or with this pending producer's identity.
A shell with another session's retained launch stays unchanged; recovery opens another tab.

`_resume` checks pending admission before it reads the model registry.
A registry error during pending admission returns `ResumeFailed` and preserves the record.
Without pending admission, a registry error permits the generic resume lookup.
The [upkeep service](herdr-restore.md#restore-services) recovers admitted producers after server loss or reboot.
`agents stop NAME`, `agents stop --all`, and local producer exit save an exit hold before the stop.
If the stop fails, the command restores the previous hold.
A remote producer exit saves a pause before the stop and changes it to an exit hold after confirmation.
If the exit hold write fails, the pause keeps the producer stopped.
The staff row stores the hold. The restart record keeps the producer's fixed ID for `agents restart`.
Upkeep checks the hold under the producer's row lock. It reports the skipped producer once per run.
`agents resume` and `agents restart` clear the hold after the producer starts or a live process confirms it already runs.
Resume in the producer's own pane clears the hold before `execvpe`, so upkeep can retry if the new agent exits.
A saved restart ID alone does not block upkeep. A crash without an exit hold remains eligible for recovery.
Accepted limit: a storage fault after the hold write (a failed directory sync, or an unreadable staff record during rollback) can leave a running producer with a hold. Upkeep then does not recover it after a crash; `agents resume NAME` clears the hold.

## Worker resume results

A session move selects only worker rows whose staff `parent` names the moved row.
Owning a run does not select every worker in that run.
The fixed staff ID identifies each child once, even when several runs name it.

`inventory.worker_resumes` returns `WorkerResume` results from `resume_message.py`.
`Resumable` and `Idle` carry `ClaudeChildResume` values.
Each child has one result:

| Result | Evidence | Message |
|---|---|---|
| `Resumable` | The child's turn was live | The unchanged command from `resume_worker_command` |
| `Idle` | The child's readable turn was not live | `idle` and `nothing to run now` |
| `Unavailable` | The run, worker record, or known staff role is unavailable | One `anomaly:` line with the child and reason |

The reader selects staff members under the moved parent.
It reads each member's backend and current native session.
The member's `KnownWorkerRole` supplies the child's role without a history fallback.
A Claude child uses its `impl_worker_<slot>.json` record and the matching status file.
The status reader searches the run root and `phases/*`; it does not search `tmp/**`.
A readable status other than `implementing`, including `capacity_exhausted`, gives `Idle`.
An unavailable child does not hide another child's result.

`session_staff.worker_run_entries(snapshot, rows)` returns real `(run, entry)` pairs.
`worker_run_index(snapshot)` also includes owner-marker runs without a worker record.
Source release uses native process evidence and does not scan those runs.
Both readers share one scan and report unreadable evidence as anomalies.
`unit_runs.run_folders` preserves root and name order, removes duplicate resolved paths, and skips each root's `active` marker folder.
An unreadable root does not prevent reads of other roots.

Pane worker resumes use `PaneWorkerResume` with a fixed staff id and an active dispatch id.
The director receives `Worker <staff id>: busy dispatch <dispatch id>` for each active direct child.
The message names idle children separately.
It keeps the account move source and destination.
Restart and pane restore select each worker's current staff parent directly.
Closed idle workers stay closed until their next assignment.

`quota_state.no_worker_mode()` reads the quota state at each call, from the current `XDG_STATE_HOME`.
`scripts/whoami/quota_state.py` is a leaf module, so restart, resume and worker code never import `quota_alert`.
A missing or unreadable state reads as off.
In no-worker mode, `worker.restore` prints `<worker>: not resumed: no-worker mode` for each Codex worker it would start or resume, and leaves it stopped.
In no-worker mode, no command starts new worker work. Each refuses with exit 2 and the `quota_state.NO_WORKER_START` text:
- `assign.sh`, before it writes run state;
- `worker.py add` and `worker.py assign`;
- `worker_panes.py start-or-resume`.
In the mode, `style_plan.py next-phase --session-dir` restores the run and phase records of each ticket and sends no assignment. It prints `style-ticket-<id>: <the refusal text>`, and the director does the ticket directly.
`worker.restore` and account moves keep work that started before the mode, so a Claude worker with an open pass resumes.
`agents restart`, the restart of a director, `agents resume` and unit recovery all restore workers through `worker.restore`.
A skipped worker leaves the restart record. The next assignment resumes it.
`stop_resume` refuses a Codex worker row with `no-worker mode keeps Codex workers stopped`, before it opens a pane.
`resume_message.worker_mode()` gives `WorkersHeld` in no-worker mode.
The resume, restart and relaunch messages of a director then carry the `no_worker_mode_on` text from `config/accounts.conf` and no worker lines.
A paused director gets `Continue where you stopped.` and the same text.
Pane worker resume uses the dispatch record and transcript to select one step.
An idle worker receives a continuation prompt when its transcript contains the open dispatch.
Restore holds the dispatch lock across the record read, receipt read, and prompt delivery.
Restore, named resume, and director notes use one receipt rule.
An unsubmitted dispatch keeps the saved assign command and its saved prompt path.
Director notes reuse shared dispatch history reads.
A matching DONE or a finished record suppresses the resume step for that pass.
A later detached result from that pass still requires another matching DONE.
Resume checks the later result records in the run that owns the dispatch.
Resume preserves DONE when every later result belongs to another pass.
Resume reports each unreadable record once. An unreadable record for a later result keeps that worker's transcript receipt.
Restore uses the worker pane with the proved native identity for its continuation.
It checks that the worker is idle. The prompt checks the native identity again before it sends input.
The decision reads the record and transcript once per worker and uses the restart's shared run index.

## Launch names

`LaunchNames.read()` supplies one staff snapshot for lookup, labels, and restore.
The Mac obtains that snapshot from the staff owner through one SSH read.
The native session ID selects the staff row and its current name.
An unavailable staff read preserves the saved launch name and prints a diagnostic.
An empty name does not clear an existing title.
Restore updates a stale tab title even when its pane title already matches.
A resume resolves the saved staff reference to its current native session.
Saved launch marks contain no copied `STAFF_ID`.
The [pane restore](herdr-restore.md) uses this same naming policy.

## Restart

A status pass shares one Codex snapshot, one worker-run search, and one pane observation.
It reads pane processes through a direct socket batch and reuses the results during that pass.
The server accepts one request per socket. The batch reads those sockets in parallel.
Read-only layout results share their data. Listed rows use shallow copies for caller edits.
The launch helper uses the same Codex snapshot.
Confirmed stops remove the selected rows' old Codex liveness entries.
An unconfirmed stop checks only its target's current identity and process generation before a launch.
A live target stays running. An unreadable target stays on the restart list.
A missing rollout file or changed generation alone does not prove exit.
Restart launches after a failed stop only when it proves absence or a different process running another conversation.
Worker restore uses the shared run index.
The stop helpers use the shared Codex snapshot.
Action checks still prove the target process before a signal.
Independent rows stop and start in parallel.
Each row stop reads the pane list and pane processes once, inside its own `herdr.observation_snapshot()`.
Each identity check forgets its pane's evidence before it reads that pane again.
A pane process that exits between the pane read and the process read counts as starting or exiting; the scan prints no notice.
Each restore line starts with the staff name, including worker restore results.

Before a power-off, run `agents stop --all`.
After login, run `agents restart`.

`agents stop --all` selects live Claude and Codex rows on this machine from `staff.json`.
The selection includes producers, such as brain, morty, and dwight.
A complete unscoped read prunes recorded rows whose sessions still run.
Each prune checks current liveness again while it holds the row lock.
A refused row lock preserves the entry.
A partial liveness read prints an anomaly and prunes nothing.
Recorded stopped rows remain pending.
Each stop holder saves its selected rows while it holds their row locks.
A refused stop removes its provisional entry before the row lock releases.
No command clears the whole record.
It starts nothing.
`stop --all --only ID[,ID...]` and `stop --all --account ACCOUNT` merge selected rows into the record.
These scoped stops preserve other saved rows.
A live session without a staff row prints one anomaly line. Stop does not save it.
`agents restart --all` uses the same selection and stop loop, then resumes the selected rows.
It keeps only the row IDs whose starts failed.
Before it stops a worker, restart checks the settings for that worker's assigned role.
If the settings are invalid or unreadable, restart reports the role error and leaves the worker running.
Restart checks the worker's current role under its row lock. If the role changed, restart leaves the worker running.
Other selected rows continue through stop and restart.
Worker restore uses the checked policy if the role still matches.
A role write between the last restart check and restore can make restore refuse and leave the worker stopped.
If this rare race occurs, a later restore recovers the worker when its role and the role's settings are valid.

`agents restart ACCOUNT --all` selects live rows on that Claude or Codex account.
It reads the account from each observed process environment.
A hosted Codex thread uses its daemon's account home.
Selected rows restart on ACCOUNT.
Each worker matches its own account.
Filtered restarts leave unselected owners and workers unchanged.
A live worker without account evidence prints `NAME: left alone; account unknown` once.
Without `--all`, ACCOUNT filters saved rows by their last session's account.
An unknown ACCOUNT prints one error and returns status 2 before any stop.
`--dry-run` lists only selected rows and changes nothing.
Without ACCOUNT, the existing selection and role accounts apply.

`--only ID[,ID...]` also limits `--all`; ACCOUNT and `--only` must both match.
An `--only` run observes only the requested rows as restart candidates.
It judges workers only for selected worker rows and their parents.
It retains incomplete evidence and anomaly lines for a selected worker.
It skips the scan for live sessions without staff rows.
The shared pane read still supplies complete identity evidence.
A run without `--only` keeps the existing observations.
If a stop includes the caller, restart continues in a detached process before the first stop.
The caller prints the restart log path and returns.
Other restarts run in the caller's process.
A stop checks the current conversation before it sends a signal.
A failed row does not stop other rows.
After the start pool finishes, restart reads each failed unit director or showrunner conversation again.
If the read proves that the conversation stopped, restart retries that start once with the same arguments.
A successful retry prints only `<name>: down; started`.
If the row becomes live before the retry launches it, the retry prints only `<name>: unchanged`.
A second failure prints `<name>: down: <reason>` on stdout and keeps the row pending.
An error without text uses its type name as the reason.
A live or unknown conversation keeps the existing anomaly line and gets no retry.
Trust-write failures and row-lock refusals keep their existing behavior without a retry.
An unreadable native identity in a retained pane excludes that pane from placement candidates.
It does not abort another row's launch.

`agents restart` resumes the saved rows that do not run.
`agents restart` and `agents stop --all` print the log path first.
The commands keep the full report under `shutdown_state_root()/logs` and retain the files.
A command that stops its caller continues in a detached process.
The parent gives the detached process access to the open log file.
The detached process fixes the selected staff IDs before it attempts the claim.
The report uses these IDs for its session count.
The detached process creates a claim file before it stops any session.
On Linux, the detached process confirms the claim to `systemd-run`.
On macOS, the detached process confirms the claim through a pipe.
The parent tries to create the same claim file after the launcher returns.
If the child claims the log, the parent prints one line with the log path and the background status.
The child writes a failure report if a step fails after it claims the log.
If SIGKILL or power loss leaves only the log path, run `agents restart` again.
The failure report names each selected session and records its known result.
The detached process writes and flushes the full list of sessions before it stops any session.
If the parent claims the log, it reports the count, the cause, and the step in the terminal and the log.
A child that starts after the parent claims the log exits without stopping any session.
If a report write fails, the child tries the same log again and returns a nonzero status.
If the second write fails, the child writes the full report to stderr.
The Linux user service sends stderr to the journal.
Its final line includes the names and steps for unresolved sessions from the first process.
If log setup fails, the first line gives the path and the rest of the report still appears.
If the command needs to detach, the report gives the repair for the log error.
For an unknown log error, Nate sends the error to the efficiency unit.
The report counts confirmed sessions out of the total, including workers.
Restart counts sessions that resumed and sessions that already run.
Dry runs show the count and list sessions without making changes.
Each unresolved session gets one line with its name, the reason, and who does the next step.
A worker line also names its owner.
The final line gives the open steps for the directors and Nate.
If there are no steps, the final line says `Nothing for you to do.`
Restart keeps entries that need another restore attempt.
Restart reads the dispatch state and live detached runs by worker staff ID.
Restart counts a worker as back when it needs no resume message or when restart sends the message.
Restart sends a required resume message before it tries to recover the worker's completion.
If recovery fails after the resume, the worker counts as back and the report gives the director's step.
If removing a saved entry fails, the report names the session and gives Nate's step.
Restart removes only worker entries that it resolves.
Run `worker.py restore` in the owner session to see completed dispatches in open phases.

Herdr upkeep skips row work while a stop or restart holds the activity lock.
Saved rows can remain after the activity lock closes.
Stop pauses upkeep and waits for its producer row locks before reading fresh staff and liveness data.
The `upkeep_row_wait_seconds` setting in `config/shutdown.conf` limits the wait.
If the time expires, the report names the row and gives the command to run again.

A restarted unit director or showrunner gets one continuation message in its current conversation.
The shared resume message builder supplies the text.
The default cause is "agents restart ran".
The restart option `--why TEXT` replaces that cause.
The message asks the session to continue its work.
If Codex message delivery fails after launch, restart reports one anomaly line with the director's name and the first error line.
The director counts as started and leaves the restart record.
Restart checks each unit director and showrunner that it starts with a continuation message.
The check uses the launched account home and native session identity.
It reads only turns after that director's start.
Herdr keeps one cursor for each native transcript and preserves the requested cursor's launch offsets.
The Claude resume check uses the requested cursor when the pane reports the same ID.
The check creates its own cursor when the pane reports a different ID.
Herdr waits for the native session identity and uses that identity to check the opening prompt's target.
A starting agent can lack its registry record, its process start time or its argv.
Herdr retries the identity read and the target check until the launch deadline.
The pause between tries starts at `PANE_POLL_SECONDS` and doubles up to `START_RETRY_MAX_SECONDS`.
Herdr reports each reason for retaining a target once per target during an opening prompt attempt.
Only a failure at the deadline prints the native session error.
Herdr checks the requested and discovered transcripts before typing, sending Enter or reporting a failure.
If the send gate finds a different native ID, the failure check includes that ID's transcript.
The receipt check reports a matching turn, a readable search with no match, or unknown evidence.
An inaccessible transcript directory gives unknown evidence.
Unknown evidence blocks a send.
A matching turn gives the submission state `confirmed`.
A new launch with no transport process gives `not_sent` when Herdr finds no matching turn.
All other results without a matching turn have the state `unknown`.
Add-unit closes a session for an opening prompt failure only when the state is `not_sent`.
Herdr uses the last native ID reported by the pane to publish the staff relationship.
When a transcript confirms the prompt during startup, Herdr publishes the staff relationship before it returns success.
Staff publication uses the remaining launch time.
The result reports a staff publication error in a separate field.
A change in the recipient does not cancel a matching turn.
Herdr requires one registered foreground agent and a recorded recipient for the same process.
The registered agent must lead the foreground group or be a Claude child of the group leader.
Herdr accepts the child only when its direct parent runs the package's resolved `scripts/lib/relaunch.py` with the `run` argument.
Herdr reads the live child and parent identities before and after it reads the parent command.
Herdr reads the parent command again and refuses input if either identity or the command changes.
Herdr limits the slash-command record lock to the remaining launch time.
Herdr prepares the slash-command record before it reads the transcript.
After the gate accepts the send, Herdr writes the record.
Herdr can evict an old record after the transport starts.
If startup fails, Herdr removes the new record and keeps the older records.
Each attempt reads the transcript for a matching turn before it checks the pane.
The gate reads the composer first, then checks the terminal and registered foreground recipient.
The gate checks that the agent is idle before record publication and transport.
Typing requires an empty composer. Herdr types at most once per launch.
An empty composer ends Enter retries for that launch.
Add-unit, add-showrunner, and promote-showrunner reruns check the transcript before they send Enter for a matching draft.
A rerun in the retained pane never types the prompt.
If the composer is empty at the deadline and the transcript has no matching turn, Herdr reports "sent once, not confirmed".
The terminal, process, or composer can still change between the last check and the terminal's receipt of input.
The launch deadline limits the pane queries and both transport calls.
The prompt transport uses the remaining time after its process starts.
If Herdr declines to send a resume prompt, the anomaly tells the operator to check the retained pane and send the prompt by hand.
For a Claude launch with an opening prompt, restart uses the greater of `start_seconds` and `opening_prompt_seconds`.
`config/shutdown.conf` sets these limits to 20 seconds and 90 seconds.
The operation deadline can reduce either limit.
Other launches use `start_seconds`.
The launch limit covers the start, input readiness, and proof of the opening prompt's user turn.
The separate resume check then requires an assistant turn or tool call.
The cursor skips complete historical records without parsing their JSON.
Herdr preserves a caller's cursor and prepares one before the start command when the caller supplies none.
If the native session identity changes, the cursor reads that transcript from byte zero and filters turns by the launch time.
The cursor retains observed turns, so prompt submission does not remove evidence from the resume check.
The reader leaves a final record without a newline unread until the writer completes it.
The launch boundary follows the last complete newline.
A malformed Codex record with a newline makes the transcript unreadable.
A Claude transcript missing from the current read also makes the transcript unreadable.
A matching user turn followed by an assistant turn or tool call confirms resumed work.
All checked directors share one deadline.
The check ends as soon as every director resumes.
After the deadline, the report names each director whose check failed.
The reason can be a missing continuation message, no work after the message, or an unreadable transcript.
The director checks the continuation message.
It skips held directors, unchanged rows, failed starts, workers, producers, dry runs, and stop-only commands.
It does not change the exit code, restart record, or staff.
config/shutdown.conf sets resume_check_seconds=180 and resume_check_interval_seconds=5.

A paused or exited director stays stopped and keeps its hold record and restart entry.
A running row gets no continuation message.
Producers keep their existing start behavior.
Herdr server restore keeps its existing start behavior.
A row leaves the saved list when it starts or already runs.
`stop_resume.resume` removes a row after a confirmed start while it holds the row lock.
Upkeep, account moves, watchdog recovery, and named resume share this removal.
Named resume also removes a row after a successful continuation message.
Retired recovery removes a row after it proves the resumed native session.
These removals and restart cleanup occur before the row locks release.
Native resume, continuation messages, restart, retired recovery, and pruning use `shutdown_record.remove_started`.
A failed start adds the row to the list.
A refused row lock changes no saved row.
`--only` leaves other saved rows on the list.
`stop --all --dry-run` prints each selected name, row ID, kind, and tab position with `would stop and save`.
`restart --dry-run` prints saved rows and identifies live rows outside the saved list.
A row without a saved tab says so on the same line.
Both commands change nothing.
`--only ID[,ID...]` selects staff rows for callers that resume a moved unit.

`shutdown_record.py` stores only a JSON list of staff row IDs in `shutdown/restart.json`.
The record stores no session facts, progress, claims, recovery states, or pane positions.
The record holds only rows that a restart starts:
- `stop --all` and scoped `held-sessions` save no worker that `worker.restore` would not start.
- `worker.not_restored` names that worker: a Codex worker in no-worker mode, or a worker with no active dispatch and no detached run.
- `restart` removes a saved id with no staff row or a saved worker that `worker.not_restored` names.
- The report names the removed entry and gives the reason.
- `restart --dry-run` prints the same line with `would leave the restart record` and changes nothing.
- `staff.retire`, a worker pane close in `worker_panes.close`, and the Codex worker stop in `worker_capacity.py` remove the id with `shutdown_record.forget`.
- An id stays only after a failure that a retry can fix, such as an unreadable dispatch record.
`shutdown_record.update` holds the record lock for each change.
Unscoped `held-sessions` prunes confirmed live entries after a complete liveness read.
It checks current liveness again under each row lock.
It saves the live tab positions and preserves recorded stopped rows.
`held-sessions ACCOUNT` and `held-sessions --only ID[,ID...]` merge selected rows and preserve other saved rows.
A failed staff read preserves the record.
A partial live-row read prunes nothing.
The stop loop can still save rows that it stops.
Herdr stores the tab order and associates each saved tab with its staff row ID.

Start reads `current_session` and the worktree from staff.
A producer without a recorded folder uses the user's home folder.
The native pane reader identifies Codex sessions from their resume arguments or pane reports.
Callers without a report read the process's own pane through the same reader.
No thread identity comes from an open rollout file.
It finds the transcript and its account by the current session ID.
Claude uses `--resume ID`. Codex uses `resume ID` in the worktree.
The launch sends no work prompt.
Start leaves live sessions and other foreground work alone.
Restart does not send phone pushes or use `add_unit.py`.

`restart_support.py` retains the continuation-message API for the quota watcher.
`restart.wait_for_confirmation` supports the existing unit attachment path.

Claude launches write folder trust in the target account before the process starts.
The write uses the repository trust key, or the folder itself outside a repository.
A failed write prints one anomaly with the folder, account file, and error.
The launch then returns its failure result without a process start.
A composer with a draft waits until the deadline without changes to the draft.

Worker restore shares the command's run index and Codex snapshot through stopped-worker startup.
Restore uses `worker_panes.worker_run` for run selection and fallback.
Stop selects workers from their current staff parent.
An owner restart excludes each worker whose role check failed from both stop and restore.
An explicit stop without restart can stop workers with invalid role settings.

An unconfirmed worker stop needs targeted proof of process absence before restart can launch that worker.
Live or unknown evidence leaves the worker pending.
The worker check uses the same function as the Codex row check.

### Legacy Claude launchers

`assign_claude.run` installs no signal handler.
Its child commands inherit the launcher's process group.
Stop that group to stop the launcher and its child commands.
`assign.sh` uses `exec` for `assign.py` and creates no separate process group.
The current native worker path does not call the legacy Claude launcher.

The launcher checks the claim owner under the claim lock before each publication.
The launcher records its PID and process start in `impl_claim_<slot>.json` before work starts.
The claim also records the host, boot, and PID namespace.
`read_worker_claim` rejects dead processes, zombie processes, and reused PIDs.
It compares local processes only when the saved scope matches the current scope.
It retains claims with missing start evidence, missing scope, different scope, or unreadable identity.
Status readers recover an `implementing` status whose launcher has ended.
They write `error` and add a blocked board entry under the claim lock.
The next assignment can replace the ended claim.
Normal completion still releases the claim.
An interrupted exit retains the claim, including a Python `KeyboardInterrupt` from SIGINT.
A killed launcher cannot record pass completion.
Its ledger entry keeps the last saved state.

### Gates during a session restart

A session stop sends TERM, then KILL after the grace period, to the one proved agent PID (`stop_resume.py`).
It sends no signal to the process group, the child processes, or the build cgroup.
A normal stop keeps the pane and its shell.

A detached run continues across a session stop or restart.
`detached_runs.py start` starts its supervisor in a new process session.
The command runs in a `builds-detached.slice` scope when systemd is available.
The command runs in its own session, including through the scope prefix.
The record saves the command's process group.
The supervisor uses default TERM, INT, and HUP dispositions.
It installs no signal handler.
`detached_runs.py stop` checks the supervisor and the command identity before it signals the command group.
The supervisor stays alive to record the exit status and deliver the result.
A worker that stops its own run keeps its pass active.
A stop from another caller can end the pass and notify the director.
The command gate starts a watcher in the command group. The watcher reads the supervisor pipe.
The supervisor writes "done" to the pipe after the command ends.
If the pipe closes without "done", the supervisor ended first: a signal, a SIGKILL or a crash.
The watcher then runs `detached_runs.py supervisor-gone`. It leaves the command group and saves a stop reason.
It sends TERM to the command group, waits for the command leader and reaps the record.
So a supervisor end never leaves the command running, and the worker gets a STOPPED result.
If the supervisor dies, the next start, worker restore, or explicit `detached_runs.py reap` finishes its record and delivers its result.
The result reports no exit status unless the command or the supervisor saved one. `verify.sh` saves its own status at exit.
The record, the log and the exit code stay in the run folder's `detached_runs` directory.
After the command ends, the supervisor starts a new Python process to deliver the result from the current code tree.
The supervisor waits for delivery before it marks the run finished.
The result goes to the worker's fixed staff row and its current conversation.
A stopped worker's result goes to its parent. A stopped parent's result waits in its queue.

`verify.sh final` detaches by itself only for a registered Codex worker.
A bare call from a Claude director stays attached.
`assign.sh` does not detach the commands that its worker starts later.

Survival of a gate run in a Claude background shell is unproved.
`copies.py run` starts the gate attached, and `merge_gate.py` stops its module groups on TERM or HUP.
Before another run, check for a complete saved gate result.
Put a gate that must survive a restart in a detached run.

The cloud gate runs as its own user service and on a remote guest.
A local stop does not end it. Its outcome stays under the Git `cloud-gates` folder.
The local import and type checks after the cloud result still need the local caller.

A surviving run keeps its memory queue place and its age.
A new process does not take over the place of a dead waiter.

A session restart is not a machine restart. A reboot or power-off can end local supervisors and waiting processes.

## Reboot

Run `agents reboot` from a terminal outside Herdr. The command refuses inside a Herdr pane.
The shared Herdr session must have the name `agents`.

1. Save and stop the local sessions through `agents stop --all`.
2. Stop Herdr only when a fresh read finds no live or unidentified agent.
3. Run `~/.claude/scripts/lib/py ~/.claude/scripts/lib/session_move.py`.
4. Restore the server and the saved sessions through `agents restart`.

The server stop uses `herdr --session agents server stop`.

Before the first step, the command runs `session_move.py --dry-run` through the same Python shim.
The plan gives the size of idle folders and counts live folders to size after the stop.
Use `agents reboot --dry-run` to see the plan without running the four steps.
If session move returns an error, the plan reports that it is incomplete and omits the totals. The dry run returns status 1.

The reboot holds the Herdr upkeep job lock across the four steps and releases it on failure.
The reboot also holds `reboot.lock` exclusively beside the shared Herdr server socket across the four steps.
The common Herdr launcher takes a shared lock before it creates a pane.
The direct Herdr start for a producer reservation takes the same lock.
A lock refusal reports that a reboot is running.
Nested starts in one process use the same open lock file, including starts in the reboot's restart step.
The Herdr server start functions take the same shared lock before they can start a server.
Manual starts through a pane command, `accounts.py exec`, `cli_agent.sh`, or native Herdr can bypass the lock.
A shell wrapper can start an agent after a resume times out, and a fork without exec can retain the lock.
All staff tooling shares one `XDG_CONFIG_HOME`, because a process with a different config home locks a different file.
A slow Ghostty window or pending Linux service can start the server after its shared lock closes and reboot takes the exclusive lock.
A remote attach started during a reboot can start the server through the Herdr SSH bridge without the lock. Attach after the reboot prints its Attach line.
The `agents resume` command holds the lock while it changes staff or recovery records.
The direct resume API also holds the lock for starts in an existing pane.
The `agents restart` and `agents move` commands hold the lock while they save restart or move records.
The `agents add-producer` and `agents transfer` commands hold the lock while they change producer state.
Dry runs can read state during a reboot.
The Nix healer takes the shared lock before it saves a launch reservation.
If reboot holds the lock, the healer keeps the stop record and resumes the session on its next available sweep.
A failed stop, server stop, or restart leaves the next step in `agents/reboot/reboot.json` under the user state folder.
Run `agents reboot` again to continue at that step.
A retry at the server stop checks for resumed sessions. Step 1 saves and stops those sessions and keeps the earlier restart entries.
If an interrupt ends the reboot, the report lists the saved sessions that still need a restart.
A folder move failure permits restart to proceed.
The final report names the failed copy folder if available and assigns the move failure to the verify owner.

The first output line gives the log path in the folder set by `reboot_log_folder`.
The default log folder is `~/.local/state/agents/reboot/`.
The command adds the time to each log line. It keeps older logs and sets `latest.log` to the most recent run.
A terminal shows each step on one line with an elapsed time and a spinner.
Stop and restart progress includes the current session name.
The report counts completed sessions and reads the current total under one lock.
Output to a pipe uses one line for each name and omits the spinner.
The move step shows the `session_move` summary. The per-folder refusal details go to the log.
The report names sessions or workers that need a step and says whether Nate must act.
The last line gives the command `herdr session attach agents`.
