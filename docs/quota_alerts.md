# Quota alerts: the receiver's protocol

Natedev's agent-notes job checks Claude and Codex weekly readings. Its notes and quota parts have separate process limits in `config/agent_notes.toml`; quota uses the notes part's saved readings. Account notes have numbered labels, role lists, live-session counts and a `## Live sessions` table; an account is in use when it has a role or a live session.

## Claude: what happens next

Use the Accounts page's `next` control to set the next move for a Claude group.
The page uses the ordered move log on natedev.
See `docs/accounts.md` for the CLI, holds, and groups with more than one move.

At 2% weekly or below, an in-use Claude account with an uncovered group sends one phone reminder per episode.
For example, the title is “claude3 has 2% left with no move”.
Both next-move reminders use the full account labels and show positive quota below 1% as `<1%`.
The action is “Set a move on the Accounts page”.
The reminder has no body.
Only entries that target another account and have readable role history count as coverage.
Entries that cover every group on the account suppress the reminder.
Accounts with only ungrouped sessions send no reminder.
A failed delivery retries at the next check.
A successful delivery ends retries for the episode.
Missing readings preserve the episode.
A known recovery or reset closes it silently.

Natedev sends both Claude quota pushes.
The quota check forecasts when each in-use Claude account will run out of weekly usage.
It divides the weekly percentage left by the recent rate and adds that duration to the reading's time.
The forecast needs a positive rate, a recent reading, and a known future reset.
The quota check sends `quota.claude_expiry` when the forecast is before the reset and fewer than `claude_expiry.alert_hours` away.
Scheduled moves suppress the push when they cover every group on that account.
Consumed moves give no coverage.
The push title is `<account> runs out <time>`, with the forecast time in the user's zone.
Its action is `Type /agents move <role> <account>`, and it has no body.
The repeat interval starts at the attempt time and uses `claude_expiry.repeat_hours`.
Both keys are in `config/showrunners.conf`.
The lead time is 12 hours and the repeat interval is 2 hours.
During silence, the quota check skips the expiry push and saves no attempt.
The first quota check after silence uses the current forecast.
A clear send failure removes the attempt so the next check can retry.
An unconfirmed result keeps the attempt until the repeat interval ends.
A forecast outside the alert window, a missing reading, or scheduled coverage suppresses the push.
The next weekly window has its own repeat interval.
The expiry template requires current approval before sending.

Claude sends no weekly session alert, 30-minute repeat, showrunner relay, `Quota restored:` notice or instruction to start no new work. `/quota_ack` and its acknowledgement state are removed. `/quota_refresh` still refreshes readings when the user reports a reset; routing restoration is recorded in the moves journal without a session notice. A scheduled move consumes its entry when its group selection is recorded, keeps the usual safe shutdown/showrunner notices, and sends no phone alert.

## Token notices

The quota check sends one `quota.token_expiry` reminder per token in each configured window.
The default windows start 30 and 7 days before the end date in the user's time zone.
The key `token_expiry.lead_days` in `config/showrunners.conf` contains the day counts as a space-separated list.
Each `token_expiry.<name>.ends` key supplies a token's end date as `YYYY-MM-DD`.
The Mac fallback token gets its estimated end date from its metadata.
If a check misses a window, it uses the current window.
A missing or invalid date adds an anomaly to the log and leaves other reminders due.
Saved Mac expiry attempts still apply to the same token and window.
If two saved attempts refer to the same reminder, the quota check keeps the delivered attempt.

A Mac relay login rejection sends the separate `quota.mac_token_login` reminder.
A later successful relay suppresses the reminder for an earlier rejection.
For the same token, a new reminder requires a successful relay after the claimed rejection.
A check for an older token keeps the claim for a newer token.
Both reminders say “Read the Accounts page.”
Both Mac reminders require a Mac observation from the last 24 hours.

During silence, the quota check skips these reminders and saves no attempt.
The phone sender refuses these reminders if silence begins after the quota check.
The first quota check after silence uses current data.
A clear send failure removes the attempt so the next check can retry.
An unconfirmed attempt can retry after 24 hours if the reminder is still due.
Confirmed delivery ends retries for that window or login rejection.
Each template requires current approval.

## Codex notices

The Accounts page shows the next move for Codex workers.
Use `accounts.py next` to edit the workers queue.
Workers entries have no hold in minutes.
A move to Claude holds until the source account's weekly reset, even if Codex has room earlier.
Entries run once in log order and leave the log after a recorded move.
A refused move keeps its entry.

Above 0% and at or below 2%, Codex can send one phone reminder per episode.
The first pending workers entry suppresses this reminder, even when it targets the current account.
An open scheduled Claude hold also suppresses this reminder.
At 0%, the pass runs the zero-quota check and sends no low-quota reminder.
A pending workers entry or an open Claude hold can stop the default move.
A delivered reminder stays delivered across zero and later readings until the weekly reset.
If the move journal is unreadable, the first workers entry still suppresses this reminder.
The title gives the remaining quota and the default outcome.
For example, “codex2 has 2% left; codex1 next” means workers move to codex1.
It names paid credits only when the account allows them and no other Codex account has room.
With no paid credits and no Codex account with room, the title ends “units work alone next”: no-worker mode starts at 0%.
The reminder says “No action needed.”
The reminder has no body.

A pane worker's proved quota refusal marks its pass refused and keeps it open for redispatch after the account move.
The move preserves its native conversation, pane, and terminal.
The launcher reads the transcript before another submission.

The refusal sends one relay to the director of the stopped worker after the refusal is routed.
The relay names the outcome when it is known and tells the director not to rerun the assign command.
The pass stays open, except when no-worker mode started: then the stop of the Codex workers ends it as canceled.
The outcome is one of these: workers moved to an account, workers wait for a reset time, or no-worker mode started.
The caller supplies `--stopped <session id> <worker>`.
Unknown directors receive no message; a waiting follow keeps its continuation.

Zero quota, credits forecasts, Codex-to-Codex moves and unknown-account refusals write log lines without session delivery.
The alert pass still sends phone pushes and the natedev prompt notice and writes move journal rows.
The alert pass records restoration only in `moves.jsonl` as `routed back` or `routing not restored`.
`/quota_refresh` sends no notices.
Showrunners keep no held quota-alert list.
`always` adds recipients to the running showrunners for Claude five-hour notices, Claude no-room notices, and blocked account move notices.

The Codex `5-hour limit:` notice from `five_hour.py` writes one log line per low spell, with its back-above line also logged. The Claude five-hour notice is unchanged: tell the user in one line and change nothing else. Escalation goes to the phone unless the user types in a terminal within 15 minutes.

A Codex notice gives the reported credits and limit-reset count.
Without an account move entry, low quota above zero switches nothing.
At weekly zero, credits on moves workers to included Codex usage before paid credits.
With credits off, workers may also move to another account using its own credits.
A quota refusal requests a move only when the workers account cannot use its allowance or permitted credits.
Login faults and refusals from other homes never route workers to Claude or start no-worker mode.
Their existing notices stay unchanged.
The launcher reports the serving account, refusal time and known window through `agent_notes.py blocked --window weekly|five_hour|unknown --refused-at <aware ISO time>`.
A later reading identifies the window only when exactly one window is at zero.
The refusal record stays saved when the current allowance or permitted credits keep workers running.

A known 5-hour refusal waits when its reset is in the future and less than 20 minutes away.
One log line says `workers wait for <label>'s 5-hour reset at <time>`.
Exactly 20 minutes, a passed reset, or an unknown window or reset causes a move.
The picker excludes the refusing home and chooses the Codex account with room whose weekly reset is soonest, including credits.
The workers role changes through `swap workers`.
A pane move stops a live agent, then resumes its conversation in the same pane.
An active or refused pass receives one resume message; an idle worker receives no text.
The log says `workers moved from <old> to <new>: <old> refused (<window> limit until <reset>)`.
That follow starts `Continue where you stopped.` before held messages when the new home becomes available or its wait ends.
An exited command leaves its rerun to the director.

When no other Codex account has room, the existing Claude fallback changes assignments and its notice names the earliest reset. A failed move to the picked home also opens that fallback; the notice names the failed move and its cause. Owed assignments return when the selected Codex account can work and its refusal has resolved; assignments changed by the user meanwhile stay as chosen. A refusal from an outgoing account changes no role or assignments. A refusal creates no weekly episode. The natedev session shows fallback changes on the user's next prompt.

Each Codex pass fixes one worker selection, identity and checked home's live reading with its observation time. Spare accounts raise no low alert; making one spare preserves its episode until its own quota recovers or resets. Unknown identity preserves episodes and pending holds.

Codex move advice names its account and says `workers move to <picked> when Codex refuses work`, or `no other Codex account has room until <earliest reset>`. Above zero it uses the picker for advice; at zero the account’s credits setting decides whether workers stay or move.

Codex five-hour markers belong to a home's signed-in account and suppress repeated log lines during a low spell. Cleanup still retires queued Codex relays and escalation holds from older versions when the account changes, including when the selected account has no usage window. Unknown identity leaves that state alone. Claude notices continue independently.

A proved turn refusal carries the account that served the native conversation.
An unknown refusing identity opens no fallback.
A release failure or failed reviewer preparation command is not a provider refusal.
Fallback state distinguishes no fallback, an account-owned fallback, and a legacy fallback with an unknown owner.

`~/.local/state/accounts/moves.jsonl` records each transition under an append lock.
Entries share the time, tool, subject, and stable move id.
A workers role write records `requested`; a proved destination resume records success for that worker.
Pane moves record the request, confirmation, or failure by worker staff id and move id.
The request includes the retained terminal.
Failures, waits, wait completion, no room, and renewed room have separate entries.
Readers skip malformed lines, including during duplicate confirmation checks.
Requests omit an unknown `reset`; waits carry a known reset.
Refusal state stays in `refusals.json`.
A usage store failure writes a log line without preventing alerts.
Advice reads the saved store.

`~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py burn --since <ISO> [--until <ISO>]` reports points spent per observed day for each account and the roles it served. It reads `~/.local/state/agent-notes/readings.jsonl` and `~/.local/state/accounts/roles.jsonl`; before 2026-10-13 00:00 UTC, the login in `~/.claude`'s note label identifies historical showrunner usage. From that cutover onward, readings are labeled by account alone, with no role attribution. Historical unit directory selections resolve to the note label of that directory's login before matching readings. An unreadable historical identity refuses the report; it never picks a note label without one. Role changes split the report, including when unit directors share the account in `~/.claude`. Decreases between paired readings measure spend; an increase in remaining allowance starts a new span, so a weekly refill never becomes negative spend. Window edges and role changes within an observation interval divide its spend in proportion to elapsed time. Unobserved time and the interval across a refill contribute neither spend nor observed days. One reading alone gives no rate. `--until` defaults to now; an ISO timestamp without an offset is interpreted in the user's zone, set in `~/.claude/config/user_zone.conf`. The command reads local data and launches nothing.

## Codex weekly usage at zero

`codex_credit_accounts` in `config/accounts.conf` names the Codex accounts that may spend paid credits. Every other Codex account uses only its included usage. The setting follows the account label, including a numbered account held in `~/.codex`.

At 0% weekly with a future refill, the workers' selected account opens one zero episode. The catalog text is logged once and sent to the phone. A failed phone delivery retries every pass; sessions receive no copy. The separate low reminder remains one phone push per episode; it has no session alert or repeat. A scheduled move to Claude holds until its source reset and prevents the zero policy from undoing the move. A refill, fresh positive reading or selection of another account closes the zero episode silently.

With a workers entry queued, credit notices name the queue, not a fallback; a known refusal reset waits on the current account until that reset. Retries keep the original empty-queue facts and apply queue advice only while a move is queued.

With credits on and no queued workers entry, workers move only to a Codex account with included usage in each window.
A missing 5-hour window means it has not been used.
A weekly window with usage left needs no reset time.
That move sends no alert or move note.
Without included room, workers spend their current account's paid credits.
Each pass checks for included usage; available room causes a move and closes the paid episode without an alert.
A queued move or open Claude fallback keeps control.
The notice names the balance, refill, and destination when credits run out.
With credits off, workers prefer included usage in both windows and the soonest weekly refill.
Only when no account has weekly room can they move to another account's paid credits.
The notice names that choice.
The pane move stops the live agent before it resumes the conversation.
The request records `cause: credits off`, separately from provider refusal.
Without room, or after a failed move, that pass routes Codex assignments to Claude.
Assignment changes are live in the same pass, even while a landing holds main. Main gets them at the next notes pass that finds main free.
The notice names the earliest refill and any failure.
The fallback remains until that refill or the selected account recovers.
Turning credits on permits recovery when the selected account can work.
An account with credits off cannot buy room from its credit balance.

While credits are on, an hourly forecast is logged; only the first hourly forecast also goes to the phone, with phone-only retries on failure. It uses decreases in numeric balances from `agent-notes/readings.jsonl`, skips purchases and divides usage by the observed interval. Less than 30 minutes of observation or a newest reading older than 10 minutes makes usage unknown. Whole-credit rounding may report zero use; a missing observation never means zero use. Unlimited and unknown balances are stated explicitly.

The paid-credits opening notice uses `quota.paid_credits`.
While a workers move is queued, every send and retry uses `quota.paid_credits_queued`.
Workers spend credits until the move runs, or wait if credits run out or are turned off.
The saved notice keeps the ordinary values.
If the queue empties before a retry, that retry uses `quota.paid_credits` again.
Old saved notices are rebuilt from the current account reading.
Both notices give the action to remove the account from `codex_credit_accounts`.
An episode rereads this setting on every pass.
The first on-to-off change requests the credits-off move once, stops forecasts and repeats no opening alert.
No-worker mode starts only when the workers account is exhausted and `accounts.pick` finds no other account with allowance or permitted credits.
Unknown capacity cannot confirm exhaustion.
A failed move alone does not start the mode.
Starting the mode sends the no-capacity notice and stops the Codex workers once.
Capacity recovery ends the mode without a timer or delay.
An off-to-on change is recorded in the episode, so a later on-to-off change acts again.


Replay checks defer entries when their journal cannot be read or decoded. An undecodable final line with no trailing newline is skipped with a log naming the file; an undecodable middle line still blocks. Rows are appended as complete encoded lines. See `docs/accounts.md` for the engine guards and install order.
