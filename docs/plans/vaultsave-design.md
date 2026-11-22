# Whole-vault save (List 177)

## Schedule and push

Add a `vault_save` part to `agent_notes_job.py` on natedev. Run `vault_save.py` on `~/rust/hanadocs`.
Run the part after the existing vault part when both are due.
Run only on `main`. Check for an in-progress Git operation before making a commit.
Pass `core.fsync=committed` per Git command during the first save and scheduled saves.

1. List changed and deleted tracked paths against `HEAD` and untracked paths from `git ls-files --others --exclude-standard`.
2. Apply the text rule below at every depth. Keep paths separate with NUL delimiters and use literal Git pathspecs.
3. Mark eligible new paths with intent to add in one batch. Commit the selected paths together with `git commit --only`.
4. Log the result, the file count, and the commit hash when there is one.

Defer the run when the index has staged changes other than intent-to-add entries.
The existing `hanadocs-push` Nix job pushes pending commits to `origin/main` every ten minutes.
It keeps local commits when credentials are cold or the push fails. The next run retries.
Use `--no-overwrite-ignore` for the Nix fast-forward. Keep that flag on the Mac merge in `vault_pull.py`.
Track protection for ignored files and staged changes during replay as a separate defect.

## Text and media

Save regular files with these case-insensitive suffixes.

| Types | Suffixes |
| --- | --- |
| Notes and text | `.md`, `.txt` |
| Data and configuration | `.base`, `.json`, `.jsonl`, `.toml`, `.yaml` |
| Source and pages | `.html`, `.js`, `.py`, `.rs`, `.sh` |

Include exact basenames `.gitignore`, `.gitattributes`, and `Cargo.lock`.
Include Markdown backups named `*.md.backup-YYYYMMDDTHHMMSSffffff` with digits in the timestamp.
Save matching tracked deletions. Treat renames as a deletion and an addition.
Apply the same rule to `issues/question-<id>-*.md` and to files in new folders.
Honor Git exclusions and the existing Git clean filter for issue ranks.
Keep the `.obsidian/` and `settings.local.json` rules in the vault's root `.gitignore`.
Exclude every `.obsidian/` directory from the save.

During the first scan, add media patterns to `.gitignore` in `bob/`, `brain/`, `dwight/`, and `morty/`.
Use case-insensitive extension patterns such as `*.[pP][nN][gG]` at every depth within each folder.
Use the same pattern for `avif bmp gif heic heif ico jpeg jpg png svg tif tiff webp`.
Use it for `aac aiff flac m4a m4v mkv mov mp3 mp4 ogg opus wav webm`.
Make `producer:add` (`add_producer`) write the same media rules in each new producer folder's `.gitignore`.
Keep existing tracked media in Git. The save selects only text paths in every folder.

## One Git writer

Use `scripts/lib/main_push_lock.py` on the vault. Its `main-push.lock` is in the Git common directory.
Hold it from Git state checks through the commit in `vault_save.py`, `vault_commit.py`, and `change_review.py`.
Use the same lock from comparison through index refresh in the prioritization watcher.
In `vault_pull.py`, take it before the natedev publication fetch and hold it through `land_main.land`.
Keep any existing job or list lock outermost.
The nested calls to `land_main.land` in `vault_pull.py` and the first save reuse the vault lock in the same process.
In the Nix push job, use `flock` on the same file before the branch check. Hold it through the run.
Let the scheduled save defer and retry if the lock is busy.

## First save

Run the first save from the implementation worktree before merge. The scheduled part starts when the code reaches main.

1. Run `vault_save.py first-save scan` with `scripts/lib/py`.
2. Review the manifest, count, and stat.
3. Run `vault_save.py first-save commit <manifest hash>` with `scripts/lib/py`.

Scan takes `main_push_lock` and checks the live index before removing the previous manifest.
Fetch `origin/main` if the index is clear.
Stage the selected paths in a temporary index and object database. Use the vault's objects as an alternate.
Copy the file bytes into a temporary folder. Include the staged blob bytes when a Git filter changes them.
Run Gitleaks on the folder and `origin/main..HEAD` through `nix shell nixpkgs#gitleaks`.
Write the manifest after both scans pass. Record the history, paths, blob hashes, and source hashes.
A failed scan leaves the live index unchanged. The scan adds no candidate objects to the vault.

The installed jobs continue their daily commits and pushes of their own generated notes.
The first-save review does not cover the installed jobs' ongoing output.
A Gitleaks hit stops the first save. The director tells sdlc the redacted finding.
The director reports a hit in a job-owned path at once.

Commit takes the lock and checks the source hashes, modes, and filtered blob hashes against the manifest.
Use `git hash-object` for the filtered hashes. Write the verified blobs after all checks pass.
Build the tree from the scanned `HEAD` and the manifest in a temporary index. Create the commit with `git commit-tree`.
Use `git update-ref` to advance main only if it still points to the scanned commit.
Record the reviewed commit hash in the manifest state after main advances. Keep the original manifest hash for retries.
Use `git update-index --index-info` to sync exact manifest paths in the live index. Use mode zero for deleted paths.
During the supervised sync, Git can replace a different staged version if someone stages it and restores the reviewed bytes within milliseconds.
The supervised rerun can also replace a staged descendant when someone turns a reviewed new file into a directory before the rerun. The director runs the rerun at once, so this risk is accepted.
If an interruption leaves the index out of sync, the scheduled save defers.
Rerun the same command with the same manifest hash to recover the reviewed commit from the history of main.
Allow later commits if `HEAD` still has every reviewed entry. Sync the manifest paths from `HEAD`.
If main returns to the scanned parent, verify the recorded commit and retry the guarded ref update.
Publish the reviewed commit with `land_main.land`. Verify that `origin/main` contains it.
After a failed push, rerun the same command to publish the existing commit.
Before the first commit, scan again if a file or the local history changes.

## Generated notes

Save the bytes on disk under `machines/configuration/`, in `hana hierarchy.md`, under `agents/`, and in `agent sessions.md`.
Save timestamp-only changes in these files. The save step edits no note.

## Tests

Use temporary fixture repositories and a local bare remote for all tests.

| Test | What it proves |
| --- | --- |
| `test_vault_save_selects_text_at_every_depth` | New folders, backup names, question records, and tracked deletions use one rule. |
| `test_vault_save_honors_ignores_and_keeps_tracked_media` | Save excludes ignored files and media. Tracked media stays tracked. |
| `test_add_producer_writes_media_rules` | New producer folders ignore media at every depth. Git can still add media outside producer folders. |
| `test_vault_save_keeps_generated_bytes` | The commit contains exact note bytes, including timestamp-only changes. |
| `test_vault_save_serializes_writers` | Either writer defers when the other holds the vault lock. The next run saves the changes. |
| `test_vault_save_preserves_staged_changes` | A staged user change defers the save and stays staged. |
| `test_hanadocs_push_retries` | A rejected push leaves a reviewed commit. A retry publishes the same commit. |
| `test_vault_save_checks_first_push` | The scan covers outgoing history. A changed file, mode, filter, or manifest hash blocks the first push. |
| `test_vault_save_rejected_scan_leaves_index_and_objects_unchanged` | A rejected scan preserves the live index and object database. It produces no manifest. |
| `test_vault_save_retries_after_moving_main` | A refused scan preserves the manifest. Recovery syncs the index and pushes the reviewed commit after a later job commit. |
| `test_vault_save_recovery_refuses_changed_manifest_paths` | Recovery stops before index sync if a later commit changes a reviewed path. |
| `test_vault_save_retry_preserves_staged_descendant` | Sync keeps a staged file below a deleted manifest path. |
| `test_vault_save_retries_when_main_returns_to_the_scanned_parent` | Recovery reuses the recorded commit when main returns to its parent. |
| `test_vault_save_preserves_a_concurrent_git_add` | A concurrent add stays staged outside the reviewed commit. |
| `test_vault_save_validates_interval` | The scheduler uses the configured value and rejects values outside the range. |

## Configuration

| New key in `config/agent_notes.toml` | Value and range |
| --- | --- |
| `parts.vault_save.interval_seconds` | Set to 180 seconds. Accept values from 60 through 3,600. |
| `parts.vault_save.timeout_seconds` | Set to 60 seconds. Accept finite positive values. The job deadline can end the save sooner. |
