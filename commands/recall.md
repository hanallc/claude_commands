---
description: Ask the List producer to track an item. Also applies when a prompt ends with "recall".
---

Send an item to the List producer.

**Usage:** `/recall [park <when>] [item]`

1. **Item.** Take the item from `$ARGUMENTS`.
   With no item text, or when "recall" ends a prompt, use that prompt and the current conversation.
   Write one or two sentences that the List producer can track without this conversation.
   Include what to track, the owner when known, and the result that closes the item.

2. **Park.** Copy any park words, such as `park till tuesday`, exactly as written.
   The List producer applies the park terms.
   Park hides the item from the user’s list until the List producer unparks it.

3. **Send.** Replace `<item>` with the item from step 1.
   Replace `<park words>` with the words from step 2, or omit it when absent.
   Write the whole message on one line. Replace line breaks in the item and the park words with spaces.
   Read the List staff ID from `page.list.owner` in `~/.claude/config/console.conf`.
   Replace `<List staff ID>` and send one message.

   ```bash
   ~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to "staff:<List staff ID>" <<'RECALL_MESSAGE'
   Add this item to your tracking list: <item> <park words>
   RECALL_MESSAGE
   ```

4. **Report.** Show the caller the item sent and the `send.py` result.
   Report a non-zero exit as an anomaly, including its code and message.
   Do not send the message again after a non-zero exit.

When "recall" ends a prompt that requests other work, do that work too.
`/recall` adds a tracking item.
