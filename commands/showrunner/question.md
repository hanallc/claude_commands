---
description: Queue a user question for later review.
argument-hint: "[<unit staff id>] <the question in full>"
---

# Question

Use this command for a unit's question or your own question. Do not file questions you can decide under <DecisionEconomy/>.

1. Write the question in parts, as **Question form** below says.
2. Resolve the asker through the staff record.
3. Run the command below.
4. Tell the asking unit that you filed its question.
5. Add the question to your next `open_for_user` state.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --production <showrunner name> [--unit <unit staff id>] --ask "<question in one line>" --context "<why now>" --item "<fact>" [--item ...] --recommend "<choice and why>" --choice "<answer>" [--choice ...] [--urgency blocks]
```

`--ask` is the only form for a new question. Pass `--text` for a list title other than `--ask`. A question stored as one text before this form still shows on the page.

Omit `--unit` for your own question. A current unit label is also accepted. The stored `asked_by` always holds the fixed staff id.

`ask: ok` means the question is filed. Add nothing about it to your reply to the user.

`question held for review:` means the question needs correction. Correct the named defects before you file it again.

Questions are separate records beside proposals in the production's `Proposals` folder. `/showrunner:questions` lists them. Reminders remain separate.

The status count and `/next question` retain their behavior. The queue keeps a filed question until `answer` removes it.

After the user answers, follow the `answer` relay line. It resolves the asker's current staff row after a rename or transfer.

A follow-up from the Questions page comes to the asker with a `reply` command. Run it with your reply:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py reply --production <showrunner name> --item "<question text>" --text "<your reply>"
```

The question then returns to the user with the follow-up and the reply.

## Question form

@~/.claude/docs/question_template.md
