# Hook dispatch with forked children

**Status:** as-built (build-followups-efficiency phase 4, 2026-10-28).

## What it does

Claude Code starts one process per registered hook command. Before this
change, a Bash call started 6 Python interpreters for PreToolUse and 2 for
PostToolUse, and a Stop started 4. Each interpreter pays its start-up and
import cost again.

`scripts/hooks/hook_dispatch.py <event>` starts one Python interpreter for the event.
The events are `pre_tool_use`, `post_tool_use` and `stop`.
The dispatcher reads stdin once and forks one child per matching hook in table order.
The decisions, the text to Claude and the user, and the exit codes stay the same.

## The tables

Each row is a hook file in `scripts/hooks/`, the tool-name matcher it had in
`settings.json`, and its timeout (600 s unless stated). The retained hooks keep their order.
push-alone denies a Bash call that holds `git push` with any other command
(rca 261114-1). An ask rule such as `Bash(git push * -f *)` matches across
the whole compound command, so the deny comes before the user gets a prompt.

| Event | Hooks, in order | Matcher |
| --- | --- | --- |
| `pre_tool_use` | heredoc-guard, tool-output-read, showrunner-move-guard, change-review-gate, checkpoint-doc-gate, push-alone (10 s) | `Bash` |
| | copy and scratch policy (`scripts/copies/copy_guard.py`, `guard_timeout_seconds` in `config/copies.conf`) | `Bash\|Write\|NotebookEdit` |
| `post_tool_use` | basedpyright, fn-length | `Edit\|MultiEdit\|Write` |
| | context-usage (message off in `config/context_usage.conf`) | all tools |
| | push-delivered | `PushNotification` |
| | sendmessage-miss | `SendMessage` |
| `stop` | compaction-marker-clear, conversation-pause, context-deferral (10 s), herdr_tab_title | — |

`settings.json` has one dispatcher entry per event:

- PreToolUse: matcher `Bash|Write|NotebookEdit`, timeout 3050 s.
- PostToolUse: no matcher, timeout 2430 s.
- Stop: no matcher, timeout 1850 s.

Each outer timeout covers the largest sum of hook budgets that match one tool.
It also covers 30 s of dispatcher overhead.
The PostToolUse and Stop timeouts retain 600 s each from the retired hooks.

`scripts/hooks/test_hook_dispatch.py` checks that the tables match
`settings.json`: a dispatched hook file must no longer appear as an entry of
its own.

`stop-direct-continue.py` has a separate Stop entry with `asyncRewake: true` and a 60 s timeout.
It runs after the turn ends.
Exit 2 sends its stderr reason to the agent in a system-reminder.
The user sees only "Continuing the unit run".
The hook wakes above the threshold only when the final line differs from the last wake.
The hook permits a stop when the final status matches `paused_status_pattern`.
Holding and blocked prefixes alone do not prevent the wake.
A merge unit stops without a wake when it has no worker run, active worker dispatch, background task, or queued message.
An own detached run supplies a later completion notice and does not count as merge work.
An open recorder phase or an old worker status file alone does not prove that the merge unit has work.
The hook reads the staff unit type, active dispatches, live detached runs, and CLI background tasks.
It reads queued messages for the fixed staff ID and the current session without consuming them.
A merge unit with this work keeps the wake above the threshold.
Unknown state without work permits the stop and reports the read errors in one anomaly line.
The detached-run scan skips each damaged record, reports its path, and retains other live runs.
The scan retains folder read errors; the list readers raise read errors, and an absent folder counts as empty.
Other directors keep the existing background-task and detached-run guards.
Declared waits and paused status lines still permit a stop.

These hooks stay outside the dispatcher, as their own entries:

- the shell hooks: `pre-tool-use-build-hold-guard.sh` (Bash),
  `berth_pre_edit.sh`, `berth_post_bash.sh` and `pre-tool-use-brp-launch-gate.sh`;
- `pre-tool-use-push-gate.py` (PushNotification, 120 s);
- the SessionStart, SubagentStart, PreCompact, PostCompact and UserPromptSubmit hooks.

The random-ack PostToolUse entry is removed (user, 2026-10-28 11:59 EDT).
`post-tool-use-random-ack.sh` stays. Its header names the `settings.json` entry
that turns it on again.

## How a hook runs

- The copy policy child calls the entry point from the module that supplies its budget.
  Each other child loads its hook as a fresh `__main__` module through importlib's bytecode cache.
  The child has its own input, output, argv, environment, working directory, import path and signal state.
- The child reports its exit code, stdout and stderr to the dispatcher through a pipe.
  `SystemExit` supplies the exit code. Hook files can still run directly as scripts.
- The dispatcher enforces each deadline. It kills and reaps a child that exceeds its budget.
  The dispatcher discards that child's output and continues with the next hook.
- The dispatcher uses no signal handler. Each hook runs with its own signal state.
- A separate worker has 10 seconds to preload the shared libraries and report readiness.
  If the worker misses the deadline, the dispatcher kills and reaps it. The dispatcher then runs the hooks without shared imports.
  The preload worker waits for approval before it starts a hook.
  The worker checks its connection before each hook. If the dispatcher exits, the worker stops scheduling hooks.
- The copy policy child has a separate 10-second deadline to read its configured budget.
  The dispatcher applies the hook's usual output rules to diagnostics from this read.
- The preload worker pauses garbage collection during shared imports and restores the previous setting.
  The process that forks hooks freezes its objects to reduce page copying during child garbage collection.
  Imports made in one hook child end with that child.

## How the results combine

The full rules are in the module docstring. In short:

- Any exit 2 wins. stdout is dropped, and stderr is the stderr of the exit-2
  hooks in table order, joined by a blank line.
- Otherwise the dispatcher exits 0 and merges the JSON of the hooks that
  exited 0:
  - `permissionDecision` by precedence deny > defer > ask > allow, with the
    reasons of the winning hooks joined;
  - every `additionalContext` joined in table order;
  - `systemMessage` texts joined;
  - any `continue:false` wins, with its `stopReason`;
  - a PostToolUse or Stop `decision:block` wins, with the reasons joined.
- A hook that exits with another code, or prints invalid JSON, adds a
  `systemMessage` line that names the hook and its stderr.
- Where the Claude Code docs state no rule (simultaneous stderr, reasons,
  `updatedInput`, unknown keys), the docstring writes down the policy.

## Compaction start markers

`pre-compact-marker.py` runs separately because session hooks stay outside the dispatcher.
Claude's `PreCompact` registration passes `--family claude`.
The managed Codex `PreCompact` handler passes `--family codex` and uses matcher `*`.
Both commands read `session_id` from the hook input.
Codex 0.167.0 supports `PreCompact`; its common input includes `session_id`.
See the [Codex hook reference](https://learn.chatgpt.com/docs/hooks).

The hook writes `~/.local/state/compaction/<session_id>.json`.
The file has exactly three fields: `session_id`, `family`, and `started_at`.
`family` is `claude` or `codex`.
`started_at` uses ISO 8601 with a UTC offset.
The atomic file helper writes a temporary file beside the marker, then replaces the marker.
A new marker replaces the previous marker for that session.
An exclusive flock on `.lock` covers cleanup, publication, and rollback.
The hook never waits for the lock: when another run holds it, the hook exits 0 and writes no marker.
Each invocation removes marker files older than `retention_hours` in `config/compaction_marker.conf`.
The default retention is 24 hours.
An input or publication error produces no stdout, publishes no new marker, and exits 0.
A cleanup error skips that entry and permits the marker write.
The cargo-handler compares this marker with the last transcript `compact_boundary`.
`compaction_marker.py` shares the marker folder, session id pattern, and `clear(session_id)` function.
The clear function deletes only that session's marker and takes no lock.
Claude clears the marker at Stop, UserPromptSubmit, and PostCompact.
Stop uses the existing dispatcher process.
UserPromptSubmit clears the marker before the conversation-pause work in its existing process.
PostCompact uses `compaction-marker-clear.py` as a separate command.
Codex clears the marker at UserPromptSubmit and PostCompact.
Its UserPromptSubmit shell command exits before Python starts when no `*.json` marker exists.
Codex 0.167.0 lists both events; UserPromptSubmit reports no matcher.
All clear entry errors exit 0 without output.
The next turn clears a marker when compaction fails or the user cancels it.

Claude reads the registration from `settings.json` after deployment.
Install and trust the Codex handler separately for each account home through `CODEX_HOME`.

## Original dispatcher benchmark

Child CPU per hook event, measured by `bench_hooks.py`: 20 runs per side,
`settings.json` before and after, the same payloads and transcript.
natedev, 2026-10-28 20:51–20:53 EDT. Load average was 150–230 (other builds
ran), so the absolute numbers are high, but both sides ran in the same
window.

| Event | Before | After | Change |
| --- | --- | --- | --- |
| PreToolUse, one Bash call | 419.1 ms (7 commands) | 218.5 ms (2 commands) | −48% |
| PostToolUse, one Bash call | 157.4 ms (4 commands) | 132.2 ms (2 commands) | −16% |
| **One Bash call** | **576.5 ms** | **350.7 ms** | **−39%** |
| Stop | 317.4 ms (4 commands) | 168.3 ms (1 command) | −47% |

The PostToolUse "before" column includes the random-ack hook (10.5 ms), which
this change removes. An earlier run at a lower load (same day) gave
PostToolUse 143.8 → 104.2 ms and Stop 229.3 → 134.1 ms.
