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
2. Write the question in parts, as **Question form** below says.
3. Run the command:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --unit <your unit staff id> --ask "<one line>" --context "<why now>" --item "<fact>" [--item ...] --recommend "<choice and reason>" --choice "<answer>" [--choice ...] [--urgency blocks]
   ```
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
6. The user can send a follow-up from the Questions page.
   The message starts with `From the user (via the review page), a follow-up on your question:`.
   Answer it with the command that the message gives:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py reply --production <showrunner name> --item "<question text>" --text "<your reply>"
   ```
   The question then returns to the user with the follow-up and your reply.
   A skip message needs no action; the question stays open.

## Question form

@~/.claude/docs/question_template.md

## Investigations

File an investigation when the user wants you to find an answer.
Use a question when you need the user's answer.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/investigation.py file --title "<one line>" --question "<full question>"
```
