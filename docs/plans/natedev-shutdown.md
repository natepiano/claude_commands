# natedev shutdown-unit phases

> **Production:** `docs/plans/natedev-production.md`
> **Unit:** shutdown-unit · branch `natedev-shutdown` · as-built `docs/as-built/shutdown-and-restart.md`

Earlier phases of this unit are recorded in the as-built and in the branch history. This plan holds Phase 4 and the follow-up it routed.

## Phase 4: herdr's shutdown items and the pane-shell stop — done

Landed in checkpoint `6cfdcbb1` (branch `f0f21af6`, main `fe789e33`, 2026-10-30). Showrunner decision B (02:34 EDT): land the stop fixes and herdr's items, with no pane-close step.

- herdr's items: F032 (record locks inside the 66 s stop limit), F045, F046 and F049 (restart timer restore). Their routes are closed.
- The herdr-unit transfer failure: a stop no longer targets the interactive pane shell, and it reports "M of N stopped". A stop retry never signals a session that resumed elsewhere. Cancel withdraws a failed stop whose session runs again.
- Transfer, rename and retire of a unit whose Claude runs under an interactive pane shell refuse before any stop, with one line that names the shell.
- Review: five closure passes, four repair rounds. Merge gate: full suite green.

## Phase 5: pane close for unit operations — done

**Goal:** transfer, rename and retire work for a unit whose Claude runs under an interactive pane shell. Today they refuse.

**Work Order.** Each item names its review finding (`phases/p4/` in the Phase 4 session folder holds the reviews and Astra's notes).

1. **Pane close.** One guarded helper closes the unit's own pane after a clean stop and worker cleanup. It is serialized with launch admission, rechecks the stop request and that the director is gone, and requires the exact session and pane, the shell's pid and start, and the unit's ownership. A foreign or unknown occupant refuses the close. Shared-session membership and shell foreground state are not proof (F008: a close must never come before a completed stop; F009: the scan before the close must refresh the director's exit).
2. **F003: a process in the unit's folder is a blocker, never permission to stop it.** Retire must not terminate a process only because its working directory is in the checkout. An unowned process there blocks retirement.
3. **F007: one pane-host fixture.** Extend `scripts/production/fake_unit_host.py` with session-qualified observations; `unit_operation_test_support.py` keeps only the failed-stop injection.
4. **F012: a stop that takes over a restoring timer keeps the restore intent.** At the one takeover decision in `restart.py` `_restore_timers`, under the owner's record lock, merge `was_enabled=True` into the covering stop's record before undoing the late enable. `settle.py` merges its timer snapshot by instance (OR of the flags), so publication order cannot lose the intent. Catch an undo failure inside the update and report it after.
5. **`restart --all` keeps stay-down stops down** (showrunner, 2026-10-30). `restart --all` skips a `then=stop` record only when its request was a single-session or `--only` stop, or a self-stop. It never skips a `down --all` record or a reboot entry. `restart --only` restores a skipped session.

**Acceptance.**
- Transfer, rename and retire of a pane-shell unit complete on fixtures: the stop finishes, only the unit's own pane closes, and the unit moves.
- Regressions, each failing before the change: a visitor or another unit's shell in the checkout is never signalled and blocks retirement, with and without a marked pane; a director that resumes before the close refuses the close; F012 runs real `settle.begin` in both publication orders, and the stop's later restart enables the timer, with an originally disabled timer as the control.
- Item 5: the morning path (`down --all`, a reboot, `restart --all`) restores every session; a single-session stay-down stop stays down, and `--only` brings it back.
- One live check, started by the showrunner: transfer of a throwaway pane-shell unit.
