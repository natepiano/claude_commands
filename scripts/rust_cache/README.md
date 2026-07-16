# Rust compiler cache switch

Cargo always calls `rust-cache`; the saved backend decides which compiler cache
receives the rustc invocation.

```bash
rust-cache use sccache
rust-cache use kache
rust-cache use off
rust-cache status
rust-cache stats
```

Do not switch the saved backend while Cargo or rustc is running.

Both cache stores remain on disk. The switch stops and disables the unselected
backend's LaunchAgents; it does not purge either cache. Selecting kache
re-enables its hourly GC job, which may trim the store to kache's configured
`local_max_size`.
