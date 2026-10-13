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
- **Layout:** `scripts/bevy_upgrade/` (new) holds every module and its tests; `commands/showrunner/bevy_upgrade.md` (new) is the skill; `pyrightconfig.json` gains one entry. The dailies phase alone edits `scripts/production/dailies_input.py`, its test and `commands/showrunner/dailies.md`. The migration-check phase alone edits `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` and adds its test.
- **Key files:**
  - `scripts/production/showrunners.py` — `production_of(session_id) -> Path | None` (line 217), `current_address(slug) -> str` (line 243; empty while the showrunner is not running).
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (line 196) with fields `doc`, `slug`, `merge_branch`, `checkout`, `log`, `zone`; raises `Refusal`.
  - `scripts/message/notifier.sh` — `new <instance> --every <min> --run <cmd> [--timeout <s>]` (a `--run` job takes no `--to`, `--from`, `--check`, `--hold` or `--aligned`), `remove <instance>`, `status <instance>` (its second line is `next_due=<epoch> (<local time>)`; exit 1 for no such instance). State root: `NOTIFIER_STATE_DIR`, else `~/.local/state/notifier`.
  - `scripts/message/send.py` — `--to <address> --from <sender> --key <key> --summary <text> --text <text>`.
  - `scripts/production/proposals_table.py` and `commands/showrunner/proposals.md` — the pattern for a showrunner table script and its command file.
  - `scripts/production/dailies_input.py` lines 900–910 — how the review watch becomes a dailies topic.
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

### Phase 2 — `check` records each dependency's crates.io releases and which one is on the target  · status: done

#### As-built

`/showrunner:bevy_upgrade check` runs one check over the record: for each dependency that still needs it, it reads crates.io for the crate's releases and each release's Bevy requirement, keeps them in the record, and marks the newest release on the target as `ready`. The same run looks up whether the target Bevy is itself released. `start` runs the same check after `upgrade_record.start` and before its one save. The table carries a second line (target release, lookup failure, time of the last upstream check), a crates.io cell per row, and the states `release ready`, `no port yet` and `not checked`. Every request goes through a `Fetch`, so a fixtures file or a test function stands in for the network.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — the two fetchers, the crates.io lookups (`crate_info`, `release_requirement`, `target_release`) and the types they return.
- `scripts/bevy_upgrade/upgrade_check.py` — `run_check`, `CheckPass`, `Clock`, `CHECK_BUDGET`: one check, its skip rule and its time budget.
- `scripts/bevy_upgrade/upgrade_record.py` — `CratesIoUnread | CratesIoRead`, `TargetUnread`, `TargetBevy`, the added `Entry` and `Record` fields with their starting values and validation, `target_fit`.
- `scripts/bevy_upgrade/upgrade_report.py` — `state`, the crates.io cell (`_crates_io(entry, target)` takes the target), the Next cells, the table's second line.
- `scripts/bevy_upgrade/bevy_upgrade.py` — the `check` verb, `start` running the same check, the fetcher choice, the one save, the exit codes.
- `commands/showrunner/bevy_upgrade.md` — the `check` verb and the stale rule: a report says `stale` when the table does.
- `scripts/bevy_upgrade/test_upstream.py`, `test_upgrade_check.py`, `test_upgrade_record.py`, `test_upgrade_report.py`, `test_cli.py` — the lookups against canned bodies, `run_check` with a URL-recording fetcher and a step clock, the added record fields, the table, the CLI with fixtures.

**Binds later work:**
- **Fetching.** `Fetch = Callable[[str], bytes]` GETs one URL and raises only `FetchFailure`, whose `str()` is `<host>: <reason>` (`crates.io: HTTP 503`). `http_fetch(url: str) -> bytes` (urllib, User-Agent `bevy-upgrade (natepiano)`, timeout 30 s) turns `HTTPError`, `http.client.HTTPException` (a truncated or malformed response), `OSError` and `ValueError` into `FetchFailure`; nothing else leaves it. `fixture_fetch(path: Path) -> Fetch` serves each URL from a JSON file of URL to body; a URL the file lacks raises `FetchFailure("<host>: no fixture")`. The CLI uses `fixture_fetch(Path(<value>))` when `BEVY_UPGRADE_FIXTURES` is set, else `http_fetch`. Any new upstream lookup takes a `Fetch` as its first argument and has this one failure type for every network or HTTP fault.
- `crate_info(fetch: Fetch, crate: str) -> CrateInfo` reads `https://crates.io/api/v1/crates/<crate>`. `CrateInfo.versions: tuple[str, ...]` holds the versions not yanked, in the API's order (newest first). `CrateInfo.repository: str` is `owner/name` for a github.com URL, with any `.git` suffix and trailing path removed, and `""` for any other URL or none.
- `release_requirement(fetch: Fetch, crate: str, version: str, bevy_names: frozenset[str]) -> str` reads `https://crates.io/api/v1/crates/<crate>/<version>/dependencies`. It returns the `req` of a non-dev dependency named `bevy` whether or not `bevy_names` holds it (a lockfile with only Bevy sub-crates has no `bevy` package); else the `req` of the first non-dev dependency, by sorted name, whose `crate_id` is in `bevy_names`; else `""`. A name outside `bevy_names` is never read as Bevy: `bevy_egui` is a third-party crate.
- `target_release(fetch: Fetch, target: str) -> TargetUnpublished | TargetPrerelease | TargetReleased` reads `https://crates.io/api/v1/crates/bevy/versions` and looks at each `num` that starts with `<target>.`, with its `created_at`: no such version is `unpublished`, only pre-releases is `pre-release`, otherwise `released`.
- `target_fit(requirement: str, target: str) -> TargetFit` in `upgrade_record.py`, with `TargetFit = Literal["on target", "off target", "unreadable"]`. It strips one leading `^`, `~` or `=`; a remainder matching `\d+\.\d+(\.\d+)?(-[\w.]+)?` is compared by major.minor; anything else (a range, `*`, an empty string) is `unreadable`.
- **Entry fields.** `Entry.crates_io: CratesIoUnread | CratesIoRead`, tagged by `status` (`unread`, `read`) as `Pin` is tagged by `source`; no field is `bool | None`. `CratesIoRead` has `checked: str` (ISO-8601 UTC of the last complete check of this crate), `releases: dict[str, str]` (version → its Bevy requirement; a release never changes, so each is read once), `newest: str` (the newest version not yanked), `ready: str` (the newest release whose requirement is on the target; `""` while none is) and `repository: str` (as `CrateInfo.repository`). `Entry.error: str` is the last check's failure for this crate, or `""`. A report of what changed between checks and the production's dailies read these fields.
- **Record fields.** `Record.target_bevy: TargetBevy` (`TargetUnread | TargetUnpublished | TargetPrerelease | TargetReleased`) is tagged by `status`: `unread`; `unpublished`; `pre-release` with `since`; `released` with `version`, `date` and `since`. `since` is the YYYY-MM-DD of the first version on the target, pre-releases included. `Record.target_error: str` is the last failure of the target lookup, or `""`. `Record.checked: str | None` is the ISO-8601 UTC time of the last check that reached every entry; `None` before the first.
- `load` gives a record written before these fields existed their starting values (`crates_io` and `target_bevy` `{"status": "unread"}`, `error` and `target_error` `""`, `checked` `None`); a present field of the wrong type is a `RecordFailure`. `apply_pins` gives a new entry the starting values.
- `run_check(record: Record, pins: Pins, fetch: Fetch, clock: Clock) -> CheckPass` in `upgrade_check.py`, with `Clock = Callable[[], datetime]` (the current UTC time) and `CheckPass(record: Record, asked: int, failed: int)`: `asked` counts the entries that made at least one request, `failed` those of them whose lookup failed. It never raises for a failed lookup or a spent budget, so the caller always saves what the check learned.
  - Budget: `CHECK_BUDGET = timedelta(seconds=180)`. The run's `now` is `clock()` at its start, and the clock is read before every request. Once the budget is spent, no further request is made, the entry being read stays exactly as it was (its `error` too), and `run_check` returns. With one request's 30 s timeout, a run ends inside a scheduled job's 300 s limit.
  - Target first: while `target_bevy["status"]` is not `released`, `target_release` runs once per check; its result is stored and `target_error` set to `""`. A `FetchFailure` there leaves `target_bevy` as it was and sets `target_error` to the failure's text; the entries still run. Once the status is `released`, the lookup never runs again.
  - Entry order: entries never read first, then the oldest `crates_io["checked"]` first, then by name, so the next check continues one the budget cut short. Skip rule: no request at all for an entry that is `done`, is a patched Bevy crate, has a path pin, or already has `ready`.
  - Any other entry: `crate_info`, then a walk from the newest version down to and including the pinned one (all of the newest six when the pin is not among them), calling `release_requirement` only for versions missing from `releases`. It stores `CratesIoRead` with `newest`, `repository`, the grown `releases`, `ready` (the newest walked version whose `target_fit` is `on target`, else `""`) and `checked` set to the run's `now`, and sets `error` to `""`.
  - Per-entry failure rule: a `FetchFailure` sets that entry's `error` to the failure's text and leaves every other field of the entry as it was, `crates_io` included; the other entries still run. A crate whose every version is yanked fails the same way, with `crates.io: no version that is not yanked`. `record["checked"]` is set to the run's `now` when the check reached every entry, whether or not entries failed; a check the budget cut short leaves it as it was.
- **CLI.** `check` works inside the one `upgrade_record.locked` block: the pins re-read, `run_check` with the chosen fetcher and `lambda: datetime.now(timezone.utc)`, the one save, the table. There is a single save per run, at the end of the block; a refused verb saves nothing. When the pins re-read fails, `check` makes no lookup (the Bevy crate names come from the lockfile), prints the `pins could not be re-read` line and the table from the stored record, saves nothing and exits 1. `check` exits 1 when `asked` is above zero and equals `failed`, else 0; the target lookup does not count. `start` exits 0 once the record is written, whatever its check found. A scheduled job that runs `check` reads this exit code and is what keeps `Record.checked` inside eight hours.
- **States and Next.** `upgrade_report.state`, in order: `done`; `re-vendor` when `patched_bevy` or the pin is a path pin; `release ready` when `crates_io` is read and `ready` is not `""`; `not checked` when `crates_io` is `unread`; else `no port yet`. Next cells: `bump to <ready>` for `release ready`; `wait` for `no port yet`; `run check` for `not checked`; `re-vendor from Bevy <target>` for a patched Bevy crate; `port the copy at <path> to Bevy <target>` for a path-pinned dependent, which no check can move.
- **crates.io cell.** `<newest> on Bevy <requirement>`, with ` → <ready> on Bevy <its requirement>` appended when `ready` is set and differs from `newest`; a requirement of `""` prints `requirement unknown` in place of `on Bevy <requirement>`. The cell is `—` for a patched Bevy crate, a path pin, or a `done` entry whose `crates_io` is `unread`, and `not checked` for any other `unread` entry; a `done` entry read before it reached the target keeps its release text. An entry with `error` appends `; last check failed: <error>`. Any new cell that depends on a lookup follows the same rule: `—` for a row no check will ever read (done, patched Bevy crate, path pin), never a `not checked` that cannot change.
- **Second line of `table`**, three parts. The target: `Bevy <target>: released as <version> on <date>.` for `released`, `Bevy <target>: not released yet.` for `pre-release` and `unpublished`, `Bevy <target>: release not looked up yet.` for `unread`. Then ` Release lookup failed: <target_error>.` when `target_error` is not `""`. Then the check: ` Upstream: no complete check yet.` while `checked` is `None`; else ` Upstream checked <HH:MM zone>.`; or ` Upstream last checked <YYYY-MM-DD HH:MM zone> (stale).` when `checked` is older than eight hours, any entry has `error`, or `target_error` is not `""`.

**Gotchas:**
- `http.client.IncompleteRead` and `BadStatusLine` are `http.client.HTTPException`, which is neither `OSError` nor `ValueError`; a fetcher that catches only those two lets a truncated or malformed response escape as a traceback.
- No check ever reads a `done` entry, so a "not read yet" text on a done row would never change; that is why such a row prints `—`.
- `newest` is crates.io's first version not yanked, which is publication order. A yanked version of the target Bevy still reads as `released`.
- There is no pause between crates.io requests; about 40 in a row were all answered.
- A spent budget (`CheckBudgetSpent`, internal to `upgrade_check.py`) never leaves `run_check`; its only outward sign is that `Record.checked` does not advance.
- Tests need no network and, for `run_check`, no file: the module tests use a fetcher over a dict of URL to body that records each URL asked for and raises `FetchFailure` for an unknown one, and `test_upgrade_check.py` uses a clock that advances by a set step on each call. `test_cli.py` writes a fixtures file and sets `BEVY_UPGRADE_FIXTURES` in `setUp`; its subprocess rejects real network access with `a test attempted network access`.
- basedpyright exits 3 in this repo (no `.venv`); its result is the `0 errors, 0 warnings, 0 notes` line.

**Ruled out:** a pause between crates.io requests — not needed at this volume.

### Phase 3 — For a dependency with no release on the target, `check` records its GitHub branch and port pull requests  · status: done

#### As-built

After the crates.io step, `check` reads GitHub through `Fetch`, with no token, for an entry whose `crates_io` is read with `ready` `""`, or is unlisted, and whose `repository` is not `""`.

```python
class NotFound(FetchFailure): ...
class BranchHead(NamedTuple): rev: str; committed: str  # committed is YYYY-MM-DD
class Item(TypedDict): number: int; kind: Literal["pr", "issue"]; state: Literal["open", "closed", "merged"]; title: str; updated: str; head: str
def github_repository(url: str) -> str: ...
def default_branch(fetch: Fetch, repository: str) -> str: ...
def branch_head(fetch: Fetch, repository: str, branch: str) -> BranchHead: ...
def manifest_requirement(fetch: Fetch, repository: str, rev: str, bevy_names: frozenset[str]) -> str: ...
def port_items(fetch: Fetch, repository: str, target: str, since: str) -> list[Item]: ...
def pull_state(fetch: Fetch, repository: str, number: int) -> tuple[Literal["open", "closed", "merged"], str]: ...
class CratesIoUnlisted(TypedDict): status: Literal["unlisted"]; checked: str; repository: str
class BranchUnread(TypedDict): status: Literal["unread"]
class BranchRead(TypedDict): status: Literal["read"]; repository: str; name: str; rev: str; committed: str; requirement: str
class Entry(TypedDict): crates_io: CratesIoUnread | CratesIoRead | CratesIoUnlisted; branch: BranchUnread | BranchRead; items: list[Item]  # added and widened fields only
```

- **Lookups.** `http_fetch` raises `NotFound` for HTTP 404 and `fixture_fetch` for a fixture value of JSON `null`. Every lookup raises only `FetchFailure`, a manifest that is not valid TOML included, because `run_check` catches nothing else. `manifest_requirement` reads the root `Cargo.toml` at `rev`: the `[dependencies]` and `[workspace.dependencies]` keys that are `bevy` or in `bevy_names`, a `workspace = true` table resolved through `[workspace.dependencies]`; it returns the requirement of `bevy`, else the first by sorted key, else `""`, and `""` when the manifest answers `NotFound`. `port_items` keeps the issues and pull requests, open or closed, updated since `since` whose title contains `<target>` as a substring and whose `created_at` date is not before `since`; `updated` is the API's `updated_at`. It returns `head` `""` and never `merged`: `_github_facts` gives each `pr` its `state` and `head` (`<head.ref> @ <first 7 of head.sha>`) from `pull_state` or from the stored item.
- **Check.** `run_check` stages `_crate_facts` and `_github_facts(fetch, entry, record, bevy_names)` on a copy of the entry and stores both or neither: one `FetchFailure`, a rate-limit answer included, leaves the entry as stored, sets `error` and counts in `failed`. When `crate_info` raises `NotFound`, `crates_io` becomes `unlisted` with this check's `checked` and, as `repository`, `github_repository(<pin url>)` for a git pin and `""` for any other pin; `error` is cleared, the entry does not count in `failed`, and it asks crates.io once on every check. An entry that is `done`, a patched Bevy crate, path-pinned or holding a `ready` release makes no request of any kind and keeps its stored `branch` and `items`.
- **States and Next.** `upgrade_report.state` returns, in order: `done`; `re-vendor`; `release ready`; `git ready` when `branch` is read and `target_fit(<its requirement>, target)` is `on target`; `port in PR` when any item is a `pr` in state `open`; `not checked` when `crates_io` is `unread`; else `no port yet`. `STATE_ORDER` sorts rows `git ready`, `release ready`, `re-vendor`, `port in PR`, `no port yet`, `not checked`, `done`. Next for `git ready` is `ask the user: git pin?` with no answer, `pin git rev <first 7 of rev> (approved <at>)` with an `approved` one and `wait for a release (declined <at>)` with a `declined` one; for `port in PR` it is `wait for PR #<n>` with the largest open number.
- **Cells.** The Git column sits between crates.io and State: `<name> @ <first 7 of rev> on Bevy <requirement>` from a read `branch`, with `requirement unknown` in place of `on Bevy <requirement>` for a requirement of `""`; `not on GitHub` when `crates_io` is read or unlisted with `repository` `""`; `—` otherwise. Each stored item follows as `; PR #<n> <state> (<head>)` or `; issue #<n> <state>`, largest number first. An unlisted entry's crates.io cell is `not on crates.io`; it carries no error, so it does not make the second line read `(stale)`.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — `NotFound`, `BranchHead`, `Item` and the six functions above.
- `scripts/bevy_upgrade/upgrade_check.py` — `_github_facts`, and the `unlisted` case in `_crate_facts`.
- `scripts/bevy_upgrade/upgrade_record.py` — `CratesIoUnlisted`, `BranchUnread`, `BranchRead` and the `Entry` fields; `load` gives a missing `branch` `{"status": "unread"}` and missing `items` `[]` and validates present ones, and `apply_pins` starts a new entry with the same values.
- `scripts/bevy_upgrade/upgrade_report.py` — `state`, `STATE_ORDER`, the Git column and the Next cells.
- `scripts/bevy_upgrade/test_upstream.py`, `test_upgrade_check.py`, `test_upgrade_record.py`, `test_upgrade_report.py`, `test_cli.py` — their tests.
- `commands/showrunner/bevy_upgrade.md` — on a row reading `ask the user: git pin?`, the showrunner asks once and records the answer with `approve` or `decline`; a pin to an unmerged pull request's branch needs the same answer.

**Binds later work:**
- `Entry.crates_io` has three cases, `unread`, `read` and `unlisted`; only `read` has `ready`. Every fixture with a read branch needs `repository`, `name`, `rev`, `committed` and `requirement`, and a CLI test that runs `check` on an entry with no `ready` release needs GitHub answers in its fixture file: `write_fixtures` in `test_cli.py` holds them for `example/adapter`.
- Requests for one repository: `default_branch` only when no `branch` is stored for it; `branch_head` and `port_items` on every check; `manifest_requirement` only when the head `rev` differs from the stored one, so a stored requirement, `""` included, is reused; `pull_state` only for a `pr` that is new or whose `updated` changed. `port_items` gets the `since` of `target_bevy` when its status is `pre-release` or `released`, else the date of the record's `started`.
- Stored `branch` and `items` belong to `BranchRead.repository`: under another repository the check reads as if nothing were stored and the stored items are gone, so an item number names an item only with its repository. Within one repository a stored item that the issues answer lacks is kept as stored, with no request made for it.
- The requirement comes from the root manifest only. A workspace whose Bevy requirement sits in a member crate's manifest (bevy-inspector-egui) reads `requirement unknown` and never `git ready`; once its port pull request merges, its row reads `no port yet` for a branch that is on the target.
- GitHub allows 60 API requests an hour without a token, and `port_items` reads one page of 100 per check, newest update first. A repository's first read makes 4 requests plus one per pull request, a later check 2; one check of hana's ten dependencies makes 16 GitHub requests on a first read and 8 afterwards.
- `ask the user: git pin?` is the one Next cell that asks the user for an answer.

**Gotchas:** `NotFound` is a `FetchFailure` and is an answer in three places only: `crate_info` (the entry becomes `unlisted`), the manifest (`""`), and `branch_head` under a stored name (`default_branch` is read again and `branch_head` asked once more; a stored `rev` equal to the new head still keeps its `requirement`). From any other request, a release's dependencies included, it fails the entry.

**Ruled out:** GitHub's search for the title filter: it does not match `0.20` inside `v0.20.0` (Multirious/bevy_tween pull request 97, "Migrate to Bevy v0.20.0", checked 2026-10-12). The `gh` token: it sits behind gpg-agent, cold after every reboot, and a check failing then would leave the record stale. Filling `branch` and `items` in `apply_pins` for a stored entry: `load` fills them, and every record reaches `apply_pins` through `load`.

### Phase 4 — The check runs every four hours and tells the showrunner only what changed  · status: done

#### As-built

A watch keeps one notifier job, `bevy-upgrade-<slug>`, that runs `check --scheduled` every 240 minutes. A scheduled check tells the showrunner only what differs from what it was last told, and a check that finds every dependency on the target closes the watch.

```python
# upgrade_schedule.py
def instance(slug: str) -> str: ...                    # "bevy-upgrade-<slug>"
def ensure_job(record: Record) -> None: ...
def remove_job(slug: str) -> None: ...
def next_check(slug: str) -> datetime | None: ...      # None when the notifier has no such job
def tell_showrunner(slug: str, summary: str, text: str) -> bool: ...   # True only for exit 0 and a `SENT:` line
def retire_message(slug: str) -> None: ...
# upgrade_record.py
class Announced(TypedDict): state: str; repository: str; items: dict[str, str]   # what the showrunner was last told about one crate
class Record(TypedDict): announced: dict[str, Announced] | None   # added field; None until a table or a message reached the showrunner
# upgrade_report.py
def announcement(entry: Entry, target: str) -> Announced: ...
def changes(record: Record) -> list[str]: ...
def line(record: Record, zone: ZoneInfo, now: datetime, next_due: datetime | None, pins_error: str) -> str: ...
```

- **Job.** `ensure_job` runs `zsh <notifier> new <instance> --every 240 --timeout 300 --run "<scripts/lib/py> <bevy_upgrade.py> --production <doc> check --scheduled"`, each word of the run command through `shlex.quote`; `new` keeps an existing instance's clock. The notifier is `BEVY_UPGRADE_NOTIFIER`, else `scripts/message/notifier.sh`; the sender is `BEVY_UPGRADE_SEND`, else `scripts/message/send.py`. `start`, `status` and a hand `check` call `ensure_job` on an open record, and a closing run calls `remove_job`, both inside the record lock, directly after the one save.
- **Messages.** `tell_showrunner` sends to `showrunners.current_address(slug)` with `--from bevy-upgrade --key bevy-upgrade-<slug> --summary <summary> --text <text>`. An empty address, `SKIPPED:`, `QUEUED:`, `REFUSED:` and `FAILED:` return `False` and print `bevy_upgrade: the showrunner was not told: <the outcome line, or "no showrunner session">` on stderr. `retire_message` runs `send.py retire --to <address> --from bevy-upgrade --key bevy-upgrade-<slug>`: silent on success, nothing run for an empty address, and `bevy_upgrade: could not retire the queued message: <outcome>` on stderr for a non-zero exit or an `OSError`, one from the address lookup included. A failed retire changes neither the exit code nor the table.
- **Baseline.** Every verb that prints the table (`start`, `status`, a hand `check`, `approve`, `decline`, `forget`, `note`, `stop`) prints `Changed since the last report:` and the `changes` lines above the table when there are any, stores every entry's `announcement` in `announced` before the save, and calls `retire_message` after it, inside the lock, so a queued message never repeats what the table showed. `status` therefore always saves. A closed record prints no change lines.
- **Change lines.** `changes` compares each entry's `announcement` with `announced`: `- <crate>: <old state> → <new state>`, with ` (<ready> on Bevy <requirement>)` after `release ready` and ` (<branch name> @ <rev 7>)` after `git ready`; `- <crate>: PR #<n> <old> → <new>` and `- <crate>: new PR #<n> <state>: <title>`, with `issue #<n>` for an item of kind `issue`; `- <crate>: new dependency` and `- <crate>: no longer a dependency`. `announced` of `None` gives no lines, and an announced item the entry no longer holds gives none. `announcement` takes `repository` from the entry's read branch; when it differs from the announced one, every item the entry holds is a new item and none is a transition.
- **Scheduled check.** `check --scheduled` prints nothing on stdout, re-reads pins, runs the check and saves once. With `announced` of `None` it stores the baseline and sends nothing. With changes it sends the summary `Bevy <target> upgrade: 1 change` or `Bevy <target> upgrade: <N> changes` and, as text, the change lines, the `line` sentence and `Full table: /showrunner:bevy_upgrade`; it stores `announced` only when the send returned `True`, so a change found while the showrunner is away is sent by a later check. With a failed pins read it makes no lookup, sends nothing, saves nothing and never closes: one `bevy_upgrade: pins could not be re-read: <reason>` line on stderr, exit 1. It never calls `retire_message`.
- **Closing.** When every entry is `done` after a pins re-read that succeeded in the same run, `check` closes the watch. By hand it sets `closed`, removes the job and prints `Bevy <target> upgrade: every dependency is on Bevy <target>. The watch is closed.` above the table. Scheduled, it sends the change lines, if any, then that sentence under the summary `Bevy <target> upgrade: done`, and only when that send returned `True` does it store `announced`, set `closed` and remove the job. `stop` sets `closed`, removes the job and prints the table. On a closed record `status` prints the table under `This watch closed <YYYY-MM-DD>.`, a hand `check` refuses with `bevy_upgrade: the watch is closed; start a new one` (exit 2), and `check --scheduled` removes the job and exits 0 with no message.
- **`line`.** One sentence for a report: `Bevy <target> upgrade: <d> of <n> done`, then each other state with a non-zero count in `STATE_ORDER` as `, <count> <state>`, with ` (<k> waiting for your answer)` after `git ready` when k entries there have no answer, then `; checked <HH:MM zone>` and `, next check <HH:MM zone>` when `next_due` is not `None`. When the table would say stale it ends `; upstream last checked <YYYY-MM-DD HH:MM zone> (stale)` instead, and while `checked` is `None`, `; no complete upstream check yet`. With a failed pins read the sentence ends `; pins not re-read since <YYYY-MM-DD HH:MM zone>` and is all that stdout holds. The verb prints nothing and exits 0 for no record or a closed one.

**Files:**
- `scripts/bevy_upgrade/upgrade_schedule.py` — the notifier job, the message to the showrunner and the retire call.
- `scripts/bevy_upgrade/upgrade_record.py` — `Announced` and `Record.announced`; `load` gives a record without the field `None` and validates a present one.
- `scripts/bevy_upgrade/upgrade_report.py` — `announcement`, `changes` and `line`.
- `scripts/bevy_upgrade/bevy_upgrade.py` — `stop`, `line`, `check --scheduled`, the baseline, the job upkeep and the closing rule.
- `scripts/bevy_upgrade/test_upgrade_schedule.py`, `test_upgrade_record.py`, `test_upgrade_report.py`, `test_cli.py`, `test_upgrade_check.py` — their tests.
- `commands/showrunner/bevy_upgrade.md` — `stop`, `line`, and the scheduled message as the one exception to the same-turn rule: its lines are shown in the next reply as received, and anything beyond them needs `status` first.

**Binds later work:**
- `line` takes no lock, saves nothing and never changes `announced`, so a reader never waits behind a running check. For an open record it still makes two notifier calls, `ensure_job` and `next_check`, and no sender call.
- `announced` is the one stored state word. It records what the showrunner was told, never what is true now.
- In `test_cli.py` the notifier and sender stand-ins record every call with its arguments, whether the record lock was held and the saved `announced` and `closed`: `tool_calls(kind)`, `tool_args(kind)` (sends only for `send`), `retire_calls()`, `retire_arguments()`, `clear_calls()`.
- A new required `Record` field also needs the record fixture in `test_upgrade_check.py`.

**Gotchas:** The notifier takes its own lock only around its state edits and releases it before running a stored command, so a notifier call made under the record lock cannot deadlock with a firing job. `line` calls `ensure_job` outside the lock; a job it re-creates for a watch closed in that instant removes itself at its next run. A watch with zero dependencies closes at its first check. `next_check` raises `ValueError` for a `next_due=` line it cannot parse. A scheduled check for a production with no record exits 0 and leaves its job.

**Ruled out:** Job upkeep after the lock is released: a `start` between a closing run's save and its job removal lost its new job. Retiring a queued message only when changes were pending: every saving hand verb retires, one rule with no state to read. `<N> changes` for a single line.

### Phase 5 — The dailies carries the upgrade as a topic read from the record  · status: done

#### As-built

While a watch is open, every dailies carries a `Bevy upgrade` topic whose text is the upgrade's `line` sentence, marked as needing the user while a git pin has no answer. The builder reads it from the upgrade command, never from the record file.

```python
# bevy_upgrade.py
class UpgradeUpdate(TypedDict): update: str; needs_user: bool
# upgrade_report.py
def waiting(record: Record) -> int: ...   # entries whose state is `git ready` with no answer
# dailies_input.py
class OpenBevyUpgradeWatch(NamedTuple): update: str; needs_user: bool
class NoOpenBevyUpgradeWatch(NamedTuple): ...
class BevyUpgradeUnavailable(NamedTuple): reason: str
BevyUpgradeWatch = OpenBevyUpgradeWatch | NoOpenBevyUpgradeWatch | BevyUpgradeUnavailable
def bevy_upgrade_watch(production: Production) -> BevyUpgradeWatch: ...
def bounded_bevy_update(text: str) -> str: ...
```

- **`line --json`.** Prints one object `{"update": <the line sentence>, "needs_user": <bool>}`, or nothing for no record or a closed one. `needs_user` is `waiting(record) > 0`, read from the record the sentence is built from; `line` uses the same count for `(<k> waiting for your answer)`.
- **Lookup.** `bevy_upgrade_watch` runs `<sys.executable> <script> --production <doc> line --json` with a 30-second timeout. The script is `DAILIES_BEVY_UPGRADE`, else `scripts/bevy_upgrade/bevy_upgrade.py`. Empty or whitespace-only output is `NoOpenBevyUpgradeWatch`. An object with a non-blank single-line `update` string and a boolean `needs_user` is `OpenBevyUpgradeWatch`; extra keys are ignored. Everything else is `BevyUpgradeUnavailable`: a non-zero exit, a timeout, an `OSError`, invalid JSON, or an object of another form. Its reason is the first stderr line when there is one, else `command exited <code>`, `timed out after 30 seconds`, or the error text of the parser or the operating system.
- **Topic.** `run()` adds the topic after the review watch topic, with the title `Bevy upgrade`, the ETA `no ETA stated yet` and the lookup's `needs_user`. For an unavailable lookup the text is `the record could not be read: <reason>` and `needs_user` is false. `bounded_bevy_update` cuts the text to the smallest finite value in `dailies_render.LENGTHS`, ending in `…`. With no open watch there is no topic.
- **Step line.** `bevy upgrade: ok — <text>`, `bevy upgrade: ok — no open watch`, or `bevy upgrade: unavailable — <text>`. A failed lookup never fails the builder.

**Files:**
- `scripts/bevy_upgrade/bevy_upgrade.py` — `line --json` and `UpgradeUpdate`.
- `scripts/bevy_upgrade/upgrade_report.py` — `waiting`.
- `scripts/bevy_upgrade/test_cli.py`, `test_upgrade_report.py` — their tests.
- `scripts/production/dailies_input.py` — the lookup, the cut and the topic (owner phase-tables-unit).
- `scripts/production/test_dailies_input.py` — the topic's tests (owner phase-tables-unit).
- `commands/showrunner/dailies.md` — Gather item 7 and the Output sentence for the `unavailable` line (owner phase-tables-unit).

**Gotchas:** Every test in `test_dailies_input.py` runs behind a `DAILIES_BEVY_UPGRADE` stand-in set in the shared `setUp`, so none reaches the real upgrade command, which for an open watch calls the notifier. The timeout test replaces `subprocess.run` in-process and waits for nothing. A change that lengthens the `line` sentence shows in the dailies cut at the renderer's limit, 240 characters today.

**Ruled out:** A second copy of the 240 limit in the builder. Shortening the `line` sentence itself.

### Phase 6 — The migration plan's dependency check sees crates that depend only on Bevy's sub-crates  · status: done

#### As-built

`/bevy_migration_plan`'s dependency check lists and classifies a crate that depends on Bevy only through one of Bevy's own crates, such as `bevy_app` or `bevy_ecs`, as it does one that depends on `bevy`. A crates.io lookup that gets no answer keeps the crate in the report for manual verification.

```python
# bevy_migration_dependency_check.py — every class is a frozen dataclass
class BevyRequirement: requirement: str
class NoBevyDependency: ...
class RequirementLookupFailed: reason: str
RequirementLookup = BevyRequirement | NoBevyDependency | RequirementLookupFailed
class DiscoveredDependency: name: str; version: str; lookup: BevyRequirement | RequirementLookupFailed
class CompatibleVersion: version: str
class NoCompatibleVersion: ...
class CompatibilityUnknown: reason: str
class BevyCheckoutRead: names: frozenset[str]
class BevyCheckoutMissing: path: Path
BevyCheckout = BevyCheckoutRead | BevyCheckoutMissing
def get_bevy_internal_crates(bevy_version: str) -> BevyCheckout: ...
def bevy_requirement_names(bevy_checkout: BevyCheckout) -> frozenset[str]: ...
def get_bevy_dependency_requirement(crate_name: str, version: str, bevy_names: frozenset[str]) -> RequirementLookup: ...
def get_all_direct_dependencies(codebase: Path, bevy_checkout: BevyCheckout) -> list[tuple[str, str]]: ...
def get_bevy_dependencies(codebase: Path, bevy_checkout: BevyCheckout) -> list[DiscoveredDependency]: ...
def find_bevy_compatible_version(crate_name: str, all_versions: list[str], target_bevy_version: str, bevy_checkout: BevyCheckout) -> CompatibleVersion | NoCompatibleVersion | CompatibilityUnknown: ...
```

- **Bevy's own crate names.** `get_bevy_internal_crates` runs once, in `main`, and its result is passed down. A read checkout holds every crate name under `~/rust/bevy-<version>/crates`, plus `bevy` and `bevy_internal`: a requirement may come from any of them, and `get_all_direct_dependencies` leaves them out of its list. A missing checkout allows only `bevy` and leaves nothing out.
- **The requirement rule.** Among a release's dependencies whose kind is not `dev`: the `req` of the one named `bevy`, else of the first by sorted name whose `crate_id` is in `bevy_names`. A crates.io request that fails is `RequirementLookupFailed`, never `NoBevyDependency`, and `get_bevy_dependencies` leaves a crate out only for `NoBevyDependency`.
- **Compatible version.** The newest of the 20 most recent versions whose requirement fits; `CompatibilityUnknown`, with the first failure's reason, when none fits and at least one lookup failed.
- **Report.** A failed discovery lookup or `CompatibilityUnknown` gives a `CHECK_NEEDED` entry with the reason, under "Verification Needed", unless one of the crate's releases fits the target. Nothing else in the report changed.
- **Missing checkout.** `Warning: <path> not found; crates that depend only on a Bevy sub-crate are not detected` is printed once on stderr and stands under the report's title, on an empty report too.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_dependency_check.py` — the dependency check behind `/bevy_migration_plan` (owned by no unit of this production).
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — its tests, with canned crates.io bodies, a temporary home directory and no cargo.

**Gotchas:** The script runs `cargo tree` on the codebase, so a live run counts as a build on that project. `/bevy_migration_plan` clones `~/rust/bevy-<version>` itself; a machine that has only `~/rust/bevy` reports the missing checkout. A direct dependency that is not on crates.io fails its lookup and is listed under "Verification Needed". The script reads the codebase's direct dependencies only.

**Ruled out:** Sharing the requirement rule's code with `scripts/bevy_upgrade/upstream.release_requirement`: this script runs alone and prints its own warnings.

### Phase 7 — The check reads a workspace member's Bevy requirement and asks again about a kept open pull request  · status: done

#### As-built

When a repository's root manifest names no Bevy requirement and has a `[workspace]` table, the check finds the crate's own manifest among the workspace members and reads the requirement there, so such a dependency reads `git ready` once its branch is on the target. A pull request stored as `open` is asked for its state again whenever GitHub's answer no longer lists it.

```python
# scripts/bevy_upgrade/upstream.py
def member_manifest_paths(fetch: Fetch, repository: str, rev: str) -> list[str]: ...
def manifest_requirement(fetch: Fetch, repository: str, rev: str, crate: str, bevy_names: frozenset[str]) -> str: ...

# scripts/bevy_upgrade/upgrade_check.py
def _github_facts(fetch: upstream.Fetch, name: str, entry: Entry, record: Record, bevy_names: frozenset[str]) -> None: ...
```

- **Tree lookup.** `member_manifest_paths` asks `https://api.github.com/repos/<repository>/git/trees/<rev>?recursive=1` once and returns the sorted paths of `blob` entries ending `/Cargo.toml`; any other answer is a `FetchFailure`.
- **Member search.** `manifest_requirement` reads the root as before. Members are read only when the root gives no requirement and has `[workspace]`: the directory named like the crate first (`-` and `_` equal), then sorted order, at most five reads, until one's `[package] name` equals the crate. That member's `[dependencies]` is read by the root's rule with the root's `[workspace.dependencies]` as the inherited table. A matching member ends the search even when its requirement is empty.
- **Reuse.** `_github_facts` reuses a stored requirement only when the stored `rev` is the branch head's and the requirement is not empty.
- **Kept items.** A kept pull request in state `open` is asked again with `pull_state`, which updates its state and head. Kept issues and kept closed or merged pull requests cost no request.
- **`/bevy_migration_plan` doc.** The two bullets under "The script will" say the dependency check reads direct dependencies, including crates that depend only on Bevy's own crates, and that `CHECK_NEEDED` means compatibility is unclear or crates.io did not answer a lookup.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — the tree lookup, the shared manifest reader and requirement rule, the member search
- `scripts/bevy_upgrade/upgrade_check.py` — passes the crate name, the reuse rule, the refresh of kept open pull requests
- `scripts/bevy_upgrade/test_upstream.py`, `scripts/bevy_upgrade/test_upgrade_check.py` — tests for the tree lookup, the member search, reuse, kept items and rollback
- `commands/bevy_migration_plan.md` — the two corrected bullets

**Gotchas:**
- A member entry with `workspace = true` under a root entry that has no version gives an empty requirement.
- A member that answers `NotFound` is skipped and counts as one of the five reads. Every other member failure is a `FetchFailure`, so the entry keeps its stored facts; this includes a wrongly typed table in a member that is not the crate's.
- `[workspace] members` and `exclude` are not read; the tree decides which manifests exist.
- GitHub marks a very large tree `truncated` and the lookup does not read that field, so such a repository can give an empty requirement.
- An entry whose requirement stays empty is read again on every check: up to seven requests (root, tree, five members).
- Kept items are copied before the refresh, so a later failed request cannot change the stored entry.

**Ruled out:**
- Reading `[workspace] members` globs to find the member: the tree lookup needs no glob rules.
- Following a truncated tree with further requests: the search is bounded and answers unknown.

## Close-out for the showrunner

Production-level steps no phase can run, because they act in another production's session or on its state:

- After the scheduling phase is on main, the hana showrunner runs `/showrunner:bevy_upgrade start 0.20`, then `approve bevy_enhanced_input` with the user's words from **Source**, then `note` for what no lookup finds: bevy_tween pull request 97's CI fails only in its `bevy_tween_lookup_curve` sub-crate, which pins bevy_lookup_curve 0.12, and issue 91 asks for a crates.io release.
- The hana showrunner then removes the hand-written watch: `zsh ~/.claude/scripts/message/notifier.sh remove bevy020-watch` and the directory `~/.local/state/bevy020-watch/`.
- The hand-written watch listed five crates. Hana's lockfile has nine third-party crates on Bevy and one patched Bevy crate; `bevy_egui`, `bevy_time_runner`, `moonshine-save` and `moonshine-util` were not watched.
