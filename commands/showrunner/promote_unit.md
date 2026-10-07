---
description: Make a named running session a unit director under this showrunner — stop it, resume it in tmux as this production's unit, and hand it a unit plan.
---

**Usage:** `/showrunner:promote_unit <session name> [unit plan]`. Run it in a showrunner session during `/showrunner:produce`, with that command's state (`PRODUCTION_DOC`, `CHECKOUT`, `MERGE_BRANCH`, `LOG`, `ZONE`, `PROMPT_FILE`).

1. **Find it.** Its `ListAgents` name matches `name` in one `~/.claude/sessions/<pid>.json`, which gives `pid`, `sessionId`, `cwd` and `tmux`. Read its pane or its transcript's tail for what it is doing. For `<unit name>`, replace each run of characters outside letters, digits, `-` and `_` in the ListAgents name with one `-`, then drop leading and trailing `-`.
2. **Check fit.** Stop and say why when:
   - its work is in a repository other than the production's;
   - it is mid-turn or a form waits (the idle test in `/showrunner:produce` → Compact after a checkpoint): wait for idle;
   - `SHOWRUNNER_UNIT` in `/proc/<pid>/environ` marks it as another showrunner's unit with work open: ask that showrunner to release it. A unit whose run is done is free; tell its showrunner it moved.
3. **Plan.** Use the given plan, or write one from work this production already holds. Moving a phase from another unit is packaging; that unit director drops the phase from its plan. Give the plan the `> **Production:**` header (`production_format.md`) and commit and push it on `MERGE_BRANCH`.
4. **Check.** Run `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/add_unit.py --production PRODUCTION_DOC <unit name> --resume <sessionId> --cwd <cwd> --plan <plan> --check`. On exit 2, stop and tell the user its line; the session remains running.
5. **Stop it.** Type `/exit` into its pane (`tmux send-keys -l`, then `Enter` as a separate call); a session outside tmux, ask the user to close. Relaunch only once `pid` is gone, since two processes on one session corrupt it. Then `tmux kill-session -t <its tmux session>`.
6. **Launch and record.** Run `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/add_unit.py --production PRODUCTION_DOC <unit name> --resume <sessionId> --cwd <cwd> --plan <plan>`. When step 2 found it was another showrunner's unit, run `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/showrunners.py remove <that showrunner's session> --unit <original ListAgents name>` after the launch succeeds.
7. **Tell the user** one line: `<unit name>` is now `<unit name>-unit`; `tmux attach -t <unit name>`. If the name changed, name the original ListAgents name too.
