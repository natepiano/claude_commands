# Bevy upgrade watch

## What it is

`/showrunner:bevy_upgrade` keeps one record per production of every crate that ties the project to its Bevy version: the project's pin, what crates.io and GitHub offer for the target version, and the user's answer on a git pin, stored in the user's own words. A job in [the notifier](session-notifier.md) runs a check every four hours, which keeps the record current and writes to the showrunner only what changed since the showrunner last saw it. [The dailies builder](showrunner-automation.md) adds one `Bevy upgrade` topic read from the same record, so every statement about the upgrade in a report has one source. The user asked for it so that a report "doesn't have bad information", and so that what is known about each dependency is saved "so we can limit the work we do" (user, 2026-10-12).

`/bevy_migration_plan`'s dependency check belongs to the same feature. It lists the direct dependencies of every workspace member, sees a crate that depends on Bevy only through Bevy's own sub-crates, such as `bevy_app` or `bevy_ecs`, and looks each crate up on crates.io once, under its whole version. The watch and the check send crates.io at most one request a second, through one shared pacer. The watch lives in `scripts/bevy_upgrade/`, behind `commands/showrunner/bevy_upgrade.md`; the dependency check is `scripts/bevy_migration_plan/bevy_migration_dependency_check.py`.

## How it works

### Key files and commands

| File | Role |
| --- | --- |
| `scripts/bevy_upgrade/pins.py` | Reads `Cargo.lock` and the root `Cargo.toml` of the production checkout: the Bevy versions, Bevy's own crate names and every dependent with its pin. Runs no cargo command. |
| `scripts/bevy_upgrade/upgrade_record.py` | The record's types, lock, validation, load and save, `start`, the pins merge, answers and notes, `on_target` and `target_fit`. |
| `scripts/bevy_upgrade/upstream.py` | `Fetch`, the two fetchers, the crates.io and GitHub lookups and the types they return. |
| `scripts/bevy_upgrade/crates_io_pacer.py` | The one-second interval, `RequestPacer` and the shared `CRATES_IO_PACER`, used by the watch and by the migration plan's dependency check. |
| `scripts/bevy_upgrade/upgrade_check.py` | `run_check`: one check with its order, skip rule, time budget and per-entry failure rule. |
| `scripts/bevy_upgrade/upgrade_report.py` | The derived state, the table, the change lines and the `line` sentence. |
| `scripts/bevy_upgrade/upgrade_schedule.py` | The notifier job, the message to the showrunner and the retire call. |
| `scripts/bevy_upgrade/bevy_upgrade.py` | The CLI: the verbs, the one lock block and its save, the baseline, job upkeep, closing and exit codes. |
| `scripts/bevy_upgrade/test_*.py` | One test file per module, `test_crates_io_pacer.py` among them, and `test_cli.py`, which runs the CLI as a subprocess. |
| `commands/showrunner/bevy_upgrade.md` | The command: runs the CLI and gives the showrunner its rules. |
| `scripts/production/dailies_input.py` | `bevy_upgrade_watch` and `bounded_bevy_update`: the `Bevy upgrade` dailies topic. |
| `commands/showrunner/dailies.md` | Gather item 7 and the meaning of `bevy upgrade: unavailable`. |
| `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` | The dependency check behind `/bevy_migration_plan`. |
| `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` | Dependency-check tests, runnable from the repository root or their own directory. |
| `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh` | Regex match counts excluding `vendor/` and `target/`; single, multiple and verify modes. |
| `scripts/bevy_migration_plan/test_bevy_migration_count_pattern.py` | Counter regressions for zero matches, JSON escaping, rg errors, multiple matches per line and excluded directories. |
| `commands/bevy_migration_plan.md` | Dependency review, two-pass guide analysis with at most 10 agents in Pass 2, and production handoff to `/plan:to_phased_plan`. |
| `pyrightconfig.json` | Two `executionEnvironments` entries: root `scripts/bevy_upgrade` with extraPaths `scripts/bevy_upgrade` and `scripts/production`, and root `scripts/bevy_migration_plan` with extraPaths `scripts/bevy_migration_plan` and `scripts/bevy_upgrade`. Both stand before the closing `{"root": "."}` entry. |

```text
bevy_upgrade.py [--production <doc>] [status]
bevy_upgrade.py [--production <doc>] start <target>
bevy_upgrade.py [--production <doc>] check [--scheduled]
bevy_upgrade.py [--production <doc>] approve <crate> <words>
bevy_upgrade.py [--production <doc>] decline <crate> <words>
bevy_upgrade.py [--production <doc>] forget <crate>
bevy_upgrade.py [--production <doc>] note <crate> [<text>]
bevy_upgrade.py [--production <doc>] stop
bevy_upgrade.py [--production <doc>] line [--json]
```

`--production` comes before the verb, and no verb means `status`. The command file runs the CLI as `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/bevy_upgrade/bevy_upgrade.py" $ARGUMENTS`. The modules import each other by bare name (`import pins`); `bevy_upgrade.py` and `upgrade_schedule.py` put `scripts/production` on `sys.path` for `add_unit` and `showrunners`.

### The pin reader

`pins.py` turns the production checkout's lockfile into the Bevy versions in use and one `Dependent` per third-party crate that depends on Bevy.

```python
read_pins(checkout: Path) -> Pins          # raises PinsFailure("<path>: <reason>")

Pin = RegistryPin | GitPin | PathPin       # TypedDicts tagged by `source`
RegistryPin(source: Literal["registry"], version: str)
GitPin(source: Literal["git"], version: str, url: str, ref: str, rev: str)
PathPin(source: Literal["path"], version: str, path: str)
Dependent(name: str, pin: Pin, bevy: str, via: tuple[str, ...], patched_bevy: bool)
Pins(bevy: tuple[str, ...], bevy_names: frozenset[str], dependents: tuple[Dependent, ...])
```

- **Input.** `<checkout>/Cargo.lock` and `<checkout>/Cargo.toml`, read with `tomllib`. `PinsFailure` covers a missing or unparseable file, a wrongly typed `package` or `patch` entry, a dependency string that does not resolve to one package, a lockfile with no `bevy_ecs` package, and a package with no registry, git or path pin.
- **Bevy versions.** The versions of the package `bevy_ecs`, oldest first (`Pins.bevy`).
- **Bevy crate.** A package named `bevy` or `bevy_*` at one of those versions that has a `registry+` source or is replaced by a `[patch.*]` entry of the root manifest. `Pins.bevy_names` holds their names, and every upstream lookup uses it to decide which dependency names count as Bevy.
- **Dependent.** Any other package with a `source` whose `dependencies` name a Bevy crate. Its `bevy` is the oldest Bevy version among the Bevy crates it lists. Of two lock versions of one dependent, the one on the oldest Bevy is kept, chosen before any pin is built. Its `via` is empty when a package with no `source` lists it, otherwise the sorted names of the packages that list it. `Pins.dependents` is sorted by name.
- **Patches.** A `[patch.*]` entry belongs only to the lock package it replaced (`_is_patched`: a path patch and no `source`, or a git patch and a `git+` source). A patched Bevy crate is a row with `patched_bevy=True` and `bevy` equal to its own lock version. Every other package with no `source` is the project's own and is never a row.
- **Pins.** A `registry+` package always gets a `RegistryPin`. A `git+` source gives a `GitPin`: `url` without query or fragment, `ref` the source's query (`branch=…`, `rev=…`, `tag=…`, or `""`), `rev` the full commit after `#`. A path-patched package gives a `PathPin` whose `path` is spelled as the `[patch]` table spells it.

### The record

`upgrade_record.py` owns `<state dir>/<slug>.json`, one file per production, and is the only module that reads or writes it. Every record type is a `TypedDict`; the lines below list its keys.

```python
state_dir() -> Path                  # BEVY_UPGRADE_STATE_DIR, else ~/.local/state/bevy-upgrade
record_path(slug: str) -> Path       # <state dir>/<slug>.json
locked(slug: str) -> Generator[None, None, None]    # @contextmanager
load(slug: str) -> Record | None     # None: this production never started a watch
save(record: Record) -> None
start(production: Production, target: str, pins: Pins, now: datetime) -> Record
apply_pins(record: Record, pins: Pins, now: datetime) -> Record
set_answer(record: Record, crate: str, answer: Literal["approved", "declined"],
           words: str, today: str) -> Record
forget_answer(record: Record, crate: str) -> Record
set_note(record: Record, crate: str, text: str) -> Record
on_target(version: str, target: str) -> bool
target_fit(requirement: str, target: str) -> TargetFit   # "on target" | "off target" | "unreadable"

Record(slug: str, production: str, checkout: str, target: str, started: str,
       closed: str | None, bevy: list[str], pins_read: str,
       dependencies: dict[str, Entry], target_bevy: TargetBevy, target_error: str,
       checked: str | None, announced: dict[str, Announced] | None)
Entry(pin: Pin, bevy: str, via: list[str], patched_bevy: bool,
      answer: Answer | None, note: str,
      crates_io: CratesIoUnread | CratesIoRead | CratesIoUnlisted, error: str,
      branch: BranchUnread | BranchRead, items: list[Item])
Answer(answer: Literal["approved", "declined"], at: str, words: str)
CratesIoUnread(status: Literal["unread"])
CratesIoRead(status: Literal["read"], checked: str, releases: dict[str, str],
             newest: str, ready: str, repository: str)
CratesIoUnlisted(status: Literal["unlisted"], checked: str, repository: str)
BranchUnread(status: Literal["unread"])
BranchRead(status: Literal["read"], repository: str, name: str, rev: str,
           committed: str, requirement: str)
TargetBevy = TargetUnread | TargetUnpublished | TargetPrerelease | TargetReleased
Announced(state: str, repository: str, items: dict[str, str])
```

- **Record fields.** `production` is the doc's path and `target` is major.minor. `started`, `pins_read`, `checked` and `closed` are ISO-8601 UTC. `closed` is `None` while the watch is open. `checked` is the time of the last check that reached every entry, `None` before the first. `target_bevy` is tagged by `status`: `unread`; `unpublished`; `pre-release` with `since`; `released` with `version`, `date` and `since`, where `since` is the `YYYY-MM-DD` of the first version on the target, pre-releases included. `target_error` is the last failure of the target lookup, or `""`.
- **Entry fields.** `answer` is `None` until the user answers; `Answer.at` is `YYYY-MM-DD` in the production's zone and `words` is verbatim. `crates_io` is tagged by `status` as `Pin` is by `source`, and only `read` has `ready`. In `CratesIoRead`, `checked` is the last complete check of this crate, `releases` maps a version to its Bevy requirement, `newest` is the newest version not yanked, `ready` is the newest walked release whose requirement is on the target (`""` while none is), and `repository` is `owner/name` on GitHub or `""`. `error` is the last check's failure for this crate, or `""`. `branch` and `items` are GitHub's answers for `BranchRead.repository`.
- **Tagged cases.** A state is a tagged case and no field is `bool | None`. `None` has one meaning in each of its four places: no answer, an open watch, no complete check, no report to the showrunner yet.
- **Lock.** `locked` holds an exclusive `flock` on `<state dir>/<slug>.lock` around a whole read-modify-write. Only directory creation, open and flock errors become `RecordFailure`; an error from the caller's body propagates unchanged.
- **Load and validate.** `load` returns `None` when no file exists and raises `RecordFailure` (`<path>: <reason>`) when the file is unreadable or `_validate` rejects a field. `_validate` checks every field. It gives an absent `crates_io`, `branch` or `target_bevy` the value `{"status": "unread"}`, an absent `error` or `target_error` `""`, absent `items` `[]`, and an absent `checked` or `announced` `None`. A present field of the wrong type is a `RecordFailure`.
- **Save.** `save` writes a temp file beside the record, then replaces the record with it.
- **Start.** `start` refuses a target that is not `\d+\.\d+` with `target must be major.minor, such as 0.20`. An open record for the same target comes back after `apply_pins`. An open record for another target refuses with `a watch for <target> is open; stop it first`. A closed record moves to `<slug>.<old target>.closed.json`, and a new record begins without its answers, notes or upstream facts.
- **`apply_pins`.** Adds a new dependent with `answer=None`, `note=""`, `crates_io` and `branch` `unread`, `error=""` and `items=[]`. Updates `pin`, `bevy`, `via` and `patched_bevy` of a known one and keeps its other fields. Removes an entry the lockfile no longer has. Sets `bevy` and `pins_read`.
- **Answers and notes.** `set_answer`, `forget_answer` and `set_note` refuse an unknown crate with `<crate> is not tracked; tracked: <names>`. `set_answer` refuses empty or whitespace-only `words` with `answer words must not be empty`; a second answer replaces the first. `set_note` with `""` clears the note.
- **Target tests.** `on_target` is true for the same major.minor. `target_fit` strips one leading `^`, `~` or `=`; a remainder matching `\d+\.\d+(\.\d+)?(-[\w.]+)?` is compared by major.minor, and anything else (a range, `*`, an empty string) is `unreadable`.
- **`announced`.** `None` until a table or a message has reached the showrunner. After that it holds, per crate, the state word, the repository its items belong to and each item's state, as the showrunner was last shown them.

### Upstream lookups

`upstream.py` holds every request the watch makes. Each lookup takes a `Fetch` as its first argument and raises only `FetchFailure`.

```python
Fetch = Callable[[str], bytes]
class FetchFailure(Exception): ...     # str() is "<host>: <reason>"
class NotFound(FetchFailure): ...      # HTTP 404
http_fetch(url: str) -> bytes
fixture_fetch(path: Path) -> Fetch

CrateInfo(versions: tuple[str, ...], repository: str)
crate_info(fetch: Fetch, crate: str) -> CrateInfo
release_requirement(fetch: Fetch, crate: str, version: str, bevy_names: frozenset[str]) -> str
target_release(fetch: Fetch, target: str) -> TargetUnpublished | TargetPrerelease | TargetReleased

BranchHead(rev: str, committed: str)   # committed is YYYY-MM-DD
Item(number: int, kind: Literal["pr", "issue"], state: Literal["open", "closed", "merged"],
     title: str, updated: str, head: str)
github_repository(url: str) -> str
default_branch(fetch: Fetch, repository: str) -> str
branch_head(fetch: Fetch, repository: str, branch: str) -> BranchHead
member_manifest_paths(fetch: Fetch, repository: str, rev: str) -> list[str]
manifest_requirement(fetch: Fetch, repository: str, rev: str, crate: str,
                     bevy_names: frozenset[str]) -> str
port_items(fetch: Fetch, repository: str, target: str, since: str) -> list[Item]
pull_state(fetch: Fetch, repository: str, number: int) -> tuple[Literal["open", "closed", "merged"], str]
```

- **`Fetch`.** GETs one URL and raises only `FetchFailure`, whose text is `<host>: <reason>` (`crates.io: HTTP 503`). `http_fetch` uses urllib with the User-Agent `bevy-upgrade (natepiano)`, a 30 s timeout and no token. It raises `NotFound` for HTTP 404 and turns `HTTPError`, `http.client.HTTPException`, `OSError` and `ValueError` into `FetchFailure`; nothing else leaves it. When the URL's host is `crates.io` it calls `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before `urllib.request.urlopen`; a request to any other host, `api.github.com` and `raw.githubusercontent.com` included, never waits.
- **Fixtures.** `fixture_fetch` serves each URL from a JSON object of URL to body. A string is served as it is (a manifest), any other value as JSON, and `null` raises `NotFound`. A URL the file lacks raises `FetchFailure("<host>: no fixture")`, and an unreadable file fails every request with `<host>: fixture <path>: <reason>`. It never reaches `http_fetch`, so a run from a fixtures file never waits on the pacer.
- **Malformed bodies.** A body that is not the expected JSON, or a manifest that is not valid TOML, is a `FetchFailure` (`<host>: invalid response: <reason>`), because `run_check` catches nothing else.
- **`crate_info`.** Reads `https://crates.io/api/v1/crates/<crate>`. `versions` holds the versions not yanked, in the API's order (newest first). `repository` is `github_repository` of the crate's repository URL.
- **`github_repository`.** `owner/name` for a github.com URL, with a `.git` suffix and any trailing path removed; `""` for any other URL or none.
- **`release_requirement`.** Reads `https://crates.io/api/v1/crates/<crate>/<version>/dependencies`, with the crate and the version each percent-encoded by `quote(…, safe='')`, so a version with build metadata is asked for whole (`1.1.6+spec-1.1.0` as `1.1.6%2Bspec-1.1.0`). Among the dependencies whose kind is not `dev` it returns the `req` of the one named `bevy`, whether or not `bevy_names` holds it (a lockfile with only Bevy sub-crates has no `bevy` package); else the `req` of the first by sorted name whose `crate_id` is in `bevy_names`; else `""`. A name outside `bevy_names` is never read as Bevy: `bevy_egui` is a third-party crate.
- **`target_release`.** Reads `https://crates.io/api/v1/crates/bevy/versions` and looks at each `num` that starts with `<target>.`. No such version is `unpublished`; only pre-releases is `pre-release`; otherwise `released`, with the highest stable version and the date of its `created_at`. `since` is the earliest `created_at` date among them.
- **`default_branch` and `branch_head`.** `https://api.github.com/repos/<repository>` and `…/commits/<branch>`; `committed` is the committer date.
- **`manifest_requirement`.** Reads `https://raw.githubusercontent.com/<repository>/<rev>/Cargo.toml`. It looks at the keys of `[dependencies]` and `[workspace.dependencies]` that are `bevy` or in `bevy_names`. An entry is a version string or a table with `version`, and a `workspace = true` table is resolved through `[workspace.dependencies]`. Of the keys that give a requirement it returns `bevy`'s, else the first by sorted key, else `""`. A root manifest that answers `NotFound` gives `""`.
- **Workspace-member search.** Runs only when the root manifest gives no requirement and has a `[workspace]` table. `member_manifest_paths` asks `https://api.github.com/repos/<repository>/git/trees/<rev>?recursive=1` once and returns the sorted paths of `blob` entries ending `/Cargo.toml`; any other answer is a `FetchFailure`. The search reads at most five of them, the one whose directory is named like the crate first (`-` and `_` equal), then in sorted order, until one's `[package] name` equals the crate. That member's `[dependencies]` is read by the root's rule, with the root's `[workspace.dependencies]` as the inherited table. A matching member ends the search even when its requirement is empty, and no match in five reads gives `""`.
- **`port_items`.** Reads one page: `https://api.github.com/repos/<repository>/issues?state=all&since=<since>T00:00:00Z&sort=updated&direction=desc&per_page=100`. It keeps the issues and pull requests, open or closed, whose title contains `<target>` as a substring and whose `created_at` date is not before `since`. `updated` is the API's `updated_at`. It returns `head` `""` and never the state `merged`; the check fills both for a pull request.
- **`pull_state`.** Reads `…/pulls/<number>`: `merged` when the API says so, else the API's state, and the head as `<head.ref> @ <first 7 of head.sha>`.
- **`NotFound` as an answer.** In three places only: `crate_info` during a check (the entry becomes `unlisted`), a manifest (`""` for the root, a skipped member), and `branch_head` under a stored branch name (the default branch is read again). From any other request, a release's dependencies included, it fails the entry.

### The crates.io pacer

`crates_io_pacer.py` keeps the starts of one process's crates.io requests at least one second apart. The watch and the migration plan's dependency check both import it.

```python
CRATES_IO_MIN_INTERVAL_SECONDS: float = 1.0
RequestPacer(min_interval: float, clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep)
RequestPacer.wait() -> None
CRATES_IO_PACER = RequestPacer(CRATES_IO_MIN_INTERVAL_SECONDS)
```

- **One home.** The three names are defined in this module only. `upstream.py` and `bevy_migration_dependency_check.py` import the module and define, copy or re-export none of them.
- **`wait`.** It returns at once on its first call. A later call sleeps for what is left of `min_interval` since the previous request started, and not at all once the interval has passed. After a sleep it reads the clock again and records that reading as the request's start.
- **Callers.** Three functions reach crates.io, and each calls `crates_io_pacer.CRATES_IO_PACER.wait()` immediately before it opens the request: `http_fetch` in the watch, and `query_crates_io` and `get_bevy_dependency_requirement` in the migration check.
- **Replaced in one place.** Every caller reads the module attribute at call time, so a test replaces the pacer once, with `mock.patch.object(crates_io_pacer, "CRATES_IO_PACER", …)`, never on `upstream` or on the check.

### The check

`upgrade_check.run_check` makes one bounded run over the record and stores, per entry, either every fact it read or none.

```python
Clock = Callable[[], datetime]          # the current UTC time
CHECK_BUDGET = timedelta(seconds=180)
CheckPass(record: Record, asked: int, failed: int)
run_check(record: Record, pins: Pins, fetch: Fetch, clock: Clock) -> CheckPass
_crate_facts(fetch: Fetch, name: str, entry: Entry, target: str, bevy_names: frozenset[str],
             checked: str) -> CratesIoRead | CratesIoUnlisted
_github_facts(fetch: Fetch, name: str, entry: Entry, record: Record,
              bevy_names: frozenset[str]) -> None
```

- **Result.** `asked` counts the entries that made at least one request, `failed` those whose lookup failed. `run_check` never raises for a failed lookup or a spent budget, so the caller always saves what the check learned.
- **Budget.** The run's `now` is `clock()` at its start, and the clock is read before every request. Once 180 s have passed, no further request is made, the entry being read stays exactly as stored (its `error` too) and the run returns. A pacer sleep happens inside the fetch, after the clock is read for it, so the sleep counts against the budget. `CheckBudgetSpent` never leaves `run_check`.
- **Target first.** While `target_bevy["status"]` is not `released`, `target_release` runs once per check; its result is stored and `target_error` set to `""`. A `FetchFailure` there leaves `target_bevy` as it was and sets `target_error` to the failure's text; the entries still run. Once the status is `released`, the lookup never runs again.
- **Order.** Entries never read first, then the oldest `crates_io["checked"]` first, then by name, so the next check continues one the budget cut short.
- **Skip rule.** No request of any kind for an entry that is `done`, is a patched Bevy crate, has a path pin, or already has a `ready` release. Such an entry keeps its stored `crates_io`, `branch` and `items`.
- **crates.io step.** `crate_info`, then a walk from the newest version down to and including the pinned one, or over the newest six when the pin is not among the versions. `release_requirement` is called only for versions missing from `releases`: a release never changes, so each is read once. The step stores `newest`, `repository`, the grown `releases`, `ready` and `checked`, the run's `now`. A crate whose every version is yanked fails with `crates.io: no version that is not yanked`.
- **Unlisted.** When `crate_info` raises `NotFound`, `crates_io` becomes `unlisted` with this check's `checked` and, as `repository`, `github_repository(<pin url>)` for a git pin and `""` for any other pin. The entry carries no error, so it does not count in `failed` and does not make the table read `(stale)`, and it asks crates.io once on every check.
- **GitHub step.** Runs after the crates.io step for an entry whose `crates_io` is `read` with `ready` `""`, or `unlisted`, and whose `repository` is not `""`. With no repository it makes no request and leaves `branch` and `items` as stored.
- **Request reuse.** `default_branch` is asked only when no `branch` is stored for the repository. `branch_head` and `port_items` are asked on every check. `manifest_requirement` is skipped only when the stored `rev` is the branch head's and the stored requirement is not empty. `pull_state` is asked for a returned pull request that is new or whose `updated` changed; otherwise its stored `state` and `head` are reused.
- **`since`.** `port_items` gets the `since` of `target_bevy` when its status is `pre-release` or `released`, else the date of the record's `started`.
- **Kept items.** A stored item that the issues answer does not list is kept. A kept pull request in state `open` is asked again with `pull_state`, which updates its state and head. Kept issues and kept closed or merged pull requests cost no request.
- **Repository change.** Stored `branch` and `items` belong to `BranchRead.repository`. Under another repository the check reads as if nothing were stored and the stored items are gone, so an item number names an item only together with its repository.
- **Missing branch.** When `branch_head` answers `NotFound` under a stored name, `default_branch` is read again and `branch_head` asked once more; a stored `rev` equal to the new head still keeps its `requirement`. With no stored branch the `NotFound` fails the entry.
- **Both or neither.** The two steps work on a copy of the entry, and the entry is updated only when both succeed, with `error` set to `""`. One `FetchFailure`, a rate-limit answer included, leaves every stored field of the entry as it was, sets `error` to the failure's text and counts in `failed`. The other entries still run.
- **`checked`.** `record["checked"]` is set to the run's `now` when the check reached every entry, whether or not entries failed. A check the budget cut short leaves it as it was.

### States, the table and the change lines

`upgrade_report.py` derives everything printed from the record. A state word is computed at print time; the only stored one is in `announced`.

```python
State = Literal["git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done"]
STATE_ORDER: tuple[State, ...]          # the order above: sorts the rows and the `line` counts
state(entry: Entry, target: str) -> State
table(record: Record, zone: ZoneInfo, now: datetime) -> str
announcement(entry: Entry, target: str) -> Announced
changes(record: Record) -> list[str]
waiting(record: Record) -> int          # entries that are `git ready` with no answer
line(record: Record, zone: ZoneInfo, now: datetime, next_due: datetime | None, pins_error: str) -> str
```

`state` returns the first that applies:

1. `done` when `on_target(entry["bevy"], target)`.
2. `re-vendor` when `patched_bevy` or the pin is a path pin.
3. `release ready` when `crates_io` is `read` and `ready` is not `""`.
4. `git ready` when `branch` is `read` and `target_fit(<its requirement>, target)` is `on target`.
5. `port in PR` when any item is a `pr` in state `open`.
6. `not checked` when `crates_io` is `unread`.
7. `no port yet` otherwise.

- **Table.** In order: `Bevy <target> upgrade for <slug>: <checkout>, on Bevy <versions joined by " and ">.`; the second line; a blank line; a Markdown table headed `| Dependency | Our pin | crates.io | Git | State | Next |`, rows sorted by `STATE_ORDER`, then name. Then, when any exist, `Answers on git pins:` with one `- <crate>: <answer> <at>: "<words>"` line per recorded answer, whatever the pin source, and `Notes:` with one `- <crate>: <note>` line each. In a cell `|` is escaped and a line break becomes `<br>`.
- **Second line**, three parts. The target: `Bevy <target>: released as <version> on <date>.` for `released`, `Bevy <target>: not released yet.` for `pre-release` and `unpublished`, `Bevy <target>: release not looked up yet.` for `unread`. Then ` Release lookup failed: <target_error>.` when `target_error` is not `""`. Then the check: ` Upstream: no complete check yet.` while `checked` is `None`; else ` Upstream checked <HH:MM zone>.`; or, when stale, ` Upstream last checked <YYYY-MM-DD HH:MM zone> (stale).`
- **Stale.** `checked` is older than eight hours, any entry has `error`, or `target_error` is not `""`.
- **Dependency cell.** `<name>`, `<name> (via <a>, <b>)` or `<name> (patched Bevy crate)`.
- **Our pin cell.** `<version>`, `git <ref or first 7 of rev> @ <first 7 of rev>` or `path <path>, <version>`, each followed by ` on Bevy <bevy>` unless `patched_bevy`.
- **crates.io cell.** `<newest> on Bevy <requirement>`, with ` → <ready> on Bevy <its requirement>` appended when `ready` is set and differs from `newest`; a requirement of `""` prints `requirement unknown` in place of `on Bevy <requirement>`. An unlisted entry prints `not on crates.io`. The cell is `—` for a patched Bevy crate, a path pin, or a `done` entry whose `crates_io` is `unread`, and `not checked` for any other `unread` entry. A `done` entry read before it reached the target keeps its release text. An entry with `error` appends `; last check failed: <error>`.
- **Git cell.** `<branch name> @ <first 7 of rev> on Bevy <requirement>` from a read `branch`, with `requirement unknown` for a requirement of `""`; `not on GitHub` when `crates_io` is read or unlisted with `repository` `""`; `—` otherwise. Each stored item follows, largest number first, as `; PR #<n> <state> (<head>)` or `; issue #<n> <state>`.
- **Next cell.** `—` for `done`. `re-vendor from Bevy <target>` for a patched Bevy crate and `port the copy at <path> to Bevy <target>` for a path-pinned dependent. `bump to <ready>` for `release ready`. For `git ready`: `ask the user: git pin?` with no answer, `pin git rev <first 7 of rev> (approved <at>)` with an `approved` one, `wait for a release (declined <at>)` with a `declined` one. `wait for PR #<n>`, with the largest open number, for `port in PR`. `wait` for `no port yet` and `run check` for `not checked`.
- **`announcement`.** The entry's state word, the repository of its read branch (`""` with none) and each item's state by number.
- **Change lines.** `changes` compares each entry's `announcement` with `announced`, crate by crate in name order: `- <crate>: <old state> → <new state>`, with ` (<ready> on Bevy <requirement>)` after `release ready` and ` (<branch name> @ <rev 7>)` after `git ready`; `- <crate>: PR #<n> <old> → <new>` and `- <crate>: new PR #<n> <state>: <title>`, with `issue #<n>` for an item of kind `issue`; `- <crate>: new dependency`; `- <crate>: no longer a dependency`. `announced` of `None` gives no lines, and an announced item the entry no longer holds gives none. When the entry's repository differs from the announced one, every item it holds is a new item and none is a transition.
- **`line`.** One sentence for a report: `Bevy <target> upgrade: <d> of <n> done`, then each other state with a non-zero count, in `STATE_ORDER`, as `, <count> <state>`, with ` (<k> waiting for your answer)` after `git ready` when `waiting` gives a k above zero. Then one of three endings: `; checked <HH:MM zone>`, followed by `, next check <HH:MM zone>` when `next_due` is not `None`; `; upstream last checked <YYYY-MM-DD HH:MM zone> (stale)` when the table would say stale; `; no complete upstream check yet` while `checked` is `None`. A `pins_error` that is not `""` appends `; pins not re-read since <YYYY-MM-DD HH:MM zone>`.

### Schedule and messages

A watch keeps one notifier job that runs the check. `upgrade_schedule.py` is the only module that calls the notifier or the sender.

```python
instance(slug: str) -> str                    # "bevy-upgrade-<slug>"
ensure_job(record: Record) -> None
remove_job(slug: str) -> None
next_check(slug: str) -> datetime | None      # None: the notifier has no such job
tell_showrunner(slug: str, summary: str, text: str) -> bool   # True only for exit 0 and a `SENT:` line
retire_message(slug: str) -> None
```

- **Job.** `ensure_job` runs `zsh <notifier> new bevy-upgrade-<slug> --every 240 --timeout 300 --run "<scripts/lib/py> <bevy_upgrade.py> --production <doc> check --scheduled"`, each word of the run command through `shlex.quote`. `new` keeps an existing instance's clock, so a repeated call does not move the next check. `remove_job` runs `remove <instance>`. A non-zero exit prints `bevy_upgrade: could not schedule check: <output>` or `bevy_upgrade: could not remove check: <output>` on stderr and changes nothing else.
- **Next check.** `next_check` runs `status <instance>`. Exit 1 is `None`; otherwise it returns the epoch on the `next_due=<epoch> (<local time>)` line.
- **Message.** `tell_showrunner` sends to `showrunners.current_address(slug)` with `send.py --to <address> --from bevy-upgrade --key bevy-upgrade-<slug> --summary <summary> --text <text>`. An empty address, a non-zero exit and every outcome other than `SENT:` (`SKIPPED:`, `QUEUED:`, `REFUSED:`, `FAILED:`) return `False` and print `bevy_upgrade: the showrunner was not told: <the outcome line, or "no showrunner session">` on stderr.
- **Retire.** `retire_message` runs `send.py retire --to <address> --from bevy-upgrade --key bevy-upgrade-<slug>`. It is silent on success and runs nothing for an empty address. A non-zero exit or an `OSError`, one from the address lookup included, prints `bevy_upgrade: could not retire the queued message: <outcome>` on stderr. A failed retire changes neither the exit code nor the table.
- **Baseline.** The baseline is every entry's `announcement`, stored in `announced`. Every verb that prints the table (`start`, `status`, a hand `check`, `approve`, `decline`, `forget`, `note`, `stop`) stores it before the save and calls `retire_message` after it, so a queued message never repeats what the table showed. A closed record prints no change lines.
- **Scheduled check.** `check --scheduled` prints nothing on stdout, re-reads pins, runs the check and saves once. With `announced` of `None` it stores the baseline and sends nothing. With changes it sends the summary `Bevy <target> upgrade: 1 change` or `Bevy <target> upgrade: <N> changes` and, as text, the change lines, the `line` sentence and `Full table: /showrunner:bevy_upgrade`. It stores `announced` only when the send returned `True`, so a change found while the showrunner is away is sent by the next check that reaches it. It never calls `retire_message`.
- **Closing.** When every entry is `done` after a pins re-read that succeeded in the same run, `check` closes the watch. By hand it sets `closed`, removes the job and prints `Bevy <target> upgrade: every dependency is on Bevy <target>. The watch is closed.` above the table. Scheduled, it sends the change lines, if any, then that sentence under the summary `Bevy <target> upgrade: done`, and only when that send returned `True` does it store `announced`, set `closed` and remove the job. `stop` sets `closed`, removes the job and prints the table.
- **Job upkeep.** `start`, `status` and `check`, by hand or scheduled, call `ensure_job` when the run leaves the watch open. A closing run and `stop` call `remove_job`. Both calls happen inside the record lock, directly after the one save. `line` calls `ensure_job` outside any lock.

### The CLI

`bevy_upgrade.py` finds the production, does all its work on the record inside one `upgrade_record.locked` block and saves once at the end of it.

```python
class UpgradeUpdate(TypedDict): update: str; needs_user: bool   # what `line --json` prints
main(argv: list[str] | None = None) -> int
```

- **Production.** Without `--production` the doc is `showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))`; with none, the CLI refuses with `this session runs no production; pass --production <doc>`. `add_unit.read_production` reads the doc: its `checkout` is the tree whose lockfile is read, its `zone` is the zone of every printed time and of an answer's `at`, and its `slug` names the record.
- **One run.** Inside the lock, in order: load the record (or `start` it), re-read the pins and apply them, apply the verb's change, run the check for `start` and `check`, compute the change lines, close when due, store the baseline, save, keep or remove the job, retire the queued message. Printing follows the lock's release.
- **One save point.** Every verb other than `line` that reaches a record saves exactly once, `status` included, because it stores the baseline. A refused verb saves nothing. A new verb changes the record inside the block and adds no save of its own.
- **Pins on every verb.** Every verb re-reads the pins from the checkout and applies them before it acts. When that read fails on a verb other than `start`, the verb works on the stored pins and the table prints under the first line `pins could not be re-read: <reason>; showing pins read <HH:MM zone>`.
- **No record.** Every verb but `start` prints `no Bevy upgrade is tracked for <slug>; start one with /showrunner:bevy_upgrade start <version>` and exits 0. `check --scheduled` and `line` print nothing.
- **Output order.** The `pins could not be re-read` line; `Changed since the last report:` and the change lines, when there are any; the closing sentence or, for `status` on a closed record, `This watch closed <YYYY-MM-DD>.`; the table.
- **`start <target>`.** Reads the pins, where a failure is a refusal, calls `upgrade_record.start`, runs the same check as `check` before the one save, and exits 0 once the record is written, whatever its check found.
- **`check`.** Uses `http_fetch`, or `fixture_fetch(Path(<value>))` when `BEVY_UPGRADE_FIXTURES` is set, and the clock `lambda: datetime.now(timezone.utc)`. When the pins re-read fails it makes no lookup (the Bevy crate names come from the lockfile), prints the table from the stored record, saves the baseline and exits 1. On a closed record it refuses with `the watch is closed; start a new one`.
- **`check --scheduled`.** With a failed pins read it makes no lookup, sends nothing, saves nothing and never closes: one `bevy_upgrade: pins could not be re-read: <reason>` line on stderr, exit 1. On a closed record it removes the job and exits 0 with no message. For a production with no record it exits 0 and leaves its job.
- **`approve` and `decline <crate> <words>`.** Store an `Answer` with the current date in the production's zone. `forget <crate>` sets the answer back to `None`. `note <crate> [<text>]` stores the text and, with no text, clears the note.
- **`stop`.** Closes the watch and removes the job. On a record already closed it keeps the first `closed` time.
- **`line [--json]`.** Takes no lock, saves nothing and never changes `announced`, so a reader never waits behind a running check. Freshly read pins go to its in-memory copy only. For an open record it makes two notifier calls, `ensure_job` and `next_check`, and no sender call. `--json` prints one object `{"update": <the line sentence>, "needs_user": <bool>}`, where `needs_user` is `waiting(record) > 0` on the record the sentence is built from. It prints nothing and exits 0 for no record or a closed one. With a failed pins read the sentence is all that stdout holds.
- **Exit codes.** 0 for success. 1 for a `check` whose pins re-read failed, and for a `check`, by hand or scheduled, whose `asked` is above zero and equals `failed`; the target lookup does not count. 2 for a refusal (`RecordFailure`, `PinsFailure` on `start`, `add_unit.Refusal`), which prints one `bevy_upgrade: <reason>` line on stderr.

### The command file

`commands/showrunner/bevy_upgrade.md` runs the CLI in the showrunner session and prints its output exactly as it comes, with nothing added; on a non-zero exit the showrunner reports the error in one line. It gives the showrunner these rules:

- Every statement about the upgrade in a report or reply comes from this command's output in the same turn, never from the log or memory.
- `approve` and `decline` run only on the user's own words about that crate, passed verbatim as one quoted argument. An answer for one crate never extends to another.
- `note` holds what a script cannot look up, such as why a pull request's CI fails.
- A report says `stale` when the table does.
- `line` prints one sentence for a report and changes nothing. `stop` ends the watch and its scheduled check.
- On a row reading `ask the user: git pin?`, the showrunner asks once and records the answer with `approve` or `decline`. A pin to an unmerged pull request's branch needs the same answer.
- A scheduled check writes to the session only when something changed. Its message is this command's output and the one exception to the same-turn rule: its lines are shown to the user in the next reply as received, and anything beyond them needs `status` first.

### The dailies topic

While a watch is open, every dailies carries a `Bevy upgrade` topic whose text is the `line` sentence, marked as needing the user while a git pin has no answer. The builder reads it from the upgrade command, never from the record file.

```python
# scripts/production/dailies_input.py
OpenBevyUpgradeWatch(update: str, needs_user: bool)
NoOpenBevyUpgradeWatch()
BevyUpgradeUnavailable(reason: str)
BevyUpgradeWatch = OpenBevyUpgradeWatch | NoOpenBevyUpgradeWatch | BevyUpgradeUnavailable
bevy_upgrade_watch(production: Production) -> BevyUpgradeWatch
bounded_bevy_update(text: str) -> str
```

- **Lookup.** `bevy_upgrade_watch` runs `<sys.executable> <script> --production <doc> line --json` with a 30-second timeout. Empty or whitespace-only output is `NoOpenBevyUpgradeWatch`. An object with a non-blank single-line `update` string and a boolean `needs_user` is `OpenBevyUpgradeWatch`; extra keys are ignored. Everything else is `BevyUpgradeUnavailable`: a non-zero exit, a timeout, an `OSError`, invalid JSON, or an object of another form. Its reason is the first stderr line when there is one, else `command exited <code>`, `timed out after 30 seconds`, or the error text of the parser or the operating system.
- **Topic.** `run()` adds the topic after the review watch topic, with the title `Bevy upgrade`, the ETA `no ETA stated yet` and the lookup's `needs_user`. For an unavailable lookup the text is `the record could not be read: <reason>` and `needs_user` is false. With no open watch there is no topic.
- **Cut.** `bounded_bevy_update` cuts the text to the smallest finite value in `dailies_render.LENGTHS`, ending in `…`.
- **Step line.** `bevy upgrade: ok — <text>`, `bevy upgrade: ok — no open watch`, or `bevy upgrade: unavailable — <text>`. A failed lookup never fails the builder.
- **`dailies.md`.** Gather item 7 says the builder adds the topic from the record while a watch is open and that nothing else in the report states the upgrade's status. Output says that on `bevy upgrade: unavailable — <reason>` the report continues and carries the topic with that reason.

### The migration plan's dependency check

`bevy_migration_dependency_check.py` reads the direct dependencies of every workspace member and asks crates.io about each one once, under its whole version, never faster than one request a second. It lists and classifies a crate that depends on Bevy only through one of Bevy's own crates, such as `bevy_app` or `bevy_ecs`, as it does one that depends on `bevy`. A crates.io lookup that gets no answer keeps the crate in the report for manual verification.

```python
# every class is a frozen dataclass
BevyRequirement(requirement: str)
NoBevyDependency()
RequirementLookupFailed(reason: str)
RequirementLookup = BevyRequirement | NoBevyDependency | RequirementLookupFailed
DiscoveredDependency(name: str, version: str, lookup: BevyRequirement | RequirementLookupFailed)
CompatibleVersion(version: str)
NoCompatibleVersion()
CompatibilityUnknown(reason: str)
BevyCheckoutRead(names: frozenset[str])
BevyCheckoutMissing(path: Path)
BevyCheckout = BevyCheckoutRead | BevyCheckoutMissing

get_bevy_internal_crates(bevy_version: str) -> BevyCheckout
bevy_requirement_names(bevy_checkout: BevyCheckout) -> frozenset[str]
run_cargo_tree(codebase: Path) -> str | None
query_crates_io(crate_name: str) -> QueryResult | None
get_bevy_dependency_requirement(crate_name: str, version: str, bevy_names: frozenset[str]) -> RequirementLookup
get_all_direct_dependencies(codebase: Path, bevy_checkout: BevyCheckout) -> list[tuple[str, str]]
get_bevy_dependencies(codebase: Path, bevy_checkout: BevyCheckout) -> list[DiscoveredDependency]
find_bevy_compatible_version(crate_name: str, all_versions: list[str], target_bevy_version: str,
                             bevy_checkout: BevyCheckout
                             ) -> CompatibleVersion | NoCompatibleVersion | CompatibilityUnknown
```

- **Bevy's own crate names.** `get_bevy_internal_crates` runs once, in `main`, and its result is passed down. A read checkout holds the package name of every `Cargo.toml` under `~/rust/bevy-<version>/crates`, plus `bevy` and `bevy_internal`: a requirement may come from any of them, and `get_all_direct_dependencies` leaves them out of its list. A missing checkout allows only `bevy` and leaves nothing out.
- **Direct dependencies.** `run_cargo_tree` runs `cargo tree --workspace --no-dedupe --depth=1 --format {p}`, first with `--all-features` and then, when that fails, with the default features; both attempts carry `--workspace` and `--no-dedupe`. `--workspace` covers a root manifest that is also a package, which cargo otherwise lists alone. `--no-dedupe` prints every member's dependencies in full: without it cargo prints a package it has already printed as `name vX (*)` with no children, so a member that an earlier member depends on hides its own direct dependencies. `get_all_direct_dependencies` drops Bevy's own crates, the workspace's members and a fixed list of utility crates.
- **Whole versions, each crate once.** `get_all_direct_dependencies` reads each line with `([a-zA-Z0-9_-]+)\s+v(\d\S*)`. The version is the whole token after `v`, so `1.1.6+spec-1.1.0`, `0.20.0-rc.1` and `0.5.0-dev` are kept whole and a trailing ` (proc-macro)` or path is left out. It collects `(name, version)` pairs in a set and returns them sorted, so a crate that several members list at one version is looked up once.
- **Lookup address.** `get_bevy_dependency_requirement` reads `https://crates.io/api/v1/crates/<name>/<version>/dependencies` with the version percent-encoded by `parse.quote(version, safe='')`: `1.1.6+spec-1.1.0` is requested as `1.1.6%2Bspec-1.1.0`. The crate name is not encoded. The whole version also reaches `classify_dependency`'s equality comparison and the report row, so a crate already on the compatible version, suffix included, is `OK`.
- **Pacing.** `query_crates_io` and `get_bevy_dependency_requirement` are the only two functions that reach crates.io, and each waits for the shared pacer immediately before `request.urlopen`. The check defines no pacing name: after its standard-library imports it puts `scripts/bevy_upgrade` first on `sys.path` and imports `crates_io_pacer`.
- **Request count.** One request for each direct dependency that `get_all_direct_dependencies` returns. Then, for each one that depends on Bevy or whose lookup failed, one for its crates.io entry and up to 20 for its recent releases: the walk stops at the first release that fits, so a blocker spends them all. A run of N requests takes at least N − 1 seconds.
- **The requirement rule.** Among a release's dependencies whose kind is not `dev`: the `req` of the one named `bevy`, else of the first by sorted name whose `crate_id` is in `bevy_names`. A crates.io request that fails, or answers in another form, is `RequirementLookupFailed`, never `NoBevyDependency`, and `get_bevy_dependencies` leaves a crate out only for `NoBevyDependency`.
- **Compatible version.** The newest of the 20 most recent versions whose requirement fits the target. `CompatibilityUnknown`, with the first failure's reason, when none fits and at least one lookup failed; else `NoCompatibleVersion`.
- **Report.** A release that fits decides the classification, `OK` or `UPDATE_REQUIRED`, whatever else failed. `CompatibilityUnknown` gives `CHECK_NEEDED` with `Could not read every release's dependencies from crates.io (<reason>) - manual verification needed`. No fitting release after a failed discovery lookup gives `CHECK_NEEDED` with `Could not read release <version>'s dependencies from crates.io (<reason>) - manual verification needed`. No fitting release for a crate known to depend on Bevy is a `BLOCKER`. A crate whose own crates.io entry cannot be read gives `CHECK_NEEDED` with `Could not query crates.io - manual verification needed`. `CHECK_NEEDED` entries stand under "Verification Needed".
- **Missing checkout.** `Warning: <path> not found; crates that depend only on a Bevy sub-crate are not detected` is printed once on stderr and stands under the report's title, on an empty report too.
- **Command file.** `commands/bevy_migration_plan.md` runs the paced dependency check in the background, waits for completion, and reads and deletes its output under `/tmp/claude/`. It counts guides from the resolved `GUIDES_DIR`, uses the Agent tool for both passes, and distributes applicable guides across at most 10 Pass 2 agents. Each agent returns one complete section per guide; the merge reads every section. The sole promised output is the migration plan. A project is in a production when it has one `*-production.md` under its `docs/`; the watch is open when `bevy_upgrade.py --production <doc> status` prints a record rather than `no Bevy upgrade is tracked` (it exits 0 either way). Next steps use the project's own gate (`verify.sh` where available), then a project in a production hands the plan to `/plan:to_phased_plan`. When that production has an open `/showrunner:bevy_upgrade` watch, both plans name its current dependency table as the source for dependency state.
- **Pattern counter.** `bevy_migration_count_pattern.sh` uses `rg --count-matches --no-filename`, sums counts across files, and excludes `vendor/` and `target/`, including nested directories. Exit 1 from rg becomes zero; other failures preserve stderr and return non-zero before any count or JSON is printed. Multiple and verify modes JSON-escape pattern keys; multiple mode separates the final pattern from `_total` with a comma. Counts measure matches, so several matches on one line all count. The command's two `rg` searches (the Search Pattern line in the Pass 2 template and the Pass 2 inspection search) carry the same `vendor/` and `target/` exclusions, so the diffs an agent writes cover the same code the counts do.

### Roots and overrides

- `BEVY_UPGRADE_STATE_DIR`: the record directory (`state_dir`), default `~/.local/state/bevy-upgrade`. It holds `<slug>.json`, `<slug>.lock` and `<slug>.<target>.closed.json`.
- `BEVY_UPGRADE_FIXTURES`: a JSON file of URL to body that stands in for the network in the CLI. Unset by default, which means `http_fetch`.
- `BEVY_UPGRADE_NOTIFIER`: the notifier script, default `scripts/message/notifier.sh`.
- `BEVY_UPGRADE_SEND`: the sender script, default `scripts/message/send.py`.
- `DAILIES_BEVY_UPGRADE`: the upgrade script the dailies builder calls, default `scripts/bevy_upgrade/bevy_upgrade.py`.
- `CLAUDE_CODE_SESSION_ID`: the session whose production is used when `--production` is absent. The default is `""`, which matches no production.
- `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `SHOWRUNNER_STATE_DIR`, `SHOWRUNNERS_CONFIG`, `SHOWRUNNERS_SESSIONS`: where `scripts/production/showrunners.py` finds a showrunner and its production, defaults `~/.local/state/notifier`, `~/.claude/sessions`, `~/.local/state/showrunner`, `config/showrunners.json` and `scripts/message/sessions.py`. `showrunners.py` reads them once, at import.
- The migration plan's dependency check reads no environment variable: the Bevy checkout is `rust/bevy-<version>` under `Path.home()`.

## Invariants

- Tests never reach the network, the real state or a real session. All HTTP goes through one injected `Fetch`, which the CLI takes from the file `BEVY_UPGRADE_FIXTURES` names when it is set; every path comes from `BEVY_UPGRADE_STATE_DIR` when set; the notifier and `send.py` are replaced through `BEVY_UPGRADE_NOTIFIER` and `BEVY_UPGRADE_SEND`.
- A failed lookup never replaces a stored fact. It sets that dependency's `error`, or the record's `target_error` for the target's own release, and leaves that entry's `checked` time alone. The table and the `line` sentence then say `stale`, so old data is never shown as current.
- The record stores what a script cannot look up again for free (user, 2026-10-08): upstream facts with the time they were read, and the user's answers and notes. State words are derived when printed and never stored, with one exception: `announced` holds the state word the showrunner was last shown for each crate. It records what was said, never what is true now.
- The CLI saves the record once per run, at the end of its locked block, after every change and check of that run. A refused verb saves nothing.
- Only `approve`, `decline` and `forget` change a recorded answer. No default approves, and an answer covers one crate in one upgrade.
- No project data in the code: no crate names, repositories or issue numbers. Everything comes from the checkout's `Cargo.lock`, crates.io and GitHub.
- No Pushover and no `send.py --to user`.
- Every upstream lookup takes a `Fetch` as its first argument and has one failure type, `FetchFailure`, for every network, HTTP or malformed-body fault, because `run_check` catches nothing else.
- An entry's crates.io and GitHub facts are stored together or not at all, and `run_check` never raises for a failed lookup or a spent budget.
- No request is made for an entry no check can move: `done`, a patched Bevy crate, a path pin, or a `ready` release.
- A cell that depends on a lookup prints `—` for a row no check will ever read (`done`, a patched Bevy crate, a path pin), never a `not checked` that cannot change.
- A name outside `bevy_names` is never read as Bevy.
- `Entry.crates_io`, `Entry.branch` and `Record.target_bevy` are tagged cases. `load` validates every field, so a field added to `Record`, `Entry` or a pin needs a starting value and a check in `_validate`, and a required `Record` field also needs the record fixture in `test_upgrade_check.py`.
- Stored `branch` and `items` belong to one repository, and an item number names an item only with it.
- `ask the user: git pin?` is the one Next cell that asks the user for an answer. `waiting` counts exactly those rows, and `needs_user` in the dailies comes from it.
- `announced` moves only when the showrunner has seen the change: a table printed by a hand verb, or a scheduled send that returned `True`.
- A watch closes only on a check whose pins re-read succeeded in the same run, and a scheduled check closes it only once its message was sent.
- The job is created and removed, and the queued message retired, inside the record lock, after the one save.
- `line` takes no lock, saves nothing, never changes `announced` and sends nothing. A scheduled check prints nothing on stdout and never calls `retire_message`.
- The dailies builder reads the watch through the command, never from the record file. A failed lookup reads `unavailable`, never `failed`, because `dailies.md` treats a `<step>: failed` line as a failed report. The dailies length limit has one copy, `dailies_render.LENGTHS`.
- In the migration plan's dependency check, a failed crates.io request is never read as "no Bevy dependency", and the Bevy checkout is read once per run. A crate is looked up under its whole version, and once however many workspace members list it.
- Within one process, crates.io requests start at least one second apart: every function that reaches crates.io, in the watch and in the migration check, waits for `crates_io_pacer.CRATES_IO_PACER` immediately before it opens the request. A GitHub request never waits on it.
- One pacer: `CRATES_IO_MIN_INTERVAL_SECONDS`, `RequestPacer` and `CRATES_IO_PACER` are defined in `crates_io_pacer.py` only, and nothing copies or re-exports them.
- Tests never wait on the real clock and never run cargo. Every test class that can reach a crates.io request replaces the shared pacer in `setUp` with one on a stand-in clock and a stand-in sleep, and the migration check's `setUp` fails any `subprocess.run` call.

## Calibration and gotchas

- **Schedule.** The job runs every 240 minutes with a 300 s limit. `CHECK_BUDGET` is 180 s. The budget is read before a fetch, and the fetch can then add one pacer sleep of at most 1 s and one request's 30 s timeout: 180 s + 1 s + 30 s, so a run ends inside the job's limit. The stale limit is eight hours, twice the interval. The scheduled job reads the exit code of `check --scheduled`, and that job is what keeps `Record.checked` inside eight hours.
- **Member search.** An entry whose requirement stays empty is read again on every check: up to seven requests (root, tree, five members).
- **GitHub's limit.** GitHub allows 60 API requests an hour without a token, and `port_items` reads one page of 100 per check, newest update first. A repository's first read makes 4 requests plus one per pull request. A following check makes 2, plus one for each kept open pull request and for each returned pull request whose `updated` moved, plus the manifest reads when the head moved or the requirement is still empty. One check of hana's ten dependencies makes 16 GitHub requests on a first read and 8 afterwards, counted without a member search or a kept pull request.
- **crates.io.** Requests start at least one second apart, which the pacer module's docstring gives as crates.io's published crawler limit. Measured at 22:01 EDT on 2026-10-12 through the real `http_fetch`: three crates.io lookups in a row answered at 0.22, 1.18 and 2.17 s, and a GitHub lookup after them took 0.27 s with no wait.
- **The limit is per process.** `CRATES_IO_PACER` is one object in each Python process. A watch check and a migration check running at the same moment each keep to one request a second and are not paced against each other.
- **A paced check and the record lock.** A check that makes N crates.io requests takes at least N − 1 seconds and holds the record lock that long, about 40 s for hana_catalyst's first read. A verb that takes the lock waits behind it. `line` takes no lock, so the dailies topic never waits.
- **The pacer's clock reading.** The pacer records the reading taken after its sleep, never the planned wake time. With the planned time, one sleep that runs 0.6 s long gives request starts at 0.0, 1.6 and 2.0 s, a gap of 0.4 s. A stand-in sleep that advances the clock by exactly the amount asked for cannot show this; the regression test's stand-in runs long.
- **Dailies cut.** 240 characters today. A change that lengthens the `line` sentence shows in the dailies cut at that limit.
- **Fetcher exceptions.** `http.client.IncompleteRead` and `BadStatusLine` are `http.client.HTTPException`, which is neither `OSError` nor `ValueError`. A fetcher that catches only those two lets a truncated or malformed response escape as a traceback.
- **`newest`.** It is crates.io's first version not yanked, which is publication order. A yanked version of the target Bevy still reads as `released`.
- **Spent budget.** About 180 crates.io requests is the most one check makes before `CHECK_BUDGET` cuts it short. The next check continues from there, and the only outward sign is that `Record.checked` does not advance.
- **Title filter.** `<target>` is matched as a substring of the title, so `0.20` matches `v0.20.0` and also any other title that holds those characters. An item created before `since` is dropped, so a port pull request opened before the target's first pre-release is not listed.
- **Workspace inheritance.** A member entry with `workspace = true` under a root entry that has no version gives an empty requirement.
- **Member failures.** A member that answers `NotFound` is skipped and counts as one of the five reads. Every other member failure is a `FetchFailure`, so the entry keeps its stored facts; this includes a wrongly typed table in a member that is not the crate's.
- **Which manifests exist.** `[workspace] members` and `exclude` are not read; the tree decides. GitHub marks a very large tree `truncated` and the lookup does not read that field, so such a repository can give an empty requirement.
- **Kept items.** They are copied before the refresh, so a failed request after it cannot change the stored entry.
- **Closing.** A watch with zero dependencies closes at its first `check`; `start` never closes one. Only `check` refuses on a closed record: `approve`, `decline`, `forget`, `note` and `stop` still run and save.
- **Archive name.** `start` after a closed watch replaces an existing `<slug>.<old target>.closed.json` of the same target.
- **Hand check with unreadable pins.** It exits 1 and still saves once, for the baseline, and retires the queued message. The scheduled form saves nothing.
- **`next_check`.** It raises `ValueError` for a `next_due=` line it cannot parse, or for a status answer without one, and nothing catches it.
- **Lock order.** The notifier takes its own lock only around its state edits and releases it before running a stored command, so a notifier call made under the record lock cannot deadlock with a firing job.
- **`line` and the job.** `line` calls `ensure_job` outside the lock. A job it re-creates for a watch closed in that instant removes itself at its next run.
- **Module names.** A module here needs a name no other directory under `scripts/` uses: `record.py` exists in `scripts/shutdown` and `scripts/buildlog`, `report.py` in `scripts/buildlog` and `scripts/model_study`, and `add_unit.py` puts `scripts/shutdown` first on `sys.path`. `bevy_upgrade.py` imports the production helpers first, then its own modules. Importing the migration check puts `scripts/bevy_upgrade` first on `sys.path`, so in that process a module here is found before a same-named module later on the path.
- **basedpyright.** It prints `venv .venv subdirectory not found` and exits 3 in this repo because `pyrightconfig.json` names a `.venv` no checkout has. Its result is the `0 errors, 0 warnings, 0 notes` line.
- **Execution environments.** An `executionEnvironments` entry rooted at a script directory makes basedpyright resolve that directory's imports from the directory itself. Under the `scripts/bevy_migration_plan` entry the check's test module imports `bevy_migration_dependency_check` as a sibling; the `scripts.bevy_migration_plan` package path is an unresolved import there. The test inserts its own resolved directory into `sys.path` before the sibling import, matching other `scripts/` tests; both repository-root package invocation and invocation from the test directory work.
- **Module tests.** They use a fetcher over a dict of URL to body that records each URL asked for and raises `FetchFailure` for an unknown one, and `test_upgrade_check.py` uses a clock that advances by a set step on each call. They need no network and, for `run_check`, no file. `test_upgrade_schedule.py` points `BEVY_UPGRADE_NOTIFIER` and `BEVY_UPGRADE_SEND` at stub scripts that record their arguments.
- **Pacer in the tests.** `UpstreamTests.setUp` installs a pacer on a stand-in clock and sleep, and a `urllib.request.urlopen` stand-in that fails any test reaching the network; an `http_fetch` test replaces `urlopen` for itself. Its four pacing tests cover one wait before a crates.io request, no wait for either GitHub host, one one-second sleep between two crates.io requests, and no pacing through `fixture_fetch`. The five `RequestPacer` tests sit in `test_crates_io_pacer.py` on their own stand-in clock: no sleep on the first call, the remaining 0.75 s after 0.25 s, no sleep after the interval, two one-second sleeps for three calls in a row, and a sleep that runs long. The stand-in clock class has one copy in each of the three test modules that pace, because a test module here imports no other test module; a change to it is made in all three.
- **`test_cli.py`.** It runs the CLI as a subprocess with `--production`, `BEVY_UPGRADE_STATE_DIR` and a fixtures file named by `BEVY_UPGRADE_FIXTURES`, and its subprocess rejects real network access with `a test attempted network access`. Every CLI run names a fixtures file, so no CLI test reaches `http_fetch` or waits on the real pacer. The notifier and sender stand-ins record every call with its arguments, whether the record lock was held and the saved `announced` and `closed`: `tool_calls(kind)`, `tool_args(kind)` (sends only for `send`), `retire_calls()`, `retire_arguments()`, `clear_calls()`. It counts saves by wrapping the real `save`. `write_fixtures` holds the GitHub answers for `example/adapter`, which a CLI test needs when it runs `check` on an entry with no `ready` release. Every fixture with a read branch needs `repository`, `name`, `rev`, `committed` and `requirement`.
- **Dailies tests.** Every test in `test_dailies_input.py` runs behind a `DAILIES_BEVY_UPGRADE` stand-in set in the shared `setUp`, so none reaches the real upgrade command, which for an open watch calls the notifier. The timeout test replaces `subprocess.run` in-process and waits for nothing.
- **Migration check, live.** The script runs `cargo tree` on the codebase, so a live run counts as a build on that project. `/bevy_migration_plan` clones `~/rust/bevy-<version>` itself; a machine that has only `~/rust/bevy` reports the missing checkout. A direct dependency that is not on crates.io fails its lookup and is listed under "Verification Needed". The script reads the direct dependencies of every workspace member and nothing deeper. Its run time follows the request count, not the dependency count. Measured on 2026-10-12 against hana_catalyst for Bevy 0.20.0: exit 0 in 129 s, 45 crates asked about and every request answered; `bevy_tween` 0.13.0 and `bevy_lookup_curve` 0.12.0 are listed, both as blockers, `moonshine-save` is listed and `toml` is not.
- **Shared crates under `--no-dedupe`.** The flag repeats a shared crate under every member that uses it. Only the set in `get_all_direct_dependencies` keeps that to one lookup.
- **Build metadata.** crates.io answers 404 for `/api/v1/crates/toml/1.1.6/dependencies` and 200 for `/api/v1/crates/toml/1.1.6%2Bspec-1.1.0/dependencies` (measured 2026-10-12). A version cut off at its suffix reads as a failed lookup and a `CHECK_NEEDED` row.
- **Migration check, tests.** `test_bevy_migration_dependency_check.py` uses canned crates.io bodies behind a replaced `urlopen` and a temporary home directory, and any `subprocess.run` call fails the test, so no cargo runs. Its `setUp` replaces the shared pacer at `crates_io_pacer.CRATES_IO_PACER` with one on a stand-in clock and sleep, and one test checks that the three pacing names are absent from the check. The module adds its own directory to `sys.path` before the sibling import (`import bevy_migration_dependency_check as checker`). Both `python3 -m unittest scripts.bevy_migration_plan.test_bevy_migration_dependency_check` from the repository root and `python3 -m unittest test_bevy_migration_dependency_check` from its own directory work. The merge gate finds both test modules under `scripts/bevy_migration_plan` and reports them as that folder's suite.
- **Two copies of the requirement rule.** `upstream.release_requirement` and `get_bevy_dependency_requirement` hold the same rule, and both percent-encode the version in the lookup address. A change to one needs the same change in the other.

## Why

- **A record, with every report line read from it.** The user asked for "some kind of note for tracking the upgrade so your report doesn't have bad information" (user, 2026-10-12). A statement taken from memory or a log goes out of date; one taken from the command's output in the same turn carries the time of its lookup and says `stale` when it is.
- **Facts are saved per dependency.** The user asked for a skill that "not only does the periodic check but also saves information about each dependency so we can limit the work we do" (user, 2026-10-12). A release's requirement is read once, a branch's manifest once per head, an unchanged pull request not at all, and an entry no check can move is never asked about.
- **Stored facts, derived words.** The record holds what a script cannot look up again for free (user, 2026-10-08). A state word computed from the facts at print time cannot disagree with them.
- **`announced` holds state words, not a digest.** An opaque per-crate digest would show that something changed, but the change lines need the previous state word.
- **An answer per crate, verbatim.** A git pin is the user's call ("if we do a git dependency which i would approve", user, 2026-10-12), so the record keeps the words and the date, no default approves, and one crate's answer never covers another.
- **One request a second to crates.io.** It is crates.io's published crawler limit, and a check sends many requests in a row: about 40 on hana_catalyst's first read by the watch, and more from a migration check of a workspace.
- **One pacer module, imported by both scripts.** A second copy in the watch, or a re-export from the migration check, would be a second definition to keep equal to the first.
- **GitHub hosts are not paced.** GitHub has its own limit, and the watch already keeps to it (**GitHub's limit** under Calibration and gotchas).
- **The pacer sleeps inside the fetch.** `run_check` reads the clock before each fetch, so `CHECK_BUDGET` already brackets the sleep and neither it nor `run_check` needs a rule for pacing.
- **The clock is read again after a sleep.** Recording the planned wake time lets a sleep that runs long shorten the next gap below the interval.
- **The command file tells the runner to wait.** A paced migration check runs longer than a default 120 s command limit.
- **The check's tests import their module as a sibling.** The check's directory has its own execution environment so that basedpyright resolves `crates_io_pacer`, and under that entry the `scripts.bevy_migration_plan` package path does not resolve.
- **No test asserts that the check's pacer is the shared object.** Reaching it through a private module import draws a basedpyright warning, and the two caller-order tests already prove the check uses the replaced pacer.
- **The issues list with a substring filter, not GitHub's search.** The search does not match `0.20` inside `v0.20.0` (Multirious/bevy_tween pull request 97, "Migrate to Bevy v0.20.0", checked 2026-10-12).
- **No token.** The `gh` token sits behind gpg-agent, which is cold after every reboot, and a check failing then would leave the record stale.
- **`load` fills `branch` and `items`; `apply_pins` does not.** Every record reaches `apply_pins` through `load`, so a stored entry already has both there.
- **Job upkeep inside the lock.** With the upkeep outside it, a `start` between a closing run's save and its job removal lost its new job.
- **Every saving hand verb retires the queued message.** That is one rule with no state to read. Retiring only when changes were pending would need that state.
- **`1 change` for a single line.** `<N> changes` reads wrongly for one.
- **A scheduled close waits for its message.** The watch stays open, and its job keeps running, until the showrunner has been sent the closing sentence.
- **The builder takes the length limit from the renderer.** A second copy of the 240 limit in the builder could differ from the renderer's.
- **The `line` sentence stays whole.** The dailies cut happens in the builder; the showrunner's own report and the scheduled message get the full sentence.
- **The migration check keeps its own requirement rule.** It prints its own warnings, so it does not import `scripts/bevy_upgrade/upstream.release_requirement`. The pacer is the one module it takes from the watch's directory.
- **The tree lookup, not `[workspace] members` globs.** The tree lists the manifests that exist and needs no glob rules.
- **A truncated tree is not followed.** The search is bounded and answers unknown instead of making further requests.

## Steps still to run

These steps act in another production's session and on its state, so the hana showrunner runs them there. None has been run.

1. Once the scheduled check is on main, the hana showrunner runs `/showrunner:bevy_upgrade start 0.20`.
2. It then runs `approve bevy_enhanced_input` with the user's words quoted below.
3. It then runs `note` for what no lookup finds: bevy_tween pull request 97's CI fails only in its `bevy_tween_lookup_curve` sub-crate, which pins bevy_lookup_curve 0.12, and issue 91 asks for a crates.io release.
4. It then removes the hand-written watch: `zsh ~/.claude/scripts/message/notifier.sh remove bevy020-watch`, and the directory `~/.local/state/bevy020-watch/`.

The hand-written watch listed five crates. Hana's lockfile has nine third-party crates on Bevy and one patched Bevy crate; `bevy_egui`, `bevy_time_runner`, `moonshine-save` and `moonshine-util` were not watched.

The user's words, 2026-10-12 11:54 PDT, in full:

> okay - we should have some kind of note for tracking the upgrade so your report doesn't have bad information - are we managing this with a skill or just the script that runs ever 4 hours or what? if not, we should ask natedev to create a bevy upgrade skill for showrunners - that not only does the periodic check but also saves information about each dependency so we can limit the work we do - we can upgrade to 0.20.0 on bei now if we do a git dependency which i would approve

Step 2 passes the user's words about that crate verbatim, as the command's rule requires. The clause that approves the git pin is `we can upgrade to 0.20.0 on bei now if we do a git dependency which i would approve`; "bei" is bevy_enhanced_input.
