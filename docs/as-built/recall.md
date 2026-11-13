# /recall

`/recall [park <when>] [item]` sends an item to morty's tracking list. It is a prompt only, in `commands/recall.md`, with no script and no test. A prompt that ends with the word "recall" also uses it.

## How it works

1. **Item.** The item comes from the arguments. With no item text, or when "recall" ends a prompt, the session writes it from that prompt and the conversation: what to track, the owner when known, and the result that closes it.
2. **Park.** Park words, such as `park till tuesday`, go to morty as written. morty reads them in its own park terms (`commands/producer/morty.md` → "Parking and dependencies").
3. **Send.** One `send.py --to morty` call reads the message from stdin through a quoted heredoc. With no `--from`, `send.py` names the sender as `send.py in <session name>`.
4. **Report.** The session shows the item and the `send.py` result. A non-zero exit is an anomaly to report; the message is not sent again.

When "recall" ends a prompt that asks for other work, the session does that work too.

## Invariants

- The message is one line. Line breaks in the item or the park words become spaces, so no line can equal the heredoc delimiter `RECALL_MESSAGE`.
- The quoted heredoc does no expansion, so quotes, `$(...)` and backticks reach morty unchanged.

## Gotchas

- `--text "..."` with the item inside breaks on a double quote and runs `$(...)` in the shell. The heredoc form replaced it.
