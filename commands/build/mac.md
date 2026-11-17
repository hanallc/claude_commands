---
description: Hold the Mac, audit packages, or run a queued Mac command.
---

`$ARGUMENTS` is `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.

1. Run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py block --holder '<your session name>' --for '<why>' [--hours N]`.
   A unit director also passes `--showrunner '<showrunner name>'`.
   Resolve the name with `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py name <showrunner name>`.
   The block admits the holder's claims and refuses other sessions' new Mac work.
   It disables CI's Mac switch while pending or active.
   Existing local work and CI Mac jobs finish first.
   The free message arrives when that work ends.
   New CI runs skip macOS and remain green.
   Repeat the listed CI commits after release.
   Repeat `block` to renew it.
2. `unblock` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py unblock --holder '<your session name>'`. It restores CI's Mac switch only if this block turned it off, then lists skipped runs by branch and their re-run commands. A block remains in force after expiry until its record is removed. If expiry cannot restore CI, run `github-warmup`, then `/build:mac unblock`; the failed restore keeps the block.
3. `status` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py status`. It settles block state and may lift an expired block, restore CI, or send pending messages. For a read-only view, use `/build:status`.
4. `audit <repo dir>` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/audit.py <repo dir>`. Its table shows compile-time gating: a gated test is not built on the Mac. `Not built on the Mac` names omitted lines.

Run `block`, `unblock`, and `status` with the sandbox off; they write state, call `gh`, and start the block watcher.

## State and recovery

- `free` with no block: start work through its normal claim. `running`: let the job finish; do not remove a live job's claim.
- A kept claim means the claiming process died, but its remote work can remain. `status` shows the host, time, and PID. Confirm that the remote work ended. Read `claim_id`, `episode`, and `attempt` from the claim record. Run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`. The offload runner ends remote work before it releases a claim that it takes over.
- `blocked` or `block pending`: wait for the holder's release or free message; `unblock` removes your own block. If CI can still use the Mac, run `github-warmup`, then repeat `block`. An expiry with a failed CI restore needs `github-warmup`, then `unblock`.
- `unreachable`: use `/build:status --probe`; restore the connection before remote work.
- `state unreadable`: inspect the named `~/.local/state/mac-test/block.json`, `run.json`, or `claims/<repo>.json`. Check the holder and any running job before you repair or remove a damaged file. Repeat `block` or `unblock` for a block. Release a readable kept claim with `release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`. Use `/build:hold` for a natedev holder or release record.

## Run a command from natedev

Use `mac_queue_run.py` for every Mac build, test, install, or run of built code from natedev.
The runner takes a queue placement and sends `MAC_PLACEMENT` with the command over SSH.
The Mac cargo gate requires a live placement for agent and SSH calls. A separate Mac claim gives no permission to run.
The runner returns the placement after the command ends or a signal stops it.
`mac_run.sh` uses one placement for its builds and launch.
The user's own Mac terminal needs no placement. The detector reports unplaced runs to builds.

1. Build Hana for a shot:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_queue_run.py --what 'Hana camera shot' --kind shot --package hana --worktree ~/rust/hana --cwd /Users/natemccoy/.local/state/mac-test/mirror/hana -- /bin/zsh -c 'cargo build -p hana'
   ```

2. Install cargo-liner:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_queue_run.py --what 'Install cargo-liner' --kind install --package cargo-liner -- /bin/zsh -c 'cargo install cargo-liner --locked'
   ```

3. Run a probe in an existing scratch folder:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_queue_run.py --what 'Check the Mac compiler' --kind probe --package rustc --cwd /Users/natemccoy/.cache/claude/scratch/mac-probe -- /bin/zsh -c 'rustc --version'
   ```

Use the checkout service to create the scratch folder before the probe.
Do not run these commands through plain SSH.
Read the named placement and reason when the cargo gate refuses a command.
