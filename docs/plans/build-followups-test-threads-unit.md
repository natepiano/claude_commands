# test-threads-unit

> **Production: build-followups** — unit `test-threads-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-09 16:47 PDT

User, 2026-10-09 16:4x PDT, approving the nightly 2026-10-08 rust main proposal (~/.local/state/nightly-review/2026-10-08/rust.md: run hana tests at one nextest process per physical core, --test-threads 16 on natedev, Mac 12, so the memory gate's per-step reservation falls from 11.5 GiB): "i'm okay with implementing this as long as you put in a measurement regime - so it may require its own unit director to take care of this and then set up the measurement regime that proves or disproves whether it works"
