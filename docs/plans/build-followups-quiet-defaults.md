# Quiet defaults: no timer or footer unless asked, and phase-note pruning on the snapshot timer

> **Status: IMPLEMENTATION PLAN — both phases built; the nix change waits on the showrunner and the user's rebuild.**

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`, `docs/as-built/session-notifier.md`, `docs/as-built/phase-tables.md`

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-13, the user through hanadocs and the showrunner: "Yes do the part C", read as yes to both parts:

- **(b)** New productions and unit runs get no report timer and no reply footer by default. Each stays available on request (`/showrunner:report_interval`, `/showrunner:footer on`, `/unit:report on`). Existing productions keep what they have.
- **(c)** Phase-note pruning moves off the dailies to the job `agent-sessions-snapshot.timer` runs.

## What is there now

- **Production updates.** `update_registration.py register` reads the doc's `**Updates:**` line. `on demand` makes the `showrunner-<slug>` instance at 15 minutes and stops it, because the instance is the showrunner's only record. A missing line means every 15 minutes. `docs/production_format.md` gives `every <N> minutes (default 15), or on demand`.
- **Production footers.** `showrunner_footer.py` reads a footer as on unless `~/.local/state/showrunner/footers-off/<slug>` exists. An empty file is off on purpose; a pause writes its own mark, which a resume clears.
- **Unit runs.** `unit_notifier.sh` reads `PLAN_DELEGATE_PROGRESS_UPDATES` from `config/delegate.conf`: `scheduled` (the shipped value, also the value when the line is missing) or `on-demand`. On demand, a resumed run's schedule is stopped. Unit directors have no reply footer, so (b) has no footer to change for them.
- **Pruning.** `dailies_input.py run` calls `phase_table.py prune --production-doc <doc>` before it reads anything. That moves the phase notes of units no longer in the Units table into the vault's archive. It runs only when a dailies is built.
- **The snapshot timer.** `/etc/nixos/modules/linux/agent-sessions.nix` runs `agent-sessions-snapshot.py` every 2 minutes on natedev. Its first step, `refresh_agent_notes`, runs `~/.claude/scripts/whoami/agent_notes.py` as its own process, so a failure there can never cost the session record.
- **Live now (read 08:50 EDT).** All four registered productions read `on demand` and have their footers off, so (b) changes nothing that runs today.

## Phase 1: quiet by default (b) — done

**Productions.**

- `update_registration.py`: a missing `**Updates:**` line means on demand. An `every N minutes` line still schedules.
- `register`: when no `showrunner-<slug>` instance exists before it runs, the production is new, and `register` writes its footer switch off on purpose, the empty file. A restart finds its instance and leaves the switch alone, so existing productions keep what they have.
- `register` reports it as `footer: off — new production; /showrunner:footer on turns it on`.
- Docs:
  - `docs/production_format.md`: the Updates line defaults to `on demand`, and `every <N> minutes` is given only when asked for.
  - `commands/showrunner/produce.md`: the start paragraph and the Footer bullet.
  - `commands/showrunner/report_interval.md` and `commands/showrunner/footer.md`: one sentence each, saying they turn the scheduled updates and the footer on for a new production.

**Unit runs.**

- `config/delegate.conf`: `PLAN_DELEGATE_PROGRESS_UPDATES=on-demand`.
- `unit_notifier.sh`: a missing line also means `on-demand`. An on-demand start leaves a run's existing instance as it is, so a run started on a schedule, or turned on with `/unit:report on`, keeps it through a resume.
- Docs: `config/README.md`, `commands/unit/direct.md` (the progress-updates paragraph), and `commands/unit/report.md` if its text names the default.

**Tests.**

- A new production with no Updates line gets a stopped instance and its footer off.
- A production restarting with an instance keeps its footer state, on or off.
- A doc with `every 20 minutes` still schedules.
- `unit_notifier.sh`: with no config line it creates no instance; with an instance already running and on-demand set, it leaves that instance running.
- Existing expectations in `test_update_registration.py`, `test_delegate_check.py` and the unit notifier tests that assume `scheduled` get updated.

## Phase 2: pruning on the snapshot timer (c) — done

- **New, `scripts/production/prune_phase_notes.py`.** For every update timer's production (each `showrunner-*` instance's checked doc, running or not; no session record is read), it runs `phase_table.prune(<its doc>)` in-process. It is one process every 2 minutes, not one per production. A production whose doc cannot be read or whose prune refuses gets one stderr line, and the others still run. It prints nothing on success and exits 1 when any production failed; `phase_table.prune` returns nothing, so there is no archived count to print. It uses only the standard library, because the timer runs it with nix's `python3`.
- **`dailies_input.py`.** It loses `prune_phase_tables`, `PhaseTablesPruned` and `PrunePhaseTablesState`, and its tests lose their prune expectations. A dailies then makes no write before its checks.
- **The nix change.** It is the showrunner's to apply in `/etc/nixos`, and a rebuild is the user's. The change goes in `modules/linux/agent-sessions-snapshot.py`, the script the timer runs, not the `.nix` module, which needs no change. The exact lines go in the phase notice: a `prune_phase_notes()` beside `refresh_agent_notes()`, built the same way (its own process, a 60-second timeout, failures to stderr), called second in `main()`.
- **Order.** `dailies_input.py` stops pruning only once the timer prunes. The phase merges first, with the nix change applied and rebuilt after it, so for that gap nothing prunes. That costs only stale notes sitting in the vault, no lost data, and the next timer run after the rebuild catches up.
- **The Mac** has no snapshot timer, so after this phase nothing prunes on the Mac. The vault's phase notes are written on whichever machine runs the unit. The notice states this; a Mac job is a separate follow-up if the user wants one.
- **Tests.**
  - With stand-in productions and a scratch vault (`PHASE_TABLE_VAULT`, `NOTIFIER_STATE_DIR`): a retired unit's note is archived and a live unit's note stays.
  - A production with an unreadable doc is reported while the next production is still pruned, and the exit is 1.
  - Nothing to prune prints nothing and exits 0.
  - `dailies_input.py run` leaves a retired unit's note in place.
- **Docs.**
  - `docs/as-built/phase-tables.md`: the caller table, Order and Failure.
  - `docs/as-built/showrunner-automation.md`: one line.
  - `commands/showrunner/dailies.md`: wherever it says a dailies prunes.

## Ownership

| File | Owner | Phase |
| --- | --- | --- |
| `scripts/production/update_registration.py`, its test | enh-showrunner-unit | 1 |
| `commands/showrunner/produce.md`, `report_interval.md`, `footer.md` | enh-showrunner-unit | 1 |
| `docs/production_format.md` | model-study (also touches) | 1 |
| `scripts/delegate/unit_notifier.sh`, `config/delegate.conf`, `config/README.md`, `commands/unit/direct.md`, `commands/unit/report.md`, their tests | model-study (also touches) | 1 |
| `scripts/production/prune_phase_notes.py`, its test | enh-showrunner-unit (new) | 2 |
| `scripts/production/dailies_input.py`, its test, `commands/showrunner/dailies.md` | enh-showrunner-unit | 2 |
| `scripts/delegate/phase_table.py` | model-study; read only, not changed | 2 |
| `/etc/nixos/modules/linux/agent-sessions-snapshot.py` | the showrunner applies; the user rebuilds | 2 |

No phase touches `scripts/shutdown/*`, so neither waits on shutdown-unit's phases 8-9.

## Gate, every phase

- basedpyright on scripts/agents, production, build_hold, message, hooks, whoami, delegate, mac_test, lib and build_targets ends 0/0/0.
- These suites pass: production, message, build_hold, hooks, agents, delegate, whoami, mac_test, lib and build_targets.
