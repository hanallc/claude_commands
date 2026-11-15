---
description: Queue a unit's user question for review.
argument-hint: "<the question in full>"
---

# Question

A unit uses this command when it needs the user's answer.
This includes an action that cannot be undone or a choice only the user can make.
See `~/.claude/docs/production_format.md`, `<ProductionUnit/>` item 2.
The command adds the question to the production's open-questions list.
The status line count and `/next question` show the question.
Do not send a separate reply to the user.

Send design choices and proposed additions to the showrunner.

## Steps

1. Read your unit's staff row and its parent showrunner row.
   Use your unit's fixed staff `id` for `--unit`.
   The command reads the production document from the parent showrunner's `doc.path`.
   Do not pass a production document path or resolve one against your current directory.
   Without a parent showrunner, end the turn with `— decision:` as `/unit:direct` requires.
2. Write the question in parts, so the user can read it days later.
   The Questions page shows each part as its own section.
   - `--ask`: the question in one line, such as `alpha: keep the old key binding?`. The lists use it as the title.
   - `--context`: the unit, phase, file or screen, why you ask now, and which work waits. Keep it short.
   - `--item`: one entry for each thing to look at, such as a before and after or a link. Repeat it.
   - `--recommend`: the recommended choice and the reason.
   - `--choice`: one answer the user can pick, with its result. Repeat it. Each choice is a button that fills the Answer box.
3. Run the command:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --unit <your unit staff id> --ask "<one line>" --context "<why now>" --item "<entry>" [--item "<entry>" ...] --recommend "<choice and reason>" --choice "<answer>" [--choice "<answer>" ...] [--urgency blocks]
   ```
   `--question "<the full question>"` with `--text "<one line>"` still files a question as one text.
   Pass `--urgency blocks` when the phase needs the answer to continue.
   The command reviews the question before it adds the question to the list.
   `ask: ok` and `filed question:` mean that the command added the question.
   The user sees the filed text.
   Do not send the filed text to the showrunner or repeat it in a reply to the user.
   For `question held for review:` (exit 2), rewrite the question with the missing facts and repeat this step.
   For `already open`, do not file the same text again.
4. Continue work that does not need the answer.
   When no work can continue, end the turn with `— decision: filed for the user: <one line>`.
5. Apply the answer that the showrunner sends with `From the user (via the showrunner):`.
   Treat the answer as the user's instruction.
