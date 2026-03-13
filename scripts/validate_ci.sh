#!/usr/bin/env bash
set -euo pipefail

# Canonical local CI mirror for Nate's Rust repos.
# Variations:
# - In the cargo-mend repo, final step uses `cargo run -- --fail-on-warn`
# - In all other repos, final step uses installed `cargo mend`
# - If `benches/` exists, Criterion benches are run as a smoke check

REPO_NAME="$(basename "$PWD")"
IS_SELF_MEND=0
if [ "$REPO_NAME" = "cargo-mend" ]; then
  IS_SELF_MEND=1
fi

cargo +nightly fmt --all --check

taplo fmt --check

cargo clippy --workspace --all-targets --all-features -- -D warnings

cargo build --release --workspace --all-features --examples

cargo nextest run --workspace --all-features --tests

if [ -d benches ] && find benches -type f \( -name '*.rs' -o -name '*.bench' \) | grep -q .; then
  cargo bench --workspace --all-features
fi

if [ "$IS_SELF_MEND" -eq 1 ]; then
  cargo run -- --fail-on-warn
else
  cargo mend --fail-on-warn
fi
