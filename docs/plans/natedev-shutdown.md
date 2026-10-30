# natedev shutdown-unit phases

> **Production:** `docs/plans/natedev-production.md`
> **Unit:** shutdown-unit · branch `natedev-shutdown` · as-built `docs/as-built/shutdown-and-restart.md`

Earlier phases of this unit are recorded in the as-built and in the branch history. This plan holds Phase 4 and Phase 5, the follow-up Phase 4 routed.

## Phase 4: herdr's shutdown items and the pane-shell stop — done

Landed in checkpoint `6cfdcbb1` (branch `f0f21af6`, main `fe789e33`, 2026-10-30). Showrunner decision B (02:34 EDT): land the stop fixes and herdr's items, with no pane-close step.

- herdr's items: F032 (record locks inside the 66 s stop limit), F045, F046 and F049 (restart timer restore). Their routes are closed.
- The herdr-unit transfer failure: a stop no longer targets the interactive pane shell, and it reports "M of N stopped". A stop retry never signals a session that resumed elsewhere. Cancel withdraws a failed stop whose session runs again.
- Transfer, rename and retire of a unit whose Claude runs under an interactive pane shell refuse before any stop, with one line that names the shell.
- Review: five closure passes, four repair rounds. Merge gate: full suite green.

## Phase 5: pane close for unit operations — done

Landed in checkpoint `3c24f9de` (branch `dcca53de`, 2026-10-30); the showrunner holds the merge until the user's morning restart test.

- Transfer, rename and retire now complete for a unit whose Claude runs under an interactive pane shell. One guarded close (`unit_move.close_unit_pane`) closes only the unit's exact pane, after a clean stop and worker cleanup, under launch admission and the shutdown-record lock. A foreign, unknown or nested occupant of any UID refuses it; herdr's one ancestor-aware lookup (`herdr.pane_scopes`) finds the pane scope.
- A process in the unit's checkout blocks retirement and is never stop authority (F003). `fake_unit_host` is the one pane-host fixture (F007).
- A stop that takes over a restoring timer keeps the restore intent (F012).
- `restart --all` keeps a single-session, `--only` or self stay-down stop down, never a `down --all` or reboot entry; a `down --all` takeover records account scope, so the morning path restores every session.
- Add-on: the Mac offload test wrapper publishes its state files atomically.
- Review: two passes, two repair rounds. Gate: every suite passed across two runs; each run had one red outside this phase (memory cap, `test_codex_mesh` hang), held by the showrunner for devtools and sdlc fixes. The Phase 4 routes stay open until sdlc's `close-route` fix lands.
- Live check, started by the showrunner: transfer of a throwaway pane-shell unit.
