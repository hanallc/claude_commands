# Producer names from staff records
Base `94e07d22c4`. List item 168.
## 1. Inventory
Counts include literals and related reader lines. The code phase will list exact matches in the guard.

| File | Lines | Change kind |
| --- | ---: | --- |
| `config/README.md` | 1 | Document the AWS recipient ID |
| `config/accounts.conf` | 2 | Set FYI staff IDs |
| `config/aws_target.conf` | 2 | Set the settled-cost recipient ID |
| `config/build_admission.conf` | 1 | Add the stall recipient ID |
| `config/console.conf` | 2 | Reuse the List owner ID and remove the name comment |
| `config/models.conf` | 5 | Key launch profiles by staff ID |
| `config/staff.conf` | 1 | Add the List requester ID beside the producer folder settings |
| `scripts/accounts/accounts.py` | 1 | Read the launch profile by ID |
| `scripts/agents/producer_creation.py` | 2 | Use the saved creation profile and publish by ID |
| `scripts/agents/producer_lifecycle.py` | 16 | Use role IDs and recover the folder rename |
| `scripts/agents/producer_transfer.py` | 3 | Read and store profiles by ID |
| `scripts/agents/restart.py` | 2 | Read the saved creation profile or profile by ID |
| `scripts/agents/set_model.py` | 2 | Set the profile by ID |
| `scripts/agents/stop_resume.py` | 3 | Read the profile and check the rename journal |
| `scripts/aws_target/aws_target.py` | 3 | Read the configured staff ID in `hours_gap_notices` |
| `scripts/aws_target/config.py` | 4 | Read the AWS recipient ID |
| `scripts/aws_target/ledger.py` | 1 | Route the notice by ID |
| `scripts/build_queue/head_stall.py` | 3 | Route and record delivery by ID |
| `scripts/herdr/pane_launch.py` | 1 | Read the profile by ID |
| `scripts/lib/models_registry.py` | 5 | Use IDs for profile access |
| `scripts/message/morty_identity.py` | 9 | Identify the List producer by ID |
| `scripts/production/session_staff.py` | 3 | Identify the List producer by ID |
| `scripts/production/staff.py` | 5 | Enforce rename reservations. Keep the name stored in `HoldRecord.by` at hold time. |
| `scripts/production/staff_sources.py` | 5 | Resolve the List role by ID |
| `scripts/production/the_list.py` | 14 | Use keeper and requester IDs and read legacy records |
| `scripts/production/unit_move.py` | 2 | Refuse source and destination paths under the producer folder root |
| `scripts/production/unit_phases.py` | 1 | Render the stored attribution from `HoldRecord.by` |
| `scripts/review_links/list_page.py` | 28 | Authorize by ID and render current labels |
| `scripts/review_links/review_submissions.py` | 1 | Reuse the List owner ID reader |
| `scripts/whoami/quota_alert.py` | 3 | Read and route FYI recipients by ID |
| `templates/producer/README.md` | 1 | Use the role in the heading |
| `commands/agents.md` | 2 | Use the role and resolve current paths and routes |
| `commands/producer/add.md` | 2 | Use the role and resolve current paths and routes |
| `commands/producer/morty.md` | 19 | Use the role and resolve current paths and routes |
| `commands/producer/produce.md` | 5 | Use the role and resolve current paths and routes |
| `commands/producer/retire.md` | 2 | Use the role and resolve current paths and routes |
| `commands/recall.md` | 7 | Use the role and resolve current paths and routes |
| `commands/remove_command.md` | 4 | Use the role and resolve current paths and routes |
| `commands/showrunner/produce.md` | 11 | Use the role and resolve current paths and routes |
| `commands/showrunner/units.md` | 1 | Use the role and resolve current paths and routes |
| `docs/as-built/accounts.md` | 1 | Use current role references and preserve dated records |
| `docs/as-built/build-memory-admission.md` | 1 | Use current role references and preserve dated records |
| `docs/as-built/buildlog-memory-stalls.md` | 1 | Preserve the dated record |
| `docs/as-built/builds-aws-target.md` | 4 | Use current role references and preserve dated records |
| `docs/as-built/hierarchy.md` | 2 | Use current role references and preserve dated records |
| `docs/as-built/recall.md` | 4 | Use current role references and preserve dated records |
| `docs/as-built/remove-command.md` | 2 | Use current role references and preserve dated records |
| `docs/as-built/review-links.md` | 32 | Use current role references and preserve dated records |
| `docs/as-built/showrunner-automation.md` | 13 | Use current role references and preserve dated records |
| `docs/as-built/shutdown-and-restart.md` | 6 | Use current role references and preserve dated records |
| `docs/as-built/staff-accessors.md` | 9 | Use current role references and preserve dated records |
| `docs/plans/builds-natest.md` | 2 | Use current role references and preserve dated records |
| `docs/plans/builds-queue.md` | 1 | Use current role references and preserve dated records |
| `docs/plans/builds-queue-view-boards.html` | 1 | Preserve the font declaration as an exact guard exception |
| `docs/plans/builds-report.md` | 1 | Use current role references and preserve dated records |
| `docs/plans/builds-resources.md` | 2 | Use current role references and preserve dated records |
| `docs/plans/cargo-style-api.md` | 1 | Use current role references and preserve dated records |
| `docs/plans/cargo-style.md` | 11 | Use current role references and preserve dated records |
| `docs/plans/gate-rename.md` | 5 | Use current role references and preserve dated records |
| `docs/plans/producer-transfer.md` | 22 | Use current role references and preserve dated records |
| `docs/plans/staff-lifecycle.md` | 1 | Use current role references and preserve dated records |
| `docs/producer/morty-operations.md` | 6 | Use the role and resolve current paths and routes |
| `mods/postmaster/tests/protocol.test.ts` | 2 | Use synthetic names or staff IDs |
| `mods/postmaster/tests/replies.test.ts` | 7 | Use synthetic names or staff IDs |
| `scripts/accounts/test_accounts.py` | 1 | Use synthetic names or staff IDs |
| `scripts/agents/test_catch_up.py` | 2 | Use synthetic names or staff IDs |
| `scripts/agents/test_pane_record.py` | 1 | Use synthetic names or staff IDs |
| `scripts/agents/test_producer_lifecycle.py` | 74 | Use synthetic names or staff IDs |
| `scripts/agents/test_producer_transfer.py` | 17 | Use synthetic names or staff IDs |
| `scripts/agents/test_restart.py` | 13 | Use synthetic names or staff IDs |
| `scripts/agents/test_set_model.py` | 63 | Use synthetic names or staff IDs |
| `scripts/agents/test_shutdown_cli.py` | 10 | Use synthetic names or staff IDs |
| `scripts/agents/test_stop_resume.py` | 64 | Use synthetic names or staff IDs |
| `scripts/aws_target/fixtures/aws_target.conf` | 1 | Set the fixture recipient ID |
| `scripts/aws_target/test_ledger.py` | 1 | Use synthetic names or staff IDs |
| `scripts/build_hold/test_target_hold.py` | 1 | Use synthetic names or staff IDs |
| `scripts/build_queue/test_head_stall.py` | 15 | Change the fixture after freeze355 |
| `scripts/herdr/test_conversation_reader.py` | 2 | Use synthetic names or staff IDs |
| `scripts/herdr/test_herdr.py` | 2 | Use synthetic names or staff IDs |
| `scripts/hooks/test_sendmessage_miss.py` | 17 | Use synthetic names or staff IDs |
| `scripts/lib/test_models_registry.py` | 26 | Use synthetic names or staff IDs |
| `scripts/mac_test/test_mac_test.py` | 2 | Use synthetic names or staff IDs |
| `scripts/message/test_morty_identity.py` | 13 | Use synthetic names or staff IDs |
| `scripts/message/test_repeat_guard.py` | 25 | Use synthetic names or staff IDs |
| `scripts/message/test_send.py` | 32 | Use synthetic names or staff IDs |
| `scripts/production/test_delivery_state.py` | 1 | Use a synthetic staff ID in the legacy record fixture |
| `scripts/production/test_investigation.py` | 1 | Use synthetic names or staff IDs |
| `scripts/production/test_merge_checkpoint.py` | 1 | Use synthetic names or staff IDs |
| `scripts/production/test_session_staff.py` | 5 | Use synthetic names or staff IDs |
| `scripts/production/test_staff.py` | 40 | Use synthetic names or staff IDs |
| `scripts/production/test_the_list.py` | 377 | Use synthetic staff and preserve legacy fixtures |
| `scripts/production/test_update_registration.py` | 9 | Use synthetic names or staff IDs |
| `scripts/production/test_update_registration_questions.py` | 2 | Use synthetic names or staff IDs |
| `scripts/review_links/test_change_page.py` | 5 | Use synthetic names or staff IDs |
| `scripts/review_links/test_list_page.py` | 374 | Use synthetic staff and preserve legacy fixtures. Derive feedback labels from the test producer. |
| `scripts/review_links/test_new_item_dialog.py` | 19 | Use synthetic names or staff IDs |
| `scripts/review_links/test_review_page.py` | 5 | Use synthetic names or staff IDs |
| `scripts/review_links/test_review_repairs.py` | 1 | Use synthetic names or staff IDs |
| `scripts/review_links/test_review_submissions.py` | 4 | Use synthetic names or staff IDs |
| `scripts/review_links/test_serve.py` | 115 | Use synthetic names or staff IDs |
| `scripts/statusline/test_statusline.py` | 3 | Use synthetic names or staff IDs |
| `scripts/whoami/quota_alert_test_support.py` | 1 | Set the fixture FYI IDs |
| `scripts/whoami/test_quota_alert_refusals.py` | 3 | Use synthetic names or staff IDs |
| `scripts/whoami/test_quota_zero_episode.py` | 1 | Use synthetic names or staff IDs |
| `~/.codex/AGENTS.md` | 4 | Resolve the List producer and its current folder |
| `~/.codex/skills/generated-from-claude/agents/SKILL.md` | 2 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/producer-add/SKILL.md` | 2 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/producer-morty/SKILL.md` | 23 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/producer-produce/SKILL.md` | 5 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/producer-retire/SKILL.md` | 2 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/recall/SKILL.md` | 7 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/remove_command/SKILL.md` | 4 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/showrunner-produce/SKILL.md` | 11 | Regenerate from the command |
| `~/.codex/skills/generated-from-claude/showrunner-units/SKILL.md` | 1 | Regenerate from the command |
| `~/.local/state/staff/staff.json` | 11 | Use the name fields and update derived paths on rename |
| `~/rust/hanadocs/.obsidian/workspace.json` | — | `lastOpenFiles` stores workspace history. Preserve its paths. |
| `~/rust/hanadocs/bob/README.md` | 41 | Use the role and update current paths |
| `~/rust/hanadocs/brain/README.md` | 12 | Use the role and update current paths |
| `~/rust/hanadocs/dwight/README.md` | 11 | Use the role and update current paths. Include the timer restore instruction on line 27. |
| `~/rust/hanadocs/dwight/nate-interview.md` | 1 | Use the role on line 3. Preserve stored attribution in dated entries. |
| `~/rust/hanadocs/dwight/open-for-nate.md` | 1 | Use the role in the timer reporting rule on line 109. Preserve attribution in dated watch entries. |
| `~/rust/hanadocs/dwight/tools/a.py` | 1 | Outside the repo. Dwight changes the fixed Claude scratch import. |
| `~/rust/hanadocs/dwight/tools/b.py` | 1 | Outside the repo. Dwight changes the fixed Claude scratch import. |
| `~/rust/hanadocs/dwight/tools/c.py` | 1 | Outside the repo. Dwight changes the fixed Claude scratch import. |
| `~/rust/hanadocs/dwight/tools/d.py` | 1 | Outside the repo. Dwight changes the fixed Claude scratch import. |
| `~/rust/hanadocs/dwight/tools/k.py` | 1 | Preserve the stored attribution on line 7. |
| `~/rust/hanadocs/morty/README.md` | 18 | Outside the repo. Morty changes the name and tool references. |
| `~/rust/hanadocs/morty/counts_refresh.py` | 1 | Outside the repo. Morty uses the script folder for its outputs. |
| `~/rust/hanadocs/morty/tools/question_rotation.py` | 3 | Outside the repo. Morty changes it to read the name from staff. |
| `~/rust/hanadocs/morty/tools/README.md` | 4 | Outside the repo. Morty documents how to read the name from staff. |

## 2. The one place
Use `rows[].name` in `~/.local/state/staff/staff.json`. Change it only through `agents rename OLD NEW`. Require its authority reservation at the staff commit. Route to `staff:<id>`. Resolve names through `staff.producer_name(staff_id, *, members=None)`. Reuse one snapshot per request. Render tabs, List items, questions, and sender labels from the current row. Use `page.list.owner` for keeper authorization and retirement protection. Permit the List producer to rename. Write new List actor and requester IDs. Read legacy values. Use staff IDs for launch profiles. Use the saved creation profile before admission. Publish it by staff ID after `staff.ensure` and before `staff_admitted`. Apply this order to creation and `adopt_named_producer`, including retries. Read startup files from the retained staff document location. Use the parent of a recorded document file. Keep the startup path on a name-only rename.
## 3. Config keys for sdlc
Convert the model keys with their readers. Remove the replaced keys. Validate IDs at each reader. An empty FYI list sends zero copies. Keep `[producer_defaults].model` as the default. Remove the `[producer_creations].<submitted name>` request after creation completes.

| File | Old key and value | New key and value |
| --- | --- | --- |
| Staff registry | `rows[].name` = `morty`, `brain`, `dwight`, `bob` on their producer rows | Same fields and values at rollout. Change the selected name through `agents rename`. |
| `config/models.conf` | `[producers].morty=claude:fable:high`<br>`[producers].brain=claude:fable:high`<br>`[producers].dwight=claude:fable:high`<br>`[producers].bob=claude:fable:high` | `[producers].01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a=claude:fable:high`<br>`[producers].01a19d07-c162-7b43-bb48-3ee6c913b3f0=claude:fable:high`<br>`[producers].920b0a64-6eee-479e-8c07-e03e4dcd4ef6=claude:fable:high`<br>`[producers].a898cb38-dbac-4751-b72b-d0b14ab9c38d=claude:fable:high` |
| `config/accounts.conf` | `no_worker_mode_fyi=morty dwight` | `no_worker_mode_fyi_staff_ids=01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a 920b0a64-6eee-479e-8c07-e03e4dcd4ef6` |
| `config/aws_target.conf` | `cost_explorer_change_recipient=bob` | `cost_explorer_change_recipient_id=a898cb38-dbac-4751-b72b-d0b14ab9c38d` |
| `config/build_admission.conf` | Recipient tuple in code | `head_stall_alert_producer_id=a898cb38-dbac-4751-b72b-d0b14ab9c38d` |
| `config/staff.conf` | List requester name in code | `LIST_REQUESTER_PRODUCER_ID=920b0a64-6eee-479e-8c07-e03e4dcd4ef6` |
| `config/console.conf` | `page.list.owner=01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a` | Reuse this key and value. Remove the producer name comment. |
| `config/staff.conf` | `PRODUCER_FOLDER_ROOT=~/rust/hanadocs` | Reuse this key for both paths. |
| `config/staff.conf` | L176 follow-up | Nate approved `PRODUCER_FOLDER_LIMIT_MB=10`, range `1..`. Implement this in L176. |

## 4. Builds hunks for sdlc
```diff
--- a/config/build_admission.conf
+++ b/config/build_admission.conf
@@ -129,3 +129,3 @@
 outage_notice_seconds=60
-# Alert builds and the user once when the queue head waits this long while
+# Alert the Builds session and the configured producer when the queue head waits this long while
 # running steps hold memory and no step starts.
@@ -133,2 +133,4 @@
 head_stall_alert_minutes=30
+# Producer staff ID for head-stall notices.
+head_stall_alert_producer_id=a898cb38-dbac-4751-b72b-d0b14ab9c38d
 # Alert once when an unreadable memory reading holds build starts this long.
--- a/scripts/build_queue/head_stall.py
+++ b/scripts/build_queue/head_stall.py
@@ -14,3 +14,3 @@
 import sys
-from typing import Literal, cast
+from typing import cast

@@ -20,5 +20,6 @@
 from scripts.buildlog import store
-from scripts.lib import conf
+from scripts.lib import conf, file_lock
 from scripts.lib.atomic_file import write_text_atomic
 from scripts.lint import memory_admit as gate
+from scripts.production import staff

@@ -28,4 +29,4 @@

-type AlertRecipient = Literal["builds", "bob"]
-ALERT_RECIPIENTS: tuple[AlertRecipient, ...] = ("builds", "bob")
+ROLLOUT_RECIPIENT = "staff:a898cb38-dbac-4751-b72b-d0b14ab9c38d"
+LEGACY_RECEIPT = "bob"

@@ -94,5 +95,4 @@

-def configured_limits() -> tuple[float, float]:
+def configured_limits(settings: conf.ConfigRead) -> tuple[float, float]:
     """The stall limit and the oldest usable snapshot age, in seconds."""
-    settings = conf.read("build_admission")
     return settings.number("head_stall_alert_minutes") * 60, settings.number("silence_seconds")
@@ -226,15 +226,18 @@

-def _delivered_stalls(state: Path) -> dict[AlertRecipient, float | str]:
+def _delivered_stalls(state: Path) -> dict[str, float | str]:
     try:
         raw = cast(object, json.loads(state.read_text()))
-    except (OSError, ValueError):
+    except FileNotFoundError:
+        if state.is_symlink():
+            raise
         return {}
     saved = cast(dict[str, object], raw).get("alerted") if isinstance(raw, dict) else None
-    # An old key covers builds only. It cannot confirm delivery to bob.
+    # A scalar receipt covers the Builds session.
     values = cast(dict[str, object], saved) if isinstance(saved, dict) else {"builds": saved}
-    delivered: dict[AlertRecipient, float | str] = {}
-    for recipient in ALERT_RECIPIENTS:
-        value = values.get(recipient)
+    delivered: dict[str, float | str] = {}
+    for recipient, value in values.items():
         if isinstance(value, (int, float, str)) and not isinstance(value, bool):
             delivered[recipient] = value
+        else:
+            raise ValueError(f"{state}: invalid receipt for {recipient}")
     return delivered
@@ -250,3 +253,4 @@

-def send_alert(stall: HeadStall, state: Path, now: float, run: Callable[[list[str], str], bool]) -> None:
+def _send_alert(stall: HeadStall, state: Path, now: float, run: Callable[[list[str], str], bool],
+                recipients: tuple[str, ...]) -> None:
     """Record each accepted alert before sending to the next recipient."""
@@ -257,4 +261,7 @@
     write_errors: list[OSError] = []
-    for recipient in ALERT_RECIPIENTS:
-        if recipient in delivered and _already_alerted(delivered[recipient], stall.last_start):
+    for recipient in recipients:
+        receipt = delivered.get(recipient)
+        if receipt is None and recipient == ROLLOUT_RECIPIENT:
+            receipt = delivered.get(LEGACY_RECEIPT)
+        if receipt is not None and _already_alerted(receipt, stall.last_start):
             continue
@@ -274,6 +281,16 @@
     """Alert once for each stall. A new step start permits another alert."""
-    limit, freshest = configured_limits()
+    settings = conf.read("build_admission")
+    limit, freshest = configured_limits(settings)
     stall = current_stall(paths, now, limit, freshest)
-    if isinstance(stall, HeadStall):
-        send_alert(stall, paths.state, now, run)
+    if isinstance(stall, NoStall):
+        return stall
+    try:
+        identity = staff.native_id(settings.text("head_stall_alert_producer_id", recover_invalid=False))
+        recipients = ("builds", f"staff:{identity}")
+    except (conf.Unusable, staff.StaffRefused) as error:
+        print(f"head-stall watch: producer recipient unavailable: {error}", file=sys.stderr)
+        recipients = ("builds",)
+    state = paths.state
+    with file_lock.exclusive(state.with_suffix(".lock")):
+        _send_alert(stall, state, now, run, recipients)
     return stall
--- a/config/aws_target.conf
+++ b/config/aws_target.conf
@@ -55,4 +55,4 @@
 cost_explorer_unchanged_reads=2
-# Send notices about changes to settled amounts to this session.
-cost_explorer_change_recipient=bob
+# Staff ID for changes to settled costs and billed hours gaps.
+cost_explorer_change_recipient_id=a898cb38-dbac-4751-b72b-d0b14ab9c38d
 # UTC days in the report's gate type comparison, including the report day.
```
Update the settled-cost reader, `hours_gap_notices`, and their senders to use `cost_explorer_change_recipient_id`. Remove `HOURS_GAP_RECIPIENT`. Update the AWS docs and fixtures, including `scripts/aws_target/test_control.py`.

1. Stop `buildlog-sample.timer` and let its active run finish.
2. Install the watcher and config key from the same commit.
3. Verify the rollout staff ID in the live `build_admission.conf` before resuming the timer.
4. Attempt Builds first. Under the lock, use the legacy receipt as a fallback only for the rollout staff ID. Store the staff receipt after its first accepted send.
## Rename
Keep one durable global rename slot at the staff authority through the final vault scan. The slot belongs to one operation ID and survives process exit. Other renames wait until release. Check the authority reservation before resume, restart, transfer, or account moves. Move files only when the worktree is `PRODUCER_FOLDER_ROOT/<old>`. A transferred producer with another worktree keeps its paths and conversation. Skip the vault rewrite on that path.
If the old name has an active lookup, get its operation ID. Read its completion record before resolving the current owner or checking a session. If complete, finish any pending release by ID and clear the lookup for that ID. After cleanup, resolve the current owner of the old name. If the request matches the completed rename, return its saved result when no other staff ID owns the old name. Skip the stopped-session check for that result. Use the current owner for a new request.

1. Take the producer row lock before preflight. Hold it through completion. Read the launch profile by staff ID. Resolve the current native session backend as resume does. Refuse a mismatch and name both backends. Preserve the profile and native session on refusal. Require `herdr.read_conversation` to return `StoppedConversation` for the native session backend. Record the operation ID and the planned name, document and worktree locations, and native session ID. Include each location's machine and path. For a folder move, record the source folder identity and destination. For Claude, also record the source identities and destinations for the transcript, session directory, and links. For a Claude folder move, require the current transcript in the exact old project. Treat identical Claude project keys as one location. Sync the complete plan before claiming the reservation.
   In the shared staff writer, compare every planned protected field with the current row under the staff authority lock. Refuse any difference. Reject an occupied new name or a creation claim for either name. Reject another staff row that uses the source folder. Claim the slot and reserve both names and the staff ID in the same transaction. For a move, include the source folder and each destination. Recover an accepted claim by operation ID after a lost reply.
   Require the rename operation ID for every write to a protected field. Apply this rule in the shared staff writer. Cover `set_worktree`, `set_doc`, `update-current-session`, `transfer_producer`, `rename`, `ensure`, promotion, and creation. Check creation claims and retirement under the same lock. Refuse location writes that touch reserved paths. Allow the reserved operation's recorded commit.
2. Under the reservation, verify source identities and check destination conflicts again. On any refusal before changes start, release the operation's slot and reservations by ID. Confirm their release or absence with the authority. Clear the active plan only after confirmation. The next run preflights again. Sync the start of changes before the first mutation. Keep the slot after any later failure. Move the folder to `PRODUCER_FOLDER_ROOT/<new>` after preflight passes. Once changes start, accept a destination on retry only if it matches the recorded source and the source is absent. Sync the parent before advancing. Preserve tracked and untracked files. The folder move carries vaultsave's `.gitignore` and its relative media rules. Leave the file unchanged.
   Unit worktrees and producer folders use disjoint roots. In `unit_move.py`, refuse a resolved source or destination at or under `PRODUCER_FOLDER_ROOT` before moving files.
3. For Claude, move the planned transcript `<id>.jsonl` or `<id>.jsonl.zst` and its `<id>/` session directory from the old project. Deduplicate shared project roots. Replace only links to the moved project files. Verify and sync each move or link on retry. Require the exact Claude project key for the new cwd on resume. Keep all other copies and unrelated files. For Codex, keep the native thread in its store and resume it from the moved folder with its original native ID.
4. Rewrite `cwd` only in legacy native launches for the moved folder. Keep staff-bound launches derived from the row. Write and sync completed creation evidence by staff ID. Verify it before removing and syncing the old `.producer-add-<name>` lookup. On retry, verify the launch and receipt records against their journaled states.
5. For a folder move, scan the vault for full `hanadocs/<old>/` paths and matching Obsidian links. Resolve vault-relative link targets before matching. Preserve syntax, aliases, and the paths in Obsidian's `lastOpenFiles`. Journal the expected bytes and changed line counts for each file that the scan changes. Sync the journal before each replacement. On retry, sync the parent before marking expected new bytes as complete. Count the file once. Rescan while the same operation ID holds the durable slot. Fail if any old full path remains outside the named record or the stored workspace history.
6. Commit the name and eligible path changes through `staff.rename` with the matching reservation. Compare only the recorded old or new protected fields inside the shared writer. Apply the changes to the latest row. Preserve its other fields, including pause and hold records. Keep missing doc paths as text. Change their folder prefix when that folder moves.
7. Repair the tab and sync completion evidence by operation ID. Keep this evidence separate from the active name lookup. After changes start, release the reservations and slot by operation ID only after verified completion. Clear the active lookup if it still belongs to that operation. Report the total count of distinct changed vault lines from the journal.

Refuse unrecorded source, destination, or row states. Keep the journal for a retry after changes start. The vault scan found 49 text lines and zero folder-prefixed Obsidian links. Preserve `~/rust/hanadocs/morty/checkins.json` at its original or moved location. The rename command moves the folder and rewrites links with Obsidian closed. It uses no Obsidian command-line tool. List item 177 tracks Git for the producer folders. The producer brain owns it. The phase that reaches main second, prodnames or vaultsave, implements the media rule integration. Write relative media rules to the producer folder's `.gitignore` during creation. Preserve one copy of each rule on creation retries.
## Resume proof
1. Use a synthetic Claude producer, shared project roots, and a project-specific link. Verify its transcript and session directory at `projects/-home-natepiano-rust-hanadocs-<new>/` through every account. Add another copy under the home project and verify its bytes stay in place. Resume from the moved folder. Verify an old turn and a new turn under the original native ID.
2. Inject crashes after reservation acceptance, the folder move, and the transcript move before the session directory or link moves. Test receipt and row commits, completion before release, and release before lookup removal. Resume the producer after release and before lookup removal. Retry the rename and verify it clears the lookup without a stopped-session check. Test a transferred producer's name-only rename.
3. Check refusal of live sessions, conflicts, and unreserved staff commits. Give a stopped native session a profile for the other backend. Verify preflight names both backends and refuses before changing files, the profile, or the staff row. Test a prior creation claim and reserved name writes from unit rename, promotion, and admission. Change the doc path before the claim and verify refusal. Add a filesystem conflict during the claim and verify preflight refuses before any move. Verify both refusals clear the active plan after authority confirmation. Verify the next invocation preflights again. Verify `move-folder` refuses either endpoint under the producer root before moving files. Test resume and a competing location write during the move. Check that `update-current-session` refuses after a rename crash. Verify the rename commit preserves an intervening pause. Verify a different rename waits. Remove the Claude transcript after the claim and verify preflight releases the slot.
4. Test the move and rewrite with Obsidian closed. Include a missing doc path, a bare name, repeated matches on one line, and the listed record. Verify Obsidian's `lastOpenFiles` stays unchanged. Inject a directory sync failure after replacement. Verify the changed line count after retry. Verify failure when a matching path remains. With vaultsave, verify the folder move carries its `.gitignore` unchanged. Verify creation retries keep one copy of each rule.
5. Start with only `<id>.jsonl.zst` in the old Claude project. Rename and use native resume from the new folder. Verify an old turn and a new turn under the original native ID. Move a Codex producer folder and resume its native thread from the new folder. Verify an old turn and a new turn under the original Codex native ID.
6. Complete `A → B` and crash after release but before lookup removal. Create another producer named `A` with a different staff ID. Request `A → C`. Verify the request clears the completed lookup by its ID before resolving `A`. Verify `C` has the second producer's staff ID and `B` keeps the first.
## Folders in use now
The folders are under `~/rust/hanadocs`. Project paths are relative to `~/.claude5/projects`. All five account `projects` links resolve to `~/.claude/projects`.

| Folder | Staff row ID | Current conversation project |
| --- | --- | --- |
| `bob` | `a898cb38-dbac-4751-b72b-d0b14ab9c38d` | `-home-natepiano-rust-hanadocs-bob` |
| `brain` | `01a19d07-c162-7b43-bb48-3ee6c913b3f0` | `-home-natepiano-rust-hanadocs-brain` |
| `dwight` | `920b0a64-6eee-479e-8c07-e03e4dcd4ef6` | `-home-natepiano-rust-hanadocs-dwight` |
| `morty` | `01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a` | `-home-natepiano` |

The Morty row records the absent `morty/current.md` as its doc and `~` as its worktree. Other rows use their folder for both paths. Git tracks files in `morty`. The other three folders are untracked. The non-producer folders are `.claude`, `.git`, `.obsidian`, `.ok`, `.trash`, `agents`, `archive`, `as-built`, `change-review`, `conf`, `diary`, `feature-log`, `home-base`, `issues`, `machines`, `poc`, `showrunners`, `topics`, and `ux`.
## 5. Tests and guard
Use temporary config. After freeze355, test unreadable receipts and both destinations. Verify a matching legacy receipt suppresses only the rollout send. Verify an accepted staff send stores its receipt and that this receipt takes precedence. Test stable routes, profiles, List values, and role checks.
Track the guard inputs in `scripts/production/producer_name_guard_names.txt` and `scripts/production/producer_name_guard_exceptions.json`. Exclude both inputs from the scan by exact path. Scan for rollout names `morty`, `brain`, `dwight`, and `bob`. Add later names through normal code changes. Split identifiers at underscores and lower-to-upper case changes. Match whole tokens case-insensitively. Include possessive name tokens such as `bobs`. Report file and line.
Match each exception to its exact path and full line text. Consume each entry once. Fail on unused entries or extra matches. Expand the following base references at `94e07d22c4` into exact text during implementation.

- Frozen `scripts/build_queue/test_head_stall.py` lines 285, 293, 304, 313, 315, 325, 339, 342, 344, 354, 372, 377, 382, 383, 385, 405. Remove these entries after 355.
- Legacy `scripts/production/the_list.py` lines 50 and 204. Match `LEGACY_RECEIPT = "bob"` in `scripts/build_queue/head_stall.py`.
- Legacy feedback input in `scripts/review_links/test_list_page.py` line 1679. Match the full input line that contains `Feedback to morty:`.
- Font declaration in `docs/plans/builds-queue-view-boards.html` line 12. Match `  border:1px solid var(--border); border-radius:6px; font:15px/1.35 ui-monospace, "JetBrains Mono", "DejaVu Sans Mono", Menlo, monospace;`.
- Preserved records in `docs/as-built/buildlog-memory-stalls.md` line 124 and `docs/as-built/builds-aws-target.md` line 1230. Preserve the listed lines in `docs/plans/`. Use `builds-natest.md` 3175, `builds-queue.md` 2446, `builds-report.md` 1937, and `builds-resources.md` 2570 and 2925. Use `cargo-style-api.md` 4, `cargo-style.md` 3, 8, 14, 48, 122, 178, 185, 225, and `gate-rename.md` 3, 5, 12. Use `producer-transfer.md` 34, 38, 41–43, 45–47, 49–50, 54, 58, 69–70, 86, 88, 128, and 137.
