---
description: Report screenshot episode time by method, agent, and project.
---

`$ARGUMENTS` accepts `--since DATE`, `--until DATE`, and `--project NAME`.

Run `python3 ~/.claude/scripts/shot_report/shot_report.py report $ARGUMENTS` and show its output. Dates without a time use America/Los_Angeles. If no saved episode file exists, run `python3 ~/.claude/scripts/shot_report/shot_report.py scan` once to write `episodes.jsonl`, then rerun the report.

`scan` reads `~/.cache/hana-shot/timings.jsonl` for per-view attempts. Use `scan --timings-path PATH` to read a different timing file.

The report shows one aligned row per method, agent, and project, with 5-minute and 15-minute columns next to each other for episode count, total hours, duration quantiles, screenshots, and other calls per episode. A second table gives kept-shot evidence for each split: one cited attempt, several cited attempts, none cited, no observable image path, and older episodes without citation data. It shows median and p90 attempts before the first kept shot with the number of episodes used. A shot is cited when its whole recorded path appears later in the same transcript in assistant text, a `SendMessage` or `codex_mesh.py --message` notice, a checkpoint notice, or a file the agent wrote. Matching timing records include failed views in the attempt count; older timing lines remain valid. The report then shows median and p90 minutes per image by method and agent at the 5-minute split, `/hana_shot` call and image totals, and the newest 20 sessions that took screenshots by hand. The window heading uses Pacific time and says that `n` counts episodes.
