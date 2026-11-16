---
description: Manage brain.
---

# brain

Start brain with `resume brain`.
Use the workers account for Codex and the showrunners account for Claude.
Read the family, model, and effort from `[producers].brain` in `~/.claude/config/models.conf`.
Use the `brain` tab in the `producers` herdr workspace.
Resume the same conversation.

## Questions

File your own questions with this command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --ask "<question>" --context "<why now>"
```

Use the question parts from `commands/showrunner/question.md` when you need facts or choices.
The question review applies before filing.
Your fixed staff id owns the record, status count, and Questions page filter.
The answer goes to your current session, including after a session change.

## Transfer

Run `agents transfer brain [--model MODEL --effort EFFORT]` in the session that takes the role.
Give both options when that session's family differs from `[producers].brain`.
The session restarts as brain and receives no prompt.
After the restart, load this command and read the role documents.
Read the old conversation with the printed `catch_up.py` command, then run `send.py pending`.

## Investigations

File an investigation when the user wants you to find an answer.
Use a question when you need the user's answer.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/investigation.py file --title "<one line>" --question "<full question>"
```
