# Review links

Review links let a session on natedev or on the Mac show files to the user in a browser. The session copies the named files into a snapshot folder and prints one short URL per file. The user clicks the URL in a terminal, often an ssh session from the Mac into natedev, and the file opens in a browser on the Mac. The URL holds the Tailscale IPv4 address of the machine that has the files, so no file goes from one machine to the other. Markdown files open as rendered pages.

## How it works

| File | Role |
|---|---|
| `commands/review_link.md` | The `review_link` skill. It runs the publisher, checks each link with `curl` and gives the links. |
| `scripts/review_links/review_link.py` | The publisher. It copies files into a snapshot folder, renders Markdown and prints the URLs. |
| `scripts/review_links/serve.py` | The server. It serves the snapshot root on the Tailscale address and deletes old snapshots. |
| `scripts/review_links/test_review_link.py`, `test_serve.py` | unittest suites. The publisher tests use a fake `tailscale` on `PATH` and a temporary home. The server tests bind `127.0.0.1` on port 0. |
| `/etc/nixos/modules/common/review-links.nix` | The shared job `nate.jobs.review-links` for both machines. |
| `/etc/nixos/modules/linux/review-links.nix` | The natedev restart settings and the firewall line. |

Run the tests from the repository root with `~/.claude/scripts/lib/py -m unittest discover -s scripts/review_links -p 'test_*.py'`. They need Python 3.12 or later.

The flow:

1. The skill runs `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--new]` on the machine that has the files. `scripts/lib/py` finds Python 3.12 or later. Inline programs import from the shim’s repository root through `PYTHONPATH` and `-P`. File scripts keep their existing import path behavior.
2. The publisher checks every input, gets the host, writes the snapshot under the index lock and prints one URL per file.
3. The skill fetches each URL with `curl` on the same machine. It gives only the links that return `200`, as clickable Markdown links that show the file name.
4. The browser on the Mac gets the file from the server on that machine, over the tailnet.

### Publisher

`review_link.py FILE... [--new] [--name LABEL]`

- `--new` makes a separate link for the file set. Without it, a run on a known set refreshes its link.
- Older callers still pass `--name`. The publisher accepts it and ignores it.
- On success it prints one URL per input, in argument order, and exits 0. On an error it prints `review_link: <error>` to stderr and exits 2.

The URL form is `http://<host>:<port>/<id>/<file name>`, with the name percent-encoded.
The `server_port` setting starts at 8765.
For example, a rendered link is `http://100.103.14.30:8765/k3x9qa/herdr-issue-draft.html`.
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

Staff names use the current role colors from the Accounts reader.
`staff_names.StaffNames` shares one staff snapshot and one color reading across each server render.
It resolves fixed IDs, session IDs, and unambiguous staff names.
The shared renderer escapes unknown names and free text without a role color.
The List colors Owner, Producer, supporting unit names, staff names in log sources, and showrunner names in the count strip.
List delivery notices color morty and the recorded showrunner through the same page renderer.
They keep the notice words and time.
Review colors Owner, Source, staff log names, and owner menu options.
Images colors card owners and copies those colored names into the group links.
Legacy question pages color each source and its parent separately.
Branch pages color unit names. Rebuilds colors recorded requesters.
Builds shares the same rendered name spans in each current data reading.
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
Empty target role labels use the recorded holder. Only the holder name can have a role color.
Disks uses the same spans for slot holders, sessions, and shared tooltip fields.
Current Builds rows carry their name parts through `slotStep` to the Disks slot label.
Its existing Builds refresh updates the name spans without a new timer.
AWS and the console navigation show no staff name fields.
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

The left navigation has icons and labels for Accounts, AWS, Builds, Disks, Images, List and Review, in alphabetical order.
The desktop has no top bar: each page shows its title once, as its own heading.
Every page uses `page_frame.STYLE`.
Each page centers its main column in the console frame, with a maximum width of 1124px.
A page whose `main` has the `page-wide` class uses the full pane width instead; the Builds page is the only one.
The title starts 32px from the top and left edges of that column.
Each navigation page places `page_frame.title()` directly above its content.
The title, status line and content use the same centered column on all seven pages.
Cards and form controls keep their page styles.
Every page uses the shared background and text colors.
The status line has the same background as the page.
The frame reserves scrollbar space, so short and long pages keep the same column position.
The shared `.page-status` line follows the title.
It shows the state, data time, round trip and server render time, in that order.
Live uses `radio` and `--room`; Reading uses `loader-circle` and `--accent`.
Late uses `clock-alert` and `--warning`; Failed uses `triangle-alert` and `--error`.
Paused uses `circle-pause` and `--ink-3`.
A hidden page or a replacement hold shows Paused before the other states.
Otherwise, the state order is Failed, Late, a busy Paused state, Reading, Live.
Late, Failed and Paused show a reason after the last data time instead of timings.
Builds and Disks keep the shown data time when a later read or measure fails.
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
Accounts and List read their rendered page at `page_refresh_seconds` intervals.
Review checks the snapshot version at that interval.
It requests the compact index only when the version changes.
Builds reads `memory.json` at its feed interval.
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
The status shows Failed with **The measure did not finish.** or the read error.
If a read remains pending past the age limit, the status shows Late with the retained data age.
AWS reads its published page when shown; its hourly publisher sets the data time and render time.
Images reads its rendered page when shown.
Review retains typed text across refresh and holds replacement during an active save.

The title uses bold 26px text.
At 560px or less, the edge space is 24px at the top and 16px at the sides.
Accounts, AWS, Builds, Disks, Images, List and Review put the title and status line in the same place.
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
The entries are Accounts, AWS, Builds, Disks, Images, List and Review.
The configuration has no Changes, Investigations or Questions entries or age limits.
The console renders every configured entry without a skip rule for Review.
The URL value has the form `python:<module>:<function>`.
The function takes no arguments and returns a URL path under the review root.
The console also accepts a full URL from the configured review origin and converts it to a path.
The URL function raises one of `review_link.SNAPSHOT_ERRORS` when the page is unavailable.
An absent function also makes the entry unavailable.
That entry remains visible and shows **Not available** when selected.
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

The bottom-left `For you` button shows the global action count.
The collapsed rail keeps an icon and badge.
The phone header keeps a compact button.
The nonmodal panel shows two action entries with machine names, copy buttons, and card links.
The panel footer opens Review with the reserved parameters `type=for-you` and `show=open`.
The foryou-page phase adds the For you filter and card expansion.
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
The status line separates session `Review:` counts from the global `For you:` action count.
The Mac requests `/console/for-you.json?view=count` to read the count without command text.
The `for_you.host` setting in `console.conf` names the Tailscale peer that serves this count.
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
They are `list-checks`, `message-circle-question`, `hammer`, `cloud`, `image`, `refresh-cw`, `sparkle`, `x`, `panel-left-close`, `panel-left-open`, and `menu`.
The Accounts page also uses `chevron-down` and `minus` from the same set.
That folder includes the version, source, and ISC license.
The console inlines the icons and makes no CDN, font, script, or stylesheet request.

### Review page

`/console/review.html` replaces the Changes, Questions and Investigations navigation pages.
The old addresses redirect to Review with the corresponding type filter.
The redirect retains the owner, filters, sort and open item.
Legacy `row` fields become `item` fields.
`not-read` and `answer ready` select Open.
Old Investigations links with `show=open` or `show=all` select All.
`read` selects Done.
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
`investigation.py file` requires `--waiting-why`. The Python `followup` API stores the follow-up words as the reason.
`proposal_issue.record_review` and `efficiency_reviews.record` require `waiting_why` when setting the Waiting mark.
`candidates.py outcome E<n> deferred --waiting-why "<reason>"` stores the reason beside the ledger.
The states are **Open**, **Validating**, **Waiting** and **Done**.
Changes use Validating while the showrunner has the request.
Questions, investigations, proposals and efficiencies use Waiting while they await a response or action.
Later hides an item without changing its state.
Validating changes remain visible in Validating and All.
The count readers and `/next` use this shared reader.
Open items count in the status line.
An open investigation and a deferred efficiency item do not count.
Questions receive a stable, shared `Q<n>` ID in their registration records.
The native writer assigns the ID under the question owner's lock.
The snapshot uses the file name for a legacy question until a native write assigns its ID.
Ordinary reminders serve the owner's session; Review excludes them from items and counts.
Questions without an owner remain visible under Owner unavailable.
These questions have the state Open and retain their previous count totals.
They have no answer control until an owner becomes available.

The page shows Questions, Changes, Investigations, Proposals and Efficiencies.
Proposals show `<owner>/P<n>` and efficiencies show `<owner>/E<n>`, where the owner is the production's showrunner.
The table columns are Id, Type, Owner, Item, State and Age.
One row can open its card in place.
`record_table.py` supplies the table styles, filters, sorting and open-row controls for Review and The List.
When a Review action removes a card, the shared table keeps a copy of the row and its open details in place.
The copy fades and collapses in 200 ms, and the rows below slide up during that time.
The Review page can restore a pending card after the user scrolls one viewport.
The redraw that restores this card waits for all row animations to end.
When Later hides the last card for an owner, the Owner filter resets after the fade ends.
A save can change the card's sort values. The copy keeps its previous values until the fade ends.
If the request fails, the row slides back into view and the current card stays selected.
If the fade is still in progress, the return starts at the copy's current height and opacity.
The animation updates when the user opens or closes a card during the transition.
A successful save adds no notice above the table.
The status region above the table shows a failure only for the most recent submission.
It shows that failure when the row or its details are hidden.
The next save attempt or reload clears this notice.
Filter and sort changes apply at once.
The reduced motion preference disables row animations.
The shared `record-id` class keeps each ID and its expand arrow on one line.
Each row becomes a card at phone width.
The Type, Show and Owner rows use native buttons with a count and a pressed state.
The Owner row shows owners with the selected type and words in any Show state.
It hides the other Owner chips.
Each Owner count uses the selected type, Show and words across all sources.
An owner without that type and words returns to All.
An owner with matching rows in another Show stays selected with a zero count.
All stays first and counts every matching row.
An unknown owner returns to All and clears both owner and source-owner constraints.
Owner names resolve against all known owners before the type and Show filters apply.
The address uses the resulting selection.
Type and Show changes restore owners when their rows match again.
The Show row offers Open, Validating, Done and All below the Type row.
Old Waiting links open All.
The Owner row follows the Show row.
Exactly one Show chip stays selected. Pressing that chip changes nothing.
Each type count uses the selected owner, Show and words.
Each Show count uses the selected type, owner and words.
One typed `ReviewIndexEntry` record defines the index for counts, filters and column sorting.
The shell carries the complete index, including states outside the selected view.
Each changed version supplies a complete replacement index.
The index contains the key, ID, type, owner, source owner, owner name, title, state, filed time and update time.
It also supplies the source name, urgency label and Later time for each summary.
A Validating Change supplies the time of its request.
The wire format shares identity names and omits an ID when it equals the key.
The index contains summary values and shared staff labels.
The server sends card bodies on demand.
The server sends gzip when the browser accepts it with a positive quality.
Each body response carries its snapshot version.
If the versions differ, the browser refreshes the index before it displays the body.
The browser uses the index for every count and filter.
The Show chips replace the right count text.
Open, Validating and Done select their states.
Ask showrunner to check, Ask question and Send feedback move a change to Validating after the save.
The change leaves Open and its counts when the request starts.
A failed save restores the row and its counts.
Validating and All show the change with **With <showrunner> since <time>** and no buttons.
The Validating and All chips count the change.
The Validating chip is dim at zero.
The name comes from current staff data.
The time comes from the saved request and uses the user zone.
Older requests without a saved time use the delivery file time.
The next changed snapshot replaces the index.
An unchanged version retains the index and checks expired Later times.
The fresh index supplies each item's state and revision, including items outside the rendered Show.
The browser removes a saved marker when the index moves beyond its submitted revision and saved state.
A showrunner answer, feedback action or fault returns a Validating change to Open.
A works result moves the change to Done without a notice.
Stable chip IDs retain keyboard focus after a Show load or refresh.
The row window keeps the focused row mounted during scroll.
A refresh restores focus to the same control by row key.
The browser keeps each pending save by key while the form leaves the window.
Review and The List use Show chips.
The shared table tracks the selected Show chip.
The address and refresh retain the selected Show view.
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
Questions use a violet circle-help icon; Changes use a blue diff icon; Investigations use a teal microscope icon.
Proposals use a pink lamp icon; Efficiencies use a lime zap icon.
The HTTP server retains one `ReviewSnapshot` with the records, index and shell.
The worker records source timestamps before it reads the sources.
The background worker checks source timestamps every half second.
It also checks all stores every thirty seconds.
A complete update replaces the current snapshot.
Requests retain the current snapshot during an update.
The server starts recovery before the snapshot worker.
The live service requests the initial Review build at startup.
Creating a console manifest after server construction does not start a Review build.
The native Question writer assigns stable IDs to legacy Questions under the owner lock.
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
The server returns HTTP 409 if the record is missing or its revision differs from the form revision.
A current Done card accepts Later.
The browser refreshes the index and shows a conflict reason above the table.
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
A Done item uses the same direct body request.
An efficiency card shows its ledger line.

Questions offer Answer and Send feedback in the closing group.
Changes offer Works and Does not work in the closing group.
Ask showrunner to check, Ask question and Send feedback transfer the change to the showrunner.
Investigations offer Mark read and Send feedback in that group.
Proposals and efficiencies offer Approve, Decline and Send feedback in that group.
A plan backlog proposal has no issue to hold an answer, so it offers only Later; the user answers it in chat.
Cards with actions offer Ask question in the return group and **Later** in its own group.
The menu lists four presets, then a divider and **Choose a time…**:
1. **In 1 hour**: `later_soon_minutes` from now; the label follows the setting.
2. **This evening**: `later_evening` today. The item shows only before `later_evening_until`.
3. **Tomorrow morning**: `later_morning` tomorrow.
4. **Next week**: `later_week_time` on the next `later_week_day`.
Each preset shows its time on the right, such as `Tue 09:00`, in the user zone.
The page computes the preset times each time the menu opens.
A preset click hides the item at once.
**Choose a time…** shows a calendar and a time field in the same menu.
Review Later and List Park until share the date and time picker in `record_table.py`.
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
Tab moves from the selected day to the time field and **Save**.
The **Time** field uses 24-hour `HH:MM` in every locale.
It accepts `9:00`, `0900` and `09:00` and displays `09:00` when focus leaves the field or you press Enter.
In the time field, Up and Down change the time by 15 minutes and wrap at midnight.
An invalid time uses the error color and disables **Save**.
Enter saves a valid future time. Escape closes the menu.
The hidden `later` field keeps the wall time for the draft.
The page converts that time to UTC for the request.
A preset resolves its time again at the click.
The arrow keys, Home and End move between items, Enter picks one and Escape closes the menu.
A click outside the menu closes it.
The page refresh waits while a Later menu is open.
The server stores a preset time, sent as UTC, in the user zone.
Waiting cards for questions, investigations, proposals and efficiencies retain their Open actions.
Unowned questions retain only Later.
Waiting cards for these types keep their state and counts until the user acts.
An action on those cards supersedes the open request.
The record and the owner's production log retain the superseded wait.
Questions archive the old followup before the new action saves.
The Review and change pages use one server check to refuse another change action during an open hand-off.
The refusal preserves the request and its delivery state.
Investigations close or replace their open request.
Proposals replace their review mark; efficiencies also clear a deferred ledger state.
The primary button comes first.
The shared sender handles all five types, including retries, owner alerts and startup recovery.
Send feedback sends the words to the owner.
It finishes the item for questions, investigations, proposals and efficiencies.
It transfers a change to the showrunner until the showrunner records what they did.
The change log shows the feedback once and includes the showrunner's reply.
Ask question sends the words to the owner.
It moves a change to Validating and other types to Waiting.
The owner's answer returns every type to Open.
The answer stays in the item log.
Owners use `change_page.py answer-question --item <Review key> --question <id> --text "<answer>"` for all five types.
Each new request supplies a question ID to identify the pending exchange.
The existing question ID rule refuses a reply to a superseded exchange.
`update_registration.py reply` and `investigation.py answer` apply this rule before they save a native answer.
A native answer without the current question ID cannot replace a newer Review question.
`change_review.py verdict` also refuses a verdict during an open question or feedback request.
These commands retain the existing refusal text and exit 1.
Each showrunner check uses its delivery attempt as the validation ID.
Initial and recovered messages include `change_review.py verdict --validation <id>` with the complete answer command.
The native verdict command refuses a missing, old or completed validation ID under the same rule.
Every request message includes the complete answer command.
Later keeps the state and hides the item from the page views, the counts and `/next` until its time.
A change hand-off or return to Open clears the saved Later time.
The shared reader ignores any old Later time on a Validating change.
`<SHOWRUNNER_STATE_DIR>/review-later.json` stores one ISO timestamp for each stable Review key across all five types.
The shared reader applies that timestamp without copying the item or its state.
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
Refresh retains the open row, scroll position and typed text.
The Review navigation entry marks new Open items.
Every type can set this mark.
Approved and declined proposals become Done in the shared reader.

Approve and Decline make a proposal or an efficiency Done at once.
Approve sends the words and the exact record command to the owner showrunner through the shared sender.
For a proposal the command is `proposal_issue.py approve`; for an efficiency it is `candidates.py outcome E<n> accepted`.
A proposal Decline stores the declined answer, as `proposal_issue.py decline-answer` does.
Its message asks the owner to close the issue with `/close_issue`.
An efficiency Decline writes `candidates.py outcome E<n> declined` on the ledger line and sends no message.
A proposal keeps its page log in the `review_log` front matter list and its page mark in `review_state`.
An efficiency keeps them in `<ledger stem>.review.json` beside the ledger.
That file is keyed by the `E<n>` id and holds only the log and the mark, never a copy of the ledger line.
The mark is `waiting` after Ask question and `done` after Approve or Send feedback.

### Accounts page

Hana console, Accounts (`/console/accounts.html`) shows the accounts board.
The console lists it as **Accounts**, with the Lucide `users` icon.
The server keeps the last good page and its render times under one lock.
The first request waits for a render in a child process (`accounts_page.py`).
A later request returns the kept page at once and starts one background render.
A request during a render returns the kept page and starts no second render.
Only requests start renders.
The child keeps the status read's stderr capture away from the server's request log.
`accounts_page_seconds` in `config/review_links.conf` limits each render.
A failed or late first render gives 503 with **The Accounts page is not available.**
A failed background render keeps the last good page and writes one line to stderr.
Failed renders add no render time.
A cached Accounts response carries its render failure to the status line.

The table has two swatch columns without a heading.
The account swatch column is between **account** and **left**.
The role swatch column is between **role** and **next**.
All named headers sort on click or Enter.
The dot and swatch headers have no sort control.
The arrow sits before a right-aligned label and after a left-aligned label.
The arrow uses the column gap without adding to the table width.
The column gap and arrow scale with the root font size.
The viewer's sort choice stays through refreshes.
The Models card below the accounts table uses the same sort controls.
It stores the viewer's sort choice separately and starts in model order.
The card shows the model ID and its swatch at the start of each consecutive group of that model.
The model ID and swatch use the theme ink color when no model color is set.
The two tables have the same total.
A swatch shows for each board span with a `color_key`.
An account has one when `config/statusline.conf` has a valid `account.<label>` color.
The units, showrunners and workers roles use `role.unit-director`, `role.showrunner` and `role.worker`.
The colored words carry `data-color-text` with the same key.
One click opens the picker: a saturation and brightness area, a hue slider, a large color box, a hex field, and Try, Save, and Cancel.
The popup uses the console theme and stays inside the viewport.
Try paints every swatch and word of that key without a request.
Cancel or Escape restores the saved color.
The page pauses refresh while the popup is open or a save is pending.
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
Other lines show a gray Lucide minus icon; the total cell stays empty.
An unavailable queue shows gray `unavailable`; selected Claude groups retain their buttons.
The page uses the first targets from the status read.
The menu title is **When <account> reaches <percent>%, move <group> to**.
`next_move_percent` in `config/accounts.conf` sets the percent.
The menu lists the other Claude accounts in board order, each with its weekly percent left when known.
The current target has a highlight and `aria-checked="true"` with `role="menuitemradio"`.
Arrow keys, Home and End move between the items.
Escape, Tab, a click outside, a scroll, or a resize closes the menu.
Focus returns to the arrow button.
The menu stays inside the viewport.
The page pauses refresh while the menu is open and while a move save is pending.
A choice posts `group` and `to` to `/console/accounts.html` as form data.
The server checks the group and the account note, then calls `next_moves.set_first_next_move()`.
The new entry replaces the group's first next move, or becomes its only one, with no hold.
When the group has an entry, **No next move** posts `group` and `clear=first`.
The server calls `next_moves.remove_next_move()` at `FIRST_GROUP_POSITION`.
It refuses a clear for an empty group.
The cell then shows the next queued target, or `none`.
Success clears the cached page and refreshes the `next` cell.
A refused request returns 400 and a failed write returns 503, each with its reason.
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

### The List page

`/console/list.html` reads the producer's numbered List records on each request.
The navigation label is List, and the page title is The List.
The page uses the shared status line in `page_frame.py`.
The columns are #, Owner, Producer, Item, State, Next step, ETA, and Update.
The Producer cell shows the name of the record's producer.
The default order uses `the_list.order`, which the CLI also uses.
User decisions come first, then unread updates, then the earliest next check.
A column button sorts its values; a second click reverses that order.

The List starts with All selected in the Owner, Producer, and Show rows.
Owner and Producer list staff in name order after All and omit every trailing `-unit` from names.
If the resulting name is empty, the chip shows the original staff ID.
The address keeps the original staff IDs, including `all` when it identifies a staff member.
The words box sits beside the Producer chips and wraps below them when space is limited.
The chip order is All, Needs me, Morty answering, Working, Blocked, and Parked.
The List excludes Closed records from rows, filters, and counts.
Parked includes every non-Closed item with an active park.
An unread delivery release belongs to Needs me even if the question remains open.
Morty answering includes other unparked items with an open question, including items that need Resend.
Needs me also includes unparked items in Waiting for nate or Delivered with no open question.
Working includes other unparked items in New or Working.
Blocked includes other unparked items in Blocked or Paused.
All excludes parked items and includes unparked Done items.
The Unread only switch limits the visible items and chip counts to unread items.
Each chip count reflects the words box and the filters in the other rows.
The “need you” total uses the Needs me chip count.
A zero-count chip stays visible and dimmed.
The chip rows wrap when the available width decreases.

The Expand all and Collapse all buttons sit above the number column at the left of the item count row.
They use the vendored Lucide icons `chevrons-up-down` and `chevrons-down-up`.
Each button applies to all items that match the current filters.
Each button applies the current text in the words box before it selects the items.
Hidden items keep their open or closed state.

A link under Links opens the target item in the console.
The table can change Show to All to reveal the target.
A link to an item in Parked selects Parked.
The table clears each filter that excludes the target and keeps each filter that matches it.
The table scrolls to the target and highlights it briefly.
The table leaves a gap below the sticky status banner when it shows.
A missing target shows “Item #N is not on the List”.
Changing a filter or the sort order clears this notice.
Back restores the previous filters, open rows, and scroll position even when the address stays the same.

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
A new item without the option, and an old record without the field, belong to the record keeper, morty.
The terminal list shows a Producer column after Owner.
The CLI shows producers only their assigned items. Morty can see every item.
See the `the_list.py` section of `showrunner-automation.md` for update permissions and assignment notices.

When List records have review links, the page reads review items once per render and indexes them by key.
It stores no review states on the List record.
The State cell shows “N changes for you” when every review that needs nate is a change.
It shows “N reviews for you” for other review types or mixed types.
Without a review for nate, unfinished reviews show “N reviews waiting on others”.
A count of 1 takes the singular, for example “1 change for you”.
When every linked review is Done, it shows “Reviews done — decide: close, keep or park”.
A missing review key counts as Done.
An item without links shows no review line.
The open row repeats the line above its actions and shows each review's shown ID.
Only types in `review_page.TYPES` link to Review cards.
Other types and missing reviews show plain text.
A review ID opens one card in a dialog on the List.
A summary links to the card when exactly one review is available.
A summary for zero or multiple available reviews is plain text.
The List loads the card document only when the dialog opens.
The existing Review body route supplies the summary, body, styles, and controls.
The Review table uses the same card renderer and action code.
The dialog shows every state, including Done.
A Validating card shows its details and has no actions.
If the record disappears, the dialog shows a message.
Escape, Close, and a click outside the card close the dialog.
Focus returns to the List link.
The dialog fills the screen at the console's phone breakpoint, including on the direct List page.
On a desktop, the List centers the dialog.
Switching browser tabs keeps the dialog and its draft.
Changing console pages closes an idle dialog when it has no draft.
The dialog preserves the List and Review addresses and filters.
The verdict, feedback, and Later controls use the Review save path.
During a save, the dialog stays open and disables its buttons.
A failed save keeps the draft and shows the reason.
Review and the dialog use the same recovery code to load the current revision and keep the draft.
The draft store uses the record key and tracks edits across form replacements.
Recovery preserves edits made after the conflict, including cleared words.
The recovery lock applies to the stale revision.
After a save, Close, Escape, and a click outside work while the List refreshes.
A completed refresh closes the dialog.
If the List request fails or times out, the next periodic refresh can update the row.
List and Review refresh requests use `page_request_timeout_seconds`.
If a List action is pending, the refresh starts after that action ends.
An explicit item target joins the linked selection and opens its card.
The shared table names each linked key that the page cannot find.
It keeps the found cards when another linked key is missing.
Linked counts use only the selected cards, including selected parked cards.
The List number column and the Review ID column use the shared no-wrap rule.

The Owner menu includes record owners without a current staff row.
The Producer menu beside it lists each producer that a record names.
An address with a producer that no record names shows all producers.
The State column shows the saved state of each item.
Closing an item clears its unread mark.
An older address with `show=closed`, `show=done`, or `state=Closed` opens All.
A link to a Closed item shows “Item #N is closed.”
An older address with `show=unread` turns on Unread only.
The search field matches the item number, owner name, title, or state words.
Numbers can include `L` or `#`.
One match function ignores case for every field.
Parked items also match the word `Parked`.
The address retains the owner, producer, status chip, unread switch, search, column, and direction.

A row opens to show its latest status, completion condition, item links, and log.
The record keeps Evidence, and the open row has no separate Evidence section.
The log shows its newest entry first.
On a wide page, the log column fits the height of the left column, including the feedback controls.
The log entries scroll below the heading when they exceed the available height.
A short log has no scroll bar.
On a narrow page, `list_log_max_height_rem` in `config/review_links.conf` limits the height of the entries.
The setting starts at 16 rem.
Refresh keeps the first visible log entry at the same offset when new entries arrive.
A log at the top stays at the top to show the newest entry.
The List keeps the position by item while a filter hides the row.
The log restores that position when the row returns.
Each log writer saves a typed event.
The table and log use the same format for staff names, local ETAs, and field values.
The table, log, and Park response show parking times in the user zone.
At local midnight, the label shows the date.
The log uses plain field labels, such as Next step and Latest status.
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
Each expanded row with actions has Remove, Park until, and a text box for Ask question and Tell morty.
The text box shows “Your words for morty about this item” as its placeholder and accessible name.
Ask question and Tell morty sit below the box at its right edge, beside the send status.
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
Mark read clears the dot and adds a log entry without sending a message.
Ask question and Tell morty use the same path to mark the update read after the action saves.
The form signature includes the identity of the unread update that the row shows.
A new Ask question or Tell morty request can mark only that update read.
A repeated request cannot mark a newer update read.
An exact Tell morty retry uses the read target saved with the feedback.
The store checks the update identity under its lock.
This check preserves an update that arrives between the action save and the read write.
An update identity includes its time and log history.
Two updates with the same time keep separate identities.
Ask question and Tell morty add no read entry when the form shows no unread update.
The POST response includes the current unread state.
The form keeps its signed update until a page refresh shows a new snapshot.
The page clears the dot and Mark read button when that state is false.
It restores both controls when the response reports a new unread update.
Mark read, Park until, and Remove release the link to the item.
If the item stops matching the filters, it starts to fade as soon as the user submits the action.
If the read write fails, the question or feedback remains saved and the sender can deliver it.
The store keeps the unread update if the read write fails or a newer update exists.
Tell morty shows “sent to morty; update remains unread” in both cases.
Tell morty applies the current filters. A read item leaves the view when Unread only is on.
Tell morty with empty words changes no record.
Ask question saves an open question in the item record under the List lock.
The page uses the question and delivery code shared with Images.
The server replies to the browser before it sends morty the item number, title, question, and answer command.
Equal words select the open question. Different words create a new question.
An exact request retry uses the same question ID, even after morty answers it.
The row moves to Morty answering when the question saves.
The row shows a waiting note when each unanswered question has a live sender or confirmed acceptance.
Acceptance includes a confirmed place in the transport queue.
The List changes the search results, chip counts, and totals as soon as the browser receives the save result.
The console updates its unread indicator on its own news refresh.
The next List refresh shows the delivery notice and Resend when a question needs another send.
An unknown result shows “Question Qn may not have reached morty.”
The List shows why Resend is unavailable if the previous sender is active or cannot be checked.
Images shows the same reason when it cannot retry the question.
All attempts for one question use the same sender lock.
A damaged delivery record keeps the question visible and cannot grant Resend while the lock is held.
A released sender lock lets Resend proceed even if the previous process identity is unreadable.
Resend uses the saved question, its ID, and the current recipient to repair a mismatched delivery record.
The question reader checks the stored question words and answer command.
A List title edit preserves the delivery state. Recovery sends the prepared message with its original title.
Only an explicit Resend question action can retry that delivery on the List.
Repeating the same signed resend request does not authorize another send.
If Ask removes the focused row, focus moves to a nearby row with an available toggle or to Search.
After a failed POST restores the row, the List waits for the animation before it returns focus to the submit button.
After a successful Ask keeps the row visible, the row toggle receives lost focus and keeps it through refresh.
Focus stays on the user's selection if they move it during the request, including to the parent console.
On refresh, the List restores focus to an available action button with the same item key and action.
Column sort buttons keep focus through refresh.
A waiting row shows “The producer will answer.”
The panel shows each open question with its asked time and the linked review cards.
An actively parked waiting row shows the parking condition and Unpark.
A waiting row with an unread delivery release shows Mark read.
The waiting row keeps its log after Ask saves.
A link opens a filtered-out question or read item in a separate section and preserves the current filters.
The section does not change the results count.
The List address preserves a linked item outside the filters when the console reloads.
It counts in its current status chip.
Close the linked item or change a filter to dismiss the section.
An open question adds a Morty answering note below the saved state.
Morty runs `the_list.py answer L<N> --question <id> --text "<answer>"` to save an answer.
The answer adds one record event and marks the item unread.
Closing an item moves each open question to a “Question closed with item” log entry.
The entry keeps the question ID, words, and time.
The question no longer accepts answers or retries.
Older Closed records stay in the store in their original format.
Question delivery checks the current List record under the same lock that Close uses.
The check runs before the first send and each retry.
A close preserves the delivery result of a send already in progress.
The sender checks the item again before it sends a failure alert.
A read error during retry authorization preserves the confirmed failure and reports the read error separately.
Startup recovery skips Closed items.
A new Ask on a Closed item is refused.
A retry of a saved Ask returns its saved result without sending again.
After the last answer, the next refresh returns the row to its status chip.
The item stays in Parked while its park is active.
A signed Unpark request from an earlier page can clear parking while the item waits for an answer.
That request moves the item to Morty answering and shows its confirmation next to Show.
An unread answer appears in the panel with the question, both times, and an Unread mark.
Mark read clears the signed update shown by the form.
Opening an individual row with an unread answer uses the same signed read operation.
The List sends this request after the text of each unread answer enters the viewport while the row is open.
This rule also applies to direct links and to answers that appear in linked details after refresh.
The tab and console frame must also be visible.
Expand all leaves answers unread.
A newer update stays unread if it arrives before the read saves.
With Unread only on, an item can stay visible after this read until the user closes its row or changes Show.
Changing the Unread only switch ends this exception.
After the item is read, the log shows “Question Q<n> — nate” and “Answer — <producer>” at their recorded times.
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
Recovery claims unsent work only after the shared question reader verifies the delivery.
Recovery leaves uncertain outcomes for the user.
An older pending attempt with no live sender appears as uncertain.
Remove closes the item through `the_list.close` with source `the_list.PAGE_SOURCE`.
Remove sits at the left edge of the row above the four-line text box.
**Unpark** and **Park until** sit at the right edge of the row.
An expanded parked item shows its parking condition above the buttons.
**Unpark** appears in the form when the item has active parking and its state is outside Closed.
**Park until** opens the picker.
**Until delivered** sits above the shared calendar and 24-hour time field.
One click saves this choice and shows “Parked until delivered” in the row.
The picker aligns with the right edge of the text box.
The page refresh waits while the picker is open.
**Save** sends the chosen time in UTC.
Park writes a date, a time, or `delivered` through `the_list.park` with the List page source.
The server reads a date as midnight in the user zone.
The writer checks a new parking time against the current time under the List lock.
The writer checks a Park retry against the parking condition in the receipt or the event with the same request ID.
The picker can resend the original request and instant after the parking time passes.
When the saved date has passed, the picker gives focus to an enabled day.
The page keeps the words for Tell morty.
**Unpark** clears parking through `the_list.unpark` and keeps the item state.
It records one Unparked event and saves one notice for morty.
If the item has no active parking time, the writer saves only a hidden request receipt.
The receipt records whether the time expired or was absent.
A replay of that request preserves any newer parking time.
The response reports the saved request outcome and the current parking state.
The page shows “Parking time already expired” for an expired time.
It shows “No active parking time to clear” when the time is absent.
Both results preserve the parking draft.
A replay of a successful Unpark reports the original result and any newer active parking time.
When Unpark clears parking and removes the row from Parked, the page shows “Item #N unparked.”
The page shows confirmations for hidden rows next to the Show filter.
It keeps one confirmation per hidden row after overlapping Unpark requests.
Focus moves from **Unpark** to the Parked filter and brings the filter and confirmation into view.
If All is active when Unpark saves, the row stays expanded and its item number can receive focus.
After Unpark, the item's saved state sets its status chip.

A direct link to a parked item selects Parked.
Morty can set this parking condition with `the_list.py park --id L<N> --until delivered --source morty`.
When morty records Delivered, the List writer clears this parking condition in the same write.
The Review snapshot worker checks each snapshot after it publishes it.
It checks linked change rows that the snapshot shows as ready for the user.
Requests can use the published snapshot while the worker checks the parked items.
The worker tries the List lock, the Review delivery lock, and the change list lock in that order.
List Ask takes the List lock before the Review delivery lock.
The worker skips a busy lock or unreadable change row and can retry after a later snapshot.
List GET reads saved items and published question delivery records without the release locks.
Console news reads the saved List records.
The worker holds all three locks while it checks the current Review row and saves a release.
The same parking event and link must still be current.
The writer clears parking, sets Delivered, and marks the item unread.
Both delivery signals save an Unparked event with the reason.
The unread delivery release returns to Needs me with its review links.
After Mark read, an open question returns the item to Morty answering.
A dated park remains active until its time or an explicit Unpark.
The shared item writer saves the action and its pending notice in one locked write.
The notice tells morty which action nate completed.
The existing sender delivers the notice with its retry and alert rules.
A delivery failure does not reverse the completed item action.
Tell morty sends the user's words without changing the item's state or parking time.
Remove hides the item at once and closes it when the save succeeds.
Park moves an item to Parked at once.
With Unread only on, the row leaves the view when the user presses Mark read, Ask question, or Tell morty.
The row returns when the server reports an unread update and the row matches the current filters.
The shared table fades and collapses the row and its open details when an action hides them.
The request starts without waiting for the animation.
The page disables all row buttons while the request runs.
A second press sends no request.
The page applies the saved state and parking status from the response.
The page uses the saved parking condition to update the labels for the expanded and collapsed row.
A refusal restores the last confirmed item state, parking status, and unread state together.
The unread state includes the Update sort key.
If Mark read, Ask question, or Tell morty loses its response, the page restores the unread flag from before the request.
The page opens the restored row and shows the reason there.
The restored row slides back into view.
After a Park refusal, the page returns focus to **Park until** only if the document body has focus and the control is visible and enabled.
Each row keeps its own inline failure notice.
The List uses the same rules as Review for notices above the table.
If Remove or Park loses its response, the page restores the confirmed row and reports that the save is unconfirmed.
A new action uses a new request ID, even if the previous response did not arrive.
The page keeps each unconfirmed request ID for an exact retry, even after another action.
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
The same write saves a pending notice for actions that send one.
Under the lock, each action checks the request ID against the saved actions.
A retry changes no item fields and saves no second notice.
The writer refuses a saved ID with a different action or different words.
After an unconfirmed directory sync, the writer checks the stored record under the same lock.
If the record holds the request, the server reports that it saved the action.
The List sender uses the shared `change_page` retry and alert helpers.
A successful send appends the same request ID with the sent state.
Exhausted tries alert the item owner's showrunner.
The request becomes alerted only when at least one showrunner receives the alert.
Failed alerts leave the request pending for repeated POSTs and startup recovery.
An alerted request shows “not sent; <showrunner> told” until morty writes the item again.
The server startup hook retries pending List requests separately from change and question recovery.
A repeated POST retries a pending request without adding another pending log entry.
Sent and alerted requests do not send again.
A process claim prevents concurrent sends of one request without holding the List lock during delivery.
Delivery is at least once; a crash after a send but before its saved result can cause another send.
The Update cell shows “sent to morty” with its time until the next log entry from morty.
The page retains draft words, dates, actions, request IDs, and tokens during a refresh.
An unchanged retry uses its saved request ID and token after a lost response.
After a lost Park or Remove response, the List reads the current item before it restores the row.
For Park, it checks the receipt for the exact request even when the item already has a parking time.
Changed feedback uses the fresh signed request from the page.
A successful send clears the saved request ID and only the text that the user sent.
Each successful POST returns a fresh signed request ID for the next action.
A retry retains its original request ID.
It shows a save or send notice as soon as the user submits a form.
An unread update shows a dot and marks the navigation entry as new.
The navigation key includes the record ID and update time.
The page uses `page_refresh_seconds` and the shared refresh helper.
A refresh retains the search text, selection, focus, scroll position, open rows, and filters.
An older response cannot replace a newer response.
Phone layouts show each row as a card.
The counts strip uses the status line readers for questions, changes, and rebuilds.
Each count links to its existing page.
The strip omits proposals and efficiencies.
Closed items show no check text in the page or CLI.

### Images page

`/console/images.html` shows registered images with their instructions, oldest first.
A new image goes to the end, so the cards above it keep their places.
The console lists it as **Images**, with the Lucide `image` icon.
A showrunner or unit registers each image, or a before and after pair, with one command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py register (--image PATH | --before PATH --after PATH) --caption "TEXT" --owner STAFF_ID (--change ID | --phase ID)
```

- The caption says what changed and what to look at.
- `scripts/review_links/reply_shot.py <markdown> <columns> <png>` renders a Markdown reply in the Claude Code terminal.
  It resumes a placeholder transcript in Xvfb and xterm, so no model call happens.
  It cuts the shot at the first blank band of `reply_shot_gap_rows` pixel rows, so the page shows readable text.
  `config/review_links.conf` sets the wait, the terminal rows and the band height.
- The owner is the staff id that made the image. The command refuses an id that is not in the staff record.
- The command accepts `.png`, `.jpg`, `.jpeg`, `.gif` and `.webp` files and refuses an empty caption.
- It copies the image to `<review root>/images/<12-hex id>/image<suffix>` with a private `.review-link-image.json` record.
  A pair copies `before<suffix>` and `after<suffix>` to one folder with one record and one id.
  The record keeps the after image in `file` and the before image in `before`.
  It builds the folder under a private draft name, then renames it, so the page never shows a partial image.
- It prints the image file links (before, then after, for a pair) and the image's own console link:
  `http://<host>:<server_port>/console/?page=images&at.images=%2Fconsole%2Fimages.html%23image%3D<id>`.
  `console.place_query` builds the query from the console's place key.
- The server serves the copy, so the user never needs the source path.

Each card shows, in this order:

1. The caption as the card title, in a larger font.
2. The details, with aligned labels: `Owner:`, `For  :` and `Added:`.
3. The image link. A pair shows its two images side by side, under **Before** and **After** labels.
4. The **Feedback** field, with `feedback_rows` rows from `config/review_links.conf`.
5. The answer buttons: **Approved**, **Not Approved** and **Send Feedback**.

A plain click on a single image or either pair image opens the shared image zoom.
The zoom centers the full image over a dark backdrop within the page view.
It fits the view with a 16px margin and never exceeds the image's natural size.
Escape or a click on the backdrop or image closes it.
The page keeps its scroll position and returns focus to the image link.
Shift, Ctrl, Meta and Alt clicks retain the browser's link action.
Middle clicks and the right-click menu retain the browser's link action.
`page_frame.ZOOM_STYLE` and `ZOOM_SCRIPT` supply one zoom through `change_page.document`.
One document click listener handles `data-image-zoom` links, including links from later page updates.
The modal dialog takes focus and uses the image's alt text as its accessible name.

**Owner** is `<name> (<kind>)`, or `Unknown session: <id>`. **For** is `Change <id>` or `Phase <id>`.
**Added** is `MMM dd, yyyy, HH:MM` on the 24-hour clock in the user's zone (`config/user_zone.conf`).
The Images page shows every waiting image, oldest first, with no age filter.
A pair has one caption, one details block, one Feedback field and one set of answer buttons. The owner delete removes both images.
On a page 1108px or wider, a single card is 680px wide and a pair card is up to 1376px wide; both are centered in the images pane.
When a pair card is narrower than 600px, its images stack, before on top.
A page URL with `#image=<id>` opens at that card.

A group is a run of shown cards in a row with the same owner.
A panel at the top left of the images pane lists one row per group, in three columns: owner name, count and the time of its first image.
The owner name has no kind, such as `(unit)`.
The panel has a rounded outline and thin lines between its rows and columns.
The panel stays in place while the cards scroll.
Each row shows a hover color, and a click on a row goes to the first image of its group.
On a narrow screen the panel goes above the cards and scrolls with them, and its owner column takes the spare width.
When no images wait for an answer, the page shows **No images registered.**
The page refreshes its cards when the console shows it again and when the tab becomes visible.
A hidden frame makes no request.
The card list carries `data-script`, the page script hash.
When a refresh brings a different hash, the page loads again in place of the swap.
An older script does not bind the new card forms, so a form would post outside the script.

Each answer button posts the image id, the button's answer, the feedback text and a token signed with the console browser token.
The server checks the token.
**Approved** and **Not Approved** need no text. **Send Feedback** refuses empty text with **Enter feedback first.** and keeps the card.
Otherwise the card leaves the page at once, and the request runs in the background.
The page skips its refresh while a send runs.
An image leaves the page while each open question has a live sender or confirmed acceptance.
The page also hides an image while the server recovers its verified delivery on startup.
A failed or uncertain delivery returns the card with a delivery notice.
When the owner answers the last open question, the card returns and the console shows a new update.
The server sends one message to the owner's staff id, resolved to its current session at each try.
It uses `delivery_tries` and `delivery_pause_seconds` from `config/review_links.conf`.
The first line names the answer: `From the user (via the Images page): ` and then `your image is approved.`, `your image is not approved.` or `feedback on your image.`
A `Feedback:` line follows when the user wrote text.
Then come the image link (a `Before:` and an `After:` link for a pair), the caption, the For line and the delete command for the owner to run after it acts.
After a send, the record gets `feedback_at` and the page hides the image; the image file stays until the owner deletes it.
A page loaded before the answer buttons can still post a delete, which removes the image folder under `images/.review-link-images.lock` and sends no message.
When no try reaches the owner, the server answers 503 and writes nothing.
The card goes back in its place with one line: **Not sent: <reason>**.
An agent deletes an image with this command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id ID
```

### Rebuilds page

`/console/rebuilds.html` lists machines that require a rebuild and shows commit details.
The status line shows an action count or Unavailable.
Each render reads `rebuild_row.read()` once (`scripts/statusline/rebuild_row.py`), with `config/statusline.conf`.
The page combines the cached system comparison with open rebuild needs.
The server serves it only in a folder that holds the browser manifest.
It has no navigation entry; `?page=rebuilds` opens it.
A `data-rebuilds` block carries `data-count`, the number of machines, and `data-label`, the link label.
The page shows one block per machine, with natedev first, then the Mac.
Each block has the title `Rebuild natedev` or `Rebuild the Mac`, then the changes grouped by owner.
Each owner shows once, as a heading, and the owner with the newest change comes first.
Under an owner, the changes go newest first, and changes with the same shown time share one time.
The time is a column at the left, with the reasons of that time beside it.
The changes are the `/etc/nixos` commits on the configured ref after the commit the machine last switched to.
`rebuild_row.read_changes()` reads them from `<rebuild.cache>-changes.json`; the status line refresh writes that file.
A commit's reason is its subject and its time is the author time.
Its Who comes from the commit's `Claude-Session` line: the session whose transcript records that Remote Control session.
A filed need is a change with its requester (else the affected session) as owner, its creation time and its reason.
A need covers its own commit, so that commit shows once.
Names come from the current staff record as `<name> (<kind>)`.
An unknown session appears as `Unknown session: <id>`; a commit without a known session shows `Unknown session`.
The time shows in the user's zone as `Nov 15, 2026, 08:45` (24-hour), in gray text that keeps at least 4.5:1 contrast.
A listed machine without a recorded change list shows `The change list is not available yet.`
When no machine needs a rebuild, the page shows only `No rebuilds needed.`, with a count of 0 and an empty label.

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
A direct link shows **The showrunner will validate this** without answer buttons.
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
The row shows **Saving…** during the save and **Saved: <result>** after success.
The row then leaves the view without opening another card.
A failed save keeps the row and typed text and shows **Not saved: <reason>**.

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
Send feedback and Ask question on either change page send the words to the showrunner and move the change to Validating.
When one staff unit owns the change, every other verdict also goes to that unit.
The Producer cell identifies that unit through its fixed staff id.
An empty Producer cell belongs to the list's staff showrunner.
Retries retain stored staff recipient ids.
Older records without recipient ids compute the same recipients as a fresh verdict send.
No agent polls for answers.
A save shows **Saved: &lt;answer&gt;** from the review record.
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
The page shows **Saved: <answer>**, **The showrunner will validate this**, or **The showrunner will answer this**.
The page shows no delivery state, attention links, or resend buttons.

The server reads `config/review_links.conf` through `scripts/lib/conf.py` when delivery starts.
Each page render reads `feedback_rows` once to set every feedback box's height.
`delivery_tries` starts at 3; `delivery_pause_seconds` starts at 5.
Tests use a zero pause.
Each round sends once to every recipient that has not succeeded.
The server pauses once between rounds and retries only failed recipients.
The showrunner receives its first attempt before any recipient receives a second attempt.
A `QUEUED:` result with exit 1 counts as kept; a `KEPT:` result with exit 0 also counts as kept.
Other nonzero exits fail.
After all tries fail for a recipient, the server sends one alert to the row's showrunner, with no alert retries.
The alert names the list, row, recipient, number of tries, and last error.
It states that the user's answer is saved and requests a manual handoff.
If the alert fails, the server prints one line to stderr and stops.
The user receives no delivery alert.

At startup, the server delivers pending records with the same retry and alert rules.
Recovery preserves attempts that a live request owns.
Recovery scans each review list once and finds each record through `change_routing.delivery_path`.
It ignores the `page` field in legacy records.
The browser and recovery use `StaffRouting.owner_and_production` for the same owner and production document.
The `Production:` line names the showrunner in live and recovered messages.
It never delivers a verdict without a saved verdict in the review list.
Legacy failed records decode as nothing to deliver; they produce no send or page controls.
A legacy failed validation keeps validation ownership without a message attempt.
After a send finishes or an alert finishes, the server clears that attempt.
A validation keeps its ownership slot with state `sent`.
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
The server reads List records from `FOLDER/records` and staff records from `FOLDER/staff.json`.
Open `/console/list.html` on the fixture port.
Remove uses the normal List save path in the fixture store.
The fixture serves only the List page and accepts only Remove.
The fixture omits counts from other pages.
It skips message delivery, recovery, snapshot sweeps, and code restarts.
The server checks the fixture folder before it checks the Tailscale address.
Fixture requests use the scratch directory lock during file access.
It exits with status 0 when the folder disappears.

The rest of this section describes the normal server and its initial config values.
The server reads the current settings when it needs them.

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
- A published `changes.html` with a private manifest is rendered from the review record. Every other regular file gets 200 with `Content-Type`, `Content-Length`, `Last-Modified` and `X-Content-Type-Options: nosniff`. The server never sends 304.
- Content types: `.html` and `.htm`, in any case, go out as `text/html; charset=utf-8`. A type that Python's `mimetypes` lists under `image/` keeps that type. All other files, `.md`, `.txt`, `.json` and `.pdf` included, go out as `text/plain; charset=utf-8`.
- Each request writes the standard access line to stderr.

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
`review_words_rows` sets the height of the Review page **Your words** box from 1 to 20 rows and starts at 4.
The `later_*` keys set the Review page Later presets; each render reads them.
`later_soon_minutes` is 1 to 1440 minutes and starts at 60.
`later_evening`, `later_evening_until`, `later_morning` and `later_week_time` are HH:MM times in the user zone.
They start at 18:00, 17:00, 09:00 and 09:00.
`later_week_day` is a weekday name and starts at Monday.
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


Legacy Changes and Questions forms and the Images page share one motion helper.
A different Changes or Questions card fades out for 180ms, then fades in for 270ms with a 4px upward move.
A new switch completes the active switch first.
The first load and an update that keeps the same card do not fade.
A new card above the viewport scrolls into view smoothly.
Image links scroll smoothly to their image, then reveal it over 270ms with a 4px upward move.
Other image cards stay visible.
An image fades before removal, and the cards below close the gap over 270ms.
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
The button shows a check and the label “Copied” for two seconds after success.
A second click starts the two seconds again.
A failed copy keeps the copy icon and shows “Copy failed” for two seconds.
The shared script uses the clipboard API when available.
Plain HTTP pages use a temporary textarea and `execCommand('copy')` when the clipboard API is absent.
The fallback restores focus and the previous selection.
One document listener also handles code blocks from page updates.
