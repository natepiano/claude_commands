# Recall — send an item to morty's tracking list

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A `/recall` skill sends an item to morty, who tracks it on its list. An optional park instruction goes to morty as the user wrote it.

## Delegation Context

- **Project:** `~/.claude` commands.
- **Project started:** 2026-11-12T21:25:00+00:00
- **Stack:** Markdown commands.
- **User request (2026-11-12, via sdlc, verbatim):** "i need a skill called recall - when i say /recall or append recall to a prompt - it should be something that is sent to morty to track on morty's list.  it should accept an argument to park - i would say /recall park till tuesday and such - same language morty uses to park - so just pass the "park instructions" to morty in the message - assign this out for creation"
- **sdlc:** "Read commands/producer/morty.md for morty's park words. Keep script support minimal."
- **morty:** a Codex session. Reach it only with `python3 ~/.claude/scripts/message/send.py --to morty`. Its park words are in `commands/producer/morty.md` → "Parking and dependencies": `park # until DATE/TIME`.
- **Command style:** read `/succinct_style` (`~/.claude/commands/succinct_style.md`) before a command edit.
- **Invariants:**
  - Do not change `config/copies.conf`, `config/direct.conf` or any permission setting (`settings*.json`, `templates/settings_local.json`).
  - The user adds no checks: add no guard, refusal or scan that a Work Order does not name.

## Phases

### Phase 1 — /recall  · status: done

#### Work Order

**Goal:** the user types `/recall <item>`, or ends a prompt with "recall", and morty adds the item to its tracking list.

**Spec:**

Write `commands/recall.md` in the format of the other top-level commands (frontmatter `description`, one-line purpose, **Usage**, numbered steps). Usage: `/recall [park <when>] [item]`.

The `description` says the skill also applies when a prompt ends with the word "recall".

Steps it gives:

1. **Item.** Take the item from `$ARGUMENTS`. With no item text, or when "recall" ends a prompt, take the item from that prompt and the current conversation. Write it as one or two sentences that morty can track without this conversation: what to track, the owner when known, and the result that closes it.
2. **Park.** When the arguments hold park words (for example `park till tuesday`), copy them as written. Do not parse or convert them; morty reads them in its own park terms.
3. **Send** one message with `python3 ~/.claude/scripts/message/send.py --to morty --from "<session name>" --text "..."`. The text asks morty to track the item on its list, and adds the park words as written when there are any.
4. **Report.** Show the caller the item sent and the `send.py` result. Report a non-zero exit as an anomaly, with its code and message. Do not send the message again after a non-zero exit.

When "recall" ends a prompt that also asks for other work, do that work too; `/recall` only adds the tracking.

The skill is a prompt only. It adds no script and no test.

**Files:**
- `commands/recall.md` — new command.

**Workers:** 1 writer; one file, nothing splits.
- `impl` — `commands/recall.md`.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** `commands/recall.md` exists and gives steps 1–4. `git diff --check` is clean.

**Change-Review:** `/recall is in the command list | <machine>, any session: type /recall park till tuesday check the build page | morty adds the item to its list, parked until Tuesday`.
