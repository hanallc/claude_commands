---
description: Review decisions one at a time.
---

Use this when the user has just received a long list — recommendations, findings, options, todos — and wants to review them deliberately one by one instead of responding to the whole wall of text.

**Read `~/.claude/docs/user_facing_explanation.md` first and follow it.**

**Read `~/.claude/docs/type_design.md` too and follow it.** When an item concerns
types, APIs, stored state, or optional values, use that contract to evaluate the
proposal and recommendation. A vague name or domain-owned `Option<T>` is a
design issue; consider the restructuring needed to make a precise semantic type
truthful, not only a rename.

## Open or join a review

Run `~/.claude/scripts/adhoc_review/shared_review.py` with `~/.claude/scripts/lib/py`.
For `/adhoc_review join bob` or “join Bob's ad hoc review”, run `join bob` on the owner's machine.
For `/adhoc_review resume`, run `resume` to open your review again.
Keep the returned `owner` and `review` IDs. Pass them in that order as `OWNER REVIEW` below.
After a join or resume, run `show OWNER REVIEW`. Then go to Step 3.

## Step 1: Identify the items

Find the list to review. Look in this order:

1. The user's invocation contains the list (pasted in, or pointing at a section).
2. The most recent assistant message had an enumerated list — bullets, numbered items, table rows.
3. Neither — ask the user where the list is.

If more than one list could be intended, ask which list or section; do not
combine them. Otherwise, determine the count but do not reply yet. Carry the
count into Step 2 without listing the items.

## Step 2: Set up where decisions get recorded

If the user chose a document for decisions, use it. Otherwise, ask only if an obvious document exists:
  > `N items. Record decisions in <relative/path/to/doc.md> (recommended)? (yes / transient doc)`

If you ask, wait for the answer. Create a missing document the user names with the date and item source.
Split independent decisions before you share the list. Keep the original ID as the prefix of each subitem.
Use `scripts/lib/scratch.py new --name adhoc-items` for the input folder. Write a JSON array of full item texts.
Run `start --source SOURCE --items FILE`. Delete the input folder in Python after the script saves the list.
Tell the user the item count and where the temporary shared document is. Keep the returned IDs.

## Step 3: Build the todo list

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/statusline/adhoc_items.py" <remaining>` with the item count.

The shared list is the task list. Use its item numbers in both sessions.

## Step 3.5: Establish the shared model

Run this once, before the first item, whenever the items all live inside one
model the user has not already been walked through in this conversation — a set
of types, actors, or states that only mean anything in relation to each other.
Skip it only when the items are genuinely independent of one another, or when the
user built the model with you earlier in the session.

**This is not a glossary.** A sequence of definitions is exactly what fails: each
one is individually correct and the reader still cannot say how the pieces fit,
so they reconstruct the model themselves out of N item-scoped explanations.
State the relationships instead.

Cover this, in this order:

1. **The participants** — one line each, giving the *question it answers* rather
   than what it is. "`DeviceKey` — which physical unit is this?" beats
   "`DeviceKey` is a durable device identifier."
2. **What is durable and what is transient.** Which participants outlive which,
   and what event makes a transient one go away. This is usually the load-bearing
   fact of the whole model and it is almost never stated outright.
3. **How they connect** — which participant points at which, with cardinality in
   both directions: "one device, many roles; one role, one device at a time."
4. **One worked instance** with real values, not placeholders. A table of
   concrete rows carries relationships better than prose — this is the one place
   in this command where a table is the right tool.
5. **One change over time.** Show that same instance before and after the event
   the model exists to survive — a device unplugged, a session lost, a job
   cancelled. What holds still and what moves *is* the model.

Then hand it back for correction:

> `That's the model the N items sit inside. Restate it your way or correct it, and I'll start the walk.`

Wait for the response. A correction here is worth more than a correction ten
items deep, because every later item inherits this framing. If the user restates
it differently but equivalently, adopt **their** wording for the rest of the walk
— theirs, not the source document's.

**Ask no decision question in this step.** Orientation and decisions are separate
turns.

## Step 4: Walk the list

For each item:

1. Run `next OWNER REVIEW`. Present only the item it claims for this session.
2. On `busy`, say that another session holds the remaining items. Resume when asked. On `complete`, go to Step 5.
3. Build the causal explanation with the shared file's build order. Then use its **Ad hoc review items** template.
4. When a subagent or worker prepares items, give it the shared guide, the user's own words for the request, and the text the user sees on screen.
5. Run the shared **reader test** and **comprehension gate** on each item before the user sees it, prepared items included. Then show the item.
6. **Wait for the user's response.**
   - If the user picks `elaborate` (also: "more", "detail", "expand", "why"): add one new rationale, constraint, or example relevant to the current item. Do not broaden scope or dump the raw item. Then re-present the same choices and wait again.
   - If the user picks `simplify`, follow **Simplify the item** below.
   - If the user says they are lost, confused, or asks what the introduced concepts mean, treat that as a failed initial explanation and stop asking for a decision. Apply repair-downward-then-upward from the shared file: **the upward repair here is Step 3.5** — run it now, whether or not it ran earlier. Re-running it mid-walk costs one turn; iterating item detail at ever finer grain costs many and does not terminate.
   - If the user asks a clarifying question, answer it without recording a decision or advancing. Re-present the choices only when useful.
   - If the user proposes a modification, restate only the revised decision, update the recommendation or example as needed, and wait for explicit acknowledgment before recording it.
7. Treat `okay` or `continue` as a decision when the choice is clear. Run `record OWNER REVIEW ITEM DECISION --actions ACTIONS`.
8. Include pending actions and actions you completed. If the user chose a document, the owner copies decisions from `show` there.

Do not ask "continue or add more?" between items — assume continue unless the user volunteers something.

Update the status line with `remaining` after each change to the item count.
Use `next OWNER REVIEW --item N` to revisit an item. Use `add OWNER REVIEW TEXT` to append an item.
If the helper reports that the review ended, go to Step 5.

### Simplify the item

1. Rewrite the same item in simpler words. Keep every technical detail and add no facts or rationale.
2. Save both complete versions in private scratch files. Send the versions and one line about how the first version failed:
   `~/.claude/scripts/lib/py ~/.claude/scripts/adhoc_review/simplify_report.py --from <session-name> report --before <file> --after <file> --failure <line>`
   Continue the review if the report fails.
3. Keep the report ID, including for a queued report. Keep the simplified item file while the item is open. Show only the simplified item with the same choices.
4. If the user gives feedback on the rewrite, apply it to the explanation. Save their exact words in a private scratch file. If you have the report ID, send the feedback.
   `~/.claude/scripts/lib/py ~/.claude/scripts/adhoc_review/simplify_report.py --from <session-name> feedback --report <id> --item <after-file> --words <file>`
   Keep the report and delivery result out of the review.

## Step 5: Wrap up

If you joined, run `leave OWNER REVIEW` to release your claim. Only the owner runs `end OWNER REVIEW`.
If the review already ended, skip `leave` and `end`.
The `end` command returns the final decisions and removes the shared document, including when the owner ends early.
Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/statusline/adhoc_items.py" 0` when this session stops its review.
The command output names the held messages file. Read the file at your own pace.
Each release in the file lists its messages oldest first.

The owner summarizes each completed decision as `1. <label> → <decision>`. List pending actions or say `none pending`.
If the user chose a document, the owner copies the final decisions there and names the document.

## Rules

- One item at a time. Never present two items in the same turn.
- When you ask another agent for input during an open review, tell it to answer with `send.py --urgent`. Only an urgent message arrives while the review is open.
- **Model-level questions get model-level answers, at Step 3.5's altitude** — participants, what is durable versus transient, how they connect, one worked instance — and then return to the item. Adopt the user's wording verbatim if they restate the model themselves.
- Step 2's doc question, when asked, is a choice like any other: mark exactly one option recommended.
- Show before and after code comparisons only when the user asks.
- Code in an item: show at most the one or two lines that show what the thing is, and one use of it. Show no derive lines, asserts, exports or neighbour types.
- When the user asks a question on an open item, give the answer alone, in one to three lines. Do not show the item again with added lines. Show it again only when the user asks, and then shorter.
- The reason in the recommendation names the result that the user sees, not the mechanism.
- Put a number that the decision depends on in the point or the first fact line, with its unit.
- An item that asks the user to approve a new thing (a type, a command, a setting, a page) says in one line what exists today that is nearest to it, and why that does not do the job. Check this before you show the item.
- If the user says skip, use a supported skipped/cancelled terminal status; if none exists, mark it completed with the skip noted. Then move on without arguing.
- An explanation request never suspends the one-at-a-time rhythm.
- If the review maintains a decision ledger, update it only after acknowledgment and leave undecided decision cells blank. Show it after each acknowledgment only when the user requested a running ledger; otherwise show it at wrap-up.
- When the user adds an item, record it without asking for its decision. Put prerequisites first when you add related items.
