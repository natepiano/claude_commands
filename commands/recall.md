---
description: Ask morty to track an item; also applies when a prompt ends with the word "recall".
---

Send an item to morty's tracking list.

**Usage:** `/recall [park <when>] [item]`

1. **Item.** Take the item from `$ARGUMENTS`.
   With no item text, or when "recall" ends a prompt, use that prompt and the current conversation.
   Write one or two sentences that morty can track without this conversation.
   Include what to track, the owner when known, and the result that closes the item.

2. **Park.** Copy any park words, such as `park till tuesday`, exactly as written.
   Do not parse or convert them; morty applies its own park terms.

3. **Send.** Replace `<item>` with the item from step 1.
   Replace `<park words>` with the words from step 2, or omit it when absent.
   Write the whole message on one line. Replace line breaks in the item and the park words with spaces.
   Send one message. The quoted heredoc passes the text to morty unchanged:

   ```bash
   python3 ~/.claude/scripts/message/send.py --to morty <<'RECALL_MESSAGE'
   Add this item to your tracking list: <item> <park words>
   RECALL_MESSAGE
   ```

4. **Report.** Show the caller the item sent and the `send.py` result.
   Report a non-zero exit as an anomaly, including its code and message.
   Do not send the message again after a non-zero exit.

When "recall" ends a prompt that requests other work, do that work too.
`/recall` only adds the tracking.
