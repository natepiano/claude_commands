---
description: Start or attach a style production.
argument-hint: "<showrunner name> [check] [--pilot <unit>[=<crate>,...]]"
---

Run from the repository's main showrunner checkout, once per repository.
This creates its style branch, excluded production doc and four standing plans,
then starts the style showrunner through the configured showrunners account;
an existing production attaches to yours. A second branch point is refused.
Pass your model id and effort level; `check` prints the steps without acting.
Show the output; stop on a nonzero exit.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/style/add_style.py" --production <showrunner name> --model <your model id> --effort <your effort level> [--check]
```

On creation, repeat `--pilot` to launch only named units. They go in the doc's
`**Pilot units:**` field, and the style showrunner launches only them. Package names after
`=` restrict that unit's crates. All four plans are still written. Example:
`--pilot modules --pilot cargo-liner-style-names=cargo-handler --pilot types=cargo-port`.
A pilot accepts a family or its full staff name.
`--check` lists the units and scopes. An existing production refuses `--pilot`.
An interrupted bootstrap can change only units whose worktrees do not exist;
after launch, edit scope only in that unit's own plan copy (see
`docs/style_production.md`, Pilot scope).

A unit id is `<style showrunner name>-<family>`; the style showrunner name is `<repo>-style`,
independent of the parent production's name. Worktrees and branches use
`<style showrunner name>-<family>`.

| Family | Unit id |
| --- | --- |
| names | `<style showrunner name>-names` |
| values | `<style showrunner name>-values` |
| types | `<style showrunner name>-types` |
| modules | `<style showrunner name>-modules` |
