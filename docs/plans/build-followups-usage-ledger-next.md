# Usage ledger: token use by role and model, kept over time — Next

## Items to consider

- [ ] **Periodic app-server sweep on natedev and the Mac**
  - Target: a 30-minute timer (a systemd user timer on natedev, launchd on the Mac) running `codex_mesh.py sweep --root /tmp/claude/delegate --stop`
  - Why needed: each app-server's own watcher already stops it once unused (`idle-stop`, shipped 2026-10-08); a periodic sweep covers a server whose watcher is missing or has exited (5 orphans found on 2026-10-10)
  - Completion condition: an orphan older than the idle window is gone within 30 minutes on both machines, and no live run's server is stopped
  - Revealed by: Phase 2

- [ ] **A custom metric that looks at every turn separately prints as fast as the built-in ones**
  - Target: `scripts/usage/usage_report.py`, the general per-turn path
  - Why needed: such a metric takes 8 to 15 s on natedev's 1.2 million turns, against 2.3 to 2.8 s for every built-in metric and a 10 s limit; none of the nine built-in metrics uses that path today
  - Completion condition: a per-turn metric on the full store prints well inside the 10 s limit
  - Revealed by: Phase 7

- [ ] **The dailies show the day's usage by role and model**
  - Target: `scripts/production/dailies_render.py` (the showrunner's area); waits until the user asks for a usage section, because the dailies are kept minimal by the user's rules
  - Why needed: the user asked to track model stats across showrunners, reviewers, unit directors and workers; the dailies show the live account meters and no token figure
  - Completion condition: the dailies print a short usage-by-role table read from the store, marked stale when a source was not read in full
  - Revealed by: Phase 7
