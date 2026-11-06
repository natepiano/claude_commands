---
description: Promote a session to unit director.
---

**Usage:** `/showrunner:promote_unit <session name> [unit plan]`. Run it in a showrunner session during `/showrunner:produce`, with that command's state (`PRODUCTION_DOC`, `CHECKOUT`, `MERGE_BRANCH`, `LOG`, `ZONE`, `PROMPT_FILE`).

The other direction, a unit becoming the showrunner of its own production under this one, is `/showrunner:promote_showrunner`.

1. **Find it.** Its `ListAgents` name matches `name` in one `~/.claude/sessions/<pid>.json`, which gives `pid`, `sessionId` and `cwd`. Read its pane or its transcript's tail for what it is doing. For `<unit name>`, replace each run of characters outside letters, digits, `-` and `_` in the ListAgents name with one `-`, then drop leading and trailing `-`.
2. **Check fit.** Stop and say why when:
   - its work is in a repository other than the production's;
   - it is mid-turn or a form waits (the idle test in `/showrunner:produce` → Compact after a checkpoint): wait for idle;
   - `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/staff.py get <sessionId>` prints a path under another showrunner: ask that showrunner for `/showrunner:transfer_unit` first.
3. **Plan.** Use the given plan, or write one from work this production already holds. Moving a phase from another unit is packaging; that unit director drops the phase from its plan. Commit the plan on `MERGE_BRANCH`. Push the branch.
4. **Check.** Run `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/add_unit.py --production <showrunner name> <unit name> --attach <sessionId> --cwd <cwd> --plan <plan> --check`. On exit 2, stop and tell the user its line; the session remains running.
5. **Attach and record.** Run the same command without `--check`. add_unit stops the session safely, then resumes it as the unit.
   The unit director launch takes its model and effort from `/default_models production.director`.
6. Tell the user the staff name and the place that add_unit printed.
