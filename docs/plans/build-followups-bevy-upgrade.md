# Bevy upgrade: one record of every dependency, kept current for the showrunner

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/showrunner:bevy_upgrade` keeps one record per production of every crate that ties the project to its Bevy version: our pin, what crates.io and GitHub offer for the target, and the user's answer on git pins. A scheduled check keeps it current, and every report line comes from it.

> **As-built disposition: create**

> **Production: build-followups** — unit `bevy-upgrade-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 11:54 PDT

okay - we should have some kind of note for tracking the upgrade so your report doesn't have bad information - are we managing this with a skill or just the script that runs ever 4 hours or what? if not, we should ask natedev to create a bevy upgrade skill for showrunners - that not only does the periodic check but also saves information about each dependency so we can limit the work we do - we can upgrade to 0.20.0 on bei now if we do a git dependency which i would approve

## Delegation Context

- **Project:** `~/.claude` tooling (this repo) — a showrunner skill that tracks a Bevy upgrade's dependencies.
- **Project started:** 2026-10-12T19:08:41.107+00:00
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-bevy-upgrade`, branch `build-followups-bevy-upgrade`. Every edit and every command of every phase runs there (the user, 2026-10-12).
- **Stack:** Python 3.11+ standard library only (`tomllib`, `json`, `urllib.request`, `fcntl`, `argparse`, `zoneinfo`, `shlex`, `subprocess`); the zsh notifier.
- **Layout:** `scripts/bevy_upgrade/` (new) holds every module and its tests; `commands/showrunner/bevy_upgrade.md` (new) is the skill; `pyrightconfig.json` gains one entry. The dailies phase alone edits `scripts/production/dailies_input.py`, its test and `commands/showrunner/dailies.md`. The last phase alone edits `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` and adds its test.
- **Key files:**
  - `scripts/production/showrunners.py` — `production_of(session_id) -> Path | None` (line 217), `current_address(slug) -> str` (line 243; empty while the showrunner is not running).
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (line 196) with fields `doc`, `slug`, `merge_branch`, `checkout`, `log`, `zone`; raises `Refusal`.
  - `scripts/message/notifier.sh` — `new <instance> --every <min> --run <cmd> [--timeout <s>]` (a `--run` job takes no `--to`, `--from`, `--check`, `--hold` or `--aligned`), `remove <instance>`, `status <instance>` (its second line is `next_due=<epoch> (<local time>)`; exit 1 for no such instance). State root: `NOTIFIER_STATE_DIR`, else `~/.local/state/notifier`.
  - `scripts/message/send.py` — `--to <address> --from <sender> --key <key> --summary <text> --text <text>`.
  - `scripts/production/proposals_table.py` and `commands/showrunner/proposals.md` — the pattern for a showrunner table script and its command file.
  - `scripts/production/dailies_input.py` lines 852–861 — how the review watch becomes a dailies topic.
  - `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the repo's reference for typed crates.io access without `Any`.
- **Test lanes:** `scripts/bevy_upgrade/test_*.py`, beside the modules; `scripts/production/test_dailies_input.py` in the dailies phase.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s scripts/bevy_upgrade -p 'test_*.py'`
- **Lint:** `basedpyright scripts/bevy_upgrade` prints `0 errors, 0 warnings, 0 notes` (it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has; read the counts).
- **Invariants:**
  - Tests never reach the network, the real state or a real session. All HTTP goes through one injected `Fetch`, which the CLI takes from the file `BEVY_UPGRADE_FIXTURES` names when it is set; every path comes from `BEVY_UPGRADE_STATE_DIR` when set; the notifier and `send.py` are replaced through `BEVY_UPGRADE_NOTIFIER` and `BEVY_UPGRADE_SEND`.
  - A failed lookup never replaces a stored fact. It sets that dependency's `error`, or the record's `target_error` for the target's own release, and leaves the `checked` time alone, so old data is never shown as current.
  - The record stores what a script cannot look up again for free (user rule 2026-10-08): upstream facts with the time they were read, and the user's answers and notes. State words are derived when printed and never stored, with one exception: `announced` holds the state word the showrunner was last told for each crate. It records what was said, never what is true now.
  - The CLI saves the record once per run, at the end of its locked block, after every change and check of that run. A refused verb saves nothing.
  - Only `approve`, `decline` and `forget` change a recorded answer. No default approves, and an answer covers one crate in one upgrade.
  - No project data in the code: no crate names, repositories or issue numbers. Everything comes from the checkout's `Cargo.lock`, crates.io and GitHub.
  - No Pushover and no `send.py --to user`.
  - Python rules (CLAUDE.md): annotate every signature; `TypedDict` for JSON with known keys; no file-level ignores; a line-level `# pyright: ignore[reportAny]` only where `json` or `urllib` returns `Any`.
  - A seat that edits a file under `commands/` loads `/succinct_style` first.

## Phases

### Phase 1 — A showrunner starts a watch and reads pins and the user's answers from the record  · status: done

#### As-built

`/showrunner:bevy_upgrade` keeps one record per production: every crate in the production checkout's lockfile that depends on Bevy, its pin, and the user's answer and note for it. Every verb ends by printing one table. The four modules use flat imports (`import pins`).

- **`pins.py`**: `read_pins(checkout: Path) -> Pins` reads `<checkout>/Cargo.lock` and `<checkout>/Cargo.toml` with `tomllib`; no cargo command runs. It raises `PinsFailure(Exception)` with `<path>: <reason>` for a missing or unparseable file, a dependency string that does not resolve to one package, or a lockfile with no `bevy_ecs` package.
  - `Pin = RegistryPin | GitPin | PathPin`: `TypedDict`s tagged `source: Literal["registry"]`, `Literal["git"]` and `Literal["path"]`, each with `version: str`. `GitPin` adds `url: str` (no query or fragment), `ref: str` (the lock source's query, `branch=…`, `rev=…` or `tag=…`, or `""`) and `rev: str` (the full commit after `#`). `PathPin` adds `path: str`, as the `[patch]` table writes it.
  - `Dependent(NamedTuple)`: `name: str`, `pin: Pin`, `bevy: str`, `via: tuple[str, ...]`, `patched_bevy: bool`. `Pins(NamedTuple)`: `bevy: tuple[str, ...]` (oldest first), `bevy_names: frozenset[str]`, `dependents: tuple[Dependent, ...]` (sorted by name).
  - The Bevy versions are the versions of the package `bevy_ecs`. A Bevy crate is a package named `bevy` or `bevy_*` at one of those versions that has a `registry+` source or is replaced by a `[patch.*]` entry of the root manifest.
  - A dependent is any other package with a `source` whose `dependencies` name a Bevy crate. Its `bevy` is the oldest Bevy version among the Bevy crates it lists; of two lock versions of one dependent, the one on the oldest Bevy is kept, chosen before any pin is built. Its `via` is empty when a package with no `source` lists it, otherwise the sorted names of the packages that list it.
  - A `[patch.*]` entry belongs only to the lock package it replaced (`_is_patched`: a path patch and no `source`, or a git patch and a `git+` source). A patched Bevy crate is a row with `patched_bevy=True` and `bevy` equal to its own lock version. A `registry+` package always gets a `RegistryPin`. Every other package with no `source` is the project's own and is never a row.
- **`upgrade_record.py`** owns `<state dir>/<slug>.json`: `state_dir() -> Path` is `BEVY_UPGRADE_STATE_DIR` when set, else `~/.local/state/bevy-upgrade`, and `record_path(slug: str) -> Path` is the file. Failures raise `RecordFailure(Exception)`.
  - `Answer(TypedDict)`: `answer: Literal["approved", "declined"]`, `at: str` (`YYYY-MM-DD` in the production's zone), `words: str` (verbatim). `Entry(TypedDict)`: `pin: Pin`, `bevy: str`, `via: list[str]`, `patched_bevy: bool`, `answer: Answer | None`, `note: str`. `Record(TypedDict)`: `slug: str`, `production: str` (the doc's path), `checkout: str`, `target: str` (major.minor), `started: str` and `pins_read: str` (ISO-8601 UTC), `closed: str | None` (`None` while the watch is open), `bevy: list[str]`, `dependencies: dict[str, Entry]`.
  - `locked(slug: str) -> Generator[None, None, None]`, a `@contextmanager`, holds an exclusive flock on `<state dir>/<slug>.lock` around every read-modify-write. Only directory creation, open and flock errors become `RecordFailure`; an error from the caller's body propagates unchanged.
  - `load(slug: str) -> Record | None` returns `None` when no file exists and raises `RecordFailure` when the file is unreadable or `_validate` rejects any field. `save(record: Record) -> None` writes a temp file beside the record, then replaces it.
  - `start(production: Production, target: str, pins: Pins, now: datetime) -> Record` refuses a target that is not `\d+\.\d+`. An open record for the same target comes back after `apply_pins`; one for another target refuses with `a watch for <target> is open; stop it first`; a closed record moves to `<slug>.<old target>.closed.json` and a new one begins without its answers and notes.
  - `apply_pins(record: Record, pins: Pins, now: datetime) -> Record` adds new dependents with `answer=None` and `note=""`, updates `pin`, `bevy`, `via` and `patched_bevy` of known ones, keeps their other fields, and removes entries the lockfile no longer has.
  - `set_answer(record: Record, crate: str, answer: Literal["approved", "declined"], words: str, today: str) -> Record`, `forget_answer(record: Record, crate: str) -> Record` and `set_note(record: Record, crate: str, text: str) -> Record` refuse an unknown crate and name the tracked ones. `set_answer` refuses empty or whitespace-only `words`; a second answer replaces the first.
  - `on_target(version: str, target: str) -> bool` is true for the same major.minor.
- **`upgrade_report.py`**: `State = Literal["git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done"]`, and `STATE_ORDER: tuple[State, ...]` is that order.
  - `state(entry: Entry, target: str) -> State` is `done` when `on_target(entry["bevy"], target)`; else `re-vendor` when `patched_bevy`; else `not checked`.
  - `table(record: Record, zone: ZoneInfo, now: datetime) -> str` prints, in order: `Bevy <target> upgrade for <slug>: <checkout>, on Bevy <versions joined by " and ">.`; `Upstream: not checked yet.`; a blank line; a Markdown table headed `| Dependency | Our pin | crates.io | Git | State | Next |`, rows sorted by `STATE_ORDER`, then name; then, when any exist, `Answers on git pins:` with one `- <crate>: <answer> <at>: "<words>"` line per recorded answer, whatever the pin source, and `Notes:` with one `- <crate>: <note>` line each.
  - Cells. Dependency: `<name>`, `<name> (via <a>, <b>)` or `<name> (patched Bevy crate)`. Our pin: `<version>`, `git <ref or first 7 of rev> @ <first 7 of rev>` or `path <path>, <version>`, each followed by ` on Bevy <bevy>` unless `patched_bevy`. crates.io and Git: `—`. Next: `—` for `done`, `re-vendor from Bevy <target>` for `re-vendor`, `run check` for `not checked`.
- **`bevy_upgrade.py`**: `bevy_upgrade.py [--production <doc>] <verb>`, with the verbs `status` (also the default), `start <target>`, `approve <crate> <words>`, `decline <crate> <words>`, `forget <crate>` and `note <crate> [<text>]`; `note` with no text clears the note.
  - Without `--production` the doc is `showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))`; with none, it refuses with `this session runs no production; pass --production <doc>`. `add_unit.read_production` reads the doc: its `checkout` is the tree whose lockfile is read, and its `zone` is the zone of every printed time and of an answer's `at`.
  - Every verb but `start` on a production with no record prints `no Bevy upgrade is tracked for <slug>; start one with /showrunner:bevy_upgrade start <version>` and exits 0.
  - Every verb re-reads the pins from the checkout and applies them before it prints. When that read fails on a verb other than `start`, the table prints from the stored pins under the first line `pins could not be re-read: <reason>; showing pins read <HH:MM zone>`.
  - A refusal (`RecordFailure`, `PinsFailure` on `start`, `add_unit.Refusal`) prints one `bevy_upgrade: <reason>` line on stderr and exits 2.
- **`commands/showrunner/bevy_upgrade.md`** runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/bevy_upgrade/bevy_upgrade.py" $ARGUMENTS` in the showrunner session and prints the output exactly as it comes. It gives three rules: a statement about the upgrade comes from this command's output in the same turn, never from the log or memory; `approve` and `decline` run only on the user's own words about that crate, verbatim, and never extend to another crate; `note` holds what a script cannot look up.

**Files:**
- `scripts/bevy_upgrade/pins.py` — the lockfile and manifest reader.
- `scripts/bevy_upgrade/upgrade_record.py` — the record's types, lock, validation, store, pins merge, answers and notes.
- `scripts/bevy_upgrade/upgrade_report.py` — the derived state and the table.
- `scripts/bevy_upgrade/bevy_upgrade.py` — the CLI.
- `scripts/bevy_upgrade/test_pins.py`, `test_upgrade_record.py`, `test_upgrade_report.py`, `test_cli.py` — the tests; `test_cli.py` runs the CLI as a subprocess with `--production` and `BEVY_UPGRADE_STATE_DIR`.
- `commands/showrunner/bevy_upgrade.md` — the skill.
- `pyrightconfig.json` — one `executionEnvironments` entry: root `scripts/bevy_upgrade`, extraPaths `scripts/bevy_upgrade` and `scripts/production`.

**Binds later work:**
- One save point: the CLI saves once, at the end of its single `locked` block. `start` and every mutating verb save; `status` saves only when the pins re-read succeeded; a refused verb saves nothing. A new verb changes the record inside that block and adds no save of its own. `test_cli.py` counts saves by running the CLI in a subprocess with the real `save` wrapped.
- `load` validates every field, so a field added to `Record`, `Entry` or a pin needs a starting value and a check in `_validate`.
- No verb closes a watch: `closed` is never set, the archive branch of `start` runs only in tests, and the refusal text `stop it first` refers to a verb that does not exist.
- `state` yields only `done`, `re-vendor` and `not checked`; the other four words of `State` are declared and never produced. A path-pinned dependent reads `not checked` with Next `run check`. The table's second line is the fixed `Upstream: not checked yet.`, and `table` does not read `zone` or `now`.

**Gotchas:**
- A module here needs a name no other directory under `scripts/` uses: `record.py` and `report.py` exist in `scripts/shutdown` and `scripts/buildlog`, and `add_unit.py` puts `scripts/shutdown` first on `sys.path`. `bevy_upgrade.py` imports the production helpers first, then `pins`, `upgrade_record` and `upgrade_report`; nothing else on that import path is named `pins`.
- basedpyright exits 3 for the missing `.venv`; its counts line is the result.

**Ruled out:** an opaque per-crate digest for change detection — the change lines need the previous state word.

### Phase 2 — `check` records each dependency's crates.io releases and which one is on the target  · status: todo

#### Work Order

**Goal:** `/showrunner:bevy_upgrade check` reads crates.io once per dependency that still needs it and records the newest release, the release built on the target, and whether the target Bevy is itself released.

**Spec:**
Work only in the worktree and branch the Delegation Context names.

Types follow the user's Type Design Contract. A value that can be missing in two ways, or several values that are present or missing together, is a `TypedDict` union tagged by a `status` key, as `Pin` is tagged by `source`. One field whose absence has one meaning keeps an empty value or `None`, with a comment that says what the absence means. No `bool | None`.

`scripts/bevy_upgrade/upstream.py` holds every lookup and the types they return. Each lookup takes the fetcher as its first argument.

```python
Fetch = Callable[[str], bytes]                # GET one URL; raises FetchFailure on any failure
class FetchFailure(Exception): ...            # str() is "<host>: <reason>", e.g. "crates.io: HTTP 503"
def http_fetch(url: str) -> bytes: ...        # urllib, User-Agent "bevy-upgrade (natepiano)", timeout 30 s
def fixture_fetch(path: Path) -> Fetch: ...   # serves each URL from a JSON file of URL to body

class CrateInfo(NamedTuple):
    versions: tuple[str, ...]    # not yanked, in the API's order (newest first)
    repository: str              # "owner/name" on github.com; "" when the crate names no repository there
def crate_info(fetch: Fetch, crate: str) -> CrateInfo: ...
def release_requirement(fetch: Fetch, crate: str, version: str, bevy_names: frozenset[str]) -> str: ...

class TargetUnpublished(TypedDict):     # crates.io has no version on the target, pre-releases included
    status: Literal["unpublished"]
class TargetPrerelease(TypedDict):      # every version on the target is a pre-release
    status: Literal["pre-release"]
    since: str                          # YYYY-MM-DD of the first version on the target
class TargetReleased(TypedDict):
    status: Literal["released"]
    version: str                        # the newest release on the target that is not a pre-release
    date: str                           # its YYYY-MM-DD
    since: str                          # YYYY-MM-DD of the first version on the target, pre-releases included
def target_release(fetch: Fetch, target: str) -> TargetUnpublished | TargetPrerelease | TargetReleased: ...
```

- `crate_info` reads `https://crates.io/api/v1/crates/<crate>`: `versions[].num` and `.yanked`, and `crate.repository`. A repository URL on github.com gives `owner/name` with any `.git` suffix and trailing path removed; any other URL, or none, gives `""`.
- `release_requirement` reads `https://crates.io/api/v1/crates/<crate>/<version>/dependencies` and returns the `req` of the dependency named `bevy` when one of kind other than `dev` exists, else the `req` of the first such dependency, by sorted name, whose `crate_id` is in `bevy_names`, else `""`. A name outside `bevy_names` is never read as Bevy: `bevy_egui` is a third-party crate.
- `target_release` reads `https://crates.io/api/v1/crates/bevy/versions` and looks at each `num` that starts with `<target>.`, with its `created_at`. No such version gives `TargetUnpublished`; only pre-releases give `TargetPrerelease`; otherwise `TargetReleased`.
- `fixture_fetch` returns a fetcher over the file at `path`; a URL the file lacks raises `FetchFailure("<host>: no fixture")`.

`upgrade_record.py` grows:

```python
class CratesIoUnread(TypedDict):     # crates.io has not been read for this crate
    status: Literal["unread"]
class CratesIoRead(TypedDict):
    status: Literal["read"]
    checked: str                 # ISO-8601 UTC of the last complete check of this crate
    releases: dict[str, str]     # version → its Bevy requirement; a release never changes, so each is read once
    newest: str                  # the newest version that is not yanked
    ready: str                   # the newest release whose requirement is on the target; "" while none is
    repository: str              # "owner/name" on github.com; "" when the crate names no repository there
class TargetUnread(TypedDict):       # no lookup of the target's Bevy release has succeeded yet
    status: Literal["unread"]
TargetBevy = TargetUnread | TargetUnpublished | TargetPrerelease | TargetReleased

class Entry(TypedDict):          # added fields
    crates_io: CratesIoUnread | CratesIoRead
    error: str                   # the last check's failure for this crate, or ""
class Record(TypedDict):         # added fields
    target_bevy: TargetBevy
    target_error: str            # the last failure of the target's release lookup, or ""
    checked: str | None          # ISO-8601 UTC of the last check that reached every entry; None before the first

TargetFit = Literal["on target", "off target", "unreadable"]
def target_fit(requirement: str, target: str) -> TargetFit: ...
```

- `target_fit` strips one leading `^`, `~` or `=`; a remainder matching `\d+\.\d+(\.\d+)?(-[\w.]+)?` is compared by major.minor and gives `on target` or `off target`. Anything else (a range, `*`, an empty string) is `unreadable`.
- `load` accepts a record the first phase wrote, which has none of the added fields: a missing field takes its starting value (`crates_io` and `target_bevy` `{"status": "unread"}`, `error` and `target_error` `""`, `checked` `None`). A field that is present is validated as the first phase's fields are, and a wrong type is a `RecordFailure`. `apply_pins` gives a new entry the starting values.

`scripts/bevy_upgrade/upgrade_check.py`:

```python
Clock = Callable[[], datetime]              # the current UTC time; tests pass a fake
CHECK_BUDGET = timedelta(seconds=180)       # plus one request's 30 s timeout, a pass ends inside the scheduled job's 300 s limit
class CheckPass(NamedTuple):
    record: Record
    asked: int      # entries that made at least one request
    failed: int     # of those, the entries whose lookup failed
def run_check(record: Record, pins: Pins, fetch: Fetch, clock: Clock) -> CheckPass: ...
```

- The pass's `now` is `clock()` at its start. Before every request `run_check` reads the clock. Once `CHECK_BUDGET` has passed it makes no further request, leaves the entry it was reading exactly as it was (its `error` too), and returns. It never raises for a failed lookup or a spent budget, so the caller always saves what the pass learned.
- The target comes first. While `record["target_bevy"]["status"]` is not `released`, call `target_release` once per pass, store its result and set `target_error` to `""`. A `FetchFailure` there leaves `target_bevy` as it was and sets `target_error` to the failure's text; the entries still run. Once the status is `released` this lookup never runs again.
- Entries run with those never read first, then the oldest `crates_io["checked"]` first, then by name, so the next pass continues one the budget cut short.
- Per entry: no request at all when the entry is `done`, is a patched Bevy crate, has a path pin, or already has `ready`. Otherwise call `crate_info`; walk its versions from the newest down to and including our pinned version (all of the newest six when the pin is not among them); call `release_requirement` only for versions missing from `releases`; then store `CratesIoRead` with `newest`, `repository`, the grown `releases`, `ready` (the newest walked version whose `target_fit` is `on target`, else `""`) and `checked` set to the pass's `now`, and set `error` to `""`.
- A `FetchFailure` in one entry sets its `error` to the failure's text and leaves every other field of that entry as it was, `crates_io` included. The other entries still run. A crate whose every version is yanked fails the same way, with `crates.io: no version that is not yanked`.
- `record["checked"]` is set to the pass's `now` when the pass reached every entry, whether or not entries failed. A pass the budget cut short leaves it as it was.

`upgrade_report.py`:

- `state` is now, in order: `done`; `re-vendor` when `patched_bevy` or the pin is a path pin; `release ready` when `crates_io` is read and `ready` is not `""`; `not checked` when `crates_io` is `unread`; else `no port yet`.
- The crates.io cell reads `<newest> on Bevy <requirement>`, with ` → <ready> on Bevy <its requirement>` appended when `ready` is set and differs from `newest`. A requirement of `""` prints as `requirement unknown` in place of `on Bevy <requirement>`. The cell is `—` for a patched Bevy crate or a path pin, and `not checked` while `crates_io` is `unread`. An entry with `error` appends `; last check failed: <error>` to that cell.
- Next: `bump to <ready>` for `release ready`; `wait` for `no port yet`; `run check` for `not checked`. For `re-vendor` it stays `re-vendor from Bevy <target>` for a patched Bevy crate and reads `port the copy at <path> to Bevy <target>` for a path-pinned dependent, which no check can move.
- The second line of `table` has three parts. The target: `Bevy <target>: released as <version> on <date>.` for `released`, `Bevy <target>: not released yet.` for `pre-release` and `unpublished`, `Bevy <target>: release not looked up yet.` for `unread`. Then ` Release lookup failed: <target_error>.` when `target_error` is not `""`. Then the check: ` Upstream: no complete check yet.` while `checked` is `None`; else ` Upstream checked <HH:MM zone>.`; or ` Upstream last checked <YYYY-MM-DD HH:MM zone> (stale).` when `checked` is older than eight hours, any entry has `error`, or `target_error` is not `""`.

`bevy_upgrade.py`:

- `check`: inside the locked block, after the pins re-read, `run_check` with the fetcher and `lambda: datetime.now(timezone.utc)`; then the one save; then the table. The fetcher is `fixture_fetch(Path(<value>))` when `BEVY_UPGRADE_FIXTURES` is set, else `http_fetch`.
- `start` runs the same check after `upgrade_record.start` and before the one save.
- The save stays where the first phase put it: once, at the end of the locked block. A refused verb saves nothing.
- When the pins re-read fails on `check`, no lookup runs (the Bevy crate names come from the lockfile). It prints the `pins could not be re-read` line and the table from the stored record, saves nothing, and exits 1.
- `check` exits 1 when `asked` is above zero and equals `failed`, else 0. The target lookup does not count. `start` exits 0 once the record is written, whatever its check found.

The command file gains `check` and the stale rule: a report says `stale` when the table does.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — new: the two fetchers, the crates.io lookups and the types they return.
- `scripts/bevy_upgrade/upgrade_check.py` — new: one check pass, its skip rules and its time budget.
- `scripts/bevy_upgrade/upgrade_record.py` — the upstream fields, their starting values and validation, and `target_fit`.
- `scripts/bevy_upgrade/upgrade_report.py` — `release ready`, `no port yet`, a path pin as `re-vendor`, the crates.io cell, the second line.
- `scripts/bevy_upgrade/bevy_upgrade.py` — the `check` verb; `start` runs it; the fetcher choice.
- `scripts/bevy_upgrade/test_upstream.py` — new: each lookup against canned response bodies, and `fixture_fetch`.
- `scripts/bevy_upgrade/test_upgrade_check.py` — new: `run_check` with a fetcher that records every URL and a fake clock.
- `scripts/bevy_upgrade/test_upgrade_record.py` — `target_fit`, loading a first-phase record, a wrong-typed added field.
- `scripts/bevy_upgrade/test_upgrade_report.py` — the new states, cells and second line.
- `scripts/bevy_upgrade/test_cli.py` — `check`, `start` with fixtures, one save per verb, `check` with a failed pins read.
- `commands/showrunner/bevy_upgrade.md` — the `check` verb and the stale rule.

**Seats:** 1 writer + 1 tester — `upgrade_check.py` calls `upstream.py` and writes `upgrade_record.py` fields, so one writer holds the modules.
- `impl` — `upstream.py`, `upgrade_check.py`, `upgrade_record.py`, `upgrade_report.py`, `bevy_upgrade.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — `test_upstream.py`, `test_upgrade_check.py`, and the additions to `test_upgrade_record.py`, `test_upgrade_report.py` and `test_cli.py`, from the Spec. The fetcher in every module test is a function over a dict of URL to body that records each URL asked for; an unknown URL raises `FetchFailure`. The clock in `test_upgrade_check.py` is a fake that advances by a set step on each call. `test_cli.py` writes a fixtures file and sets `BEVY_UPGRADE_FIXTURES` in `setUp`, so every `start` and `check` reads from it; its subprocess still rejects real network access, and that guard's message becomes `a test attempted network access`. Existing assertions on the first phase's second line, crates.io cell and path-pinned row change to this phase's text.

**Constraints from prior phases:** the first phase built `read_pins(checkout) -> Pins` with `bevy_names`; `Record` and `Entry` with `load`, `save`, `locked`, `start`, `apply_pins` and `on_target` in `upgrade_record.py`; `upgrade_report.state` and `upgrade_report.table`; and the CLI's `--production` resolution and refusal form. The record file is `<state dir>/<slug>.json`. Tests set `BEVY_UPGRADE_STATE_DIR`.
- `bevy_upgrade.py` does all its work inside one `with upgrade_record.locked(slug):` block and calls `upgrade_record.save` once, at the block's end (`if not pins_error or verb != "status"`); a refused verb raises before it. `test_cli.py` counts saves by running the CLI in a subprocess that wraps `upgrade_record.save` (`run_cli(..., count_saves=True)`).
- `upgrade_record.locked` turns only a failure to create, open or lock the lock file into `RecordFailure`; an error raised inside the block passes through unchanged.
- `upgrade_record._validate` checks every field of a loaded record, and `load` raises `RecordFailure` for a wrong type. `apply_pins` keeps every field of a known entry that it does not set itself.
- `bevy_upgrade.py` imports `add_unit` first, which puts other `scripts/` directories ahead of this one on `sys.path`. A module here needs a name no other `scripts/*/` directory uses; `upstream` and `upgrade_check` are free (checked 2026-10-12).
- The first phase prints a path-pinned dependent as `not checked` with `run check`; this phase replaces that row's state and Next. `pyrightconfig.json` already has the `scripts/bevy_upgrade` entry.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. `test_upgrade_check.py` asserts the URLs requested: none for a `done` entry, a patched Bevy crate, a path pin, or an entry with `ready`; no `dependencies` URL for a version already in `releases`; one `bevy` versions URL while the target is not `released` and none after. It asserts that a failing entry keeps its facts while the others update; that a failed target lookup keeps `target_bevy`, sets `target_error`, and the entries still run; and that with the clock past the budget no further URL is asked, the unreached entries and `record["checked"]` are unchanged, and the next pass starts with those entries. `test_upstream.py` covers a yanked version, a requirement taken from `bevy_app` when `bevy` is absent, a `bevy_egui` dependency ignored, a repository URL ending `.git`, the three results of `target_release`, and `fixture_fetch` failing on an unknown URL. `test_upgrade_report.py` covers the second line for each target status and for a failed target lookup, and a path-pinned dependent's row. `test_cli.py` covers `start` and `check` saving once, `check` with a failed pins read (no request, exit 1), and `check` exiting 1 when every entry that asked failed. Run once by hand against the real network and paste the table in the handoff: `BEVY_UPGRADE_STATE_DIR=<scratch> python3 scripts/bevy_upgrade/bevy_upgrade.py --production /home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md start 0.20`.

### Phase 3 — For a dependency with no release on the target, `check` records its GitHub branch and port pull requests  · status: todo

#### Work Order

**Goal:** A dependency whose crates.io releases are all on the old Bevy shows whether its default branch is already on the target, at which commit, and which pull requests and issues track the port, with the user's answer deciding the next step.

**Spec:**
Work only in the worktree and branch the Delegation Context names. GitHub is read without a token, through `Fetch`: the `gh` token sits behind gpg-agent, which is cold after every reboot, and a check that fails then would leave the record stale. The unauthenticated limit is 60 API requests an hour, so the skip rules below are part of the behavior.

`upstream.py` grows:

```python
class BranchHead(NamedTuple):
    rev: str
    committed: str    # YYYY-MM-DD
class Item(TypedDict):
    number: int
    kind: Literal["pr", "issue"]
    state: Literal["open", "closed", "merged"]
    title: str
    updated: str      # the API's updated_at
    head: str         # a PR's "<branch> @ <first 7 of rev>"; "" for an issue
def default_branch(fetch: Fetch, repository: str) -> str: ...
def branch_head(fetch: Fetch, repository: str, branch: str) -> BranchHead: ...
def manifest_requirement(fetch: Fetch, repository: str, rev: str, bevy_names: frozenset[str]) -> str: ...
def port_items(fetch: Fetch, repository: str, target: str, since: str) -> list[Item]: ...
def pull_state(fetch: Fetch, repository: str, number: int) -> tuple[Literal["open", "closed", "merged"], str]: ...
```

- `default_branch` reads `https://api.github.com/repos/<repository>` (`default_branch`). `branch_head` reads `https://api.github.com/repos/<repository>/commits/<branch>` (`sha`, `commit.committer.date`).
- `manifest_requirement` reads `https://raw.githubusercontent.com/<repository>/<rev>/Cargo.toml` and parses it with `tomllib`. It looks at `[dependencies]` and `[workspace.dependencies]` keys that are in `bevy_names`. A string value is the requirement; a table gives its `version`; a table with `workspace = true` takes the requirement from `[workspace.dependencies]`; a table with only `path` or `git` is skipped. It returns the requirement of `bevy` when found, else the first by sorted key, else `""`.
- `port_items` reads `https://api.github.com/repos/<repository>/issues?state=all&since=<since>T00:00:00Z&sort=updated&direction=desc&per_page=100` and keeps the items whose title contains `<target>` as a substring and whose `created_at` date is not before `since`. An item with a `pull_request` key is a `pr`. The filter runs here and not in GitHub's search, because the search does not match `0.20` inside `v0.20.0` (checked 2026-10-12 against Multirious/bevy_tween pull request 97, "Migrate to Bevy v0.20.0").
- `pull_state` reads `https://api.github.com/repos/<repository>/pulls/<number>` and returns the state (`merged` when `merged` is true) and the head as `<head.ref> @ <first 7 of head.sha>`.

`upgrade_record.py` grows, with the type rule of the crates.io phase:

```python
class BranchUnread(TypedDict):   # the repository's default branch has not been read for this crate
    status: Literal["unread"]
class BranchRead(TypedDict):
    status: Literal["read"]
    name: str                    # the repository's default branch
    rev: str                     # its head commit
    committed: str               # YYYY-MM-DD of that commit
    requirement: str             # the Bevy requirement in that commit's manifest; "" when it names none
class Entry(TypedDict):          # added fields
    branch: BranchUnread | BranchRead
    items: list[Item]
```

`load` and `apply_pins` give a missing `branch` `{"status": "unread"}` and missing `items` `[]`; a field that is present is validated.

`upgrade_check.py`: after the crates.io step, an entry reads GitHub when its `crates_io` is read, its `ready` is `""` and its `repository` is not `""`.

- The crates.io phase's skip rule comes first and covers GitHub too: an entry that is `done`, a patched Bevy crate or path-pinned makes no request of any kind, whatever an earlier check stored for it. An entry with `ready`, or with no `repository`, makes no GitHub request. In each case its `branch` and `items` are left as they are.
- `default_branch` once: skipped when `branch` is read, whose `name` is kept.
- `branch_head` every check. `manifest_requirement` only when the head's `rev` differs from the stored `rev`.
- `port_items` every check, with the target's `since` when `target_bevy` is `pre-release` or `released`, else the record's `started` date. `pull_state` only for a `pr` whose `updated` differs from the stored item's, or that is new; an unchanged `pr` keeps its stored `state` and `head`.
- A git pin's own requirement needs no request: `Entry.bevy` already comes from the lockfile.
- The failure rule and the budget of the crates.io step cover these requests too: one `FetchFailure` leaves the whole entry as it was, this pass's crates.io facts included, and sets `error`.

`upgrade_report.py`: `state` is now, in order: `done`; `re-vendor`; `release ready`; `git ready` when `branch` is read and `target_fit(<its requirement>, target)` is `on target`; `port in PR` when any item is a `pr` in state `open`; `not checked` when `crates_io` is `unread`; else `no port yet`.

- Git cell: `<name> @ <first 7 of rev> on Bevy <requirement>` from `branch` (a requirement of `""` prints as `requirement unknown` in place of `on Bevy <requirement>`), then each item as `; PR #<n> <state> (<head>)` or `; issue #<n> <state>`, newest number first. It is `not on GitHub` when `crates_io` is read and its `repository` is `""`, and `—` otherwise while `branch` is `unread`.
- Next for `git ready`: with an `approved` answer, `pin git rev <first 7 of rev> (approved <at>)`; with a `declined` answer, `wait for a release (declined <at>)`; with no answer, `ask the user: git pin?`. Next for `port in PR`: `wait for PR #<n>`.

The command file gains: when a row reads `ask the user: git pin?`, ask the user once, then record the answer with `approve` or `decline`; a pin to an unmerged pull request's branch needs the same answer.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — the GitHub lookups.
- `scripts/bevy_upgrade/upgrade_check.py` — the GitHub step and its skip rules.
- `scripts/bevy_upgrade/upgrade_record.py` — the branch and item fields, their starting values and validation.
- `scripts/bevy_upgrade/upgrade_report.py` — `git ready`, `port in PR`, the Git cell and the Next cell.
- `scripts/bevy_upgrade/test_upstream.py` — the GitHub lookups against canned bodies.
- `scripts/bevy_upgrade/test_upgrade_check.py` — the GitHub step's requests and failures.
- `scripts/bevy_upgrade/test_upgrade_record.py` — the branch and item fields' starting values and validation.
- `scripts/bevy_upgrade/test_upgrade_report.py` — the new states and cells.
- `commands/showrunner/bevy_upgrade.md` — the rule for asking the user once.

**Seats:** 1 writer + 1 tester — the same four modules as the crates.io phase, each extended.
- `impl` — `upstream.py`, `upgrade_check.py`, `upgrade_record.py`, `upgrade_report.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — the additions to `test_upstream.py`, `test_upgrade_check.py`, `test_upgrade_record.py` and `test_upgrade_report.py`, from the Spec, with the recording fetcher and the fake clock the crates.io phase's tests use.

**Constraints from prior phases:** the crates.io phase built `Fetch`, `FetchFailure`, `http_fetch` and `fixture_fetch`; `crate_info`, whose `repository` is `owner/name` or `""`; `run_check(record, pins, fetch, clock) -> CheckPass` with its per-entry failure rule, its 180 s budget read before every request, and its entry order; `target_fit(requirement, target) -> TargetFit` (`on target`, `off target`, `unreadable`); `Entry.crates_io` (`CratesIoUnread | CratesIoRead`, the latter holding `checked`, `releases`, `newest`, `ready`, `repository`) and `Entry.error`; and `Record.target_bevy`, tagged by `status`, with `since` on `pre-release` and `released`.
- `upgrade_report.state` returns `re-vendor` for a path pin and `no port yet` for a read entry with no `ready`; this phase refines the second.
- `load` fills a missing added field with its starting value and validates a present one. `apply_pins` keeps every field of a known entry that it does not set itself, so an entry that became `done` still holds what an earlier check stored.
- The CLI saves once, at the end of its locked block; this phase leaves `bevy_upgrade.py` alone.
- A module here needs a name no other `scripts/*/` directory uses, because `add_unit` puts those directories first on `sys.path`.
- Tests use a fetcher over a dict of URL to body that records every URL asked for. CLI tests set `BEVY_UPGRADE_FIXTURES` to a JSON file of URL to body.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. `test_upgrade_check.py` asserts: no GitHub URL for an entry with `ready` or with no `repository`; no URL of any kind for an entry that became `done`, patched or path-pinned while it stores a repository; no repository URL once `branch` is read; no manifest URL while the head `rev` is unchanged; no pull URL for a `pr` whose `updated` is unchanged; a failing GitHub request leaves the entry's crates.io facts as they were. `test_upstream.py` covers a manifest that inherits `bevy` from `[workspace.dependencies]`, a title holding `v0.20.0`, an item created before `since`, and a merged pull request. `test_upgrade_record.py` covers a record without the branch fields. `test_upgrade_report.py` covers the three Next cells of `git ready` and both empty forms of the Git cell. Run once by hand against the real network and paste the table in the handoff, with the command of the crates.io phase's gate.

### Phase 4 — The check runs every four hours and tells the showrunner only what changed  · status: todo

#### Work Order

**Goal:** Starting a watch schedules its check; a scheduled check is silent unless a dependency's state changed, and the watch closes itself when every dependency is on the target.

**Spec:**
Work only in the worktree and branch the Delegation Context names.

`scripts/bevy_upgrade/upgrade_schedule.py`:

```python
def instance(slug: str) -> str: ...                           # "bevy-upgrade-<slug>"
def ensure_job(record: Record) -> None: ...
def remove_job(slug: str) -> None: ...
def next_check(slug: str) -> datetime | None: ...             # from `notifier.sh status <instance>`; None when the notifier has no such job
def tell_showrunner(slug: str, summary: str, text: str) -> bool: ...   # True when the message was handed to the showrunner
```

- The notifier is `BEVY_UPGRADE_NOTIFIER` when set, else `scripts/message/notifier.sh` beside this repo's `scripts/`; it runs as `zsh <notifier> …`. `send.py` is `BEVY_UPGRADE_SEND` when set, else `scripts/message/send.py`.
- `ensure_job` runs `new <instance> --every 240 --timeout 300 --run "<py> <bevy_upgrade.py> --production <doc> check --scheduled"`, each word of the run command passed through `shlex.quote`, `<py>` being `scripts/lib/py`. `new` keeps an existing instance's clock, so calling it again is safe.
- `tell_showrunner` sends to `showrunners.current_address(slug)` with `--from bevy-upgrade --key bevy-upgrade-<slug> --summary <summary> --text <text>`. It returns `False`, sending nothing, when the address is empty or `send.py` exits non-zero.

`upgrade_record.py` grows:

```python
class Announced(TypedDict):      # what the showrunner was last told about one crate
    state: str                   # the state word it was told
    items: dict[str, str]        # item number → the item state it was told
class Record(TypedDict):         # added field
    announced: dict[str, Announced] | None   # None until a table or a message has reached the showrunner
```

`announced` is the one place a state word is stored, the exception the Delegation Context names. It records what the showrunner was told, never what is true now; every state shown is still derived when printed. `load` gives a record without the field `None`; a present field is validated.

`upgrade_report.py`:

```python
def announcement(entry: Entry, target: str) -> Announced: ...
def changes(record: Record) -> list[str]: ...
def line(record: Record, zone: ZoneInfo, now: datetime, next_due: datetime | None, pins_error: str) -> str: ...
```

- `changes` compares each entry's `announcement` with `record["announced"]` and returns one line per difference: `- <crate>: <old state> → <new state>` when the state word changed, with ` (<ready> on Bevy <requirement>)` after `release ready` and ` (<branch name> @ <rev 7>)` after `git ready`; `- <crate>: PR #<n> <old> → <new>` or `- <crate>: new PR #<n> <state>: <title>` for an item; `- <crate>: new dependency` and `- <crate>: no longer a dependency` for entries that came or went. `announced` of `None` gives no lines.
- `line` is one sentence for a report: `Bevy <target> upgrade: <d> of <n> done`, then each other state with a non-zero count in `STATE_ORDER` as `, <count> <state>`, with ` (<k> waiting for your answer)` after `git ready` when k entries there have no answer, then `; checked <HH:MM zone>` and `, next check <HH:MM zone>` when `next_due` is not `None`. When the table would say stale, it ends `; upstream last checked <YYYY-MM-DD HH:MM zone> (stale)` instead, and while `checked` is `None`, `; no complete upstream check yet`. When `pins_error` is not `""` the sentence ends with `; pins not re-read since <YYYY-MM-DD HH:MM zone>`, from `pins_read`.

`bevy_upgrade.py`:

- The baseline. Every verb that prints the table (`start`, `status`, `check` by hand, `approve`, `decline`, `forget`, `note`, `stop`) prints `Changed since the last report:` and the `changes` lines above the table when there are any, and stores every entry's `announcement` in `announced` before the one save. So `start` leaves a baseline, and from this phase `status` always saves. A closed record prints no change lines.
- `line` prints only the sentence. It takes no lock and saves nothing: `save` replaces the file whole, so a reader never sees half a record. It loads, re-reads pins, applies them in memory and prints. It never touches `announced`. With a failed pins read it prints the sentence with its pins clause and nothing else on stdout.
- `check --scheduled` prints nothing on stdout. It re-reads pins, runs the check and saves once. With a failed pins read it makes no lookup, sends nothing, saves nothing and never closes; it prints one `bevy_upgrade: pins could not be re-read: <reason>` line on stderr and exits 1. With `announced` of `None` it stores the baseline and sends nothing. With `changes` it sends `tell_showrunner(slug, "Bevy <target> upgrade: <N> changes", <the change lines, then the line sentence, then "Full table: /showrunner:bevy_upgrade">)` and stores `announced` only when the send returned `True`, so a change found while the showrunner is away is sent by a later check.
- Closing. When every entry is `done` after a pins re-read that succeeded in the same run, `check` closes the watch. By hand it sets `closed`, removes the job and prints `Bevy <target> upgrade: every dependency is on Bevy <target>. The watch is closed.` above the table. Scheduled, it sends the change lines, if any, then that sentence, with the summary `Bevy <target> upgrade: done`; only when that send returned `True` does it store `announced`, set `closed` and remove the job. Otherwise the record stays open and the job stays, so the next scheduled check sends it. `remove_job` runs after the save.
- `stop` sets `closed`, removes the job and prints the table. On a closed record `status` prints the table under `This watch closed <YYYY-MM-DD>.`, `check` by hand refuses with `bevy_upgrade: the watch is closed; start a new one`, and `check --scheduled` removes the job and exits 0 with no message, so a job that outlived its watch removes itself.
- `line` prints nothing and exits 0 when the production has no record or the record is closed.
- `start` calls `ensure_job` after its save. `status`, `check` and `line` call it too whenever the record is open, so a job that went missing comes back.

The command file gains `stop`, `line`, and: the scheduled check writes to this session only when something changed; show those lines to the user in the next reply.

**Files:**
- `scripts/bevy_upgrade/upgrade_schedule.py` — new: the notifier job and the message to the showrunner.
- `scripts/bevy_upgrade/upgrade_record.py` — the `announced` field, its starting value and validation.
- `scripts/bevy_upgrade/upgrade_report.py` — `announcement`, `changes` and `line`.
- `scripts/bevy_upgrade/bevy_upgrade.py` — `stop`, `line`, `check --scheduled`, the baseline, the job upkeep and the closing rule.
- `scripts/bevy_upgrade/test_upgrade_schedule.py` — new: the notifier and send commands, through stub scripts.
- `scripts/bevy_upgrade/test_upgrade_record.py` — `announced` in a record without it and with a wrong type.
- `scripts/bevy_upgrade/test_upgrade_report.py` — `announcement`, `changes` and `line`.
- `scripts/bevy_upgrade/test_cli.py` — `stop`, `line`, `check --scheduled`, the baseline and the closing rule.
- `commands/showrunner/bevy_upgrade.md` — `stop`, `line`, and what a scheduled message means.

**Seats:** 1 writer + 1 tester — the CLI ties the schedule, the changes and the closing rule together.
- `impl` — `upgrade_schedule.py`, `upgrade_record.py`, `upgrade_report.py`, `bevy_upgrade.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — `test_upgrade_schedule.py` and the additions to `test_upgrade_record.py`, `test_upgrade_report.py` and `test_cli.py`, from the Spec. The notifier and `send.py` stubs append their arguments to a file the test reads; a stub that exits non-zero stands for a showrunner that is away.

**Constraints from prior phases:** `run_check(record, pins, fetch, clock) -> CheckPass` updates the record and never raises for a failed lookup or a spent budget; its 180 s budget plus one request's 30 s timeout stays inside this phase's 300 s job limit. `upgrade_report.state` returns all seven states and `STATE_ORDER` orders them. `Entry.items` holds `number`, `kind`, `state`, `title`, `updated` and `head`; `Entry.branch` is `BranchUnread | BranchRead` (`name`, `rev`, `committed`, `requirement`); `Entry.crates_io` is `CratesIoUnread | CratesIoRead` (`checked`, `releases`, `newest`, `ready`, `repository`). `Entry.answer` is `None` or holds `answer`, `at`, `words`. The stale rule is the table's: `checked` older than eight hours, any entry with `error`, or `target_error` not `""`.
- Every CLI verb re-reads pins before it prints. When that fails, a verb other than `start` prints `pins could not be re-read: <reason>; showing pins read <HH:MM zone>` above the table, and `check` then makes no lookup and exits 1.
- The CLI does all its work inside one `with upgrade_record.locked(slug):` block and saves once, at the block's end; a refused verb raises before the save. Before this phase a `status` whose pins re-read failed saves nothing, and `test_cli.py` asserts it through `run_cli(..., count_saves=True)`.
- `bevy_upgrade.py` uses `fixture_fetch` when `BEVY_UPGRADE_FIXTURES` names a JSON file of URL to body, and `test_cli.py` sets it in `setUp`.
- The type rule: a value that can be missing in two ways, or values that are present or missing together, is a `TypedDict` union tagged by `status`; one field whose absence has one meaning keeps an empty value or `None` with a comment that says what it means.
- A module here needs a name no other `scripts/*/` directory uses, because `add_unit` puts those directories first on `sys.path`; `upgrade_schedule` is free (checked 2026-10-12).

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. The tests cover: the exact `new` command and its quoting for a doc path with a space; `next_check` read from a stub's `next_due=` line and `None` for exit 1; `start`, then a scheduled check with a change sends once, and a second run sends nothing; a scheduled check with no change sends nothing; a record whose `announced` is `None` gets its baseline from a scheduled check, which sends nothing; with an empty address `announced` stays and the next run sends; `status` clears pending changes; `line` leaves them and leaves the record file byte for byte as it was; all entries `done` closes the watch and removes the job; all entries `done` with a failing send leaves the record open and the job in place, and the next scheduled check sends and closes; a scheduled check with a failed pins read prints nothing on stdout, exits 1 and does not close; `line` with a failed pins read prints one sentence; `stop`; `check` by hand refused on a closed record, and a scheduled one removing the job. `bash -n` is not needed (no shell file changes).

### Phase 5 — The dailies carries the upgrade as a topic read from the record  · status: todo

#### Work Order

**Goal:** Every dailies of a production with an open watch shows one `Bevy upgrade` topic whose text is the record's line, and it needs the user only while a git pin waits for their answer.

**Spec:**
Work only in the worktree and branch the Delegation Context names. This phase edits three files owned by other units (the showrunner allowed it; name them in the checkpoint notice as `also touches`).

`bevy_upgrade.py`: `line --json` prints one JSON object, `{"update": "<the line sentence>", "needs_user": <true when an entry is git ready with no answer>}`, or nothing when there is no open record. Stdout holds that object and nothing else, also when the pins re-read failed: the sentence then carries its pins clause.

`scripts/production/dailies_input.py`, directly after the review watch block (the `report("review watch", …)` call near line 861):

- Run `"<python>" <script> --production <production doc> line --json`, where `<script>` is `DAILIES_BEVY_UPGRADE` when set, else `scripts/bevy_upgrade/bevy_upgrade.py` beside this repo's `scripts/production/`. Timeout 30 s.
- Exit 0 with a JSON object: append `{"title": "Bevy upgrade", "update": <update>, "eta": "no ETA stated yet", "needs_user": <needs_user>}` to `topics_out`, then `report("bevy upgrade", "ok", <update>)`.
- Exit 0 with no output: no topic; `report("bevy upgrade", "ok", "no open watch")`.
- Any other outcome (non-zero exit, timeout, output that is not that object): append the topic with update `the record could not be read: <first stderr line or the reason>` and `needs_user` false, and `report("bevy upgrade", "failed", <the same text>)`. The dailies itself never fails because of this topic.

`commands/showrunner/dailies.md`, in **Gather**, after the Review watch item: one item saying the input builder adds the `Bevy upgrade` topic from the record while a watch is open, and that nothing else in the report states the upgrade's status.

**Files:**
- `scripts/bevy_upgrade/bevy_upgrade.py` — `line --json`.
- `scripts/bevy_upgrade/test_cli.py` — `line --json` with and without an unanswered git pin, with no record, and with a failed pins read.
- `scripts/production/dailies_input.py` — the topic, after the review watch block.
- `scripts/production/test_dailies_input.py` — the topic present, absent and failed, through a stub script.
- `commands/showrunner/dailies.md` — the Gather item.

**Seats:** 2 writers — the split is by directory, and each writer tests its own side.
- `impl` — `scripts/bevy_upgrade/bevy_upgrade.py`, `scripts/bevy_upgrade/test_cli.py`.
- `test` opens as impl — `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/dailies.md`. It codes against the JSON object above and a stub script set through `DAILIES_BEVY_UPGRADE`, the way `watch_stubs` in `test_dailies_input.py` replaces `review_regime.py`.

**Constraints from prior phases:** `bevy_upgrade.py line` prints one sentence for an open record and nothing otherwise. It takes no lock, saves nothing and never changes `announced`, so the dailies never waits behind a running check. `upgrade_report.line(record, zone, now, next_due, pins_error)` builds that sentence; with a failed pins read it ends `; pins not re-read since <YYYY-MM-DD HH:MM zone>`. A `git ready` entry's `answer` is `None` until the user approves or declines. `test_cli.py` sets `BEVY_UPGRADE_FIXTURES` in `setUp`, and its subprocess rejects real network access.

**Acceptance gate:** `python3 -m unittest discover -s scripts/bevy_upgrade -p 'test_*.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'` pass. `basedpyright scripts/bevy_upgrade scripts/production/dailies_input.py scripts/production/test_dailies_input.py` prints `0 errors, 0 warnings, 0 notes`. The CLI tests cover `line --json` output that parses as one JSON object when the pins re-read failed. The dailies tests cover the topic with `needs_user` true and false, no topic for empty output, and the failed form for a non-zero exit, with the report still built. Before the checkpoint notice, trial-merge the owner's tip per the production contract (item 9) and run the dailies tests.

### Phase 6 — The migration plan's dependency check sees crates that depend only on Bevy's sub-crates  · status: todo

#### Work Order

**Goal:** `/bevy_migration_plan`'s dependency check lists and classifies a crate that depends on Bevy only through Bevy's own crates, such as `bevy_app` or `bevy_ecs`, as it does one that depends on `bevy`.

**Spec:**
Work only in the worktree and branch the Delegation Context names. The user approved this phase on 2026-10-12, through the showrunner. The file belongs to no unit of this production; name it in the checkpoint notice as `also touches`.

`scripts/bevy_migration_plan/bevy_migration_dependency_check.py` today reads a release's Bevy requirement only from the dependency whose `crate_id` is `bevy` (`get_bevy_dependency_requirement`). A crate that depends only on `bevy_app` or `bevy_ecs` (moonshine-save does) has no such dependency, so `has_bevy_dependency` drops it from the report, and one with `bevy` in its name gets no compatible version and reads as a blocker.

- `get_bevy_dependency_requirement(crate_name, version, bevy_crates)` takes the set of Bevy's own crate names. It returns the `req` of the dependency named `bevy` when one of kind other than `dev` exists, else the `req` of the first such dependency, by sorted name, whose `crate_id` is in `bevy_crates`, else `None`. A name outside the set is never read as Bevy: `bevy_egui` is a third-party crate. `DependencyItem` gains `kind` when it lacks it.
- The set is `get_bevy_internal_crates(bevy_version)`, read once in `main` and passed down through `get_bevy_dependencies`, `has_bevy_dependency` and `find_bevy_compatible_version`. It always holds `bevy`.
- When `~/rust/bevy-<version>/crates` is missing, the set holds only `bevy` and `bevy_internal`, and a crate that depends only on a sub-crate cannot be seen. The script then prints `Warning: <path> not found; crates that depend only on a Bevy sub-crate are not detected` on stderr once, and the report carries the same sentence under its title, so an incomplete list never reads as a complete one. `get_all_direct_dependencies` keeps its present behavior for a missing checkout.
- Nothing else in the report changes: the classifications, their wording and the `--output` file stay as they are.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the requirement rule, the names passed down, the missing-checkout warning.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — new: the rule and the warning, with `urlopen` replaced.

**Seats:** 1 writer + 1 tester — one script and its first test file.
- `impl` — `scripts/bevy_migration_plan/bevy_migration_dependency_check.py`.
- `test` — `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py`, from the Spec. Each test replaces `urllib.request.urlopen` with canned response bodies and reaches no network; the Bevy checkout is a temporary directory with two `crates/<name>/Cargo.toml` files, found through a patched `Path.home`.

**Constraints from prior phases:** the crates.io phase states the same rule for `upstream.release_requirement` in `scripts/bevy_upgrade/`; the two scripts keep separate code, because this one runs alone from `commands/bevy_migration_plan.md` and prints its own warnings. This script has no test file and no `pyrightconfig.json` entry today; `basedpyright` on it prints `0 errors, 0 warnings, 0 notes` (checked 2026-10-12).

**Acceptance gate:** `python3 -m unittest discover -s scripts/bevy_migration_plan -p 'test_*.py'` passes, and `basedpyright scripts/bevy_migration_plan/bevy_migration_dependency_check.py scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` prints `0 errors, 0 warnings, 0 notes`. The tests cover: a requirement read from `bevy`; one read from `bevy_app` when `bevy` is absent; a crate with only a `bevy_egui` dependency giving `None`; a `dev` dependency on `bevy` giving `None`; `get_bevy_dependencies` keeping a crate without `bevy` in its name that depends only on `bevy_ecs`; `find_bevy_compatible_version` finding a version for such a crate; and the warning on stderr and in the report when the checkout is missing. Run once by hand against the real network with `~/rust/bevy-<version>` in place and paste the dependency list in the handoff: `python3 scripts/bevy_migration_plan/bevy_migration_dependency_check.py --bevy-version <version> --codebase /home/natepiano/rust/hana_catalyst` lists `moonshine-save`.

## Close-out for the showrunner

Production-level steps no phase can run, because they act in another production's session or on its state:

- After the scheduling phase is on main, the hana showrunner runs `/showrunner:bevy_upgrade start 0.20`, then `approve bevy_enhanced_input` with the user's words from **Source**, then `note` for what no lookup finds: bevy_tween pull request 97's CI fails only in its `bevy_tween_lookup_curve` sub-crate, which pins bevy_lookup_curve 0.12, and issue 91 asks for a crates.io release.
- The hana showrunner then removes the hand-written watch: `zsh ~/.claude/scripts/message/notifier.sh remove bevy020-watch` and the directory `~/.local/state/bevy020-watch/`.
- The hand-written watch listed five crates. Hana's lockfile has nine third-party crates on Bevy and one patched Bevy crate; `bevy_egui`, `bevy_time_runner`, `moonshine-save` and `moonshine-util` were not watched.
