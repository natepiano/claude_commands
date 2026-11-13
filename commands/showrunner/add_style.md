---
description: Start or attach a style production.
argument-hint: "<showrunner name> [check] [--pilot <unit>[=<crate>,...]]"
---

Run from the repository's main showrunner checkout, once per repository.
This creates the style branch, the excluded production document and the configured standing plans.
It starts the style showrunner through the configured showrunners account.
An existing style production attaches to yours. A second branch point is refused.
Pass your model id and effort level; `check` prints the steps without acting.
Show the output; stop on a nonzero exit.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/style/add_style.py" --production <showrunner name> --model <your model id> --effort <your effort level> [--check]
```

On creation, repeat `--pilot` to launch only named units. They go in the doc's
`**Pilot units:**` field, and the style showrunner launches only them. Package names after
`=` restrict that unit's crates. Plans remain for every configured unit. Example:
`--pilot modules --pilot cargo-liner-style-names=cargo-handler --pilot types=cargo-port`.
A pilot accepts a family or its full staff name.
`--check` lists the units and scopes. An existing production refuses `--pilot`.
An interrupted bootstrap can change only units whose worktrees do not exist;
after launch, edit scope only in that unit's own plan copy (see
`docs/style_production.md`, Pilot scope).

The production document's `Style units` table names each unit, its families and its `Open tickets` count.
Launch or attach each style unit with this command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py --production <owning production> <unit> --unit-type style --plan <unit plan>
```

Staff selects `/unit:style` for that unit.
Each family has exactly one owner. One unit can own all four families.
`Open tickets` sets the number assigned at once; an empty value means 1.
Refill refuses a table with a missing family owner.

Bootstrap creates four default rows named `<repo>-style-<family>`, each with one family and `Open tickets` set to 1.
The table can instead name one to four units with any staff names.
Read worktree and branch locations through staff.
For units without a style showrunner, put the table in the parent production document.
These units use the direct route in `docs/style_production.md`.
