# builds-resources — Next

## Items to consider

- [ ] **Memory pressure is sampled every 5–10 s, not only at each admission poll**
  - Target: `scripts/lint/memory_admit.py` (pressure reading), the buildlog sampler, `docs/as-built/build-memory-admission.md`
  - Why needed: the pressure hold (full avg10 > 5.0) is judged only from readings taken when a step polls, so the record has no steady avg10 series to tell how long holds last or to replay another limit against a freeze.
  - Completion condition: `/proc/pressure/memory` some and full avg10 are recorded every 5–10 s on natedev with a time stamp, kept for at least 7 days, and the pressure replay reads them; a test fakes the pressure file and checks the series.
  - Revealed by: the pressure hold question during Phase 16 (builds showrunner 2026-10-30: limit 5.0 stays, the sampler is `next`)
