---
description: File a New item for a producer, showrunner or unit, and name its producer. /new_item with no arguments shows the usage.
argument-hint: "<type> <assignee> <description> [extra, such as a time]"
---

# New item

**Arguments**: $ARGUMENTS

The CLI is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/new_item.py"`. Run it with the sandbox off.

If Send loses the Scheduled reply, reopen New item in the same tab. Select Retry to recover the submitted item.

## Steps

1. With no arguments, run the CLI with no arguments. It shows the usage and exits 5. Show its output as is and stop.
2. Split the arguments into the type, assignee, description, and extra. Keep nate's words as he wrote them.
   For `scheduled`, put the instruction, `Start`, and optional `Repeat` in the description. For other types, put the final time or order in the extra.
3. Run `<CLI> <type> <assignee> "<description>" ["<extra>"]`.
   For `scheduled`, first run with `--preview`. On success, show the start and repeat. Then run without `--preview` to save.
   On failure, show the error and stop.
   Use nate's zone unless the start includes an IANA zone or a UTC offset.
   For a time that occurs twice, give both the offset and the zone. Use a form like 01:30-08:00[America/Los_Angeles].
   Use the current year if the start omits the year.
   `/new_item scheduled natedev Check the account predictions. Start Mon Nov 30 09:00. Repeat every 2 weeks on Monday and Thursday.`
4. On exit 0 for `scheduled`, show the filing receipt and any notice warning. Then stop. For other types, show the output as is. With an extra, show how you read it. Then do step 8.
5. Exit 3: the output asks which producer. Nothing is filed. Send nate the output as is and wait for his answer. Save it with his exact words:
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/producer_map.py" set <assignee> <producer> --user-words "<nate's answer>"`
   Add `--unit-only` when nate says "<producer> for <unit> only". Send nate the "Saved:" line, then do step 3 again. On a nonzero exit, show nate the error line and stop.
6. Exit 4: the output asks about the name. Nothing is filed. Send nate the output as is. On "yes", do step 3 again with the name that the output guessed. Else use the name he gives.
7. Exit 1 or 2: show nate the error line and stop.
8. If the result line says "this session", do the work now. Send no "On deck" line.
   Else go back to the open point in a few words: "On deck: <open point>. <its question, if any>"
