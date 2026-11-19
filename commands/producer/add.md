---
description: Add a producer.
---

If the user gives no name, ask “What is the producer’s name and job?” and stop.

1. Run `agents add-producer NAME [--model MODEL] [--effort EFFORT] [--job TEXT]` with the supplied arguments.
2. Show the output. Success prints the producer name, staff ID, account, and tab.
3. After success, tell morty the name, staff ID, and job. Use “No job supplied” if the user gave no job.

   ```bash
   python3 ~/.claude/scripts/message/send.py --to morty --from "<session name>" \
     --text "Added producer <name> with staff ID <staff ID>. Job is <job>."
   ```

Repeat with the same options to resume an interrupted creation.
See [agents.md](../agents.md#create-a-producer) in `~/.claude/commands`.
