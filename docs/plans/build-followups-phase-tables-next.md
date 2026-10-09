# phase-tables — Next

## Items to consider

- [ ] **The dailies count a phase's repair rounds from the run's records**
  - Target: `scripts/production/dailies_input.py` (`eta.fixes`), fed by `scripts/delegate/phase_table.py`
  - Why needed: after the dailies phase, `eta.fixes` is the one ETA number the showrunner still types; the recorder already holds each repair pass and the first stated ETA's time.
  - Completion condition: a dailies input built with no `eta.fixes` in the judgment carries the count of repair rounds started after the phase's first stated ETA.
  - Revealed by: Phase 1

- [ ] **A wrapped production's notes are archived**
  - Target: `scripts/delegate/phase_table.py` (an archive command for one production's notes), run by `scripts/production/production_lifecycle.py` (`wrap`)
  - Why needed: nothing moves a finished production's notes out of the way, and a later unit that takes the same session name under another production is refused at every report because the target is another unit's note. The user wants the phases kept in the vault rather than deleted (2026-10-09).
  - Completion condition: after a wrap, each generated note of that production sits under an archive folder named for it (for example `showrunners/<showrunner>/archive/<production>/<session>.md`), notes written by hand are untouched, and a unit of another production with the same session name gets a fresh note at its first report.
  - Revealed by: Phase 2
