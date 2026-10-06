# cache-evict

> **Production: build-followups** — unit `cache-evict-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-06 15:29 PDT

have that unit take on this question:

"Trial merge, not built: I trial-merged Phase 13 against the current work of trunk, the widget unit and fps. All three merge without conflicts. I didn't build or test those merges: the disk is at its 500 GB floor, and a second build folder would trigger the cleanup that deletes every unit's build caches. The notice says so."

i.e. figuring out the safest buld cache to delete in such situations so we don't simply get rid of everything but we maybe get rid of the least used to get us under our threshold
