---
description: Add a producer.
argument-hint: 'NAME [--model MODEL] [--effort EFFORT] [--job TEXT]'
---

If the user gives no name, ask “What is the producer’s name and job?” and stop.

1. Run `agents add-producer NAME [--model MODEL] [--effort EFFORT] [--job TEXT]` with the supplied arguments.
2. Show the output. Success prints the producer name, staff ID, account, and tab.
3. After success, tell morty the name, staff ID, and job. Replace the three placeholders below. Use “No job supplied” if the user gave no job. The key and window stop a second notice when a repeat finds the producer already made.

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to morty --key producer-added-<staff ID> --repeat-minutes 1440 <<'EOF'
Added producer <name> with staff ID <staff ID>. Job is <job>.
EOF
```

Repeat with the same options to resume an interrupted creation.
See [agents.md](../agents.md#create-a-producer) in `~/.claude/commands`.
