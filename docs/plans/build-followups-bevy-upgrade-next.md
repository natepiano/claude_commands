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

#### Work Order

**Goal:** Run on a workspace, the check asks about the direct dependencies of every member, once per crate and version and never faster than one crates.io request a second, and a dependency whose version carries a pre-release or build suffix is looked up, compared and printed under that whole version.

**Spec:**
Work only in the worktree and branch the Delegation Context names. The showrunner approved this phase on 2026-10-12.

`run_cargo_tree`:

- The base command becomes `['cargo', 'tree', '--workspace', '--no-dedupe', '--depth=1', '--format', '{p}']`. The `--all-features` first attempt and the fallback to the default features stay as they are, and both carry the two new flags.
- Why, for the docstring, in one or two sentences: cargo prints a package it has already printed as `name vX (*)` with no children. In a workspace, a member that an earlier member depends on is printed that way when its own turn comes, so its direct dependencies never appear. `--no-dedupe` prints every package in full each time. `--workspace` covers a workspace whose root manifest is also a package: without it cargo lists only that root package.

`get_all_direct_dependencies`:

- The line pattern becomes `r'([a-zA-Z0-9_-]+)\s+v(\d\S*)'`: the version is the whole token after `v`, up to the next whitespace. The lines it reads look like these:

```
alpha v0.1.0 (/home/u/project/crates/alpha)
├── beta v1.1.6+spec-1.1.0
├── gamma v0.20.0-rc.1
├── delta v2.0.1 (proc-macro)
├── epsilon v0.5.0-dev (/home/u/project/crates/epsilon)
[dev-dependencies]
└── beta v1.1.6+spec-1.1.0
```

- From those lines the versions are `0.1.0`, `1.1.6+spec-1.1.0`, `0.20.0-rc.1`, `2.0.1` and `0.5.0-dev`. The exclusions (Bevy's own crates, workspace members, the utility list) and the sorted, de-duplicated result stay as they are. Update the docstring's sentence on `--depth=1` to name the two new flags.

`get_bevy_dependency_requirement`:

- The version is percent-encoded in the address: `urllib.parse.quote(version, safe='')`, so `1.1.6+spec-1.1.0` is requested as `.../crates/<name>/1.1.6%2Bspec-1.1.0/dependencies`. The crate name is unchanged. Every caller goes through this function, so the versions `find_bevy_compatible_version` takes from crates.io are encoded the same way.

One lookup per crate:

- `get_all_direct_dependencies` already collects `(name, version)` pairs in a set, so a crate that several members list at one version is returned once and `get_bevy_dependencies` asks about it once. Keep that, and say so in the docstring: `--no-dedupe` repeats a shared crate under every member that uses it.

Paced requests (crates.io's published limit for a crawler is one request a second):

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

- `wait` returns at once on its first call. On a later call it reads the clock, and when less than `min_interval` has passed since the moment the previous call let its request start, it sleeps for the remainder. It then records the moment this request starts: the clock reading, taken again after the sleep when it slept, so a sleep that runs long never shortens the next gap.
- `query_crates_io` and `get_bevy_dependency_requirement` each call `CRATES_IO_PACER.wait()` immediately before `request.urlopen`. They are the only two functions that reach crates.io. They read the module attribute at call time, so a test can replace it.
- `Callable` comes from `collections.abc`.

Nothing else changes. `classify_dependency` already compares the current version with the compatible one by equality, and crates.io's own version strings carry the suffix, so the comparison becomes right once the current version keeps its suffix. The report prints the version it is given. `commands/bevy_migration_plan.md` stays as it is: its bullet says the script reads direct dependencies, which stays true. The paced run takes longer than before, about one second for each request: the unit director measures it in the Smoke run and reports the time.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the cargo command, the line pattern, the encoded version in the lookup address, the pacer and its two calls, the two docstrings.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — the tests the Acceptance gate lists.

**Seats:** 1 writer + 1 tester — one module and its test module.
- `impl` — `scripts/bevy_migration_plan/bevy_migration_dependency_check.py`.
- `test` — `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py`, from the Spec.

**Constraints from prior phases:**
- `get_bevy_dependency_requirement(crate_name, version, bevy_names)` returns `BevyRequirement`, `NoBevyDependency` or `RequirementLookupFailed`. `get_bevy_dependencies` keeps a crate on the first and the third and drops it on the second; a kept `RequirementLookupFailed` becomes a `CHECK_NEEDED` row.
- Measured on 2026-10-12: crates.io answers 404 for `/api/v1/crates/toml/1.1.6/dependencies` and 200 for `/api/v1/crates/toml/1.1.6%2Bspec-1.1.0/dependencies`. In hana_catalyst, `cargo tree --depth=1 --format '{p}' --all-features` prints 27 member roots, most of them as `name (*)` with no children, and never prints two direct dependencies that only later members declare; with `--workspace --no-dedupe` added it prints both.
- Existing tests that need a tree replace `checker.run_cargo_tree` with a canned string (`mock.patch.object(checker, "run_cargo_tree", return_value=tree)`).

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. The tests cover:
- `run_cargo_tree` passes `--workspace` and `--no-dedupe` to cargo, in the `--all-features` attempt and in the fallback after that attempt fails. This test replaces the `subprocess.run` stand-in for itself and reads the argument lists it was called with.
- `get_all_direct_dependencies`, given the tree lines the Spec shows, returns each dependency with its whole version: the build suffix, the pre-release suffix, and a version that ends before ` (proc-macro)` or a path. A member named as a dependency of another member is still left out.
- A dependency that only the second member of a tree lists is returned.
- A crate two members list at the same version is looked up once: with a tree that names it under both, crates.io is asked for that crate's dependencies one time.
- `RequestPacer` with a stand-in clock and sleep: the first `wait` does not sleep; a `wait` 0.25 seconds after the previous start sleeps 0.75 seconds; a `wait` after more than the interval does not sleep; three calls with no time passing sleep twice, one second each.
- `query_crates_io` and `get_bevy_dependency_requirement` each call the pacer once per request, before the request.
- `setUp` installs a pacer with a stand-in clock and sleep, so no test waits on the real clock.
- `get_bevy_dependency_requirement`, given a version with a build suffix, requests the address with `+` encoded as `%2B`.
- End to end with canned answers: a direct dependency whose version has a build suffix and which has no Bevy dependency gets no row in the report, and one that depends on a Bevy crate and already sits on the compatible version, suffix included, is classified `OK`.
