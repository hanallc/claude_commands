# A readable units board

This design replaces the two phase tables with one block per unit.
Each block states the current work, each wait reason, and the next work.
The summary counts units once.
The board remains a report; it makes no scheduling decisions.

The design covers the phase board in `scripts/production/unit_phases.py`.
The separate style-ticket table keeps its current behavior.
The unit named `style` in this production belongs to the phase board.

## Proposed board

The following mock uses the five units in the brief and local observations during this design.
The live records changed during inspection; this is a fixed example, not a second live report.
Short work descriptions below are proposed author-written summaries of the existing titles and queue text.
The recording rules below specify their source and the fallback for older records.
Missing reasons remain visible.

```text
working 2 · waiting to merge 0 · waiting on other work 2 · unknown 1 · total 5
Times: America/New_York. Dependency checks use the local copy of main.

admin-console — working
  Now: The implementation worker writes the screen transitions (switch-motion); finish time not recorded.
  Also: Question text in short parts (question-text). Its worker finished; the director's task is not recorded.
        Estimated finish 18:30.
  Merge: Image zoom (image-zoom) waits to merge, 1st in line; queued since 15:20.
  Next: switch-motion remains in the queue, but it has already started. Later work is not recorded.

devtools — waiting on other work
  Now: No task runs. The first queued item needs builds-report on main; wait start not recorded.
  Next:
    1. Merge the account changes (acctland). After builds-report: absent from the local copy of main.
    2. Add account colors (acctcolor). After handler/23: submitted; absent from the merge line; reason not recorded.
    3. Add the account color picker (acctchooser). After devtools/acctcolor: not started.

phases — unknown
  Now: The director is busy, but its task is not recorded. No worker runs.
  Wait: Phase 30 has not started; reason not recorded.
  Next: Measure cheap A again on worker-mode phases (30).

style — waiting on other work
  Now: The release build for reader-start cannot start. Estimated finish 16:30.
  Wait: It needs build memory in use by tipbox; since 15:15.
  Next: Nothing queued. The remaining plan work is already current.

unit-board — working
  Now: The designer writes the readable units board (board-design); finish time not recorded.
  Also: Group work that merges together (merge-once). No task runs for merge-once; reason not recorded.
        Estimated finish 16:15.
  Next: Nothing queued. The remaining plan work is already current.
```

The `tipbox` holder comes from a later local read than the brief's `research` holder.
Both reads refer to the same wait, which started at 15:15.
A holder change must not restart that clock.
The image-zoom queue entry supplies 15:20; that time means queue entry, not work completion.

The admin-console worker now works on `switch-motion`.
Its earlier fix pass for `question-text` finished at 15:22.
The board must not describe that finished pass as running.
The queue still contains `switch-motion`; the mock exposes that inconsistency.
The old board calls handler/23 `not sent`, although its dependency state contains a checkpoint.
The proposed text distinguishes submission from a place in the merge line.

The brief says sdlc holds `merge-once` until this design.
The current run records do not contain that reason or a ready commit for this phase.
After the director records those facts, its `Also` line becomes:

```text
  Merge: merge-once is done. sdlc waits for board-design acceptance before submission; start not recorded.
```

The board must not obtain that sentence from a private conversation or this design document.
The director records the acceptance condition; the board does not decide whether acceptance occurred.

## Reading and layout rules

1. Print the summary before the unit blocks.
2. Sort units by name without case distinctions.
3. Print each unit name once as a bold block title, followed by its summary state.
4. Print `Now` and `Next` in every block.
5. Print `Wait`, `Also`, and `Merge` only when they contain a fact.
6. Wrap text under its label at the available width, with a maximum width of 120 columns.
7. Print multiple tasks or queue items as numbered lists under their label.
8. Preserve every reason, dependency name, queue item, and unresolved merge group.
9. Place an empty line between unit blocks.

The mock uses plain text to show the intended width.
The command emits Markdown blocks and lists, without a code fence or an HTML line break.
Each field is a list item with a bold label.
Numbered task lists sit inside their field item.
Native wrapping stays inside the same block; it never creates another unit row.
The renderer does not use `display_cells.wrapped()` to create continuation rows.
Long identifiers wrap without changing their stored text.

Remove the progress bar, raw session status column, separate worker column, and repeated phase labels from this default board.
Name the worker or director in the sentence that describes its work.
Use a worker's role when its stored name is an internal identifier.
If two workers share a role, include their configured display names.
Technical work IDs remain available in parentheses and dependency references.
Remove a repeated work ID from the start of its title.

Append `Estimated finish <time>` to the task with that promise.
Append `late <duration>` when the promise has passed.
Use `finish time not recorded` when an active task has no promise.
Do not transfer an estimate between phases.
Use `HH:MM` for today and include the date for other days, in the displayed zone.
Keep plan forecasts in the detailed phase report; do not add a second forecast to this board.

## Sources and task identity

These paths describe the inspected checkout at `5971ba136`.
`phase_table.py` and `phase_waits.py` are under `scripts/direct`, not `scripts/production`.

| Fact | Source | Use and limit |
| --- | --- | --- |
| Unit membership | `live_units.read_units`, `session_staff.production_members`, `staff.StaffSnapshot` | Use the same production membership as the current board, including paused and exited members. |
| Selected run | `unit_phases.newest_unit_runs`, `open_unit_runs`, `run_is_finished` | Prefer the staff current run; otherwise use the newest owned run. Respect `reopened`. |
| Plan work | `production_records`, `phase_table.build_plan`, `build_run`, `fallback_plan` | Distinguish completed, open, and unstarted work. An open phase does not prove execution. |
| Current task | `display_cells.open_step`; phase, pass, and activity events | Retain task identity and actor before formatting. The existing two-field `OpenStep` alone is insufficient. |
| Worker activity | One `census.read`; `CensusWorker`, `CensusRun` | Join the worker to its open pass and selected run. A live worker can work while its director is idle. |
| Director activity | `activity_started`/`activity_finished`, director census entry | A busy session alone supplies no task description. |
| Build or command | `CensusSession.shells`, existing `shell_waits`, activity records | A running command can do work without a worker. A resource wait overrides that command's execution state. |
| Wait | `phase_waits.phase_waits`, underlying wait events | Preserve the start, actor, task, kind, reason, and latest holder. The current projection drops several fields. |
| Worker restrictions | `quota_state.no_worker_mode()` | Explain why another worker cannot start. This does not prove that the director works. |
| Queue | `unit_queue.read_items` | Keep stored order and every item. `after` names a dependency; prose does not create one. |
| Dependency | `waits_on.state`, local `origin/main`, owner run | Show evidence about the dependency. `PhaseOpen` alone does not prove that another unit currently works. |
| Merge | `land_queue.land_view`, `land_runs` | Keep direct and carried commits, queue positions, failures, and successful results. |
| Pause or exit | `StaffRow.paused`, `StaffRow.exited` | Use the recorded `by`, `why`, and `since` values. A request can precede the end of current work. |
| Time | Event timestamps, queue `added`, `user_zone` | State what each time measures. Never use file modification time as a wait start. |

Use `(staff ID, run ID, phase instance ID, pass/activity ID)` for task identity.
Use `(repository, commit)` for merge evidence and the merge item identity for grouping.
Use stable staff IDs for joins; names serve display only.

Match an open pass's `worker_id` to `CensusWorker.staff_id` first.
Check the worker's parent against the unit and the pass against the selected run.
Use the census assignment as supporting evidence, not the only join.
The current board-design pass has a `worker_id`, while its census worker has `NoPass()`.
This join explains why the designer currently disappears from the phase worker cell.
For older events, allow a unique worker-name match within that unit and run.
Ambiguous matches produce `worker assignment not recorded`.

An explicit assignment conflict produces an unknown task state; it does not move a worker between phases.
A finished pass cannot supply current work.
Keep post-phase activities, such as a director's document update, until their matching finish event.
`progress_history._start_activity` already supports activities after a phase finishes.
It requires an active run with a previously started phase.

## Exact state text

The following rules describe tasks first.
The summary rules below combine their states into one unit state.
`<work>` means the recorded human summary, followed by its work ID when useful.
`<since>` means the actual wait start, or the literal `start not recorded`.

| Condition | Board text | Selection rule |
| --- | --- | --- |
| Worker executes a matched open pass | `<worker> writes <work>.` | Live busy worker, matched pass, no blocking wait for that task. Use the role verb below. |
| Director performs a recorded activity | `The director <activity>. No worker is needed for this step.` | Open activity names the director; census confirms activity; no blocking wait applies. |
| Director reviews between fix attempts | `The director reviews the fixes for <work>. No worker is needed for this step.` | Explicit review activity; never infer review from a finished fix pass. |
| Fix worker executes | `<worker> fixes the review findings for <work>.` | Open fix pass with live worker evidence. Omit historical fix counts from the default board. |
| Own command executes | `<command description> runs for <work>.` | Live shell or detached run with task ownership; no resource wait blocks it. |
| Own command waits for memory | `<work> needs build memory in use by <holder>; since <time>.` | Open wait records memory as the resource. Resolve the latest holder through staff. |
| Own command waits for a build slot | `<work> waits for a build slot used by <holder>; <since>.` | Open wait records a slot, rather than memory. Do not guess from a broad wait kind. |
| Resource holder is absent | `<work> waits for <resource>; holder not recorded; <since>.` | The wait is valid but lacks a holder. Preserve any recorded reason. |
| Worker waits on its own running command | `<command description> runs for <work>; <worker> waits for its result.` | Treat the executing command as working. Do not count the worker's parent wait as inactivity. |
| Result awaits collection | `<work> finished; the director has not read its result.` | Census run state is `finished_queued`. This does not prove why collection has not started. |
| Child run is missing | `<work> has no running command; reason not recorded.` | Census run state is `missing`; do not present its old label as running. |
| Work awaits another task | `<work> waits for <dependency>: <dependency state>; <since>.` | Explicit current wait, or the first queued item's unmet `after` when nothing current can run. |
| Done work awaits merge | `<work> is done; waiting to merge, <ordinal> in line; queued since <time>.` | Known ready or submitted work matches a merge queue item. First means next to merge. |
| Merge executes | `<work> is done; merging now.` | The live merge record owns or carries the commit. |
| Merge has a recorded hold | `<work> is done. <actor> waits for <condition> before submission; <since>.` | Ready commit and explicit hold identify the owner and release condition. |
| Done work has no submission | `<work> is done; not submitted for merge; reason not recorded.` | A ready fact exists, but no submission or hold explains its delay. |
| Submitted work has no queue entry | `<work> was submitted; absent from the merge line; reason not recorded.` | A checkpoint exists, but no running merge, result, or queue entry explains its state. |
| Merge queue has never been recorded | `<work> was submitted; merge position not recorded.` | A checkpoint exists and `LineUnused` supplies no position. |
| Merge fails | `<work> did not merge: <failure>; <time>.` | Latest failed result remains unresolved and the commit is absent from local main. |
| No current task, next work exists | `<work> has not started; reason not recorded.` | No active task or recorded wait explains the delay. Busy or idle session status supplies no reason. |
| Director busy without a task | `The director is busy, but its task is not recorded. No worker runs.` | Healthy census confirms both facts; no current activity supplies the task. |
| Worker capacity is restricted | `Workers cannot start because no-worker mode is on.` | Recorded quota state is true. Add the director's actual task or its missing reason. |
| Unit pauses or exits | `<actor> paused this unit: <reason>; since <time>.` / `<actor> stopped this unit: <reason>; since <time>.` | Staff hold exists and no current task continues. |
| Session disappears without a reason | `The director session is absent; stop reason not recorded.` | Census scanned its machine and did not find the session. |
| Plan complete and queue empty | `Nothing to do: the plan is complete and the queue is empty.` | All sources are readable; no unresolved task, merge, wait, or activity remains. |
| No plan and queue empty | `Nothing to do: no plan or queued work.` | Known absent plan; readable empty queue; no other work remains. |
| Required source is unreadable | `<source> unavailable; <fact> cannot be determined.` | Preserve all other known facts. Do not treat unreadable data as empty. |

Map `impl` to `writes`, `fix` to `fixes the review findings`, `review` to `reviews`, and `test` to `tests`.
Map `astra-designer` to `writes the design` and `verification` to `runs the checks`.
Use a recorded specific activity when it provides a clearer action.
For an unknown role, use `<worker> works on <work>`; do not expose `<role> dispatch`.

Each active task keeps its own waits.
A wait for one worker must not suppress another worker's current work.
Two concurrent tasks appear as separate numbered items inside one unit block.
`Now` lists executing tasks first; `Also` lists other unfinished tasks and any missing explanations.

The wait projection must preserve `wait_attribution`, `pass_instance_id`, `owner_session`, `wait_kind`, and the holder's resource fields.
Use existing wait and pass finish events to close waits.
Treat `wait_unclosed` as an ended observation with an unknown outcome, not proof that the blocked task resumed.
Deduplicate a shell wait and a phase wait only when they identify the same task and resource.

## Next work

`Next` always describes work after the current task or tasks.
A blocked first queue item remains in `Next`; `Now` explains why no task starts.
The summary counts that unit once, regardless of the number of queue dependencies.

| Input | Exact presentation |
| --- | --- |
| Queue item without `after` | `<summary>. No dependency recorded.` |
| Queue item with a satisfied dependency | `<summary>. Ready: <dependency> is on the local copy of main.` |
| Queue item with an unmet dependency | `<summary>. After <dependency>: <state>.` |
| Dependency is not found | `<summary>. After <dependency>: dependency not found locally.` |
| Pending plan work | `Plan next: <first title> (<ID>); then <remaining IDs>.` |
| Queue and plan both contain work | `Queued:` list, then `Plan next:`; `Order between queued work and plan work is not recorded.` |
| Queue item identifies current work | `<ID> remains in the queue, but it has already started.` |
| Empty queue and complete plan | `Nothing queued. The plan is complete.` |
| Empty queue and no plan | `Nothing queued. No plan is assigned.` |
| Empty queue with only current plan work | `Nothing queued. The remaining plan work is already current.` |
| Queue read fails | `Queue unavailable; next work cannot be determined.` |

Keep the queue's existing first-item order.
Do not skip its blocked first item or invent precedence between the queue and the plan.
A director can state an explicit next selection through the recording proposal below.
That statement reports the director's choice; it does not change scheduling policy.

Translate dependency states into their evidence:

| Existing state | Display |
| --- | --- |
| `OnMain` | `on the local copy of main` |
| `NotOnMain` | `absent from the local copy of main`, plus known merge state |
| `PhaseOpen` | `started; current activity not recorded`, unless the dependency's observed task supplies a stronger fact |
| `PhaseSent` | The known merge state, or `submitted; absent from the merge line; reason not recorded` |
| `PhaseNotStarted` | `not started` |
| `NotFound` | `dependency not found locally` |

A successful merge result does not prove that this checkout has received the commit.
When local main lacks it, show `merge completed <time>; absent from the local copy of main`.
Do not claim a remote branch lacks work from this local observation.

`waits_on.phase_state` currently treats a started phase without a checkpoint as `PhaseOpen`, even after some finishes.
The reader must inspect the latest instance and its terminal events before calling the dependency started or complete.
An older merge of the same phase ID must not satisfy a dependency on a reopened instance.
It must also distinguish a failed Git read from a proven negative ancestry result.

## Merge groups

Keep the commit grouping introduced by `5971ba136`; replace its presentation in the first phase row.
Print each merge group once in the unit's `Merge` section.
Include ready phases even when they no longer have an open phase row.
Group direct and carried commits by the actual queue item or running merge operation.
Do not combine unrelated running operations merely because both run now.

```text
  Merge: workers-why, checkpoint-wait, and land-line are done; merge together, 2nd in line.
```

The sample contains an existing technical phase ID; the action remains `merge`.
A carried commit names the other unit and work that includes it when that context matters.
Each unit in a shared batch gets one description of its own work and the common queue position.
A previous failed attempt stays after the current queue position as `Last attempt failed: <reason>; <time>`.
Without a recorded queue timestamp, use `queue entry time not recorded`.
Keep unresolved failures even when their phase has closed.
Remove resolved failures when local main contains their commit.
Successful merges leave the current board unless a local dependency check still needs that result.
Remove the daily merged count entirely.

## Summary: one state per unit

Let `U` contain the distinct staff IDs in the phase board's displayed membership.
Style-ticket rows outside this board do not enter `U`.
The summary states its scope through the adjacent phase board; `total` means `|U|`.

Select the first applicable rule for each unit:

| Order | Bucket | Rule |
| --- | --- | --- |
| 1 | `working` | At least one identified task executes through a worker, director, or own command. A merge operation alone does not qualify. |
| 2 | `inactive` | No task executes; an effective pause or stop explains the unit, or complete readable records prove nothing remains to do. |
| 3 | `unknown` | Missing or conflicting current-work evidence prevents a reliable choice among the remaining states. |
| 4 | `waiting on other work` | An explicit current hold requires a named task or resource before current work or merge submission can continue. |
| 5 | `waiting to merge` | Current completed work awaits or undergoes merge, with no earlier rule applicable. |
| 6 | `waiting on other work` | No current work can proceed; the first queued item has a known unmet dependency. |
| 7 | `unknown` | Work remains, but the records explain neither execution nor its delay. |

An unstarted task with an unresolved reason invokes rule 3 only when it could change the unit's primary state.
A known executing task still invokes rule 1 when another task lacks a reason.
The block must show the missing reason for that other task.
An unrecorded delay reason does not change a proven merge wait into an unknown state.
An unexplained busy director prevents the reader from declaring that nothing remains to do.
Future queue waits do not change a unit that already works or awaits its own merge.
A recorded merge hold before submission invokes rule 4; normal queue order invokes rule 5.
An effective stop overrides a passive wait, while a worker finishing after a pause request still invokes rule 1.

Always show the three requested buckets, including zero counts.
Show `inactive` and `unknown` only when nonzero.
`inactive` covers a proven pause, stop, or absence of work; the unit block states which one.
`unknown` covers unavailable or missing evidence, not a guessed wait reason.

Every unit reaches a rule because rule 7 has no further condition.
The first matching rule assigns exactly one bucket.
Therefore, the bucket counts sum to `|U|`.
Count neither task rows, merge groups, nor queue items.
The mock gives `2 + 0 + 2 + 1 = 5`.
admin-console counts as working despite its pending image-zoom merge.
unit-board counts as working despite its unfinished explanation for merge-once.

## Board vocabulary

This list defines the fixed labels and state terms.
Task descriptions and names come from recorded work; they are not additional status codes.

| Word or phrase | Meaning |
| --- | --- |
| Unit | One named team with a director and assigned work. |
| Working | At least one task currently executes. |
| Waiting to merge | Completed current work awaits or undergoes merge into main. |
| Waiting on other work | A named task or resource prevents the unit's next required action. |
| Inactive | The unit has a recorded stop, pause, or no remaining work. |
| Unknown | The records cannot explain the current state. |
| Total | Number of distinct units in this board. |
| Now | What executes, or why no task executes. |
| Also | Other unfinished current work, including missing reasons. |
| Wait | The condition that prevents a named task from continuing. |
| Merge | Completed work that still needs integration into main. |
| Next / queued | Assigned work that has not started. |
| Plan next | The first remaining plan phase, followed by later phase IDs. |
| Phase | One named part of a unit's plan or recorded work. |
| Director | The session that manages the unit and can perform work itself. |
| Worker / designer | A session assigned a specific task. |
| Review findings / fixes | Problems found during review / changes that correct those problems. |
| Checks / release build | Verification commands / a build with release settings. |
| Build memory / build slot | Memory allocation / permission for another build to execute. |
| Holder / in use by | The named session or unit that currently occupies the resource. |
| Busy | The census observes session activity; this alone does not identify its task. |
| Done | The unit recorded completion; merge may still remain. |
| Submitted | The unit sent a ready commit for merge. |
| 1st in line | The next queued merge, after any merge already running. |
| Merge together | One merge item includes the listed work. |
| After / dependency | Work that must reach local main before that queued item can start. |
| Ready | The recorded dependency is satisfied; this does not mean work has started. |
| Main / local copy | The shared integration branch / this checkout's stored `origin/main` reference. |
| Since / queued since | Start of the continuing wait / time the merge entered its queue. |
| Estimated finish / late | Recorded target time / elapsed time after that target. |
| Not recorded | The source contains no fact that answers this point. |
| Unavailable | The board could not read the source. |
| Nothing to do | Readable records contain no remaining assigned work or unresolved merge. |

Do not display `in work`, `fix rounds`, `memory admission`, `held by`, or `non-phase work` as status wording.
Do not display internal role identifiers as task descriptions.
Use `merge` for the user action, regardless of existing Python module names.

## Missing facts and the smallest recording changes

These changes add facts at their source.
The renderer never records a wait, clears a queue, or infers a process choice.

| Gap | Smallest change | Writer and time |
| --- | --- | --- |
| Director task between worker attempts | Use existing `start-activity` and `finish-activity` with specific review text. | Director, when review starts and ends. A finished fix pass is insufficient. |
| Why phase 30 has not started | Extend existing wait events with explicit unit scope and a target work reference. | Director, when it defers the next task; close when the reason ends. |
| Ready but unsubmitted merge-once | Add a `work_ready` event with phase instance, commit, and completion time. Record its hold with a wait. | Director, when it declares the commit ready and receives the hold. |
| Hold depends on acceptance | Record the responsible actor and exact release condition in that wait. | Director or authorized showrunner, when the hold begins or changes. |
| Wait projection loses task identity | Preserve existing wait ownership, resource, and kind fields in `phase_waits`. | Reader change only; existing wait producers already supply these facts. |
| Designer omitted from Workers | Join the pass's existing `worker_id` to the census worker. | Reader change only; no new agent launch or census process. |
| Queue text repeats current work | Add optional `work_ref` to queue items; bind it to a phase or stable queue item ID. | Queue writer when adding work; director when starting it. |
| Queue delay has no start time | Add `added_at` to new queue items; record an actual wait start when the director begins waiting. | Queue writer and director respectively. Never treat queue age as wait duration. |
| Long titles contain implementation instructions | Add an optional human `summary` to phase-start records and queue items. | Task author when assigning work; old records retain their full existing title or text. |
| Queue and plan precedence is absent | Add an optional explicit next-work reference to the unit-scoped declaration. | Director when selecting the next task. Absence remains visible. |
| Reads collapse failures into empty values | Return absent, readable, and unreadable states separately from relevant readers. | Reader change; classify uncertain units honestly. |

A summary describes the user-visible change in one sentence.
It must not remove the named dependency, acceptance condition, or failure from the separate state text.
The board never generates summaries with a model during rendering.
Legacy titles and queue text remain readable through wrapping.

The proposed unit-scoped wait reuses the event log, clock, wait ID, reason, holder, and closure rules.
It adds `scope=unit` and `target_work`; it does not borrow the last phase instance.
The target can identify an unstarted plan phase or a queue item.
The writer records `owner_staff_id`, the declaring actor, and the active run.
The declared reason must name the condition that permits work to continue.
An explicit `next_work` reference is optional and does not itself start work.
For a unit without an active run, queue dependency data still works; other explanations remain unrecorded until the director starts its run.

The writer closes a unit-scoped wait on explicit release, target start, target removal, replacement, or run completion.
Run replacement prevents a previous run's declaration from describing the new run.
A fulfilled dependency closes its displayed block even before the director records the matching finish.
The renderer preserves the discrepancy as `dependency satisfied; next task has not started` if necessary.
It does not silently claim that a ready task executes.
Actor-only holds require an explicit release; elapsed time never supplies release.

`work_ready` is evidence of readiness, not phase acceptance or a merge submission.
A later work start for that phase invalidates its earlier readiness until another ready event or checkpoint appears.
A hold ends through an explicit release or its recorded condition; another phase's start does not clear it.
Existing `checkpoint` events remain valid evidence of submission.
Existing explicit completed phase records can describe completion, but cannot invent a ready commit or its hold reason.

Legacy queue items have no reliable work identity or start time.
The renderer can flag an exact leading phase ID as a possible duplicate, but must not delete or suppress the item.
The mock's switch-motion warning uses its explicit leading ID; it does not infer the prose dependency.
New `work_ref` values permit exact attribution after a recorded start.
Until the queue writer removes a started item, show it as a queue inconsistency rather than future work.
Queue mutation remains the queue writer's responsibility.

## Read cost and correctness

The inspected live render took 2.335 seconds on this checkout.
That is a baseline measurement, not evidence that the current command meets the requested one-second target.
The implementation must measure the source reads separately from formatting.

1. Read staff and census once per report.
2. Reuse the existing history snapshot and cached run, plan, queue, and dependency reads.
3. Build task states once, then derive both text and counts from those states.
4. Reuse one merge view and retain queue timestamps during that read.
5. Cache ancestry checks by repository, local main commit, and target commit.
6. Preserve the existing bounded census and width reads.
7. Keep all new reads local; perform no fetch, message exchange, or per-row process query.

`LandView` currently discards the queue's `added` timestamp; retain it without rereading the queue.
Retain the running merge operation identity to prevent unrelated groups from combining.
The board needs no new Git operation for grouping.
Missing queue files and malformed queue files must produce different states.
The same distinction applies to plans, history, merge records, and dependency checks.
One source failure must not erase other units.

Measure three warm renders with the same five-unit fixture and representative history size.
Target about one second for the live five-unit board; report the actual median and slowest read.
Do not claim that target from a formatter-only benchmark.
If existing census latency prevents the target, record that limit separately before expanding this change's scope.

## Build phases and estimates

These are implementation effort estimates, not calendar promises.
They assume one implementation worker and focused review.
The unit director sets the ETA from worker availability and required checks.

| Phase | Size | Work | Completion evidence |
| --- | --- | --- | --- |
| 1. Task evidence | Medium; 3–5 hours | Create per-unit task states; join workers by ID; preserve wait ownership; distinguish failed reads. | Cases cover the designer, director review, post-phase activity, missing workers, and partial census failure. |
| 2. Missing reasons | Medium; 3–5 hours | Add unit-scoped waits and readiness facts; extend queue identity, summaries, and timestamps. | Cases cover defer, release, target start, replacement, reopening, and legacy records. |
| 3. Board output | Medium; 2–4 hours | Render unit blocks, merge groups, next work, exact state text, and the disjoint summary. | A fixed five-unit example answers all three user questions at 120 columns. |
| 4. Integration | Small; 1–2 hours | Update command and as-built documentation; exercise live reads; measure latency. | Focused tests pass; output and summary agree; timing results identify any remaining limit. |

Total estimated effort: 9–16 hours.
Phase 3 depends on phases 1 and 2.
Phase 4 follows phase 3.
The estimates include focused tests and review corrections within each phase.
They exclude a redesign of census collection or scheduling policy.

The acceptance cases must cover these behaviors:

1. Count a working unit once despite several phases, merge groups, or blocked queue items.
2. Explain a director review without claiming a worker runs.
3. Mark a missing phase-start reason explicitly instead of displaying `nothing open`.
4. Name a build resource and its latest holder without resetting the wait start.
5. Show the active designer through its existing worker ID.
6. Keep a held ready commit distinct from work already submitted for merge.
7. Preserve shared merge groups, positions, and unresolved failures.
8. Preserve all queue items and every pending plan phase.
9. Keep unknown data distinct from empty work.
10. Keep each unit within one visible block at 80, 120, and 180 columns.

The director must confirm who records the phase-30 deferral and the merge-once release condition.
This design does not choose either reason or alter either approval process.
No other product choice is required to implement the proposed board.
