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

#### As-built

- `scripts/bevy_upgrade/crates_io_pacer.py` is the one home of the pacing. It holds `CRATES_IO_MIN_INTERVAL_SECONDS`, `RequestPacer` and the shared `CRATES_IO_PACER`, with the signatures and the `wait` behaviour they had in the migration check. Its docstring gives the source of the figure: crates.io's published crawler limit of one request a second.
- `bevy_migration_dependency_check.py` defines none of the three names and does not re-export them. After its standard-library imports it runs `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bevy_upgrade"))` and `import crates_io_pacer`. `query_crates_io` and `get_bevy_dependency_requirement` call `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before `request.urlopen`.
- The watch paces its crates.io requests. `http_fetch` in `scripts/bevy_upgrade/upstream.py` calls `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before `urllib.request.urlopen` when `urlsplit(url).hostname` is `crates.io`, and for no other host: a request to `api.github.com` or `raw.githubusercontent.com` never waits. `fixture_fetch` never reaches `http_fetch`, so a run from a fixtures file does not wait.
- Both scripts read `crates_io_pacer.CRATES_IO_PACER` at call time, so a test replaces the pacer once, with `mock.patch.object(crates_io_pacer, "CRATES_IO_PACER", ...)`, never on the check or on `upstream`.
- `run_check` in `scripts/bevy_upgrade/upgrade_check.py` is unchanged. It reads the clock before each fetch and the sleep happens inside the fetch, so a sleep counts against `CHECK_BUDGET`. The scheduled job's 300 second limit holds the 180 second budget, one sleep of at most a second and one request's 30 second timeout.
- `pyrightconfig.json` has an `executionEnvironments` entry with root `scripts/bevy_migration_plan` and `extraPaths` `scripts/bevy_migration_plan` and `scripts/bevy_upgrade`, before the closing `{"root": "."}` entry.
- `commands/bevy_migration_plan.md` says how a migration check's requests add up: one for each direct dependency, then up to 21 more for each one that depends on Bevy.
- Measured on 2026-10-12 at 22:01 EDT through the real `http_fetch`: three crates.io lookups in a row answered at 0.22, 1.18 and 2.17 seconds, and a GitHub lookup after them took 0.27 seconds with no wait.

**Files:**
- `scripts/bevy_upgrade/crates_io_pacer.py` — the interval, `RequestPacer` and the shared `CRATES_IO_PACER`.
- `scripts/bevy_upgrade/upstream.py` — `http_fetch` waits for the pacer before a crates.io request.
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — imports the pacer module from the watch's directory; its two crates.io functions call it.
- `pyrightconfig.json` — the execution environment that lets the check's directory resolve `crates_io_pacer`.
- `scripts/bevy_upgrade/test_crates_io_pacer.py` — the five `RequestPacer` tests on their own stand-in clock: no sleep on the first call, the remaining 0.75 seconds after 0.25, no sleep after the interval, two one-second sleeps for three calls in a row, and a sleep that runs long.
- `scripts/bevy_upgrade/test_upstream.py` — `UpstreamTests.setUp` installs a pacer on a stand-in clock and a `urllib.request.urlopen` stand-in that fails any test reaching the network. Four tests cover one wait before a crates.io request, no wait for either GitHub host, one one-second sleep between two crates.io requests, and no pacing through `fixture_fetch`.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — `setUp` replaces the pacer at `crates_io_pacer.CRATES_IO_PACER`; one test checks that the three pacing names are absent from the check.
- `commands/bevy_migration_plan.md` — the time note beside the command that runs the check.

**Gotchas:**
- An `executionEnvironments` entry rooted at a script directory makes basedpyright resolve that directory's imports from the directory itself. The check's test module therefore has `import bevy_migration_dependency_check as checker`; the `scripts.bevy_migration_plan` package path is an unresolved import under the entry. The module imports only under `python3 -m unittest discover -s scripts/bevy_migration_plan -p 'test_*.py'`, which puts its directory on `sys.path`; named by package path from the repository root it raises `ModuleNotFoundError`.
- Importing the check puts `scripts/bevy_upgrade` first on `sys.path`, so a module there is found before a same-named module later on that process's path. A module added to `scripts/bevy_upgrade` under a name that process imports later is loaded in place of the intended one.
- The limit holds per process: `CRATES_IO_PACER` is one object in each Python process. The watch and a migration check running at the same moment each keep to one request a second and do not see each other.
- A check that makes N crates.io requests takes at least N − 1 seconds and holds the record lock that long, about 40 seconds for hana_catalyst's first read. A verb that takes the lock waits behind it; `line` takes no lock. About 180 crates.io requests is the most one check makes before `CHECK_BUDGET` cuts it short. The next check continues from there, and the only outward sign is that `Record.checked` does not advance.
- `test_cli.py` names a fixtures file for every CLI run, so no CLI test reaches `http_fetch` or waits on the real pacer.
- The stand-in clock class has one copy in each of the three test modules that pace, because a test module here imports no other test module. A change to it is made in all three.

**Ruled out:**
- A second copy of the pacer in the watch, or a re-export from the check: both scripts import one module.
- Pacing GitHub hosts: GitHub has its own limit and the watch already keeps to it.
- Changing `run_check` or `CHECK_BUDGET`: the sleep falls inside the fetch the budget already brackets.
- A test in the check's suite that reaches the shared pacer through a private module import to assert identity: basedpyright warns, and the two caller-order tests already prove the check uses the replaced pacer.
- Keeping the `scripts.bevy_migration_plan` package-path import in the check's tests: it does not resolve under the new execution environment.
