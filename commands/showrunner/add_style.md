---
description: Start or attach the repository's standing style production
argument-hint: "<parent production doc> [check] [--pilot <unit>[=<crate>,...]]"
---

Run from the repository's main showrunner checkout, once per repository.
This creates its style branch, excluded production doc and four standing plans,
then starts the style showrunner through the configured showrunners account;
an existing production attaches to yours. A second branch point is refused.
Pass your model id and effort level; `check` prints the steps without acting.
Show the output; stop on a nonzero exit.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/style/add_style.py" --production <parent production doc> --model <your model id> --effort <your effort level> [--check]
```

On creation, repeat `--pilot` to launch only named units; package names after
`=` restrict that unit's crates. All four plans are still written. Example:
`--pilot modules-unit --pilot names-unit=cargo-handler --pilot types-unit=cargo-port`.
`--check` lists the units and scopes. An existing production refuses `--pilot`.
An interrupted bootstrap can change only units whose worktrees do not exist;
after launch, edit scope only in that unit's own plan copy (see
`docs/style_production.md`, Pilot scope).
