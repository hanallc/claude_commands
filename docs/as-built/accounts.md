# Accounts and machine-local settings

The account tools separate provider identity from shared configuration and conversations.
Claude groups and Codex workers select numbered accounts through one role map.
Producers use the group account for their configured family.
Both machines use that map, but each machine keeps its own sign-in state.
Account moves preserve conversations and worker threads.
Git filters keep machine-local settings outside promoted configuration.

The [account guide](../accounts.md) describes commands and repairs.
This document describes the implementation and its constraints.

## Ownership and data flow

| Module | Responsibility |
| --- | --- |
| `scripts/accounts/accounts.py` | Account checks, preparation, launch selection, moves, trust, updates, and status. |
| `scripts/accounts/role_map.py` | The shared role map, its transport, cache, locks, and change history. |
| `scripts/accounts/next_moves.py` | Ordered moves, holds, positional edits, and validation. |
| `scripts/accounts/refusal_watch.py` | Refusal detection, login renewal, queued moves, and automatic group moves. |
| `scripts/accounts/login_sittings.py` | Weekly and safety reminders from stored Claude login end dates. |
| `scripts/accounts/refusals.py` | Provider refusals and the freshness of usage readings. |
| `scripts/accounts/moves.py` | Move outcomes, waits, assignment routing, and replay evidence. |
| `scripts/accounts/unit_watchdog.py` | Recovery from pane launch records and fresh session liveness. |
| `scripts/accounts/outage_alerts.py` | Durable alert claims for retained record-driven restart paths. |
| `scripts/whoami/agent_accounts.py` | Account identity, Codex homes, worker selection, and credit policy. |
| `scripts/whoami/note_file.py` | Numbered account notes and the shared notes-folder resolver. |
| `scripts/whoami/quota_state.py` | The quota state file, the no-worker mode read, and the `NO_WORKER_START` refusal text. It is a leaf module. |
| `scripts/settings/settings_local_keys.sh` | Settings classification, local snapshots, and Git filter transformations. |
| `scripts/settings/local_key_notices.py` | Deferred notices for new machine-local keys. |
| `scripts/lib/conf.py` | Validated configuration reads and recovery. |

Shared whoami modules use canonical `scripts.whoami` imports.
Loading the same module under two names creates distinct runtime types and breaks state checks across callers.

The agent-notes job runs these parts in separate processes:

1. Notes.
2. Quota alerts.
3. Pacing.
4. Refusal moves.
5. Unit recovery.
6. Login reminders.
7. Phase note pruning.
8. Producer folder size checks.
9. Vault commits.

`scripts/maintenance/agent_notes_job.py` owns that job.
`config/agent_notes.toml` supplies its schedules and process limits.
An invalid schedule block fails its part. The other due parts still run.
Herdr producer recovery, layout saves, and detection rules belong to herdr-upkeep, with `config/herdr_upkeep.toml`.
Neither job collects recovery snapshots or a desktop session note.

## Producer folder sizes

`Part.FOLDER_SIZE` checks active producers' recorded folders below `PRODUCER_FOLDER_ROOT`.
A recorded document file identifies its parent folder.
`parts.folder_size` sets a 3,600-second interval and a 60-second process limit.
Each check reads `producer_folder_limit_mb` from `staff.conf`. The limit is a whole number of at least one and defaults to 10 MB.
One MB is 1,000,000 bytes.
The check counts the bytes of every regular file, including media. It skips symlinks.
The size is an estimate. A folder change during the walk can cause one extra notice.
A missing or unreadable folder or a failed metadata read produces one log line. The check continues with the next producer.
The check measures every folder before it sends a notice. It writes each warning as soon as it occurs.
The job sends one notice to the staff ID when a measured size exceeds the limit.
The job saves each crossing in `agent-notes/job/folder-size.json` under the state directory.
A send has a 5-second delivery limit and a 10-second process limit. The job saves the crossing state after each attempt.
A retry uses the same message key. A queued notice counts as accepted.
The job sends notices with fewer attempts first on the next pass.
An interruption after Codex accepts a notice can cause a duplicate on retry.
A measurement at or below the limit clears the crossing and permits a new notice after the next crossing.

## Claude quota expiry pushes

Natedev sends the Claude low quota and expiry pushes.
The quota check uses saved weekly readings from `~/.local/state/agent-notes/readings.jsonl`.
It divides the remaining percentage by the weighted use rate from `run_out.py`.
It adds that duration to the time of the reading.
The newest reading must be within the rate code's maximum gap.
An in-use Claude account qualifies only if the forecast falls before its weekly reset and within the configured lead time.
The lead time check uses a strict limit.
Both pushes use the same scheduled move coverage rule.
Role history removes consumed entries from coverage.
The phone push title names the account and the estimated time in the user's zone, such as `claude3 runs out Sun 11-22 07:00 EST`.
Its action is `Type /agents move <role> <account>`, and it has no body.

`config/showrunners.conf` sets `claude_expiry.alert_hours=12` and `claude_expiry.repeat_hours=2`.
Both settings require whole numbers from 1 to 168, including host overrides.
The quota check skips expiry pushes during silence and saves no attempt.
The first quota check after silence uses the current forecast.
Before sending, the check saves the attempt and its weekly reset under `claude_expiry`.
The repeat interval starts at the attempt time.
A clear send failure removes the attempt so the next check can retry.
An unconfirmed result keeps the attempt until the repeat interval ends.
Changes to the forecast or scheduled coverage keep the attempt for that weekly window.
The expiry template needs a current approval and uses the approved text without live review.

## Preventive login reminders

The `Part.LOGINS` timer part calls `login_sittings.run` in its own child process.
It runs in parallel with the other independent parts.
`parts.logins` in `config/agent_notes.toml` sets a 120-second interval and a 45-second process limit.
The argument-free `agent_notes.main` path does not call this reminder.
The Mac usage refresher does not call this reminder.
The module sends only on `login_sitting_machine`.
It reads local dates through `accounts.read_status(source=StoredUsageOnly())`.
It uses the same numbered Claude rows and `login_ends_at` values that stored status exports.
Removed accounts cannot retain a date through an old usage-store directory link.
This shared status path inspects account readiness but does not refresh usage.
The peer command is `accounts.py status --stored --json` through `ssh -o BatchMode=yes` and `remote.other_machine()`.
An attempt time limits peer reads, including failed reads.
The last successful peer read time and dates remain available after a failed attempt.

`config/accounts.conf` supplies all reminder settings:

| Key | Current value | Meaning |
| --- | --- | --- |
| `login_sitting_machine` | `natedev` | The machine that sends reminders. |
| `login_sitting_first` | `2026-11-17T10:00:00-05:00` | The first sitting. |
| `login_sitting_interval_days` | `7` | The interval between sittings. |
| `login_sitting_window_days` | `10` | The selection window after a sitting. |
| `login_safety_margin_days` | `3` | The immediate reminder margin. |
| `login_peer_read_minutes` | `60` | The minimum interval between peer attempts. |
| `login_peer_read_timeout_seconds` | `30` | The peer command limit. |

Each pass reads these settings through `conf.read`.
The earliest end on either machine selects the account for a sitting.
The sitting push includes both known machine dates for that account.
When no account is within the window, the push selects the account with the earliest end.
No known dates means no sitting push.
The latest sitting replaces any missed sittings.
Safety pushes include only machine, account, and end-date combinations that no accepted push has named.
An unknown date never enters either list.

The atomic state file is `accounts_home()/.local/state/accounts/login_sittings.json`.
It holds the latest completed sitting, named login dates, peer dates, and peer read and attempt times.
Each save removes a named record only when the same machine and account have a different known end date.
A missing date keeps the named record.
Cached peer dates remain current evidence until the next successful peer read replaces them.
The module saves each peer attempt and its result before it reads local dates.
It contains no credential or login identity.
`locked_history` holds the lock in `login_sittings/` beside the state file throughout each pass.
The lock covers state reads, local and peer reads, sends, and saves.
A busy pass performs no account reads or sends.
The module records a sitting after an accepted push or an empty selection.
A failed push leaves its sitting and named dates unrecorded for another attempt.
The `accounts.login_sitting` catalog entry supplies both push kinds as normal notes.
The `login-sitting` and `login-safety` keys replace older retained pushes of the same kind.
Delivery uses `push_texts.user_send_argv` and checks `send_result.send_outcome`.
It never sets `--through-silence`.
The push formats dates in the user's zone.

## Account identity and storage

Claude account names have the form `claudeN`.
Codex account names have the form `codexN`.
The number identifies an account note, not necessarily the suffix of its current Codex home.
Legacy spaced labels remain valid input, but output uses compact labels.

`~/.claude` and `~/.codex` are shared configuration and conversation stores.
They are not the selected account after cutover.
Numbered homes retain private sign-in and runtime state.
Claude numbered homes link shared commands, skills, settings, transcripts, memory, and the session registry to `~/.claude`.
Codex homes link shared configuration, instructions, skills, session files, and history to `~/.codex`.
Codex authentication, app-server state, caches, logs, and SQLite databases remain private.
SQLite companion files remain private with their database.

Shared transcripts and registry paths let a conversation resume under another account.
The account homes use links rather than Git worktrees, so commands and hooks use the same promoted scripts.
Account checks detect broken or replaced shared links.
Preparation repairs safe shared links before launch.
It refuses an unknown conflicting file rather than deleting private data.

`NumberedAccountLabel` validates the tool and positive account number.
`NamedAccount` carries a Claude label and directory.
`CodexWorkersAccount` carries a Codex label and resolved home.
`PreparedAccount` carries the launch environment and variables to remove.
`AccountRefused` carries the reason a launch cannot proceed.
Directory checks distinguish logged-out, wrong-account, absent, unreadable, and broken-link states.
An unreadable identity is never an absent identity.

Codex identity resolution uses the account note's identity mapping.
It handles numbered homes and a legacy `~/.codex` home through the same resolver.
Status counts use that resolver too.
Setup and account checks detect duplicate sign-ins across homes.
Cutover journals preserve interrupted identity moves and support an explicit undo.
Setup waits for Codex to answer `initialize` before it reports success.
Shared session indexing can require up to twenty minutes.
The [trust guide](../accounts.md#trusted-worktrees) describes configured project roots and guarded trust-file updates.

## Role map and launch selection

Natedev owns the role map for both machines.
The map contains the Claude groups `units`, `showrunners`, and `producers`.
It also contains the Codex `workers` account and an optional timed Claude worker route.
Claude groups can share an account.
Codex producers use the workers account.
Claude producers use the producers account when it has a selection.
They use the showrunners account until the first producers selection.
`[producers]` in `config/models.conf` selects each producer's family, model, and effort.
An explicit move saves a group selection.
Status, launch, and preview reads never create a selection.

| State | Location |
| --- | --- |
| Claude groups | `~/.local/state/accounts/groups.json` |
| Worker selection | `~/.claude/config/accounts.json` |
| Role change history | `~/.local/state/accounts/roles.jsonl` |
| Peer role-map cache | `~/.local/state/accounts/role_map_cache.json` |
| Ordered account moves | `~/.local/state/accounts/next_moves.json` |

All role readers and writers use `role_map.py`.
A command reuses one map observation.
Mac writes use the shared SSH transport to natedev.
The authority locks the selection and history together.
A failed transaction restores the prior selection and history length.
A failed peer write never creates an independent local choice.

`RoleMap` carries selections and provenance.
`CurrentRoleMap`, `CachedAfterFetchFailure`, and `ExplicitStoredRoleMap` distinguish fresh, degraded, and stored observations.
A failed peer read may use a valid cache, with its observation time and failure shown.
Without a usable map, the operation refuses.
No reader invents a replacement account.

`account_for_group_launch()` resolves unit and showrunner launches.
Explicit account or directory arguments must agree with that selection.
A checked unit recovery can use its source account when the selected destination cannot launch.
The recovery records that fallback reason.
Unscoped Claude launches retain a valid caller account; otherwise, they use the configured showrunner account.
An active timed Claude worker route takes precedence for a worker launch.

`GroupNotConfigured`, `UnitsResetSoonest`, `ShowrunnersMostLeft`, and `GroupNoRoom` preserve the selection result's meaning.
Readings rank eligible destinations; they do not silently replace a configured launch account.
Units prefer the earliest weekly reset, then the most weekly allowance left.
Showrunners prefer the most weekly allowance left.
Labels provide a stable final ordering.
Recorded provider refusals block a destination until the refusal resolves.
`pick` and `placement` take the model family of the work to place.
A refusal for one model family blocks only work of that family.
When the family is not known, any unresolved family refusal blocks the destination.
`group_family()` reads a group's family from the staff model setting in `config/models.conf`.
The producers use their `[producers]` rows.
Unit directors and showrunners use `director` and `showrunner` in `[production.claude]`.
Producers on two families, or an unreadable setting, give an unknown family.
Automatic group moves, their next-move target checks and group completion use the group's family.
Codex selection also checks allowance and whether `codex_credit_accounts` lets the account spend credits.

`_exec_codex_account()` serves both `codex` and numbered Codex aliases.
It recognizes `update` before account admission.
The local update stays attached to the terminal and uses `accounts_home() / ".codex"`.
Version reads are captured.
The peer command directs update output to stderr and leaves only the version on stdout.
The caller discards peer stderr and rejects an empty version.
Each machine gets one summary line; success requires both updates and version reads.

The [launch environment](nix-shell-launch.md) describes shared Nix setup and account environment preparation.

## Moves, refusals, and workers

Account moves use the shared stop and resume API.
Each recorded session attempt writes a request and one confirmed or failed outcome to `~/.local/state/accounts/moves.jsonl`.
If the request append fails, the batch reports the error and refuses that member before any stop.
Each attempt has its own move ID and cause.
A session proved live on the target writes nothing.
A failed live source read stops the move before it records a source or sends a signal.
The stop checks that the live source PID and process start time still match.
A Claude target needs a real account directory and a matching local sign-in.
A refresh token needs a future `refreshTokenExpiresAt`.
If only an access token is present, it needs a future `expiresAt`.
The watcher checks the sign-in when it selects a target and again before it changes the group account.
The [shutdown design](shutdown-and-restart.md) owns process identity, pane reuse, prompt delivery, and recovery details.
The batch stops the available members before it resumes them.
Each session resumes its own conversation on the target account.
A selected caller delegates the batch to a detached process before any signal.
The notes timer delegates its moves through the same boundary before its part deadline can kill them.
For workers, the notes job writes the role and starts the moves in their own process.
The request stays pending until the detached process ends.
A later notes pass starts an unfinished move again; workers already on the target stay unchanged.
The detached process confirms registered requests for its final target, with the target home.
It fails registered requests for other targets with `superseded by <label>`.
Replay consumes the queued entry after either completion result.
Each worker row records its own move result.
Worker refusals do not keep the group request open.
The next assign moves a refused or stopped worker to the selected account, so a lasting anomaly cannot cause endless group retries.
One `accounts-workers-move` unit serves every target.
The runner reads the role before each worker and repeats after a changed final role.
A run lock serializes worker moves, including the fallback without systemd.
Direct Codex worker moves use this runner and read the selected role before each worker.
A Codex worker move skips a target that may not spend credits and whose stored reading has no included usage left. The worker stays, its row records the reason, and the group request fails with the same reason.
Both direct preview and execution refuse a Codex target that differs from the workers role.
The refusal says `workers role is <role>; run accounts.py swap workers <codexN> to select it`.
The swap preview shares the worker preview helper and can show a new target without changing the role.
Claude worker moves use this runner with a fixed Claude label under the same run lock.
Only Codex moves read or complete pending group requests and their marker.
A short schedule lock protects startup, the pending marker, the final role check, and group completion.
Both locks use `file_lock.exclusive`.
The marker is `~/.local/state/accounts/workers_move.json`.
Startup writes the role and marker while it holds the schedule lock.
A new handoff during unit exit retains this marker for the next notes pass.
A failed start leaves the request pending; the existing blocked-entry notice reports the cause once.
Requests use each session's observed process account, including members outside their selected group account.
Blocked notices check the queue head and target again before delivery.
Quota recipients use one session address for both names and IDs.
Workers keep their threads when their Claude owner moves.
An assignment compares the process account home with the selected account home for Claude and Codex.
It uses one process environment read for this check and the run check.
An unreadable environment permits reuse only for a worker whose pass is active in the current run.
A different account follows the existing close checks, stop, and resume in the same pane.
Workers moves and previews omit workers of the other family.
The preview shares the move decision and lists idle panes that would close, workers that would resume, and refusals with each reason.
One count line covers workers with nothing to move.
These include closed workers without an active dispatch, workers without a current session, and workers already on the target.

A group move validates the destination before it changes the selection for future launches.
`check_move_target` reads sign-in metadata from the local credential file or the macOS keychain.
If the target has no usable sign-in, the check blocks the move before any stop.
The refusal names the destination label and gives the sign-in command.
The watcher checks sign-in when it finds a destination for a due queue entry.
It checks again with the current time before it writes the new selection and removes the entry.
If no destination is ready, the watcher keeps the entry queued and notifies showrunners when the reason changes.
Staff rows define the group.
The live registry supplies each member's account.
A local row lock keeps an eligible member in the batch even without a local process or worktree.
The group passes stable staff references to the mover.
The mover resolves each staff reference to its native session before it saves the request.
The detached batch uses that native session and reads the current group selection under the session locks.
Moves and unit attachment take the native session lock before the staff row lock.
A failed live account read refuses that member before recording a source account or stopping its process.
The batch checks the PID and process start time from the live account read against the native stop proof.
A different process gets no request or signal under the old source account.
Each native session has one latest-request file, even when it gains or loses a staff row.
The file holds the request ID and the account or group.
The move checks each selected member under the native lock.
This check includes group members that appear to be on the target.
A member on the target clears its pending file.
A busy lock makes an automatic move report the row as pending.
A user command waits for the row, and at its limit it prints a fresh `batch --target` command for an explicit target.
For a group request, it prints `batch --group` so the batch reads the current group target under the session lock.
`retry` can run only a saved request.
The detached child checks its request ID before it reports a member outcome.
The mover checks the request ID under the locks before stopping the member.
It removes its file after success and leaves it after failure.
A failed journal append restores the prior journal contents under the journal lock.
`stop_resume.py result` and `retry` check each member under the session locks.
They remove completed requests, including moves completed by `shutdown resume --to`.
They remove a missing session's request only after a fresh read proves absence.
If the registry cannot prove liveness because the process start is unreadable, both commands keep the request and report the cause.
Both commands name each file with a decode error and continue with valid files.
Legacy reports supply display data only.
Both commands show a fresh batch command for an off-target legacy member.
The command uses `--group` when the legacy report names a group.
`retry` starts a new move for each saved request that still needs a move.
It uses the current selection for a saved group.
A workers swap also selects eligible Codex producers.
Their move requests record group scope and retain the selected account for retry.
A producers group move selects only eligible Claude producers.
`eligible_producers` requires active, local, unpaused staff rows with valid settings for the requested family.
`producer_settings` returns `LaunchSettings` or `InvalidProducerSettings`.
One `settings_report_scope` limits each producer's invalid-settings warning to one per operation.
A repeated move selects a stopped producer even when its home already names the target.
Only proof that the producer runs on the target permits a skip.
Unreadable live account evidence prevents the stop, start, and home change.
The [producer launch contract](shutdown-and-restart.md#producer-launch-and-admission) defines the shared move and publication checks.
An empty group can still select its account.
The preview and move report an incomplete member list when the Claude registry is unreadable.
The move saves the group selection and limits session moves to members with live evidence.
Each member has an independent result.
The group command prints moved, pending, and failed members by staff name.
A retry of the selected destination reuses the group transition ID.
The refusal watcher skips members whose staff rows are busy.
The watcher uses one read of the move journal for all members.
The watcher keeps a member on a different account when its latest individual move was by hand and newer than the group selection.
Each new move request records a `scope` of `group` or `session`.
Worker group requests and requests for their worker panes use `group`.
The watcher treats legacy requests without `scope` as individual moves.
A group request keeps the member eligible for completion.
A selection without a time is older than any individual move by hand.
A later group selection lets the watcher complete the move for that member.
A pending refusal stays eligible until its move completes or a detached batch accepts it.
Each machine completes only the group members in its own session registry.
On natedev, the refusal pass of the notes timer completes them.
On the Mac, the `usage-only` job runs `refusal_watch.complete_local_groups` after its usage read.
The Mac pass runs at most once in each `parts.refusals.interval_seconds`, less half a poll, also after a failed move.
Its schedule history is `~/.local/state/agent-notes/group-completion/completed.json`.
Natedev is the only authority on which groups a machine may complete.
Each natedev refusal pass that is not a dry run or a proof writes `~/.local/state/accounts/completion_plan.json`.
The plan holds the pass time `at` and, for each permitted group, `target`, `cause`, `why` and `selected_at`.
The plan leaves out a skipped group, a legacy selection and a target with an unresolved refusal for the group's model family.
Legacy selections stay natedev's job, because natedev upgrades them in the role map.
`accounts completion-plan-read` prints the plan on natedev, and the Mac reads it through the role map ssh transport.
The Mac pass first reads its local group members and stops before ssh when no member is live.
It then reads the plan fresh and does nothing when the plan is unreadable.
It also does nothing when the plan is older than three `parts.refusals` intervals.
Natedev runs its refusal part one interval after the last run ends, so two plans can be almost two intervals apart.
The Mac completes only the planned groups, with the plan's target, cause and why.
It applies the Mac journal check for moves by hand made after `selected_at`.
It never writes the role map and never calls `set_group_account`.
The move batch reads the group's current target again under the session row lock, so a later selection wins.
The Mac pass uses the watch lock, the journal checks and detached moves, but no refusal placement or quota alerts.
An in-progress result from another move keeps the refusal pending.
The watcher saves the decision ID before it requests the move.
A retry uses that decision ID when only some members still need to move.
A single-session move uses the explicit account.
If the group uses a different account, the command reports the return to that account at the next resume.
The move path creates no ready mark, conductor, follow command, or cancellation step.

`TranscriptRefusal`, `ProofRefusal`, and `NoSessionRefusal` distinguish provider evidence from a test assumption and absence.
Proof evidence stays with its selected session.
The watcher saves the first refusal observation and suppresses repeated actions for that entry.
Known short five-hour resets can wait instead of moving.
The default wait boundary is strictly less than 20 minutes.
An unknown reset or the exact boundary does not qualify for that wait.
Failed continuation delivery retains the retry state.

A Claude refusal entry gives its limit type and reset in `apiErrorParams.rate_limit_info` or in `quotaLimits`.
`refusal_watch.claude_entry_refusal(entry, session_family, families)` reads one entry.
The caller supplies the family of the session's prior model and `families`, the key set of `[claude.families]`.
The prior model is the newest real assistant entry, even when its model is not a known family.
`seven_day` and `weekly` give an all-model weekly refusal; `five_hour` gives an all-model five-hour refusal.
A family name is a key of `[claude.families]` in `config/models.conf`; any other name is not a family.
`seven_day_<family>` gives a weekly refusal for that model family.
Another `seven_day_` type gives an all-model weekly refusal.
`seven_day_overage_included` gives a weekly refusal for the family of the refused work.
The text "reached your <Name> limit" names that family.
Without a family in the text, the newest real assistant entry before the refusal names it: `message.model`, then `requestedModel`.
Without either, the refusal applies to all models.
The refused session's own family is used when the watcher places that session without a group.
When a refused group member's own family differs from the group's family, the watcher places the group with an unknown family.

`refusals.json` keeps one all-model record and one record for each model family for each account.
A family record has the key `tool:login:family` and a `family` field.
A newer refusal replaces only the record with the same key.
A family refusal resolves at its reset.
A fresh reading never resolves it, because saved readings do not hold the family window.
Without a known reset, it resolves 7 days after the refusal.

Queued moves use group-local positions and stable internal identities.
Only the head of each group can run.
An unavailable target remains queued and blocks later entries.
A changed head loses its old hold and notice state.
`FixedHold` carries a duration; `HalfPointHold` requests a pace-based duration.
`ResolvedHold` keeps the duration and its source.
The half-point estimate uses the selected account's history, including other groups that share it.
The default estimate is 40 minutes when no usable pace exists.
The duration stays with the started hold rather than changing on each pass.

The watcher removes a Claude group's head when its target matches the current group selection.
The rule applies before the head is due and uses the group selection as the completion condition.
The watcher takes the group lock inside the queue lock for the selection check and queue write.
It uses the same lock order for a match on a fallback account.
The hold check uses the account and transition ID from the same locked snapshot.
A new hold starts at the clock time of that snapshot.
Pending members keep their move results and retry command.
The watcher reports each removal after the queue write succeeds and before it saves the hold state.
This rule also applies during replay.
A failed removal report does not stop the move or the state save.
It evaluates the next head on a later pass.
An automatic move removes its own entry by ID after it saves the selection, including when it selects a fallback.
Replay checks retain a blocked entry when history is unreadable.
An incomplete final journal row is distinct from corruption between complete rows.
Queue edits and execution share locks on natedev.
The Mac forwards edits instead of maintaining a second queue.

Without a queued workers move, a quota refusal can select another Codex account when the workers account cannot work.
Credit policy determines whether an exhausted account can continue.
Worker assignment fallback to Claude remains separate from the selected Codex account.
Routing returns only when the selected Codex account can work and its refusal has resolved.
An owned workers refusal checks `codex_can_work` before it selects another account or routes work to Claude.
Remaining allowance or a positive balance with credits on keeps the current account working.
Login faults and refusals from other homes never route workers to Claude or start no-worker mode.
The existing login fault notice stays unchanged.
No-worker mode requires exhaustion on the workers account and no alternate account from `accounts.pick`, except at weekly zero with credits off, where a failed move counts as no room.
Unknown capacity cannot confirm exhaustion.
The flag starts only on the workers-role host, `role_map.ROLE_MAP_HOST`.
The refusal handler sets this flag independently of the first `NoRoomMove` entry and the Claude assignment switch.
On the refusal path, a failed move does not start no-worker mode while another account has allowance or permitted credits. On the credits-off zero path, a failed move counts as no room and can start the mode.
The saved flag triggers one broadcast to showrunners and unit directors on both machines.
`config/accounts.conf` supplies `no_worker_mode_on` and `no_worker_mode_off`; `quota-alert` identifies the sender.
Each producer that `no_worker_mode_fyi` names (`morty dwight`) gets one copy of each notice through `send.py`.
The copy begins `FYI, no action:`; producers direct no workers.
The ON notice directs directors to write code, run tests, and do reviews without assigning workers.
Successful Codex recovery clears the flag and broadcasts OFF after the workers account and assignment restoration succeed.
Recovery also clears the flag when no Claude assignment fallback was saved.
An unfinished Claude switch or failed restoration retains the flag until recovery can finish.
The state context saves each flag change and releases the lock before it starts a separate broadcast unit.
When the flag goes on, the same step starts `scripts/production/worker_capacity.py stop` as a second transient unit.
That command stops each Codex worker of every local parent, busy or idle, under the worker's lock, and proves the stop from a new pane observation.
It ends each open pass of a stopped worker as `canceled`, also for a Codex worker whose pane is already closed.
It closes the pane of a stopped worker when the close checks permit. Claude workers are not touched.
Each affected director gets one stop report. A worker that is not proved stopped, or a pass that is not canceled, also goes to `faults_to`.
A live detached run of a stopped worker continues; the report names its result file and its log.
Its result goes to the director: `send.deliver_worker_text` sends the text for a stopped or closed worker to the holder and starts no worker.
No command starts workers when the flag goes off; the next assign of a director starts its worker.

The credits setting of the hold owner releases a credits-off hold: when `codex_credit_accounts` names the owner again, routing can return before the hold time.
An unresolved refusal does not block the return when credits are on for the selected account and a reading taken after the refusal shows a credit balance.
This rule is local to the return; `accounts.pick` and the next-move target check still treat that account as refused.
The start uses `systemd-run --user --collect --quiet --no-block` with the user manager's environment.
The unit uses the repository working directory and `scripts/lib/py` to run `scripts/production/broadcast.py`.
The quota pass waits only for the unit start, so slow peer delivery cannot consume its 30-second budget.
A failed unit start logs one failure line and has no retry.
The separate unit records later delivery output in its journal.
A refresh that clears the flag reports `notes refreshed; workers-back notice sent`.
The flag survives state reloads and suppresses repeated ON notices during the same episode.
`agent-notes.timer` starts `agent-notes.service` 30 seconds after the preceding service finishes, with one-second timer accuracy.
`scripts/maintenance/agent_notes_job.py` runs its quota part after the notes part.
`config/agent_notes.toml` sets the quota and notes intervals to 120 seconds.
Normal recovery takes about two to three minutes plus reading, restoration, and broadcast time.
This interval is not a fixed reset-to-notice deadline; stale readings, failed moves, or interrupted jobs can delay OFF.
Pane workers move under their dispatch lock with fresh pane evidence.
A worker whose source account is the target returns `already on <target>` with exit 0; the move closes and starts nothing.
The move reads the role settings for the worker's current family through `worker_panes.launch_policy(role, family)`.
After the stop, `pane_moves._own_policy` reads the latest real Claude assistant turn for the model and effort.
The launch record or role supplies missing settings. A turn without effort keeps the fallback effort.
An explicit unit resume `--model` overrides the model.
For Codex, `worker_panes.kept_settings` keeps the saved model, effort, and service tier.
The destination and rollback use the same policy.
An idle worker closes its pane through the shared `worker_panes.can_close` check.
`worker_panes.close` repeats that check under its worker lock before it closes the pane.
Held dispatch locks, live launchers, and unfinished submitted passes keep the pane open.
A live detached run never holds an account move.
The move stops and resumes its worker on the target account, even when the worker is idle.
The preview and resume text name each run, its result record, its log, and the command that reads them.
The detached run continues and delivers its result through the worker's stable staff ID.
The normal delivery route uses the current session or its holder.
The new-assignment check still refuses a worker with a live run.
The preview prints `kept open: <reason>;` before its resume text.
A held worker lock instead prints `not moved: dispatch lock is held` and refuses the move.
Every finished pass needs its dispatch DONE at the end of the last transcript turn before its open pane can close.
This proof permits closure when herdr still reports a working agent.
Without a dispatch pass, the last transcript turn must end with the agent's final reply.
Unavailable transcript evidence keeps the pane open and reports its reason.
A later message removes that proof.
Unknown process identity keeps its pane and reports its reason.
Closing the idle pane writes no move journal row.
The next assign resumes it on the group account.
A busy worker or a kept worker uses `restart.stop_row` and `worker_panes.start_or_resume` to keep its conversation and pane.
A stopped worker with a held dispatch resumes in the same pane.
A closed worker without an active dispatch has nothing to move.
An active or refused pass receives resume text through the shared target check.
A worker with a live detached run also receives resume text when its pass has ended.
Single moves, group moves, swaps, refusal moves, and shutdown recovery use the same run context formatter.
`config/shutdown.conf` sets the stop deadline from `STOP_GRACE_SECONDS`, `KILL_GRACE_SECONDS`, and `MOVE_STOP_MARGIN_SECONDS`.
The [quota guide](../quota_alerts.md) owns phone episodes, forecasts, routing notices, and delivery retries.

Login renewal uses named success and failure results.
The watcher rate-limits attempts and records their reason.
A failed renewal remains visible; it does not convert a stale reading into a fresh reading.

### Worker move results

`accounts.py move workers <target>` stops same-family workers and leaves their restart pending.
`assign.sh` cannot yet carry both the fixed staff ID and the destination account for this operation.
The result names the target account: `stopped, resume pending on <account>: <reason>`.

The preview uses the same family decision before it takes a row lock or stops a source.
A same-family worker prints `<name> (<id>): would stop; restart on <target> stays pending`.
A cross-family worker prints `<name> (<id>): not moved: cannot restart on <target> through assign.sh`.
Unavailable family evidence also refuses the restart.
The real refusal leaves the source running.

Source release preserves each stop reason and gives the next action.
The [family handoff](long-lived-workers.md#family-handoff-and-retry) defines its named results and native source evidence.
A session move's opening message selects only its [own worker children](shutdown-and-restart.md#worker-resume-results).

## Recovery and alert ownership

The unit watchdog reads current names, parents, sessions, and folders from staff.
It reads the account from the pane launch record and checks current process liveness.
It saves pending jobs before it starts detached recovery services.
Each child resolves the current staff session before resume.
The watchdog skips a member while an account move holds its row lock.
A queued recovery holds the same row lock through its liveness check and resume.
Historical jobs remain keyed by session id.
Account notes keep historical rows without using them as current counts.
A failed roster read reports one anomaly and sets the derived count to zero.
Recovery keeps its grace period, dispatch limit, and result until a fresh observation confirms the session state.
The watchdog sends no phone push, showrunner notice, or message to a live session.

Retained record-driven restart paths share a durable alert ledger.
The [shutdown design](shutdown-and-restart.md#outage-alert-ledger) describes claims, delivery, leases, machine ownership, and observation-scoped clearing.
The ledger survives shutdown-record archival.
Claim ownership prevents concurrent sends and stale updates.
A second outage or five-hour delivery caller prints `already claimed by <id>` and does not repeat delivery.
Its delivered state means a successful push, not an unfinished send claim.

Config publication uses a blocking `fcntl.flock` with no time limit or retry settings.
The kernel releases the lock when its holder exits.
Usage and renewal schedules keep separate claims.
A second schedule caller prints `already claimed by <id>` and exits with code 0.

## Status, counts, and notes

`StatusReading` separates stored facts from their display.
`StatusDocument` carries account rows, the board, problems, and session input paths.
The `board` comes from `accounts.status_board()`.
The console uses its cells to build the Accounts and Roles tables.
`accounts.models_board()` groups the same role counts by full model ID and effort.
The reader finds the Claude transcript by session ID across project folders.
Claude model and effort come from the latest real assistant message.
Codex model and effort come from the latest `turn_context`.
The model reader scans a plain file backward in 16 KiB blocks until it finds the newest model row.
`session_model_scan_kib` in `config/accounts.conf` limits the scan.
The default is 8192 KiB. The setting accepts 128 to 32768 KiB.
The reader checks one byte before the range to detect a row that starts at the limit.
A damaged complete model row gives `unknown`. The reader skips damaged output rows.
An unfinished last row can leave an earlier model in effect.
A scan that reaches the limit without a model row gives `unknown`.

A status scan loads the plain transcript cache once and saves changes atomically.
The cache file is `$XDG_CACHE_HOME/claude/session-models.json`.
If `XDG_CACHE_HOME` is unset, the reader uses `~/.cache`.
The cache retains the largest transcripts up to `session_model_cache_entries`.
The cache drops entries whose encoded fields exceed 32 KiB.
A missing or damaged cache starts a fresh scan.
Unchanged transcripts need no reads. On growth, the reader checks hashes at both ends of the old content.
The reader also checks the cached model row before it reuses that row.
A rewrite that preserves the sampled edges and the cached model row can escape detection.
If the hashes match, the reader checks new bytes and any unfinished row within the scan range.
If the file grows during the scan, the next scan reads the bytes from that append.
If an unfinished row starts before the range, the reader checks only the new bytes.
The reader uses a cached model row only while the row remains in range.
The reader scans again after replacement or truncation.
For a compressed file, the reader streams the records and caches the result in the process.
The compressed cache evicts the least recently used result at `session_model_cache_entries`.
A missing Claude transcript gives `unknown`.
Live process options apply when the reader can establish that no turn was recorded.
The peer count document carries model counts and session names by account and role.
The Models table puts the remainder of each role count in the `unknown` row.
Hover over a positive count or focus it to see the session names in order.
The tooltip uses the staff name or the Claude registry name.
If neither name is available, the tooltip uses the first eight characters of the session ID.
The tooltip includes the machine for each peer session.
An older peer count adds a line such as `2 more on mac` for sessions without names.
`config/statusline.conf` holds each account color under `account.<label>`.
`ROLE_COLOR_KEYS` maps each role group to its color key:

1. Units use `role.unit-director`.
2. Showrunners use `role.showrunner`.
3. Workers use `role.worker`.
4. Producers use `role.producer`.

The producers role has a separate row under each account it uses.
Producer rows follow the other role rows for that account.
`account_details_in_row_order` accepts the complete rows in display order.
It shows account details only on the first row of each consecutive account run.
The account details include the dot, label, swatch, allowance, reset, credits, and credits setting.
A separated row shows the account details again.
A lone producer row shows all account details.
The helper preserves the complete input rows for later sorts.
The Accounts page shows a Lucide warning triangle or urgent octagon beside the weekly percentage.
`accounts_quota_warning_percent` and `accounts_quota_urgent_percent` in `config/review_links.conf` start at 10 and 5.
The warning value must be greater than the urgent value.
The Accounts entry shows the most urgent active icon during the console count refresh.
The row, the sidebar and Clear use stored weekly readings for accounts that are currently signed in.
Select a row icon to read the alert and clear it for that account.
Clear keeps the percentage color and stores the cleared level and reset time on the server.
A cleared warning can return as urgent. A cleared urgent alert hides both levels.
The page, sidebar, and Clear use the same rule to identify a weekly window.
Two reset times name the same window when they differ by less than `accounts_quota_same_window_seconds`.
The setting starts at 3600 seconds in `config/review_links.conf`.
The clear record survives a page refresh and a server restart.
After the weekly reset, a low reading in the new window can show an alert again.
If a reading has no reset time, Clear keeps the last reset time in the account's clear record.
The server keeps the clear records in `console/.accounts-quota-cleared.json` under the review root.
If the file is malformed, the table shows the problem, the sidebar hides the icon and Clear refuses to write.
The Accounts table shows the account, weekly quota, hourly rate, run-out time, role, next move, reset, and credits.
Each role on an account has a row with its color swatch.
When rows for one account are next to each other, the first row shows the account details.
When sorting separates those rows, the account details appear at the start of each group.
An account without a role has an empty role cell.
The page and the Claude expiry push use `quota_alert.weekly_forecast` with the same usage history.
The page starts its forecast from the displayed weekly reading.
It excludes history before the current weekly window or the last refill.
It shows the hourly rate for Claude and Codex when recent history gives a rate.
If the rate is missing, both forecast cells show a dash with an explanation.
If the quota lasts until the reset, the run-out cell shows a dash with that explanation.
The page shows forecast times in the user's time zone.

The Roles table has one row per role.
It shows the selected account for each role.
Each role has a color swatch and a line of its busy sessions over the last 24 hours.
`scripts/accounts/role_activity.py` keeps those samples: the time and the busy count of each role.
`role_activity.sample_now()` records one sample from the stored-usage status read, and the file keeps 24 hours.
A gap longer than `role_activity.SAMPLE_GAP` (10 minutes) breaks the line. The notes job samples about every 2 minutes.
The count columns are busy, idle and total. Each shows the total for the role across all accounts.
A warning icon after the total names the paused and exited sessions of the role.
`StatusRow.named_sessions_by_role` gives the staff name and state of each local session of a role.
It is filled only for local sessions, and only when the read uses `include_models=True`.
A peer count has no names, so the icon gives the count and `names unknown` for those sessions.
The count tooltip lists counts by account when any observed sessions run outside the selected account.
The tooltip also lists accounts outside the selected account when their counts are unknown.
If the role has no selected account, the tooltip lists the count for each observed account.
If a role has no selected account, the account cell shows muted `no account` text.
The next column shows the next move of the selected account as text.
Only the Accounts table has the control that changes the next move.
The total row stays last, including when the account list is empty.

The Models table groups session counts by model ID and effort.

Each named header has a sort control.
The Accounts table starts in account order. The Roles table starts in role order.
The quota, rate, run-out time, and reset sort from the smallest or earliest value.
Credits sort from the largest balance. An unlimited balance comes first.
Enabled accounts come first when you sort by the credits setting.
The role column sorts by role name without regard to letter case.
The next column sorts by its text without regard to letter case.
`auto`, `none` and `unavailable` sort as words. Rows with no next move stay last.
Role counts sort by the sum for the role, largest first.
The busy line column has no sort.
Missing values stay last in both directions.
Select the active header again to reverse the order.
Press Enter or Space to sort by a focused header.
The sort arrow uses the gap beside its label.
Browser storage keeps the sort for each table through refreshes and page visits.
A failed storage read or an unknown saved column uses the default sort.
A failed storage write keeps the current choice for the open page.
The shipped value of `role.producer` is `pink`.
`role_colors` reads the current settings for the Accounts board and the console name renderer.
`staff_role_color` maps a staff row through those keys and `account_color`.
Unknown or invalid colors use the normal text color.
The value accepts a color name or a six-digit hex color.
A board span with a valid configured color carries its key as `color_key`.
Configured accounts and role groups have a color swatch on the console Accounts page.
The swatch opens a color chooser with Try, Save, and Cancel.
Try changes every label and swatch of that key locally without a request.
Cancel or Escape restores the saved color.
Save publishes the color through this checkout's `scripts/lib/tune.py` and refreshes the board.
Save records the value and its order in the settings journal before it writes the live value.
Natedev issues the orders from a persistent counter. The published commit carries them in `config/settings-orders.json`.
Each pull holds the journal lock from the ancestry check through restore.
The Mac accepts an SSH live write only when its order is greater than the accepted order.
The journal retains the accepted order after the tree includes that order and the pending value clears.
Restore changes only the keys with pending values in the live files.
The move marker records the plan before Git changes the checkout and records each completed Git step.
At the source commit, recovery reverses the recorded changes and keeps separate staged edits.
At the target commit, recovery completes the planned writes if the files still match their recorded versions.
If recovery cannot account for a change, it keeps the marker and gives the command to inspect the file.
When you remove a key by hand in Git, keep its entry in `config/settings-orders.json`.
A peer commit with the saved value and no order still needs a commit that records the order.
A host added later whose name matches a model ID suffix makes that model's color key a host override.
The page pauses refresh while the chooser is open.
A board line of a selected Claude group carries its account and group.
The Accounts table shows each next move beside its role.
Sorting keeps each next move with its role.
A selected Claude group shows its first target, or `none`, and an arrow button opens its move menu.
A selected Codex workers line shows its first target, or `auto`, without a button.
While it shows `auto`, each spare Codex line also shows `auto`; together these lines are the codex pair.
`background.codex-pair` in `config/statusline.conf` tints the pair rows on the Accounts page.
Other assignments show a minus icon.
`StatusReading.first_next_moves` distinguishes first targets from an unavailable read with its reason.
One queue read supplies the first targets for the Accounts page and the upcoming list for the text and JSON status output.
Off the role-map host, first targets are unavailable.
The menu marks the current target and can remove the group's first entry.
Its `stale_at` is the next local midnight, when its reset text changes.
The text table appends `Login ends` in the user's zone, with the same date format as `Weekly resets`.
Each JSON account exports `login_ends_at` as an ISO 8601 UTC string, or null for an unknown date or Codex.
`Report` distinguishes `LoginEndKnown` from `LoginEndUnknown`.
The usage reader takes the date from `claudeAiOauth.refreshTokenExpiresAt` in the metadata it already loads.
Both live paths use this source, so the disk identity shortcut still avoids a Claude process start.
The usage store and the timer cache preserve the date without storing other credential fields.
An accepted reading without a date removes the stored date.
Both renderers use the same problem lines from `StatusReading.notes`, including shared-home repairs.
JSON `problems` matches the text board's problem lines, with only the `* ` footnote prefix removed.
Each account row exports `sessions` beside `role_counts`.
Each entry has a native `session` ID and a classified `state`: `busy`, `idle`, `paused`, or `exited`.
The list reuses local count observations and reads no pane again.
Peer caches provide aggregate counts only; the session list contains local members.
Named problem causes and repair steps preserve the subject, fault, evidence, and action.
Status attributes a session to its account independently of the current role selection.
This preserves visible off-target members during a move.

Local role counts read the registry, staff, pane launches, and worker records without writing state.
`session_state.classify` supplies the shared unit status for the accounts board and the units table.
An exit record takes precedence over a pause record.
A busy or starting pane gives `busy`. Live detached work also gives `busy`.
On Linux, live detached work comes from one /proc scan for `detached_runs.py supervise` processes.
The read opens only the records that those supervisors name. Without /proc, it reads each record.
Exit and pause records take priority over all activity states.
Without a live detached run, a missing live session gives `exited`.
Other live activity gives `idle`.
Role counts are `busy`, `idle`, `paused`, `exited` and `total`.
The accounts board has no activity column, the same as the cargo-handler accounts table.
The activity of each session shows only in the cargo-handler tiles, from `cargo-handler census --json`.
An older peer's document holds waiting within busy and starting within idle, with separate `starting` and `waiting` fields.
The decoder moves that peer's `starting` count from idle to busy.
Claude and Codex producers count under `producers` with the same hold and activity rules.
A live producer counts on the account from its process environment.
Without a live Claude session, producers count on their launch account.
Without a live Codex session, producers count on their recorded home or the selected workers account.
Claude producers use the producers move group.
Codex producers retain their workers move group.
`session-counts-read` prints the same live count document without publishing a file.
Peer counts use the shared role-map transport in a detached fetch.
One claim prevents concurrent fetches.
The normal status path never waits for that fetch.
The cache keeps each machine's counts separate and uses the `usage_stale_minutes` age limit from `config/accounts.conf`.
Canonical `mac` and `natedev` names identify observations independently of a desktop hostname.
The board sums available machine counts.
It shows the time and age of usable Mac counts after a failed fetch or once the copy is one minute old.
It reports the peer as unavailable when no usable copy exists.

`status --stored` reads saved usage, role maps, and peer counts.
It does not wait for the detached counts fetch.
Local session counts still use current local registry evidence.
The [console Accounts page](review-links.md#accounts-page) defines the display and refresh behavior.

Account notes use numbered filenames.
Each note shows its roles and live session count.
Existing session rows remain as history and supply no current staff facts.
Unchanged notes are not rewritten.
Usage reads retain account identity and reading outcome; a failed read never becomes a zero-percent reading.
Recent usage can be reused, but low quota, resets, queued moves, and identity changes require fresh checks.
One Codex app-server serves each home within a pass.

## Machine-local settings and promotion

The settings clean filter classifies top-level keys against `git show HEAD:settings.json`.
The fixed local list contains `model`, `effortLevel`, `modelSettings`, `extraKnownMarketplaces`, `theme`, and `voice`.
A new key absent from the committed baseline is also local.
An unreadable or invalid baseline refuses the operation; it never means an empty baseline.
The hook and plugin mergers preserve their shared portions and known local additions separately.

The clean filter saves local values in `settings.local-keys.json` and omits them from Git's shared representation.
It captures stdin once, so snapshot and clean use the same document.
An empty snapshot retains saved keys that remain local.
Smudge restores saved local values without replacing incoming shared keys.
A key that arrives through committed settings becomes shared unless the fixed list keeps it local.
Unknown keys on one machine therefore do not block promotion on another machine.

`NoticeHistory` stores `pending` or `accepted` per machine and key in `settings.local-key-notices.json`.
The filter records a pending notice but sends nothing.
The normal refresh sends the notice later to the destination in `config/settings.conf`.
Only the machine and key name enter the notice; the setting value stays local.
A stable sender key suppresses duplicates.
Successful, skipped, or durably queued delivery marks acceptance.
A failed send leaves the notice pending.
The sender holds no ledger lock during delivery.

The index refresh hashes one file copy before and after cleaning.
It refreshes only when the clean hash matches the staged blob.
The guarded refresh checks the same input and staged hashes under Git's index lock.
It preserves concurrent edits, staged content, unmerged entries, and executable-mode changes.
A clean failure stops refresh and writes its diagnostic to the local error log.
Promotion never hides an unrelated dirty setting.

## Runtime configuration and constraints

`config/accounts.conf` supplies account timeouts, ages, limits, sizes, holds, leases, and path roots.
Each pass takes one `conf.read("accounts")` and shares its `ConfigRead` with its helpers.
The read supplies `whole`, `number`, and `text` methods.
Session loops reuse a value read for that call instead of reading configuration for each session.
Paths with `~` expand through `HOME`, so scratch homes remain isolated.
A configuration change needs no build.

`CONF_DIR` selects an authoritative configuration folder.
Without that override, an absent file or key can use the repository's matching `config/<stem>.conf`.
That default lookup precedes Git recovery.
An invalid present value still uses validated Git recovery, rather than an unrelated repository default.
Explicit folders have no repository fallback.
`ConfigRead.origin(key, convert)` applies the same rules and distinguishes live, recovered, and unavailable values.
The [shared configuration contract](showrunner-automation.md#shared-runtime-configuration) defines reads, limits, and publication.

`note_file.notes_dir()` gives `AGENT_NOTES_DIR` and an explicit `AGENTS_DIR` override precedence.
Its private cached helper reads `notes_directory` again only when the accounts configuration file stamp changes.
Account creation, identity resolution, and missing-note instructions use that same helper.
A later call in the same process sees a changed notes-folder setting.

The default usage age is ten minutes.
Codex alert readings use the same `usage_stale_minutes` setting.
The quota relay limit defaults to 40 seconds, and the registry editor limit defaults to 30 seconds.
Account commands and Claude usage HTTP requests each default to 15 seconds.
The Codex account server has three seconds to stop after termination.
The role-map transport has a five-second connection limit and a twenty-second command limit.
The outage claim lease is fifteen minutes, longer than the normal send deadline.
These values live in configuration; their values are not independent code defaults.
Maintenance schedules and recovery dispatch limits remain in their owning TOML configuration files.

Account and role changes preserve these boundaries:

1. Keep private identity files outside shared links.
2. Use the authoritative role map for every machine.
3. Keep unknown evidence distinct from absence or exhausted allowance.
4. Validate the destination before a destructive move step.
5. Preserve conversation identity and worker threads across account changes.
6. Keep status reads independent of the background counts fetch.
7. Mark an alert delivered only after successful delivery.
8. Clear only alerts that precede the observation of a live session.
9. Preserve shared settings and concurrent edits during local-key filtering.

## Token end dates

The Tokens table appears below Models on the Accounts page.
It shows each token's purpose, end date, days left, and state.
The page uses the user's time zone and shows the renewal steps below the table.
Each token's steps are closed by default under a **Renew `<name>`** or **Replace the token on the Mac** summary.
A click on the summary opens the steps.
Set a GitHub token's end date with `/tune showrunners token_expiry.<name>.ends <YYYY-MM-DD>`.
The page warns during a reminder window and marks expired tokens as errors.
The table includes the token that registers the Hana Linux CI runners.
It also includes the token that checks runner availability for optional Hana macOS CI.

The Linux renewal step runs `hana_runner_token.py reseal` from a plain natedev terminal.
The helper authenticates with sudo before it reads the token from 1Password.
It keeps the token in memory and atomically replaces the live credential with the sealed file.
Ctrl+C before publication exits with status 130 and keeps the live credential.
After publication starts, the helper replaces the credential or removes the temporary file on failure.

### Mac fallback token

The Mac notes timer publishes `Mac fallback token.md` in the shared account notes.
The `mac_token.account` key in `config/showrunners.conf` sets the Claude account for display and renewal.
A blank or invalid label prompts the user to set the account.
The relay and timer use no account probe.
The note stores the token file's modification time and the times of relay results.
The timer records a fresh observation at most once in 12 hours for the same file: half the 24-hour stale limit (`OBSERVATION_INTERVAL = OBSERVATION_MAX_AGE / 2`).
The page marks data stale when the observation is more than one day old, missing, or in the future.
Replacing the file starts a new record.
The page estimates expiry as 365 days after the modification time.
Copying the file or changing its timestamp can change this estimate.
The table shows an active login failure until a relay succeeds.

The relay puts the decoded value in its Claude child environment.
The selected account folder remains in `CLAUDE_CONFIG_DIR`.
The relay removes inherited credential, provider, endpoint, proxy, and TLS overrides.
It tells Claude Code to ignore provider and endpoint overrides from settings files.
Settings stored in the account folder can still select another credential, such as a gateway sign-in or an `apiKeyHelper`.
The relay records the login result separately from the message delivery result.
See https://code.claude.com/docs/en/authentication#authentication-precedence and https://code.claude.com/docs/en/network-config.


In a plain Terminal or iTerm window, not in a herdr pane, run `mac-token-renew`.
The zsh alias runs this command.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/mac_fallback_token.py renew
```

1. Approve browser access while signed in to the configured account.
2. Copy the token that `setup-token` shows.
3. Paste the token at the hidden prompt.
4. Press Enter.

The command refuses in these conditions.

- Standard input or standard output is not a terminal, or `CLAUDECODE` is set.
- `TERM_PROGRAM` is neither `Apple_Terminal` nor `iTerm.app`, or `SSH_CONNECTION`, `TMUX`, or `HERDR_ENV` is set.
- `mac_token.gpg_recipient` is missing or has an invalid fingerprint.

The command runs `setup-token` for the configured account.
It passes the pasted token to GPG through standard input.
GPG uses the 40-digit hexadecimal fingerprint in `mac_token.gpg_recipient`.
GPG encrypts the token in a private temporary file.
The command replaces `~/.config/claude/oauth-token.gpg` with one rename.
After `setup-token` starts, the command clears the terminal screen, scrollback, and system clipboard when the command exits.
The `pbcopy` utility clears only the system clipboard.
If a clipboard history app or Universal Clipboard holds the token, clear it there too.
The command publishes the new file date.
It prints the account and estimated expiry in the user's zone.

The relay child holds `CLAUDE_CODE_OAUTH_TOKEN` in its environment.
Any process that runs as the same user can read the variable.
The relay has used this design since 2026-10-01.
