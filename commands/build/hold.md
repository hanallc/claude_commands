---
description: Clear, drain, or release natedev build work.
---

`$ARGUMENTS` accepts `clear [target] <why>`, `drain [target] <why>`, `release [target]`, `quiet`, or `status`.
The shared `config/build_targets.conf` sets `default_target=natedev`.
Only natedev supports `clear`, `drain`, and `release` in this delivery.
Mac, AWS, and `all` return exit 2 without changing any target.
Use `/build:mac block <why>` and `/build:mac unblock` for the Mac.
Clear and drain require Nate's explicit request. Pass his exact words with `--user-words`.

## Clear existing work

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py clear [target] --for '<why>' --user-words '<exact request>' --release-eta HH:MM --zone <IANA zone>`.
2. Add `--keep <pid>` for each run that must continue.
3. Use `YYYY-MM-DDTHH:MM` instead of `HH:MM` for a later date.
4. Send the printed notice with `/notify_top_level --here`.

Your fixed staff ID owns the hold.
Repeated calls add checked PID and start-time pairs to the same keep list.
The command stops existing queued gates and running work outside the allow rules.
It keeps the sole holder's work, explicit keep entries and their children, user terminal work, and shared sccache work.
Live Mac or AWS claims also keep their runs.
Unreadable claim records prevent enforcement until those records become readable.
The release notifies stopped sessions.
New natedev work waits until release.
CI work that the service cannot stop continues if already admitted.
Other CI requests stay queued until release.

## Drain existing work

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py drain [target] --for '<why>' --user-words '<exact request>' --release-eta HH:MM --zone <IANA zone>`.
2. Add `--keep <pid>` for an additional run that must continue.
3. Use `YYYY-MM-DDTHH:MM` instead of `HH:MM` for a later date.

The command keeps CI workers, builds that the enforcer could stop, and outermost gates that hold work.
A gate holds work when it has a cargo child, an admission reservation, a live Mac or AWS claim, or a remote waiter.
It publishes the hold and repeats discovery before starting the enforcer.
The second discovery adds only processes whose boot-relative start ticks do not exceed the publication bound.
A concurrent release ends the command without restoring the hold.
Kept runs finish.
A gate that only waits in the queue is not kept: it waits for the release like new work.
New natedev work waits until explicit release.
Each 60 seconds, the command prints `draining: N runs remain`.
Completion prints `drained; natedev stays held until release` and returns 0.
Ctrl-C returns 130 and retains the hold.

## Status and release

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py status` to inspect the hold.
2. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py quiet` before a test that needs quiet builds.
3. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py release [target]` when the work ends.

Status derives `draining (N runs left)` or `held` from live keep entries.
It names each queued or running CI job and the time that state began.
Clear and drain require a future release time in the named zone.
At that time, status adds `overdue` and the enforcer sends the holder one message.
The hold stays in force until release.
Release the hold or repeat clear or drain with a later ETA to end the overdue state.
`quiet` waits up to ten minutes for every user's cargo and compiler processes to end.
Load alone does not delay it.
Exit 1 names remaining builds by user.
Success prints the load and the five processes with the highest current CPU use.
Use a detached run for this wait.

Release removes the caller's holder file before recording delivery state.
Another holder keeps the target held.
The enforcer exits after the last holder file disappears.
Release skips a recipient when it confirms that both the staff row and the session are absent.
Release lists a missing session with a staff row under `Not reached`.
The sender skips closed Codex workers with no route. Their next assignment resumes them.
Release sends a restart notice to the original session when the stop is confirmed and the saved evidence matches.
If that session is closed or gone, release tries the director.
An unsent restart notice stays pending while its evidence is valid or unreadable.
Release removes invalid restart notices from the pending record and reports the count.
On `No hold cycle: broadcast this release to every session.`, use `/notify_top_level --here`.
On `Not reached, tell them yourself: ...`, tell each named session.
`release --resume` completes pending notices only when no holder file remains.

## Build calls and recovery

Before raw cargo, run `build_hold.py wait`.
Exit 0 permits the build.
Exit 10 refuses it and records a known session for release notice.
Exit 12 means the state cannot be read.
Start builds only after exit 0.
The existing Bash guard uses the same hold and caller check.
Eligible `verify.sh` test steps can use the Mac while natedev remains held.

Inspect any path named by `state unreadable`.
Repair your natedev holder record with the clear command above, or remove it with `release`.
`/build:hold release` sets a damaged release record aside.
For a damaged Mac claim, check its holder and running job before repairing the file.
Use `/build:mac block` or `/build:mac unblock` for a Mac block.

Installed scripts refuse hold-directory overrides and a changed `HOME`.
Unset the override and use a shell with the user's writable `HOME`.
