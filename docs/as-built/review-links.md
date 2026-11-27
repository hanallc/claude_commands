# Review links

Review links let a session on natedev or on the Mac show files to the user in a browser. The session copies the named files into a snapshot folder and prints one short URL per file. The user clicks the URL in a terminal, often an ssh session from the Mac into natedev, and the file opens in a browser on the Mac. The URL holds the Tailscale IPv4 address of the machine that has the files, so no file goes from one machine to the other. Markdown files open as rendered pages.

## How it works

| File | Role |
|---|---|
| `commands/review_link.md` | The `review_link` skill. It runs the publisher, checks each link with `curl` and gives the links. |
| `scripts/review_links/review_link.py` | The publisher. It copies files into a snapshot folder, renders Markdown and prints the URLs. |
| `scripts/review_links/serve.py` | The server. It serves the snapshot root on the Tailscale address and deletes old snapshots. |
| `scripts/review_links/test_review_link.py`, `test_serve.py` | unittest suites. The publisher tests use a fake `tailscale` on `PATH` and a temporary home. The server tests bind `127.0.0.1` on port 0. |
| `scripts/review_links/test_review_links_conf.py` | A source scan. It fails when a module in `scripts/review_links/` reads a key that has no row in `config/review_links.conf`. |
| `/etc/nixos/modules/common/review-links.nix` | The shared job `nate.jobs.review-links` for both machines. |
| `/etc/nixos/modules/linux/review-links.nix` | The natedev restart settings and the firewall line. |

Run the tests from the repository root with `~/.claude/scripts/lib/py -m unittest discover -s scripts/review_links -p 'test_*.py'`. They need Python 3.12 or later.

The flow:

1. The skill runs `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--new]` on the machine that has the files. `scripts/lib/py` finds Python 3.12 or later. Inline programs import from the shim’s repository root through `PYTHONPATH` and `-P`. File scripts keep their existing import path behavior.
2. The publisher checks every input, gets the host, writes the snapshot under the index lock and prints one URL per file.
3. The skill fetches each URL with `curl` on the same machine. It gives only the links that return `200`, each as the full URL in plain text. The user's terminal cannot open a Markdown link.
4. The browser on the Mac gets the file from the server on that machine, over the tailnet.

### Publisher

`review_link.py FILE... [--new] [--name LABEL]`

- `--new` makes a separate link for the file set. Without it, a run on a known set refreshes its link.
- Older callers still pass `--name`. The publisher accepts it and ignores it.
- On success it prints one URL per input, in argument order, and exits 0. On an error it prints `review_link: <error>` to stderr and exits 2.

The URL form is `http://<host>:<port>/<id>/<file name>`, with the name percent-encoded.
The `server_port` setting starts at 8765.
For example, a rendered link is `http://192.0.2.254:8765/k3x9qa/herdr-issue-draft.html`.
The URL of a `.md` input names its rendered `<stem>.html`.
The host comes from `tailscale ip -4`, parsed as IPv4.
The `tailscale_timeout_seconds` setting starts at 10.
Empty output, an IPv6 address, a host name, or a nonzero exit refuses the run.

Two environment variables change the defaults. Neither changes the address the server binds.

- `REVIEW_LINKS_ROOT` sets the snapshot root for both scripts. The default is `~/.local/share/review-links`. An empty value counts as unset.
- `REVIEW_LINKS_HOST` sets the URL host, and the publisher then does not run `tailscale`. It uses the value as given and removes only a trailing dot.

Each snapshot is a flat folder `<root>/<id>/`.
The ID has six lowercase hex characters from `secrets.token_hex`.
An exclusive `mkdir` creates the folder.
If an ID already exists, the publisher tries another ID.
The `snapshot_allocation_tries` setting starts at 10.
After that many tries, the publisher refuses the run.
Each output keeps the name of its source file.
A `.md` file, with a suffix in any case, gives two outputs: the source bytes and a rendered page `<stem>.html`.
The publisher copies all other files, including HTML and images, byte for byte.
Files in one snapshot can link to each other by name.

The publisher refuses the whole request, and writes nothing, for:

- a path that is not a regular file, for example a missing file or a folder;
- a `.md` file that is not valid UTF-8;
- a reserved output name, in any case: `.index.json`, `.index.lock`, the `.review-link-` prefix, or an atomic temporary name;
- two outputs whose names are equal after `str.casefold`, a generated `<stem>.html` included. The error names both source files;
- no valid Tailscale IPv4 address.

`<root>/.index.json` maps a file set to its ID. The key is the JSON text of the sorted list of `(resolved source path, published file name)` pairs, so the argument order does not change the ID. The publisher first reads the inputs, renders the Markdown and gets the host. Then it takes an exclusive `fcntl.flock` on `<root>/.index.lock`. Under that one lock it does these steps:

1. Read the index. Drop each entry whose ID is not six lowercase letters or digits, or whose folder is missing or is a symlink.
2. Reuse the folder of the set. Pick a new ID when the set is new or `--new` is set.
3. Write each output through `atomic_file` to a temporary file in the same folder. Publish it with `os.replace`.
4. Delete the files and symlinks in the folder that are no longer outputs. Subfolders stay.
5. Set the modification time of the folder to now.
6. Write the index through `atomic_file` to a temporary file in the same folder. Publish it with `os.replace`.

The lock file stays on disk. Closing it releases the lock, also on an error. If an `OSError` occurs in steps 3 to 6 and the folder is new, the publisher deletes that folder and the index does not change. In a reused folder, the files already replaced keep their new bytes.

### Markdown page

- The page uses the source file name, for example `notes.md`, as its `<title>`.
- A viewport meta tag and a centered 48rem text column make it fit a phone. Long words and URLs outside tables wrap with `overflow-wrap: anywhere`.
- It follows the system light or dark mode with `color-scheme: light dark` and `light-dark()` colors.
- It loads `marked` 12.0.2 from `https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js`.
- It holds the HTML-escaped source in `<pre id="markdown-source">`. The `<pre>` shows until `marked` renders the page. It stays if the script does not load or throws an error.
- Code blocks and the `<pre>` fallback scroll sideways without wrapping. Tables are centered and use as much of the window width as their content needs; wider tables scroll inside the page. Cells wrap at spaces, never inside a word or number. Images fit the width.
- A custom `marked` renderer escapes raw HTML. A `<details>` or `<script>` block shows as text and does not run.

### Hana console

Each control has one name across the console.
Use one shared function for a control that appears on multiple pages.
A console phase reads this section before it starts.

Review and The List open a record below its summary row.
A refresh updates the open row at once.
The open row keeps its place on the screen, the draft, focus, and text selection.
The New item dialog uses the console dialog frame.
Select **Close**, press Esc, or select the backdrop to close the dialog.

Staff names and explicit role labels use the current role colors from the Accounts reader.
The reader gets the colors from `statusline.conf` through `accounts.role_colors()` and `accounts.role_color()`.
`staff_names.StaffNames` shares one staff snapshot and one color reading across each server render.
It resolves fixed IDs, session IDs, and unambiguous staff names.
The shared renderer escapes unknown names and free text without a role color.
`StaffNames.role_label(role, text)` escapes the label and applies the configured color.
Unit, unit director, merge director, and style director use the same unit color.
Without a valid color, the label uses plain text.
Button text, accessible names, filter keys, sort keys, and exports stay plain.
The owner corner shows the active unit, showrunner, or producer set in `page.<page>.owner`.
It uses the owner's staff color.
If the configured owner is unavailable, the corner shows the default recipient.
An item for a unit goes to its showrunner.
Showrunner and producer owners receive their items directly.
The page owner button names the default recipient and opens one shared dialog.
The dialog offers Bug, Question, Feedback, Proposal, Investigation, Enhancement, and Scheduled.
The **To** row offers chips for all eligible producers and showrunners in the staff record.
For a new draft, **To** selects the page owner or the showrunner who receives items for that unit.
On an unavailable AWS Machines page owned by the AWS unit, **To** selects its showrunner, builds.
On the **To** line, at its right side, a muted reason says why the server chose that recipient, such as `(why morty? because morty owns the Studio page!)` and a duck emoji.
The reason shows only while that chip is selected; a long reason wraps onto more lines, aligned right.
The To group's accessible description reads the reason without the emoji.
The active List producer appears in the row when `page.list.owner` names its staff ID.
The row can show that producer when its production document is unavailable.
Send checks the selected recipient again.
If the recipient's production document is missing, Send names its path and the parent showrunner, or you, who must fix the staff row.
If the page has no default recipient, the button opens the dialog with no recipient selected.
Select a recipient to enable Send.
Select any other chip to change the recipient.
Proposal limits the row to recipients with a native proposal folder.
The proposal writer checks the title and body before it creates the record.
`/issue` passes a private draft to `proposal_issue.py number --draft-file`.
Numbering checks the title and body before it takes the locks.
It verifies the draft under the locks and publishes the new proposal with its number.
Generated style decisions use their own filing path.
When the selected recipient becomes unavailable, the dialog clears the selection and disables Send until you choose a recipient.
Changing the type or reopening a kept draft preserves this empty selection.
Feedback uses the native Investigation record.
Bug, Enhancement, and Investigation use the same List workflow.
Review files a Change on the List when you send feedback or return the change to a team.
**Ask showrunner to check** files a Validation on the List for the showrunner.
These paths save the List number, owner, and producer in one row write.
The List keeps the report type and shows the original words and picture.
The state shows when the item entered New, Working, Repaired, or Delivered.
The producer uses `the_list.py update --state Repaired --next-step "How to try the repair"` after testing.
Needs me shows the repair instructions and the Works and Not fixed buttons.
Works records Delivered after the Undo timer expires.
Not fixed sends the user's words to the owner and producer through the same Undo control.
Both verdicts clear the repair instructions from Next step.
A saved Not fixed verdict returns the item to Working.
The browser stores the signed Not fixed request and its hold ID before sending.
The card keeps Not fixed available after an unknown result, including after a reload.
A confirmed Undo of the original hold or an exact refusal ends the request before acceptance.
A refusal keeps the draft so the user can correct it and start a new request.
A retry sends the original words and keeps any newer edits in the draft.
The List receipt identifies the exact request and distinguishes acceptance from confirmed delivery.
Confirmed delivery clears the draft if it still contains the unchanged submitted words.
When Tell or Ask clears the draft, the browser also clears its stored copy.
Server recovery retries pending notices with the same delivery key.
Review and Changes use Does not work for a change verdict.
Server recovery finishes an interrupted report filing before it sends the first notice.
The dialog keeps a report's draft ID across a reload.
Run `the_list.py move-review-bugs` from the Morty session to move existing open Review bugs.
A migration marks the native record before it writes the List item.
The command keeps the original words and attachments and records the List number on each completed Review record.
Run the command again to finish an interrupted migration.
Run `the_list.py move-review-waits` from the Morty session to move Waiting and Validating Review records.
The command links the source record or uses it as evidence on the List.
The source retains its words and records `moved to List item <n>`.
The command checks the source state under its lock before it files the report.
Rows that become Open before filing stay in Review.
Replies to moved records appear on the List.
The moved records stay out of Review counts and `/next`.
Run the command again to finish a move that stopped before the source log was saved.

Select **Scheduled** to create a List item directly.
Select the owner in **To**.
Enter the instruction, a `Start` time, and an optional `Repeat` in the words box.
The dialog shows an example for the start and repeat.
The dialog hides page details and attachment controls for Scheduled.
The saved confirmation shows the List number, start, repeat, and any assignment notice warning.
If the dialog cannot confirm the save, **Retry** sends the original submitted fields.
A retry of a filed Scheduled draft returns the same List number.

The List colors Owner, Producer, supporting unit names, staff names in log sources, and showrunner names in the count strip.
The Producer filter, table headers, and narrow card label use the producer color.
The log colors Producer and Unit labels. The Go box colors the role phrase.
The status colors Producer in Producer answering. The waiting note and Go box color staff names.
List delivery notices and feedback log entries use role colors for the notice recipient.
Delivery notices also color the showrunner who received an alert.
They keep the notice words and time.
Review colors Owner, Source, staff log names, and owner menu options.
The Review action heading colors the word showrunner in Sends to showrunner.
Review colors the staff names in Sends to, Question for, and Answer from headings.
Proposal and efficiency cards show Assign next to Approve.
The default shows the card owner's name and sends the approval to that owner.
Assign lists current showrunners and unit directors by name in their role colors.
Approve sends only to the selected assignee when you select a name.
Images colors card owners and their roles. The group links use the colored names.
Legacy Changes and Questions color the Showrunner filter label. Changes also colors the feedback prefix.
Legacy question pages color each source and its parent separately.
Branch pages color the Unit prefix and unit names. Rebuilds colors recorded requesters.
Each Builds data reading shares the name spans and role colors with the page.
Its tables, charts, target owners, queue priorities, and marked staff fields in tooltips use those spans.
The tooltip renderer escapes other fields as plain text.
The swap reader marks titles that name staff and keeps those markers in smaller-group members.
The swap chart and tooltips color only marked staff titles.
Program titles stay plain.
Swap details carry separate staff references and context text.
The swap display applies the existing public text filter and keeps the staff markers.
Grouped process labels and Disks lease labels also keep those parts separate.
Gate descriptions and Mac hold reasons keep their staff fields separate from the recorded prose.
The reader marks each staff field before it joins the display text.
The renderer colors marked staff parts and escapes paths, programs, and other context text.
Old readings without marked parts stay plain.
It renders each priority name before it joins the names.
Target availability and Builds tooltips color explicit staff role labels.
When a target has no unit or showrunner, the page shows the recorded holder with a plain Holder label.
The AWS costs table colors the Unit header and recorded staff names in the Unit column.
The Unit group control uses the role color before selection.
An open AWS day applies role and staff-name color changes on the next Builds data reading.
The refresh keeps focus on the same control in the open day.
The Builds page uses the saved staff ID to color the name in the bump mark and tooltip.
Disks uses the same spans for slot holders, sessions, and shared tooltip fields.
The slot tooltip colors the Showrunner label.
Current Builds rows carry their name parts through `slotStep` to the Disks slot label.
Its Builds refresh updates the name spans and role colors without a new timer.
The console navigation shows no staff name fields.
The Units board emits Markdown through `unit_phases.py`; this checkout has no Units console route.

Saved role colors apply after the next page render or Builds data reading.
Color changes need no console restart.
The console and Builds processes need their normal code reload when this implementation is installed.
The renderer preserves each configured color in both themes.
It does not choose a different color when contrast is low.

`change_page.py` without rows prints one fixed link, `http://<host>:<server_port>/console/`.
`/console` without the final slash forwards to `/console/` with the same query.
The server renders `/console/` from the current code and configuration at each GET.
A merge to main thus changes the console with no new link and no restart step.
A render error gives a 503 page: **The console is not available.**
Selected-row links with `--production NAME C27` still open their own signed `changes.html` directly.
The console sends `Cache-Control: no-store`, a same-origin content policy, and permits same-origin framing.
Its title is **Hana console**, followed by the selected page title.

The navigation shows Accounts, AWS, AWS Machines, Builds, Disks, Images, Studio, Review and Placement disk POC.
The desktop has no top bar: each page shows its title once, as its own heading.
Every page uses `page_frame.STYLE`, except the Placement disk POC page.
That page is a standalone document with its own layout and styles, and it has no status line.
Each page centers its main column in the console frame, with a maximum width of 1124px.
A page whose `main` has the `page-wide` class uses the full pane width instead; the Builds page is the only one.
The title starts 32px from the top and left edges of that column.
Each navigation page places `page_frame.title()` directly above its content.
The title, status line and content use the same centered column on all console pages.
Cards and form controls keep their page styles.
Review and The List label the state filter **Show**.
They share the chip markup and the chip control script in `record_table.py`.
The List uses the shared facet count code in `record_table.py`.
Review counts its chips in one pass over the table rows, because a Review chip click can also reset Show or Owner.
They share the parking menu, date picker, and browser script.
The timed park action saves to the item's store.
Every page uses the shared background and text colors.
The status line has the same background as the page.
The frame reserves scrollbar space, so short and long pages keep the same column position.
A successful action updates the page without a confirmation line by default.
List **Unpark** and Review **Done** show confirmation lines.
An action that fails uses the shared action failure notice.
Branches shows the remaining branches after deletion.
The page uses the shared notice for deletion or delivery failures.
The original address follows the latest saved list, including when report delivery fails.
If the manifest update and next-list publication fail, the POST response still excludes the branches it deleted.
Images uses the page notice if the image is deleted and the list cannot refresh.
The shared frame freezes the title, the action area, and the status line at the top of the page.
Review also freezes Search.
Search sits to the right of the status above 800 px and below the status at smaller widths.
The frozen top has an opaque page background.
The content scrolls under it.
At widths of 640 px or less, only the title stays in view.
The action area and status line scroll with the content at these widths.
Review Search also scrolls with the content at these widths.
The action area holds the owner name and the New item button.
The script puts the actions beside the title when they fit on one line.
When they do not fit, the actions stay above the title.
The frame measures the frozen top when its size changes.
It uses this height as the page scroll padding for anchor jumps and focused rows.
A page can add action buttons between the owner name and New item.
`page_frame.page_action_button(action, icon, label, dialog_id)` makes one button, and pages pass it through `actions`.
`owner_control()` and `with_owner()` take `actions`. The names `new-item` and `owner` are reserved.
A press on a page action button opens its dialog with `showModal()` and binds it with `bindConsoleDialog`.
`bindConsoleDialog` binds each dialog one time. A second call for the same dialog does nothing.
When the dialog closes, focus goes back to the button that opened it, and the page does not scroll.
A press on the copy of the button inside its own open dialog moves the focus to the first control of the dialog.
The AWS region filter sticks directly below the frozen top.
The page gives targets below the filter a scroll margin equal to the filter height.
The Images group index sticks 16 px below the frozen top.

The stacking order is as follows, from lowest to highest.

1. Page content and its sticky bars (z-index 3 or lower).
2. The frozen top (z-index 4).
3. Menus, tooltips, and the Builds paused badge (z-index 5 to 10).
4. The Undo stack.
5. Modal dialogs and the image zoom in the browser top layer.

The window rule in `page_frame.py` keeps New item available while a page window is open.
A page window is a `<dialog>` that a page opens with `showModal()`.
These are the windows:
1. The picture zoom window in `page_frame.py`.
2. The New item window in `new_item_dialog.py`.
3. The List review box in `list_page.py`.
4. The Accounts color picker in `accounts_page.py`.
A modal dialog makes the rest of the page inert, and a popover shown after it is also inert.
Thus the frame adds a copy of the action area to each window when the window starts to open (`beforetoggle`).
The copy is the last child of the window.
It follows the position of the action area and stays in view when that area scrolls off the phone screen.
The window removes the copy when it closes.
The copy has the same labels as the area. It has no `id` attributes.
The controls in the copy have no plate and no edge, so each part of the copy that shows is a control.
The copy itself takes no clicks: a click beside its controls goes to the window, as a click on the backdrop does.
A window meets the action area when a part of the window and the controls of the area overlap from left to right.
While a window meets the copy, the frame sets `--page-window-top` to the bottom of the copy.
The frame measures each open window at these times:
- when the window opens, and when another window closes,
- when the view or the action area changes size,
- when a picture loads.
It measures first with the current value. A window that meets with the value keeps it, so the measure keeps the scroll of the window.
Only when the window does not meet with the value, the frame removes it and measures again.
This second measure is necessary for the zoom image, because its width follows its height limit.
A scroll does not change the left and right edges. On a scroll, the frame moves the copy and sets the value again only when it changes.
A centered window stays centered on both axes.
Its maximum height is `100dvh - 2 * max(16px, --page-window-top)`, so a tall window that meets the area clears the copy.
New item, the other console dialogs and the picture zoom image use this limit.
A window that does not meet the area has no value, so it keeps the place and the height limit of main.
The Accounts color picker keeps its place next to its swatch, and the copy covers none of its controls.
The List review box is placed at the top of the view, so it starts below the action area.
The keyboard reaches the copy in the window: press Tab after the last control of the window.
Escape, the backdrop and the focus return of each window do not change.
New item opened over another window opens above it.
When New item closes, the window under it stays open, and focus goes back to the New item button in that window.
A press on the New item button in the copy inside the New item window moves focus to the words box.
In the Saved state the words box is hidden, so the press moves focus to **Done**.
The shared `.page-status` line follows the title.
It shows the state, data time, round trip and server render time, in that order.
Live uses `radio` and `--room`; Reading uses `loader-circle` and `--accent`.
Late uses `clock-alert` and `--warning`; Failed uses `triangle-alert` and `--error`.
Paused uses `circle-pause` and `--ink-3`.
A hidden page or a replacement hold shows Paused before the other states.
Otherwise, the state order is Failed, Late, a busy Paused state, Reading, Live.
Late, Failed and Paused show a reason after the last data time instead of timings.
Builds and Disks keep the shown data time when a later read fails.
A first read shows only Reading until data arrives.
A slow read retains the last data time and timings.
The age increases each second without a request.
After one minute, the line shows the age in whole minutes, without a clock time or zone.
Round trip covers request start through the first frame after the data appears.
Render time comes from the server or the data writer.
The browser does not estimate render time.
`config/console.conf` sets timing limits, the Reading delay and each page's age limit.
`status.review.age_limit_seconds` supplies the age limit for Review and the legacy renderers.
Values above the timing limits use `--warning`.
Late and Failed remain at the top during scrolling.
The line wraps between its positions on a phone.
The status speaks only when its state changes.
Reduced motion stops the loader icon.
`page_frame.createPageRefresh` owns the refresh timers, visibility handling, failures and status drawing.
Each loader checks the request identity before it changes content.
An older response cannot replace a newer response.
A page passes `page_version.script_version(*VERSION_PARTS)` to `title`, which puts it on the status line as `data-script-version`.
`VERSION_PARTS` holds the page's static script, style and markup.
The hash also covers `page_version.SHARED_PARTS`, the parts that each page shares: the frame style and refresh script, the owner heading assets (`PAGE_SCRIPT_ERRORS`, `HEADING_STYLE`, `OWNER_STYLE`), `CONSOLE_DIALOG_ASSETS` and the New item dialog script and style.
A page in `change_page.document` adds `change_page.DOCUMENT_ASSETS`, the style and script around each page.
`reloadForNewScript(page, held)` is the first step of each refresh after the loader's stale-request check.
It runs before any page early return and before the page swaps content.
When the fetched page has another version, it reloads the page.
Accounts, Images, The List, Review, the Changes and Questions pages, and Investigations call it.
An open page then never runs an old script on a fragment from a changed page script. No page has a second reload rule.
Review reads its snapshot version as JSON, so that answer names the script version as `script`, and Review passes the string.
A Review refresh reads only the index and the card bodies. Its version also hashes `SHELL_PARTS`, the fixed shell markup: the search field, the Type and Show chips, the notices, the table head and the open-row template (`record_table.render_inline_detail()`).
Only a visible edit or a save in flight holds the reload. The shared `pageHoldsInput` finds a visible edit:
- a shown text field with unsaved text,
- an open dialog, menu or date picker,
- a shown text selection that is not empty.
A text field marked `data-reload-safe` holds the reload only while it has focus, because the address restores its words after a short wait.
The List and Review search fields carry it.
Words in closed items or hidden cards do not hold the reload.
The reload discards words without a saved draft. Images restores saved drafts from hidden cards.
Each page passes its save in flight as `held`:
- Accounts: its `hold()`, which covers the color popup, the move menu, the quota panel and their pending saves,
- Images: `sending`,
- The List: `saving`,
- Review: `pendingSaves`,
- the Changes and Questions pages: `activeSaves`,
- Investigations: `sending`.
The New item dialog stays open while it sends, so the open dialog holds the reload.
Each save POST uses `page_request_timeout_seconds` from `config/review_links.conf`.
Pages write the duration in `data-request-timeout-seconds`.
The Review card frame and the New item dialog read the duration from their own settings.
A held save gives the result its own timeout and allows time for the server's Undo grace period.
The New item screenshot request has no time limit.
When a save fetch ends by its time limit, the result of the save is not known.
The shared `saveTimeoutWords` then gives **No answer in N s. The save can still finish.**, with N from `page_request_timeout_seconds`.
Accounts, Images, Investigations, the Changes and Questions pages and Review show these words in place of **Not saved**, **Not sent** or the Accounts failure words.
An Accounts color save that ends by its time limit keeps the new color. The next refresh shows the true color.
A held reload can follow a timed-out save at once. Then the words do not show, and the loaded page shows the true state.
While the reload waits, the status shows Paused: **A newer page loads when your edit ends.** It never shows Failed for a hold.
When `reloadForNewScript` holds a reload, it records the hold. When the hold ends, the page reloads at once.
The shared `reloadWhenHoldEnds` checks the hold on the 1 s status clock and on each `input`, `change`, `close` and `selectionchange` event.
The first held reload adds these event listeners, so the shared script has no effect on the page when it loads.
It listens in the bubble phase, so the page's own handlers run first. A `close` event does not bubble, so its check runs one task later.
Each save end calls the page refresh's `changed`, so the check also runs then.
A selection holds the reload only while it shows. A selection in a hidden row or a closed dialog has no client rects and does not hold it.
The check does not wait for a timed refresh, so a page with refresh period 0 also reloads.
There is no time limit on a hold.
A held reload answers with only the busy reason. A page hold does not keep this answer back.
The status keeps the data it shows and counts the answer as current, so a long edit stays Paused and never shows Late.
Before the first data, a held answer shows Paused with its reason.
The browser keeps the scroll place across the reload. The address keeps the open row, the shown card and the filters.
Accounts and List read their rendered page at `page_refresh_seconds` intervals.
Review checks the snapshot version at that interval.
It requests the compact index only when the version changes.
On machines that read staff over SSH, GET, HEAD, News polls, and background Review builds share a staff snapshot.
The server reuses this snapshot for `staff_snapshot_seconds`.
After a cache read fails, readers use direct staff reads for the same interval.
The staff owner reads its local file directly.
POST routes that can save answers, change staff, or send messages read staff directly.
These routes clear the snapshot when the action ends.
Accounts preferences, News polls, and Disks polls keep the snapshot.
Builds revalidates `memory.json` at its feed interval.
The server uses only `If-None-Match` to decide whether to return HTTP 304 for a static file.
The server sends HTML responses, Review versions and Review indexes with gzip when the client accepts gzip.
Static HTML, CSS, JavaScript, JSON, SVG and TXT files also support gzip.
An explicit gzip quality takes precedence over the wildcard quality.
Responses that support gzip include `Vary: Accept-Encoding`.
Static files have different ETags for gzip and identity.
HEAD returns the headers for the selected encoding with no body.
Disks shows the drive cards and the session list.
Disks reads `disks.json` at `page.poll_seconds` intervals and requests a new measure when needed.
Disks shows Paused with the reason **Measuring…** while a measure runs and reads succeed.
The loader result carries `busy:'Measuring…'` while the measure runs within `wait_seconds`.
The shared component records the data, age and answer time from each busy result.
Failed and Late override this busy reason.
The `hold` function delays page replacement; a busy result does not delay it.
The old sizes remain visible during the measure.
An unsuccessful read shows Failed with the read error while the measure runs.
Old sizes alone do not make Disks Late because the page measures on demand.
Disks supplies `answeredAt` to the shared status component after a server answer.
The shared component uses that time for Late and keeps the size timestamp for the visible age.
Other pages use their data timestamp for Late.
A completed measure returns Disks to Live with the new size age.
A measure that exceeds `wait_seconds` clears the measure state even when reads fail.
The action failure notice shows **The measure did not finish.**
A refused measure shows its reason in the same notice.
A successful measure request clears the notice.
A failed data read uses the refresh status line.
If a read remains pending past the age limit, the status shows Late with the retained data age.
AWS reads its published page when shown; its hourly publisher sets the data time and render time.
Images reads its rendered page when shown.
Review retains typed text across refresh and holds replacement during an active save.

The title uses bold 26px text.
At 560px or less, the edge space is 24px at the top and 16px at the sides.
Accounts, AWS, Builds, Disks, Images, Studio and Review put the title and status line in the same place.
Its toggle collapses the labels into an icon rail.
Hover and keyboard focus show each collapsed label.
The browser stores the collapse state in `localStorage`.
Each storage operation catches failures, so blocked storage does not stop navigation.
The selected page fills a same-origin frame beside the navigation.
Each page that the user opens keeps its own live frame, and the console hides the others.
A navigation click back to a page shows it as the user left it: filter, card, type, scroll and typed text.
A hidden frame keeps its layout with `visibility:hidden`, so its scroll position stays.
The shell follows the system light or dark theme.
At phone width, a slim top bar shows **Hana console** and a menu button.
The frame uses the full page width below the top bar.
The menu button opens the navigation above the frame.
A page choice, the Escape key, or a click outside closes the navigation.
The phone navigation starts closed and does not change the stored desktop state.

`?page=<id>` selects a page.
Missing and unknown ids select the first configured entry.
Navigation clicks update this query with `history.replaceState`.
A plain click on a console link from a page frame selects the destination in the shell.
The console applies the link's recorded place and keeps the source frame open.

The console URL also records the place of each opened page as `at.<id>=<path, query and fragment>`.
A reload or a shared link opens each page at its recorded place.
The console takes a recorded place only when its path is the configured path of that page; otherwise it opens the configured URL.
A place equal to the configured URL removes its `at.<id>` key.
The console reads a frame's place when the frame loads and at each `hashchange` in the frame.
A frame load that fails, such as a refused connection, shows the browser's error page.
The console then loads the frame's last place again after `frame_retry_seconds` in `config/review_links.conf`, which starts at 2 seconds.
A page that changes its place with `history.replaceState` posts `console-place` to its parent, so the console reads the place again.

A page in a hidden frame can pause its refresh: `window.frameElement?.hidden === true` while the frame is hidden.
When the console shows a frame that was open before, it posts `console-shown` to that frame, and the page can load an update at once.
The Review page follows both rules.
The shared refresh component stops data requests in hidden frames and hidden browser tabs.

Older links forward to the fixed address with a 303 and keep their query.
The server forwards `/<snapshot>/console.html` to `/console/`.
It forwards an all-list `/<snapshot>/changes.html` to `/console/changes.html`.
It forwards `/<snapshot>/rebuilds.html` to `/console/rebuilds.html`.
A snapshot whose folder holds a selected-row manifest keeps its own `changes.html`.
A forwarded link can come from a folder that the sweep already deleted.
Older browser folders still accept answers that a page posts to them.

`config/console.conf` defines entries in file order.
The server reads it through `conf.read("console")` at each console request.
Each entry defines `page.<id>.label`, `.icon`, `.title`, and `.url`, and can define `.items`.
AWS Machines follows AWS in the navigation.
The configuration has no Changes, Investigations or Questions entries or age limits.
The console renders every configured entry without a skip rule for Review.
The URL value has the form `python:<module>:<function>`.
The function takes no arguments and returns a URL path under the review root.
The console also accepts a full URL from the configured review origin and converts it to a path.
The URL function raises one of `review_link.SNAPSHOT_ERRORS` when the page is unavailable.
An absent function also makes the entry unavailable.
That entry remains visible and shows **Not available** when selected.
The unavailable view shows the configured owner and uses the shared New item dialog when a recipient is available.
Each console request resolves the current configuration and page availability.

A page joins with one configuration block and its URL function.
The page serves itself at that path and permits same-origin framing.
A link that leaves the page uses `target="_top"`.
The server must not send `X-Frame-Options: DENY` or `frame-ancestors 'none'` for these pages.
`change_page.page_url` returns the fixed `/console/changes.html` path.
`change_page.questions_url` returns the same page with `?type=questions`.
The Builds entry calls `memory_feed.page_url`.
That function returns the configured Builds page URL without publishing a reading.
The AWS entry calls `aws_page.page_url`.
That function returns the AWS page URL without publishing the page.
The AWS Machines entry calls `machines_page.page_url`.
The function returns the URL for `machines.html` in `aws_page.output_folder()`.
The entry shows **Not available** until the publisher writes that file.
`serve.published_console_page` identifies the published path as `aws_machines`.
The server uses the configured AWS Machines owner in the published page and the unavailable view.
The New item button names the recipient resolved from that owner's staff record.
The AWS Machines entry uses the Lucide `server` icon.

A page includes `__PAGE_SETTING_CONTROLS__` once in its head.
`page_frame.template` replaces that slot with the style and script from `setting_controls.py`.
The page renders these controls with the following functions.

1. `slider` takes the label, bounds, step, chosen value, live value, decimal places and an optional live note.
   `pageSettingControls.connectSlider(root)` connects the native range input to the display.
   Read `value` for the choice or `live` for the saved value.
   Use `setValue(value)`, `setLive(value, note)` and `format(value)` to update or display a value.
   Use `input` to listen for the native range input events.
2. `amount_box` takes the text before and after the USD field, its minimum, step and value.
   Read or write the field through `valueAsNumber`.
   Listen for its native `change` event to apply the amount to the slider.
3. `go_button` takes the button label and the page and key for the setting.
   Call `pageSettingControls.connectGo(root, slider, amount, describe)` with the slider and the amount input.
   Return `newValue`, `monthlyChange` in USD, and an optional `liveNote` from `describe`.
   Go uses the slider live value as the old value in each confirmation.
   The Go button shows the confirmation line before Confirm can send the choice.
   Cancel closes the confirmation. Confirm disables the controls while the server saves.
   Go updates the Now mark from the live value in the reply.
   A successful save with a live value emits `setting-saved` with `detail.value`.
   A refusal or failure shows one line below Go and keeps the proposed value.
   Use `setLabel(label)` to update the button and `cancelConfirmation()` to close an obsolete confirmation.

Go reads the request token from `GET /console/tune`.
`POST /console/tune` accepts the fields `page`, `key`, `value`, and `token`.
The POST requires a same-origin Origin header and the current request token.
`console_tune.SETTINGS` registers allowed page and key pairs with the target document, range and step.
The server validates exact decimal steps from the minimum.
It refuses pairs absent from the table.
The AWS Machines page uses `aws_machines` in this table and in the POST.
The frame leaves the table empty for a page to register its settings.
The route calls `tune.py` with the page and Go button in the reason and user words.
It returns Saved with HTTP 200, Refused with HTTP 400, or SaveFailed with HTTP 503.
Each result includes `live_value` from a fresh configuration read.
An unavailable live value is `null`.

The bottom-left `For you` button shows the global action count.
The collapsed rail keeps an icon and badge.
The phone header keeps a compact button.
The nonmodal panel shows two action entries with machine names, copy buttons, and card links.
The panel footer opens Review with `type=for-you` and `show=open`.
Each card link adds its stable action key as `item` and expands that card.
Notifier links preserve the console query entries for other pages.
Escape closes the panel and focuses the visible For you button.

`/console/for-you.json` reads the shared action reader and uses `Cache-Control: no-store`.
The shell reads at each `page_refresh_seconds` interval while the tab is visible.
It also reads on visibility, focus, panel opening, and a framed `for-you-completed` message.
A completion message during a read schedules a second read.
A hidden tab stops the timer.
A refresh preserves focused commands and text selections.
A transport failure keeps the last complete count and marks it stale.
The phone badge shows a dotted outline for a stale count in both navigation states.
An explicit unavailable response shows `?`, including with HTTP 503.
A known zero uses a quiet button and shows `No actions required.` in the panel.

`rebuilds.url` in `config/console.conf` still names `change_page.rebuilds_url`.
That function returns `/console/rebuilds.html`.
`?page=rebuilds` still selects that page for commit details.
The shell refreshes its grouped details while that page is visible.
The status line shows session counts under `Review:`.
The question count includes questions from staff that wait for Nate's answer.
The same count includes questions from Nate that wait for the session's answer.
Review saves the delivery recipient with each pending follow-up.
The footer uses that recipient for the count.
For older follow-ups without a saved recipient, the footer uses the question's relay.
Older questions from New item use the record owner.
Ask question in Review sends to the showrunner.
Followup on the legacy Questions form uses the question's relay.
When staff answer a question from Nate, Review shows the answer under Responses and the footer count drops.
The List's other-page counts include only questions that wait for Nate.
The status line names waiting rebuilds and counts other open actions under `For you:`.
`assignment.py` reads both rows before it replaces itself with jq.
The Mac requests `/console/for-you.json?view=count` for the rebuild machines and counts without command text.
The `for_you.host` setting in `console.conf` names the Tailscale peer that serves this data.
`tailscale ip -4` returns the peer address.

A navigation entry shows the Lucide `sparkle` icon when its page holds an item added after the user last opened that page.
An optional `page.<id>.items` key names the page's items function, in the form `python:<module>:<function>`.
The function takes a `console.NewsRequest` and returns the keys of the items that wait on the page.
Review uses `review_page.waiting_items` for Open records.
Images uses `images.waiting_images` for registered images without `feedback_at`.
The Images page and its items function share the same waiting-record filter.
Temporary change images stay outside the Images page and its new mark.
A page without an items function, or an unavailable page, never shows the icon.
The console posts `open=<id>` for the page it shows to `/console/news`.
While the Rebuilds page is open, it posts an empty `open=`, which records no page as seen.
It posts when it loads, at each page choice, and at each `page_refresh_seconds` interval while the browser tab is visible.
The server records the open page's item keys in the private `console/.review-link-seen.json`.
It answers `{"new": [<id>, ...]}` with each other page that holds a key absent from its record.
The server keeps the record, so the phone and the desktop show the same marks.
A page with no record takes its present keys, so the first visit marks nothing.
Opening a page clears its icon at once.
An item that leaves a page and comes back after the user opens the page shows as new again.
An items function that fails writes one line to the server log, and its page shows no icon.
In the open sidebar the icon's center lines up with the status dots of unavailable pages.
In the collapsed rail the icon sits at the top right of the page icon.
At phone width the menu button shows the icon while any page holds new items.
Each poll reads the change lists once, about 0.1 seconds of server time.
The Changes and Questions items functions share that read through `NewsRequest.shared`.

The console vendors SVGs from `lucide-static` **0.468.0** under `scripts/review_links/lucide/`.
`torus.svg` comes from `lucide-static` **1.77.0**, because 0.468.0 has no torus icon.
The console navigation can use only the names in `console.ICON_NAMES`.
Other pages use more icons from the same folder.
That folder includes the versions, the sources, and the ISC license.
The console shell inlines the icons and makes no CDN, font, script, or stylesheet request.

### Builds memory tooltip

Hover over a running local Hana nextest step with a program memory trace to see Compile and Test charts.
The charts show program memory without file cache.
Blue shows this run, and purple shows the latest whole run with the same build shape.
Each chart has its own elapsed time axis and a dashed line for the phase reserve when known.
The Compile chart scales this run to the past run's time axis and labels the scale.
It shades the time when the past run compiled the final `hana` crate.
The values show the latest sample, the past peak, and the past time above half its reserve.
The Test chart shows **Not started** until tests start.
The tooltip shows **No past run yet** when no whole run matches within the chart age limit.
The `feed.past_run_max_age_days` setting in `config/memory_trace.conf` applies only to the chart.
The trace worker keeps old trace files.
On recovery, the worker saves an incomplete copy and keeps the original live trace.
The open tooltip updates with each `memory.json` reading.
Disks uses the shared step tooltip fields.

### Review page

Select a Review row to show its detail and actions below the summary.
A refresh updates the table and the open row at once; there is no **Show updates** step.
Review and The List use the shared `record_table` function `preserveRecordRowTop` around each refresh, detail load, and save result.
The function measures the open row before and after the update and scrolls by the difference.
It turns off scroll anchoring while it sets its top space, and its scroll target keeps the current fraction of a pixel.
Firefox scrolls by whole pixels from a fractional position, so the row stays within 0.1 px there too.
The open row keeps edits made during a save, including the field selection.
A save response from before the row closed cannot replace the reopened detail.
The draft stays editable during the refresh and stays in the new detail.
The next open shows the latest detail.
A question or feedback on a change or a proposal keeps its row open, and the row shows its own saved result immediately.
A Tell that completes a proposal keeps the completed result in its open row.
Every other save fades the row out during the save and closes it.
If the response has no detail, the page requests the detail immediately.
A confirmed action that closes the item closes the row, even if the detail cannot refresh.
The response reports whether message delivery is pending.
Each refresh in the delivery window asks for the delivery state, through the first refresh that reports delivery ended.
The setting page_delivery_window_seconds limits the delivery window.
Review publishes the current question delivery notice before it reports that delivery ended.
Linked and standalone Review cards reserve a 52 pixel status row above the body.
The row shows the save failure and **Show updates** without moving the shown detail.
A long failure reason ends in an ellipsis, and its title contains the full reason.
The old form cannot submit while the update waits.
The card keeps edits when the user selects **Show updates**.
The open row shows a refresh error below the detail and keeps the shown detail and draft.
If a refresh removes the open item, its row leaves the table.
An item link in the address to an item that is not on the Review page closes the open row and shows the missing item notice.
A `data-record-jump` link inside a card to a missing item keeps the row open and shows the link notice in the row.
A linked item that a save handed off stays hidden until a snapshot shows it in its new state.
The exception is a row that its own save kept open with the result.
That row stays open and listed until the user closes it or opens another item.
A refresh, a chip that keeps the row listed, Back or Forward to the same open item, or a link to that item never ends it.
After that, a new link to the item shows no row until the snapshot arrives.
Closing the row returns focus to its toggle when focus was in the open row.
The open row's log scrolls in its own area, like the List log, with the same `list_log_max_height_rem` limit.
A refresh, a detail load, or a save result keeps the log's first visible entry at the same place.
The question and answer show below the log, in the log column.
The log then keeps at least 8 rem of height.

The For you Type chip follows All.
It uses the same action reader as the console notifier.
It defaults to Open and keeps the Show, Owner, and Search controls.
All includes withdrawn actions and their reasons.
The card shows the command, machine, working directory, requester, reason, waiting work, and completion method.
Copy uses the shared code block. It leaves the action open.
Open actions show Done for both manual and automatic completion.
Review shows the same confirmation for both methods.
Done uses the signed Review form to complete the action through the action store.
The saved action leaves Open and appears in Done and All at once.
The same signed Done request can return the saved result again.
The console refreshes its count after the save.
The notifier opens the selected action in a live Review document and keeps its drafts.
If the frame shows an error, the console reloads Review at the requested place.
Rebuild cards show automatic completion and link to the Rebuilds details.
Review shows source read failures in the For you view.
Review requests For you rows with `for-you=1` on index requests.
The server omits those rows from index responses to older tabs.

`/console/review.html` replaces the Changes, Questions and Investigations navigation pages.
The old addresses redirect to Review with the corresponding type filter.
The redirect retains the owner, filters, sort and open item.
Legacy `row` fields become `item` fields.
Old Investigations links with `show=not-read`, `show=answer ready`, or `show=read` select Open.
Old Investigations links with `show=open` select Open.
Old Investigations links with `show=all` select All.
Legacy investigation owner filters retain the exact record owner.
The Review navigation entry uses the Lucide `inbox` icon.
Selected-row snapshots retain their signed legacy forms.

`review_items.py` reads the existing question, change, investigation, proposal and efficiency stores.
It projects each record into one `ReviewItem`; it stores no second record.
Each item has an ID, type, owner, source unit, title, state, times, log and permitted actions.
Each item also exposes `waiting_why`, which is empty on older records without a reason.
Waiting filers must supply a reason.
The shared rule refuses empty text and text that contains only spaces.
Questions store the reason in their registration.
Investigations and proposals store it in their frontmatter.
Efficiencies store it in the review file beside the ledger.
The user's Ask question text supplies the reason.
Changes store the request reason with their delivery state.
Validation uses the user's feedback when the feedback contains text.
Validation uses the stated validation request when the feedback is empty or contains only spaces.
`waiting_why.py list` lists Waiting items without a reason, with their key, type, title and filer.
This command does not allocate question IDs or write records.
`waiting_why.py set --item <key> --text "<reason>"` uses the filer's writer and refuses items outside Waiting.
The writer checks the current state under its lock before it stores the reason.
Questions use the canonical owner lock, including questions without a current owner.
The command prints one plain error line for a refused update.
`investigation.py file` requires `--waiting-why` and files an Investigation on the List.
The caller owns the investigation.
The command saves the List row in the folder of the producer set in `page.list.owner`.
A producer who files an investigation also produces it.
For other callers, the command uses the producer map or the configured List producer if the map has no active producer.
The Python `followup` API stores the follow-up words as the reason.
`proposal_issue.record_review` and `efficiency_reviews.record` require `waiting_why` when setting the Waiting mark.
`candidates.py outcome E<n> deferred --waiting-why "<reason>"` stores the reason beside the ledger.
The states are **Open**, **Validating**, **Waiting** and **Done**.
Changes use Validating while the showrunner checks the change.
Records use Waiting while they await an answer or action.
List and Review use the queue rule in `question_path.py`.
An active Later value hides the item in Review and puts it in Parked on the List.
An unread answer puts the item in Open on Review and Needs me on the List.
The row shows **New answer** while the answer is unread.
Review uses the saved answer time or the latest Answer event time.
An answer is unread if its time is after the last Mark read or Later.
During validation, a Change answer must also follow the current handoff and question.
An answer without either time remains visible in the record.
Saved positions and exchange IDs identify copies in the display.
Review keeps the legacy history when it cannot identify a copy.
Review, the question count and `/next` interpret legacy local times in the user's zone.
Mark read removes the mark.
Mark read also works when the Question owner is unavailable.
An active Later value continues to apply after Mark read.
A decision that still requires an action stays in Open after Mark read if Later does not hide it.
The List and Review keep their existing Later storage formats.
Review keeps native Waiting and Validating states for records that still use the native reply path.
The count readers and `/next` use this shared reader.
Open items count in the status line.
An open investigation and a deferred efficiency item do not count.
Questions receive a stable, shared `Q<n>` ID in their registration records.
The native writer assigns the ID under the question owner's lock.
All Review reads use the file name for a legacy Question until a native write assigns its ID.
This includes the Open read and the Review snapshot.
When the allocator cannot read a named local document needed to find record folders, it refuses the allocation.
Proposal filing reports the reason before it saves a record or reserves its ID.
Review checks that the selected Question still matches the saved record before it replaces a pending follow-up.
Ordinary reminders serve the owner's session; Review excludes them from items and counts.
Questions without an owner remain visible under Owner unavailable.
These questions have the state Open and retain their previous count totals.
They have no answer control until an owner becomes available.

The Type row has All, For you, Questions, Responses, Changes, Investigations, Proposals and Efficiencies.
Legacy Bug records remain visible under All and in searches by ID.
Review opens old Bug filter links with All types selected.
Only a Question filed through the dialog appears as a Response after an answer.
The Response uses the Question's existing key, owner, exchanges and log.
The Question stays in its native store during the complete exchange.
Proposals show `<owner>/P<n>` and efficiencies show `<owner>/E<n>`, where the owner is the production's showrunner.
The table columns are Id, Type, Owner, Item, State and Age.
One row can open its card in place.
`record_table.py` supplies the table styles, filters, sorting and open-row controls for Review and The List.
When a Review action removes a card, the shared table keeps a copy of the row and its open details in place.
The copy fades and collapses while the rows below slide up.
`card_fade_out_ms` in `config/review_links.conf` sets the duration. Its initial value is 450 ms.
The Review page can restore a pending card after the user scrolls one viewport.
The page restores this card after all row animations end.
When Later hides the last card for an owner, Review keeps the Owner filter selected and shows a zero count.
A save can change the card's sort values. The copy keeps its previous values until the fade ends.
If the request fails, the row slides back into view and the current card stays selected.
`card_fade_in_ms` sets the duration of the return. Its initial value is 600 ms.
Review and The List read both settings when they render.
If the fade is still in progress, the return starts at the copy's current height and opacity.
The animation updates when the user opens or closes a card during the transition.
The status region above the table shows a failure only for the most recent submission.
It shows that failure when the row or its details are hidden.
It also confirms when Done completes a For you action with either completion method.
The next save attempt or reload clears this notice.
Filter and sort changes apply at once.
The reduced motion preference disables row animations.
The shared `record-id` class keeps each ID and its expand arrow on one line.
Each row becomes a card at phone width.
The Type, Show and Owner rows use native buttons with a count and a pressed state.
The Owner row shows the selected owner and owners with the selected type and words in any Show state.
It hides the other Owner chips.
Each selected chip counts the rows in the current view.
Each other chip counts the rows that a click on it would show.
Click another Owner to clear the source filter.
An explicit filter change can return the Owner filter to All when no items match the type and words.
An owner with matching rows in another Show stays selected with a zero count.
All stays first and counts every matching row.
An unknown owner returns to All and clears both owner and source-owner constraints.
Owner names resolve against all known owners before the type and Show filters apply.
The address uses the resulting selection.
Type and Show changes restore owners when their rows match again.
The Show row offers All, Open, and Done below the Type row.
Old Waiting and Validating links select Open.
Review expands legacy filter names in the query and fragment before it combines them.
The query wins for names in LIST_QUERY_FIELDS that both sources supply.
The fragment wins for other names, including sort and dir.
The Owner row follows the Show row.
Exactly one Show chip stays selected. Pressing that chip changes nothing.
Type and Show counts include an Owner reset if the click would return Owner to All.
Type counts use Open when a click selects For you.
The table and the three chip rows use the same row data and filter rules.
The browser counts all matching rows, including rows outside the current scroll window.
One typed `ReviewIndexEntry` record defines the row data for counts, filters and column sorting.
The shell carries the active index, including states outside the selected view.
Each changed version supplies a complete replacement index.
The index contains the key, ID, type, owner, source owner, owner name, title, state, filed time and update time.
It also supplies the source name, urgency label and Later time for each summary.
A Validating Change supplies the time of its request.
The wire format shares identity names and omits an ID when it equals the key.
A Response uses the Question type in the index and adds a Response flag.
Tabs that use the earlier script ignore the flag and display the row as a Question.
The index contains summary values and shared staff labels.
The server sends card bodies on demand.
The server sends gzip when the browser accepts it with a positive quality.
Each body response carries its snapshot version.
The browser displays an available body as soon as it arrives, even if its version differs from the index.
The body supplies the row state before the browser enables its actions.
A version difference starts the guarded page refresh.
If the index changes during the request, the browser accepts only a body with the current index version.
The refresh keeps the open form when its body has the same version as the new index.
If the body request or index reports a missing item, the browser closes the card and shows the missing item notice.
The Show chips replace the right count text.
All includes active Review records and all For you actions.
Done shows completed For you actions.
**Ask showrunner to check** files a Validation on the List.
**Send feedback** and **Does not work** file a Change on the List.
Standalone change review links use the same filing path.
List filing completes before the verdict saves.
If List filing fails, the verdict stays available for retry.
**Ask question** sends the question through the native reply path.
The change leaves Open and its counts when the request starts.
A failed save restores the row and its counts.
An unmoved Validating change opens from its row in All.
The row and card show **With <validator> · Validating for <duration>**.
The validation record stores the validator’s staff ID.
The page resolves the name from that ID.
At deploy, the conversion adds the validator’s staff ID to each open validation that needs it.
The conversion uses the saved message destination or the current showrunner when the log has no destination.
The browser updates the duration from the saved handoff time.
When the record has no handoff time, the page shows **With <validator>**.
The card shows the item, steps to check it, proof and the log.
**Ask a question** sends **Your words** to the validator named above the button.
The card shows **Question for <validator>** and **Answer from <validator>** when the answer arrives.
The active validation log shows **Asked <validator> to validate**, **Asked a question about validation**, and **Answered the question**.
The question and answer sections show the full text.
The question and answer preserve the active validation and its handoff time.
All counts the change while it still belongs to Review.
A chip with a zero count is dim.
The next changed snapshot replaces the index.
An unchanged version retains the index and checks expired Later times.
The fresh index supplies the state and revision of each active item.
The browser removes a saved marker when the index moves beyond its submitted revision and saved state.
A fault returns an unmoved change to Open.
A reply returns an unmoved change to Open.
For a moved change, the List links to the native record with the result.
A works result moves the change to Done without a notice.
Review removes the completed change.
Stable chip IDs retain keyboard focus after a Show load or refresh.
The row window moves other rows around the focused row when the user scrolls.
Each focus change mounts the adjacent rows before the next Tab or Shift+Tab.
A refresh restores focus to the same control by row key.
The browser keeps each pending save by key while the form leaves the window.
The shared table tracks the selected Show chip on both pages.
The address and refresh retain the selected Show view.
Review submissions retain the Type, Show and Owner chips and the search words.
Standard Send, List Send and Review Send to the List owner use the same owner check.
Send uses an active parent when the owner has wrapped.
A recipient chosen in the dialog gets no parent: Send refuses an unavailable chosen recipient.
If a named local document cannot be read, Send reports the reason and leaves the request free for an edit.
Send checks native ID allocation and reads the folder registry before it saves a request binding or an image.
The page owner dialog lists the evidence under **Sent with your words**.
The server starts a screenshot when the dialog opens with **Include a screenshot** selected.
The browser copies the live document before it opens New item.
The copy keeps open rows, open windows, loaded text, form values, canvas pixels, scroll positions, and the focus ring.
The copy omits page scripts and the New item window.
The server renders the copy in Firefox at the same viewport size and theme.
The console address supplies the base for relative asset paths.
The server restores scroll positions and focus after layout.
The browser compresses the copy with gzip.
The screenshot uses a maximum of 5 MiB of compressed data and 32 MiB of decompressed HTML.
If the snapshot exceeds either limit, Send uses page data and visible text.
The draft keeps one copy of the snapshot while editing or sending.
Closing the draft or confirming a filing releases the browser snapshot and its page context.
If a filing leaves edits in the dialog, the next draft captures fresh page data and a fresh snapshot.
Closing the draft aborts the preview upload and polling. The browser asks the server to discard the unfiled capture by key.
Expiry removes an unfiled capture if the discard request reaches the server before it reserves the key.
A retry after closing uses the saved capture key and viewport.
The dialog keeps **Send** available while it takes the screenshot.
Send uses page data if the preview is still pending.
Send saves the capture intent with the native item and links the List draft to it.
The capture key belongs to the browser draft, even when The List saves a different request ID.
The private capture store retains the snapshot until Firefox finishes or the capture expires.
After a restart, it resumes a pending snapshot from that store.
If the screenshot is already attached, the server can restore a missing lookup from the native item.
The item retains its page data if the screenshot is unavailable.
An invalid snapshot, source address, or viewport makes the screenshot unavailable.
An address for a different page also makes the screenshot unavailable.
Send rejects an invalid capture key or a key that belongs to another draft or item.
The server attaches the screenshot when the capture finishes, even if you leave the page.
A saved capture remains available after the preview expires.
The server retries a failed attachment and keeps one screenshot on the item.
After a lost response for a pending capture, Send retries the original payload, draft ID and capture key.
The dialog keeps any edits for a new Send.
Review refreshes the saved row.
The List refreshes its records after the save.
Both pages keep the address and focus while an open item holds edits.
The open row keeps its place on the screen.
Select the preview to open the image at full size.
Send saves the item with the evidence shown in the dialog.
If the native write fails, the server discards the staged evidence.
The server holds the preview for the retry.
If Cancel discards the preview during filing, the server releases it when filing ends.
The draft stays available for edits after a failed Send.
Send publishes the saved item before it confirms success.
Send confirms message delivery before it reports success.
Screenshot capture can continue after the response.
A retry with the same request ID resumes publication and delivery of the saved item.
If the content matches, the retry returns the saved item.
If the content changed or the screenshot cannot be verified, the dialog shows the saved item ID and keeps the edits.
The next Send uses a new request ID.
When Back restores an open dialog, the dialog takes a new screenshot if the box is checked.
Changing the type keeps the same capture.
The server keeps the capture key and preview in its private store until they expire or the dialog discards them.
After a restart, the dialog can request an unfinished preview with the same key and a fresh token.
If the preview expires, the dialog keeps the draft and shows the placeholder while it takes a new screenshot.
If the first Send fails because the preview expires, the checkbox stays available.
Select **Send** again to use the new screenshot or page data.
Send requests a reload if a legacy request for a checked Bug lacks both a capture key and a filed match.
A retry can recover the filed item after a restart.
For a synchronous screenshot, the server uses the capture digest to verify a retry.
It replaces the old screenshot when the checkbox is selected.
Clear **Include a screenshot** to omit the image.
A Bug includes structured page sections and visible text from when the dialog opens.
`new_item.max_page_rows` sets the visible row limit. It ships at 48.
Other types include the same page data when Send requests a pending screenshot or the screenshot is unavailable.
The Review card shows this evidence under **Page data**.
A Bug also includes the page title, address, filters, and render time.
The dialog shows Error lines and Script errors when the page has errors.

The `new_item.*` keys and `page_capture.max_active` in `config/review_links.conf` set the New item limits.
A new Send uses the live values.
Saved items, restart recovery and attachment use the upper bound of each limit line.
If a limit is unusable, Send gets a 503 response and the dialog keeps the draft.
The console unavailable view omits the owner controls and the dialog.

Review and Accounts reuse gzip bytes for an unchanged page with its current owner control.
A Bug with a screenshot also includes the viewport.
A new draft selects the page's default assignee when one is available.
Select **None** to send the item to the producer without an assignee.
The route ends with **bob assigns it** when bob is the producer.
For a Bug, the route says **bob tracks this bug and assigns it.**
Before you choose a producer, the route ends with **The producer assigns it**.
The producer notice says **You assign it.**
Changing the producer keeps **None** selected.
The browser keeps the assignee choice in the submitted draft for recovery and Retry.
**Send** closes the New item dialog at once.
The user can work on the page while the fading **Undo** toast shows.
**Undo** reopens the dialog with the same type, recipient, words, images and page capture.
A failed send reopens the draft and shows the error.
A retry after a lost response sends the original request again.
If that retry confirms the original save, the dialog reopens with any edits made after the lost response.
Review refreshes when Send succeeds and opens the new item if it matches the current filters.
Review tells you when the current filters hide the new item.
A save conflict shows its reason and preserves the current filters.
The Type, Show and Owner chip rows wrap at phone width.
If a refresh hides the focused Owner chip, focus moves to the selected Owner chip.
All types that wait on the user have the state Open.
The Open view shows only Open records.
Legacy For you Show values select Open.
The records keep their actions and their totals in /next, footer counts and the status line.
`ReviewItem.for_you` keeps its name for automation callers and selects Open records outside Later times.
The text filter matches the ID and title.
The address retains the filters, sort and open item.
An address with only `item` selects All and clears the filters.
The default order uses the existing urgency levels and age.
Questions use a violet circle-help icon.
Responses use a gold inbox icon and a gold card border.
Changes use a blue diff icon.
Investigations use a teal microscope icon.
Proposals use a pink lamp icon.
Efficiencies use a lime zap icon.
The HTTP server retains one `ReviewSnapshot` with the records, index and shell.
The worker records source timestamps before it reads the sources.
The background worker checks source timestamps every half second.
It also checks all stores every thirty seconds.
A complete update replaces the current snapshot.
Requests retain the current snapshot during an update.
The server starts recovery before the snapshot worker.
The live service requests the initial Review build at startup.
Creating a console manifest after server construction does not start a Review build.
The same guard tracks and skips each source or record that fails during a build.
A notice names each skipped source or record.
An invalid efficiency mark or Later time leaves the other entries available.
The snapshot converts offset-free times to the user's time zone before it sorts records.
A card with invalid display data opens with an error.
A change to a tracked path starts a new build.
If a shared read fails, the worker keeps the current snapshot.
The worker logs the failure once until a build succeeds.
Review GET and Review news read the retained snapshot.
Each card form carries the record revision.
The revision includes the complete retained record and the card assets.
A Question prerequisite resolves to a Review key during sorting.
A Change with an unreadable delivery record produces a notice and stays out of the snapshot.
A Review save reads the selected record and its sources.
An Efficiency save checks the ledger in the production document retained for its row.
An unreadable Proposal prerequisite leaves its relationship empty in both the snapshot and the save.
The save checks the revision under the store's write lock.
An exact Question save requires a revision check.
The server returns HTTP 409 if the record is missing or its revision differs from the form revision.
The browser refreshes the index and shows a conflict reason above the table.
The refresh keeps the selected filters.
The reason stays visible if the refresh removes the card.
After a save, the server reads and publishes the saved record if it can.
If publication fails, the server retains the current snapshot and requests a full rebuild.
The store write determines success, so the server returns HTTP 200 and runs delivery after either publication result.
A Change verdict uses the current staff assignment to select the production and the recipients.
The server writes the cleanup intent and prepares the verdict file before feature-log closeout.
The server cleans the Change scratch folders after the Review response and before delivery.
On restart, the server reads each cleanup intent and checks its saved verdict.
The server releases the list and delivery locks before it cleans the scratch folders.
An unreadable Change list produces one recovery notice. Recovery continues with the other lists and intents.
The delivery message includes the cleanup result and warnings.
A message uses retained fields if a staff read fails after a verdict.
The server retries a delivery claim that fails before sending.
A rebuild that started before the save must retry.

The browser filters, sorts and counts the index without a request.
It mounts at most ninety viewport and margin summaries, plus one distant row with focus.
It removes distant rows and retains the open key and form draft.
The draft records edits during a pending save and retains them if the save fails.
A card requests its body by key only when it opens (`review.html?body=<key>`).
The snapshot supplies the body, form, log and image reference.
The body uses the time zone from the snapshot.
A change to the time zone changes the snapshot version.
A proposal body includes its issue text without front matter.
An item fragment selects its summary before any body request.
An efficiency card shows its ledger line.

Questions offer Answer and Send feedback in the closing group.
A Response shows the original Question, the owner's latest answer, its time and the log.
**Got it** closes the native Question.
**Ask a follow-up** sends Your words to the same owner with a new reply token for the same key.
The Your words placeholder names **Ask a follow-up** as the action that sends the text.
The Question waits for that reply, then returns as a Response with the same key.
Both actions retain the current filters.

#### Reply tokens for New item

Review stores each reply proof in `review-reply-proofs` under the showrunner state folder.
The default folder is `~/.local/state/showrunner/review-reply-proofs`.
The proof stays with the item number and request ID when the item changes folder or owner.
Each follow-up selects a proof by the reply wait revision in the note.
Review saves the new proof before it changes the note and retains the previous proof.
The store also holds staff question request keys for retry.
Each new item or wait gets a random nonce.
Review derives a 43-character token from the nonce and the shared key with HMAC-SHA256.
The reply command hashes its token and compares the hashes in constant time.
A missing proof refuses the reply.

Each server stores the 32-byte secret key in `$XDG_STATE_HOME/review-links/reply-token.key` with mode `0600`.
The default path is `~/.local/state/review-links/reply-token.key`.
The server creates the key folder with mode `0700`.
The server compares the resolved paths and the file identities of existing folders.
It rejects a key path inside the served root.
natedev creates the key once when it needs to mint a token.
On first use, the Mac copies the key from natedev over SSH and checks its size before saving it.
If another process saves the key first, the Mac reads that file.
Both servers keep the existing key if they cannot read it or if it has an invalid size.
New delivery records store the submission key and a reply exchange ID.
The sender reads the proof from the state store and checks the derived token before sending it.
If token derivation fails during delivery, the server records a delivery failure and returns HTTP 503.
Send uses the transport result even if the server cannot save the delivery failure.
The dialog shows the error and keeps the draft for another Send.
A retry after the wait ends returns the existing item without sending another message.

Both `/new_item` and the New item window send the reply command to the owner through Review delivery.
The CLI and browser return the public filing receipt.
Keep the reply command in the owner session.
Use the delivered `change_page.py answer-question` command for every New item type.

Run `scripts/lib/py scripts/review_links/new_item.py resend-reply I45` to resend a waiting item's reply command.
Use the native item ID or the full submission key.
The command reads the proof from the state store and renews a legacy proof if necessary.
The command sends to the current owner session.
It refuses unknown items and items that no longer wait for a reply.

Run `scripts/lib/py -m scripts.review_links.review_submissions move-reply-proofs --dry-run` to preview the move.
The preview prints three lines.

1. The number of notes and legacy fields to move.
2. The number of those notes with uncommitted changes or without a tracked Git path.
3. The number of those notes that are open items.

The preview validates the same byte changes as the move.
The preview takes the writer locks that already exist.
It reads a note without its lock when the lock file is absent.
The move takes all writer locks and checks each note again.
A wrap saves a legacy proof under the transfer locks before it publishes the destination note without proof fields.
The server finishes interrupted transfers before it moves legacy proofs.
The move holds the same transfer lock as wrap through the pending transfer check and the scan.
The server checks note headers and reads the full record for each note with a legacy proof field.
The move uses the Issue reader for Questions and Proposals.
It uses the numbered record reader for Investigations and Bugs.
The move scans the vaults in `staff.producer_folder_root` and `issue_score.vault_root`.
It also scans the native state stores, the stores in the local folder registry, and both archive layouts.
After you restore an external or former store, run this command with its root.

```sh
scripts/lib/py -m scripts.review_links.review_submissions move-reply-proofs --root <folder>
```

Add `--dry-run` to preview the move without writing files or creating folders.
The move skips source notes that vanish during discovery or a read.
The move requests remote staff records only to resolve an unknown legacy Question owner.
The server moves legacy proofs under the record writers' locks before it binds its socket.
Before rewriting a note, the move saves the entire original note under `review-reply-proof-backups` in the showrunner state folder.
Each run uses a time-stamped folder with mode `0700` outside the vault and Git.
The backup mirrors the note's absolute path inside that folder.
Keep the backup until the director approves its removal.
The move saves the proof and reads it back before it removes the legacy fields.
It retains the original note bytes except for the removed fields and their JSON separators.
It parses the planned rewrite again and checks the remaining values before it writes the note.
It verifies the saved note against that rewrite.
The move refuses duplicate proof containers, duplicate nested submissions, and duplicate proof members.
It leaves a refused note unchanged and reports its path.
The preview reports each refused path and exits with a failure status if any note is refused.
The preview checks conflicts against saved proofs and proofs from earlier notes in the same run.
Discovery checks the header for a proof container on a Question, Proposal, Investigation, or Bug.
The move refuses a malformed container if it contains a legacy proof field name.
The preview and the move check the backup location and the existing journal before scanning notes.
Both check pending legacy transfers and the numbered record metadata that a rewrite uses.
The report reads `review-reply-proof-backups/rewrites.jsonl` in the showrunner state folder.
The journal has mode `0600`.
Before each rewrite, the move appends the note path and field names and syncs the journal.
A restart uses the same journal and reuses entries for notes that still need the rewrite.
If an append stops before it writes a complete line, the next move discards the incomplete line and retries.
The proof store keeps a verifier for each legacy raw token.
A failed move stops the server before it can serve requests.
Run the move before serving notes that still hold proof fields.
Each server that handles scoped replies needs the proof store for the records it serves.

#### Other Review actions

Changes offer Works and Does not work in the closing group.
Ask showrunner to check, Ask question and Send feedback transfer the change to the showrunner.
Investigations offer Mark read and Send feedback in that group.
Proposals and efficiencies offer Approve, Decline and Send feedback in that group.
A plan backlog proposal has no issue to hold an answer, so it offers only Later; the user answers it in chat.
Cards with actions offer Ask question in the return group and **Later** in its own group.
The menu has three presets.

1. **1 hour** adds `later_soon_minutes` to the current time. The label follows the setting.
2. **Tomorrow** uses `later_morning` on the next day in the user zone.
3. **Next week** uses the next `later_week_day` at `later_week_time` in the user zone.

**Tomorrow** shows its time on the right.
**Next week** shows its weekday and time, such as `Mon 09:00`.
A preset click calculates the deadline and hides the item at once.
**Pick a date…** shows a calendar and a time field in the same menu.
Review Later and List Park share the date and time picker in `record_table.py`.
Later presets and date Save send words from the box to the record owner.
Mark read on an Investigation or Bug sends words from the box to the record owner.
If delivery fails, the card keeps the words and offers **Retry**.
The retry sends the saved message. The server also retries pending messages at startup.
Mark read saves the delivery key in the native log with the state change.
If the receipt is missing, Retry and startup recovery confirm the save with that request's delivery key.
Recovery clears an unsaved Mark read delivery after it checks the native log.
A new press can then save Mark read and send the words.
If the box contains newer words, **Retry** keeps the card open and leaves those words in the box.
The calendar keeps a valid future Later draft when it opens, including a draft restored after a refresh.
Otherwise, it opens on tomorrow in the user zone, with `later_morning` as its time.
The month header has previous and next buttons.
The grid starts on Sunday, marks today with a ring, and fills the selected day with the accent color.
Past days are dimmed and disabled.
A summary shows the resolved time beside **Save**.
A skipped wall time advances by the daylight saving gap.
A repeated wall time uses the earlier instant.
The presets use the same conversion.
**Save** is disabled unless that time is in the future.
In the calendar, the arrow keys move one day or one week, including across months.
Tab moves from the selected day to **Time** and **Save**.
The **Time** field uses 24-hour `HH:MM` in every locale.
It accepts `9:00`, `0900`, and `09:00`.
It shows `09:00` when focus leaves the field.
The suggestion list in **Time** starts at the next time step in the user zone.
The default step is 15 minutes.
The list shows future times for today and all times for a future date.
Open the list with the arrow button or an Up or Down key.
Use Up and Down to move through the suggestions.
While the list is open, Escape closes it. Enter accepts the highlighted suggestion; with none highlighted, Enter closes the list.
With the list closed, Enter saves the time and Escape closes the picker.
An invalid time uses the error color and disables **Save**.
Select **Save** to submit the chosen time.
Escape closes the menu from a preset or the calendar.
The hidden `later` field keeps the wall time for the draft.
The page converts that time to UTC for the request.
The arrow keys, Home and End move between items, Enter picks one and Escape closes the menu.
A click outside the control or a move of focus outside the control closes the menu.
While the menu is open, a refresh checks only the script version.
The server stores the preset time in the user zone.
List and Review refuse a new save if the deadline has passed.
A List retry can still return the result of an earlier save.
Waiting cards for questions, investigations, proposals and efficiencies retain their Open actions.
Unowned questions retain only Later.
Waiting cards for these types keep their state and counts until the user acts.
An action on those cards supersedes the open request.
The record and the owner's production log retain the superseded wait.
Questions archive the old followup before the new action saves.
The Review and change pages use one server check to refuse another change action during an open hand-off.
The refusal preserves the request and its delivery state.
The selected Changes route shows the reason from the shared check when the change is Done or with the showrunner.
Investigations close or replace their open request.
Proposals replace their review mark; efficiencies also clear a deferred ledger state.
The primary button comes first.
The shared sender handles all five types, including retries, owner alerts and startup recovery.
Send feedback sends the words to the owner.
It finishes the item for questions, investigations, proposals and efficiencies.
It transfers a change to the showrunner until the showrunner records what they did.
The Review log stores feedback with its request ID.
The Review card shows feedback in its log.
It matches a log entry with a request ID by that ID.
For a legacy entry without a request ID, the card matches the feedback text on the same row.
The card uses the delivery record when no log entry matches.
An older change with only the showrunner's reply in its log shows both the feedback and the reply.
If a delivery slot write fails, a retry of the same Send keeps its ID and uses the existing log entry.
Ask question sends the words to the owner.
A question from Open moves the item to Waiting.
The owner’s answer returns that item to Open.
The answer stays in the item log.
Owners use `change_page.py answer-question --item <Review key> --question <id> --text "<answer>"` for all five types.
The Console text check runs before the answer is saved.

For native replies, the model reads the draft outside the delivery lock.
For scoped replies, the model reads the draft outside the recipient and record locks.
The writer checks the selected record again under the locks before it saves the reply.
Questions and investigations use the check in their own writers.
A failed draft leaves the question open and returns each rule and quote to the writer.
The review log records the reason if the model is unavailable.
Each new request supplies a question ID to identify the pending exchange.
The existing question ID rule refuses a reply to a superseded exchange.
`update_registration.py reply` and `investigation.py answer` apply this rule before they save a native answer.
A native answer without the current question ID cannot replace a newer Review question.
`change_review.py verdict` refuses a verdict while a change is Waiting for an answer.
These commands retain the existing refusal text and exit 1.
Each showrunner check uses its delivery attempt as the validation ID.
Initial and recovered messages include `change_review.py verdict --validation <id>` with the complete answer command.
The native verdict command refuses a missing, old or completed validation ID under the same rule.
Every request message includes the complete answer command.
Later keeps the saved state.
An unread answer takes priority over an active Later time in Review, the counts and `/next`.
An answer keeps the saved Later time.
Mark read returns the item to Later if the time is still active.
Saving Later also marks the answer read.
A legacy Question reply can lack an Answer event.
Each new native reply saves its Answer event position with the exchange.
A Change row with an empty Verdict can still show in Open after it moves to Reviewed.
A validation answer stays in Open, the status counts and `/next` until Mark read.
The change also stays in Validating until the handoff ends.
The shared reader ignores any old Later time on a Validating change.
`<SHOWRUNNER_STATE_DIR>/review-later.json` stores one ISO timestamp for each stable Review key across all five types.
The shared reader applies that timestamp without copying the item or its state.
When wrap moves a Bug or Investigation, it moves the Later timestamp to the new owner key.
The queue and status counts compare and order Later deadlines by epoch time.
The status cache watches that file and expires at the first pending Later time.
The item returns after that time without a new action.
Every action keeps its time, actor and words in the existing record log.
Closed question records retain the user's words without the answer replay archive's 48-hour limit.
Ask showrunner to check transfers the next action to the showrunner.
A successful check moves the change to Done without a notice.
A fault returns the change to Open.
A saved answer or feedback action also returns the change to Open.
After a save, the page shows its result.
A change with the showrunner appears in All as soon as the page reads the saved request.
The page does not open another card automatically.
The shared status component refreshes the page every `page_refresh_seconds`, initially 15 seconds.
Refresh keeps the open item and its draft, including the Assign choice.
The open row keeps its place on the screen when new text or log entries appear above it.
Its log keeps the first visible entry at the same place.
If the assignee becomes unavailable, Assign keeps the choice and shows “not available”.
Approve refuses an unavailable assignee.
Select another option to approve.
The Review navigation entry marks new Open items.
Every type except For you actions can set this mark.
Approved and declined proposals become Done in the shared reader.

Approve and Decline make a proposal or an efficiency Done at once.
Approve sends the words through the shared sender.
For a proposal assigned to a unit director, one save records the approval and the Review log.
The approval records the unit director, their parent showrunner, and the user as approver.
The unit director receives a confirmation.
For other proposal approvals, the message includes the `proposal_issue.py approve` command.
The showrunner chooses the unit when they run the proposal command.
For an efficiency, the command is `candidates.py outcome E<n> accepted`.
The saved answer and log show the selected name as “approved, assigned to <name>”.
A proposal Decline stores the declined answer, as `proposal_issue.py decline-answer` does.
Its message asks the owner to close the issue with `/close_issue`.
An efficiency Decline writes `candidates.py outcome E<n> declined` on the ledger line.
If the box contains text, Decline sends those words to the record owner.
A proposal keeps its page log in the `review_log` front matter list and its page mark in `review_state`.
An efficiency keeps them in `<ledger stem>.review.json` beside the ledger.
That file is keyed by the `E<n>` id and holds only the log and the mark, never a copy of the ledger line.
The mark is `waiting` after Ask question and `done` after Approve or Send feedback.

### Accounts page

Hana console, Accounts (`/console/accounts.html`) shows the accounts board.
The console lists it as **Accounts**, with the Lucide `users` icon.
The **Moves** section precedes the Accounts table.
The section reads batch progress from `~/.local/state/accounts/move-progress/`.
A progress record stores the initiator, cause, target, start time, and member order.
Each member result keeps the requested account. An unknown account shows as moving in the Now line.
The Now line shows which members move now.
The Next line names the next unit and gives the number of other units that wait.
It estimates the time left after the first result.
**All N units** shows the state of each member.
A refusal without named members omits the unit list, count, and progress bar.
It distinguishes members already on the target from moved members.
It shows that a member stopped only when the result confirms the stop.
**Earlier moves** includes the three newest ended batches and any interrupted batches.
The holder note uses `move.stuck_seconds` in `config/shutdown.conf`.
The note uses "task" when the command does not identify the type of task.
The page preserves open move details during refresh.
The writer saves the current batch with each result and saves the full batch at the end.
The page offers retry when the member is off the target account and no operation holds it.
The page uses the board's local session read to check the account, including sessions outside counted roles.
The Accounts POST accepts one failed member from one saved batch.
The server starts the standard hand move command in a detached process.
The command handles the row lock, queue and wait limit.
The failure line and retry response use the same rule for these messages.

- `Another move is in progress. Try again when it ends.`
- `<unit> has no saved session.`
- `The retry did not start. Try again.`

The progress record keeps the diagnostic for the move.
The server logs one diagnostic line to stderr when it refuses a retry or fails to start it.
The browser accepts a retry only when the HTTP response succeeds and its state is `saved`.
A malformed retry or any other response uses the third message.
A second retry starts another hand move.
A hand move to the account the member already uses reports "already on" and stops no process.
The server keeps the last good page, its completion time, its render times, and its usage store revision under one lock.
The first request waits for a render in a child process (`accounts_page.py`).
The server records the usage store file metadata before it starts each render.
File absence is a revision.
A request returns the kept page at once if its revision matches the store and its age is at most `page_refresh_seconds`.
It starts a background render if no render is in progress.
A request for an older page can share an active render if its revision still matches the store when the render ends.
A request for a changed store uses the first render that starts after it reads the store revision.
Requests can share that render and use its result if the store changes again during the render.
The next request checks the store revision again.
The server reads `page_refresh_seconds` from `config/review_links.conf` when it checks the page age.
If the server cannot read this setting, it returns the last good page with a read error.
It returns 503 if it has no good page.
Only requests start renders.
The child keeps the status read's stderr capture away from the server's request log.
`accounts_page_seconds` in `config/review_links.conf` limits each render.
A failed or late first render gives 503 with **The Accounts page is not available.**
A failed render keeps the last good page and writes one line to stderr.
Failed renders add no render time.
A cached Accounts response carries its render failure to the status line.

The Logins card appears before Accounts. When Moves is present, it appears after Moves.
It lists reported login ends in date order, with one row per account and machine.
The card matches each row by SSH alias and account.
A row clears when its stored end changes. Each row gives the `/login` action.
The card uses account colors and formats dates in the user's zone.
`login_card.error_days` and `login_card.warning_days` set the color limits.
The card uses the timer's selector for the next sitting or safety request.
On the other machine, the card reads the copy from the sending machine's timer.
It shows **Login requests unavailable.** if the copy is missing or older than two peer intervals plus the peer timeout.
It shows **No login request.** when no requested date remains.

The Accounts table has two swatch columns without a heading.
The account swatch column is between **account** and **left**.
Hover over an account name to see the email from its login record.
When the record has no usable email, the title says **No email in the login record**.
**Fable left** follows **left** and shows the cached weekly Fable percent for Claude accounts.
It is empty for Codex accounts. It shows a dash for a Claude account when the cache has no current Fable window.
The role swatch column is between **role** and **next**.
After **next**, **weekly reset** shows the weekly reset time.
A running account uses green. This includes a Claude account with an unassigned session.
Claude accounts with weekly quota left use green. Codex accounts use green when current quota or enabled credits permit work.
The color ignores Codex quota and credit readings from the future or older than the configured age limit.
For exhausted accounts with no running session, the reset color uses a scale from green to orange.
The reading time is the start of the scale.
The scale ends at the furthest future reset among accounts with no role and 0% quota in the current weekly window.
A Codex weekly reading sets the end only if its age is zero through `usage_stale_minutes`.
The page uses one status snapshot for the scale and the row colors.
At one quarter of this time span, the color uses 50% orange.
The orange weight changes linearly on each side of that point.
A reset at or after the end of the scale uses 100% orange.
When no account sets the end, future exhausted resets use 100% orange.
An expired reset for an exhausted account uses the default text color.
The reset colors keep at least 4.5:1 contrast in both themes.
**resets** shows the reset count from the Codex account note or a dash when the count is missing.
Claude accounts show a dash in **resets**.
Select a named header or press Enter to sort.
The dot and swatch headers have no sort control.
The arrow sits before a right-aligned label and after a left-aligned label.
The arrow uses the column gap without adding to the table width.
The column gap and arrow scale with the root font size.
The viewer's sort choice stays through refreshes.
The Models card below the accounts table uses the same sort controls.
Its role headings use the same colors as role labels in the Accounts table.
It stores the viewer's sort choice separately and starts in model order.
The card shows the model ID and its swatch at the start of each consecutive group of that model.
The model ID and swatch use the theme ink color when no model color is set.
The two tables have the same total.
The Tokens card below Models shows the renewal steps of each token in a closed `details` element.
Each `details` element carries its token key in `data-token`.
Before a refresh replaces the view, `keepOpenSteps()` opens the fresh steps of each token that the viewer opened.
A refresh also keeps focus on the same summary or command in a token's steps.
A swatch shows for each board span with a `color_key`.
An account has one when `config/statusline.conf` has a valid `account.<label>` color.
The units, showrunners and workers roles use `role.unit-director`, `role.showrunner` and `role.worker`.
The colored words carry `data-color-text` with the same key.
One click opens the picker: a saturation and brightness area, a hue slider, a large color box, a hex field, and Try, Save, and Cancel.
The popup uses the console theme and stays inside the viewport.
Try paints every swatch and word of that key without a request.
Cancel or Escape restores the saved color.
While the popup is open or a save is pending, the status shows Paused and a refresh checks only the script version.
Save closes the popup at once and posts `key` and a lowercase `#rrggbb` value to `/console/accounts.html` as form data.
Saves of one key publish in order; saves of different keys run at the same time.
The server accepts configured `account.` keys, configured keys in `ROLE_COLOR_KEYS`, and valid `model.<id>` keys.
Save can add a missing model color to `config/statusline.conf` for a full ID in the model catalog.
A key that ends in a known host name is a host override.
Save refuses a model ID with that suffix.
The settings journal lets the Mac keep an accepted color during a pull after the model leaves the catalog.
It runs this checkout's `scripts/lib/tune.py` through `scripts/lib/py` to publish the value.
The tune command records the user's Save action and commits the configuration change through its normal publication path.
Success clears the kept page and refreshes the board when no save is pending.
A refused request returns 400 with its reason.
A failed publication returns 503 with tune's last stderr line.
A failed save restores the prior color and shows `<name>: <reason>` in the status line.

The `next` column follows the role color swatch.
A selected Claude group shows its first target in bold text with the target account color.
The same control serves units, showrunners, and producers.
An empty group shows gray `none`.
Its arrow button opens the move menu with a click, Enter, or Space.
The move button leaves the sort order as it was.
The button exposes its menu state through ARIA attributes.
A selected Codex workers line shows its first target, or gray `auto`, without a button.
The `auto` title explains that Codex workers move between Codex accounts by themselves.
While that line shows `auto`, each spare Codex account line also shows `auto`.
Two or more Codex lines with `auto` are the codex pair.
Each pair row has the `auto-pair` class, so its tint moves with the row in every sort.
`background.codex-pair` in `config/statusline.conf` sets the tint: a color name or `#RRGGBB`, drawn at 12% over the card.
An absent or invalid value gives no tint.
When two pair rows are next to each other, the row line between them is hidden.
In a pair row, `auto` takes focus and shows a tooltip on hover and focus.
The tooltip reads, for example, **codex1 and codex2 alternate when available. codex2 is working right now. We are in working workers mode.**
It reads **No account has availability.** only when every pair account has a known 0% left.
Otherwise it names the selected workers account. An unknown percent makes no claim.
The mode is **self-actualizing** while `quota_state.no_worker_mode()` is on, and **working workers** otherwise.
The mode and the quota are separate facts.
Other lines show a gray Lucide minus icon; the total cell stays empty.
An unavailable queue shows gray `unavailable`; selected Claude groups retain their buttons.
The page uses the first queued entry for each group from the status read.
The arrow opens the **Next move of <group>** dialog.
The radio group lists the other Claude accounts in board order.
Each account shows its weekly percent left when known.
The first queued entry supplies the target, percent, and quota measure.
An empty group uses `next_move_percent` from `config/accounts.conf` and the **all models** measure.
An unreadable queue shows **The next moves cannot be read now.**
The dialog disables **Save** and **Move now** in that case.
Editing a field changes only the form.
Tab stays inside the dialog. Cancel and Escape discard edits and return focus to the arrow.
The page pauses refresh while the dialog is open.
**Save** sets the first entry to the selected target, percent, and measure.
The dialog checks the percent range from 0 to 100.
An invalid percent keeps the dialog open and shows **The settings were not saved. Try again.**
**Save** with **No next move** removes the first entry and keeps later entries.
**Move now** starts the CLI hand move in a detached process with the selected account as its target.
The POST returns when the launcher accepts the request.
The detached command claims the move before it changes the selected account.
The Moves card shows the console origin and reports a refusal if another move holds the claim.
The click leaves the next-move log unchanged. **No next move** disables the button.
A failure shows a short message in the dialog and keeps the edits.
Success closes the dialog and refreshes the board.
Save sends `group`, `to`, `percent`, and `measure`. Clear sends `group` and `clear`.
Move now sends `group` and `move_now` and requires the console origin.
The POST logs the exact cause of a dialog action failure. The dialog uses its own messages.

The Roles table columns are role, swatch, **busy, last 24 h**, account, next, busy, idle and total.
The selected account appears first for each role and shows its own counts.
Other accounts with sessions appear directly below it, ordered by descending total and then by account label.
These rows show their own counts and `none` in the next column.
They leave the role, swatch and busy line cells empty.
The table sorts each role as a group.
The busy, idle and total columns sort by the sum across all accounts for the role.
The other columns sort by the first row.
The summary row includes counts from all accounts.
An unassigned role with one observed account keeps its count tooltip.
The busy line column has no sort control.
The first row of each role draws its busy history from `role_activity.read()` as an inline SVG line in the role color.
The page reads the samples once for each render, at the reading's check time.
The x axis is the 24-hour window. The y axis scales to the largest count of that role in the window, at least 1.
The line has one point for each pixel column at most: 288 columns of 5 minutes each.
Each point sits at the end of its column and keeps the largest count in it, so peaks stay.
A sample without the role breaks the line into separate segments; a lone point shows as a dot.
Two samples more than `role_activity.SAMPLE_GAP` (10 minutes) apart also break the line.
Under the line, the hours in the user's zone show as numbers every 3 hours and as small ticks between them.
With no samples, the cell shows only the base line and the hour marks.
The summary row has an empty cell in this column.
The Roles table has no paused or exited column.
A Lucide `triangle-alert` icon in `--warning` follows an account's total when it has paused or exited sessions of the role.
The icon takes focus and shows a tooltip on hover and focus, for example **2 not running: 1 paused (tile), 1 exited (fix2)**.
The icon is an image named **not running**.
The names come from local sessions. A peer count without names shows `names unknown` for the rest.
The summary total shows one icon for all roles.
Each tooltip target (the icon and the pair `auto`) has `data-tip` and `aria-describedby`.
`aria-describedby` points to a hidden element next to the target that holds the tooltip text.
A refresh keeps the focus and the open tooltip on the same target id.
A fixed slot after each total keeps the totals aligned when no icon shows.
The Roles table has a `next` column after **account**.
It shows the same target, `none`, `auto` or `unavailable` as text, without a button.
A role with no selected account shows a dash.
A failed move shows `<group>: <reason>` in the status line.
Codex workers keep their own moves; the menu shows no Codex account.

- The table comes from `accounts.status_board()`: the board columns, rows, total row and tones.
- The problems follow the table.
- The page reads the stored usage only, as `accounts.py status --stored` does.

The page reads its color tokens and base type from `console_theme.py`, and its main column and title from `page_frame.STYLE`.
It refreshes every 15 seconds through `page_refresh_seconds` while the page is visible.
It also refreshes when the console shows it again and when the tab becomes visible.
Review, Images and Accounts use the same refresh helper in `page_frame.py`.
Images has no interval timer.

The shared status line shows the last server render time.
The render value's hover text shows the median of at most 10 successful renders.

### List records in the Studio

The Studio shows the numbered List records.

Select a List row to expand its detail below the summary.
A refresh applies the new data immediately.
The open row keeps its element through a refresh and takes the new summary, data and detail.
Its focus, draft and text selection stay.
The List measures the open row before and after an update or a save result, then scrolls by the difference.
At a scroll limit, it adds space to keep the row in place.
The List keeps the open row in place while another row changes height during an animation.
It reserves space below the list before a row shrinks near the bottom.
User scrolling ends the correction and removes the extra space.
Scroll input at a limit can release the space above the list without changing the scroll position.
Closing the row removes the extra space.
A filter that hides the row closes it.
If the row closes with an action result, the page notice keeps the result until the row opens again or a new action starts.
The footer gets fresh counts from the other pages on each refresh.
The List discards a result if a newer snapshot has replaced its source.
The open row's log scrolls in its own area.
On a desktop, the log ends with the details beside it.
On a narrow page, the log is at most `list_log_max_height_rem` tall, which each render reads.
A refresh, a save result, a sort or a filter change keeps the log's first visible entry at the same place.
Opening a List item leaves its unread state unchanged.
After a lost Park or Remove reply, The List reads the current row state.
A confirmed Park or Remove collapses the row.
An unconfirmed action keeps the row expanded.
If the current state is unavailable, the row shows the last confirmed state.
If a refresh removes an open item, its row closes.

`/console/studio.html` reads the numbered List records on each request.
The page uses the shared status line in `page_frame.py`.
The page owner button opens the shared New item dialog.
For a new draft on the Studio, **To** selects the producer from `page.studio.owner`.
If the Studio has no configured producer, **To** has no selection.
For a Review kind, **Send** files and delivers the Review item to the selected recipient.
The List assigns numbers when its own record writer creates an item.
The browser stores the recipient selection with the draft ID and restores both after a reload.
Retry uses the submitted fields when the browser saved them.
Select **Edit a separate draft** to keep new edits while you confirm the earlier Send.
If the browser stored only a draft ID and recipient, enter the original fields and select **Send**.
Each Review draft ID can file one Review item and one linked List row.
An exact retry returns the saved item ID.
The dialog closes after the retry unless it holds a separate draft or shows an attachment warning.
The reply includes the List number if the row exists.
If a retry changes the saved draft, the window shows the saved item ID and preserves the new text.
A retry can use a new screenshot if the first screenshot expires before filing.
After filing, an exact retry uses the saved screenshot identity and digest even if the preview expires.
After Send, the List refreshes with the current filters.
Select a row to open it.
If a row save is in progress, the List refreshes when that save ends.

The `/new_item` command (`scripts/review_links/new_item.py`) sends the dialog request to the same New item endpoint.
The server checks the producer against current staff under the List lock.
If the producer is active, the server writes the row in morty's List folder before it sends the notice.
The server reply contains the Review item ID and key.
The title uses at most 120 characters from the first line with words.
Latest status holds the words.
Evidence links to the Review item.
The server saves the row and its Review link in one write.
Owner is the recipient.
The command sends the assignee ID so the server can find the producer and unit.
For a List window Send, the server uses the recipient's producer.
Send reads the producer map from disk without Git recovery.
If the producer map cannot supply a producer, the server keeps the saved note in Review and sends its notice.
The same path applies if a readable staff record has no producer row for the saved producer.
For a Review item with a separate List row, the same path applies if that producer retires.
The server logs the reason it skipped the List row.
The window keeps the dialog open and names the saved Review item if it could not file a List row.
The command reports the same result.
The command saves its draft ID, recipient ID, and assignee ID before the POST.
A retry of the same command uses those saved IDs even if the assignee name changes.
The saved assignee ID can recover a draft after removal of the staff row.
A legacy draft matches the same request by the typed reference, normalized reference, or resolved staff ID.
The command refuses a matching legacy draft if its filename cannot prove the assignee ID.
The command ignores other legacy drafts.
The server resolves the assignee by ID if the first request did not file.
The command clears the draft after success or a confirmed refusal before filing.
The result names the type and assignee.
It includes the List number if the server filed a row.
For a new filing to a showrunner or unit, it also names the producer.

The columns are #, Owner, Producer, Item, State, Next step, ETA, and Update.
The Producer cell shows the name of the record's producer.
A one-time Scheduled row shows its start below the state.
A repeated row shows the rule and the next start, or **No next run**.
For a pending run, Next step shows the instruction and ETA shows the start.
On desktop, the ETA cell stays on one line and shows an ellipsis if the text is too long.
Hover or focus on the ETA to see the full text.
On a phone, the ETA wraps to show the full text in the row.
Past times and past range end times use the same warning color as Check overdue.
The ETA, next check, and parking time use the local date and time in the form Nov 20 09:00 EST.
The Park picker uses this date form for the selected time.
Active parking hides the next check.
When parking ends, the row shows the current check.
The server puts parked items after other items in the default order.
Within each group, the List uses `the_list.order`.
User decisions come first, then unread updates, then the earliest next check.
The page uses the order from the latest server response.
A column button sorts all matching items by its values.
A second click reverses that order.

By default, the List starts with All selected in the Owner, Producer, Tag, and Show rows.
Owner and Producer list staff in name order after All and omit every trailing `-unit` from names.
If the resulting name is empty, the chip shows the original staff ID.
The address keeps the original staff IDs, including `all` when it identifies a staff member.
The words box aligns with the right edge of the Top priority band.
When the box contains text, it uses the same accent as a selected chip.
The box also shows the Clear filter button.
Click Clear filter or press Escape in the box to remove the words.
The other filters stay selected.
The address stores the words as `q`.
The List restores these words on reload.
The List ignores form text that the browser restores without an input event.
The chip order is All, Needs me, Producer answering, Working, Blocked, Parked, and Scheduled.
The List excludes Closed records from rows, filters, and counts.

Tags appear as chips under each item title.
The Tag row counts open items that match the words and the other filters.
The server applies the `tag` query before the first paint.
The server and browser ignore query tags that contain control characters or lone surrogates.
Select a chip in the Tag row or under a title to filter the items.
Each item must have every selected tag.
Type the first letters of a tag to find it in the suggestions.
Select All in the Tag row to clear the tag selection.

Opening an item creates its Tags section from a shared template.
The editor reuses signed List action data when the item has it.
Otherwise, it requests signed data for the keeper and item when the item opens.
The Tags section appears before Links.
Type a tag and press Enter, or select a suggestion.
The suggestions show matching tags first and offer Create for a new tag.
The field keeps focus after a save.
The field shows the tag validation error for invalid text.
Tags cannot contain Unicode control characters (`Cc`) or lone surrogates (`Cs`).
Select × to remove a tag, or press Backspace in the empty field to remove the last tag.
The editor offers Undo only for a removal that the server confirms.
If another session already removed the tag, the editor shows the current tags.
Select Undo before the notice fades to add the tag again.
The same signed List action path uses `the_list.add_tag` and `the_list.remove_tag` to save the record header.
The List sends tag edits for one item in order and shows the pending count.
Backspace uses the tag order after the pending edits.
If a save fails, the List reads the item's current tags.
The editor shows a save error only for the latest edit.

Parked includes items with an active park, no unread answer, and no open Go request.
Needs me includes each item with an unread answer or an open Go request.
It also includes each unparked item with an unread delivery release.
Producer answering includes other unparked items with an open question.
The Producer selection limits which items the chip shows and counts.
Producer answering uses the Show value `producer-answering`.
Saved links with `waiting-answer` or `morty-answering` select Producer answering.
Needs me also includes unparked items in Waiting for nate or Delivered with no open question.
Working includes other unparked items in New or Working.
Blocked includes other unparked items in Blocked or Paused.
Scheduled includes items in the Scheduled state.
All includes every open item, including parked items and Done items.
The State cell shows the parking end time or delivery condition under the saved state.
The Unread only switch shows and counts unread items.
Each chip count reflects the words box and the filters in the other rows.
The server applies the address query filters before the first paint. Python and the script share one filter rule and one list of address names.
Each source first expands its legacy names (`showrunner` to `owner`, `show=unread` to Unread). Then, for each name the server reads (`LIST_QUERY_FIELDS`), the query wins when the query and the fragment both name it. A value that only the fragment names applies after load.
For other names, such as sort and dir, the fragment value wins after load.
Search folds case with a pinned Unicode 15.1 table in `search_fold.py`, the same in Python and the script.
In All, each Owner and Producer chip counts matching parked items.
The “need you” total uses the Needs me chip count.
A zero-count chip stays visible and dimmed.
When filters hide every row, the empty row names the filters and offers a reset.
The chip rows wrap when the available width decreases.

The expanded row shows the saved state, status, next step, ETA, and done condition.
It also shows the questions, answers, actions, and log.
Open Details in a log row to see its evidence.
The address identifies the open item or the target of its link.
Back and Forward can open an item that the table filters hide.
Select the row again to collapse it.
Expanding or collapsing a row preserves the table filters and sort order.
A linked Review card opens over the expanded row.
An answer that supports **Undo** closes the card at once.
**Undo** or a failed answer reopens the card with its draft and the result message.
**Undo** focuses the feedback field.
A successful answer refreshes the List.
The List shows the item count above the table.

A link under Links opens the target item in the console.
The table can change Show to All to reveal the target.
A direct link with no filters opens a parked item in All.
The table clears each filter that excludes the target and keeps each filter that matches it.
The table scrolls to the target and highlights it briefly.
The table leaves a gap below the frozen top.
A missing target shows “Item #N is not on the List”.
A closed target shows “Item #N is closed”.
The source row keeps its form and draft while it shows the target notice.
Changing a filter or the sort order clears this notice.
Back restores the previous filters, open item, and table scroll position.

Each List record stores unique `ReviewItem.key` values in link order, without duplicates.
`the_list.py link L<n> --review <id> [--review <id> ...] --source <who>` adds links.
`the_list.py unlink L<n> --review <id> [--review <id> ...] --source <who>` removes links.
Both commands accept `--review <shown id>` and `--review-key <key>`.
Both options can repeat and mix in one command.
The record retains the order of the options.
The `--review` option resolves only shown IDs.
The `--review-key` option selects only an exact key.
The commands resolve shown IDs through `review_items.read` before changing the record.
An ambiguous shown ID refuses the command and lists the matching keys and their types.
The link command refuses unknown IDs and keys.
Closed items refuse both commands.
Repeated links and absent links cause no write or log entry.
Each changed record gets one log entry with the resolved keys.
Old records without the reviews field load with no links.

Each List record stores a `producer`: the fixed staff ID of a staff row with the producer role.
`the_list.py add` and `update` take `--producer <name or staff id>`.
The option refuses a name or ID that does not resolve to exactly one current producer row.
New assignments, batch creation, and reopen require a current producer staff row.
Morty's adds without `--producer` assign the item to morty.
Another producer's adds without `--producer` assign the item to the caller.
That producer can name only itself in `--producer`.
Old records without the field use their record keeper.
The terminal list shows a Producer column after Owner.
The CLI shows producers only their assigned items. Morty can see every item.
Producers can park and close their assigned items. See `showrunner-automation.md` for CLI permissions and assignment notices.

See the [List store](showrunner-automation.md#the-list-the_listpy) for the dispatcher, confirmation, and CLI rules.
The `schedule` field uses `start` and `do` for the first start and instruction.
The `repeat` and `run_allowance` fields hold the series rule and allowance.
The `runs_before` field gives the run number offset.
The `runs_used` field counts runs that have used the allowance.
The `finished_runs` field names the runs that finished.
The optional `following_runs` field names the run numbers kept by a scoped rule edit.
The `exceptions` field stores changes and skips by run number.
Active deliveries and confirmations use the `run_deliveries` field.
The page and the dispatcher use `ScheduledSeries.next_run()` to select the pending run from saved progress.
Herdr upkeep calls `the_list.fire_scheduled`.
The dispatcher reads `scheduled_check` to decide when to load the full record.

For a browser filing, `new_item_draft_id` holds the Scheduled draft ID and its filing claim.
The claim includes the request fingerprint, keeper, filing readback, and assignment notices.
`list_draft.py` defines the claim and parses the saved field.
It also constructs the List store used by the writer and recovery.
The draft index records the reserved List number before the writer saves the item.
If a claim has no receipt, recovery validates the complete List record and checks the claim before it recreates the receipt.

When List records have review links, the page reads review items once per render and indexes them by key.
It stores no review states on the List record.
The State cell shows “N changes for you” when every review that needs nate is a change.
It shows “N reviews for you” for other review types or mixed types.
Without a review for nate, unfinished reviews show “N reviews waiting on others”.
A count of 1 takes the singular, for example “1 change for you”.
When every linked review is Done, it shows “Reviews done — decide: close, keep or park”.
A missing review key counts as Done.
An item without links shows no review line.
The expanded List row repeats the line above its actions and shows each review's shown ID.
Only types in `review_page.TYPES` link to Review cards.
Other types and missing reviews show plain text.
A review ID opens one card in a dialog on the List.
A summary links to the card when exactly one review is available.
A summary for zero or multiple available reviews is plain text.
The List loads the card document only when the dialog opens.
The existing Review body route supplies the title, metadata, body, and controls.
The Review table and the dialog use the same body renderer and action code.
The dialog places the full title above the metadata, with Close beside the title.
The metadata shows the Waiting reason and urgency when available.
The header reserves a 52 px row for the save status and Show updates.
A revision conflict keeps the current body and draft until the user selects Show updates.
The body scrolls between the header and the feedback controls.
With feedback controls, the body reserves at least 96 px.
The whole box scrolls if the header, footer, and body minimum exceed the frame height.
The Later menu and calendar open over the body above the footer.
The box reserves room for the menu and calendar so they do not cover the footer.
The body hides empty sections and sections that match the title without inline code marks.
The body shows the item text and log in one column below 800 px.
When the item has no text, the log uses the full body width.
The dialog reads a Done card on demand and shows it without actions.
A Validating card lets the user ask the validator a question.
If the record disappears, the dialog shows a message.
Escape, Close, and a click outside the card close the dialog.
Focus returns to the List link.
The dialog reaches the right edge of the List column, so it meets the action area and starts below it (`--page-window-top`).
At the console's phone breakpoint, the dialog fills the screen below the action area, including in the Studio.
On a desktop, the List centers the dialog horizontally, 16 px below the action area.
On a page without an action area, the phone dialog fills the screen and the desktop dialog is 16 px from the top.
The dialog dims the List with a 90 percent black backdrop and a 3 px blur.
Switching browser tabs keeps the dialog and its draft.
Changing console pages closes an idle dialog when it has no draft.
The dialog preserves the List and Review addresses and filters.
The dialog uses the Review save path for its actions.
During a save, the dialog stays open and disables its buttons.
A failed save keeps the draft and shows the reason.
Review and the dialog use the same recovery code to load the current revision and keep the draft.
The draft store uses the record key and tracks edits across form replacements.
Recovery preserves edits made after the conflict, including cleared words.
The recovery lock applies to the stale revision.
After a save, Close, Escape, and a click outside work while the List refreshes.
A completed refresh fades the card out and closes the dialog.
A revision replacement fades the old card out and slides the new card in.
Both transitions use the configured card durations and respect reduced motion.
If the List request fails or times out, the next periodic refresh can update the row.
List and Review refresh, save, counts, and linked record requests use `page_request_timeout_seconds`.
During a pending List action, periodic and console visibility refreshes check only the script version.
A refused List action restores the confirmed row and shows the reason.
A refresh for a saved Review change or a new item waits until pending List actions end.
An explicit item target joins the linked selection and opens its card.
The shared table names each linked key that the page cannot find.
It keeps the found cards when another linked key is missing.
Linked counts use only the selected cards, including selected parked cards.
The List number column and the Review ID column use the shared no-wrap rule.

On desktop, the List sizes the Owner column to the widest owner name within the available space.
Item and Next step share the remaining width in their original proportion.
On desktop and phone, owner names wrap at hyphens first.
A segment wraps within a word only if the segment exceeds the available width.

The Owner menu includes record owners without a current staff row.
The Producer menu beside it lists each producer that a record names.
An address with a producer that no record names shows all producers.
The State column shows the saved state of each item.
Closing an item clears its unread mark.
An older address with `show=closed`, `show=done`, or `state=Closed` opens All.
A link to a Closed item shows “Item #N is closed.”
An older address with `show=unread` turns on Unread only.
The search field matches the item number, owner name, title, state words, or original request text.
Numbers can include `L` or `#`.
One match function ignores case for every field.
Parked items also match the word `Parked`.
The address retains the owner, producer, status chip, unread switch, search, column, and direction.

A row opens to show its latest status, completion condition, item links, and log.
HTTP and HTTPS links in the open row's item text and log open in a new tab.
Whitespace, angle brackets, double quotes, and the start of the next URL scheme end a link.
Only HTTP and HTTPS URLs become links.
Trailing punctuation stays outside the link. Asterisks and apostrophes within a URL stay in the link.
The Links section follows the item fields and precedes the actions.
The section includes jumps to related and blocking items on the List.
URLs in blocking conditions appear as links beside the blocking item.
The section lists each distinct URL from the fields, log, and answers in order of first appearance.
The section uses the Next step label for URLs in schedule instructions.
Each URL row shows a link label, the oldest source field, and the full URL.
The Links section uses the original question time after the question closes.
The section is absent when the item has no jumps or URLs.
The log shows its newest entry first.
On a wide page, the log is to the right of the current fields.
On a narrow page, the log is below the current fields.
A refresh restores open Evidence Details by item and log entry.
Each Details summary has a stable ID based on its item and log entry.
Each log writer saves a typed event.
The table and log use the same format for staff names, local ETAs, and field values.
The table, log, and Park response show parking times in the user zone.
At local midnight, the Park notice shows only the date. The table, log, and Park response show the time `00:00`.
The log uses plain field labels, such as Next step and Latest status.
The List replays the log in record order to find the previous value of each field.
Replay starts with the creation defaults for fields that Added omits, except the requester.
If Added omits the requester, the first update that records it shows the value.
New Added entries record the effective requester.
Replay applies linked delivery, Close, and Reopen before it compares later updates.
An Updated entry shows its time, source, and the fields that changed.
An Updated entry compares Evidence lines with the last Evidence value that a field change recorded.
The comparison ignores line endings.
The entry shows new lines in order or shows “Not recorded” when the update clears Evidence.
It omits Evidence in other updates with no new lines.
Evidence cleanup entries remain in the log and do not affect this comparison.
Added keeps all its recorded fields.
The Evidence in Added and Updated starts inside a closed Details control.
The reader converts old Added, Updated, Parked, Closed, and feedback entries to the same format.
An old entry keeps its text if the reader cannot parse it.
The reader requires the fields for each event.
The reader applies the same field rules to log values and item values.
Typed feedback and request receipts require the List page source.
All entries use the same label column width.
The log shows feedback words and delivery status.
Answer and Mark read store explicit events.
The Answer event includes the question ID.
Link and Unlink events include the review keys.
Evidence cleanup events name the copy that was removed.
Request receipts stay in the record and are hidden from the log display.
The item dialog shows Remove, Park, and a text box for Ask question and `Tell <producer>` when available.
The text box uses `Your words for <producer> about this item` as its placeholder and accessible name.
The page reads the producer name from the staff snapshot.
A producer without a staff row shows as its staff ID.
The Studio header and its copy above the open Review card use the same New item button.
The dialog selects the configured Studio producer for a new draft.
Ask question and Tell sit below the box at its right edge, beside the send status.
Both buttons stay disabled until the box contains words.
Clearing the box disables both buttons.
Record text uses the shared review formatter for bold text, headings, lists, code spans, and links.
Code spans take priority over links and bold text.
URLs keep unmatched backticks and interior `*` characters, but a URL never ends with `*`.
A matched code span ends the URL before its opening backtick.
Bold delimiters require adjacent text and a valid whitespace or punctuation boundary.
Bold content is nonempty and cannot start or end with whitespace or `/`.
Delimiters between word characters or slashes stay literal, so `src/**/test/**/file.py` stays literal while `**read/write**` becomes bold.
Mark read appears only when the item has an unread update.
Mark read clears the dot and adds a log entry.
Mark read on an unparked Waiting for nate item with no open question and no open ask also sets the state to Working.
An open ask whose target has no staff row does not count here, because the page shows no box for it.
A Go that leaves no ask with a box moves the item to Working by the same rule; see [Asks for nate's Go](#asks-for-nates-go).
The same write logs the state change and saves a notice for the item's producer.
The notice asks the producer to write the next step with `the_list.py update --source <producer> --next-step`.
For an item in a legacy keeper folder, the command also has `--keeper <keeper>`. For morty's folder, it has no `--keeper`.
The existing sender delivers it with its retry and alert rules.
The page applies the Working state from the response, so the row leaves Needs me in place.
Mark read on any other item sends no message.
All actions use the record keeper and item ID from the signed form.
Ask question and Tell use the same path to mark the update read after the action saves.
The form signature includes the unread update identity and the feedback recipient's staff ID.
A new Ask question or Tell request can mark only that update read.
A repeated request cannot mark a newer update read.
An exact Tell retry uses the read target saved with the feedback.
The store checks the update identity under its lock.
This check preserves an update that arrives between the action save and the read write.
An update identity includes its time and log history.
Two updates with the same time keep separate identities.
Ask question and Tell add no read entry when the form shows no unread update.
The POST response includes the current unread state.
The form keeps its signed update until a page refresh shows a new snapshot.
The page clears the dot and Mark read button when that state is false.
It restores both controls when the response reports a new unread update.
Mark read, Park, and Remove release the link to the item.
If the item stops matching the filters, it starts to fade as soon as the user submits the action.
If the read write fails, the question or feedback remains saved and the sender can deliver it.
The store keeps the unread update if the read write fails or a newer update exists.
Tell shows `sent to <producer>` and reports whether the update remains unread.
Tell applies the current filters. A read item leaves the view when Unread only is on.
Tell with empty words changes no record.
Ask question saves an open question in the item record under the List lock.
List and Images share the code that saves and sends a question.
Ask saves the current producer as the question recipient.
If the producer is unavailable, Ask saves the record keeper as the recipient and records the unavailable producer.
On the shared List, the record keeper is morty.
At send time, the server finds the saved recipient’s current session.
The server replies to the browser before it sends the item number, title, question, and answer command.
Reassignment keeps the saved recipient. A question without a saved recipient belongs to the record keeper.
Equal words select the open question. Different words create a new question.
An exact request retry uses the same question ID after an answer.
The row moves to Producer answering when the question saves.
The row shows a waiting note when each unanswered question has a live sender or confirmed acceptance.
Acceptance includes a confirmed place in the transport queue.
The List changes the search results, chip counts, and totals as soon as the browser receives the save result.
The console updates its unread indicator on its own news refresh.
The shared question renderer shows one “Question not delivered.” line for each confirmed failure.
List, Images, and Review use this renderer.
The saved attempt ID names the sender lock.
List, Images, and Review question sends use the same retry loop.
An unconfirmed send keeps its saved message ID for the next attempt.
The server retries unresolved attempts during recovery.
If the server cannot record a failure, it logs one line and keeps the attempt for recovery.
The question reader checks the saved question words and answer command.
An attempt saved before the `--keeper` flag also matches the same command without the flag.
A List title edit preserves the delivery state.
Recovery sends the prepared message with its original title.
If Ask removes the focused row, focus moves to a nearby row with an available toggle or to Search.
After a failed POST restores the row, the List waits for the animation before it returns focus to the submit button.
After a successful Ask keeps the row visible, the row toggle receives lost focus and keeps it through refresh.
Focus stays on the user's selection if they move it during the request, including to the parent console.
On refresh, the List restores focus to an available action button with the same item key and action.
Column sort buttons keep focus through refresh.
A waiting row shows `<recipient> will answer.` for each saved recipient with an open question.
If Ask chose the record keeper because the producer was unavailable, the row shows `<producer> is not live, so <keeper> will answer.`
The panel shows each open question with its asked time and the linked review cards.
An actively parked waiting row shows the parking condition and Unpark.
A waiting row with an unread answer or delivery release shows Mark read.
The waiting row keeps its log after Ask saves.
A link opens a filtered-out question or read item in a separate section and preserves the current filters.
The section does not change the results count.
The List address preserves a linked item outside the filters when the console reloads.
It counts in its current status chip.
Close the linked item or change a filter to dismiss the section.
An open question adds a Producer answering note below the saved state.
The saved recipient, the record keeper, or morty can answer an open question.
The message supplies the command `the_list.py answer L<N> --keeper <keeper> --question <id> --text "<answer>"`.
Without `--keeper`, `answer` uses the caller's List folder if it holds the item. Otherwise, it uses morty's folder.
`update` uses morty's folder by default.
For a legacy item, use `the_list.py update --keeper <keeper> --id L<N> --next-step "<step>"`.
A producer can name any keeper in `update`, but it updates only its own fields on items it produces.
Each command refuses a `--keeper` that has no List folder.
In a keeper folder other than morty's, `update` refuses `--producer` and `--owner`.
The answer adds one record event and marks the item unread.
Closing an item moves each open question to a “Question closed with item” log entry.
The entry keeps the question ID, words, time, and saved recipient.
The question no longer accepts answers or retries.
Older Closed records stay in the store in their original format.
Question delivery checks the current List record under the same lock that Close uses.
List, Images, and Review use the same send loop.
The loop checks the saved source and recipient before each attempt.
New List attempts save the keeper and the resolved item path.
The sender checks that the keeper's item still resolves to that path.
An older attempt can name the keeper only in its saved answer command.
The sender checks that this keeper's item resolves to the saved path.
For a command without `--keeper`, the sender can use the recipient, the source path, or Morty to locate the keeper.
Recovery sends the original command.
A close preserves the delivery result of a send already in progress.
The sender checks the item again before it sends a failure alert.
A read error during retry authorization preserves the confirmed failure and reports the read error separately.
Recovery skips closed or answered questions.
The server checks deferred questions again after the sender locks become free.
A new Ask on a Closed item is refused.
An Ask with a saved request ID can resume its existing attempt.
A replay after a confirmed failure returns the saved result without sending again.
After a confirmed failure, Ask requires a new request ID to start a new attempt for the same question.
An item with an active park stays in Parked while it waits for its first answer.
A signed Unpark request from an earlier page can clear parking while the item waits for an answer.
That request moves the item to Producer answering and shows its confirmation next to Show.
An unread answer appears in the panel with the question, both times, and an Unread mark.
Mark read clears the signed update shown by the form.
Mark read sends words from the box to the item's producer.
If Mark read requests the next step, it adds the words to that request.
Opening a row leaves its unread state and status chip unchanged.
This rule also applies to direct links, refreshes, and reloads.
A newer update stays unread if it arrives before the read saves.
After the item is read, the log shows “Question Q<n> — nate” and “Answer — <producer>” at their recorded times.
Each of these rows has a left bar in the Review question color and a bold Question or Answer label.
The List and Review use the same question color rule.
Mark read restores the List queue for the saved item state and its active parking condition.
The panel clears the question when the log shows it.
The List uses its local date and time format for questions and answers.
A newer unread update leaves previously read answers in the log.
The command refuses an unknown ID, a second answer, and empty text.

List and Images questions share one delivery record for each question and recipient.
The record stores the source, stable message ID, revision, attempt ID, job owner, transport receipt, and error.
The delivery lock protects every atomic update.
A job holds the question's sender lock from claim through result publication.
The sender publishes `sending` before it calls the transport.
The transport can confirm `sent`, `queued`, or `failed`.
A missing confirmation leaves the outcome `unknown`.
Recovery takes the row lock, the saved attempt lock, and the lock named by the saved job ID without waiting.
Recovery creates missing lock files.
If a lock is busy, recovery queues the attempt for another pass.
If a lock fails to open, recovery queues the attempt and logs one line.
Recovery verifies the saved question and keeps all locks through the send and result write.
If a sender stopped after it wrote the send marker, recovery reuses the same message ID.
A closed item or a question that has an answer requires no send.
The server clears that claim without a failure notice.
If the server cannot start the send for another reason, it clears the claim and logs the error.
Remove closes the item through `the_list.close` with source `the_list.PAGE_SOURCE`.
Remove sits at the left edge of the open row above the four-line text box.
Remove saves the exact text from the box as closing words and adds it after the removal line in the notice.
A blank box uses the short Studio notice and the default closing words.
Remove accepts a retry if trimming its reason gives the reason in an older receipt.
After a confirmed Remove, the page clears the submitted draft if it still matches.
Park and Remove collapse the row after the save.
The table applies the saved state before the next refresh.
Park, Remove, Tell, Ask question, and Mark read show a message if the page cannot confirm the save.

#### Schedule controls

Open **Park**, then choose **Pick a date…** to use the calendar and time picker on an ordinary row.
Leave **At that time, do this** empty to park the item.
Enter an instruction and choose a future start to schedule the item.
The picker names the owner who receives the instruction and the producer who receives a copy.
**Edit schedule** opens the picker for a Scheduled item.
**Start now** moves the pending run to the current time.
The start message adds the words from the main box after the saved instruction.
Each press replaces the box words for that run.
For a series, **Start now** applies to **This run**.
**Unpark** on a Scheduled item removes the schedule for the selected scope.
It sends any words from the box to the item's producer.
Clear the instruction to replace the schedule with a parking time or **Until delivered**.

The **Repeat** menu appears when the picker has an instruction and a start.
The menu offers **Does not repeat**, **Daily**, weekly, monthly, and annual presets, **Every weekday**, and **Custom…**.
The date-based presets follow the selected start date.
**Custom recurrence** sets an interval in days, weeks, months, or years.
For a weekly rule, select the weekdays.
For a monthly rule, select a calendar day or a weekday pattern.
The rule can end on a date or after a count.
**Never** uses the saved run allowance.
Select **Done** to return the rule to the picker.
Select **Save** in the picker to apply the rule.

The scope dialog offers **This run**, **This and following runs**, and **All runs** when they apply.
**This run** changes or skips the next run. A repeat rule change needs a broader scope.
**This and following runs** is available after a run finishes and a pending run remains.
An instruction-only edit with this scope preserves run dates, moved runs, and skipped runs.
Park disables **This run** and explains that parking applies to the item.
The dialog selects the first available scope.
If a repeat edit omits a start, the List uses the selected run's displayed start, including a moved start.
The writer checks the schedule and selected run under the write lock before it saves a scoped edit.
This check applies when **All runs** uses the displayed run as the new start.
If the schedule or run changed, the writer refuses the edit and keeps the saved schedule.

A new rule uses the page's display time zone.
A new CLI rule uses the selected run's time zone.
A retained rule keeps the saved series time zone when the start changes.
A retained Custom rule also keeps its saved text.
The picker shows a validation message if the new start falls outside the retained rule.

#### Ordinary parking and feedback

**Unpark** and **Park** sit at the right edge of the row.
The dialog shows a parked item's parking condition above the buttons.
**Unpark** appears in the form when the item has active parking and its state is outside Closed.
**Park** opens the menu with **Until delivered** before the shared time presets.
A click on **Until delivered** saves the choice and shows “Parked until delivered” in the row.
A click on a timed preset saves the selected time.
**Pick a date…** opens the shared calendar and 24-hour time field.
The picker aligns with the right edge of the text box.
The server publishes the earliest active parking deadline.
The List refreshes at that deadline.
If the Park menu or picker is open, the List refreshes when it closes.
While the picker is open, a refresh checks only the script version.
**Save** sends the chosen time in UTC and keeps the words in the request.
Each Park preset also sends the words in the request.
Park writes a date, a time, or `delivered` through `the_list.park` with the List page source.
List and Review use the same Later code to read and write their saved values.
The server reads a date as midnight in the user zone.
The writer checks a new parking time against the current time under the List lock.
The writer checks a Park retry against the parking condition in the receipt or the event with the same request ID.
After an unconfirmed Park save, the List retries once with the same request ID, token, and deadline.
The List uses `frame_retry_seconds` for the retry delay and `page_request_timeout_seconds` for each request.
A new preset click uses a fresh request ID and calculates the deadline at the time of the click.
**Retry words** sends the saved words without changing the parking time.
A queue timeout keeps the words unconfirmed, even when the existing notice has a sent state.
The item log shows “Feedback delivery unconfirmed to” for those words.
When the item opens after a reload, the page reads the saved delivery and shows **Retry words**.
Reopening the picker keeps a draft that matches an unconfirmed request, even after its deadline.
The calendar gives focus to an enabled day.
Park sends the words to the producer when the box contains text.
Park and both forms of Unpark send the words to the producer recorded when the action saves.
If an existing notice goes to that producer, it includes the words.
If the notice goes to another recipient, the producer gets a separate copy.
**Unpark** clears parking through `the_list.unpark` and keeps the item state.
It records one Unparked event and saves one notice.
The record keeper receives the usual notice.
If the item has no active parking time, the writer saves a hidden request receipt.
If the box contains words, the writer then sends those words to the producer.
The receipt records whether the time expired or was absent.
A replay of that request preserves any newer parking time.
The response reports the saved request outcome and the current parking state.
The page shows “Parking time already expired” for an expired time.
It shows “No active parking time to clear” when the time is absent.
Both results preserve the parking draft.
A replay of a successful Unpark reports the original result and any newer active parking time.
If the row stays open after Unpark clears parking, it shows “Unparked”.
If clearing parking removes the row from Parked, the page shows “Item #N unparked.”
The page shows confirmations for hidden rows next to the Show filter.
It keeps one confirmation per hidden row after overlapping Unpark requests.
Focus moves from **Unpark** to the Parked filter and brings the filter and confirmation into view.
If All is active when Unpark saves, the item row stays open.
After Unpark, the item's saved state sets its status chip.
After Park or Unpark saves, the List refreshes the server order even while the item waits for an answer.

The CLI commands `park`, `unpark`, `close`, and `reopen` use morty's folder.
The item's producer can run `the_list.py park --id L<N> --until delivered --source "<nate's words>"`.
Morty can use `--source morty` to set this condition.
The List ends this park automatically when the producer sets the item state to Delivered.
The same write clears the park, marks the item unread, and records an Unparked event.
Linked changes supply review information on the List.
The List shows historical Unparked events that name a linked change.
The unread delivery release returns to Needs me with its review links.
After Mark read, an open question returns the item to Producer answering.
A dated park stays active until its time, Unpark, or Close.
The shared item writer saves the action and its pending notice in one locked write.
The notice tells its recipient which action nate completed.
A Go next-step notice names the item's producer as its recipient.
The existing sender delivers the notice with its retry and alert rules.
A delivery failure does not reverse the completed item action.
Tell saves the user's words in the log and sends them to the item's producer.
Tell keeps the parking time.
Tell moves a Waiting for nate or Delivered item to Working in the same write that saves the feedback.
Remove hides the item at once and closes it when the save succeeds.
Park moves an item to the parked group at once.
The item stays visible in All if it matches the other filters.
With Unread only on, the row leaves the view when the user presses Mark read, Ask question, or Tell.
The row returns when the server reports an unread update and the row matches the current filters.
The shared table fades and collapses the row and its open details when an action hides them.
The request starts without waiting for the animation.
The page disables all row buttons while the request runs.
A second press sends no request.
The page applies the saved state and parking status from the response.
The page uses the saved parking condition to update the labels in the row summary and its open detail.
A refusal restores the last confirmed item state, parking status, and unread state together.
The unread state includes the Update sort key.
If Mark read, Ask question, or Tell loses its response, the page restores the unread flag from before the request.
The page opens the restored row and shows the reason there.
The restored row slides back into view.
After a Park refusal, the page can return focus to **Park**.
The document body must have focus.
The button must be visible and enabled.
Each row keeps its own inline failure notice.
The List uses the same rules as Review for notices above the table.
If Remove loses its response, the page restores the confirmed row and reports that the save is unconfirmed.
If a Park retry fails, the List checks the original receipt and reads the current row.
The List shows the shared failure line if the receipt cannot confirm Park.
If the List cannot read the receipt, it restores the confirmed row.
A new action uses a new request ID, even if the previous response did not arrive.
The page keeps unconfirmed request IDs for actions that support another manual submission.
An HTTP 5xx response to Ask question leaves the request unconfirmed.
The page discards each request ID when it receives a success or refusal for that request.
A sent or alerted notice sends no second message.
The one-off `the_list.py clean-evidence --source morty` command removes migration JSON copies and matching List row blocks from Evidence.
It preserves other text and saves each changed record once, with one log entry per copy kind.
The row block requires the record's own number, a List state line, and field bullets.
Only field bullets and their continuation lines belong to the block.
A continuation line starts with at least two spaces or a tab.
A second run changes nothing.
The page can mark an item read, close it, park it, unpark it, or append feedback and delivery states.
The page uses signed forms and rejects invalid fields or tokens with HTTP 400.
The List record saves the request ID, action, and words with each change in one locked write.
The same write saves the pending notice and its recipient for actions that send a notice.
Action messages add box text under `Nate's words:`. An empty box leaves the message text the same.
For Tell, the recipient is the producer named when the page renders the form.
For Remove and ordinary Park or Unpark, the recipient of the existing notice is the record keeper.
Park, Unpark, and Make top priority send words from the box to the producer.
For a Mark read notice, the recipient is the item's producer when the notice saves.
Under the lock, each action checks the request ID against the saved actions.
A retry of a saved action changes no item fields and saves no second notice.
**Make top priority** writes the priority to the staff record.
Each press of **Make top priority** starts a new action.
The List prepares the request ID, recipient, words, and delivery key before it changes priority.
The staff writer saves the request proof and the priority in one write.
The List uses the proof to release the prepared words for delivery.
Recovery uses the same proof after a restart and preserves later priority changes.
If the request did not apply, the staff writer saves a refusal under the same lock.
The refusal prevents a delayed copy of the request from changing priority.
A status check with a missing receipt leaves the result unknown.
An explicit **Retry words** can prepare a missing request under the List lock and ask the staff writer to refuse it.
If the original POST saves before Retry acquires the lock, Retry uses the saved words.
A Retry that finds the List lock busy keeps the request unresolved.
The server reports the final result with the request ID.
The browser retains unresolved requests across reloads.
The browser removes each request after confirmed delivery or a final refusal.
**Retry words** uses the saved recipient, text, and delivery key.
Each retry button shows the saved words and the recipients for that delivery.
The retry leaves the current priority and any newer draft edits in place.
The sender retries pending records and unconfirmed Park words at startup.
Existing notices use their usual delivery and alert rules.
The writer refuses a saved ID with a different action or different words.
After an unconfirmed directory sync, the writer checks the stored record under the same lock.
If the record holds the request, the server reports that it saved the action.
The List sender uses the shared `change_page` retry and alert helpers.
The sender uses the saved recipient's current session on each attempt.
For older feedback without a saved recipient, the sender uses the record keeper.
If the recipient is missing or retired, the words stay saved and delivery follows the same alert path.
A successful send appends the same request ID with the sent state.
Exhausted tries alert the item owner's showrunner.
The request becomes alerted only when at least one showrunner receives the alert.
Failed alerts leave the request pending for repeated POSTs and startup recovery.
An alerted request shows `not sent to <recipient>; <showrunner> told` until the next item update outside the Studio.
The server startup hook retries pending List requests separately from change and question recovery.
A repeated POST retries a pending request without adding another pending log entry.
Sent and alerted requests do not send again.
A process claim prevents concurrent sends of one request without holding the List lock during delivery.
Delivery is at least once; a crash after a send but before its saved result can cause another send.
The Update cell shows a notice only when delivery fails.
The Log keeps the sent record.
The log and retry use the original recipient after reassignment.
The page retains draft words, dates, actions, request IDs, and tokens during a refresh.
An unchanged retry uses its saved request ID and token after a lost response.
After a lost Park or Remove response, the List reads the current item before it restores the row.
It checks the receipt for the exact request, including a Remove that saved without a notice.
Changed feedback uses the fresh signed request from the page.
A successful send clears the saved request ID and only the text that the user sent.
Each successful POST returns a fresh signed request ID for the next action.
A retry retains its original request ID.
The form disables its action controls while the request runs.
An unread update shows a dot and marks the navigation entry as new.
The navigation key includes the record ID and update time.
The page uses `page_refresh_seconds` and the shared refresh helper.
A refresh retains the search text, selection, focus, open item, and filters.
An older response cannot replace a newer response.
Phone layouts show each row as a card.
The List uses the published Review snapshot for rows with all their linked records.
The List requests the counts strip separately after it renders the rows.
The same request reads from disk if the snapshot lacks a linked record.
List action responses also read from disk if the snapshot lacks a linked record.
The List shows unavailable status when it cannot read a linked record.
A failed Review read leaves the counts available.
A refresh keeps the current counts until the next counts request succeeds.
A refresh lets an active counts request finish.
The response updates linked Review status only when the row version matches the request and the response.
If a row starts waiting for linked Review status during a request, the List starts a follow-up when that request ends.
The counts strip uses the status line readers for questions, changes, and rebuilds.
Each count links to its existing page.
The strip omits proposals and efficiencies.
Closed items show no check text in the page or CLI.

#### Asks for nate's Go

An ask names one change that waits for nate's go or approval (List item 232).
A producer opens it with `the_list.py ask` ([The List: `the_list.py`](showrunner-automation.md#the-list-the_listpy)).
An open ask whose target has a staff row puts the item in Needs me, also during an active park.
An open ask whose target has no staff row shows no box and does not hold the item.
`the_list.answerable_asks` is the one rule for both: the page boxes, Needs me, Mark read and the Go use it.
Mark read on an item with an open ask that shows a box clears the dot and keeps the state.
It sends a message to the producer when the words box contains text.
The open row shows one box for each open ask, above the item fields.
The box has a warning border and a heading: “Needs your go” or “Needs your approval”.
It shows What, Who needs it (the target's staff name and role) and Why.
The role comes from the staff row, such as “a unit director of sdlc”, “the merge director of sdlc” or “the List producer”.
The button is Go for a go ask and Approved for an approval ask. A config ask has the same button.
Small muted text next to the button names the target: “Pressing Go sends your go to <agent>.”
For an approval ask, it says “Pressing Approved sends your approval to <agent>.”
The button has `aria-describedby` on that text. The keyboard and a screen reader use it like the other row buttons.
A click sends a signed POST to `/console/list-go` with the item, the ask id, the page address, and the box words.
The Go or Approved message adds those words to the existing message.
The server reports the saved Go before it sends the message.
If delivery fails, the item keeps the draft and offers **Retry Go delivery**.
The retry uses the same Go ID and sends only to recipients whose delivery is pending.
The retry keeps the ask closed and keeps any newer draft edits.
For each click, the server reads natedev's current Tailscale IPv4 and IPv6 addresses.
It refuses a client address that matches either address, is loopback, or belongs to natedev.
A successful socket bind to the client address proves that natedev owns it.
The server returns 403 for these addresses or for an invalid client address.
It returns 503 if a Tailscale read fails or the bind fails with an error other than `EADDRNOTAVAIL`.
A refusal writes no ledger record.
The row shows a refusal in its normal status place, as for the other row actions.
An accepted click does these steps in this order:

1. The server writes one ledger record in `go-ledger/<go id>.json` under its state folder.
   The record has the go id (random), the ask, the item, the target, What, the time, the client address, the page address and `used: false`.
   It also has `sent` and `reported` for each recipient, all false at first.
   `sent` is true when the message reached the recipient. `reported` is true when nate saw that it did not.
   The file lock `go-ledger/.lock` orders each ledger change across processes and threads.
   The server holds it from this record to the end of the List save in step 2.
2. The List closes the ask with the go id and logs “**Go** — nate: <what> → <agent>” (or **Approved**).
   When no ask that shows a box stays open, the item leaves Needs me.
   When the Go leaves no ask that shows a box on an unparked Waiting for nate item with no open question, the item becomes Working.
   The same write logs the state change and saves a next-step notice for the item's producer, as Mark read does.
   When the Go makes the item read, it also logs “Marked read” after the state change, as Mark read does.
   A later unread walk stops there, so a later Go on the same item can make it read again.
   The notice has the request id `<go id>-next-step`, so its delivery key differs from the key of the Go message.
   An item in another state keeps its state.
   The item becomes read only when it has no other unread change, such as an unread answer, an unread delivery or a newer update.
   When the save cannot confirm folder durability, the List reads the item again.
   If the ask holds this go id, the server keeps the record and continues.
   When that read fails, the Go is on disk. The page shows “The Go is saved. It is sent at the next server start.”
   If the save fails, the server reads the item again.
   If an ask holds this go id, the server keeps the record, and the page shows “The Go is saved. It is sent at the next server start.”
   If no ask holds this go id, the server removes the ledger record, and the page shows “The Go could not be saved. Try again.”
   When that read also fails, the record stays and the page shows “The Go could not be saved. Try again.”
   At each server start, the server reads the item of each kept record. It removes the record when no ask holds its go id, and keeps it when the read fails.
3. The server answers the page. The page removes the box and applies the saved state, so a Working row leaves Needs me in place.
4. The server sends the target and morty one message through the List Tell sender.
   The message names What and includes the go id and the check command.
   The message includes any text from the box under `Nate's words:`.
   When the Go set Working, the producer gets one message.
   A producer that is a Go recipient gets the Go message with the next-step line at its end. The notice becomes sent with it.
   Another producer gets the next-step notice alone, through the List Tell sender.
   The server sets `sent` to true for each recipient whose send succeeds or returns `pending`.
   A `pending` result leaves the words unconfirmed for that recipient.
   A failed send alerts the item's showrunners with the title “List Go needs attention”.
   It also adds the log line “Go not delivered to <agent>”, so nate sees it.
   The alert and the log line occur once for each recipient of a Go.

**Retry Go delivery** uses the saved message and delivery key for each unconfirmed recipient.
The retry shows the ask's **What** text beside the saved words and recipients.
When the item opens after a reload, the page reads the saved delivery and shows the retry button.
It also sends to any recipient whose original send failed.
The retry preserves the closed ask and the next-step notice state for each accepted send.

At server start, the server retries each Go with an unsent message or unconfirmed words.
It uses the saved delivery key for each affected recipient.
It removes each record whose item shows no ask with its go id. When the item read fails, the record stays.
It does this before the List Tell recovery, so a resend to the producer carries its next-step line in the same message.
The Go recovery handles its next-step notice when the recipient still has an unsent message or unconfirmed Go words.
Only the Go path sends that notice, with the Go, also when nate presses Go during the start recovery.
When the producer is not a Go recipient, the List Tell recovery sends the notice alone.

`go_check.py <go id>` asks the server with the caller's staff id from `staff.py`.
The server answers `go ok` only when all of these are true:

1. The record exists.
2. The List item has a closed ask with this go id, the same target and the same What.
3. The target is the caller.
4. The record is not used.

Then it marks the record used, under the ledger file lock, so a Go has one use at most.
When that write cannot confirm folder durability, the mark is on disk, and the server answers `go ok`.
A second use, another caller and an unknown id get one refusal line and exit 1.

Risks:

- The `caller` value is what the session says. A session that has a forwarded go id can name the target as the caller.
  morty and the List record both hold the go id. This is in the class of a session that breaks the rules on purpose.
- An agent on the Mac that breaks the rules can still post a Go. The refusal stops only a click from natedev.
- The item log shows nate every Go.
A Go sends immediately.

### The Studio page

`/console/studio.html` shows the List rows with one picture tab for each producer.
The menu shows Studio with the Lucide `clapperboard` icon.
The page title is The Studio.
`list_page.render` provides the rows, filters, script and status line.
The Studio menu entry uses `list_page.waiting_updates` for its New badge.
The first news request moves the saved List seen set to Studio and keeps any unread updates.
The Studio puts the Producer picture tabs on the left and centers the words box vertically on the right.
When space is limited, the words box moves below the tabs and stays on the right.
On a phone, the words box uses the full width.
The Top priority band follows the tabs and the words box.
The Owner and Show rows follow the band.
A render failure gives a 503 page with **The Studio page is not available.** and logs `review-links studio:`.

The tabs are the List Producer filter: `record_table.chip_row('Producer', ...)` with `data-filter-row="producer"`.
The tabs use `staff:<id>` values and the `#producer=<id>` fragment.
All is first. Then each active producer that is not retired follows in name order, also with a count of 0.
A producer that has rows but no active staff row also gets a tab.
Each tab shows a picture with the name and the item count on it. A click filters the rows to that producer.
The counts change with the Owner, Show and words filters.

`studio_avatar_root` in `config/review_links.conf` names the picture folder.
The All tab reads the folder `all`. A producer tab reads the folder named by its staff ID.
A picture has three files: `NN-64.webp`, `NN-128.webp` and `NN-256.webp`.
Each render lists each folder and picks one picture number at random for each tab. The page does not read `index.json`.
The `<img>` uses the 128 file as `src` and all three files in `srcset`.
A missing root, a missing folder, or a folder with no complete picture shows the initial of the name in a circle.
A page refresh copies the `src` and `srcset` of each `img[data-keep-src]` into the new content.
So a picture changes only when the page loads.

`GET /console/studio/avatars/<folder>/<NN>-<size>.webp` serves one picture.
The folder is `all` or a 36-character staff ID. The size is 64, 128 or 256.
The request gets the Tailscale, host and origin checks of a Review attachment request, and a failure gets 403.
Any other name, a link below the root, a missing file, or a file over 64 KB gets 404.
A picture goes out as `image/webp` with `Cache-Control: max-age=86400`.

On the Studio, `data-list-path` on `[data-list]` sets the path for row actions and item-state reads to `/console/studio.html`.
The Studio uses the shared HTML gzip response.
An old List page request redirects to the Studio with the original query.
The browser keeps the item fragment.
The console translates `page=list` and `at.list` to `page=studio` and `at.studio` with the same query and fragment.
When `page=list`, the console opens a valid `at.list` place first.
If that place is empty or invalid, the console opens a valid `at.studio` place.
If neither place is valid, the console shows the open Studio frame or opens the Studio home.
The news endpoint treats `open=list` from an old tab as `open=studio`.
Review and message links to List items open the Studio.
The old List path accepts the same POST actions, item-state reads and count reads as the Studio path.
Both paths use the same Tailscale, origin and form-token checks.
See [Undo after Send](#undo-after-send) for the actions that use the send hold.

Each producer has one top priority item: the `top_item` List number in its staff row.
`list_page.top_priority_mark` reads the mark at render time.
An item is the top item when it is open, its producer is an active producer row, and that row names its number.
A Closed or missing item reads as no top item, and the page writes nothing when an item closes.
A flag before the title marks the top item in its row, also on the All tab.
Each other open item of an active producer has the button **Make top priority** in its panel. An item of a retired or unknown producer shows neither the flag nor the button.
`list_page.top_button` gives the button to both panel forms: beside Remove, or beside Mark read and Unpark when the item waits for an answer.
The button posts the action `top`. `the_list.make_top_priority` names the item as the top item of its producer, so it takes the place of the old one.
The List path refuses `top`. The refresh after the save moves the flag and the band.

The band appears on a producer tab and stays hidden on All.
It shows the Top priority label with the flag, a link with the item number and title, the next step, the state, and the ETA.
Click the link or press Enter while it has focus to open the item and save its place in the address.
The link uses the same filter rules as other item links to show an item outside the current results.
The title keeps its usual color and weight and uses the same hover and focus style as other item links.
A refresh keeps focus on the same top item link. If that link is gone, focus moves to Search.
A producer with no top item shows **No top priority item** and **Open an item and click "Make top priority".**
The page renders one band for each top item and one band for no top item.
A `:has()` rule for each band shows the band of the pressed Producer tab, so a tab click makes no request.
`CONSOLE_DOCUMENTS` maps the Studio path to `studio` for screenshot capture.
The Studio header and New item dialog use `page.studio.owner`.
The List records stay in the store selected by `page.list.owner`.
The Studio uses the List status age limit and has no `status.studio` key.

### Images page

`/console/images.html` shows registered images with their instructions, oldest first.
A new image goes to the end, so the cards above it keep their places.
The console lists it as **Images**, with the Lucide `image` icon.
A showrunner or unit uses one command to register an image or a pair.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py register (--image PATH [--two-halves] | --before PATH --after PATH) --title "TITLE" [--look-for "POINT"]... --owner STAFF_ID (--change ID | --phase ID) [--list-item L<n>]
```

- The title says what the image shows. Give one `--look-for` for each point the user needs to check.
- `max_chars.image_title` in `config/review_links.conf` sets the title limit. The default is 80 characters.
- `scripts/review_links/reply_shot.py <markdown> <columns> <png>` renders a Markdown reply in the Claude Code terminal.
  It resumes a placeholder transcript in Xvfb and xterm, so no model call happens.
  It cuts the shot at the first blank band of `reply_shot_gap_rows` pixel rows, so the page shows readable text.
  `config/review_links.conf` sets the wait, the terminal rows and the band height.
- The owner is the staff id that made the image. The command refuses an id that is not in the staff record.
- The command accepts `.png`, `.jpg`, `.jpeg`, `.gif` and `.webp` files.
- Use one nonempty line for the title and one for each point.
- Use `--two-halves` for a single file with two halves other than before and after.
- The command runs the fast check and the model review before it copies the image.
  The model receives the title, the look-for points, and the image layout.
- Use `--list-item L<n>` to link the picture to a List item.
  Registration searches the current and old List folders.
  The picture record stores the List id and its keeper id.
- It copies the image to `<review root>/images/<12-hex id>/image<suffix>` with a private `.review-link-image.json` record.
  The record stores `title` and the `look_for` list. It also stores the full text in `caption` for older services.
  The page uses the full `caption` as the title if the record has no `title`.
  A pair copies `before<suffix>` and `after<suffix>` to one folder with one record and one id.
  The record keeps the after image in `file` and the before image in `before`.
  It builds the folder under a private draft name, then renames it, so the page never shows a partial image.
- It prints the image file links (before, then after, for a pair) and the image's own console link:
  `http://<host>:<server_port>/console/?page=images&at.images=%2Fconsole%2Fimages.html%23image%3D<id>`.
  `console.place_query` builds the query from the console's place key.
- The server serves the copy, so the user never needs the source path.

A card with an image shows these parts in order.

1. The full title in normal weight at the List title size. The title wraps when it exceeds the card width.
2. The optional points as bullets in the secondary text color.
3. The details with aligned labels for the owner, subject, and time.
4. The image link. A pair shows both images under **Before** and **After** labels.
5. The **Feedback** field. `feedback_rows` in `config/review_links.conf` sets its height.
6. The **Approved**, **Send Feedback**, and **Ask question** buttons.

A plain click on a single image or either pair image opens the shared image zoom.
The zoom centers the full image over a dark backdrop within the page view.
It fits the view with a 16px margin and never exceeds the image's natural size.
When the image meets the action area, its height also clears the copy, and the image stays centered.
Escape or a click on the backdrop or image closes it.
In a pair zoom, a badge above the image says Before or After.
The left arrow key shows the before image and the right arrow key shows the after image.
A left and a right arrow button at the sides of the view do the same. A button is dim and off at the end of the pair.
A hover or keyboard focus on a button that is on shows a tip, such as "Press ← to show Before". On a Mac the tip names ◀ or ▶. A screen without hover shows no tip, so a tap only changes the picture.
A single image has no badge, no arrow buttons, and ignores the arrow keys. An arrow key with a modifier keeps the browser action.
The page keeps its scroll position and returns focus to the image link that opened the zoom.
Shift, Ctrl, Meta and Alt clicks retain the browser's link action.
Middle clicks and the right-click menu retain the browser's link action.
`page_frame.ZOOM_STYLE` and `ZOOM_SCRIPT` supply one zoom through `change_page.document`.
One document click listener handles `data-image-zoom` links, including links from later page updates.
The modal dialog takes focus and uses the image's alt text as its accessible name.

**Owner** is `<name> (<kind>)`, or `Unknown session: <id>`. **For** is `Change <id>` or `Phase <id>`.
**Added** is `MMM dd, yyyy, HH:MM` on the 24-hour clock in the user's zone (`config/user_zone.conf`).
The Images page shows every waiting image, oldest first, with no age filter.
A pair shares one title, bullet list, details block, Feedback field, and set of answer buttons.
The owner can delete both images with one command.
On a page 1108px or wider, a single card is 680px wide while the group panel is 300px or narrower, and a pair card is up to 1376px wide; both are centered in the images pane.
When a pair card is narrower than 600px, its images stack, before on top.
A page URL with `#image=<id>` opens at that card.

A group is a run of shown cards in a row with the same owner.
A panel at the top left of the images pane lists one row per group, in three columns: owner name, count and the time of its first image.
The owner name has no kind, such as `(unit)`.
The panel has a rounded outline and thin lines between its rows and columns.
Above 900px each panel column is as wide as its widest cell, so no name or time wraps; a wide panel can stack a pair below 1108px.
The panel stays in place while the cards scroll.
Each row shows a hover color, and a click on a row goes to the first image of its group.
On a narrow screen the panel goes above the cards and scrolls with them, and its owner column takes the spare width.
When no images wait for an answer, the page shows **No images registered.**
The page refreshes its cards when the console shows it again and when the tab becomes visible.
A hidden frame makes no request.
When a refresh brings a newer page script, the shared `reloadForNewScript` loads the page again in place of the swap.
An older script does not bind the new card forms, so a form would post outside the script.

Each answer button posts the image id, the button's answer, the feedback text and a token signed with the console browser token.
The server checks the token.
**Approved** needs no text. **Send Feedback** refuses empty text with **Enter feedback first.** and keeps the card.
Feedback always means that the image is not approved, so the page has no separate **Not Approved** button.
A card loaded before that change can still post `not approved`; the server refuses it with **Not Approved was removed. Use Send Feedback.**
The card leaves the page when the shared Undo toast appears.
**Undo** restores the card and focuses the feedback field with the typed words.
The server saves no answer for an undone send.
While a send runs, a refresh checks only the script version.
An image leaves the page while each open question has a live sender or confirmed acceptance.
The page also hides an image while the server recovers its verified delivery on startup.
A failed or unconfirmed delivery returns the card.
Only a confirmed failure shows the shared question failure line.
When the owner answers the last open question, the card returns and the console shows a new update.
The first line names the answer: `From the user (via the Images page): ` and then `your image is approved.` or `your image is not approved. The feedback follows.`
The Images command checks replies to questions before it takes the image write lock.
The writer checks that the question is still open.

The server sends the answer to the owner's staff id.
For a linked picture, the server saves the verdict and the user's words in the List log before it sends.
The same List write sets Next step to `<producer name>: act on nate's answer to picture <id>`.
It marks the item read and keeps open List questions.
It closes open asks that name the picture id and keeps other asks open.
An answer changes Waiting for nate to Working.
Feedback changes Delivered to Working. Approval leaves Delivered for the producer to close.
The answer clears a Waiting for nate ETA unless it approves a Delivered item.
The server sends the answer to the item's saved producer and the picture owner.
One staff id receives one message if it serves both roles.
The server resolves each recipient to its current session at each try.
The send uses `delivery_tries` and `delivery_pause_seconds` from `config/review_links.conf`.
An answer message gives the verdict and any feedback.
For a current picture it also gives the image links, the title, the look-for points, the subject, and the delete command.
The question message also includes the title and the look-for points.
For a removed picture it gives the picture id.
The message includes the List id for a linked picture.

A private answer record outside the picture folder saves the words, delivery key and confirmed recipients.
A retry uses the saved delivery key and sends only to recipients that have no confirmation.
The live picture gets `feedback_at` after all recipients confirm the send.
The page then hides the card unless a new question answer needs review.
Delete saves the picture reference, title, and look-for points before it removes the picture folder.
The server deletes the folder under `images/.review-link-images.lock`.
An open card can still send an answer after deletion.
The page shows **This picture was removed; your words went to <owner name>.**
If a save or delivery fails, the server answers 503 and names the step or recipient.
On reload, each pending answer for a removed picture shows its saved title and look-for points.
It also shows the saved words, **Not sent yet**, and a **Retry** button.
Older records without a saved title show the picture id.
The page also shows Retry if the picture folder remains after its record is removed.
The retry card leaves the page when delivery is confirmed.
The browser keeps editable drafts separate from saved answer attempts.
Each edit saves the draft in the tab.
Before each submit, the browser saves the draft and the action and round for an unchanged answer.
A reload restores the drafts, including drafts for removed pictures.
If draft storage fails, the page keeps the drafts in memory and blocks automatic reloads.
While one request is active for a picture, its submit buttons are disabled.
The buttons remain disabled until response handling and card removal finish.
Other pictures can accept a request during this time.
A confirmed send or Retry clears its matching draft if the draft has the same revision.
A refresh keeps each Retry error with its saved attempt.
The linked answer stays in the List log if delivery fails or the owner deletes the image.
A timeout shows **No answer in N s. The save can still finish.**
An agent deletes an image with this command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id ID
```

### Placement disk POC page

`/console/placement-disk.html` shows the placement disk proof of concept from Hanadocs.
It is the last navigation entry, with the label `Placement disk POC` and the `torus` icon.
The server reads the HTML file named by `placement_disk.file` in `config/console.conf` on each request.
It does not copy the file, so an edit to the file shows after a page refresh.
The server adds the page owner control at the first `<h1>` after `<main>`.
The panel adds the frozen top height to its top padding when it scrolls a control into view.
When the file is missing or unreadable, or the `~` path names an unknown user, the server returns 503 with `The Placement disk POC page is not available.`
The page loads three.js from cdnjs. Served page documents send no Content-Security-Policy, so the load works in the console frame.

### Rebuilds page

`/console/rebuilds.html` lists machines that require a rebuild and shows commit details.
The status line names each waiting rebuild in the `For you:` row.
The same row shows the count of other open actions or `Unavailable` when the count fails.
Each render checks whether the rebuild cache needs a refresh.
The page combines the cached system comparison with open rebuild needs and completed requests.
The server serves it only in a folder that holds the browser manifest.
It has no navigation entry; `?page=rebuilds` opens it.
A `data-rebuilds` block carries `data-count`, the number of machines, and `data-label`, the link label.
The page shows one block per machine, with natedev first, then the Mac.
Each machine block shows open request reasons and groups them by requester.
The affected session appears when a request has no requester.
The Commits section lists subjects for commits without an open request.
The section lists commits on the configured ref after the verified running revision.
`rebuild_row.read_changes()` reads them from `<rebuild.cache>-changes.json`.
The status line refresh writes that file.
The commit author time appears beside the subject.
The `Claude-Session` line identifies the author through the transcript for that Remote Control session.
Each section groups entries by owner.
The owner with the newest entry appears first.
Entries with the same owner and displayed time share one time.
Names come from the current staff record as `<name> (<kind>)`.
An unknown session appears as `Unknown session: <id>`; a commit without a known session shows `Unknown session`.
The time shows in the user's zone as `Nov 15, 2026, 08:45` (24-hour), in gray text that keeps at least 4.5:1 contrast.
A listed machine without a recorded change list shows `The change list is not available yet.`
When no machine needs a rebuild, the page shows `No rebuilds needed.`, with a count of 0 and an empty label.
Completed requests remain available below the waiting machines.
They appear once per machine and commit, newest completion first.
The page names each damaged archive and shows the valid records.

Machine headings use 20px text; owner headings use 17px; the changes use 15px.
The content column is up to 1060px wide.
The machine blocks sit side by side when each block has at least 420px, with 48px between them; otherwise they stack with 32px between them.
At 480px or less, each time sits above its reasons.
Controls wrap on narrow screens, keep a minimum height of 44px, and show keyboard focus in both color themes.

### Answering a change review

`change_page.py` publishes one page with every waiting change from every configured showrunner list.
Run this command without a row count or batch size:

```sh
scripts/lib/py scripts/review_links/change_page.py
```

The page finds list paths through `change_review.lists_directory()` and the existing filename prefix.
Each GET reads each list once through `change_review.review_snapshot`.
The snapshot, pending-row reader, and saved-answer reader use one table reader.
This reader checks each table row against its header's cell count.
It refuses a malformed row instead of silently losing cells.
The browser receives every waiting row in that response.
It shows one row at a time.
The page resolves production documents from the update timers, staff records, and the list's production references.
A list with no available production document retains its rows with a notice and no answer controls.
The page never uses a review list as a production document.

Tabs show each showrunner and its waiting count.
`scripts/production/change_routing.py` owns the routing rule for the page, the status line, and `/next`.
Each row stores one fixed staff id in the `Producer` cell.
The cell stores no name.
The router reads the name and parent ids from the staff record at use.
A unit producer belongs to its current staff showrunner.
A showrunner producer belongs to that showrunner.
An empty Producer cell belongs to the list's showrunner.
A rename or a removed worktree does not change the producer id.
An unknown id or a broken parent chain belongs to `unrouted`.
The page shows its note under the change.
The router prints the note and change id to stderr.
A verdict on an unrouted row saves without sending a message or recording a delivery failure.
An unrouted row offers no question control.
The server refuses its question submissions without saving.
The Production cell supplies no tab or message recipient.
The router alerts the configured showrunner about each new unrouted row and reason only within the real change-review folder.
The list's resolved parent must equal `change_routing.configured_lists_directory().resolve()`.
The `lists_directory` setting names this folder.
`CHANGE_REVIEW_DIR` is a test override for the reader folder.
This override never makes a list real.
Lists outside this folder print the routing note without sending an alert or writing an alert record.
It alerts only from the production staff record; a `STAFF_FILE` override never alerts.
`unrouted_alert_recipient` starts at `natedev` in `config/review_links.conf`.
A routed change goes to this root showrunner when its list has no staff showrunner or its production document is gone.
An unknown producer id or a broken parent chain keeps the change unrouted.
An unrouted change uses the list's document when available.
Otherwise, it takes only the root showrunner's document and keeps its owner.
The page, the verdict and the footer counts use the same owner.
The router keeps an available document, including a document that an earlier verdict added to the list.
Otherwise, the router uses the root showrunner's current document.
If neither document is available, the page shows the unavailable message.
If the root showrunner is unavailable, the router keeps the original owner.
The router reads the root setting once when it first needs this rule.
`unrouted_alert_retry_seconds` starts at 60 and accepts 5 through 3600 seconds.
The router reads both keys when it uses them.
A private record stores the last attempt time and accepted alert for each row and reason.
A nonblocking exclusive lock prevents concurrent renders from sending the same alert twice.
The router marks the alert only after the sender accepts or queues it.
A failed alert prints one stderr line and remains pending until the configured pause ends.
An alert configuration or storage failure does not stop routing.
A new reason permits one new alert.
A crash between acceptance and the durable mark can repeat an alert.
Routed rows and rows that need no user answer send no routing alert.

The status count watches the routing configuration, staff record, review lists, and delivery records.
The owner's staff document supplies the production document when the file exists.
The supplied production document for that showrunner is the fallback.
A missing document reference in the source list does not block a row with an available owner document.
An unrouted row retains verdict controls when its list's staff showrunner has an available document.
In that case, its owner remains unrouted and receives no message.

`change_review.list_ready`, which `merge_checkpoint` and `merge_gate` call, stores each checkpoint unit's Producer id.
A row covers a text-matched change when its merge contains that checkpoint or its merge is unknown.
An edited row covers a change from the same merge only when its Producer matches the checkpoint unit's fixed staff id.
Within each producer, pair ready changes first, in commit order.
Pair held changes next only when all uncovered held changes for that producer wait on one step.
An empty Producer cell or an unknown checkpoint producer permits only text matching.
`merge_gate` reads the staff record before the merge.
`merge_checkpoint` reads the staff record before the push.
An unreadable staff record stops either run before anything is pushed.
`merge_checkpoint` also undoes its unpushed local merge.
One read supplies the rows for each run.
Each writer matches the branch to a unit staff row through its worktree branch.
An exact Branch name wins over a Plan, Unit, or worktree alias.
The staff lookup prefers present worktrees, then the latest row within each worktree state.
An unregistered unit writes an empty cell and one named note.
`change_review.py add --repo <directory>` stores the unit id of that worktree.
A showrunner directory or an unowned directory stores an empty cell.
A verdict retains the Producer cell in Reviewed.
Delivery records store fixed staff ids.
Each send reads `staff.snapshot()` and sends to `session:<current_session>` from the saved staff row.
An account move changes the next recipient address without changing the stored staff id.
The status line counts the same staff groups and shows a separate `unrouted` item.
The `unrouted` count covers all page lists across independent production roots.
The page and status line use the same waiting rule.
Saved verdicts, validation requests and open questions leave the user count and queue as soon as they save.
The page, footer, and `/next` use this rule regardless of delivery results.
Review shows one row per record and keeps one card open.
The type, owner and state controls select the visible rows.
Queue keys and prerequisites retain their list-based IDs.

The record's `Kind` column remains the only source for the kind.
Set it with `change_review.py set-kind --production <showrunner name> --row C27 --kind 'See in action'`.
Use **See in action** for a change to something the user runs or sees.
Use **Read only** for other changes.
Both kinds appear.
An older row without Kind shows no kind line.
The page shows every row with its recorded kind.

The Review address is `/console/review.html`.
A link can name a row by its key or by the ID the row shows.
Each Change card shows its check text and recorded proof as plain paragraphs that keep their line breaks.
A showrunner Answer on a Change or Investigation card, and a Reply on a Question card, show code blocks with a copy control.
A code block is fenced code or a line that holds only one backtick span.
Other backtick spans stay inline code.
The **Your words** box is `review_words_rows` lines high; the setting starts at 4.
The box accepts line breaks for every outcome.
The server converts CRLF, CR, and Unicode line separators to newline characters.
The verdict cell joins stripped, nonblank lines with one space.
The message and a question keep the normalized line breaks.
Works with text saves `works: <text>`.
Does not work with text saves `not works: <text>`.
Send feedback sends the words and transfers the change to the showrunner.
Each verdict saves the text from **Your words** as its comment.
The existing `change_review.lands` function decides whether the verdict lands or returns the row.

**Ask showrunner to check** sends the showrunner the row ID and **the user asks you to validate this**.
The message includes any feedback from the form.
This answer writes no verdict and runs no feature-log command.
The row leaves the view when the request saves.
The review list retains the row until the showrunner saves a verdict.
A direct link shows **With the showrunner for validation** in the card details.
The delivery record preserves this state across page loads and server restarts.
The page calls `next_item.move_to_end` to move the row to the end of the showrunner's own queue.
The CLI keeps its current form and still moves the pinned item.
An unknown key raises `NextError` before the queue changes.
Rows from an earlier list use their qualified keys.

**Ask question** uses the **Your words** field.
The server refuses empty questions and questions that contain only whitespace.
It saves no verdict and runs no feature-log command.
The server stores the question before it sends one message to the row's showrunner.
The message names the row, list, production, change, and question.
It includes a separate `Question id: <id>` line.
The saved row leaves the view without opening another card.
An open question survives page loads and server restarts.
A failed HTTP save keeps the typed words and restores the form.
A direct link shows the open question without verdict buttons.

The owner answers with `scripts/review_links/change_page.py answer-question --item <Review key> --question <id> --text "<answer>"`.
The command refuses a row without an open question.
The command refuses an old or unknown question ID.
The same ID and answer text change nothing and exit 0 on a rerun.
The same ID with different answer text is refused.
The row returns to Open after the answer saves.
The page shows the question and answer below the change, with the usual answer buttons.
Question and Answer show text lines in paragraphs and use the same list forms as How to check.
The answer keeps its internal line breaks and strips only whitespace at its two ends.
All text is escaped, including text in list items.
The user can ask another question or save a verdict.

Send feedback uses the existing sender and stores `kind: feedback`.
The message includes the feedback ID and the command to record the action.
The recorded action returns the change to Open.

Each answer uses a background `fetch` POST.
After the save, the row leaves the view without opening another card.
A failed save keeps the row and typed text and shows **Not saved: <reason>**.
A save that ends by its time limit shows **No answer in N s. The save can still finish.** in place of that line.

The fixed `console/` folder holds the all-list browser's private `.review-link-browser.json` and no HTML file.
The first `change_page.page_url` call creates the folder and the manifest with a hard link, so two first requests keep one secret.
Later calls keep the same manifest, so a page stays valid across merges and restarts.
This manifest contains a random page secret and an optional initial row key.
It stores no copied rows or answers.
Each prepared row receives a token signed from that secret and its qualified list/row key.
The server accepts only a row with that token and a row that remains in a configured list.
The request cannot select a local path, command, or recipient.
The server never serves private manifests.
Each form contains only row, token, outcome, and feedback.
Each browser form names its page as the save URL.
Unknown rows, extra fields, wrong tokens, POST queries, and invalid outcomes receive 4xx.
Path fields, command fields, and non-ASCII tokens also receive 4xx.

Older selected-row links still work with their existing plain HTML forms and 303 responses.
Their `.review-link-changes.json` manifest keeps its immutable row allowlist.
The selected-row publisher still accepts `--production <showrunner name> [--list LIST] C27`.
It requires a valid kind, description, and proof for each selected row.
The normal command uses the all-list page instead.

The server accepts writes only when its bound address equals the current `tailscale ip -4` address.
A different bind or a failed address lookup refuses the write.
The server resolves each row's Production staff name across the root production's complete hierarchy.
A child page can therefore answer sibling rows on the shared root list.
An unknown production is refused before the verdict saves.

The server follows `CloseChangeReview` in `commands/showrunner/produce.md` before it saves a new verdict.
It selects projects through `ProjectSpec.closes_by_change_review` in `scripts/feature_log/feature_log.py`.
`feature_log.project_of` identifies the owning production's repository from its origin.
There is no second project policy list.
Projects outside that policy skip the feature-log operation.
A row with no proposal also skips the operation and saves the user's answer.
The server calls `feature_log.land_review` and `feature_log.drop_review` with `if_proposed=True`.
The writer checks proposal absence under its existing lock on the host that owns the feature log.
On the Mac, each function forwards the matching `feature_log.py` command to natedev through SSH and captures its output.
An absent proposal returns `skipped: no proposal for <row>` and is not an error.
Malformed proposals, incompatible states, and SSH failures raise `FeatureLogError`.
The server makes no separate proposal lookup.
A Hana list uses a bare review ID, such as `C27`.
Other feature-log review IDs retain their filename's showrunner name, such as `natedev/C27`.
These stored IDs stay valid when the displayed ID names another responsible showrunner.
A landing answer calls `land_review`, which lands the proposal with the verb that the proposal records (`add` or `supersede`).
A returning answer calls `drop_review` with the saved verdict.
An operation failure keeps the row open and sends the showrunner the row ID and the `FeatureLogError.diagnostic` text.
The server sends one alert per list, row, and error text.
It saves sent alerts under `.review-link-closeout-alert-*.json` in the snapshot root.
The delivery lock serializes the alert check, send, and record write.
Repeated saves and server restarts retain this record.
A different error or row can send another alert.
The server does not repeat the feature-log operation for a saved answer.

The server then calls `change_review.save_verdict` with paths from the configured list and production records.
It returns `VerdictSaved` with the output lines, or `VerdictRefused` with the refusal text.
`save_verdict` serves the page and the `change_review.py verdict` command.
Single and batch verdicts share the same stale-ID check.
Both paths take the delivery lock before the list lock.
They check the current exchange and save while the delivery lock remains held.
Batch verdicts use the validation message's ID in the `validation` field.
A refused batch exchange leaves every list unchanged.
Both paths retain their conflict checks, scratch cleanup and list commit behavior.
Classified rows retain Kind, How to check, and Production in Reviewed.
Older rows without Kind retain their existing Reviewed table behavior.
An identical saved answer changes nothing and sends nothing; a different saved answer is refused.

The feature-log and verdict calls run in the server process on the request's HTTP worker thread.
They start no child Python process.
They change no cwd, environment, `sys.argv`, stream or process umask, so other request threads are safe beside them.
Each call takes its own `flock` on a new open file.
`end_row_scratch` can still start `folder_audit.py` as a child process when a row's Cargo checkout has a build folder outside it.

The all-list page returns JSON with status 200 after `change_review.save_verdict` writes the answer.
Older selected-row pages retain their 303 response.
The server checks whether the feature proposal can accept the verdict before it saves.
The pending intent records the feature proposal and the reason for replacing a wait.
The server syncs the list directory and commits the verdict before it closes the feature proposal.
If the sync or commit fails, recovery confirms both before it closes the proposal.
Recovery retries temporary closeout failures.
A permanent conflict alerts the row's showrunner once and ends the closeout.
Recovery uses the saved reason to write the same production log entry as the request.
The request thread flushes the response before it runs the existing `scripts/message/send.py` sender.
The server starts no detached shell for message delivery.
The all-list page sends to the row's current staff showrunner.
Older selected-row pages use the same staff route.
The saved verdict selects recipients when a new message attempt begins.
An exact `works` verdict goes only to the showrunner.
**Send feedback** and **Ask question** on an open change send the words to the showrunner.
The change moves to Waiting.
When one staff unit owns the change, every other verdict also goes to that unit.
The Producer cell identifies that unit through its fixed staff id.
An empty Producer cell belongs to the list's staff showrunner.
Retries retain stored staff recipient ids.
Older records without recipient ids compute the same recipients as a fresh verdict send.
No agent polls for answers.
`DeliveryStatus` owns one private delivery record per review list and row under the configured review root.
`scripts/production/delivery_state.py` owns its typed states and decoder.
Both the page and waiting-count readers use this decoder.
The record has separate validation and verdict slots, each with its latest attempt ID and state.
Its question slot holds the latest question and its answer in the same record.
`NoQuestion`, `OpenQuestion`, and `AnsweredQuestion` state who must act next.
Each new question gets a `question_id` from `secrets.token_hex(4)`.
An open question stores `question_id`, `text`, and `delivery`.
The delivery contains its attempt ID and state or `null` after delivery.
An answered question stores `question_id`, `text`, and `answer`.
The command replaces the open question with the answered question under the delivery lock.
Repeated question saves with the same text never write or send again while that question is open.
A fresh question after an answer gets a new ID.
Before the verdict write, the server stores a pending intent with a new attempt ID.
If this intent write fails, the server refuses the verdict write.
The record stores staff recipient IDs without page context.
The server reads the owner and its production document at use through `StaffRouting.owner_and_production`.
`DeliveryIntent` models a pending send; it has no failed state.
`ValidationRequested` keeps validation ownership and feedback after delivery or an alert.
Only a showrunner answer returns an open question to the user.
The page shows the recorded verdict or the current state in the card.
The shared question renderer shows “Question not delivered.” after a confirmed failure.
The page has no Resend or Retry buttons.

The server reads `config/review_links.conf` through `scripts/lib/conf.py` when delivery starts.
Each page render reads `feedback_rows` once to set every feedback box's height.
`delivery_tries` starts at 3; `delivery_pause_seconds` starts at 5.
Tests use a zero pause.
Each round sends once to every recipient that has not succeeded.
The server pauses once between rounds.
For questions, it retries failed or unconfirmed sends.
The showrunner receives its first attempt before any recipient receives a second attempt.
Review uses the record and saved attempt to identify each message.
Retries and recovery use the same key for each staff recipient.
List feedback uses its saved request ID.
Image feedback uses the image registration, action, and feedback to identify the attempt.
The server saves the answered question IDs before the first send.
A retry hides the image only if no new question answer has arrived.
A skipped send can save the feedback mark after a failed write.
`QUEUED` and `KEPT` count as kept.
`SKIPPED` confirms that the sender already accepted the key.
For a Review send, `PENDING` means the message may have arrived.
Codex queue retries reuse the same client message ID.
A timeout before the queue request reaches the socket is a definite failure.
The server stops delivery for that recipient.
After all tries fail, the server alerts the showrunner who owns the item's tab.
For a Review producer tab without a showrunner, the server uses the configured routing alert showrunner.
For a List question, the item owner determines the alert route.
If the showrunner is the failed recipient, the server records the failure without sending an alert.
The alert names the list, row, recipient, number of tries, and last error.
It states that the user's answer is saved and requests a manual handoff.
Each saved alert has its own key.
Recovery reuses the alert key after an unconfirmed send or a failed clear.
The server sends saved alerts outside the delivery lock.
It takes the lock to clear an alert after confirmed acceptance or final failure.
The user receives no delivery alert.

At startup, the server delivers pending records with the same retry and alert rules.
New item saves a first-message marker before it writes the native record.
A separately selected assignee receives a copy of the saved words.
The server saves the copy before it claims the first message.
For a Scheduled draft, the List saves the copy with the item before the server sends the assignment notices.
Send and Retry attempt the saved copy even when an assignment notice needs a retry.
The copy omits the main recipient's reply command.
Retry sends only copies whose delivery is unconfirmed.
For a List Send, the server saves the List route before it writes the native record.
The route stores the unit ID. The List write reads the current unit name from the staff record.
Pending routes with only a unit name retain the name saved with the route.
The server holds the recipient locks while it reserves a List number and saves the linked row.
It releases the recipient locks before it takes the delivery lock.
Recovery reuses the reserved number if the process stops during the List write or before the notice claim.
If the saved producer retires before a native List report files, recovery keeps its purpose, route, and reservation.
If a readable staff record has no row for the saved producer, recovery skips the List row and clears its reservation.
Recovery keeps the saved note in Review and sends its notice.
For a Review item with a separate List row, recovery skips the row if its producer retires and sends the notice.
Recovery logs the reason for the skip.
If the staff authority is temporarily unavailable, the server leaves the List write and notice pending.
Recovery still clears the first-message markers for items that already have an answer.
The delivery claim reads the native owner under the same lock as wrap.
The server clears the marker after the claim saves.
Startup recovers a marked message only when the native record exists.
Wrap saves both content digests in the move journal before it publishes the destination.
The journal also preserves the browser token digest and the deferred capture flag.
If the destination still matches its digest, startup recovery moves the current source.
If only the destination changed, startup recovery keeps that copy.
If both copies changed or a digest is missing, recovery reports the refusal and keeps both copies and the journal.
The completed move retains the original delivery path if the filing receipt is missing.
Recovery preserves attempts that a live request owns.
It resumes a List or Images question if the saved sender released its lock during delivery.
It reuses the question's message ID.
Recovery scans each review list once and finds each record through `change_routing.delivery_path`.
It ignores the `page` field in legacy records.
The browser and recovery use `StaffRouting.owner_and_production` for the same owner and production document.
The `Production:` line names the showrunner in live and recovered messages.
It never delivers a verdict without a saved verdict in the review list.
Legacy failed records decode as nothing to deliver; they produce no send or page controls.
A legacy failed validation keeps validation ownership without a message attempt.
The server retains the attempt if both the Change question and its alert fail.
It records acceptance of the question or its alert before it clears the attempt.
Recovery clears an accepted attempt without sending again.
Recovery retries a started Change question without an acceptance record.
A stop between acceptance and the receipt write can cause a second send.
Review confirms that the question was saved before it sends the message.
A validation keeps its ownership slot and recipient IDs with state `sent`.
An open question keeps its question ID and text; an answered question keeps its answer.
The server deletes the record when every slot is empty.
One stable `fcntl.flock` protects attempt creation and conditional completion.
Sends run outside the lock.
An older attempt cannot clear or change a newer attempt's record.
Each server guards active attempts against duplicate sends.
A request owns its attempt from creation through completion.
Startup recovery skips that request's record under the same lock.
Recovery clears an unfinished verdict intent if the previous process did not save the verdict.
The pending question and its reply ID stay in the delivery record until the verdict saves.

A failed POST keeps its 4xx or 503 status.
The all-list page returns a short JSON reason and keeps the typed answer in the browser.
The browser shows **Not saved: <reason>** and keeps the answer form for another submission.
The server writes full errors only to stderr and the existing showrunner failure message.
Older selected-row forms retain their styled failure page and typed answer.
A fallback form needs only the page manifest and notice when row or queue reads fail.
An unknown page, invalid content type, or refused bind shows one short sentence in the same style.

Legacy browser forms use their prepared queue for Skip for now and store no second queue on the server.
Ask showrunner to check calls the existing queue writer before the response.
One stable lock in `urgency.py` protects pin, selection, and move operations across all queues.
Each operation opens the lock separately and retains atomic file replacement.
A GET never publishes or advances a page.
Older selected-row pages retain their server Skip for now form for compatibility.
The sender's repeat rules also apply when the server recovers a pending message.
The review record remains the only verdict store.

Restart each running review-links service once when this code check reaches deployment.
No NixOS or service configuration change is necessary.

### Server

`serve.py [--port PORT]` reads its default port from `server_port`.
The setting starts at 8765.
`--port` overrides the setting for one server run.

Use `--list-fixture FOLDER` to serve a private List from an existing folder.
The page owner button on the practice List opens the shared dialog with Send disabled.
The server reads List records from `FOLDER/records` and staff records from `FOLDER/staff.json`.
Open `/console/studio.html` on the fixture port.
Remove uses the normal List save path in the fixture store.
The fixture reads dialog recipients from the private staff file.
Remove is the only action that the fixture accepts.
The fixture omits counts from other pages.
It skips message delivery, recovery, snapshot sweeps, and code restarts.
The server checks the fixture folder before it checks the Tailscale address.
Fixture requests use the scratch directory lock during file access.
It exits with status 0 when the folder disappears.

The rest of this section describes the normal server and its initial config values.
The server reads the current settings when it needs them.

The shared check covers POST requests, New item token requests, Go token requests, Review attachment requests, and Studio picture requests.
These requests require a single `Host` header for the server's bound address and port.
The server also accepts the published host on the same port if its process receives the same `REVIEW_LINKS_HOST` override.
On port 80, the server also accepts either host without a port.
If the request supplies `Origin`, it must match the HTTP origin for that `Host`.
A Go change (`POST /console/tune`) must supply `Origin` and the token.
The server returns 403 when the host or origin check fails.

The Builds Kill action uses `GET /console/build-kill` for current handles and stop results.
`POST /console/build-kill` requires the matching Origin and request token.
The server limits the form to `kill.request_bytes`, initially 4096 bytes.
Both requests require the Tailscale login in `operator_login`.
The default admits the user ID that owns the untagged local Tailscale node.
The server checks the socket peer with Tailscale WhoIs.
The Builds queue verifies the detached command and its saved run slice before it offers Kill.
The first read of the detached run record determines acceptance.
The server requires `completion_after_slice_cleanup` to offer or accept Kill.
The supervisor sets that field when it publishes its process identity.
A finished or missing record on that read gives **Run already ended**.
The server saves accepted requests under `.review-link-build-kill` in its private state.
The live supervisor finishes the run record.
When the supervisor is gone, the server asks the normal reaper to check the selected record.
The reaper finishes that record when the slice is empty and recovers its pending completion.
The server saves **Stopped** when the record confirms the stop and the saved slice is empty.
It then sends the keyed notice.
A change in the detached record identity ends recovery with **Stop unconfirmed**.
After restart, the server restores saved results and resumes pending actions.
The server retries pending actions in its existing maintenance cycle.
The Builds page reads these results at `page_refresh_seconds` intervals through its existing refresh cycle.

- At start, the server runs `tailscale ip -4` (10 s timeout) and parses the output as IPv4. It creates the root if it is missing and runs the sweep. Then it binds a `ThreadingHTTPServer` to that address and port, and prints `review-links: serving <root> on <address>:<port>`. A failure at start, for example no address or a failed bind, prints one `review-links: <error>` line to stderr, and the server exits 1.
- The server repeats the address check after `tailscale_check_seconds`, which starts at 60 seconds.
  The server uses the monotonic clock. A failed lookup prints `tailscale address gone (was <address>)`. A different address prints `tailscale address changed (was <old>, now <new>)`. In both cases the server exits 1, and the service manager starts it again on the current address. If Tailscale stops but still reports the same address, the server keeps running, because its socket stays bound to that address.
- The server checks loaded module files under its own checkout after `code_check_seconds`, which starts at five seconds.
  It uses a monotonic deadline and reads the interval only when the check is due.
  It compares each file's modification time in nanoseconds and size.
  A file modified at or after the process start counts as changed, even before its first stamp.
  An older module loaded later joins the stamps at the next check without a restart.
  A changed or unreadable file requires a restart.
  The server checks live delivery attempts under the delivery lock.
  It defers the restart while an attempt is live and keeps the old stamps for the next check.
  When no attempt is live, an exception leaves the serve loop and the listening socket stays open.
  The server prints `review-links: code changed: <path>` and calls `os.execve(sys.executable, sys.orig_argv, …)`.
  It passes the listening socket's descriptor in `REVIEW_LINKS_LISTEN_FD`.
  The new process keeps the pid, arguments and environment.
  It takes that socket when the socket listens on the same address and port, and binds a new one otherwise.
  The kernel holds connections that arrive during the restart, and the new process answers them, so no request is refused.
  A failed process replacement or new start exits 1, and the service manager starts the service again.
- The server percent-decodes the path and removes the query. A path with a `..` segment gets 404. The server resolves the path under the root and follows symlinks. The result must be a regular file under the root. Folders and targets outside the root get 404. Index names, the `.review-link-` prefix, and atomic temporary names get 404. The server never lists a folder.
- `/console/` is rendered at each request, as [Hana console](#hana-console) states. Older console, browser and rebuild links get a 303 to the fixed address.
- The server renders a published `changes.html` with a private manifest from the review record. A published console page, such as `memory_live.html`, gets HTTP 200 with no validator. For other regular files, it sends `ETag` and `Last-Modified` with HTTP 200 or 304. A matching `If-None-Match` gives HTTP 304. HTTP 200 also includes `Content-Type`, `Content-Length` and `X-Content-Type-Options: nosniff`.
- Content types: `.html` and `.htm`, in any case, go out as `text/html; charset=utf-8`. A type that Python's `mimetypes` lists under `image/` keeps that type. All other files, `.md`, `.txt`, `.json` and `.pdf` included, go out as `text/plain; charset=utf-8`.
- `POST /console/list-go` takes a List Go. It passes the same host and origin check as the other POST requests.
  It writes the ledger record in `<state folder>/go-ledger/` before it closes the ask; see [Asks for nate's Go](#asks-for-nates-go).
- `GET /console/go-check?id=<go id>&caller=<staff id>` answers `go_check.py` with JSON `ok` and `text`.
  It gives 200 and marks the record used for the first check by the target, and 403 for each refusal.
- Each request writes the standard access line to stderr.

### Undo after Send

The following actions wait so the user can undo them.

1. Review items hold all answers, Later, and Validate.
2. The Studio holds Tell, Ask question, both Park choices, Unpark, Mark read, and Remove.
3. Images holds Approved, Send Feedback, and Ask question.
4. The New item dialog waits on each console page.
5. The change browser holds each answer and the question Skip action.

The browser marks the send with `X-Undo-Hold: 1` and sets `X-Undo-Request` to a new request ID.
If the browser loses the response, a retry of the same action uses the same request ID.
The browser allows POST retries for two minutes after the first POST. After two minutes, it shows a failure.
The server uses this ID for the hold. A repeated POST returns the same hold and the remaining Undo time.
The server saves the body, with the Content-Type, Origin, Host and Accept headers, in `<root>/.review-link-holds/<id>.json`.
Only the owner can read the folder and its files. The server answers 202 with `X-Send-Hold: held`.
The 202 body gives the hold ID and the remaining Undo time in `seconds`.
The server allows a one-second grace period for late Undo requests.
After a visible Undo window, the browser waits for the remaining grace period before it reads the result.
If the reply gives zero Undo time, the browser reads the result at once and allows for any remaining grace in the deadline.
The result reads and retry delays share the request timeout from the page or dialog settings.
Nothing is saved and nothing is sent before the time ends.
When `undo_seconds` is zero, the server does not hold the send and handles it at once.

The toast says `Sent to <name>.` and shows an **Undo** button and a bar.
If the page has no receiver name, the toast says "Sending."
For an action that saves without a message, the toast says "Saved."
Later, Validate, Park, Unpark, Mark read, Remove, and Skip also use "Saved."
The existing sender delivers any notice after the hold ends.
The toast uses the recipient name shown when the user presses **Send**.
For **Tell**, the toast uses the signed producer.
If a draft can recover an earlier filing, the toast says "Sending."
The toast appears at the bottom right of the top window.
It sits 24 px from the bottom and right edges.
At 560 px or less, the right gutter is 16 px.
At 640 px or less, the stack is at most 280 px wide.
Stacked toasts share the right edge. Long names wrap.
The stack scrolls when it reaches the space reserved for the frozen header and a full card button.
The stack keeps room for a full toast and its Undo button within the viewport.
The page adds bottom scroll space and scroll padding from the top of the stack to the bottom of the page frame.
A linked Review card reduces its height by the same amount.
The card body scrolls when it has enough space between the header and footer.
The full card scrolls when the body cannot fit between them.
Closing a sending frame removes its toast.
The page removes the scroll space from frames that close or when no toast is visible.
The toast uses the console colors.
The toast moves to the top open modal dialog, or to the page, so no dialog covers it or makes it inert.
The toast stays clear for the first half of the time and then fades out. The bar becomes shorter over the full time.
Images, Review, and List hide the item while the Undo toast is available.
**Undo** posts `/send-hold/<id>/undo`. The server replaces the saved request with an undone mark and answers 200.
A repeated Undo, after a lost answer, also gets 200.
The page then shows "Undone. Edit and send again." and keeps the draft.
**Undo** restores the item. If the item is still open, **Undo** focuses its editable draft field.
A failed send restores the item and its draft with the failure message.
The New item dialog and the linked Review card reopen with the draft.
You can open a new draft or another linked card while a send waits.
The List keeps a separate recovery slot for each unresolved draft in the browser tab.
If the server confirms a linked card save and its response is unreadable, the List shows the refresh error.
A successful List refresh clears the error.
A change browser answer goes back in the list with an **Open it** button.
**Undo** restores the Review or List row in the current sort order.
Undo after Unpark restores the row to Parked.
Held Unpark hides a linked parked row during the hold. Undo restores the row and its link.
Undo returns to a skipped question if the user stays at the next question.
The page tracks the request from the first POST until it receives a held result or confirms Undo.
The action POST uses `keepalive` for bodies of 64 KiB or less. Larger bodies use a regular POST.
If the keepalive POST rejects before a response, the page retries once without `keepalive` and uses the same request ID.
The page also keeps the request ID after a direct 5xx response to the POST.
The page stops retrying the same hold after two minutes.
The page keeps this limit for a missing result unless the action has its own saved hold ID.
When the console page or the card frame closes, the page removes the toast and posts `/send-hold/<id>/send`.
The server sends the request at once. If the release arrives first, the server records it until the POST arrives.
A failed Undo leaves release on close active.

When the grace period ends, the server sends the saved request to its own bound address.
The normal handler then saves and sends, as for a send that does not wait.
If delivery fails after a confirmed save, the toast shows the delivery failure. The action stays saved.
An Undo after release gets 409.
The toast waits for the result, then shows `Too late to undo. Sent to <name>.`, the save text, or the failure.
The browser retries an Undo once if it loses the connection.
A failed Undo says "Undo failed. The send can still leave."
The toast still reads the result and shows any delivery failure.
The page reads `GET /send-hold/<id>`, with the same Tailscale and origin checks as the New item options.
The server answers 202 with `X-Send-Hold: pending` until the send ends.
It then gives the saved response, so each page shows its normal result.
A repeated POST to a released hold gives zero Undo time. The next read returns the cached result.
A retry after a held save returns a failure starts a new hold with the full Undo time.
The automatic Park retry keeps the signed form and starts a new hold after it receives a failed save result.
A read that loses its connection tries again each second. After 30 failed reads, the page shows the error.
A failed connection to itself gives a 503 result.
Before the replay, the server writes a 503 result: "The send may have left. Check before you send again."
A server that stops during a replay keeps that result, so it never sends the request two times.
A handler that runs longer than 5 minutes gives the same result, because it can still finish.

Each saved request is a file, so a server restart sends each request it finds when its time ends.
A code change restarts the server only when no replay runs and no hold request waits for its answer.
After that check, no replay starts, and a new held send gets 503: "The console is restarting. Send again."
A hold file that the server cannot read gets a stderr line, and the sweep deletes it.
The sweep also deletes undone marks after 10 minutes, and other files in the folder after 10 minutes.
If the server cannot write before a replay, it deletes the saved request and keeps a failed result in memory.
If the server cannot write the result after a replay, the page reads that result from memory.
After a restart, that page reads "may have left".
If the server cannot write the undone mark, the Undo answers 503 and the send still leaves.
A page can read a result again until the sweep deletes it, 10 minutes after the send.
A held New item with current authorization uses the new token after a server restart.
A New item hold that needs authorization waits for a valid token.
After a lost response, the browser can submit the new token with the same hold ID.
A received stale token result ends the browser hold.
New item keeps the draft ID across holds and retries unconfirmed delivery for the same filed item.
The `.review-link-` prefix keeps the folder private: a GET of a saved request gets 404.
An undo needs the console host and origin, as other POST requests do.

### Sweep

`sweep_snapshots(root: Path, now: float) -> int` deletes expired snapshot folders and returns the number deleted.
The server runs it before the bind and after each `snapshot_sweep_seconds` interval.
That interval starts at one hour.

- `snapshot_retention_seconds` sets the expiration age and starts at seven days.
  Each publish resets the folder modification time.
  The sweep deletes expired folders at its next run.
  A stopped server sweeps at its next start.
- The sweep never deletes the fixed `console/` folder or the `images/` folder, at any age. Only a delete removes an image.
- At the root, the sweep checks `is_symlink()` before `is_dir()`, so it never follows a symlink. It skips symlinks and entries that are not folders, for example `.index.json`, `.index.lock` and temporary files.
- A root folder is a flat snapshot when its name is six lowercase letters or digits and it holds no subfolder and no symlink. The sweep deletes a flat snapshot as one folder.
- Any other root folder is a session folder of the older `<session>/<UTC stamp>-<label>-<unique suffix>/` layout. The sweep checks each child folder on its own, skips child symlinks and never deletes the session folder.
- An `OSError` on the root listing, on an entry check or on a delete prints `review-links sweep: <path>: <error>` to stderr, and the sweep goes on to the next entry. A folder that is gone before its delete is skipped with no line. The hourly call has one more `OSError` catch around the whole sweep.
- The sweep does not take the index lock and does not change the index. The next publish drops the entries of deleted folders.

### Services

Both machines import `/etc/nixos/modules/common/review-links.nix`. It defines the job `nate.jobs.review-links` with `keepAlive = true` and the script `exec "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/serve.py"`. The job runs the script from the `~/.claude` checkout, not from a Nix store copy, with the user's `PATH`, which has `python3` and `tailscale`. The backends `/etc/nixos/modules/linux/nate-backend.nix` (systemd) and `/etc/nixos/modules/darwin/nate-backend.nix` (launchd) turn the job into the service on each machine. The services set no `REVIEW_LINKS_ROOT` and no `REVIEW_LINKS_HOST`.

- natedev: the systemd user unit `review-links`. The backend sets `Type=simple`, `Restart=always` and `WantedBy=default.target`. `modules/linux/review-links.nix` adds `RestartSec=10s`, `StartLimitIntervalSec=0` and the firewall line `networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8765 ];`. Logs go to the user journal.
- Mac: the launchd user agent `org.nixos.review-links`. The backend makes `launchd.user.agents.review-links` with `KeepAlive = true` and `RunAtLoad = true`. It sets no `Label`, so the nix-darwin prefix `org.nixos` names the agent. There is no darwin-only module. The default launchd `ThrottleInterval` of 10 s is the restart gap. stdout and stderr go to `~/Library/Logs/nate-jobs/review-links.log`.

## Invariants

- Ordinary snapshot content changes only when the same file set is published again without `--new`. Change pages read the change review records at request time. Their publisher writes only a private manifest and placeholder into the snapshot folder; the sweep deletes whole folders.
- A new ID never takes an existing folder. The exclusive `mkdir` refuses a taken ID.
- Each output and the index change by one `os.replace` of a complete temporary file in the same folder. A reader gets the old bytes or the new bytes, never part of a file.
- Every index read, ID pick, snapshot write, cleanup and index write happens under the one `flock` on `<root>/.index.lock`.
- A refused request writes nothing. The input checks and the host lookup run before the publisher creates the root or takes the lock. The one exception is the 10-tries ID refusal: it can leave the root and `.index.lock`, but no snapshot and no index change.
- New snapshot folders hold only regular files. The flat-or-legacy test of the sweep depends on this.
- `atomic_file` names temporary files `.<target>.<pid>.<random>.tmp`. Its `temporary_target` function identifies these names. The server hides every atomic temporary file, including temporary files for public outputs. The publisher refuses snapshot inputs with these names, index names, or the `.review-link-` prefix. For example, `.draft.txt.123.456.tmp` is a reserved snapshot input name.
- The server binds only the IPv4 address from `tailscale ip -4`. `--port` changes only the port. The server exits 1 when that address is gone or changes, and it never serves on an old address.
- The server serves regular files under the resolved root and the selected rows of published change pages, and never lists a folder. It never serves index files, page manifests or unfinished writes, including names in a different letter case.
- One failed sweep entry never stops the bind, the server or the sweep of other entries. The sweep never follows a symlink.
- The configured sweep deletes expired snapshots.
  A session end deletes nothing.
- The skill gives a link only after `curl` on the same machine returns `200`, and only for the review-links service, never for a hand-run server.

## Gotchas

- **Test port.** The publisher uses `server_port`.
  A server with another `--port` is not at the printed link.
- **Mac case-insensitive filesystem.** The publisher compares output names after `str.casefold`, which is full Unicode case folding. `Report.txt` and `report.txt` collide. `Straße.txt` and `STRASSE.txt` collide. `Review.md`, with its `Review.html`, collides with `review.HTML`. This stops one output from overwriting another on the Mac.
- **Private filenames.** `private_name` uses case folding before it checks names. It blocks index names, the `.review-link-` prefix, and atomic temporary names. The server checks names before and after it resolves symlinks. These checks hide page manifests and unfinished writes on both machines.
- **Symlink aliases.** The key uses the resolved path and the published name. A symlink with a different name gets its own ID and its own copy, and the original link stays. A path through a symlinked folder to a file with the same name reuses the ID.
- **The legacy `manual/` folder.** `manual` is six lowercase letters, so it matches the ID pattern. A new snapshot never holds a subfolder or a symlink. So a six-character folder that holds one is a legacy session, and the sweep checks its children one by one. A `manual/` that holds only a symlink keeps it. Empty legacy session folders stay.
- **Links between Markdown files.** A `.md` output goes out as `text/plain`, so a link to `other.md` opens the raw text. Link to `other.html` for the rendered page.
- **Raw HTML.** GitHub renders raw HTML such as `<details>`, and this page shows it as text. Keep a draft for GitHub in plain Markdown, so it looks the same in both places.
- **PDF and other binary files** go out as `text/plain; charset=utf-8` with `nosniff`, so the browser shows them as text.
- **`--new`.** Later runs without `--new` refresh the new folder. The old folder keeps its content until the sweep deletes it, and no run can refresh it again.
- **Atomic per file, not per bundle.** During a refresh of several files, a reader can see one new file next to one old file.
- **A bad index stops all publishing.** If `.index.json` is not valid JSON or is not a JSON object, every run exits 2 until someone repairs or removes the file.
- **`REVIEW_LINKS_ROOT` in a session.** A session that sets it publishes to a folder that the service does not serve, and its links return 404. The `curl` check finds this.
- **Code changes restart the server.** The next scheduled code check detects changes to loaded modules under the checkout and replaces the server process.
  Other publisher changes take effect at the next publisher run.
  These changes need no rebuild.
- **Sweep and refresh at the same moment.** The sweep takes no lock. A refresh of the folder that the sweep deletes at that moment can fail, or can print a link that returns 404. The `curl` check finds this, and a second run gets a new ID.

## Why

- **An IPv4 literal, not a MagicDNS name.** On natedev, `/etc/hosts` maps the short name `natedev` to local addresses, for example `127.0.0.2`, where the server does not listen. So a `curl` check or a browser on natedev fails with that name. A bare `natedev` URL also failed in the user's browser on the Mac, although the Mac resolver returned the Tailscale address of natedev. The literal is the address that the server binds.
- **A short URL.** The URL must not wrap in a terminal. The target is 60 characters. The IPv4 address of each machine has 13 characters, so a 20-character file name gives 53 characters, and a test checks the limit. The full name `natedev.tail21e383.ts.net` gives 65 characters. Percent-encoding makes names longer: a space becomes `%20`, and each non-ASCII letter becomes 6 or more characters.
- **Plain HTTP on the tailnet, no `tailscale serve`.** The server binds only the Tailscale address, so only tailnet devices can reach it. One user service per machine needs no change to the Tailscale configuration. HTTPS, `tailscale serve` and access from outside the tailnet are out of scope.
- **`marked` in the browser.** natedev has no `pandoc` and no Python `markdown` module, so the publisher does not render Markdown itself. The escaped `<pre>` keeps the page readable with no network.
- **Snapshots and change pages.** Ordinary files are copied into the served root and stay after their publishing session ends. Change pages instead show only the review rows authorized by their private manifests. Both expire through the configured sweep.
- **Refresh in place.** A second run on the same file set updates the same link, so the user refreshes the page that is already open to see an edit. `--new` keeps the old content when a separate link is necessary.
- **Exit 1 on an address change.** A socket stays bound to one address, and only a new process can bind a new address. Exit 1 makes the service manager start the server again on the current address.
- **`StartLimitIntervalSec=0` and `RestartSec=10s`.** When Tailscale is down at login, the server exits 1 at once. A user unit cannot order itself after the system's `tailscaled.service`, so the restart every 10 s is the wait. The systemd defaults, a 100 ms restart delay and a limit of five starts in ten seconds, would stop the unit for good before the tailnet returns.
- **The firewall line.** Port 8765 on the Tailscale address of natedev answered the Mac with no firewall rule for it. The active `firewall-start` script trusts only `lo`, so the probable path is the `ts-input` chain of tailscaled. Nobody has read that chain, because it needs root (`iptables -S ts-input`). The rule states the intent and keeps the port open if that chain changes.
- **The script from `~/.claude`.** Script updates reach both machines with no rebuild.
- **The symlink check comes first in the sweep.** A legacy session can hold only links to targets that the sweep cannot read. The sweep checks for a link first, so it does not follow the link or fail on it.

## Service checks

natedev:

```sh
systemctl --user status review-links
journalctl --user -u review-links -n 30
systemctl --user restart review-links
```

The journal shows the start line `review-links: serving <root> on <address>:8765` and one access line per request. It shows a `review-links sweep:` line for each entry that the sweep skips on an error, and a `review-links: tailscale address ...` line before each exit on an address loss.

Mac (from natedev, put each command in `ssh mac '...'`; the single quotes make `$(id -u)` run on the Mac):

```sh
launchctl print gui/$(id -u)/org.nixos.review-links
launchctl kickstart -k gui/$(id -u)/org.nixos.review-links
tail -n 30 ~/Library/Logs/nate-jobs/review-links.log
```

Before you give a link, fetch it on the machine that printed it:

```sh
curl -s -o /dev/null -w '%{http_code}' '<URL>'
```

Give the link only if the result is `200`. To see the page title too, run `curl -s '<URL>' | grep -o '<title>[^<]*</title>'`. To check from the other machine, run the same `curl` there, for example from natedev: `ssh mac "curl -s -o /dev/null -w '%{http_code}' '<URL>'"`. If a link fails, compare the URL host with `tailscale ip -4`, and check that the service is active.


## Configuration

### Code changes

`ReviewLinksServer.service_actions` checks loaded checkout modules through `loaded_module_stamps`.
`code_check_seconds` in `config/review_links.conf` sets the interval, initially five seconds.
Each stamp holds `st_mtime_ns` and `st_size`.
An unreadable file, a file modified at or after process start, or a changed stamp raises `CodeChanged`.
The server prints the changed path, closes its socket, and calls `os.execv(sys.executable, sys.orig_argv)`.
The replacement keeps the pid and environment.
A failed replacement exits for the service manager to restart.
An active verdict delivery postpones the code check without replacing the saved stamps.
The next check can therefore detect the same change.
No merge-time restart command is needed.
The verdict code in `change_review`, `feature_log` and `atomic_file` is loaded into the server, so a change to it also replaces the server process.

### Settings

The scripts read `config/review_links.conf` through `scripts/lib/conf.py` when each operation starts.
`server_port` selects the server port and the port in published URLs.
`tailscale_timeout_seconds` limits address commands.
`snapshot_retention_seconds` controls snapshot age.
`snapshot_sweep_seconds` and `tailscale_check_seconds` control the server checks.
`snapshot_allocation_tries` limits folder allocation attempts.
`delivery_tries` and `delivery_pause_seconds` control message attempts.
`feedback_rows` sets the textarea height from 1 to 20 rows and starts at 5.
`undo_seconds` sets how long a send waits for Undo, from 0 to 60 seconds, and starts at 10. Zero turns Undo off. Each send reads it.
`review_words_rows` sets the height of the Review page **Your words** box from 1 to 20 rows and starts at 4.
The List and Review pages read the parking settings at each render.
`later_soon_minutes` is 1 to 1440 minutes and starts at 60.
`later_morning` is an HH:MM time from 00:00 to 23:59 in the user zone. It starts at 09:00.
`later_week_day` is a full English weekday name with an initial capital. It starts at Monday.
`later_week_time` uses the same format as `later_morning` and starts at 09:00.
`park_time_step_minutes` is 1 to 60 minutes and starts at 15.
`unrouted_alert_recipient` selects the alert recipient and the root showrunner for changes that nobody can take.
This owner rule applies to routed changes when the list has no staff showrunner or its production document is gone.
An unknown producer id or a broken parent chain keeps the change unrouted.
If the list has no document, the unrouted change takes only the root showrunner's document.
If no document is available for the change or the root showrunner, the page shows the unavailable message.
`unrouted_alert_retry_seconds` controls repeated alerts.

The routing scripts read `config/change_review.conf` when they need a folder.
`lists_directory` selects the default change review folder.
`review_links_directory` selects the default delivery folder.
`CHANGE_REVIEW_DIR` overrides the reader folder for tests.
Each publication uses one delivery root for the HTML and its manifest.
Each router resolves the configured alert folder once, when an unsaved unrouted row needs it.
The status line watches `change_review.conf` and `review_links.conf` so folder and root showrunner changes invalidate its counts.
The alert guard uses only the configured folder.
`REVIEW_LINKS_ROOT` overrides the delivery folder.
The page, the footer and `/next` use these same folder readers.

`placement_disk.file` in `config/console.conf` names the Placement disk POC file; see [Placement disk POC page](#placement-disk-poc-page).
A `~` at the start of the path expands to the home folder.
`studio_avatar_root` in `config/review_links.conf` names the Studio picture folder; see [The Studio page](#the-studio-page).
It must be an absolute path or start with `~`.


Legacy Changes and Questions forms and the Images page share one motion helper.
The page reads `card_fade_out_ms` and `card_fade_in_ms` when it renders.
A different Changes or Questions card uses both durations, with a 4 px upward move during the fade in.
A new switch completes the active switch first.
The first load and an update that keeps the same card do not fade.
A new card above the viewport scrolls into view smoothly.
Image links scroll smoothly to their image, then reveal it with a fade in and a 4 px upward move.
Other image cards stay visible.
An image fades out before removal. The cards below close the gap over the fade in duration.
Reduced motion disables fades and uses instant scrolling.

Question items and change rows show a Lucide copy button inside the top right of each code block.
Both Changes views recognize fenced code blocks and lines that contain only one backtick span.
This applies to How to check, Proof, Question, Feedback, and Answer.
The shared renderer emits each code block with its button.
Plain text uses `<p>` blocks without a box or a copy button.
The paragraph style preserves line breaks, including a two-line check without code.
The renderer removes blank lines at each text part's edges.
Code blocks and paragraphs use the same 16px margin.
Inline code keeps its current style.
The button copies the code text without the button or its label.
The button shows a check for two seconds after success. Its label stays the same.
A second click starts the two seconds again.
A failed copy keeps the copy icon and shows “Copy failed” for two seconds.
The shared script uses the clipboard API when available.
Plain HTTP pages use a temporary textarea and `execCommand('copy')` when the clipboard API is absent.
The fallback restores focus and the previous selection.
One document listener also handles code blocks from page updates.

### Builds queue moves

The Builds page requests a token with `GET /console/builds/queue-move`.
The token request, Move requests and Undo requests use the shared `builds_operator(peer)` check.
The check uses the TCP peer and rejects tagged nodes.
The `operator_login` setting in `config/review_links.conf` names the operator.
Its value is a Tailscale login name, or `local-node-owner` for the owner of the untagged local Tailscale node.
The setting starts at `local-node-owner`.
An empty value refuses each of these requests with HTTP 403.
The console also checks the Tailscale address and Host.
The JSON POST requires the same Origin and a console token.
The console limits the request size and time with `build_queue.conf`.

A drop and an Undo each send a request ID, a wait ID, and an order revision.
A drop also sends a head destination or a neighbor and side.
An Undo sends the original move ID in addition to the common fields.
The admission service restores the anchor it saved for that move.
The console and the admission socket reject fields outside the request type.
The admission service commits the move on its loop and replays the reply for a repeated request ID.
The console returns HTTP 409 with the current order when it detects a conflict.
