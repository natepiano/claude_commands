# Production — guilds

> **Status: PRODUCTION — running.** Four lasting guild heads maintain tips and read daily member evidence.

## Production Context

- **Source plans:** `docs/as-built/guilds.md` — Guilds and heads, Daily read; G1–G10 in `docs/plans/sdlc-production.md`, Review decisions.
- **Repository:** `~/.claude`
- **Merge branch:** `guilds`
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-guilds-trunk`
- **Showrunner session:** guilds
- **Log:** `guilds-production.log`
- **Updates:** on demand
- **Merge tests:** `python3 -m unittest discover -s scripts/guilds -p test_*.py`
- **Capacity:** four Claude heads on natedev; these standing loops run no builds or workers.
- **Change review:** `~/rust/hanadocs/change-review/change-review - guilds.md`

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| designers | (retired 2026-10-28, worktree removed, session 78d3a48b-b86d-4952-b150-401507c8dbe1) `docs/plans/guilds-designers.md` | `/home/natepiano/worktrees/claude-guilds-designers` | `guilds-designers` | — | `~/rust/nate_style/guilds/designers/`; `~/rust/nate_style/guilds/designers_base/`; common tips tagged only `head/designers` |
| reviewers | `docs/plans/guilds-reviewers.md` | `/home/natepiano/worktrees/claude-guilds-reviewers` | `guilds-reviewers` | — | `~/rust/nate_style/guilds/reviewers/`; `~/rust/nate_style/guilds/reviewers_base/`; common tips tagged only `head/reviewers` |
| showrunners | `docs/plans/guilds-showrunners.md` | `/home/natepiano/worktrees/claude-guilds-showrunners` | `guilds-showrunners` | — | `~/rust/nate_style/guilds/showrunners/`; `~/rust/nate_style/guilds/showrunners_base/`; common tips tagged only `head/showrunners` |
| units | `docs/plans/guilds-units.md` | `/home/natepiano/worktrees/claude-guilds-units` | `guilds-units` | — | `~/rust/nate_style/guilds/units/`; `~/rust/nate_style/guilds/units_base/`; common tips tagged only `head/units` |

These are unit directors running standing plans, with the exact unsuffixed IDs above. Heads belong to the units guild. Worker IDs keep their current names.

## Hub files

No shared implementation files. Common tips belong to the head named by their sole `head/` tag; only that head publishes changes to them.

## Gates

No phased gates or checkpoints for the standing loops. Launch from the landed Phase 5 docs and reader; retain ordinary gates for other productions.

## Close-out

No automatic wrap. Stop a head or timer only on an explicit instruction; retain tips, daily reports and unsent evidence.

## Production rules

1. Runs on natedev under sdlc in the showrunner tree; its herdr space stays top level (the user, 2026-10-28: attach). `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py parent guilds` prints sdlc.
2. Use herdr space `guilds`: showrunner in tab 1, then `designers`, `reviewers`, `showrunners`, `units`, in that order. Start each from its Units row with `/showrunner:add_unit <id> --plan <Plan cell>`. Resolve existing marks before launch; resume missing heads and verify tab order after a restart. Use no goddess names (G7).
3. Resolve each head afresh with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --json`: exactly one natedev entry with production `guilds`, role `unit director`, and the matching unit ID. Missing or multiple entries mean no send; resolve the lookup problem before scheduling. Do not cache an address across restarts.
4. Own four timers: `guilds-designers-daily`, `guilds-reviewers-daily`, `guilds-showrunners-daily`, `guilds-units-daily`. For each resolved Claude `session:<id>`, run `~/.claude/scripts/message/notifier.sh new guilds-<guild>-daily --every 1440 --to session:<id> --from guilds --command "Daily guild check-in: cover the uncovered interval under your standing plan; a duplicate check-in does no second read."`, then `notifier.sh start guilds-<guild>-daily`. Inspect `status` first. After a head restart, stop and remove its stale timer, resolve the current session, recreate and start it; leave other timers alone. Verify all four targets and intervals. Never wake Codex members for a check-in.
5. Relay the user's guild feedback to that guild's head with sender and context (G4). Report the heads' evidence, tip paths and pushed commits, declined reports, process proposals and missing host coverage. A queued message is not delivered.
6. Heads own tips, not assignments, acceptance, model choices or gates. A process proposal goes from its head through guilds to sdlc for `guild-process-unit` (G9); no head changes a user rule. The daily reader selects evidence and adds no stop rule.

Source: `~/.claude/docs/as-built/guilds.md`, Guilds and heads / Daily read; G1–G10 in `docs/plans/sdlc-production.md`, Review decisions.
