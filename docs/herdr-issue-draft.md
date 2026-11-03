# Draft: herdr issue (not filed)

Status: draft for the user to read. Nothing is filed on GitHub until the user approves it.

---

**Title:** Claude detection: `live_prompt_box` reads an open permission dialog as idle when a stray horizontal line (`─`) is above it

_Disclosure: I wrote this issue with help from Claude Code. Claude ran the repro steps and the `herdr agent explain` checks below, and wrote most of the text. I read, edited, and approved it before filing._

## What happens

A Claude Code pane at a permission dialog ("Do you want to proceed?") shows as **idle**, not **blocked**, for as long as the dialog is open.

`herdr agent explain` names `live_prompt_box` (idle, priority 950). `bash_permission_prompt` (blocked, priority 850) also matches but ranks lower.

## Cause

`live_prompt_box` reads region `prompt_box_body`, the text between the last two rows of `─`, and matches when a line there starts with `❯`. Claude Code draws these rows around its input box. A row need not be full width: an indented row of 140 `─` in a 158-column pane counts.

While a dialog is open, it takes the input box's place, and the last row of `─` is above the dialog. If the transcript above that row holds one more row of `─`, the text between the two is read as the input box, and an echoed `❯` prompt there makes `live_prompt_box` match.

The rule's `not` clauses (`esc to cancel`, `enter to select`, …) do not help: they test the same region, and the dialog's footer is outside it.

## How to reproduce

Use a herdr pane 158 columns wide. Start Claude Code in default permission mode, then send three prompts.

**Prompt 1** puts a row of `─` in the transcript:

```text
Reply with only this, as plain text and no code block: the word ok, then on the next line 140 copies of the box-drawing character U+2500 and nothing else. Do not use any tool.
```

**Prompt 2** puts a `❯` line below that row. The command runs with no dialog.

```text
Run this exact Bash command and nothing else: uptime
```

**Prompt 3** opens a permission dialog ("Do you want to proceed?"):

```text
Run this exact Bash command and nothing else: curl -sI https://example.com
```

While the dialog is open, save the screen and ask herdr how it reads it:

```sh
herdr pane read <pane> --source detection > screen.txt
herdr agent explain --file screen.txt --agent claude
```

**Result** with the downloaded rules (`version = "2026.10.11.1"`): idle, not blocked.

```text
state: idle
rule: live_prompt_box (region=prompt_box_body priority=950)
evidence: "\n✻ Cooked for 4s · done 11:09\n\n❯ Run this exact Bash command and nothing else: uptime\n…"
```

**Control:** remove only the row of `─` from `screen.txt` and run `herdr agent explain` again. The result is `state: blocked`, rule `bash_permission_prompt`.

The saved `screen.txt`:

```text

 ▐▛███▛█   Claude Code v2.1.296
▝▜██████▀  Opus 5.5 with xhigh effort · Claude Max
 ▝▝   ▝▝   ~/scratch/herdrcheck


❯ Reply with only this, as plain text and no code block: the word ok, then on the next line 140 copies of the box-drawing character U+2500 and nothing else.
  Do not use any tool.

● ok
  ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

✻ Cooked for 4s · done 11:09

❯ Run this exact Bash command and nothing else: uptime

  Ran 1 shell command

● The system has been up 28 days, 18 hours and 23 minutes, with 1 user logged in. Load averages are 82.66, 72.89 and 87.87 over the last 1, 5 and 15 minutes.

✻ Brewed for 3s · done 11:09

❯ Run this exact Bash command and nothing else: curl -sI https://example.com

● Fetching HTTP headers from example.com
  ⎿  $ curl -sI https://example.com

──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
 Fetch HTTP headers from example.com
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 curl -sI https://example.com
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: curl *
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No

 Esc to cancel · Tab to amend
```

## Expected

The pane is **blocked** while the permission dialog is open, whatever the transcript above the dialog holds.

## Actual

The pane is **idle**. The rule is `live_prompt_box`.

## Version

- herdr 0.9.5, Linux (NixOS).
- Claude detection rules downloaded to `~/.local/state/herdr/agent-detection/remote/claude.toml`, `version = "2026.10.11.1"`.
- Claude Code 2.1.296.

## Suggested fix

We use a local override with two edits:

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
