# Rebuild needs

`scripts/rebuild/need.py` records needed system rebuilds for `natedev` and `mac`.
The default folder is `~/.local/state/rebuild/needs`.
`REBUILD_NEEDS_DIR` selects a different folder for tests or callers.
The command reads this variable for each operation.

## Record

Each open need has one JSON file named `<machine>-<nixos_commit>.json`.
The machine and full NixOS commit form the key.
The record has these fields:

| Field | Meaning |
| --- | --- |
| `id` | The machine and commit, joined with `-`. |
| `machine` | `natedev` or `mac`. |
| `nixos_commit` | The full commit that needs a rebuild. |
| `reason` | Plain words that explain what changes for the user after the rebuild. |
| `concerns` | The staff ID affected by the rebuild. |
| `requested_by` | The requesting staff ID, or an empty string when omitted. |
| `created_at` | The creation time in ISO format with the UTC offset. |
| `cleared_by` | The generation log line or verified running system that completed the request. |

Open records omit `cleared_by`.
`NeedRecord` describes an open record.
`CompletedNeedRecord` adds the required `cleared_by` field for a completed record.
Completed records remain in `needs/done/`.
Record writes use temporary files and atomic file operations.
Adding the same key changes neither an open record nor a completed record.
The needs folder contains one `.lock` file.
`add_need` holds an exclusive `fcntl.flock` from the existence check through the record write.
`clear_needs` holds the same lock while it archives each completed record and removes the open file.
The list command takes no lock and stays read-only.

## Commands

Run commands from the repository folder.

```sh
scripts/lib/py -m scripts.rebuild.need add --machine natedev --commit <full-nixos-hash> --reason "The CI repair command will report when its toolchain test fails." --concerns <staff-id> --requested-by <staff-id>
scripts/lib/py -m scripts.rebuild.need list
scripts/lib/py -m scripts.rebuild.need list --json
scripts/lib/py -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

Use `--reason` to explain what the user can do or see after the rebuild.
The requesting session supplies this text.
The command rejects a missing or blank reason.
Supply a full 40-character Git hash with `--commit`.
The command rejects short or malformed hashes.
Use `--requested-by` to record the requesting session or staff ID.
Old requests without a reason show `No reason given`.
The list command returns one row per machine with open needs.
Each row uses the newest need by creation time.
The row joins all open reasons for that machine with spaces, from oldest to newest.
The JSON output contains the newest record fields with the joined `reason`.

## Typed reader

`read_needs() -> list[MachineNeeds]` gives the status line the same summaries as the list command.
Each `MachineNeeds` contains `newest: NeedRecord` and `reasons: tuple[str, ...]`.
Its `records: tuple[NeedRecord, ...]` retains each open record, from oldest to newest.
Its `record()` method returns the newest fields with the joined reason.
The reader reads only the open JSON files in the needs folder.
It does not read completed files or run subprocesses.
A missing folder returns an empty list.
The reader skips files that a concurrent clear moves before the read.
The reader returns valid summaries and reports each damaged file through the optional `errors` list.
Without an errors list, the reader reports problems to stderr.

## Status line

On a Linux status line cache miss, `scripts/statusline/rebuild_row.py` combines cached system comparisons with the open needs from `read_needs()`.
A fresh render uses the saved fields and skips the scan.
Its `read()` answer keeps the compared machines in `compared` and the needs in `needs`.
The console Rebuilds page renders that answer, so the page and the row name the same machines.
The refresh also writes each machine's change list to `<rebuild.cache>-changes.json`.
It reads the active system and `rebuild.generations` on each machine.
It connects to the Mac over `rebuild.ssh`.
The refresh uses the same completion rule as the clear command.
The refresh lists commit subjects after the verified running revision.
If Git cannot list the range, the refresh lists paths changed in the configured commit.
The refresh also lists those paths when it cannot verify the running revision.
The Rebuilds page shows up to five commit subjects or paths and gives the number of additional entries.
Each display checks the running path and the configured commit before it uses the cached list.
When the list is unavailable, the display shows `Changes not read yet` and the configured commit subject if available.
Each commit's `Claude-Session` id maps to a local session through the transcript that records it.
The refresh finds that transcript with one `rg` scan of the Claude projects folders and keeps the answer in `<rebuild.cache>-sessions.json`.
A new session id costs one scan; a known id costs none.
A failed probe keeps the last list and its system identities.
The status line names each waiting rebuild once in the `For you:` row.
The status line shows the first cached commit subject for a rebuild found by the system check.
It cuts the subject to fit and adds `+N` when more commits wait.
For a filed need, it shows the latest open reason.
Linux reads the cached details during the render.
The Mac reads the same short reasons from `rebuild_reasons` in the console count response.
The console reads the cached details for that request.
These reads run no Git commands.
The rebuild reasons remain available when a damaged action record prevents a count.
The status line shows `reason not read` if the response has a machine with no reason.
Open needs appear even when evaluation has no result or the cache does not exist.
Completed needs leave the row unless the cached system comparison still requires a rebuild.
The For you card lists the open reasons and uses the oldest open request for its age.
Review and Rebuilds keep the history for an old request with a blank reason.
They also show the cached comparison for that request when the system check found a rebuild.
For a rebuild found by the system check, the card shows the same cached reason as the Rebuilds page.
It shows up to five commit subjects or paths and gives the number of additional entries.
The card shows `Found by the system check` under `Asked by`.
The Rebuild details page shows completed requests once per machine and commit, newest completion first.
The page names each damaged archive and shows the valid records.
A failed need read leaves the cached system comparison visible.
For the partial count rule, see [For you](for-you.md).
The refresh compares running systems with `rebuild.ref` in `rebuild.repo`.
Each machine is evaluated in its own Nix command.
A valid machine path is reused for the same commit and source.
A timeout, error, or empty path leaves no reusable path for that machine only.
Each valid machine path still supplies its current comparison when the other machine has no path.
The cache records the failed commit, evaluation source, failure time, and the machines that succeeded.
After the wait, the retry evaluates only the machines that failed.
`rebuild.eval_retry_seconds` sets a 600-second wait before another evaluation for that commit and source.
Refreshes during this wait keep the cached row.
A new commit or evaluation source bypasses the wait.
A successful evaluation clears the failure record.
`rebuild.eval_timeout` in `config/statusline.conf` sets the evaluation time limit to 120 seconds.
Other commands retain their 30-second limit.
`rebuild.interval` sets the refresh interval to 60 seconds.

## Clear rule

The rebuild writer records each activation in `generations.log` with five space-separated fields:

```text
<time> <action> <generation-link> <nixos-commit> <store-path>
```

The time uses `%FT%T%z`.
The clear command and the refresh first read the active system at `/run/current-system`.
They use its embedded `configuration-revision` when present.
Otherwise, they require a switch log entry for the active generation.
The system profile must name a generation that resolves to the active store path.
The profile link must precede the active system link.
The newest log entry for that generation must be a switch to that store path.
Its timestamp must follow the change to the active system link.
The log records whole seconds, so an entry in the activation second cannot confirm the order.
The probe checks the links twice to detect changes during the read.
The probe separates live metadata from the log before checking the revision.
An older switch can share the active store path.
The store path and the evaluated path supply no completion evidence.
An unlogged activation without embedded metadata leaves the revision unknown.
The card shows that state and the requests stay open until a logged rebuild confirms the active revision.
A revision with local changes has a `-dirty` suffix and leaves the requests open.

For a clean active revision, the reader checks the Git ancestry of each open request for that machine.
It accepts a legacy short hash only when the prefix names one Git object and resolves to that commit.
An ambiguous or unknown short hash keeps the request open.

```sh
git -C <nixos-repo> merge-base <needed-commit> <active-commit>
```

A merge base equal to the needed commit completes the request.
The reader records the active switch log line or the time, embedded revision, and store path in `cleared_by`.
It stores the request under its full hash in `needs/done/` and removes the open file.
When Git returns no merge base, the reader checks which commits are available.
An unknown commit keeps the need open.
The reader reports each distinct unknown full commit once per machine check.

```text
anomaly: unknown logged commit <commit>
anomaly: unknown need commit <commit> (need <id>)
```

Run the clear commands on natedev.
The natedev command reads the local active system.

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

The Mac job copies the Mac log to a local file and calls the same command with `--machine mac`.
The command checks the active system over SSH.

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine mac --generations <local-copy-of-mac-log> --nixos-repo /etc/nixos
```

Both commands write to the same needs folder on natedev.
Supply the selected machine's log and a repository that contains the compared commits.
