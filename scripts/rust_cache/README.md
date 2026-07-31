# Rust compiler cache switch

Cargo always calls `rust-cache`; the saved backend decides whether the rustc
invocation goes through sccache or straight to rustc.

```bash
rust-cache use sccache
rust-cache use off
rust-cache status
rust-cache stats
```

Do not switch the saved backend while Cargo or rustc is running.

The cache store remains on disk across switches. `use off` stops and disables
the sccache LaunchAgent; it does not purge the cache.
