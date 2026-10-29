# Draft: herdr issue (not filed)

Status: draft for the user to read. Nothing is filed on GitHub until the user approves it.

---

**Title:** Claude detection: `live_prompt_box` reads an open permission dialog as idle when a stray horizontal line (`─`) is above it

## What happens

A Claude Code pane sits at a permission dialog ("Do you want to proceed?"), but herdr shows it as **idle**, not **blocked**. The pane stays idle for as long as the dialog is open.

`herdr agent explain` names `live_prompt_box` (idle, priority 950) as the rule that set the state. `bash_permission_prompt` (blocked, priority 850) also matches, but it has a lower priority.

## Cause

`live_prompt_box` uses region `prompt_box_body`: the text between the last two horizontal lines on the screen (full-width rows of the box-drawing character `─`, U+2500, which Claude Code draws above and below its input box and at the top of a dialog). It matches when a line in that text starts with `❯`.

While a permission dialog is open, the last horizontal line on the screen is the top border of the dialog, not the input box. If the transcript above the dialog holds one more horizontal line (for example, a separator in tool output or in a rendered message), the text between that stray line and the dialog's top border is taken as the input box. When that text holds a line that starts with `❯` (an echoed earlier prompt, or a `❯` in tool output), `live_prompt_box` matches.

The rule's `not` clauses (`esc to cancel`, `enter to select`, …) do not help, because they test the same region, and the dialog's footer is below the last horizontal line, outside that region.

## How to reproduce

1. Start Claude Code in a herdr pane in default permission mode.
2. Make the transcript show a horizontal line of `─` characters and, below it, a line that starts with `❯`. For example, ask Claude to print a line of `─` characters, then send a second prompt so that the echoed `❯ <prompt>` line is below that line.
3. Ask Claude to run a Bash command that needs approval, so that the permission dialog opens at the bottom of the screen.
4. Run `herdr pane get <pane>` and `herdr agent explain <pane>`.

Steps 1-3 are the shape of the screen we saw; we found the cause from a saved screen, not from these exact steps. To test without a live session, save the screen to a file and run `herdr agent explain --file <screen> --agent claude`.

## Expected

The pane is **blocked** while the permission dialog is open, whatever the transcript above the dialog holds.

## Actual

The pane is **idle**. The rule is `live_prompt_box`.

## Version

- herdr 0.9.5, Linux (NixOS).
- Claude detection rules downloaded to `~/.local/state/herdr/agent-detection/remote/claude.toml`, `version = "2026.10.11.1"`.
- Claude Code 2.1.296.

## Suggested fix

We use a local override with two edits, and it has worked since 2026-10-21:

1. `live_prompt_box` matches only a box whose body **opens** with the `❯` line, not any body that has a `❯` line somewhere:

   ```toml
   # was: line_regex = ['^\s*❯']
   regex = ['\A(?:[ \t]*\n)*[ \t]*❯']
   ```

2. A new rule reads a dialog at the bottom of the screen as blocked, above the idle rule:

   ```toml
   [[rules]]
   id = "live_permission_prompt"
   state = "blocked"
   priority = 980
   region = "after_last_horizontal_rule"
   visible_blocker = true
   contains = ["do you want to proceed?", "esc to cancel"]
   all = [
     { any = [
       { line_regex = ['(?i)^\s*❯?\s*1\.\s*yes\b'] },
       { line_regex = ['(?i)^\s*❯?\s*2\.\s*yes\b'] },
       { line_regex = ['(?i)^\s*❯?\s*2\.\s*no\b'] },
       { line_regex = ['(?i)^\s*❯?\s*3\.\s*no\b'] },
     ] },
   ]
   ```

Either edit alone fixes the case we saw. The second one also covers a dialog whose footer has no "enter to" hint, which `live_blocked_form` does not match.
