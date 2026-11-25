---
description: File a New item for a producer, showrunner or unit, and name its producer. /new_item with no arguments shows the usage.
argument-hint: "<type> <assignee> <description> [extra, such as a time]"
---

# New item

**Arguments**: $ARGUMENTS

The CLI is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/new_item.py"`. Run it with the sandbox off.

## Steps

1. With no arguments, run the CLI with no arguments. It shows the usage and exits 5. Show its output as is and stop.
2. Split the arguments into the type, the assignee, the description and the extra. The extra is the last words that give a time or an order. Keep nate's words as he wrote them.
3. Run the CLI with each part as a separate quoted argument:
   `<CLI> <type> <assignee> "<description>" ["<extra>"]`
4. On exit 0, send nate the filing receipt from standard output. If the user gave extra words, add a line that gives your reading of them. Then do step 8.
   If the line says "no List item exists" or "exists without its Review link", the CLI filed the Review item but the List write failed. Do not run the CLI again, because a second run files a second item.
5. Exit 3: the output asks which producer. Nothing is filed. Send nate the output as is and wait for his answer. Save it with his exact words:
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/producer_map.py" set <assignee> <producer> --user-words "<nate's answer>"`
   Add `--unit-only` when nate says "<producer> for <unit> only". Send nate the "Saved:" line, then do step 3 again. On a nonzero exit, show nate the error line and stop.
6. Exit 4: the output asks about the name. Nothing is filed. Send nate the output as is. On "yes", do step 3 again with the name that the output guessed. Else use the name he gives.
7. Exit 1 or 2: show nate the error line and stop.
8. If the result line says "this session", do the work now. Send no "On deck" line.
   Else return to the open point. Name it in one full sentence in nate's words.
   Ask one question with one answer word, for example, "Say show it."
   Do not repeat the choice words of an item that is not on the screen.

The owner receives the private reply command through Review delivery. Keep it in the owner session.
