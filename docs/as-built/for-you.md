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
Each rebuild card lists its open requests under Why.
Each request shows its reason, requester and time.
An old time without a UTC offset uses UTC.
An old request with no reason shows `No reason given`.
The card uses the oldest open request for its age.
When only the comparison calls for a rebuild, the card has no request age.
The card reports an unknown running revision when neither embedded metadata nor an active switch confirms it.
The requests stay open until the background refresh or the clear command confirms completion.
Completed requests appear on Rebuild details in order of completion, newest first.
Staff names replace known staff IDs for display.
`rebuild.cwd.natedev` and `rebuild.cwd.mac` supply each target machine's rebuild working directory.
The reader preserves `~/` for expansion on the target machine.
`rebuild.repo` remains the evaluation repository.
The reader creates no second rebuild store.

A stale comparison or failed machine probe cannot prove completion.
The cache records each machine's probe outcome, last running path, and last evaluated path.
A cache without the per-machine fields adds no rebuilds and becomes due for refresh.
Each evaluated path includes its revision.
An older revision's path gives an unknown comparison and preserves any saved rebuild need.
The card says `The running revision is unknown.` when it cannot identify the running revision.
`UnavailableSource` identifies each failed source.
`KnownOpenCount` counts each open machine row and each open agent action once.
`UnavailableOpenCount` has no count.
If the action store read fails and there is no saved action count, the server returns `available: false` and omits the count.


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

Before the first count, the badge shows no number.
When a probe fails and the machine has no usable comparison, the count uses the other comparisons and the readable requests.
A comparison-only result shows the count without a state word or outline.
The panel shows the entries without stale text.
A transport failure keeps the newest complete count and entries and marks them stale.
For a transport failure, the phone badge shows a dotted outline in both navigation states.
A response without a count also keeps the newest complete count, including with HTTP 503.
A partial count shows only until the next read result.
A confirmed or comparison-only zero uses the quiet `For you 0` button.
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
The status line omits a known zero count.
When only the needs scan is incomplete, the status line keeps the valid rebuild names and shows a count such as `1+ actions`.
For other incomplete reads, it shows `Unavailable`.
It keeps the known rebuild names when the count is unavailable.
Legacy review cache labels remain readable during the transition.
The status line no longer inserts rebuild text into review counts.
The Rebuilds page remains available at `?page=rebuilds` for commit details.
