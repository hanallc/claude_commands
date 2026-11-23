# Long-lived workers and held Astra roles

## Native workers and detached commands

Live consumers read staff membership and backend evidence.
`StaffSnapshot.backends` derives backend evidence from each current native session.
`session_staff.worker_backend` returns recorded evidence or a reason for unavailable evidence.
Worker displays and cleanup read `worker_panes.observe_parents`.
They show stopped, closed, unknown and incomplete states.
`inventory.worker_resumes` keeps Claude child commands in `ClaudeChildResume`.
`refusal_watch.py` owns `QUOTA_REFUSAL`.

The assignment launcher uses native worker panes.
Detached commands use `detached_runs.py` and the `detached_runs/` record folder.
Completed run folders keep their roster files.
`progress_history.py` and `usage/agents_log.py` keep their historical reads.

## What it is

Unit workers retain their native conversations across assignments.
Staff records identify each worker independently of its current session, run or account.
A later assignment resumes the same conversation when the worker remains available.
Codex code review uses read-only review workers.
The [daily simplify run](simplify-lens.md) uses a fresh Claude review.
Workers compact between assignments when their recorded context exceeds the configured limit.
The launcher returns when a child accepts that dispatch through `scripts.lib.detached.start_detached`.
The child uses a separate session, so a signal to the caller's process group does not stop it.
`WORKER_COMPACT_SECONDS` sets the deadline for `/compact --wait --timeout` and the following turn-end wait.
Only a new compaction record in the transcript proves completion.
If the deadline expires, the worker checks for this record before it reports a timeout.
A complete compaction record lets the worker send the assignment.
The child then sends the prompt and confirms its transcript receipt within `WORKER_SETTLE_SECONDS`.
It writes its output to `compact-<dispatch id>.log` in the run folder.
A compaction failure sends no prompt and clears only matching `/compact` composer text.
A detached delivery failure ends the pass with `error` and reports through the existing completion hook.
Unit pause, move, and end use staff membership.
Usage reports compare checkpointed phases through the usage store.

## Pane submission and completion

`assign.sh` returns after successful prompt delivery or an accepted detached compaction. Both return exit 0.
`assign.sh` and `assign.py` accept `--reply-file <absolute path>` for the reply output.
Text, relative paths, and empty values exit 2 before dispatch.
The old `--summary` option exits 2 and names `--reply-file`.
Both reply writes and dispatch reply paths use the checked `Arguments.reply_file` value.
Worker lists and automatic selection check all saved passes for each worker.
An older pass can hold the worker while it waits for `DONE`.
Automatic selection skips a held worker even if its pane is idle.
If all reusable panes are held, the selector can choose a free worker name.
The launcher checks the selected worker under its lock before it records a new pass or starts compaction.
Detached run records identify the owner by staff ID.
Legacy records without a staff ID identify the owner by worker name.
If a live supervisor names a missing or unreadable record, the check holds the workers that share its run folder.
Explicit assignments check the current pass.
The early return prints `compaction runs detached; the prompt follows` with the worker name.
The pass stays active while the child completes delivery.
An assignment with no compaction due keeps its attached delivery path.
Codex dispatch also requires the prompt in its transcript before it reports submission.
Codex dispatch waits up to `WORKER_SETTLE_SECONDS` for the receipt.
An unproved Codex prompt returns uncertain when this bound passes or the process exits.
A recorded prompt remains submitted after a process exit.
Claude can record the prompt later.
A failed delivery command returns an error.
The launcher copies captured worker output to stderr on a nonzero exit.
Assignment recovery prints the reason if pending pane cleanup prevents submission.
The transcript prevents replay.
A matching final transcript reply proves completion.
A matching dispatch prompt also permits completion from the hook's own DONE reply before the final transcript record appears.
The native hook sends its payload to a separate handler through stdin and returns.
The child reads stdin before it applies the completion rule.
The payload stays outside the child argument list.
The handler runs once outside the terminal session.
The pass retains its id, launcher arguments, reply path, and completion actions.
The director ends its turn after the assigned synchronous review.
The turn hook sends the director the worker, phase, outcome, and pass id.
The notice includes the reply path when the reply has text.
The director reads that reply when the message resumes it.

`scripts/herdr/turn_end.py` serves both families.
Codex uses the global `Stop` handler installed through `scripts/hooks/codex_hooks.py`.
Codex worker launch lines contain no `-c notify` argument.
Claude receives an asynchronous `Stop` command through launch-line `--settings` JSON.
The launcher changes no account settings file.
The hook finds the worker through its native conversation id.
It follows a per-worker link to the dispatch's existing state file.
The link lives in `dispatches/<staff id>` beside the staff record.
This lookup includes dispatches outside the run root.
Older workers without a link use the held-run lookup.
A session without a worker pass changes nothing.
A later event retries unfinished completion steps for a completed pass.
The hook reads the worker's run inside the worker lock.
A new assign finishes the unfinished completion before it replaces the link.
That notice names its own pass and says it is a late result. The pane stays for the new assign.
The late step does not retire the worker, and it keeps a hold that the new assign also owns.
If notice delivery fails, the assign returns uncertain and reports the pending notice.
A replacement for a failed pass starts after the hook delivers the pending notice.
The recorder gives the replacement a new pass id even when the assignment inputs match.

A dispatch retry matches the slot, phase, role, prompt path, launcher arguments, and saved prompt content digest.
`assign.sh` prepares a separate prompt for each call.
It removes the prompt when the recorder confirms that no pass uses it.
The worker writes the numbered paths manifest at admission before it submits the prompt.
A matching retry checks the receipt before it validates and restores the recorded prompt under the recorder lock.
The pass records the digest before submission.
Changed content at the same prompt path starts a new pass after completion.
With an active pass, changed content returns busy and names that pass without printing `submitted`.
This refusal applies when the launcher arguments change.
The recorder repeats the content check under its lock before it changes the pass.
The intentional second review stage keeps its existing pass across its separate prompt path.
An identical rerun of a completed dispatch uses the saved pass notice.
It saves the reply without typing another prompt.
This rule also applies to a completed managed Astra review.
The saved completion includes the reason and pane for a close anomaly.
A completed rerun sets its status file to `done` before it checks the notice.
A replay of a settled hook event sends no second message.
This includes a legacy status replay that arrives after a different status.
Each completion notice ends its first line with `; pass <dispatch instance id>`.
An explicit rerun can retry a pending notice with its original delivery key.
An explicit rerun leaves settled notices alone.
Each notice uses the worker's native identity and name, including a notice from the director's rerun.
The saved completion supplies the sender identity for a retired managed Astra reviewer.
The sender identity remains the same after a session change and retirement.
Each status transition gets a sequence ID and a delivery key.
A repeated native turn ID uses its existing event.
Without a native turn ID, an unchanged status uses the current event.
A transition back to an earlier status gets a new event.
The pass keeps notices separately from the saved reply.
Recovery can replace the saved reply after the notice settles.
Waiting notices add `; runs <run ids>` to the first line.
The run ids include received results that still await a worker reply.
An empty turn reply writes no reply file and adds no `; reply <path>` text.

A new Codex worker has no resume argument.
Its pane session report supplies the first native identity.
`herdr.foreground_pane_owner` proves the owner for pane actions, retained launches, and status reports.
`_pane_sessions` reads identities from that proof.
Resume uses pane metadata to find candidates when the process scan is incomplete.
It requires the proved conversation before it reports an existing Codex session.
The roster uses the proved Codex PID for the conversation, account, and folder.
Unit reports join the proved owner to staff.
Inherited marks and retained launches can identify which unit has an unknown pane.
The report records an anomaly when no eligible unit can receive an unknown pane.
The report also records failures to list panes.
A unit without owner proof remains unknown when either anomaly leaves its pane uncertain.
A registry entry supplies activity only for a proved pane owner or a confirmed non-pane process.
An inherited identity on a proved shell can associate a stopped unit with its pane.
Session commands require a matching foreground owner before they read or route to a pane.
The unit pane selector returns exit 3 without an address when the owner is unknown.
The Claude SessionStart hook checks the nearest Claude ancestor against the foreground PID before either write.
A worker roster needs a proved open pane before it uses a Claude registry entry for status and routing.
A proved Codex PID can supply its account while its conversation address stays unknown.
`process_placement` distinguishes a verified pane, a confirmed non-pane process, and unknown placement.
The staff thread reader uses resume arguments only for a confirmed non-pane process.
`pane_activity` checks whether the pane runs a program or sits at its shell.
Program names from occupancy and startup checks are diagnostics.
The list shows `unknown` for an unproved owner, including a live Node descendant with unreadable arguments.
A shell with an inherited thread id has no foreground agent.
The Linux placement scan and the owner check share each process state in the report, including failed reads.
An input check reads fresh process state.
During an input check, the ancestry read supplies each descendant's exit state.
The terminal check uses that state to skip zombie processes.
Codex launch capture refreshes the process state and uses one foreground proof.
Launch diagnostics omit argument tokens.
The function requires one foreground Codex process with known arguments.
All other foreground processes must descend from that Codex process.
A Claude child cannot claim the Codex pane.
If a foreground Codex process has no arguments, the pane owner stays unproved.
If the identity remains unknown, the pane notice lists the foreground program names.
After its first turn, the launcher clears the cached pane list before it checks the published worker.
This refresh includes the pane session report and the process evidence within an observation snapshot.
An incomplete final scan names the state, the scan reason, and membership in the parent's tabs.
Every worker operation takes the name lock before the fixed-id lock.
The launcher holds both locks from publication through dispatch recording and submission.
The recorder validates the selected slot, phase, and predecessor before it records a retry, stage continuation, or new assignment.
The first `ready` hook waits for these locks before it checks the dispatch.
An ignored turn can close the pane when no dispatch holds the worker.
A matching DONE for an ended pass follows the late DONE rule if the hook has no completion for that reply.
The hook saves the late reply in a file named for the pass.
It sends one notice with the earlier pass state and reply path.
The hook records delivery before it tries to close the pane.
A later matching DONE updates the saved reply even after a completion action fails.
The hook preserves the action error and notice record.

`worker.dispatch_receipt` reads the transcript and detached run evidence.
A live detached run holds DONE.
If the transcript omits the result, completion requires proof that delivery preceded the matching DONE.
That completion records an anomaly.
A later assistant message preserves the matching DONE.
Delivery after DONE or unknown delivery order requires a new matching reply.
A reply without the matching DONE keeps the earlier DONE and the pending reply requirement.
The hook, restore, assign, and close keep the pane while that requirement remains.
A pending run reply keeps the pass active without another prompt.
Run events preserve the last worker reply while a result awaits a reply.
A live run reports `waiting on runs`, including when an earlier result awaits a reply.
The sender submits a detached result to an idle worker pane.
The sender confirms delivery only when the transcript contains the run ID.
The run record retains a result until the transcript confirms delivery or the holder receives it.
The turn hook submits a retained result after the pane becomes idle.
A run event uses the stable staff ID to find the worker's current session.
A session change during the hook keeps the run event pending.
A worker puts its question in its reply and ends the turn.
The turn hook sends that reply to the director.
An unexpected screen prompt ends no turn, and the director finds it only when it reads the pane.
A turn without DONE reports `no DONE` when no pending work explains it.
The notice includes the worker's last reply and leaves the pass active.
A turn without an assistant reply reports `no DONE` when the transcript records compaction after the accepted dispatch prompt.
An empty turn without that compaction evidence stays ignored.
Resubmit that work with `worker.py answer <name> --text "Read the assigned prompt and do the work."`.
`answer` takes the same worker lock as `assign`.
It requires an active pass, a saved delivery, and transcript proof that the worker accepted the delivery.
If compaction holds the lock or the pass lacks proof, `answer` refuses to send input.
A matching DONE in the transcript blocks input before the completion hook updates the pass.
Use `assign.sh` for new work.
A blocked Codex question receives only the supplied answer text.
Other answers include the saved completion ID in the DONE instruction.
The normal hook handles DONE for the same pass.
If the reply ends with a different DONE ID, the no DONE notice names both IDs.
The pass stays active and the pane stays open until the reply contains the matching DONE.
A saved stage ID controls pane closure and reuse.
The reply on disk must match the completed receipt.
If the report path resolves to the reply path, the hook keeps the raw reply there.
If the caller requests a distinct report path, the hook writes the report there.
The report includes the heading when the assignment has a summary.
The completion hook saves the reply and records the pending notice for the exact pass.
Live detached work delays normal and late DONE notices.
Restart restores a selected live worker before it removes the worker from the restart record.
This rule also applies when restart cannot confirm that a stop ended the live process.
An incomplete restore keeps the worker in the restart record.
The hook runs saved completion actions and records delivery before it closes the pane.
A failed delivery keeps the notice pending and the pane open.
The supervisor loads the completion handler before it starts the detached command.
Deferred completion imports use the same source from before the command.
A prompt launch failure permits another input attempt.
If the prompt may have sent input, replay reads the transcript receipt before it continues.
Unreadable completion evidence keeps the run completion pending.
The result receipt alone does not clear the run completion flag.
The next detached start in the run directory, worker restore, or the reap command retries a pending completion event.
The hook, restore, and assignment use the same completion recovery under the worker lock.
Recovery continues a saved completion or checks for matching DONE in the transcript.
The restart prefilter sends each saved worker to restore, including workers with no active pass.
Unavailable completion evidence keeps the restart entry.
Pane closure alone keeps the restart entry.
Completion removes the entry after it saves the final result.
Restore reports incomplete recovery if pane closure fails.
Restore reports the worker name and reason when completion recovery keeps its pane.
The next restore retries closure without sending the completion notice again.
Each pass keeps an append-only notice record in its run directory.
The record holds each event ID, notice, route, and settlement.
The record retains event IDs so a retry can recognize a duplicate.
The first notice for a pass with a legacy stamp imports the stamp as one settled event.
The record keeps the stamp for that status even if a different status arrives first.
The progress recorder keeps the stopped pass receipt for late reply recovery.
The hook saves pending diagnostics in the completion checkpoint before it writes the notice record.
The hook sets `recording_done` after all pending diagnostics are durable in the notice record.
If the notice write fails, the next hook publishes the saved diagnostics.
A new assignment can use an open, stopped, or closed idle worker while the old notice remains pending.
The new assignment still requires valid completion evidence and safe pane reuse.
A new assignment can append notices without reading the notice record or taking a message lock.
A later hook or reap retries the pending notices with their original delivery keys.
The stopped pass can retry its diagnostics while it waits for a matching DONE.
The retry checks for a director receipt with the same delivery key.
A matching receipt settles the notice without a send.
A receipt lookup failure leaves the notice pending.
A repeat-guard hold settles the notice after the guard queues a notice for the owner.
Peer delivery preserves the repeat-guard hold reason. Other holds leave the notice pending.
The hook marks `scratch.worker_task(row.id)` ended before it closes the pane.
The worker's scratch folders remain for manual removal.
A failed cleanup adds a diagnostic to the director notice and permits pane closure.
The hook saves each retained scratch path and reason separately from the notice text.
Each send checks the saved paths and omits confirmed missing folders.
The notice omits legacy scratch details that have only text.
A failure to read or check the saved paths leaves the notice pending without a send.
`progress_history.complete_dispatch` records both completed passes and failed managed Astra review passes.
It matches the slot and dispatch instance id before it records an outcome.
A refused launcher retry retains the existing pass and its run, including a run with one worker.
When DONE arrives during launcher error recovery, the hook finishes the saved actions.
The director's notice names the earlier state.
The recorder preserves the status of each ended pass.
A DONE that arrives after recovery has finished sends a late notice.
`astra_review.py --read` reads a reply when the saved completion names its file and the final DONE matches the dispatch.
It reads the reply file once and prints the text from that read after validation.
The check includes second-stage IDs and late reply files.
The hook saves the completion before it sends the notice.
The reply is ready before the hook stamps notice delivery, including when completion actions report an error.
The check reads passes in the current recorder state.
The review trial uses the latest completion duration for each pass.
Named adversary and contract reviews use the same completion hook.
Their DONE reply records `done` and sends one notice.
The hook also saves an assigned style review result.
A close anomaly retains the completed outcome and sends a separate diagnostic to the director.
A held reviewer role remains held after submission. Completion ends that reviewer and releases the role.
The close matches the pane's terminal identity and closes it without a foreground-process wait.
Uncertain ownership or failed closure produces a retained-pane diagnostic.

Event producers call `handle(WorkerEvent(thread_id, EventKind.RUN_FINISHED))` after a run record reaches `finished`.
The final diff event uses `EventKind.STAGE_READY` for a review with two stages.
`detached_runs.py` calls the handler after supervisor completion and orphan completion.
The director sends the final-diff event with `turn_end.py stage-ready <worker>`.

### Hook input sources

The [Codex Stop reference](https://learn.chatgpt.com/docs/hooks#stop) defines JSON on stdin.
Its fields include `session_id`, `turn_id`, `cwd`, `hook_event_name`, `stop_hook_active`, `transcript_path`, `model`, and `last_assistant_message`.
A null `last_assistant_message` becomes an empty reply.
Saved launch records can retain legacy `notify` arguments.
The handler ignores their `agent-turn-complete` payloads.
The global Stop hook supplies completion for those workers.
The [Claude Stop reference](https://code.claude.com/docs/en/hooks#stop) defines JSON on stdin.
Its fields include `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `stop_hook_active`, and `last_assistant_message`.
The hook uses the native id to select the worker.
The hook applies the completion rule described above.

### Stop hook deployment

The director installs the hook after deployment.
Each registered Codex home must trust the shared `hooks.json` handlers.

1. Set `CODEX_HOME` to the account home and run `scripts/lib/py scripts/hooks/codex_hooks.py install`.
2. Run `scripts/lib/py scripts/hooks/codex_hooks.py check` with the same `CODEX_HOME`.

The installer reports the `turn-end` handler as trusted.
A worker needs no new launch-line setting to use the installed Stop hook.
Every Codex session runs the handler at each turn end. A non-worker turn exits as ignored and adds one line to `~/.local/state/herdr/turn-end-ignored.jsonl`.
A Codex process that started before the Stop hook was installed (2026-11-09 08:27 EST) runs no Stop hook. Measured: such a process finished turns after that time and added no line.
Close that worker's pane. Its next assign starts a new process that has the hook.
A pane launched with `-c notify` sends a payload that the handler saves as an ignored line with an empty thread.
Why: legacy notify passed the payload as one argument, and Linux refuses one argument over 128 KiB. Long-lived threads lost turn ends that way (2026-11-09).

## Unit code reviewers

Unit reviewers use read-only access.
Writers retain their writing roles.
Concurrent review assignments use separate worker identities.
Titles use the current staff placement and assignment role.
`workers.shown_name` numbers unit workers in their parent's staff order.

## How it works

### Staff identity and admission

A worker has one fixed staff id, name, parent and worktree.
Its `role` records its last dispatch. Its `home` records the absolute account folder.
`KnownWorkerRole | WorkerRoleUnknown` and `SessionHome | HomeUnknown` represent these values.
Missing keys mean unknown. Serialization omits missing values.
An invalid value remains unknown and retains its raw JSON value.
`progress_history.begin_dispatch` uses `staff.set_worker_role`.
Pane publication uses `staff.set_home`.
`current_session` selects its Claude session or Codex thread.
A family change preserves the fixed staff id.
The [staff design](staff-accessors.md#staff-identity-and-backends) defines the row fields and backend evidence.

`worker_admission.admit(name, run, *, reference="")` returns `WorkerStartAllowed` before a backend starts.
Its slot is `NewWorkerSlot` or `ExistingWorkerSlot(row)`.
`PublishedWorker(row, handoff)` means that staff publication succeeds.
Only successful publication permits successful dispatch.
`assign.py` completes admission before it records the implementation pass.
A publication failure runs the caller's cleanup callback and reports the native id.
The Claude coordinator tries to stop an unpublished session.
A failed stop reports `cleanup unproved`.
A failed pane admission closes only the new pane that this call created.
A reviewer hold failure or a failure before dispatch submission ends only the worker pane that this call created.
The same cleanup applies when worker restore fails before it submits its resume prompt.
Cleanup closes only the saved pane and terminal.
The close target is the worker's current session for both Claude and Codex.
It retires a staff row only if this launch created the row.
A resumed worker keeps its existing row after cleanup.
An existing pane keeps its pane and staff row.
A failed close prints `anomaly: <pane>: cleanup failed: <reason>` and preserves the original failure code.
An incomplete start also closes its new pane.
The start result identifies a new pane and its saved terminal.
Dispatch reads staff rows before start and adds no pane scan for cleanup.
A target refusal before prompt delivery runs cleanup.
Cleanup stops only after target checks pass and the prompt can reach the agent.
Restore cancels cleanup through the callback in `Herdr.prompt`.
For an existing pane, the launcher quits its own agent and keeps the shell.
Both paths compare the current terminal and process with the identities saved after launch.
A replacement process remains open. Cleanup prints one anomaly with the pane id.

A new slot takes the parent/name lock before the row lock.
If a sibling split fails with `pane_not_found`, the start retries once from the parent pane.
If all siblings disappear before the layout read, the start uses the parent pane when the layout confirms it.
Other errors and a failed parent split keep the current failure path.
An existing slot takes the row lock.
`worker_operation` locks the admitted row.
For a new slot, it first checks membership under the parent/name lock.
Admission and publication use the same fixed staff reference.
The held `workers.resolve` loop continues candidate selection after `AdmissionRefused`.

### Family handoff and retry

`switch_worker` extracts source context, releases the source, creates the destination and publishes its native identity.
Both families use this sequence.
`FamilyHandoff` has `NoFamilyHandoff`, `TranscriptHandoff(extract, source)` and `TranscriptUnavailable(reason)` variants.
The extract contains assignments, final replies and changed paths.
It excludes tool request bodies and tool output.
A missing transcript prints an anomaly and permits dispatch without a handoff.
`TornTranscriptRecord` permits extraction from the valid lines of a damaged transcript.

The source result is `SourceReleased`, `SourceUnresolved(reason)` or `SourceReleaseUnknown(reason)`.
`stop_resume.release_source(row, backend)` preserves each stop reason and supplies the next action.
Native evidence must prove that the source stops or is absent.
Unknown evidence preserves the source and prevents the family change.
`StopUnproved` proves neither absence nor continued execution.
The operation checks the fixed staff id and current session before it stops a source.

An existing worker's default switch request is `sha256(f"{staff id}:{destination family}")`.
`assignment_id(prompt)` hashes the resolved prompt path, modification time and contents.
Retries keep the original prompt file as the assignment identity.
`DispatchRecord(assignment_id, state)` uses these states:

| State | Stored value | Meaning |
| --- | --- | --- |
| `NO_RECORD` | empty text | No known dispatch state. |
| `CREATION_INTENT_RECORDED` | `prepared` | The request precedes backend creation. |
| `DISPATCH_IN_FLIGHT` | `sending` | Delivery starts, but acceptance is uncertain. |
| `ASSIGNMENT_ACCEPTED` | `accepted` | The destination accepts the assignment. |

`DispatchOwner.TURN_SUBMISSION` delegates acceptance recording to turn submission.
`record_dispatch` checks its expected request under the exclusive record lock.
It preserves a request that another operation replaces before lock acquisition.
`dispatch_record(run, name, *, report_anomaly=True)` reports an unknown stored state and uses `NO_RECORD`.
The diagnostic is `anomaly: <name>: unknown dispatch_state <value>; using NO_RECORD`.
This anomaly does not refuse admission.
`report_anomaly=False` suppresses the diagnostic on repeated reads.

`dispatch_pending(run, name, prompt)` matches the assignment id and a state other than `ASSIGNMENT_ACCEPTED`.
An unreadable record prints one anomaly and returns false.
A family change still reaches the switch operation.
A retry reuses the published destination and places the saved extract before the assignment.
An accepted retry prints `unchanged`.
The launcher retains its original prompt identity and its dispatch record.
`review.sh` has no pending dispatch check and does not restore a saved extract.

### Claude coordinator

`assign.py.execute_herdr` sends normal assignments through `worker.py` and native worker panes.
The retained `scripts/direct/claude_worker.py` API runs Claude coordinator steps within its caller's process.
`assign_claude._run` calls its functions directly.
The module has no command line or shell protocol.
It reads `CLAUDE_WORKER_REGISTER_WAIT_SECS` before it stops a source.
It verifies the destination against the running Claude registry before staff publication.
It records the role before delivery.
Delivery starts one Python child through `scripts/lib/py`.
The child calls `send.py --to session:<id> --from launcher --file <prompt>`.
`CLAUDE_WORKER_SEND_SCRIPT` selects the delivery script.

`assign_claude.run(ClaudeLaunch)` provides the retained coordinator API and its integration fixture entry.
`ClaudeLaunch.worker` selects `NewClaudeWorker` or `ClaudeFollowUp`.
`claude_worker.open_worker` and `follow_up` take a `ClaudeTurn`.
The launch returns `Delivered(native_id, turns_before, launch_environment)`, `Unchanged`, or `NotDelivered(code)`.
`TurnsNotCounted` represents a new session without an earlier turn count.
`wait_for_turn` reads Claude agent listings and logs in the same process.
`CLAUDE_WORKER_POLL_SECS` sets its interval; `UNIT_WORKER_POLL_SECS` overrides it.
One `nix_env.ensure` supplies the settings and start environment.
The account step uses `accounts.account_for_unscoped_launch`.
The banner's `backgrounded` line supplies the native id.
The registry lookup resolves that id within `CLAUDE_WORKER_REGISTER_WAIT_SECS`.
`assign_claude._run` records published workers in `finally` before it starts the heartbeat watch.
The watch follows the coordinator process during the wait.
A signal after the banner stops an unpublished session through `claude stop`.
An unreadable log supplies a fallback summary.
A transcript record without a message object adds no turn.
The launch uses exit 127 for a missing binary and exit 2 for refusal or failed delivery.
Other launch failures use exit 1; TERM, INT, and HUP use 143, 130, and 129.

A retry that finds its original user prompt in the transcript records acceptance without another send.
A complete transcript without that prompt permits delivery.
A damaged transcript without that prompt refuses uncertain delivery.
The refusal says `Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused`.
A missing transcript keeps the published session and places the saved handoff before the assignment.

### Launcher claims

`worker_state.publish_worker_claim` writes `impl_claim_<slot>.json` atomically.
The record holds `pid`, `worker`, and the process start in `started` when available.
An unreadable start prints an anomaly and leaves `started` absent.
`read_worker_claim` returns `HeldWorkerClaim(pid)` when the start matches or the current start cannot be read.
It returns `EndedWorkerClaim` for a gone process, zombie, changed start, or missing recorded start.
An absent or malformed record, or a record without a positive pid, gives `NoWorkerClaim`.
Claim release compares the recorded pid with the launcher pid.

### Account selection

The [account design](accounts.md) defines the selected workers account and its authority on natedev.
Both machines resolve this selection through `role_map.py`.
The selected account is separate from a worker's thread and role.
Account moves preserve the thread while the native worker operation changes its home.
Claude assignment fallback does not replace the selected Codex account.

### Launchers

The launcher records the fixed worker id before it reports successful dispatch.
Role and lens records describe the current assignment.
A later assignment retains the worker identity and selects its current native session.
A prompt names its expected HEAD and asks the worker to read the current tree.
Review prompts retain the Three Gods sentence and the Type Design Contract.

A failed launch keeps the run open.
The launcher records failure only for the pass that it started.
A retry without `--session-dir` uses the current run from the run pointer or the environment.
The director records `finish-run` and calls `end_session.sh` when the run ends.
The phase reservation stays until a checkpoint releases it.
`prepare_session.sh` and `end_session.sh` select `CODEX_THREAD_ID` before `CLAUDE_CODE_SESSION_ID`, as `staff.runtime_id()` does.

### Choosing a worker for a review

Each review lens uses the unit's read-only reviewer through `assign.sh --role review`.
Concurrent reviews use distinct workers.
Only the adversary lens starts early.
The director retains its dispatch handle for Stage 2 and cancellation.
Closure review uses the review role without a lens.
`progress_history.py` gives each extra worker its own `Agent N` number after `impl` and `test`.
The first recorded pass or board handoff establishes the order.
The original `review` worker appears only when a displayed stage assigns it.

### Resume messages

`resume_message` builds one prompt for resumed conversations.
`ResumeWorkers(results, cwd, panes)` combines child resume results with pane worker states.
`inventory.worker_resumes` reads direct Claude worker children and preserves their saved launcher arguments.
Each child's `KnownWorkerRole` supplies its current role.
An unknown staff role produces `Unavailable`; the reader does not use dated assignments as a fallback.
Pane observations supply native worker resume state.
`AccountMove` names the source and destination accounts.
A busy recipient gets only its `own_pass` continuation.
An idle worker row receives no prompt.
A director can receive the list of busy and idle worker children.
Restore reaps detached orphans before it resumes workers.
A full restore reaps once per held run folder.
A limited restore reaps once per selected worker in each held run folder.
It preserves excluded workers and workers with unknown pane evidence.
It prints each reaped run id.
Restore reopens only workers with active dispatches.
It reads each worker's dispatch record and transcript once to select the resume step.
An accepted dispatch gets a continuation prompt only when the worker is idle.
Restore holds the dispatch lock across the record read, receipt read, and prompt delivery.
An unsubmitted dispatch keeps the saved assign command as its step.
Restore, named resume, and director notes use the same receipt rule.
Director notes keep the saved prompt path and reuse shared dispatch history reads.
A dispatch with a DONE line or a finished record gets no resume note or step.
Later chat does not reopen a completed dispatch.
The restart shares its run index and snapshots.
Idle workers stay closed until their next assign.
Account moves must preserve closed idle workers.

### Cleanup

Run end acts on every run that the ending director holds.
It leaves another director's run unchanged.
The marker's run precedes the other runs under the run roots.
`run_folders` excludes each root's `active` directory.
A failed scan still retains the marker's run.

`end_session.sh` first removes held design bindings without ending their workers.
A failed unbind prevents the later steps.
It then removes phase workers and tracked run scratch.
Scratch removal keeps a checkout that contains a live detached command's working directory.
It does not follow directory links or remove run logs, histories or the run directory.
The later steps continue after a failure.
Run end does not start a sweep.
It removes only the ending session's active marker.
Another marker's live or unknown owner prevents removal of its former run folder.

Staff supplies worker names, parents, worktrees, and backend identities.
A rename or transfer preserves the fixed staff references.
`rename.py` changes the unit row and repairs its session and display names.
A changed unit name changes matching folder and branch suffixes.
`--to-branch <name>` sets an explicit branch.
Every unit branch change pushes the target to origin before sessions resume.
Equal old and new unit names keep the folder and staff name.
See [production rename](production-rename.md) for unit branch moves.

### Measurement: `scripts/whoami/worker_usage.py`

`worker_usage.py` reads the usage store through `usage_report.open_read_store` without changing it.
It reads no rollout, SSH, git or account readings file.
The collector stores requests, compactions, edits, checkpoints, account readings and role changes from both machines.
The [usage design](usage-ledger.md) defines the worker report, dated model weights and source states.
The report compares before and after windows, or daily windows around `DEFAULT_T_LIVE`.
It reports usage per checkpointed phase and names incomplete source evidence.
Missing readings, credits or checkpoints produce explicit unavailable results.

## Lasting roles

A design has one designer slot and one reviewer slot.
Open roles retain fixed staff ids across rounds, bindings, restarts and account moves.
The worker row's parent supplies its holder.
`worker_parent` resolves that parent from the supplied staff snapshot.
A rename never selects a replacement through the worker's former name.

### Store and types

`design_roles.py` stores each design under `DESIGN_ROLES_STATE`.
The default is `~/.local/state/astra/design-roles/<design id>/`.
`design.json` stores placement, checkout ownership, document, area, binding, role slots and assignment claims.
The record has no `holder` field.
`prompts/`, `replies/` and `pending/<role>/` retain work and queued messages.
`transition.json` retains an incomplete lifecycle operation.
The binding is `DesignUnbound` or `DesignBoundToPhase(run_directory, phase, repository, output_paths)`.
`HeldDesign` and `DesignBoundToPhase` have a keyword-only `checkout` field.
Its value is `UnitCheckout(owner)` or `IndependentCheckout()`, with the independent checkout as the default.
An explicitly unit-owned checkout stores `repository_owner: <unit staff id>` in the design and binding.
The saved owner supplies the checkout path; matching paths never establish ownership.
An owned record stores no `repository` path.
Its `document` is relative to the checkout.
`design_repository` and `binding_repository` resolve the owner's current staff worktree.
`read_design` returns the current absolute repository and document paths.
A worktree move therefore changes the resolved document path without rewriting the record.
An independent checkout stores its absolute `repository` and `document` paths.

The lifecycle is `RoleNotOpened`, `RoleOpen`, `RolePausedForMove`, `RoleNeedsRecovery` or `RoleEnded`.
Open, paused and recovery states retain the fixed staff id.
Ending an opened role retains its staff id.
Ending an uncertain creation can record an empty id when no worker exists.
`HeldWorker.thread_id` derives the current thread from staff.
A role with a worker staff id stores no `name`.
`instance_name` reads the current worker name for prompts, dispatch, recovery, end, and role lists.
A role without a worker id stores its `name` as the creation claim.
The roster's `_durable_roles` and `worker_shown_name` use `instance_name` and `design_repository` for display.
An absent checkout owner raises `RoleRefused`.
The roster reports that record as a warning and continues with other records.
`ReadOnlyReviewer` and `PhaseBoundDesigner` define the access policy.
`_read_designs` reports each skipped damaged record once per operation.
It refuses a matching active design that needs recovery.
`list` returns damaged record problems alongside readable roles.

`DamagedDesignRecord` identifies the record path and damaged field.
Record scans decide whether matching damage prevents the requested operation.
`held_document` resolves owned document paths before it matches damaged records.
Writes validate the complete design before publication.
An omitted binding repository uses the authority checkout; an empty repository refuses.
`rebind --unbind` or a complete replacement binding can repair an empty repository under the generation lock.
Unrelated damaged records produce one warning per operation and do not stop other designs.
`unbind-run` refuses matching damage, including damage without a live role.
Plain `list` keeps diagnostics on stderr; JSON retains `problems`.
An identical `rebind` retry completes only after launch context and census publication agree.

### Operations

`open` reserves placement, checks the binding and records creation intent before launch.
An uncertain start retains its claim.
`resume` requires an open or paused role, a valid binding and no active assignment claim.
It saves the headed prompt, checks the current lifecycle and generation, then records the claim before dispatch.
`recover` proves the recorded worker idle and clears its uncertain claim without creating a replacement.
`rebind` requires a reason and the expected generation.
It records a transition and advances the authority generation before it publishes a new binding, checkout or document.
Rebind and checkout moves preserve the recorded checkout owner for any caller.
Both operations preserve the binding's checkout reference.
Each operation refreshes the census and clears the transition.
An interrupted checkout move resumes its saved transition before applying any destination shortcut.
The retry uses the saved target and generation after the staff worktree moves.
`end` confirms release before it records `RoleEnded`.
A failed end retains its transition record for retry.
`list` shows an active role with an incomplete transition as `RoleNeedsRecovery`.
`unbind-run` removes the binding without ending the role.
`list` reports roles and problems without a network request for a local read.
An ended identity cannot reopen.
`[roles] new_open=false` in `config/design_roles.conf` preserves existing roles and refuses new ones.

A design role uses `worker.py` operations.
`worker.py list all --json` gives each retained worker row `name`, `id`, `current_session`, `state`, `role` and `activity`.
The staff row supplies `role` and `home`. The `home` folder name supplies the account label.
`work` is `idle`, `busy` or `unknown`, from `worker_panes.worker_state`.
An unknown role uses `WorkerRoleUnavailable`. An unknown home uses `AccountUnavailable` and omits `home`.
`worker.py list all --json --all-parents` reads workers for every manager and adds each parent's name as `manager`.
It needs no caller row and makes no process, pane, or thread read.
Its `state` uses the saved dispatch status or staff hold.
Its `activity` uses the saved pass activity, then its status.
History lookup resolves fixed manager ids through the staff record, even after a session change.
History lookup reads the last 64 KiB of each file, plus one byte to skip a leading partial line.
History-only dispatches outside this window use the same unknown fields as missing dispatches.
It skips invalid lines and decodes only wanted worker events.
Listing reads no role map or account history. Missing dispatch history does not change the staff role or home.
Per-manager listing keeps its live observations.

History lookup resolves fixed manager ids through staff, even after a session change.
It merges events from newest to oldest across files.
A file search stops when newer dispatches supply each worker's state or all remaining events precede each worker's creation.
A UUIDv7 staff id supplies its first conversation's creation time.
Older id formats have no creation bound.
The reader skips invalid lines and keeps history events absent from the current pass slots.
Stored account history resolves only the unit group and reads its history once.

The authority host comes from `[ownership]` in `config/design_roles.conf`.
The configured authority host owns placement.
Both roles remain on the design's recorded host.
Host transfer is unsupported.
Repository registration requires each host's checkout mapping.
Supply every host mapping on the first registration.
Use `register_repository` for multiple mappings.
Use `register --repository-owner <unit staff id>` for a unit-owned checkout.
The library accepts `UnitCheckout(owner)` for the same mapping.
Path-only registration stays independent, including when its path matches a unit's worktree.
`ConfiguredCheckout(path)` also records an independent mapping.
Repeated registration must keep the same mappings.
Placement uses a hash of the repository id and relative document path.
The authority supports registration, reservation, lookup, generation advance, checkout changes, and aliases.
An authority error stops the local lifecycle operation without changing placement.
Renames and transfers retain worker staff ids.
Readers resolve the worker's current parent from staff.

### Dispatch and messages

`assign.pane_policy` selects designer and reviewer settings through `worker_panes.launch_policy` and the shared registry.
A designer requires a phase binding and authorized output paths.
A reviewer uses read-only access.
`resume` saves one assignment claim and includes pending message ids in the prompt.
Before recording the claim, `resume` rechecks the binding generation and fixed staff lifecycle.
A pending binding transition prevents dispatch.
The claim permits one `dispatch_turn` call through `assign.sh --target durable-design-role`.
Success removes only the included pending messages.
Failure retains pending input and records recovery state.
A role's `send` queues text for its next authorized assignment without waking the role.
An unbound design opens `astra-review` only when that phase is closed.
Each `dispatch_turn` records `finish-phase` after assign returns, with `completed` or `error`.
A failed recording after refusal preserves the dispatch refusal text.
Ending an active unbound design records `finish-run --status completed` under the lock.
A phase-bound design uses the unit run and records no separate design phase.
The one-shot managed review closes its phase and run in `assign.py` after worker cleanup.
`worker.dispatch` owns successful pass completion; `assign.py` records pass failure only.
Detached worker results use `detached_runs.deliver` and `send.deliver_worker_text`.
A unit director or showrunner gives `detached_runs.py start` its own folder with `--session-dir`.
The run record saves the caller's staff ID. The supervisor starts in a separate process session.
`send.py` uses the saved staff ID to deliver the result to the current session or queue it during a move.
After the command ends, the supervisor starts `detached_runs.py deliver <record>` in a new Python process.
This process imports the current delivery code and saves the delivery outcome in the run record.
The supervisor records the command exit before it starts delivery.
After the first delivery attempt, it marks the run finished and keeps the latest delivery outcome.
A failed push saves the send error as pending delivery.
The finished run leaves the live-run limit and keeps its record and log.
The start lock serializes orphan scans.
The reaper saves an ended orphan as finished before it makes one delivery attempt.
The reaper alerts the director if a finished run still has a failed push and its supervisor is gone.
Before the reaper alerts for an unconfirmed push, it checks for a receipt.
A failed turn-end replay waits for the next start or launcher reap to send its alert.
A deferred replay keeps a saved push failure.
The supervisor uses the retry count and delay in `detached_runs.conf` after it releases the completion lock.
The supervisor sends again only when the previous attempt sent nothing.
If a pane or relay does not confirm delivery, the supervisor checks only for a receipt.
After the last retry, the supervisor sends the delivery state to the director.
The alert holds the delivery lock through submission and records the outcome after sender acceptance or a logged failure.
The next reap can recover an alert if the process exits before the record saves its outcome.
A rare exit or a retirement can repeat the alert once. It never repeats the result.
The alert for an unconfirmed push says that the result may have arrived.
If an alert fails, the sender prints one anomaly and keeps the result pending.
The command runs in its own session.
On natedev, the launcher starts the command scope in `builds-detached-<id>.slice`.
The slice name uses the run ID without dashes.
Each run slice is under `builds-detached.slice`, which is under `builds.slice`.
The limits and anonymous memory totals of `builds.slice` include the nested scopes.
The queue charges each unowned scope inside a run slice.
The launcher indexes the run record and saves the slice identity before it starts the command.
It exports `DETACHED_RUN_SLICE` to the command.
The index lets the live-run reader find the slice after its supervisor exits.
The reader also checks records named by live supervisors and legacy stop markers.
It matches older records without an index to their slice names.
An index without its record gives a read error.
The supervisor or reaper removes the slice index after it saves the finished record.
The scope launchers use the run slice when the variable is set.
Transient services keep their usual placement.
`detached_runs.py start` requires `systemd-run --scope` or `systemd-run --user --scope` when the program is `systemd-run`.
Other `systemd-run` forms return exit 2.
With a run slice, the launcher uses the user manager and adds `--slice-inherit` to this scope wrapper.
The guard skips `env` assignments and the two-word `-u` and `--unset` options.
If `env` uses another option, the guard refuses the start when a later argument contains `systemd-run`.
To limit memory, use `systemd-run --user --scope --collect -q -p MemoryMax=<cap> -- <command>`.
The record saves the command's process group as `command_group`.

1. Wait for the stop lock.
2. Read the current run record.
3. Check whether the slice is populated.
4. Check that the caller's stop route still applies.
5. Save the stop reason and pane cancellation if both checks pass.
6. Send TERM through pidfds to processes that still belong to the run slice.
7. Give the processes the stop grace to exit.
8. Write `1` to the slice's `cgroup.kill` if the slice is still populated.
9. Confirm that `cgroup.events` reports `populated 0` or that the kernel removed the empty slice.

An unreadable slice, a replacement directory, or a slice that stays populated after KILL produces an error.
The record stays running until the supervisor or recovery can confirm that the slice is empty.
The supervisor saves the exit code and cleanup intent before it releases the watcher.
The run record saves `finished_at` when it records the confirmed end of the slice.
The caller saves the stop commit before it sends a signal. The stop commit is final.
Recovery syncs the record directory before it signals a slice for a saved stop commit.
A sync error leaves the record available for another attempt.
A read error before the commit leaves the run record unchanged.
After supervisor loss, the watcher retries slice reads within the stop grace.
The hold enforcer checks the active holder set again after it acquires the stop lock.
If pruning removes the run during the lock wait, the stop returns a refusal.
Builds Kill recovery uses the same locked route check for a first commit.
It can finish the commit for its action.
The stop commit saves the notice ID in the run record.
Pruning keeps a finished run while its notice ID is present.
Builds acknowledges the notice ID after it saves `SendingNotice` or confirms that the action was abandoned.
Builds recovery uses the saved run path to acknowledge notices after either event.
An acknowledgment confirms the run directory sync even when the notice ID is absent.
An acknowledgment error does not block notice delivery.
After acknowledgment, the usual pruning rules apply.
A run that ends by itself just after the stop commit reports as stopped.
Recovery finishes committed stops and the pending cleanup of runs without a supervisor.
On Linux with pidfds, a run without a saved slice can receive TERM and reports an unconfirmed stop.
On systems without pidfds, the stop checks the saved leader identity and sends TERM to the group.
The watcher confirms the command group from its own membership before it leaves the group.
It can send TERM to surviving members after the leader exits.
The watcher waits for the saved leader and the group to end before it reaps the run.
The stop grace limits this wait.
After a normal command exit, the supervisor gives the slice members the stop grace to exit.
It stops any processes that remain and reports them as a cleanup in the result.
A build hold records the run as stopped after the same slice stop confirms the end.
The supervisor stays alive to record the exit status and deliver the result.
A run with a saved stop reason reports `STOPPED` and includes the reason.
The result includes the command's exit status.
Exits 76 and 77 also include instructions for the next action.
A committed slice stop cancels the verified owning pass.
A group stop with `self_stop: true` keeps the pass active and sends the result to the owning worker.
Delivery stays pending if the worker cannot receive the result.
If the worker's staff row is gone, the director receives the run result.
New worker runs save the recorder pass ID or the reason the lookup failed.
An unavailable pass ID prevents cancellation of the pass.
For older runs, the stop checks the fixed worker ID, run folder, and pass start time.
The supervisor records the leader's exit status.
The supervisor waits for the entire group to end.
The supervisor starts delivery once the group ends.
The run stays pending for completion readers until delivery or the reaper confirms that the group ended.
The folder reader finds pending stops by index and verifies their status in the run record.
The stop uses the same command and group checks as the orphan reaper for other runs of the worker.
The stop keeps the pass active if it cannot confirm ownership or the end of the commands.
It reports the reason for keeping the pass.
The stop holds the worker lock, then the admission lock, then the recorder lock.
The stop checks the shared receipt for DONE before it cancels the pass.
The recorder saves a verified DONE through normal completion.
The recorder saves the canceled status, stopped outcome, reason, and notice in one state write.
Assign can reuse the idle worker while the stopped pass waits for another DONE.
Restore keeps the stopped pane open while a detached result waits for another DONE.
`worker.py answer` reports `no active pass`.
The turn hook sends the director one stopped notice with the worker name and phase.
The recorder retains the notice for delivery after another pass replaces the stopped pass.
The turn hook uses the shared receipt to save a late DONE reply and keep the stopped outcome.
The receipt checks detached results that belong to the stopped pass.
A result received after DONE requires another matching DONE.
The turn hook records the saved reply path when the stopped pass is still current.
After the notice is recorded, the hook locks the worker and checks the pass again.
It refreshes the saved file from the latest verified reply.
The normal close path requires a current idle state for a stopped pass with a live agent.
After it releases the worker lock, the hook records a close refusal and sends the reason to the director.
The hook releases the worker lock before it retries undelivered close refusals, including refusals from replaced passes.
The notice lock prevents duplicate delivery.
The recorder retains the original prompt and reply path after a replacement pass starts.
If the Stop event has no DONE marker, the hook uses each unsaved receipt to check the transcript for its DONE marker.
The hook records an unreadable archived reply in `turn-end-ignored.jsonl` and continues with the current pass.
A run without a pane pass uses the ordinary result delivery.
A newer run of the same command suppresses the older orphan result for an ordinary run.
A repeated request to stop a pass sends no additional signal or notice.
If the supervisor dies, the orphan reaper finishes the record and delivers the result.
The next start, worker restore, or explicit `detached_runs.py reap` invokes this recovery.
The result carries `Detached run: <run_id>` as its stable marker.
Under a per-run delivery lock, `detached_runs.deliver` checks the worker transcript for that marker.
It skips delivery when a user entry already contains the marker.
The sender submits to a proved worker pane or sends the result and cause to the current holder.
Before Herdr sends keys to Codex, `foreground_pane_owner` checks which descendants can hold the terminal.
The function reads the kernel process state and skips each confirmed exited child.
It checks stdin for the other descendants before Herdr checks the composer for a draft.
A pipe permits input to Codex.
If a descendant uses the pane terminal or its stdin is unreadable, Herdr rejects the prompt.
The holder receives the result with one anomaly line that names the program.
`check_patch` checks patch paths against the binding generation and phase reservation.
It rejects patches outside the bound paths or repository.
The check does not cover arbitrary shell or Python writes.

### Astra work header

`scripts/astra/astra_work.py` records assignment identity before dispatch in `<!-- astra_work {JSON} -->`.
`AstraWork` schema 1 identifies the role, member, document, assignment time, reply path and work.
`PhaseWork` records the run, phase, phase instance and history file.
`StandaloneWork` identifies work without a phase.
`HeaderMissing` and `HeaderInvalid` preserve failed read reasons.
`phase_work` requires exactly one matching active or open phase in the run state.
`read_header` checks exact keys, schema, role and the UTC assignment time.
The brief command creates the prompt exclusively and refuses an existing reply.
An identical brief retry keeps the original file and assignment time.
`astra_review.py` requires both phase flags or neither.
An unresolved phase returns exit 2.

### Census for cargo-handler

`astra_roles.py` stores `staff_id` and `held_at` under `ASTRA_ROLES_STATE`.
A process hold also stores `pid` and `pid_start` to identify its process lifetime.
A durable hold stores `pid: null`, `design_id`, and `role_id`.
The record stores no session directory, worker name, thread, account home, or manager.
Staff supplies the current session, name, account, parent, and process-hold role.
`hold` sets `astra-designer` or `astra-reviewer` through `staff.set_worker_role`.
It separately records dated `impl` or `review` attribution when the agents log does not already show that assignment.
Repeated holds with matching dated attribution append no duplicate assignment.
`hold`, `release` and `live_roles` manage the census.
Their record writes and removals share the exclusive `role_lock` under the state directory.
A missing runtime does not erase a durable design identity.
Cargo-handler reads staff names and parents from schema 2.
The [session roster](session-roster.md) shows open roles through their current staff identity.

## Worker operation fencing

`scripts/herdr/worker_panes.py` owns `worker_lock` and `WorkerLockHold`.
`worker_lock` takes the parent's name lock before it reads the published worker and locks the fixed staff id.
It holds both locks until the operation ends.
The name lock prevents a hook from taking the fixed-id lock between publication and `WorkerLockHold.bind`.
A competing dispatch returns `Kept` when the lock is held.
`worker.py dispatch` holds both locks from launch through pass creation, prompt submission, or detached handoff.
The detached child takes the same lock before compaction and keeps it through prompt delivery.
It releases the lock before the completion hook ends the pass.
`worker_dispatch_state` keeps an active pass with a saved submission held during this gap.
A transcript that ends with the dispatch DONE line proves completion before the hook records the pass end.
An active pass without a saved submission, a held worker lock, or a live detached run is stale.
`scripts/production/staff.py` owns `slot_operation` and `row_operation`.
These locks protect native identity publication and family changes.
Inherited child processes share the staff lock through its file descriptor.

Native stop paths use the caller's absolute monotonic deadline through row locks and worker stop calls.
`operation_deadline` carries that limit through callbacks and nested helpers.
An omitted deadline inherits the active limit.
An explicit limit can only reduce it.
Nested scopes restore their parent on every exit.
New threads and processes receive the absolute deadline explicitly.
`acquire_flock` handles shared, exclusive and immediate lock requests.
Lock expiry propagates as `OperationDeadlineExpired` to the incomplete shutdown report.
A released lock does not prove that a turn stops.
Native idle or stopped evidence must prove the source state.

## Staff membership and dispatch

A worker has a fixed staff id, a parent and a current native session.
A family change preserves the staff id and changes the current session.
Staff storage uses schema 2.
Backend readers derive the family and account from current session evidence.
They store no separate backend record.

Native dispatch identity comes from the recorder's pass and history records.
The held Claude launcher stores the fixed staff reference and slot in `impl_worker_<slot>.json`.
It stores no worker name or backend family.
Mesh roster entries use fixed staff IDs as keys and retain dispatch telemetry.
Pass selection uses the fixed worker ID and checks the current staff parent.
Run selection searches the current parent's runs.
A supplied run folder must name that parent's fixed staff ID.
A dispatch link supplies dispatch detail only.
Current membership comes from the staff parent, rather than the presence of a record.
`worker_state.worker_record_path` resolves the staff member before it selects the slot file.
`worker_admission.parent` reads the run owner from the state file.

## Displays and messages

The roster, board and account display read staff members and pane evidence.
`worker_panes.observe_parents` shares one pane read across the selected parents.
Each display preserves the reason for incomplete evidence.
A missing pane record does not prove that a worker has ended.

Staff worker addresses use `worker:<parent>/<worker>`.
Message selection resolves the fixed staff id and current session.
Ordinary messages can go to a verified worker pane with status `working`, `idle`, or `done`.
For a worker address, the holder receives the message and cause if the pane is unavailable.
The sender also uses the holder if it confirms that submission failed before input.
If the pane may have received the message, the sender reports unconfirmed delivery and stops.
Daemon sessions retain their `codex:<thread>` route.

## Retirement and resume

Unit retirement stops workers through the pane worker path.
DONE closure preserves worker rows and conversations.
Run cleanup selects workers from its dispatch records and calls `worker.py end` for each selected member.
`remove_workers.py` resolves the selected staff member before it calls the worker command.
Shutdown keeps saved Claude child launcher commands in `ClaudeChildResume`.
Pane worker resume messages keep the fixed staff id and dispatch id.
Account move messages preserve the move source, destination and task context.
Native source release requires a confirmed stop or absence.
Unknown source evidence preserves the source and prevents the family switch.

The sweep uses staff ownership and pane states to retain live run folders.
Stopped or closed workers can supply end evidence.
Unknown or incomplete evidence retains the folder and names the cause.
Detached command records retain their own process and working directory checks.

## Shared helpers

`worker_lifecycle.py` owns watchers and watcher events.
`finished_run_status` has the same owner.
`herdr.py` owns `loaded_threads`.
`codex_test_support.py` owns the shared launcher fixtures with their existing names.

## Pane closure and restore

The completion hook closes a worker pane after DONE.
An immediate repair or phase dispatch can retain the pane.
A canceled or interrupted submission keeps its open or stopped pane until the saved stage receipt and reply match.
Pruning uses the same receipt and reply for results from the current pass.
A live detached run keeps the pass active, so the hook closes no pane until that run ends.
A live detached run also keeps a stopped worker's pane at the shell, so the Codex capacity stop cannot close it.
When that run ends, its run-end event checks whether the stopped pane can close.
The stop report names each pane that stays open.
The close reads no process. It closes the pane at once.
Closure keeps the staff row and thread.
The next assign resumes that thread in a new pane.
An account move resumes an open or stopped worker in its same pane and closes no pane.
Restore reopens only workers with active dispatches.
Idle workers stay closed until their next assign.
Account moves preserve closed idle workers.
After restore, the command lists completed passes in open phases, newest first.
Each line gives the current worker name, phase, dispatch id, and reply path.
The reader uses recorder passes and history events without adding a record.
Each finished event retains the saved reply path before another dispatch replaces its pass.
A missing saved reply path prints `unavailable`.
Repeated restore calls list the same results while their phases remain open.
The director applies `<RestartTakeover/>` from `docs/direct/restart_takeover.md` before repeating work.

## Worker account display

`worker.py list all --json` gives each retained worker its name, fixed id, current session, state, role and activity.
JSON rows also give `dispatch_id`, `run_path`, `completion_state`, and `reply_path` from the newest recorder pass.
Missing dispatch evidence uses empty strings and `completion_state: "unknown"`.
It computes `work`, `account` and `home` when it reads each worker.
It stores none of these three fields.
`worker_panes.worker_state` supplies `work` as `idle`, `busy` or `unknown`.
`member_account.resume_account` supplies the account label and absolute home path.
Unavailable backend evidence gives `account` the `AccountUnavailable` state and its cause.
That row has no `home` field.

`worker.py list [open|closed|all]` defaults to open panes.
`list closed` shows closed panes. `list all` includes uncertain rows too.
The positional filter uses `WorkerFilter`; `--all-parents` replaces the old `--all` flag.
The flag still selects saved dispatches from every parent.
`list all --json --all-parents` needs no caller staff row or live pane read.
Open and closed filters use current pane evidence, including with `--all-parents`.

Text output has aligned headers: `#`, `Name`, `Account`, `Activity`, `Pane`, `State`, and `Waiting on`.
Activity uses the active dispatch kind, then the worker's role when no dispatch is active.
Waiting on uses existing live detached run labels and shows `-` without a specific run.
Text rows place open panes first and retain the current order inside each group.
`_worker_pane_state` maps each observed worker to its pane and state words.

| Observed worker | Pane | State |
| --- | --- | --- |
| `OpenWorker` | `open` | `paused` or `exited` from staff, else `busy`, `idle` or `unknown` |
| `StoppedWorker` | `open` | `exited` |
| `ClosedWorker` | `closed` | `-` |
| Any other class | `unknown` | `-`, with its reason |

A row whose pane is not open prints `-` for the account.
Saved dispatch rows print `-` for Pane and use their recorded dispatch state.
`display_cells.machine_column` adds a foreign machine inside the account cell, in parentheses.
A worker uses the machine of its parent's worktree.
`list_workers(as_json=False)` supplies text values without changing JSON fields.
JSON retains its existing row order and schema; only the selected filter removes rows.

## Assignment settings and command files

Pane workers have no option for compaction during a turn.
Codex keeps its own full-window limit.
`commands/unit/direct.md` defines `<LongLivedWorkers/>`.
`docs/direct/repair_rounds.md` references it from `<FixDispatch/>`.
`docs/direct/launch_implementation.md` references it from `<LaunchImplementation/>`.
`docs/direct/early_review.md` defines `<EarlyReviewArm/>` and the early review worker.

### Key files

| File | Purpose |
| --- | --- |
| `scripts/production/worker_admission.py` | Admission, publication and family changes. |
| `scripts/direct/claude_worker.py` | Claude launch and retry. |
| `scripts/herdr/worker_panes.py` | Native worker observation and pane operations. |
| `scripts/astra/design_roles.py` | Held design identities and assignments. |
| `scripts/agents/worker_lifecycle.py` | Lifecycle locks, watchers and event records. |
| `scripts/whoami/worker_usage.py` | Usage store report. |

## Invariants

Staff publication precedes successful assignment delivery.
A family change preserves the fixed staff id, parent, worktree and queued messages.
An uncertain stop does not authorize a replacement source.
A retry preserves the original assignment identity and the saved handoff.
A worker identity file names the fixed staff id.
Saved identity records do not select the next model.
Native policy checks retain a compatible running worker and restart it in place when its policy must change.
Review artifacts identify the pass and lens.
Every worker prompt repeats the Three Gods sentence and the Type Design Contract, directly or through a reference.
Usage reports name unavailable values and incomplete sources without changing the usage store.

## Calibration / gotchas

Worker reuse depends on fixed staff identity, assignment role and availability.
A rename does not authorize name-based replacement of an existing worker.
A missing completion message does not prove that a native turn stops.
Usage weights have dates and can differ between report windows.
`DEFAULT_T_LIVE` is `2026-10-10T18:17:00Z`.
The before window covers the preceding seven days.
Codex edit calls can contain `tools.apply_patch(` inside JavaScript.
Edit detection excludes quoted text.

## Design rules

Persistent conversations retain context between assignments.
Read-only reviewers and writers have separate access policies.
Checkpointed phases provide the comparison unit for usage reports.
Staff owns worker identity and parent relationships.
Native evidence owns current execution state.

## Incomplete owner evidence

The worker scan records incomplete evidence for each backend. Staff removal refuses an unreadable scan that could include the target worker.
The scan skips a process when the pane proves a different foreground PID or an empty shell.
The scan can exclude a different PID before it reads the foreground owner's conversation.

A unit report records the staff lifecycle and current owner evidence separately. A unit can have a recorded pause or exit while the current owner is unknown. The watchdog preserves the outage and any recovery result when the owner is unknown. A direct resume checks current owner evidence for a unit in a production. Demotion selects a launch mode only after a fresh owner check.

The unit list takes the status and address from the checked owner. It leaves the pane address blank for a standalone owner.
The unit report uses activity from the current record that matches the checked PID, process start, and conversation.
The unit board uses the checked owner for presence.
The board marks an unknown owner as unknown in the summary, Director column, and detail heading.
Census session rows contain no PID or process start.
The board omits command names and shell waits from these rows.
The board shows unknown director work when shell rows indicate possible work and other records do not establish the work state.
The board uses checked owner activity when the census is readable.
It shows idle for explicit registry idle.
It shows unknown director activity when the census is unreadable or the checked evidence does not show activity.
Proved absence remains visible when the census is unreadable.
The live unit ID collection retains unknown units and scan failures.
The inventory reports unreadable placement as unknown.
Worker director lookup checks the staff member's current conversation.
It returns `UnknownUnitDirector` when it cannot establish ownership.
A standalone Claude director needs a recorded process start and matching native identity.
A single proved owner takes precedence over incomplete registry records.
Conflicting owner proofs for the same conversation return `UnknownUnitDirector`.
A missing or unavailable messaging socket does not change proved ownership.
Stall watch checks the unit owner before it reads a pane or retires prompt state. A retained launch alone supplies a candidate.
An empty candidate pane stays unassigned to the unit. The unit's prompt state stays in place when its pane is unassigned or unknown.
