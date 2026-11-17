# For you actions

The action store holds steps that only the user can perform now.
Questions and changes remain ordinary Review items.
This phase adds no page behavior.

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
