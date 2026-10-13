# Migration dependency check: every workspace member's dependencies, each looked up by its whole version

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/bevy_migration_plan`'s dependency check lists the direct dependencies of every crate in a workspace, and asks crates.io about each one once, under its whole version, never faster than one request a second. The scheduled upgrade watch keeps to the same limit through the same pacer.

> **As-built disposition: amend** — `docs/as-built/bevy-upgrade.md`

> **Production: build-followups** — unit `bevy-upgrade-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 21:24 EDT

The first live run of the dependency check against hana_catalyst for Bevy 0.20.0 gave two wrong results. The unit reported both with their measured causes, and the showrunner answered:

> (a), yes. Do the one follow-up phase: `--no-dedupe` for the direct list, keep build metadata in the version you look up, a regression test for each, rerun the live check on hana_catalyst, and correct docs/as-built/bevy-upgrade.md. Send me the plan at docs/plans/build-followups-bevy-upgrade-next.md first.

On the plan, the showrunner approved it with one change:

> The 80 requests: `--no-dedupe` repeats a crate under every member that uses it, so dedupe by name + whole version before any lookup, and request each crate once. If what remains still sends faster than crates.io's published crawler limit (one request per second), pace it to that. Add a test that a crate two members share is looked up once.

Phase 1's review found that the scheduled upgrade watch sends crates.io about 40 requests in a row with no pause. The unit sent that to the showrunner as an add-on, and the showrunner answered at 21:47 EDT:

> current, yes. Phase 2 runs before the as-built. Use the migration check's pacer, moved where both scripts can import it, not a second copy, with its test kept and one test that the watch goes through it. Suites: bevy_upgrade and bevy_migration_plan.

## Delegation Context

- **Project:** `~/.claude` tooling (this repo) — the dependency check `/bevy_migration_plan` runs before a Bevy upgrade, and the scheduled Bevy upgrade watch.
- **Project started:** 2026-10-13T01:26:51.404+00:00
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-bevy-upgrade`, branch `build-followups-bevy-upgrade`. Every edit and every command runs there (the user, 2026-10-12).
- **Stack:** Python 3.11+ standard library only (`re`, `subprocess`, `time`, `urllib.request`, `urllib.parse`, `json`, `unittest`).
- **Layout:** `scripts/bevy_migration_plan/` holds the check and its test module. `scripts/bevy_upgrade/` holds the upgrade watch, one module per concern, each with a `test_<module>.py` beside it. A script that imports from another directory under `scripts/` puts that directory on `sys.path` first, as `scripts/bevy_upgrade/bevy_upgrade.py` does for `scripts/production`, and `pyrightconfig.json` lists the same directory under that root's `extraPaths`.
- **Key files:**
  - `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the check. `RequestPacer`, `CRATES_IO_MIN_INTERVAL_SECONDS` and `CRATES_IO_PACER` sit at its top; `query_crates_io` and `get_bevy_dependency_requirement` are the two functions that reach crates.io.
  - `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — one `unittest.TestCase`; its `setUp` replaces `subprocess.run` with a function that fails on any cargo command, `request.urlopen` with the class's own `urlopen` method, which answers from canned data, and the pacer with one on a stand-in clock.
  - `scripts/bevy_upgrade/upstream.py` — the watch's lookups. `http_fetch(url: str) -> bytes` is the only function that opens a network request, for crates.io and for GitHub alike; every lookup takes a `Fetch` (`Callable[[str], bytes]`) and the CLI passes `http_fetch` unless a fixtures file is named.
  - `scripts/bevy_upgrade/test_upstream.py` — `UpstreamTests`; its `http_fetch` tests replace `urllib.request.urlopen` and use addresses under `https://crates.io/api/v1/crates`.
  - `pyrightconfig.json` — one `executionEnvironments` entry per script directory that imports across directories.
- **Test lanes:** each test module sits beside the module it tests, in `scripts/bevy_migration_plan/` and `scripts/bevy_upgrade/`.
- **Build:** none.
- **Test:** two suites, each its own command: `python3 -m unittest discover -s scripts/bevy_migration_plan -p 'test_*.py'` (seconds) and `python3 -m unittest discover -s scripts/bevy_upgrade -p 'test_*.py'` (about a minute: its CLI tests start subprocesses).
- **Lint:** `basedpyright scripts/bevy_migration_plan scripts/bevy_upgrade` prints `0 errors, 0 warnings, 0 notes` (it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has; read the counts).
- **Smoke:** the unit director runs it, never a seat, because it runs cargo on another project. From a scratch home whose `rust/bevy-0.20.0` links to `/home/natepiano/rust/bevy`: `HOME=<scratch> CARGO_HOME=/home/natepiano/.cargo CARGO_NET_OFFLINE=true python3 scripts/bevy_migration_plan/bevy_migration_dependency_check.py --bevy-version 0.20.0 --codebase /home/natepiano/rust/hana_catalyst --output <scratch file>`. It passes when the report has a row for `bevy_tween` and one for `bevy_lookup_curve`, still has `moonshine-save`, and has no row for `toml`. For the watch, the unit director sends three crates.io lookups and one GitHub lookup in a row through the real `http_fetch`: it passes when all four answer, the three crates.io requests take at least two seconds together, and the GitHub one adds no wait.
- **Invariants:**
  - Tests never run cargo and never reach the network. The `setUp` stand-ins stay in force; a test that needs different behaviour replaces one stand-in for that test only.
  - Tests never wait on the clock: every test class that can reach a crates.io request replaces the shared pacer in `setUp` with one driven by a stand-in clock and a stand-in sleep.
  - One pacer: `RequestPacer` and the shared instance are defined in one module, and nothing copies or re-exports them.
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

### Phase 2 — The upgrade watch sends crates.io at most one request a second, through the same pacer as the migration check  · status: done

#### Work Order

**Goal:** The scheduled upgrade watch starts its crates.io requests at least one second apart, as the migration check does, and both scripts take that pacing from one module.

**Spec:**
Work only in the worktree and branch the Delegation Context names. The showrunner asked for this phase on 2026-10-12, in the words the Source section quotes.

`scripts/bevy_upgrade/crates_io_pacer.py` (new):

- It holds the three definitions that sit at the top of the migration check today, moved as they are, docstring included:

```python
CRATES_IO_MIN_INTERVAL_SECONDS: float = 1.0

class RequestPacer:
    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None: ...
    def wait(self) -> None: ...

CRATES_IO_PACER = RequestPacer(CRATES_IO_MIN_INTERVAL_SECONDS)
```

- `RequestPacer.wait` keeps its behaviour to the letter: it returns at once on its first call; a later call sleeps for what is left of `min_interval` since the previous request started, then records the clock reading taken after the sleep.
- The module starts with `from __future__ import annotations` and a one-line docstring, like its neighbours, and says in a comment or the docstring where the one-a-second figure comes from: crates.io's published limit for a crawler.

`scripts/bevy_migration_plan/bevy_migration_dependency_check.py`:

- It no longer defines `CRATES_IO_MIN_INTERVAL_SECONDS`, `RequestPacer` or `CRATES_IO_PACER`, and it does not re-export them. Drop the imports that only they used.
- After its standard-library imports it puts the watch's directory on the path and imports the module, in the idiom `scripts/bevy_upgrade/bevy_upgrade.py` uses for `scripts/production`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bevy_upgrade"))

import crates_io_pacer
```

- `query_crates_io` and `get_bevy_dependency_requirement` call `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before `request.urlopen`, reading the attribute at call time, so a test can replace it.

`scripts/bevy_upgrade/upstream.py`:

- It imports `crates_io_pacer`, a sibling module.
- `http_fetch` calls `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before `urllib.request.urlopen` when the address's host is `crates.io`, and only then. `urlsplit(url).hostname` already gives the host. A request to `api.github.com` or `raw.githubusercontent.com` is never paced: GitHub has its own limit and the watch already keeps to it.
- Nothing else in the watch changes. `fixture_fetch` never reaches `http_fetch`, so a run from a fixtures file does not wait.

`pyrightconfig.json`:

- Add one `executionEnvironments` entry, before the closing `{"root": "."}` entry: root `scripts/bevy_migration_plan`, `extraPaths` `scripts/bevy_migration_plan` and `scripts/bevy_upgrade`. The `scripts/bevy_upgrade` entry already lists its own directory, where the new module lives.

**Files:**
- `scripts/bevy_upgrade/crates_io_pacer.py` — new: the interval, `RequestPacer` and the shared `CRATES_IO_PACER`.
- `scripts/bevy_upgrade/upstream.py` — `http_fetch` paces a crates.io request.
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — imports the pacer module; its own copy is gone.
- `pyrightconfig.json` — the execution environment that lets the check's directory resolve the import.
- `scripts/bevy_upgrade/test_crates_io_pacer.py` — new: the five `RequestPacer` tests, moved from the check's test module with a stand-in clock.
- `scripts/bevy_upgrade/test_upstream.py` — `setUp` installs a stand-in pacer; the tests that the watch goes through it.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — replaces the pacer at its new home; the five `RequestPacer` tests leave.

**Seats:** 1 writer + 1 tester — one moved class, two callers, and their test modules.
- `impl` — `scripts/bevy_upgrade/crates_io_pacer.py`, `scripts/bevy_upgrade/upstream.py`, `scripts/bevy_migration_plan/bevy_migration_dependency_check.py`, `pyrightconfig.json`.
- `test` — `scripts/bevy_upgrade/test_crates_io_pacer.py`, `scripts/bevy_upgrade/test_upstream.py`, `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py`, from the Spec.

**Constraints from prior phases:**
- The watch's check has a time budget: `run_check` in `scripts/bevy_upgrade/upgrade_check.py` reads the clock before each fetch and stops asking once `CHECK_BUDGET` (180 seconds) is spent. A pacer sleep happens inside the fetch, so it counts against that budget and no budget code changes. The scheduled job's limit is 300 seconds: the budget, one request's 30 second timeout and one pacer sleep fit inside it.
- The watch sends about 40 crates.io requests for hana_catalyst, so pacing adds about 40 seconds to a check that nothing waits on.
- The pacer records the clock reading after its sleep, never the planned wake time; the test whose stand-in sleep runs long guards that and moves with the others.
- `commands/bevy_migration_plan.md` has an uncommitted edit by the unit director. Leave it as it is; it is committed with this phase.
- The check's test module replaces the pacer in `setUp` with `mock.patch.object(checker, "CRATES_IO_PACER", ...)` and two of its tests patch `checker.CRATES_IO_PACER`'s `wait`. After this phase the object to patch is `crates_io_pacer.CRATES_IO_PACER`.

**Acceptance gate:** both Delegation Context **Test** commands and the **Lint** command pass. The tests cover:
- The five `RequestPacer` tests pass in `scripts/bevy_upgrade/test_crates_io_pacer.py` against `crates_io_pacer.RequestPacer`, with their assertions unchanged: first call does not sleep; a call 0.25 seconds after the previous start sleeps 0.75 seconds; a call after more than the interval does not sleep; three calls with no time passing sleep twice, one second each; a sleep that runs long does not shorten the next gap.
- `http_fetch` on a crates.io address calls the shared pacer's `wait` once, before `urllib.request.urlopen`. The test records the order of the two calls.
- `http_fetch` on an `api.github.com` address and on a `raw.githubusercontent.com` address does not call the pacer.
- Two crates.io `http_fetch` calls in a row, under a pacer on a stand-in clock, sleep once for one second.
- `UpstreamTests.setUp` installs a pacer with a stand-in clock and sleep, so its existing `http_fetch` tests never wait on the real clock.
- The check's tests pass with the pacer replaced at `crates_io_pacer.CRATES_IO_PACER`: `query_crates_io` and `get_bevy_dependency_requirement` each still call it once per request, before the request.
- No name `RequestPacer`, `CRATES_IO_PACER` or `CRATES_IO_MIN_INTERVAL_SECONDS` is defined in `bevy_migration_dependency_check.py`.
