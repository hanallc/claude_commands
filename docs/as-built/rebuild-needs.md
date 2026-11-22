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
| `reason` | One sentence that explains the rebuild. |
| `concerns` | The staff ID affected by the rebuild. |
| `requested_by` | The requesting staff ID, or an empty string when omitted. |
| `created_at` | The creation time in ISO format with the UTC offset. |
| `cleared_by` | The exact generation log line that cleared a completed need. |

Open records omit `cleared_by`.
`NeedRecord` describes an open record.
`CompletedNeedRecord` adds the required `cleared_by` field for a completed record.
Completed records remain in `needs/done/`.
Record writes use temporary files and atomic file operations.
Adding the same key changes neither an open record nor a completed record.
The needs folder contains one `.lock` file.
`add_need` holds an exclusive `fcntl.flock` from the existence check through the record write.
`clear_needs` holds the same lock while it rewrites and moves each completed record.
The list command takes no lock and stays read-only.

## Commands

Run commands from the repository folder.

```sh
scripts/lib/py -m scripts.rebuild.need add --machine natedev --commit <full-nixos-hash> --reason "Apply the rebuild settings." --concerns <staff-id> --requested-by <staff-id>
scripts/lib/py -m scripts.rebuild.need list
scripts/lib/py -m scripts.rebuild.need list --json
scripts/lib/py -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

`--requested-by` is optional.
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

## Status line

`scripts/statusline/rebuild_row.py` combines cached system comparisons with `read_needs()` on each status read.
Its `read()` answer keeps the compared machines in `compared` and the needs in `needs`.
The console Rebuilds page renders that answer, so the page and the row name the same machines.
The refresh also writes each machine's change list to `<rebuild.cache>-changes.json`.
It reads the machine's last switch from `rebuild.generations`. It reads the Mac's log over `rebuild.ssh`.
The change list shows commits after that switch on the machine's rebuild ref.
It uses the configured commit from the current comparison when available.
A dirty switch still supplies its base commit for the Rebuilds list.
Each commit's `Claude-Session` id maps to a local session through the transcript that records it.
The refresh finds that transcript with one `rg` scan of the Claude projects folders and keeps the answer in `<rebuild.cache>-sessions.json`.
A new session id costs one scan; a known id costs none.
A machine that does not answer keeps its last list.
The status line names each waiting rebuild once in the `For you:` row.
It shows `rebuild natedev`, `rebuild mac`, or both.
Open needs appear even when evaluation has no result or the cache does not exist.
Completed needs leave the row unless the cached system comparison still requires a rebuild.
A failed need read leaves the cached answer visible.
The refresh uses `rebuild.ref` in `rebuild.repo` for natedev because its rebuild includes local commits.
It uses `rebuild.ref.mac` in the same repository for the Mac because the Mac rebuild builds `origin/main`.
The cache records the commit for each machine.
A cache with the legacy `commit` field requires a refresh before it can show a comparison.
One Nix evaluation can supply both paths when the machines use the same commit.
The refresh reuses valid paths for the same machine, commit, and evaluation source.
The cache keeps each valid path, even when the same evaluation fails to return the other machine's path.
The cache records the failure time for each commit with a missing path.
`rebuild.eval_retry_seconds` sets a 600-second wait before another evaluation of a missing path for that commit and source.
Each refresh probes both running systems and compares the available paths.
For you lists pending commits only when the cached running path and configured commit match that machine's comparison.
The status line reads the waiting machines and action count without reading the cached commit list.
If a ref fails to resolve, its machine has no current expected path and shows no comparison row.
For you shows that machine as unknown if it has only completed requests.
For you uses each machine's own expected path and probe to determine whether its completed requests can show as done.
A new commit or evaluation source bypasses the wait.
A successful evaluation clears that commit's failure record.
`rebuild.eval_timeout` in `config/statusline.conf` sets the evaluation time limit to 120 seconds.
Other commands retain their 30-second limit.
`rebuild.interval` sets the refresh interval to 60 seconds.

## Clear rule

The rebuild writer records each activation in `generations.log` with five space-separated fields:

```text
<time> <action> <generation-link> <nixos-commit> <store-path>
```

The time uses `%FT%T%z`.
Only `switch` lines can clear a need.
The command removes a final `-dirty` suffix from the logged commit before comparison.
It runs this command for each open need of the selected machine:

```sh
git -C <nixos-repo> merge-base --is-ancestor <needed-commit> <logged-commit>
```

A successful comparison clears the need.
The command adds the exact log line as `cleared_by` and moves the record to `needs/done/`.
A non-ancestor keeps the need open.
When the comparison returns neither 0 nor 1, the command checks both commits with `git cat-file -e <commit>^{commit}`.
These checks run only after a comparison error.
An unknown commit keeps the need open.
Each distinct unknown commit produces one line per clear command:

```text
anomaly: unknown logged commit <commit>
anomaly: unknown need commit <commit> (need <id>)
```

The command does not delete completed records.

Run the natedev clear command on natedev:

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

Run the Mac clear command on the Mac:

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine mac --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo "$HOME/nixos"
```

Both commands use the selected needs folder on their machine.
The job must supply the selected machine's log and a repository that contains the compared commits.
