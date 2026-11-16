---
description: Manage dwight, the strategist.
---

# dwight — strategist

Start dwight with `resume dwight`.
Use the workers account for Codex and the showrunners account for Claude.
Read the family, model, and effort from `[producers].dwight` in `~/.claude/config/models.conf`.
Use the `dwight` tab in the `producers` herdr workspace.
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

Run `agents transfer dwight [--model MODEL --effort EFFORT]` in the session that takes the role.
Give both options when that session's family differs from `[producers].dwight`.
The session restarts as dwight and receives no prompt.
After the restart, load this command and read the role documents.
Read the old conversation with the printed `catch_up.py` command, then run `send.py pending`.
