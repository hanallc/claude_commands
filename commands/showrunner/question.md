---
description: Queue a user question for later review.
argument-hint: "[<unit staff id>] <the question in full>"
---

# Question

Use this command for a unit's question or your own question. Do not file questions you can decide under <DecisionEconomy/>.

1. Write the question in parts. The Questions page shows each part as its own section:
   - `--ask`: the question in one line. The lists use it as the title unless you pass `--text`.
   - `--context`: why it is asked now and what waits on it, in one or two sentences.
   - `--item`: one entry for each thing to look at, such as a before and after or a link. Repeat it.
     For an image, give the image's own link that `/show_image` prints, not the Images page link.
   - `--recommend`: the recommended choice and why.
   - `--choice`: one answer the user can pick, such as `go for both`. Repeat it. Each choice is a button that fills the Answer box.
   - A question with choices needs at least two `--choice` and one `--recommend`. An open question has no `--choice`.
2. Resolve the asker through the staff record.
3. Run the command below.
4. Tell the asking unit that you filed its question.
5. Add the question to your next `open_for_user` state.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --production <showrunner name> [--unit <unit staff id>] --ask "<question in one line>" --context "<why now>" --item "<entry>" [--item "<entry>" ...] --recommend "<choice and why>" --choice "<answer>" [--choice "<answer>" ...] [--urgency blocks]
```

`--ask` is the only form for a new question. A question stored as one text before this form still shows on the page.

Omit `--unit` for your own question. A current unit label is also accepted. The stored `asked_by` always holds the fixed staff id.

`ask: ok` means the question is filed. Add nothing about it to your reply to the user.

`question held for review:` means the question needs correction. Correct the named defects before you file it again.

Questions are separate records beside proposals in the production's `Proposals` folder. `/showrunner:questions` lists them. Reminders remain separate.

The status count and `/next question` retain their behavior. The queue keeps a filed question until `answer` removes it.

After the user answers, follow the `answer` relay line. It resolves the asker's current staff row after a rename or transfer.
