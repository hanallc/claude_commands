---
description: File a New item for a producer, showrunner or unit. /new_item with no arguments shows the usage.
argument-hint: "<type> <assignee> <description> [<extra info>]"
---

# New item

**Arguments**: $ARGUMENTS

## Steps

1. Run the CLI with the sandbox off. Give the type, the assignee, the description and the extra info as separate quoted arguments:
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/new_item.py" <type> <assignee> "<description>" ["<extra info>"]`
   With no arguments, show its usage output as is and stop. On exit 1 or 2, show nate its error line and stop.
2. On exit 3, the output names the member to map by staff id and name. For a unit, this is its showrunner, so one row covers its other units too. Ask nate in one short line which producer that member reports to. Save his answer with his exact words, then do step 1 again:
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/producer_map.py" set <staff id to map> <producer> --user-words "<nate's answer words>"`
   The save goes through /tune: it is live at once, and main and the Mac get the row. On a nonzero exit, show nate its error line and stop.
3. Send nate one line: the type, the item id and the assignee.
4. If the item belongs to this session, do the work.
   Else give the short on-deck update: what is on deck, in a few words, and whether nate will look at it.
