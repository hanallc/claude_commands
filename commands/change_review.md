---
description: Add a change for the user to check to the top-level showrunner's change review list, show the next change when the user asks for one, and record their verdicts.
---

# Change review

Each action refuses an option it does not read.

## Bare command

For `/change_review` with no action:

The command prints the Hana console link.
Review combines questions, changes, investigations, proposals and efficiencies.
The Show row offers Open, Validating, Done and All.
Cards with actions have Your words, closing actions, Ask question and Later.
Send feedback closes questions, investigations, proposals and efficiencies.
Ask showrunner to check, Ask question and Send feedback transfer a change to its showrunner.
The change moves from Open to Validating.
Validating and All show **With <showrunner> since <time>** in the user zone.
The row has no buttons.
Only the Validating chip counts the change.
The Validating chip is dim at zero.
The server refuses a second request while the change is Validating.
A showrunner answer, feedback action or fault returns the change to Open.
A works result moves the change to Done without a notice.
Ask question sends the words to the owner and returns the item after the owner answers.
Owners use `change_page.py answer-question --item <Review key> --question <id> --text "<answer>"` for all five types.
Use the question ID from the request message.
Later keeps the state and hides the item from Review, the For you count and `/next` until its time.
A change hand-off clears the saved Later time.
The canonical question log retains the user's answer words without a time limit.

1. Run the page script:

   ```sh
   "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py"
   ```

2. Check the printed link with `curl` on the publishing machine.
3. Give the user only the link, then stop. Say nothing else.

The actions below remain for unit directors and showrunners.

Each top-level showrunner keeps one list of changes waiting for the user's
check, named for it: `~/rust/hanadocs/change-review/change-review - <showrunner>.md`,
such as `change-review - natedev.md`. A child production's rows go to its
root's list. `list_ready` adds checkpoint changes only after they reach `origin/main` and become live.
This command adds other ready changes, shows the list, and records verdicts.

Every action runs:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/change_review.py" <action> …
```

On exit 1 report its one line.

## Find the list

Every action finds the list from its production, written `[--production <name>]` below:

- A showrunner passes nothing: the script finds the production from this
  session's update timer, as `/showrunner:proposals` does. A session with no
  timer passes `--production <showrunner name>`.
- A unit director passes `--production <showrunner name>` from the parent showrunner row's `name`.

The list is named for the production's top-level showrunner while it runs, and
otherwise is the path the doc's **Change review** line names. `list-path`
prints it and `showrunner` prints the name. A production doc without a Change
review line has no list: say so in one line. The showrunner adds the line
(`~/.claude/docs/production_format.md`). A missing list file is created on its
first row, with its frontmatter.

Every write sets the list's `date_modified` and adds the production's doc to
its `productions`, so the list's frontmatter names every production it holds.

Every write commits the list through Git plumbing without an index refresh.
Do not commit the list by hand. A `written but not committed` warning needs nothing: the
write stands and the next write commits it. The vault's Templater must exclude
the change-review folder, since its creation trigger rewrites a new list within
half a second and can drop a row; the vault owner sets Excluded folders →
`change-review`.

## add

A row requires a change the user can check now, merged where they run it and live after rebuild, restart or deploy.
Until then, add no row.
`add --merged` refuses a hash that is not on `origin/main`.

For a change the user should check that no checkpoint `Change-Review:` line
carries: a showrunner's own change, a fix outside a run, or a change nobody
sees in the app, such as a table's columns or a status line.

```sh
… add [--production <name>] --change "<what changed for you>" --check "<how to try it: where and what to type>" --see "<what you see>" [--merged <hash>] [--urgency <level>] [--due <ISO>] [--after C<n>] [--image <png> | --output <text file>]
```

`--urgency` is `blocks` (work stops until the user checks it), `soon` (with
`--due`), `later` or `user`; leave it out for a normal row. `--after` names a
row that must be checked first.

One row per change, written for a person with no context.
The change description uses one line. The check and result can use several lines.
`--check` names the machine (natedev or the Mac), the session (a
terminal, a showrunner session, a unit session, any Claude session) and what to
type; `--see` is what the person then sees. Example: `--change "The Units table
shows each unit's phase" --check "In a showrunner session on natedev, type
/showrunner:units" --see "Each row has a Phase column"`. The row's How to
check cell is `<how to try it> You see: <what you see>`. Pass `--merged` when a
commit holds the change. The Merged cell holds the date and time of the add, in the
user's zone, then the commit: `2026-11-13 16:20 · 31770c75`. It prints the new row's id, such as `C12`.

## Change images

Every change review carries one image: a dark mode shot at desktop width, or `image --output <text file>` for terminal output.
The commit hook requires a final ` | image <id>` field on each visible Change-Review line.
Merge checks require that field for commits dated at or after 2026-11-16 00:00 EST, using the Git committer time.
Use `add --image <png>` to attach an existing screenshot to a manual row.
Use `edit --row <id> --image <png>` to attach or replace an image on an existing row.
Use `add --output <text file>` or `edit --row <id> --output <text file>` to attach rendered text.
Pass one of `--image` and `--output`. Other actions refuse these options except `image`.
An image replacement deletes the old image after the row is written.
For a checkpoint, register the image first:

```sh
… image --image <png>
… image --output <text file>
```

Use one option. `--output` renders the text as a dark terminal screenshot with headless Chrome.
The action prints an image id and a hint.
Add ` | image <id>` as the last Change-Review field, after ` | after <step>` when present.
The id contains twelve lowercase hexadecimal characters.
`list-ready` and `live` copy it into the row's `Image` cell.
A missing image prints one line and leaves the cell empty.

The console card and the one-row page show a 280px thumbnail beside the change text.
The thumbnail moves below the text on narrow screens.
Click the thumbnail to zoom over the page.
Press Escape or click the zoom to return.
Shift-click or use the right-click menu to open the full image in a new tab.
A landing or return verdict deletes the image and clears its cell.
Change images use `change-images/` and never appear on the Images page.
`| image <id>` accepts a change image id or an Images page id.
An Images page id copies its file into `change-images/` and uses the new id in the row.
For a pair, it copies the after image. The source stays on the Images page.
The server deletes unclaimed images after `change_image_unclaimed_days`, which starts at 14 days.

## list-ready and live

All merge callers use `list_ready` after the final promotion step succeeds, including the Mac pull.
It fetches `origin main` and scans merge commits from 2026-11-06.
It adds each uncovered checkpoint line, including after an interrupted call.
A row covers a text-matched change when its merge contains that checkpoint or its merge is unknown.
An edited row covers a change from the same merge only when its Producer matches the checkpoint unit's fixed staff id.
Within each producer, pair ready changes first, in commit order.
Pair held changes next only when all uncovered held changes for that producer wait on one step.
An empty Producer cell or an unknown checkpoint producer permits only text matching.
A later fix can add a new row with the same words.
A failed fetch prints one error line and adds nothing.
The `append` and `drift` actions are removed.

```sh
… list-ready --repo <checkout> --production <showrunner>
```

A checkpoint line can end with ` | after <step>` when it needs a rebuild, restart or deploy.
The scan prints the merge, the step and the exact `live` command only for uncovered lines.
The session that completes the step runs that command:

```sh
… live --merged <hash> --step <step> --repo <checkout> --production <showrunner>
```

`live` requires both `--merged` and `--step`.
It adds only that merge's lines that wait for the named step, after the merge reaches `origin/main`.
A step with no matching line gives an error.
A repeated call adds nothing.
A hash outside `origin/main` takes the same refusal as `add --merged`.

## edit

To rewrite a row still to review, such as one that lacks where to try it or
what the person sees:

```sh
… edit [--production <name>] --row <id> --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
… edit [--production <name>] --row <id> --image <png>
… edit [--production <name>] --row <id> --output <text file>
```

Pass the displayed ID, such as `sdlc/C9`.
The prefix names the responsible showrunner, even when another showrunner's list stores the row.
Commands also accept the list-based ID and a bare row number with the selected list.
Pass `--list <path>` when the displayed ID matches several lists.
The row keeps its id, its Merged cell and its place. It prints the new row.
Add `--image <png>` or `--output <text file>` to a text edit to attach or replace the image.
With only an image option, the row keeps all other cells.
With any text option, pass all three text options.

## batch

Apply several edits and verdicts with one folder lock and one commit:

```sh
… batch --file <json>
```

The file contains an array of items:

```json
[
  {"op": "edit", "production": "hana", "row": "hana/C1", "change": "New text", "check": "Open the page", "see": "The result"},
  {"op": "edit", "production": "hana", "row": "hana/C2", "image": "/path/shot.png"},
  {"op": "verdict", "production": "hana", "row": "hana/C3", "verdict": "works", "validation": "<validation id>"}
]
```

Each item can set `list` to the path accepted by `--list`.
For a showrunner check, copy `validation` from the validation message's ID.
Omit `validation` for an ordinary user verdict.
All lists must be in one folder.
Each row can appear only once per list in a batch.
A repeated row reports `batch item <j>: row <id> is already item <i>` before any list write.
After a partial write, repeat the batch to finish the remaining writes and commit all target lists.
An edit with only `image` or `output` keeps the row text.
Use `output` for a rendered text file. Do not combine it with `image`.
An edit with text needs `change`, `check`, and `see`.
An invalid item reports its zero-based index and leaves all lists unchanged.
The command applies items in array order and prints each single command's output.
Image removal and verdict scratch removal follow the writes, outside the lock.

## show

When the user asks to review changes, follow [Bare command](#bare-command).

The page has one tab bar for Questions, Changes, and Rebuilds, with waiting counts.
The selected tab joins the content panel and has an accent line on its top edge.
Use the arrow keys, Home, and End to select a tab.
Questions and Changes show one card at a time.
The compact **Showrunner:** menu starts with **All (n)**, then lists each showrunner with its waiting count.
All is the default and includes every showrunner in the existing item order.
The URL retains the selected type, filter, and card across page loads and automatic updates.
Each type tab shows its own count. The page has no combined total.
The page updates itself without a full reload through its existing GET URL.
`page_refresh_seconds` starts at 15 seconds and permits 5 to 300 seconds.
The page loads updates only while visible and loads at once when its browser tab becomes visible again.
An update retains the type, showrunner, waiting card, open Details sections, scroll position, and retained field focus and cursor.
Typed text or an active save holds replacement; only the newest loaded update waits.
Replacement continues when all unsaved fields are empty or the save ends.
After a successful save, the page requests a fresh update to exclude older responses.
A failed load changes nothing; the next interval tries again.
The heading follows the type: Questions to answer or Changes to review.
An empty showrunner filter shows `No questions for this showrunner.` or `No changes for this showrunner.`
An empty type shows `No questions to answer.` or `No changes to review.`
The selected showrunner filter supplies the count; the card has no separate remaining count.
A question stays with the showrunner whose question list holds it.
An unknown question owner appears under **Owner unavailable**.
Owned question cards show the title before the filed text, choices, and `Asked by <session>`.
They omit the separate Question label.
Choices show as radio items; a choice fills the **Your answer** box.
An **Owner unavailable** card shows `No showrunner owns this question.` without an answer form; the server refuses an answer.
Answer records the words and finishes the question.
Send feedback sends the words to the owner and finishes the question.
Ask question moves it to Waiting until the owner uses the shared answer command.
Later replaces Skip on the Review card.
Older selected-row forms retain Followup and Skip for compatibility.
Review does not open another card after a save.
A stale question refuses an answer if another session already answered it.
Saved question answers use the same delivery attempts, startup recovery, and failure alert as changes.
The delivery record stores `saved: false` before the answer saves and `saved: true` after it saves.
A save error clears the delivery intent and releases its claim.
Recovery sends only `saved: true` answers for closed questions.
Recovery clears other intents; a closed question without save confirmation prints `answer save state unknown; not sent`.
The relay follows the asker's current staff route; an answer without a relay goes to the question's showrunner.

Rebuilds shows natedev before the Mac.
Each block has the machine title, then one entry per change with Who, When and Reason, newest first.
Unknown staff IDs appear as `Unknown session: <id>`.
An empty view shows `No rebuilds needed.`
Rebuilds has no showrunner menu, card navigation, Skip for now, or answer form.
The status line also shows open needs as `rebuild natedev` or `rebuild mac`, even before system evaluation completes.
It combines these needs with cached system comparisons without duplicate machines.
`rebuild.eval_timeout` in `config/statusline.conf` sets the evaluation limit to 120 seconds.
`rebuild.eval_retry_seconds` sets a 600-second wait after failed or partial evaluations; a new commit bypasses this wait.

The Changes tab contains every waiting change.
Each card starts with its 20px title; section labels use 17px text.
How to check or Proof follows the title.
Works, Does not work, Ask showrunner to check, Ask a question, Send feedback, and Skip for now share one wrapping row.
Aligned lines under the buttons always show `change`, `date`, `time`, `commit`, and `kind`; an unknown value has no line.
The Does not work button submits `not works` and saves `not works: <text>` when the user supplies text.
An open Production cell holds the unit's fixed staff id or the showrunner's fixed staff id.
The reader follows a unit's current staff parent and displays current names.
Reviewed rows keep the production name recorded at the verdict.
Renames and transfers do not rewrite these cells.
The **Owner unavailable** option shows an unknown id or a broken staff parent chain.
Verdicts save without sending a message.
These rows hide all showrunner actions, including Send feedback.
The server refuses question and feedback requests without saving.
Its count covers all page lists, including independent production roots.
Menu options with waiting changes precede empty options in alphabetical order.
Each showrunner view follows `/next change` for that production, with its pin and moved rows.
All lists each root's `/next change --all-below` order in turn, with each root's pin and moved rows.
The page reads `changes_first_root` from `config/review_links.conf` at each render.
The named root goes first, and other roots follow by name.
The default is `natedev`.
A name outside the live roots leaves the root order unchanged.
Rows outside these queues go last.
The first card and Skip for now follow the selected view's order.
Keyboard focus keeps its visible outline.
Answers save in the background.
Works and Does not work open the next waiting change at once, before the server answers.
The next card shows **Saving review.** until the save ends, then **Review saved.**
Other answers open the next waiting change in the same filter after a successful save.
Navigation during a save prevents this automatic change.
Skip for now skips a change without saving an answer.
A failed HTTP save keeps the typed text, answer form, and waiting count, and shows `Not saved: <reason>`.
A failed Works or Does not work save puts the row back in the list.
Its notice shows `Not saved: <row>. <reason> It is back in the list.` and an **Open it** button.
An automatic move after another change save shows **Review saved.** above the next card.
An automatic move after a question save shows **Answer saved.** above the next card.
Manual navigation clears this notice.
A direct link to a saved verdict shows **Saved: <answer>**.
Ask showrunner to check removes the row from every waiting count and Skip for now order when the request saves.
A direct link shows **The showrunner will validate this** without answer buttons.
Ask a question uses the **Comment, question or feedback** field.
It removes the row from every waiting count until the showrunner answers.
A direct link shows **The showrunner will answer this** without answer buttons.
Send feedback asks the showrunner to act on the text.
Open feedback removes the row from every waiting count until the showrunner records its action.
A direct link shows **The showrunner will act on this** without answer buttons.
The recorded note shows **Showrunner: <note>**, and the row returns to the count.
Feedback uses the existing question delivery record, retry budget, and startup recovery.
Works and Does not work save the text from **Comment, question or feedback** as their comment.
The server retains its verdict rules for text that starts with `works` or `fine`.
The server manages delivery; the page shows no delivery notes, links, or resend controls.
The server reads `delivery_tries` and `delivery_pause_seconds` from `config/review_links.conf` when delivery starts.
The initial values are 3 tries and a 5 second pause.
Each round sends to all remaining recipients before one pause; later rounds retry only failed recipients.
A `QUEUED:` result counts as kept and needs no retry.
After all tries fail, the server alerts the tab's showrunner once to pass the saved answer on by hand.
The server resolves every staff recipient ID to its current session for each send.
At startup, the server scans the review lists and resumes pending records with the same retry budget.
It ignores legacy page context and reads the current owner and production document through the same function as the browser.
Recovery skips records that a live request owns.
It ignores legacy failed records and preserves validation ownership and open questions.
The router alerts `unrouted_alert_recipient` once for each unrouted row and reason.
It alerts only for a list in the real change-review folder; a list elsewhere never alerts.
It alerts only from the production staff record; a `STAFF_FILE` override never alerts.
This config key starts at `natedev`.
`unrouted_alert_retry_seconds` starts at 60 seconds and accepts 5 through 3600 seconds.
Both keys are in `config/review_links.conf` and are read when used.
Failed alerts remain pending until the configured pause ends.
A nonblocking lock prevents two renders from sending one alert at once.
The router records acceptance only after the sender accepts or queues the alert.
Do not ask for a batch size or take page answers in chat.

The diagnostic command `show [--production <name>]` prints the current row and count.
Use `show --row <id>` to print a selected row, with the same ID rules as `edit --row`.
A row absent from the list gives an error.

The list keeps the order of `/next` (`commands/next.md`): the row `show`
printed stays first until its verdict. When the user calls a row important,
run `mark-important [--production <name>] --row <id>`, with the ID as `/next` shows it,
such as `sdlc/C434` or `sdlc/C9`.

## A folder for the user to try

When the user needs a folder to try a change in (a repro or a copy), make a worktree after `add` prints the row's number.
The try folder needs a repository checkout. Never make the folder by hand.
The team that made the row runs this from its own worktree:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/worktree/worktrees.py" make <repo> <team> try-C<n>
```

`<team>` is the row's Producer (the unit, or the showrunner for its own change), and `<n>` is the row's local number.
It prints `~/worktrees/<repo>/<team>/try-C<n>`. Its `target` link points to the build output `/build/<repo>/<team>/try-C<n>`.
Name that path in the row's How to check cell (`edit`).

The row's verdict or withdraw sends the try folder to `worktrees.py remove` and keeps its branch.
That removal lists the checkout and its build output for manual removal.
The removal by hand of a try folder also deletes its branch `<team>-try-C<n>`.
The hold in `docs/as-built/removal-by-hand.md` keeps script removal off.
A verdict checks only the worktree records on the machine that runs it.
A try folder on another machine stays, and its team removes it there.
A try folder with uncommitted changes, or with commits that are on no remote, stays.
The verdict or withdraw then prints one line with the path, the reason and the command the team runs after it saves the work.
A Git, file or removal error prints `<path> not removed: <first line of the error>`.
Any other error prints `try folder not checked: <first line of the error>`, and the row's verdict stays.
A verdict from the review page keeps its retry record after a `try folder not checked:` line.
Cargo stores final binaries in `target_directory` and intermediates in `build_directory`.
Check both output filesystems with `df -h` before a large build.

## verdict

A verdict beginning `works` or `fine`, after optional `okay`/`ok` and a row id
(`c17`), lands the row. Case and later words do not affect the result; any
other start returns it to its unit. In a Hana or `~/.claude` production, the showrunner first runs the
feature-log step of `/showrunner:produce` → `<CloseChangeReview/>`; a failure
there leaves the row open.
Different review rows can land feature-log notes on one merge link without `--separate-result`.
The review page sends one failure alert per list, row, and error text.
Then:

```sh
… verdict [--production <name>] --row <id> --verdict "<the user's words>" [--validation <id>]
```

Pass the displayed ID, such as `sdlc/C9`, or its existing list-based ID.
For a showrunner check, include `--validation <id>` from the validation message.
An ordinary verdict moves the row to `## Reviewed` with the verdict, today's date and its Merged cell.
It prints `<showrunner>/C<n> landed` or `<showrunner>/C<n> returns to its unit (merged …)`.
It ends the row's scratch task: each scratch folder of the row stays for a manual removal list.
It sends the row's try folder to removal (see "A folder for the user to try").
The Producer cell stays with the row in Reviewed.
Send a returning row and the user's words to its Producer staff id.
Read its current session from the staff record at send time.
An empty Producer cell belongs to the list's showrunner.
The fix returns as a new row.

Rerunning the same verdict prints `already has this verdict`, the first run's
line and the scratch and try folder lines. A different verdict for a Reviewed row is
refused; it still ends the row's scratch folders.

## withdraw

Use this action when the user withdraws an open or reviewed change.

```sh
… withdraw [--production <name>] --row <id> --reason "<the user's words>"
```

Pass the displayed ID, such as `hana/C386`.
The row moves to Reviewed with the verdict `withdrawn`.
The Review log keeps the user's words, the withdrawal time and any previous verdict.
A repeat with the same words leaves the history unchanged.
Like a verdict, it ends the row's scratch task and sends the row's try folder to removal.
Wait for any pending verdict message before you withdraw the row.

## Validate

The validation message asks the row's showrunner to check the change for the user.

1. Read the validation ID from the message.
2. Check the change with evidence from a run, its output or the screen.
3. Save `works (validated by the showrunner: <evidence>; not seen live: <what>)` if the check passes.
4. Save a verdict that names the fault if the check fails.

Use the verdict command from the validation message for either result. It includes `--validation <id>`.
A fault returns the row to Open with the evidence in its log.
A successful check moves the row to Done without a notice.
The command refuses a missing or old validation ID.

## Ask question

1. Read the Review item key, question ID and user's words in the message.
2. Check the item or ask its source unit.
3. Save the answer with this command:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" answer-question --item <Review-key> --question <id> --text "<answer>"
```

Use the question ID from the request message for all five Review types.
The item returns to Open with the answer in its log.
The answer and cells can hold several lines; lines that start with `- ` or `1. ` show as lists.
The user can give a verdict or ask another question.
The same question ID and answer text change nothing and exit 0 on a rerun.
The command refuses an old or unknown ID, a different answer, or a row without an open question.

## Feedback

Send feedback transfers the change to the showrunner.
Use the command in the feedback message to record the action.

1. Read the feedback and its `Feedback id` in the change review message.
2. Complete the directed action.
3. Record what you did with the feedback id:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" answer-question --item <Review-key> --question <id> --text "<what you did>"
```

The note returns the change to Open and its counts.
A verdict closes the row under the existing verdict rules.

## Never a question

A change for the user to check is never put in `open_for_user` and never
counted among the open questions. It goes on this list, and the user asks for
"change review" when they want it.

### Producer ids

Producer retains the unit's fixed staff id for verdict delivery.
Unknown units leave Producer empty and print one named note.
For `add`, `--repo` selects the directory; the default is `.`.
A showrunner directory or an unowned directory records a production-owned change.
