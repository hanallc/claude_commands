# Nix shell launch and recovery

A worktree can have `DIRENV_*` variables while its `.envrc` is still blocked.
That process never entered the nix shell: native links can fail with errors
such as `mold: cannot find gbm`. A running Claude process cannot replace its
environment. Launches now prove the shell first; existing broken sessions
restart through the supported shutdown path when idle.

## Launch helper

`scripts/lib/nix_env.py` uses only Python's standard library on Linux and macOS.
`ensure(directory: Path, base_env: Mapping[str, str] = os.environ)` returns one
of three immutable named tuples:

| Result | Meaning |
| --- | --- |
| `Loaded(env, envrc)` | The nearest `.envrc` loaded and the result has a nonempty `IN_NIX_SHELL`. |
| `NoEnvrc(env)` | No ancestor has `.envrc`; `env` is a copy of the caller's environment. |
| `Refused(reason)` | Loading, approval, decoding, or nix shell proof failed. |

It searches the start directory and then its parents. Resolved `.envrc` files
under the default trusted roots `~/rust`, `~/worktrees`, and `~/.claude*` are approved with
`direnv allow <envrc>`. Linked worktrees outside those roots are also approved
when their `.git`, `commondir`, and `gitdir` back pointer prove registration by
a repository under a trusted root, using file reads alone.
Style-sync lint uses `copies.standing`, a numbered copy at `<roots.build>/<repository>/<unit>/<N>`.
Copy trust requires a copy lease whose source checkout is under a trusted root.
The copy's `.envrc` must match the `.envrc` of the recorded commit byte for byte.
The `roots.slots` settings in `build_folders.conf` select the slot root for slot trust.
natedev sets no slot root, so no folder there gets slot trust. The Mac sets one.
Slot trust requires a registered worktree and an `.envrc` that matches `HEAD`.
Forged links still require explicit approval.

Standing copies under the configured copies root (`build_folders.conf` `roots.copies`: `/tmp/claude/copies` on natedev
and on the Mac) at `<pair>/<copy>` also qualify.
Task clones from `copies.py clone` qualify with the same proof. A clone is its task folder, `<scratch root>/<task>`.
The `.envrc` must be a regular file at the checkout root.
`copy_record.record_path` finds a standing copy's record inside the copy.
For a task clone, it finds the parent's record and requires `test_hana_clone: true`.
`copies.py clone` records the checkout HEAD as `source_commit` after its Git reset.
`registered_copy` requires a readable `copy_record.Registered` record.
Its `source_path` must resolve to a directory under a trusted root.
A `copies.py run` snapshot qualifies with the same proof, at any location. Its checkout is `<tool folder>/source`.
`copy_record.snapshot_source` reads the `snapshot-origin.json` binding beside the checkout.
The folder that holds the binding must belong to the user, and no group or other user can write in it.
The binding must name that checkout and a full commit id.
Its `repository` and `source_commit` take the place of the record fields.
A snapshot whose `.envrc` differs from `source_commit`, from a worktree edit or a merge, requires explicit approval.
Git resolves `.envrc` at `source_commit` and hashes the copy's file with `hash-object --no-filters`.
Approval requires equal blob hashes.
Both Git commands run in the trusted source repository without `GIT_*` environment variables.
The copy supplies no Git configuration or filters.
Missing, malformed, unreadable or mismatching evidence requires explicit approval.
Older clones without `source_commit` do not qualify. Copies and clones outside the configured copy locations do not qualify.
`scripts/lib/copy_record.py` uses only the standard library.
Importing it does not reach scratch or herdr.
The helper imports it only when it checks a copy.

A direct child of the scratch root without `.envrc` passes Linux source trust when its readable registered record has `generated_source: true`.

This rule checks only `.envrc`. Files it loads, including `flake.nix`, are not
checked and may differ from the source commit.

Files elsewhere require existing explicit approval;
a blocked-file refusal names the `direnv allow <path>` command. Each direnv
command uses `command_timeout_seconds`, which starts at 600 seconds. `direnv export json` runs in the requested
directory with the supplied base environment. Strings replace variables;
JSON null removes them. Failures retain direnv's last stderr line. The proof
failure is `nix_env: <dir>: .envrc loaded but no nix shell (IN_NIX_SHELL unset)`.
An empty successful export means no variables changed: the caller's environment
is retained and still must supply the `IN_NIX_SHELL` proof.

`nix_env.py ensure <directory>` prints the full environment as one JSON object
and exits 0, or prints one `nix_env: ...` stderr line and exits 1.
`nix_env.py exec <absolute program> [args...]` ensures its current directory and
uses `os.execvpe` with that environment. A relative program is a usage error
(exit 2), preventing a PATH wrapper from finding itself by a bare name.
For `Loaded`, exec sets `NIX_ENV_ENVRC` to the resolved `.envrc` path; for
`NoEnvrc`, it removes an inherited `NIX_ENV_ENVRC`. The `ensure` API and its
JSON output keep their existing environment-diff behavior; only `exec` adds
or clears this provenance marker.
Launchers resolve Claude with `command -v` or the existing absolute-binary
resolver before calling `exec`.

## Launch paths

| Path | Integration |
| --- | --- |
| `herdr/herdr.py` | `launch_agent` checks `ensure(cwd, base_env)` before `open_tab`. It returns `AgentLaunched(pane, outcome)` or a typed `Refused`; refusal creates no pane and starts no agent. |
| `production/add_unit.py` | Unit launches use `launch_agent`. Account moves and watchdog recovery use the shared stop and resume path. |
| `production/add_showrunner.py` | Child showrunners use `launch_agent`. |
| `agents/restart.py` | Record-driven restarts prepare the saved cwd and account environment before `restore_server`, then use `launch_prepared_agent`. Refusal retains the record. |
| `agents/stop_resume.py` | Shared resume runs the account exec command in the session's pane. The exec path prepares the launch shell. |
| `herdr/herdr_sessions.py` | Standalone starts and resumes use `launch_agent`. |
| `direct/claude_worker.py` | Calls `nix_env.ensure` once in the assign process. The settings and the Claude start use the same loaded environment. |
| `agents/agent_exec.sh` | Claude print runs use `exec` after changing to their requested directory. |
| `accounts/accounts.py` | Normal `exec` launches call `launch_shell_environment(cwd)`. It calls `ensure`, restores the caller's HOME, and refuses an unusable shell. `codex update` bypasses account launch. |
| `cli_agent/cli_agent.sh` | Interactive and command Claude launches use `exec` in the caller's directory. |
| `notify/review.py` | Loads the current directory's env before the Claude reviewer starts; refusal returns an unreviewed result. |
| `make_a_worktree/direnv_allow.sh` | Thin, silent-on-success `ensure` call; failure retains the helper's stderr and exit status. `commands/make_a_worktree.md` requires stopping on failure. |

`herdr agent start --help` and the herdr adoption documentation specify that
`--kind claude` selects the executable itself. Prefixing its argument list with
a wrapper would pass that wrapper to Claude as an argument. The integration
therefore passes the loaded environment through the new pane's `--env` values.
Before loading, it removes inherited caller account, pane, Codex and unit
identity variables and the caller's `SHELL`; explicit launch account and unit
marks are then applied. Herdr retains ownership of its pane shell and identity.
Account moves also pass their explicit variable removals through the choke
point; inherited values and direnv exports cannot put those variables back.
The existing `start_agent` APIs retain their signatures and prompt proof.
`PreparedAgentEnvironment` binds a proved environment to its exact directory;
`launch_prepared_agent` opens that directory without another direnv load.

Before tool setup or record access, `verify.sh` repairs a missing nix shell.
When `IN_NIX_SHELL` is empty or unset, it sets `VERIFY_NIX_ENV_REEXEC` and re-execs
once through the sibling `lib/py -S` and `nix_env.py exec`, preserving the absolute
script path, cwd and every original argument, including empty arguments. The
helper owns the nearest `.envrc` search, including ancestors, and runs direnv
export at most once. In the re-run, shell proof triggers one stderr notice;
no `.envrc` means no direnv call or notice. A helper refusal exits nonzero with
only its `nix_env:` line before verification runs. A caller already in the nix
shell takes the original path silently. If the re-run loses shell proof while
`NIX_ENV_ENVRC` still names the loaded file, it refuses with one line instead
of another re-exec. The internal marker is removed before tool setup.
The final command's exit status passes through.
`user_clock` runs `user_zone.py` with `python3 -S`; that helper and `/zone` share
the `stored_zone` configuration reader.

Launch scan: `scripts/lib/test_claude_launch_scan_sources.py` runs the scanner in
`scripts/lib/test_claude_launch_scan.py` over the scripts. The scanner checks Python process calls
whose argv starts Claude, Codex or `herdr agent start`, typed `start_agent`
calls for either agent kind, and shell launch lines. Queries such as `--help`
and `agents` do not count as starts. Each Python scope must call `nix_env.ensure`,
contain a `nix_env.py` command string, or call a same-module guard by bare name.
A top-level function is a guard when its own body calls `nix_env.ensure` or
`loader.ensure` with `loader` assigned from an expression containing
`import_module("...nix_env")`, including casts; calling another guard propagates
that status until no more guards are found. Nested functions and classes are
excluded from each body's scan, and methods and other-module attribute calls
do not establish module guards. Shell scripts must reference the helper.
No file is exempt.

## Agent operation environment

`nix_env.py exec` filters its final environment immediately before `os.execvpe`.
The [model registry boundary](agent-registry.md#agent-environment-boundary) defines which operation values the filter removes.
Helpers retain these values until the agent starts.

## Existing sessions

`scripts/nix_env/heal.py` uses the shared reader for the configured registry and each discovered `~/.claude*/sessions` registry.
An incomplete read returns `Unavailable` if the exact lookup finds no live match.
The healer reports discovery errors and keeps a pending restart when its replacement lookup is incomplete.
The health check reports unreadable paths and exits with status 1.
The healer skips headless print runs and records without a confirmed process start.
`HealSession` carries the session id, pid,
process start, name, cwd, status and config directory. `inspect` distinguishes
`Healthy`, `Broken`, `Unavailable(reason)` and `Gone`; `broken(session)` is true
only for `Broken`. No `.envrc` means no restart is needed. With `.envrc`, Linux
reads `/proc/<pid>/environ`; macOS uses the existing stdlib `ctypes` readers
`whoami/account.py:kern_procargs2` and `procargs2_parts`. They separate arguments
from environment, so a quoted argument cannot supply false nix proof. Identity is checked before
and after the environment read so a reused pid never becomes a restart target.

Run `heal.py sweep`, or restrict it with `--only <session id>`. For each broken
session it checks this machine's build-target hold state, then requires `idle`
or `shell`. Unreadable hold state also prevents restart. It calls `ensure(cwd)`
before calling `stop_resume.stop` for that session. Its existing journal keeps a
pending recovery across a hold. Process liveness proves the original generation
has ended; shutdown records do not decide stop completion. It rechecks the hold
and the environment before `stop_resume.resume`. Account exec loads the shell
through the normal account launch path. Pending recoveries run before new broken
sessions, including sessions that have left the live registry.

from server starts, pane overlays and agent-start CLI processes, including explicit
environment overrides. Later panes cannot inherit another session's proof from
the server. Unit restores through `add_unit` consume the same handoff
at `launch_agent`, matched against the resumed session id in the arguments.
A heal restores only its target, so restoring unrelated
saved panes cannot introduce another direnv load after the final hold check.
Failed stops never authorize relaunch. One bad session does
not abort the rest of a sweep. Nothing is loaded when all sessions are healthy
and no restart is pending.

`~/.local/state/nix-heal/state.lock` excludes overlapping sweeps.
`state.json` reserves a restart before shutdown starts, preserving the pid and
start time. The `restart_interval_seconds` setting starts at 60 minutes.
A session id gets at most one restart in that interval, including failed attempts. A pending stop retains this reservation across holds. Immediately
before relaunch it becomes a durable `launching` reservation; the saved login,
cwd, config directory and original process identity remain in the journal.
The relaunch budget equals twice `command_timeout_seconds`, plus `relaunch_launch_seconds` and `relaunch_confirmation_seconds`.
The initial budget is 1320 seconds. It includes two 600-second commands, 90 seconds for launch, and 30 seconds for confirmation.
A timeout, an error exit, or a failure to start keeps the reservation and sends an alert.
The timer never repeats an unconfirmed launch, even after an interrupted sweep.
Exit 0 also keeps the reservation until the healer confirms a live replacement.
The replacement must have the same session ID, cwd and account directory, a different process identity, and a healthy nix shell.
The healer sends an alert if the replacement is still broken or the launch budget expires without confirmation.
A late healthy replacement clears the pending record without starting another copy.
An unchanged session that is still broken after the guard interval also requires repair.
After the healer observes a healthy session, it can recover the session again after the hour guard.
An unreadable journal prevents automatic restarts.

Every restart, refusal, and change in a skip reason appends one object to
`~/.local/state/nix-heal/log.jsonl`, with keys `time`, `machine`, `session_id`,
`name`, `cwd`, `action`, and `reason`. Times are UTC ISO timestamps. Repeated
unchanged skip reasons do not add lines.

A refused environment or failed recovery alerts the nearest showrunner above the session's staff row.
The message gives the cause, the raw reason, and the recovery commands.
The healer accepts `SKIPPED` as confirmation when the sender already holds or accepted the alert.
The journal records `showrunner_alerted` with the name of the showrunner after a confirmed send.
If the staff record is unreadable, no showrunner owns the session, or the send lacks confirmation, the healer sends Nate the push.
The healer writes the fallback reason to stderr. The timer saves it in its run log.

1. Repair the nix setup on the affected machine. Get Nate's approval before you change `/etc/nixos`.
2. Run `heal.py retry --only <id>` after the repair. It clears the restart guard and notification flag for the next sweep.
3. Run `heal.py escalate --only <id>` if you cannot repair the setup. It uses the saved failure reason to send Nate the push.

Both commands take the same state lock as the sweep. If the lock is busy, run the command again after the sweep ends.
Retry refuses while a restart is pending. The refusal and the showrunner alert give the escalation command.
Escalation requires a saved failure reason and a live session or a pending restart.
If the session is gone, escalation uses the name and worktree from the pending restart.
A successful retry command gives the next showrunner alert a new message key.

The push catalog owns the title, action and text of `nix_heal.failed`.
The push keeps the key `nix-heal-<id>` and the clear command `heal.py sweep --only <id> --check`.
Check mode exits 0 only when the session is live and healthy. It performs no restart.

## Timer registration

There is no SessionStart hook. Phase 6 built one and phase 7 removed it before
it was registered (showrunner ruling, 2026-10-28): the five-minute sweep and the
PATH wrappers cover a session that starts outside its shell. Revisit only if a
failed build from such a session is seen.

The local five-minute sweep is a notifier timer, registered on main on natedev:

```sh
~/.claude/scripts/message/notifier.sh new nix-heal --every 5 --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/nix_env/heal.py sweep"
```

The showrunner still owns the bare `claude` and `codex` PATH wrappers in
`/etc/nixos`; each must call this helper with the real absolute executable.
Accounts owns `accounts.py exec`, which calls `launch_shell_environment`;
the scan recognizes that function as a guard.


## Configuration

The helper and the healer read `config/nix_env.conf` through `scripts/lib/conf.py` when each operation needs a setting.
`command_timeout_seconds` limits direnv commands and Git proof commands.
`trusted_home_folders` and `trusted_home_prefix` select folders that permit automatic approval.
`heal_state_directory` selects the healer journal folder.
Each sweep keeps its lock, state, and log in the folder selected for that operation.
The next sweep reads the folder setting again.
`session_directory_glob` selects session folders below the home folder.
`restart_interval_seconds` controls repeated restarts.
`relaunch_launch_seconds` allows herdr to launch the replacement.
`relaunch_confirmation_seconds` allows command start and record confirmation.
Twice `command_timeout_seconds` plus these two settings sets the relaunch budget.
`alert_timeout_seconds` limits alert commands.
All configured values match the previous code values.
