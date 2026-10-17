---
description: Start or attach the repository's standing style production
argument-hint: "<parent production doc> [check]"
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
