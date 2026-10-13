# Migration dependency check: every workspace member's dependencies, each looked up by its whole version

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/bevy_migration_plan`'s dependency check lists the direct dependencies of every crate in a workspace, and asks crates.io about each one once, under its whole version, never faster than one request a second.

> **As-built disposition: amend** — `docs/as-built/bevy-upgrade.md`

> **Production: build-followups** — unit `bevy-upgrade-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 21:24 EDT

The first live run of the dependency check against hana_catalyst for Bevy 0.20.0 gave two wrong results. The unit reported both with their measured causes, and the showrunner answered:

> (a), yes. Do the one follow-up phase: `--no-dedupe` for the direct list, keep build metadata in the version you look up, a regression test for each, rerun the live check on hana_catalyst, and correct docs/as-built/bevy-upgrade.md. Send me the plan at docs/plans/build-followups-bevy-upgrade-next.md first.

On the plan, the showrunner approved it with one change:

> The 80 requests: `--no-dedupe` repeats a crate under every member that uses it, so dedupe by name + whole version before any lookup, and request each crate once. If what remains still sends faster than crates.io's published crawler limit (one request per second), pace it to that. Add a test that a crate two members share is looked up once.

## Delegation Context

- **Project:** `~/.claude` tooling (this repo) — the dependency check `/bevy_migration_plan` runs before a Bevy upgrade.
- **Project started:** 2026-10-13T01:26:51.404+00:00
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-bevy-upgrade`, branch `build-followups-bevy-upgrade`. Every edit and every command runs there (the user, 2026-10-12).
- **Stack:** Python 3.11+ standard library only (`re`, `subprocess`, `time`, `urllib.request`, `urllib.parse`, `json`, `unittest`).
- **Layout:** `scripts/bevy_migration_plan/` holds the check and its test module. Nothing else changes.
- **Key files:**
  - `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — `run_cargo_tree` (line 177), `get_all_direct_dependencies` (line 215; its line pattern is at line 246), `get_bevy_dependency_requirement` (line 323; it builds the lookup address in its first statement).
  - `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — one `unittest.TestCase`; its `setUp` (line 42) replaces `subprocess.run` with a function that fails on any cargo command and `request.urlopen` with the class's own `urlopen` method, which answers from canned data.
- **Test lanes:** `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py`, beside the module.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s scripts/bevy_migration_plan -p 'test_*.py'`
- **Lint:** `basedpyright scripts/bevy_migration_plan` prints `0 errors, 0 warnings, 0 notes` (it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has; read the counts).
- **Smoke:** the unit director runs it, never a seat, because it runs cargo on another project. From a scratch home whose `rust/bevy-0.20.0` links to `/home/natepiano/rust/bevy`: `HOME=<scratch> CARGO_HOME=/home/natepiano/.cargo CARGO_NET_OFFLINE=true python3 scripts/bevy_migration_plan/bevy_migration_dependency_check.py --bevy-version 0.20.0 --codebase /home/natepiano/rust/hana_catalyst --output <scratch file>`. It passes when the report has a row for `bevy_tween` and one for `bevy_lookup_curve`, still has `moonshine-save`, and has no row for `toml`.
- **Invariants:**
  - Tests never run cargo and never reach the network. The `setUp` stand-ins stay in force; a test that needs different behaviour replaces one stand-in for that test only.
  - Tests never wait on the clock: `setUp` replaces the module's request pacer with one driven by a stand-in clock and a stand-in sleep.
  - No project data in the code or the tests: test crates carry invented names.
  - Python rules (CLAUDE.md): annotate every signature; `TypedDict` for JSON with known keys; no file-level ignores; a line-level `# pyright: ignore[reportAny]` only where `json` or `urllib` returns `Any`.

## Phases

### Phase 1 — The check lists every workspace member's direct dependencies and looks each one up by its whole version  · status: done

#### As-built

- `run_cargo_tree` runs `cargo tree --workspace --no-dedupe --depth=1 --format {p}`, with `--all-features` first and the default features when that fails; both attempts carry the two flags. Cargo prints a package it has already printed as `name vX (*)` with no children, so without `--no-dedupe` a member that an earlier member depends on hides its own direct dependencies. `--workspace` covers a root manifest that is also a package, which cargo otherwise lists alone.
- `get_all_direct_dependencies` reads each line with `r'([a-zA-Z0-9_-]+)\s+v(\d\S*)'`: the version is the whole token after `v`, so `1.1.6+spec-1.1.0`, `0.20.0-rc.1` and `0.5.0-dev` are kept whole and a trailing ` (proc-macro)` or path is left out. Its set of `(name, version)` pairs returns a crate that several members list at one version once, so that crate is looked up once.
- `get_bevy_dependency_requirement` percent-encodes the version in the address with `parse.quote(version, safe='')`: `1.1.6+spec-1.1.0` is requested as `.../crates/<name>/1.1.6%2Bspec-1.1.0/dependencies`. The crate name is not encoded. The whole version reaches `classify_dependency`'s equality comparison and the report row, so a crate already on the compatible version, suffix included, is `OK`.
- crates.io requests start at least one second apart:

```python
CRATES_IO_MIN_INTERVAL_SECONDS: float = 1.0
RequestPacer(min_interval: float, clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep)
RequestPacer.wait() -> None
CRATES_IO_PACER = RequestPacer(CRATES_IO_MIN_INTERVAL_SECONDS)
```

- `wait` returns at once on its first call. A later call sleeps for what is left of `min_interval` since the previous request started, then records the clock reading taken after the sleep. `query_crates_io` and `get_bevy_dependency_requirement`, the only two functions that reach crates.io, call `CRATES_IO_PACER.wait()` immediately before `request.urlopen` and read the module attribute at call time, so a test can replace it.
- `commands/bevy_migration_plan.md` tells the runner to run the check in the background and wait, and how many requests a run sends.
- Measured on 2026-10-12 against hana_catalyst for Bevy 0.20.0: exit 0, 129 seconds, 45 crates asked about, every request answered. `bevy_tween` 0.13.0 and `bevy_lookup_curve` 0.12.0 are listed, both as blockers; `moonshine-save` is still listed; `toml` is not.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the check: the cargo command, the version pattern, the encoded lookup address, `RequestPacer` and its two calls.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — its tests; `setUp` installs a `RequestPacer` on a stand-in clock and sleep, so no test waits on the real clock.
- `commands/bevy_migration_plan.md` — the command that runs the check; carries the time allowance.

**Gotchas:**
- The pacer records the clock reading after its sleep, never the planned wake time. With the planned time, a sleep that runs 0.6 seconds long gives request starts at 0.0, 1.6 and 2.0 seconds. A stand-in sleep that advances the clock by the amount asked for cannot show this; the regression test's stand-in runs long.
- Run time follows the request count, not the dependency count: one request per direct dependency, then for each one that depends on Bevy one for its crates.io entry and up to 20 for its recent releases. A blocker spends all 20.
- `--no-dedupe` repeats a shared crate under every member that uses it; only the set in `get_all_direct_dependencies` keeps that to one lookup.
- crates.io answers 404 for `/api/v1/crates/toml/1.1.6/dependencies` and 200 for `/api/v1/crates/toml/1.1.6%2Bspec-1.1.0/dependencies` (measured 2026-10-12): a version cut off at its suffix reads as a failed lookup and a `CHECK_NEEDED` row.
- `scripts/bevy_upgrade/upstream.py` `release_requirement` encodes the version the same way, so the two copies of the lookup address agree. That module does not pace its requests.

**Ruled out:**
- Recording the pacer's planned wake time: a long sleep then shortens the next gap.
- Leaving `commands/bevy_migration_plan.md` without a time allowance: the paced run is longer than a default 120 second command limit.
