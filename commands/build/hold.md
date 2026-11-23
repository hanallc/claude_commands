---
description: Clear, drain, or release build work on one machine.
---

`$ARGUMENTS` accepts `clear <machine> <why>`, `drain <machine> <why>`, `release <machine>`, `quiet`, or `status`.
The machine is `natedev`, `mac`, `mac-linux` or `hana-aws`. It has no default.
With no machine or another name, the command prints `Name the machine: natedev, mac, mac-linux or hana-aws.` and returns exit 2.
`hana-aws` returns exit 2 with `<verb> hana-aws is not supported yet` until the queue hold store exists.
A hold acts only on the named machine.

## Hold the Mac

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py drain mac --for '<why>'`.
2. Add `--release-eta HH:MM --zone <IANA zone>` to set the block end. Without it, the block uses the configured `block_hours`.
3. A unit director also adds `--showrunner '<showrunner name>'`. Resolve the name with `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py name <showrunner name>`.
4. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py release mac` when the work ends.

A `mac` hold uses the Mac block. It makes no second hold for `mac-linux`.
`clear mac` also ends `mac-linux` runs. New `mac-linux` runs are not refused yet: placement reads only the `mac-linux` block until the queue hold store exists.
A `mac-linux` hold blocks only the Linux guest. It stays after a `mac` release.
`drain mac` ends no run. Running Mac work and CI Mac jobs finish first, and the free message arrives when that work ends.
`clear mac` blocks the Mac, then ends each run with a live claim on `mac` or `mac-linux`.
The run stops as refused, and its stderr gets `ended: mac cleared by <owner> — "<why>". A Mac run cannot go back to its lines yet.`
If CI's Mac switch cannot be turned off, the block stays and `clear mac` still ends the runs. It then prints the CI advice and returns exit 2.
If the block request is refused (for example a release time past `block_max_hours`) but this holder already has a block for the same purpose, `clear mac` still ends the runs and prints the refusal.
The block admits the holder's claims and refuses other sessions' new Mac work.
It disables CI's Mac switch while pending or active. New CI runs skip macOS and remain green.
Release restores CI's Mac switch only if this block turned it off. It lists skipped runs by branch and their re-run commands.
Repeat the drain to renew the block. A block stays in force after expiry until its record is removed.
If expiry cannot restore CI, run `github-warmup`, then `release mac`. The failed restore keeps the block.
`--keep` is not supported for the Mac.
Run Mac holds with the sandbox off. They write state, call `gh`, and start the block watcher.

## Clear natedev

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py clear natedev --for '<why>' --release-eta HH:MM --zone <IANA zone>`.
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

## Drain natedev

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py drain natedev --for '<why>' --release-eta HH:MM --zone <IANA zone>`.
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
3. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py release natedev` when the work ends.

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
On `No hold cycle: broadcast this release to every session.`, use `/notify_top_level --here`.
On `Not reached, tell them yourself: ...`, tell each named session.
`release natedev --resume` completes pending notices only when no holder file remains.

## Build calls and recovery

Before raw cargo, run `build_hold.py wait`.
Exit 0 permits the build.
Exit 10 refuses it and records a known session for release notice.
Exit 12 means the state cannot be read.
Start builds only after exit 0.
The existing Bash guard uses the same hold and caller check.
Eligible `verify.sh` test steps can use the Mac while natedev remains held.

Inspect any path named by `state unreadable`.
Repair your natedev holder record with `clear natedev --for '<why>' --release-eta HH:MM --zone <IANA zone>`, or remove it with `release natedev`.
`/build:hold release natedev` sets a damaged release record aside.
For a damaged Mac claim, check its holder and running job before repairing the file.
Use `drain mac` or `release mac` for a Mac block.

Installed scripts refuse hold-directory overrides and a changed `HOME`.
Unset the override and use a shell with the user's writable `HOME`.
