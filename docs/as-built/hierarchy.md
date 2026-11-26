# Agent hierarchy

## What it is

The hierarchy connects the user's product choices to work in separate repository checkouts. Showrunners coordinate productions, unit directors coordinate assignments, and named workers change or inspect assigned files. Staff identities connect these roles across session changes, account moves, renames, and transfers. Shared commands carry messages, record results, present user decisions, and remove temporary output when its task ends.

## How it works

### Roles and authority

| Role | Responsibility | Contract |
| --- | --- | --- |
| User, the executive producer | Decides product direction, scope changes, and irreversible actions. | [Decision criteria](../decision_criteria.md) |
| Producer | Runs its own named job through shared producer management. | `commands/producer/produce.md`; morty also loads `commands/producer/morty.md` |
| Showrunner | Coordinates units, resolves dependencies, integrates checkpoints, and presents user decisions. | `commands/showrunner/produce.md` |
| Unit director | Defines assignments, dispatches workers, checks results, and prepares checkpoints. | `commands/unit/direct.md` |
| Worker | Changes or inspects its assigned files through a native Claude or Codex conversation. | [Worker lifecycle](long-lived-workers.md) |
| Held designer or reviewer | Serves one design through a fixed staff identity and an explicit binding. | `scripts/astra/design_roles.py` |

Morty, brain, and dwight have staff kind `producer` and no parent.
They remain individual operators; their shared kind defines management.
Each producer's job is in `<producer_folder_root>/<name>/README.md`.
`commands/producer/greenlight.md` and `commands/producer/to_production.md` define production preparation separately.
The [staff contract](staff-accessors.md#producer-rows-and-commands) defines their commands and document folders.

Top-level showrunners are peers. A child showrunner retains its own production, units, and integration branch.
Its staff parent identifies the receiving showrunner. The user can speak directly to either showrunner.
Each showrunner starts its own workers. The hierarchy has no central worker allocator.
Directors and showrunners coordinate planned implementation through workers.
Review assignments use read-only access.

The [production format](../production_format.md) defines production documents and role contracts.
The [role and name contract](roles-and-names.md) separates organizational role, assignment role, and displayed job.
`config/names.toml` supplies display names through `scripts/lib/names.py`.
Names do not identify a member's stored relationships.

### Staff and execution

`scripts/production/staff.py` owns `~/.local/state/staff/staff.json`, with schema 2.
The Mac sends staff operations to the owner on natedev.

| Type or field | Meaning |
| --- | --- |
| `StaffRow.id` | Fixed member identity. |
| `StaffRow.parent` | `ReportsTo(StaffId)` or `StaffRoot`. |
| `StaffRow.current_session` | Current native Claude session or Codex thread. |
| `KnownWorkerRole`, `WorkerRoleUnknown` | The worker's current role or an explicit unknown role. |
| `SessionHome`, `HomeUnknown` | The account configuration folder or an explicit unknown home. |
| `PresentWorktree`, `RemovedWorktree`, `WorktreeUnfilled` | Current worktree location or its explicit missing state. |
| `LiveDoc`, `WrappedDoc`, `DocUnfilled` | Production document location or its explicit missing state. |
| `Paused`, `Exited`, `Active` | Staff hold state. |
| `ClaudeBackend`, `CodexBackend`, `BackendUnavailable` | Backend evidence derived from current native records. |

Rows, locations, and hold records preserve unknown JSON fields through `unknown_fields`.
A location change preserves those fields. A new hold starts a new hold record.
An incompatible change requires a new schema number.
The [staff accessor list](staff-accessors.md) identifies readers and writers.
Stored references keep the fixed staff id and read other current staff facts when used.
Dated history retains event-time facts for attribution.
Display names do not select stored relationships or replace native identity evidence.

`scripts/herdr/worker_panes.py` owns worker execution and observation.
`observe_parents(parents, record) -> dict[StaffId, list[WorkerPane]]` shares one pane read across parents.
It reads no panes for an empty parent list.
The roster, unit board, staff display, and cleanup consumers use this evidence.

| Observation | Meaning |
| --- | --- |
| `OpenWorker` | A worker occupies a known pane with a reported agent status. |
| `StoppedWorker` | A known worker pane has stopped execution. |
| `ClosedWorker` | No worker pane remains in the observed evidence. |
| `ProcessUnknownWorker` | The pane is known, but its process evidence is incomplete. |
| `UncertainWorker` | The evidence cannot establish the worker's pane state. |

Unknown evidence retains its reason. Unrelated process diagnostics do not refuse a staff write about another member.
The target's own live identity still refuses retirement. Unavailable peer evidence also retains the target.
`unit_runs.report_anomaly` prints each repeated run anomaly once per command. A `ContextVar` holds the printed messages, and verify_helper.py runs each command in a new context copy.
A shared read through `worker_lifecycle.locked_state` creates no missing state folder.

### Assignment and result flow

1. The director sends an assignment through `scripts/direct/assign.sh`.
2. Staff admission selects the fixed worker identity before native publication.
3. `progress_history.begin_dispatch` records the dispatch, assignment role, and expected completion id.
4. The launcher delivers the prompt to the worker's native pane and returns after submission.
5. `scripts/herdr/turn_end.py` matches the native event to the dispatch and its completion evidence.
6. The completion handler saves the reply, records the result, and runs completion actions.
7. The handler ends `scratch.worker_task(row.id)` and closes the worker pane.
8. The handler sends the director one result notice with the reply path and dispatch id.

`WorkerEvent` carries the native identity, event kind, and optional reply.
`TurnOutcome` distinguishes completion, an unfinished reply, pending work, uncertainty, and errors.
`HookCompletion` records completed actions so an interrupted handler can continue.
An already finished hook event changes no dispatch.
A missing final transcript record does not erase a matching DONE reply supported by the dispatch prompt.
A live detached command prevents completion.
A pending command result keeps the assignment active.

Normal DONE closure retains the staff row and native conversation.
The next assignment resumes that conversation in a new pane.
An explicit worker end can retire the row.
A managed design completion can end its worker and release its held role.
Closure uses pane and terminal identity. It does not wait for foreground process cleanup.
Unknown pane ownership produces a retained-pane diagnostic.

A family change preserves the fixed staff id and transfers source context.
It checks busy work and live detached commands for open, stopped, and closed sources.
It releases the source before it selects the destination pane.
`DestinationAfterSourceRelease` represents that ordering.
Failed publication cleans only the recorded destination process and terminal.
`DestinationStopped`, `DestinationRetained`, and `NoDestinationToStop` preserve the cleanup result.
A replacement process in that terminal stays open.

`scripts/agents/detached_runs.py` starts and supervises long commands.
Records and logs live under `<run>/detached_runs/`.
`detached_run_process.py` proves supervisor identity before a caller stops a run.
The command runs in its own session, and the record saves its process group.
`detached_runs.py stop` signals the command group.
The supervisor stays alive and delivers the result.
A worker that stops its own run keeps its pass active and receives the STOPPED result.
The supervisor installs no signal handler.
The orphan reaper finishes a dead supervisor's record and delivers its result during start, restore, or explicit recovery.
Results return to the worker or its current holder. Callers do not poll.
Finished runs are deleted with their logs and lock files when no reader needs them.
A worker's pass end deletes the folder's finished runs, and so does each start.
A run stays while its worker's pass is open and the run started in that pass.
A run of another owner stays until that owner starts a newer run.
A run whose owner has no staff row goes at once.

### Channels and user decisions

| Channel | Current owner and route |
| --- | --- |
| Session discovery | `scripts/message/roster.py` reads local and peer evidence. |
| One recipient | `scripts/message/send.py` resolves staff identity before selecting transport. |
| Groups | `scripts/production/broadcast.py` selects roles, a production's unit directors, or a unit's workers. |
| Assignment status | `scripts/direct/board.sh` records worker status for the director. |
| Native completion | `scripts/herdr/turn_end.py` saves the reply and sends its result notice. |
| User question | `scripts/production/update_registration.py` records the question and its asker. |
| Changes for user review | `scripts/production/change_review.py` supplies the shared scoped queue. |
| Phone message | `send.py --to user` uses the notification review and delivery path. |
| Unit status | `/showrunner:units` shows phases, ETAs, worker runs, and queued work through `scripts/production/unit_phases.py`. |

A bare worker name resolves within the sender's own unit.
A match only in another unit refuses delivery and lists the available full paths.
An explicit path matches its complete canonical path.
Use `<production>/<unit>/<worker>` or `worker:<unit>/<worker>` under the sender's showrunner.
Fixed staff ids and native addresses remain valid explicit identities.
The [messaging contract](session-roster.md) defines peer lookup and delivery outcomes.

`change_review.change_queue(path, scope, below=False) -> ChangeQueue` supplies both `/next change` and `change_review.py show`.
`ChangeQueue` carries the pin name and ordered entries.
Scoped lookup includes earlier production lists and routes their rows through current staff ownership.
Displayed IDs use the responsible showrunner, such as `sdlc/C9`, through the page's shared staff resolver.
The queue retains plain keys for current-list rows and list-qualified keys for other rows.
Prerequisites retain that list qualification.
Row commands accept both displayed IDs and existing list-based IDs.
An ambiguous displayed ID requires `--list <path>`.
Saved pins therefore survive attachment changes without copying or renumbering rows.

`change_review.py withdraw --row <id> --reason <text>` records the caller's staff name and preserves any prior verdict.
The event uses `unknown` when the current session has no staff name.
Withdrawal of an open row keeps its staff ID, urgency, and check instructions.
`reopen --row <id> --reason <text>` restores the row to To review.
It keeps the row ID and review log. It adds an event with the caller's name and reason.
The change queue uses the saved urgency and move-to-end time to order the row.
The command refuses a row without a withdrawn verdict.
If an older withdrawal lacks the saved urgency or check instructions, the command names the missing fields and refuses to reopen it.
`correct-withdraw-who --row <id> --at <ISO timestamp> --who <staff name>` corrects one withdrawal event in either table.
Use the full timestamp from the log, with its offset. The correction keeps the other log entries.

`record_verdict` returns `VerdictRecorded`, `VerdictAlreadyRecorded`, or `VerdictConflict`.
An identical retry repeats the result and scratch cleanup.
A conflicting retry reports the saved verdict and still attempts scratch cleanup.
`lands` accepts opening `works` or `fine`, after optional `okay` or `ok` and a row id.
Case and later words do not change that decision.
The [change page](review-links.md) uses the same saved answers and ownership.

Open questions and their replay files use the owner's fixed staff id.
`answer` records the answer under the list lock before it removes the open item.
The replay resolves current names and parents through staff, including after rename or transfer.
Its 48-hour replay record supports retry.
Canonical Review question logs retain the user's answer words without that limit.

### Checkpoints and durable records

`progress_history.py` records run, phase, dispatch, wait, and completion events.
`waits` exposes recorded wait kinds, holder observations, missing evidence, and attribution problems.
`NamedHolder`, `HolderNotNamed`, and `MalformedHolder` keep different evidence states separate.
Merge receipts record project, target branch, merge commit, and time.
These producers remain available independently of hierarchy reporting.
There is no hierarchy report command in this repository.

`atomic_file.write_text_atomic` publishes complete content and persists the directory entry.
Question and change-list writers use shared locks around their durable updates.
Scratch removal leaves an external removal record until deletion completes.
A retry can therefore find an interrupted removal.

`change_review.merge_review_problem(message, *, is_merge)` defines the merge-message rule.
Merge commits permit only invisible `Change-Review: none` entries.
Visible entries and `Change-Review-Covers` belong on a later plain commit.
The shared checkpoint parser applies this rule to commit, merge, continue, and amend paths.
Trailer arguments do not replace a complete checked message.
`unit_commits` excludes changes already on registered production integration branches in the same Git repository.
The checkpoint scope check excludes imported paths only while their content still equals the imported content.
An independently changed outside path still requires authorization.
The [merge contract](showrunner-automation.md) describes integration and publication.

### Transfer and cleanup

`transfer_unit.py apply` changes the fixed staff parent before it sends the unit a notice.
The notice uses the bare staff id and a key containing the member and receiving parent ids.
An unchanged transfer sends no second notice.
Tab reconciliation follows the notice. A failed tab move does not reverse the membership notice.
`herdr.tab_order` keeps saved tabs in order and inserts new tabs after the last compatible position.
The showrunner comes first, then unit directors alphabetically, then other tabs alphabetically.

`staff.set_run(identity, OpenRun(run))` records the director's open run in its staff row.
`unit_runs.open_run(session_id)` reads the row whose `current_session` matches the session.
`staff.clear_run(identity, run)` clears the pointer only while it still names that run.
`progress_history.py finish-run` clears the pointer even for an already finished run.
`end_session.sh` clears it after role, worker, and run scratch cleanup.
A cleanup failure prints its step and returns nonzero.
The failure does not prevent pointer cleanup.
A busy repository copy can remain through the separate `copies.py remove` step.

The director creates a model worktree for a parallel phase or trial project.
The director runs `~/.claude/scripts/lib/py ~/.claude/scripts/worktree/worktrees.py remove <path>` after the checkpoint reaches origin.
The director sends the reported paths to the showrunner as a removal list.
Workers use numbered copies for tests and proofs that need source copies.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/lib/build_copies.py run --source <worktree> --commit <hash> --showrunner <name> --unit <unit> --work <word> --purpose <word> -- <command>`.
Workers use `scratch.py new --worker [--name <word>]` only for downloads and files unrelated to builds, tests, gates, or source copies.
Names have the form `<showrunner>-<unit>-<worker>-<suffix>[-<word>]` under `TMPDIR`.
`scratch.worker_task(staff_id) -> str` names their shared task.
`scratch.end(task)` removes folders for that exact task and permits retries.
The DONE handler attempts cleanup before pane closure.
A cleanup failure adds one line to the director notice and permits closure.
Remote temporary files and folders also require a context-managed lifetime or an ended scratch task.
Fixed remote paths such as `~/.cache` do not provide that lifetime.

## Invariants

1. Preserve the user's authority over product direction, scope, irreversible actions, and changes to roles or responsibilities.
2. Keep top-level showrunners as peers and read parent relationships from staff.
3. Preserve fixed staff identity across session, account, name, and parent changes.
4. Keep assignment role separate from member identity and displayed job.
5. Distinguish missing evidence from zero, stopped execution, and closed panes.
6. Match completion and retries to the exact dispatch identity.
7. Preserve unrelated workers, marks, folders, and replacement processes during cleanup.
8. End temporary output through its owning task without adding periodic checks.
9. Keep tests on fixture staff, sessions, histories, and folders.
10. Keep Python typed and preserve explicit unavailable states at storage boundaries.
11. Keep user-facing times in the user zone and durable event stamps in UTC.
12. Retain the user's review until a verdict arrives; queue age does not authorize closure.

## Calibration and gotchas

1. Read detached-command limits from `config/detached_runs.conf`. The current per-worker limit is three, and a live twin takes precedence.
2. The same config limits each result line to 300 characters and each result message to 2,000 characters.
3. Keep the 48-hour answer replay separate from any long-term response-time measure.
4. Keep `unit_runs.py` top-level imports within the standard library. The Mac usage collector ships this file without sibling modules.
5. Retain the supported `delegate` run-root fallback until its removal condition holds. A calendar date alone does not prove that old runs ended.
6. Keep diagnostics for unreadable evidence without treating an unrelated process as the target of a staff operation.
7. Avoid one-process runs of unrelated test modules. Shared fixture state can invalidate their results.
8. Keep measured build intervals separate from elapsed delivery time. Concurrent queue, build, and repair intervals can overlap.
9. Do not infer a missing demo time, wait cause, or freed capacity from a merge, closed issue, or removed mark.
10. A `TemporaryDirectory` context does not guarantee cleanup after SIGTERM. The design adds no signal handler for this case.

## Why

The user prioritizes Hana functionality, ergonomics, beauty, and a robust development process without a fixed ship date.
Supporting tool work records its direct or indirect link to Hana.
The design imposes no fixed Hana capacity share, measured admission-cost requirement, central worker allocation, or daily user-review budget.
Account expansion requires evidence of more Hana delivery; lower phase cost alone is insufficient.

Persistent native conversations preserve context while separate panes provide temporary execution.
Fixed identities prevent names, historical paths, and account changes from selecting the wrong member.
Shared queues prevent attachment changes from hiding earlier user decisions.
Atomic writes and repeatable end operations preserve work after an interrupted command.
Each task removes its output at its end without adding another leftover scan.

User contact stays on demand for routine reports and queued reviews.
Peers receive routine coordination directly. Showrunners present user-only choices and actions.
Fewer hierarchy levels alone do not establish faster delivery.
Historical worker counts, token shares, and build-hour sums do not establish a causal delivery improvement.

## Not built

- The four-week wait-holder trial has no recorded start; it requires named holder evidence from both build and model producers.
- The approved one-week widget completion trial has no recorded start; the user must restart it through Hana's showrunner.
