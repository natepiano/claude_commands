---
description: Set every running unit director's effort, on every production, and the effort new unit directors launch with.
argument-hint: '<level> [--check]'
---

# Effort

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_effort.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- It types `/effort <level>` into each unit director's pane on this machine, whatever its
  production, and prints `<name>: sent` or `<name>: skipped — <reason>` for each. A pane showing
  a form, prompt or menu, or a draft in its input line, is skipped; a director mid-turn is not.
- Its last line is the registry's `production.director` row: same model, the new level.
- `--check` sends nothing and changes nothing (`<name>: would send`).
- A level the row's model does not take is refused before anything happens.

`/effort` also saves that level as the account's default for new sessions of that model,
because every account shares one settings.json.
