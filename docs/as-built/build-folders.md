# Build folders

Editable source lives under `~/worktrees`. Cargo output lives under the configured build root. Fixed-commit tests use numbered copies with their own Cargo output. The same folder lock protects each numbered copy during use and removal.

## Paths and names

The build root is `/build` on natedev and `~/build` on the Mac and the Hana AWS guest. The table uses `/build` for the build root.

The Mac Linux guest uses the default build root `~/build` and the main checkout `~/.claude` for the `gate-runner` user.
The route takes the Mac's repository claim through `mac_run.held` before it contacts the guest.
The guest run holds a separate lock.
The route waits up to `unreachable_wait_seconds` (`build_targets.conf`) for proof that the guest run ended.
After proof, the route releases the Mac claim.
The current API also releases the Mac claim when the wait expires.
The guest run keeps its lock until it ends.
Item 191 B3 will keep the Mac claim at the unreachable bound until the guest proves the run ended.

| Form | Path | Cargo output |
| --- | --- | --- |
| Worktree | `~/worktrees/<repo>/<unit>/<short-name>` | `target` links to the worktree output. |
| Worktree output | `/build/<repo>/<unit>/<short-name>` | The folder holds intermediate and final output. |
| Numbered copy | `/build/<repo>/<unit>/<number>` | The copy holds the fixed source and a real `target` folder. |
| Main output | `/build/<repo>/main` | Local main checkouts link `target` here. The Hana guest creates no main checkout `target` link. |
| CI | `/build/<repo>/ci/<number>` | Each number has a runner folder that also holds the runner's home. |

The main checkouts stay at `~/rust/<repo>` and `~/.claude`. The repo word is the last part of the Git origin path without the `.git` suffix. The local alias for `claude_commands` is `claude`. The path has no showrunner component.

The Hana guest stores its shell cache under `<build root>/hana/main/shells`.
Each numbered copy uses its own real `target` directory.

The unit word names the unit. Use a unit word for only one active unit. A new unit can reuse a retired unit's word. The folder rules reserve `main` and `ci`.

A short name is one nonempty path component. It must contain a character other than an ASCII digit. The tool refuses `.`, `..`, slashes, and NUL characters. A short name must be free within its repo and unit. A copy number is a positive decimal integer without leading zeroes.

A new unit or style production uses `worktree` as its main short name. The branch is `<unit>-worktree`.

## Worktree commands

- `worktrees.py make <repo word> <unit> <short-name>` creates the source, the output link, and the worktree record.
  It uses the main checkout for the repo word and checks its origin.
  It creates the branch `<unit>-<short-name>` from the main checkout's HEAD.
- A phase that runs beside the current phase uses a model worktree.
  The director creates it with `worktrees.py make <repo word> <unit> p<phase id>`.
  The director uses a distinct short name `p<phase id>` for each phase.
  The director runs `~/.claude/scripts/lib/py ~/.claude/scripts/worktree/worktrees.py remove <path>` after the checkpoint reaches origin.
  The director sends the reported checkout and output paths to the showrunner as a removal list.
  `phase_reservation.py claim` accepts the unit's home worktree or a model worktree.
  The command records the path only for a separate model worktree.
- `worktrees.py move <path> <unit> <short-name>` gets the repo word from origin and prints the new path.
  The move keeps the Git worktree entry and branch.
  It refuses a numbered build copy before it reads the move record.
  It updates the worktree and staff records, adds a link at the old path, and links the Claude transcript folders.
  A retry resumes from the move record.
  If a prior copy stopped before it recorded the stage identity, a retry reports that stage and makes a fresh copy.
  A managed unit rename uses `unit_folder_rename` to move the unit's source and build folders together.
- `~/.claude/scripts/lib/py ~/.claude/scripts/worktree/worktrees.py remove <path>` reports the checkout and its assigned output for manual removal.
  `worktrees.py retire <unit>` removes that unit's named outputs and numbered copies across the build root.
  A held lock prevents removal of its folder.
- `worktrees.py migrate --list <file> --dry-run` prints the plan for the listed worktrees without writing files.
  Each list row supplies `<path> <unit> <short-name>`.
  Use the exact staff unit name for `<unit>`.
  If staff or the worktree record identifies the owning unit, use that unit's name.
  For example, use `unit-board` for the staff path `sdlc/unit-board`.
  The command refuses a unit word with an added machine, repo, or production prefix.
  The command refuses a row that names a numbered build copy.
  Omit `--dry-run` to apply the plan and resume saved moves for the selected worktrees.
- `worktrees.py migrate --all --dry-run` prints the plan for the full switch.
  Add `--list <file>` to supply names for specific worktrees during the full switch.
  Omit `--dry-run` to apply the full plan, resume saved moves, and link main output.
  Moved worktrees of retired units have no assigned output.
- `worktrees.py migrate --repo <word> --dry-run` prints the plan for one repository.
  `<word>` is the repo word (`build_folders.repo_word`).
  The plan has the main checkout, its worktrees, its scratch clones, and its saved moves, and it links the main output as `--all` does.
  `--list <file>` supplies names, as it does for `--all`. `--repo` and `--all` do not go together.
  This form takes no Mac claim on any machine.
  An unknown repo word (`UnknownRepoWord`) prints one line, `migrate --repo <word>: <reason>`, and exits 2. Other errors keep their own text and exit status.
  `--repo` exits 1 when it keeps a checkout of that repository, so a loop can see the keep. `--all` keeps its exit status.
  Keeps that need no work do not count: `main checkout stays in place`, `already moved`, `already at the destination`, and `already moved; defer output: archive the old build root first`. A deferred main output is not a keep.
- `worktrees.py migrate --repo <word> --host mac` runs on natedev only (`remote.local_machine()`) and migrates that repository on the Mac.
  It holds the repository's Mac claim on natedev (`mac_run.repository_claim`, target `mac`, kind `mirror`, what `migrate --repo <word>`).
  The claim id has the `mac-linux-` prefix, so the Mac activity watch does not release it. The holder releases it.
  The claim identity comes from natedev's main checkout for `<word>`. Without that checkout, the command prints `migrate --repo <word>: <reason>` and exits 2.
  Inside the claim it runs `~/.claude/scripts/lib/py ~/.claude/scripts/worktree/worktrees.py migrate --repo <word>` over ssh to the `host` of the `mac` target.
  The ssh options are `mac_linux.SSH`, which include `ServerAliveInterval=15` and `ServerAliveCountMax=2`, so a dead link ends ssh.
  It prints the Mac output and returns the Mac exit status.
  ssh runs in its own session with its input from `/dev/null`, so a terminal or pane signal does not reach it.
  natedev waits for ssh inside `cancellation.defer_cleanup_signals()`, so SIGINT, SIGTERM and SIGHUP take effect only after ssh ends.
  ssh starts inside that block, so it inherits SIGINT, SIGTERM and SIGHUP blocked. These three signals stay pending at the ssh pid. Other signals, such as SIGQUIT or SIGKILL, end ssh. Then the claim ends while the Mac run can go on.
  After ssh ends, SIGINT raises `KeyboardInterrupt`, and the claim block releases the claim. SIGTERM and SIGHUP end natedev without the release. The next `live_claims` read removes the claim, because its pid is dead.
  The claim is released at the end, also when ssh fails.
  Limit: the claim covers the Mac run only while ssh lives. If the link dies, ssh ends and the Mac run can go on without the claim.
  Limit: SIGKILL ends natedev at once. ssh and the Mac run go on, and the claim counts as ended because its pid is dead.
  The process scan and the Cargo flocks on the Mac always guard each target, with or without the claim.
  A busy or refused claim prints `kept: <word>: <reason>`, for example `kept: origin: mac is busy with native Mac run`. It exits 1 with no ssh.
  `--dry-run` takes no claim, needs no natedev main checkout, and passes `--dry-run` to the Mac.
  `--host` needs `--repo`, takes no `--list`, and is refused on the Mac.
- Before migration links a real `target` folder to its output, it checks the target.
  This applies to a main checkout and to an already moved worktree whose `target` is a real folder or is missing. That worktree links to the output in its worktree record.
  A linked moved worktree prints only `output <checkout> -> <output>`.
  1. Migration takes a non-blocking exclusive flock on each Cargo lock (`sweep.LOCK_NAMES`) at any depth under `target`. The walk reads only folder and file names. It stops at a folder with a lock and skips Cargo output folders (`sweep.HASHED_DIRS`, `incremental`, `doc`). A held lock keeps the checkout as `kept: <lock> held`.
  2. A process scan of the target must find no holder. A holder keeps the checkout as `target in use (pids …)`. The flocks come first, so a cargo that waits on one keeps the lock file open and the scan finds it.
  3. Migration holds the flocks through the rename and the link.
  Migration moves a `target` only when it was a real folder at the start of these steps. A `target` that appears after the check is not moved: migration keeps the checkout as `kept: <target>: expected a link to <output>; retain the existing path`.
  When the output is not empty, migration moves the target to `.saved-target-<checkout name>-*/target` beside the checkout instead.
  Migration defers SIGTERM, SIGHUP and SIGINT from each rename to its link (`cancellation.defer_cleanup_signals`), so a stop does not leave the checkout with no `target`.
  If a new `target` appears between the move and the link, migration keeps the checkout as `rerun: …`. The reason names the folder that holds the earlier target: the output, or `.saved-target-<checkout name>-*/target` when the output was not empty. Run migration again.
  If the link fails for another reason, migration moves the target back from the output to where it came from and keeps the checkout as `kept: <error>`. If the move back also fails, the target stays in the output, and the next run links it.
  Each later run reports each `.saved-target-<checkout name>-*` folder as `kept for a manual removal list: …`. This applies to each selected main checkout and moved worktree, before any defer or keep.
  It also reports each older `.saved-target-XXXXXXXX` folder beside such a checkout. That form names no checkout, so a run reports each folder once, also when many checkouts share the parent folder.
  A held or changed build-folder lock keeps the checkout as `kept: <output>: the build folder lock is held. Rerun when idle`.
  In each case migration goes on with the next checkout.
- `worktrees.py old-links` lists recorded links and the live sessions that use them.
  `--remove-unused` removes a link only after a completed move with a verified destination and no possible live user.
  Unreadable session evidence or an earlier link through that path keeps the link.

While `/build/claude/scratch` exists, `move_worktree` and migration defer the new build output link for live `claude` units.
Migration also defers the `~/.claude` main output link to `/build/claude/main` while `/build/claude/scratch` exists.
Both publish the checkout and its record at the new path.
They write the checkout record before they record the move as `linked`.
Cleanup keeps a branch that Git registers in any worktree.
The move record stays open and the tool prints `defer output <new>: archive the old build root first`.
Until the move finishes, `make` reports the moved destination as occupied.
After the archive, repeat the same move or run migration to link the output and record its assignment.

For a worktree without a list row, migration first checks staff ownership.
It then checks the folder name, the worktree record, and the commit subject in that order.
It can remove known machine, repo, and production prefixes from old names to find the staff unit name.
Migration keeps a worktree when the available names cannot identify a staff unit.

Migration derives the short name by removing known repo, checkout, machine, production, and unit words from the old folder name. It uses `worktree` if the result is empty. For a result made of digits, it tries the branch name with the same words removed. It adds `p` if the name still contains only digits. It keeps checkouts with no proved unit or with conflicting destinations.

Migration keeps the showrunner checkouts until Nate answers question 4. This includes the Hana merge checkout and showrunner checkouts recorded in archived production documents. It keeps the user's `~/worktrees/nate_style-message-clarity` checkout. Tool-owned checkouts, such as Claude Code worktrees and temporary gate checkouts, stay with their tools.

Migration keeps build unit folders, numbered copies, CI runner folders, and main outputs. It keeps checkouts that contain the build root or a build repository folder. A legacy scratch clone stays with its linked worktrees if migration must keep one of them.

Both modes plan again when a saved move still has its original source. Only execution removes that saved record. Execution lists any staged copy for manual review and keeps the copy on disk. A previously published copy stays with its record for review. A move with an unverified source identity also stays with its record.

A clone stays if its current linked checkouts differ from the planned set.
A move renames the checkout into the destination only when both paths are on the same mount.
A move across mounts copies the checkout and renames the source into the backup.
This includes bind mounts of the same filesystem.
After relocation, recovery continues with publication and repair.
Recovery repairs nested linked checkouts at their paths under the verified destination.
Both modes plan from the Git registrations that recovery would produce.
Execution writes the repair.

Migration keeps a source if a process uses it as its working directory, root, or executable. It also keeps a source if a process opens or maps a file in it. Both modes check the locks without waiting. A held lock keeps the source and adds the reason `kept: <lock> held`.
On Linux, migration reads the mount table to find alternate paths to each checkout through bind mounts.
It checks all these paths for process use.

Migration scans the sources together and checks each source again before source relocation. The scan cannot stop a process that enters the source after the scan. The pass ends with `moved N, kept in use M, kept K`. The dry run uses `would move`. Run the same command again later to move kept folders.

Linux migration checks the working directory of each live thread. It checks for live threads when the thread leader is a zombie. After `EACCES`, it checks whom the working directory link in `/proc` belongs to. It also checks whether the process has a permitted capability the caller lacks. Either cause can account for the missing paths. Migration prints `not checked (hidden by the kernel): <pid> <comm>` once per hidden process. Other read errors for processes of the current user keep every candidate. An incomplete process scan keeps every candidate on either host.

Migration holds busy claims and copy leases through source relocation. A copy request waits for another process to release the unit allocation lock. A second request from the same process fails immediately.

Both `move` and migration hold busy claims and copy leases during a copy and until the source reaches its backup.
They check for processes that use the source before the copy and before they move the source to its backup.
They compare source entries before and after the copy. A change makes them discard the staged copy and keep the source.
If another process creates the destination during the copy, the move stops and reports the occupied path.
If the source is already in the backup, a retry can publish the verified stage after the destination becomes free.
Full migration and `--repo` migration report each legacy scratch backup that recovery still needs and exit with status 1. `--repo` reports only the backups of its repository.
For a linked checkout, Git starts repair from the saved Git entry so it can update a relative `.git` pointer.
The copy includes only tracked files from a real `target` folder.
The move renames an untracked `target` into an empty or missing assigned output when the paths permit a rename on the same filesystem.
If a copy cannot reuse the old `target`, the move keeps and reports it in the backup.
If a rename of the checkout for a live unit cannot reuse the old `target`, the move saves and reports it beside the checkout.
A rename of a retired unit's checkout has no output, so a real `target` stays inside the moved checkout.
If Git tracks files in `target`, the move keeps and reports any existing `target` at the new checkout.
The checkout record has no output assignment in this case.

## Cargo

The Cargo front reads `folders.check` in `config/build_folders.conf`. The setting is `report`. In both `report` and `enforce`, it repairs a missing or incorrect `target` symlink for a recorded worktree. It also recreates missing output for an assigned worktree or a main checkout with its expected link. It updates the use stamp under the build folder lock.

A real worktree `target` stays in place. `report` warns and lets Cargo run when the folder check finds a legacy or unassigned checkout, a real worktree `target`, or mismatched Cargo paths. `enforce` refuses the same calls. Both modes refuse a repair that fails. The ordinary check skips `cargo install`, calls in an existing build scope, and nested calls that inherit the check marker.

A full `cargo clean` for an assigned worktree or linked main checkout empties its output folder and frees the disk space. It keeps the folder and the `target` link. The front takes the folder and Cargo output locks before deletion. A narrower clean or a clean in a numbered copy uses Cargo's own clean command.

The Cargo seed is absent. The first test build of a new Hana worktree takes about 3.5 minutes more without it. The shared `sccache` stays warm.

## Numbered copies and removal

Use a numbered copy for tests and proofs that need a source copy.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/lib/build_copies.py run --source <worktree> --commit <hash> --showrunner <name> --unit <unit> --work <word> --purpose <word> -- <command>`.
The director creates a model worktree for a trial project.
Workers use `scratch.py new --worker` only for downloads and other files unrelated to builds, tests, gates, or source copies.

`build_copies.take_copy` takes the lowest free existing number for the repo and unit.
If none is free, it takes the smallest unused number.
A worker has no fixed number.
The scan uses the standard Git ignore rules.
The maker skips a copy with untracked directories outside those rules.
The maker saves work in `refs/rescue/` before it checks out the requested commit.
The rescue includes ignored files outside the tool output folders.
The maker keeps the copy's real `target` folder.
Nightly tests of main use the merge unit's numbered copies.

Copy `1` has sibling files `1.lock` and `1.lease.json`.
The lease records the checkout path, holder, source, commit, and directory identities.
The kernel lock determines when the copy is free.
The owner passes the lock descriptor to children.
The last holder releases the lock when it closes the descriptor.

The Nix environment check uses the lease to verify the numbered copy's `.envrc` against the recorded commit in a trusted source. Style lint passes its copy lease to the verifier. Legacy temporary cleanup excludes these folders and their lock and lease files.

The Hana AWS guest fetches verified bundles into its main checkout at `~/rust/hana`.
The command uses its assigned copy number under `~/build/hana/aws-hana`.
Each copy keeps its Cargo output under `target`.
The guest keeps the shell cache under `~/build/hana/main/shells` and holds the main output lock during use.

`lint/sweep.py --unused-folders` uses each folder's `.lint-sweep-used` stamp. The cutoff is two days. The sweep checks the stamp again under the folder lock. It keeps unstamped folders, folders in use, and CI folders. It lists eligible folders and keeps unit and repository parents. The nightly gate invokes the sweep.

Use `build_copies.unused_build_folders` to list free numbered copies.
The inventory also lists gone unit folders when all their numbered copies are free and the unit folder holds no other file.
It lists each unit folder after its copies.
It lists temporary `p-*` folders with a free lease lock when the process with the recorded owner PID has ended.
It marks temporary folders with Git storage or symbolic links that can point outside the folder as review-only.
Git storage includes a `pack` directory or a loose object file anywhere in the temporary folder.
The check recognizes full object hashes and the shorter names that Git uses in two-digit hexadecimal directories.
The inventory reads each symbolic link path without opening its target.
Each entry gives the path, disk usage in bytes, and reason.
The bytes for a unit include its directory and copy sidecars. Each copy has its own entry.
The inventory is a read-only snapshot.

`build_copies.claim_unused_build_folder(path)` takes one exclusive lock without waiting.
It accepts free numbered copies and ownerless temporary folders that pass the removal checks.
The inventory and the claim treat a numbered copy with a keep file as in use.
The lock file is `<build root>/.build-claim-locks/<relative folder path>.lock`.
The lock file stays outside the folder during removal.
Code that starts work inside a claimable folder takes the lock in shared mode.
Folder creation takes shared claim locks from the first folder under the build root to the target folder.
A nested temporary claim prevents lease creation on a parent with a temporary folder name.
Git source setup takes the shared lock for the folders that hold the source's object directory, its alternates, or a link to a Git directory.
The SSH source holds the shared lock through its fetch.
The claim checks lease locks, lease owners, busy marks, mounts, and current borrowers under the exclusive lock.
The borrower scan checks Git directories, Git directory links, and alternates for scratch and numbered copies.
If a scratch checkout has no default object store, the scan keeps any Git objects it could use.
The scan accepts a missing local store if a removal record identifies the checkout and the checkout is idle.
One walk checks child leases and busy marks, including marks in external Git directories.
The claim releases these checks' locks before it yields.
The claim raises `BuildFolderInUse` with a reason when it cannot verify a folder.
In the resources cleanup script, remove each claimed folder inside the `with` block.
The claim refuses review-only folders.
Check the unit again under its allocation lock before removing the empty unit folder.
Keep the claim lock file in place.
Only Nate runs the removal.

Known limit: the claim cannot see a checkout outside the build root and the scratch root that has no copy record.
If that checkout's `objects/info/alternates` or `commondir` file is a symbolic link to a plain file in an ownerless temporary folder, the claim can admit that folder.
If that checkout's `objects/pack` folder is a symbolic link into a numbered copy, the claim can admit that copy.
Removal then breaks the outside checkout.
Only a hand-made link of a Git metadata file or folder makes these cases. No Git command makes them.

Automatic owner cleanup for these temporary folders takes the exclusive claim lock without waiting.
Cleanup keeps the folder and lease record if a reader holds the lock or the borrower check cannot confirm that the folder is unused.
Cleanup also keeps review-only temporary folders.
The inventory can list the folder after the owner ends and all borrowers release it.

Run `scripts/lib/py scripts/lint/sweep.py --switch-removal-list` to list the former build folders at the switch.
The list uses the `removal_guard.py` format. Legacy paths require manual review because they do not match the templates in `removal.path_templates.<machine>`.

See [Repository copies](copies.md) for the copy commands and [Worktrees](worktrees.md) for worktree removal.
