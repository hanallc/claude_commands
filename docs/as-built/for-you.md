# For you actions

The action store holds steps that only the user can perform now.
Questions and changes remain ordinary Review items.
The console shell shows the global action notifier on each page.

`UserAction` is the typed record in `scripts/for_you/action.py`.
Each action uses `~/.local/state/for-you/actions/<uuid>.json` on the console host.
`FOR_YOU_ACTIONS_DIR` selects a fixture folder.
One lock covers deduplication and each atomic file replacement.
Done and withdrawn records remain on disk.

File an action with the following command:

```sh
scripts/lib/py -m scripts.for_you.action add \
  --title "Restore GitHub access" --command github-warmup \
  --machine natedev --cwd /home/natepiano --requested-by sdlc \
  --reason "The login requires your browser." --waits "The release check" \
  --concerns admin-console --dedupe-key github-access
```

The filer prints the action key and its Review URL.
The URL selects `type=for-you`, `show=open`, and `item=<key>`.
The filer never executes the stored command.
A repeated requester and dedupe key returns the existing open action.

Read all retained records with `scripts/lib/py -m scripts.for_you.action list --json`.
Withdraw an action with the following command:

```sh
scripts/lib/py -m scripts.for_you.action withdraw \
  --id action/<uuid> --requested-by sdlc --reason "The request is obsolete."
```

Withdrawal checks the supplied requester against the stored requester.
This local command uses the same trust boundary as other local filers.
`ActionStore.mark_done(key, evidence=...)` records manual completion.
The later signed page route must authorize the user before it calls this method.
The CLI provides no completion command.

`scripts/review_links/for_you.py` returns a `ForYouSnapshot` with typed `ForYouRow` values.
It groups rebuild needs into one row per machine.
It also reads completed needs and the current rebuild comparison.
Each `RebuildRequest` retains its reason, requester, concerns, and switch evidence.
Staff names replace known staff IDs for display.
`rebuild.cwd.natedev` and `rebuild.cwd.mac` supply each target machine's rebuild working directory.
The reader preserves `~/` for expansion on the target machine.
`rebuild.repo` remains the evaluation repository.
Waiting work shows the affected staff member's name and open phase from the staff member's recorded run.
A worker uses its unit's recorded run.
A missing staff row or open phase shows the name or staff ID with `Not recorded`.
The reader creates no second rebuild store.

A stale comparison or failed machine probe cannot prove completion.
The comparison cache records running-system probe availability for each machine.
A legacy cache without that field leaves the comparison unavailable.
Historical rows use `unknown` when the reader cannot confirm their current state.
`UnavailableSource` identifies each failed source.
`KnownOpenCount` counts each open machine row and each open agent action once.
`UnavailableOpenCount` retains the known open count without claiming a complete count.


The shell reads `/console/for-you.json` through the shared reader.
The response contains the complete count and at most two open action entries.
The desktop button uses the approved amber style.
The collapsed rail keeps the icon and badge.
The phone header keeps the button visible.

The button opens a nonmodal panel with machine names, copy buttons, and card links.
Each card link selects Review with `type=for-you`, `show=open`, and the action key.
The footer opens Review with the reserved parameters and no action key.
Both links keep other pages’ saved locations.
The foryou-page phase adds the For you filter and card expansion.
Escape closes the panel and focuses the visible For you button.
Copy writes the command to the clipboard.
Copy does not complete the action.

The shell uses `page_refresh_seconds` for timed reads.
It pauses the timer while the tab is hidden.
It also reads on visibility, window focus, and panel opening.
A same-origin framed page can send `for-you-completed` after a successful completion.
The shell accepts that message only from one of its page frames.
If a read is in progress, the shell reads again when it ends.
A refresh keeps focused commands and text selections until the user releases them.

Before the first complete read, the badge shows `?` with an unavailable label.
A transport failure keeps the last complete count and entries and marks them stale.
The phone badge shows a dotted outline for a stale count in both navigation states.
An explicit unavailable response clears the previous count, including with HTTP 503.
At zero, the quiet button shows `For you 0`.
The panel says `No actions required.`

The status line shows session review counts under `Review:`.
It retains adhoc items and the existing review count colors.
A separate `For you:` row names each waiting rebuild and counts the other open actions.
On Linux, the shell checks the source stamps and deadline before it passes saved session fields to jq.
These fields include the assignment, branch, Review counts, adhoc count, and For you row.
On a miss, `assignment.py` reads the current sources.
It saves the session fields only after a complete read with unchanged sources and a valid deadline.
The Mac requests the rebuild machines and counts from the console host without command text.
The Mac resolves the peer in `console.conf` setting `for_you.host` with `tailscale ip -4`.
The status line omits a known zero count and shows `Unavailable` for an incomplete read.
It keeps the known rebuild names when the count is unavailable.
Legacy review cache labels remain readable during the transition.
The status line no longer inserts rebuild text into review counts.
The Rebuilds page remains available at `?page=rebuilds` for commit details.
