# Bevy upgrade: one record of every dependency, kept current for the showrunner

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/showrunner:bevy_upgrade` keeps one record per production of every crate that ties the project to its Bevy version: our pin, what crates.io and GitHub offer for the target, and the user's answer on git pins. A scheduled check keeps it current, and every report line comes from it.

> **As-built disposition: create**

> **Production: build-followups** — unit `bevy-upgrade-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 11:54 PDT

okay - we should have some kind of note for tracking the upgrade so your report doesn't have bad information - are we managing this with a skill or just the script that runs ever 4 hours or what? if not, we should ask natedev to create a bevy upgrade skill for showrunners - that not only does the periodic check but also saves information about each dependency so we can limit the work we do - we can upgrade to 0.20.0 on bei now if we do a git dependency which i would approve

## Delegation Context

- **Project:** `~/.claude` tooling (this repo) — a showrunner skill that tracks a Bevy upgrade's dependencies.
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-bevy-upgrade`, branch `build-followups-bevy-upgrade`. Every edit and every command of every phase runs there (the user, 2026-10-12).
- **Stack:** Python 3.11+ standard library only (`tomllib`, `json`, `urllib.request`, `fcntl`, `argparse`, `zoneinfo`, `shlex`, `subprocess`); the zsh notifier.
- **Layout:** `scripts/bevy_upgrade/` (new) holds every module and its tests; `commands/showrunner/bevy_upgrade.md` (new) is the skill; `pyrightconfig.json` gains one entry. The dailies phase alone edits `scripts/production/dailies_input.py`, its test and `commands/showrunner/dailies.md`.
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
  - Tests never reach the network, the real state or a real session. All HTTP goes through one injected `Fetch`; every path comes from `BEVY_UPGRADE_STATE_DIR` when set; the notifier and `send.py` are replaced through `BEVY_UPGRADE_NOTIFIER` and `BEVY_UPGRADE_SEND`.
  - A failed lookup never replaces a stored fact. It sets that dependency's `error` and leaves its `checked` time alone, so old data is never shown as current.
  - The record stores what a script cannot look up again for free (user rule 2026-10-08): upstream facts with the time they were read, and the user's answers and notes. State words are derived when printed and never stored.
  - Only `approve`, `decline` and `forget` change a recorded answer. No default approves, and an answer covers one crate in one upgrade.
  - No project data in the code: no crate names, repositories or issue numbers. Everything comes from the checkout's `Cargo.lock`, crates.io and GitHub.
  - No Pushover and no `send.py --to user`.
  - Python rules (CLAUDE.md): annotate every signature; `TypedDict` for JSON with known keys; no file-level ignores; a line-level `# pyright: ignore[reportAny]` only where `json` or `urllib` returns `Any`.
  - A seat that edits a file under `commands/` loads `/succinct_style` first.

## Phases

### Phase 1 — A showrunner starts a watch and reads pins and the user's answers from the record  · status: todo

#### Work Order

**Goal:** `/showrunner:bevy_upgrade start 0.20` writes a record of every crate in the production's lockfile that depends on Bevy, and `approve`, `decline`, `forget` and `note` keep the user's answers in it, all printed as one table.

**Spec:**
Work only in the worktree and branch the Delegation Context names. All modules use flat imports (`import pins`), as `scripts/production/` does.

`scripts/bevy_upgrade/pins.py` reads `<checkout>/Cargo.lock` and `<checkout>/Cargo.toml` with `tomllib`. No cargo command runs.

```python
class RegistryPin(TypedDict):
    source: Literal["registry"]
    version: str
class GitPin(TypedDict):
    source: Literal["git"]
    version: str
    url: str    # without query or fragment
    ref: str    # the lock source's query: "branch=main", "rev=abc1234", "tag=v1", or ""
    rev: str    # the full commit after "#"
class PathPin(TypedDict):
    source: Literal["path"]
    version: str
    path: str   # as the [patch] table writes it
Pin = RegistryPin | GitPin | PathPin

class Dependent(NamedTuple):
    name: str
    pin: Pin
    bevy: str               # the Bevy version this crate resolves against in the lockfile
    via: tuple[str, ...]    # empty when one of the project's own packages names it
    patched_bevy: bool      # a Bevy crate replaced through [patch]

class Pins(NamedTuple):
    bevy: tuple[str, ...]              # every Bevy version in the lockfile, oldest first
    bevy_names: frozenset[str]         # the names of the Bevy crates
    dependents: tuple[Dependent, ...]  # sorted by name

class PinsFailure(Exception): ...
def read_pins(checkout: Path) -> Pins: ...
```

- The Bevy versions are the versions of the package `bevy_ecs`. With none, raise `PinsFailure("<lock path>: no bevy_ecs package, so this is not a Bevy project")`. A missing or unparseable file raises `PinsFailure("<path>: <reason>")`.
- A **Bevy crate** is a package named `bevy` or `bevy_*` whose version is one of those versions, and that either comes from the crates.io registry (`source` starts with `registry+`) or is named by a `[patch.*]` table of the root manifest.
- A **dependent** is any other package with a `source` (registry or git) whose `dependencies` list names a Bevy crate. A lock dependency string is `name`, `name version` or `name version (source)`; a bare name resolves to that name's only version. The dependent's `bevy` is the oldest Bevy version among the Bevy crates it lists. With two lock versions of one dependent, keep the one on the oldest Bevy.
- A package with no `source` is the project's own (a workspace member or a path crate) and is never a row, unless a `[patch.*]` table names it.
- A `[patch.*]` entry whose key is a Bevy crate is a row with `patched_bevy=True`, `bevy` equal to its own lock version, and a `PathPin` (or a `GitPin` when the patch is `git`). A `[patch.*]` entry whose key is a dependent gives that dependent its `PathPin` or `GitPin`.
- `via` is empty when a package with no `source` lists the dependent. Otherwise it is the sorted names of the packages that list it.

`scripts/bevy_upgrade/record.py` owns the record file, `<state dir>/<slug>.json`, where the state dir is `BEVY_UPGRADE_STATE_DIR` when set, else `~/.local/state/bevy-upgrade`.

```python
class Answer(TypedDict):
    answer: Literal["approved", "declined"]
    at: str      # YYYY-MM-DD in the production's zone
    words: str   # the user's words, verbatim
class Entry(TypedDict):
    pin: Pin
    bevy: str
    via: list[str]
    patched_bevy: bool
    answer: Answer | None
    note: str
class Record(TypedDict):
    slug: str
    production: str      # the production doc's path
    checkout: str
    target: str          # major.minor, e.g. "0.20"
    started: str         # ISO-8601 UTC
    closed: str | None
    bevy: list[str]
    pins_read: str       # ISO-8601 UTC
    dependencies: dict[str, Entry]

class RecordFailure(Exception): ...
def state_dir() -> Path: ...
def record_path(slug: str) -> Path: ...
def locked(slug: str) -> ContextManager[None]: ...   # exclusive flock on <state dir>/<slug>.lock
def load(slug: str) -> Record | None: ...            # None when no file; RecordFailure when unreadable
def save(record: Record) -> None: ...                # temp file beside it, then replace
def start(production: Production, target: str, pins: Pins, now: datetime) -> Record: ...
def apply_pins(record: Record, pins: Pins, now: datetime) -> Record: ...
def set_answer(record: Record, crate: str, answer: Literal["approved", "declined"], words: str, today: str) -> Record: ...
def forget_answer(record: Record, crate: str) -> Record: ...
def set_note(record: Record, crate: str, text: str) -> Record: ...
def on_target(version: str, target: str) -> bool: ...   # same major.minor
```

- `start` refuses a target that does not match `^\d+\.\d+$` with `RecordFailure("target must be major.minor, such as 0.20")`. With an open record for the same target it returns that record after `apply_pins` (idempotent). With an open record for another target it refuses: `RecordFailure("a watch for <target> is open; stop it first")`. With a closed record it moves the old file to `<slug>.<old target>.closed.json` and begins a new one; answers and notes do not carry over.
- `apply_pins` adds new dependents with `answer=None` and `note=""`, updates `pin`, `bevy`, `via` and `patched_bevy` of known ones and keeps their other fields, and removes entries the lockfile no longer has.
- `set_answer`, `forget_answer` and `set_note` refuse an unknown crate with `RecordFailure("<crate> is not tracked; tracked: <names, comma separated>")`. `set_answer` refuses empty `words`. A second answer replaces the first.
- Every read-modify-write of the file runs inside `locked(slug)`.

`scripts/bevy_upgrade/report.py`:

```python
State = Literal["git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done"]
STATE_ORDER: tuple[State, ...]   # the order above
def state(entry: Entry, target: str) -> State: ...
def table(record: Record, zone: ZoneInfo, now: datetime) -> str: ...
```

- This phase derives three states: `done` when `on_target(entry["bevy"], target)`; else `re-vendor` when `patched_bevy`; else `not checked`.
- `table` prints, in this order: the line `Bevy <target> upgrade for <slug>: <checkout>, on Bevy <versions joined by " and ">.`; the line `Upstream: not checked yet.`; a blank line; a Markdown table with the header `| Dependency | Our pin | crates.io | Git | State | Next |`; then, when any exist, `Answers on git pins:` with one `- <crate>: <answer> <at>: "<words>"` line each, and `Notes:` with one `- <crate>: <note>` line each.
- Rows sort by `STATE_ORDER`, then name.
- Cells. Dependency: `<name>`, `<name> (via <a>, <b>)`, or `<name> (patched Bevy crate)`. Our pin: `<version>` for a registry pin, `git <ref or first 7 of rev> @ <first 7 of rev>` for a git pin, `path <path>, <version>` for a path pin, each followed by ` on Bevy <bevy>` unless `patched_bevy`. crates.io and Git: `—` in this phase. Next: `—` for `done`, `re-vendor from Bevy <target>` for `re-vendor`, `run check` for `not checked`.

`scripts/bevy_upgrade/bevy_upgrade.py` is the CLI:

```
bevy_upgrade.py [--production <doc>] [status]
bevy_upgrade.py [--production <doc>] start <target>
bevy_upgrade.py [--production <doc>] approve <crate> <words>
bevy_upgrade.py [--production <doc>] decline <crate> <words>
bevy_upgrade.py [--production <doc>] forget <crate>
bevy_upgrade.py [--production <doc>] note <crate> [<text>]
```

- Without `--production` the doc is `showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))`. With none: exit 2 and `bevy_upgrade: this session runs no production; pass --production <doc>` on stderr. The doc is read with `add_unit.read_production`; its `checkout` is the tree whose lockfile is read and its `zone` is the zone of every printed time.
- Every verb but `start` on a production with no record prints `no Bevy upgrade is tracked for <slug>; start one with /showrunner:bevy_upgrade start <version>` and exits 0.
- Every verb re-reads the pins from the checkout and saves them before it prints, so `Our pin` is never older than the lockfile. When the read fails on a verb other than `start`, the table prints from the stored pins under the first line `pins could not be re-read: <reason>; showing pins read <HH:MM zone>`.
- Every verb ends by printing `table`. `note` with no text clears the note. The `at` of an answer is today's date in the production's zone.
- A refusal (`RecordFailure`, `PinsFailure` on `start`, `Refusal`) prints one `bevy_upgrade: <reason>` line on stderr and exits 2.

`commands/showrunner/bevy_upgrade.md` follows `commands/showrunner/proposals.md`: frontmatter `description` and `argument-hint`, run in the showrunner session, run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/bevy_upgrade/bevy_upgrade.py" <arguments>` and print its output exactly as it comes. It also states three rules. Any statement about the upgrade in a report or reply comes from this command's output in the same turn, never from the log or memory. `approve` and `decline` are run only on the user's own words about that crate, passed verbatim; an answer for one crate is never extended to another. `note` holds what a script cannot look up, such as why a pull request's CI fails.

`pyrightconfig.json` gains one `executionEnvironments` entry: root `scripts/bevy_upgrade`, extraPaths `scripts/bevy_upgrade` and `scripts/production`.

**Files:**
- `scripts/bevy_upgrade/pins.py` — new: the lockfile and manifest reader.
- `scripts/bevy_upgrade/record.py` — new: the record's types, store, pins merge, answers and notes.
- `scripts/bevy_upgrade/report.py` — new: the derived state and the table.
- `scripts/bevy_upgrade/bevy_upgrade.py` — new: the CLI.
- `scripts/bevy_upgrade/test_pins.py` — new: tests of `read_pins`.
- `scripts/bevy_upgrade/test_record.py` — new: tests of the store, `start`, `apply_pins`, answers and notes.
- `scripts/bevy_upgrade/test_report.py` — new: tests of `state` and `table`.
- `scripts/bevy_upgrade/test_cli.py` — new: the CLI run as a subprocess.
- `commands/showrunner/bevy_upgrade.md` — new: the skill.
- `pyrightconfig.json` — one execution environment for `scripts/bevy_upgrade`.

**Seats:** 1 writer + 1 tester — the four modules import each other, so one writer holds them; the tests are written from the Spec.
- `impl` — `pins.py`, `record.py`, `report.py`, `bevy_upgrade.py`, `commands/showrunner/bevy_upgrade.md`; hub: `pyrightconfig.json` (owner followups-unit, run done; one entry).
- `test` — `test_pins.py`, `test_record.py`, `test_report.py`, `test_cli.py`, from the Spec's signatures and rules. Lockfiles and manifests are written into temporary directories by the tests. The CLI tests pass `--production` with a production doc written to a temporary directory and set `BEVY_UPGRADE_STATE_DIR`.

**Constraints from prior phases:** none.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. The tests cover: a registry, a git and a `[patch]` path pin; a patched Bevy crate; a dependent reached only through another (`via`); a lockfile with two Bevy versions; `start` idempotent, refused for a second target, and archiving a closed record; an answer replaced, forgotten, and refused for an unknown crate; the table's header, row order and the `Answers on git pins:` and `Notes:` lists. Run once by hand and paste the output in the handoff: `BEVY_UPGRADE_STATE_DIR=<scratch> python3 scripts/bevy_upgrade/bevy_upgrade.py --production /home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md start 0.20` lists ten rows, among them `bevy_render (patched Bevy crate)` and `bevy_egui (via bevy-inspector-egui)`.

### Phase 2 — `check` records each dependency's crates.io releases and which one is on the target  · status: todo

#### Work Order

**Goal:** `/showrunner:bevy_upgrade check` reads crates.io once per dependency that still needs it and records the newest release, the release built on the target, and whether the target Bevy is itself released.

**Spec:**
Work only in the worktree and branch the Delegation Context names.

`scripts/bevy_upgrade/upstream.py` holds every lookup. Each takes the fetcher as its first argument.

```python
Fetch = Callable[[str], bytes]            # GET one URL; raises FetchFailure on any failure
class FetchFailure(Exception): ...        # str() is "<host>: <reason>", e.g. "crates.io: HTTP 503"
def http_fetch(url: str) -> bytes: ...    # urllib, User-Agent "bevy-upgrade (natepiano)", timeout 30 s

class CrateInfo(NamedTuple):
    versions: tuple[str, ...]    # not yanked, in the API's order (newest first)
    repository: str | None       # "owner/name" when the crate's repository is on github.com
def crate_info(fetch: Fetch, crate: str) -> CrateInfo: ...
def release_requirement(fetch: Fetch, crate: str, version: str, bevy_names: frozenset[str]) -> str: ...

class TargetRelease(NamedTuple):
    version: str | None    # the newest release on the target that is not a pre-release
    date: str | None       # its YYYY-MM-DD
    since: str | None      # YYYY-MM-DD of the first version on the target, pre-releases included
def target_release(fetch: Fetch, target: str) -> TargetRelease: ...
```

- `crate_info` reads `https://crates.io/api/v1/crates/<crate>`: `versions[].num` and `.yanked`, and `crate.repository`. A repository URL on github.com gives `owner/name` with any `.git` suffix and trailing path removed.
- `release_requirement` reads `https://crates.io/api/v1/crates/<crate>/<version>/dependencies` and returns the `req` of the dependency named `bevy` when one of kind other than `dev` exists, else the `req` of the first such dependency, by sorted name, whose `crate_id` is in `bevy_names`, else `""`. A name outside `bevy_names` is never read as Bevy: `bevy_egui` is a third-party crate.
- `target_release` reads `https://crates.io/api/v1/crates/bevy/versions` and looks at each `num` that starts with `<target>.`, with its `created_at`.

`record.py` grows:

```python
class Entry(TypedDict):          # added fields
    releases: dict[str, str]     # version → its Bevy requirement; a release never changes, so each is read once
    newest: str | None
    ready: str | None            # the newest release whose requirement is on the target
    repository: str | None
    checked: str | None          # ISO-8601 UTC of the last complete read of this dependency
    error: str                   # the last check's failure, or ""
class Record(TypedDict):         # added fields
    target_release: str | None
    target_date: str | None
    since: str | None
    checked: str | None          # ISO-8601 UTC of the last check that finished
def requirement_on_target(requirement: str, target: str) -> bool | None: ...
```

- `requirement_on_target` strips one leading `^`, `~` or `=`; a remainder matching `\d+\.\d+(\.\d+)?(-[\w.]+)?` is compared by major.minor. Anything else (a range, `*`, an empty string) is `None`.
- `load` fills the added fields with their empty values when a record written by the first phase lacks them. `apply_pins` gives a new entry the empty values.

`scripts/bevy_upgrade/check.py`:

```python
def run_check(record: Record, pins: Pins, fetch: Fetch, now: datetime) -> Record: ...
```

- While `record["target_release"]` is `None`, call `target_release` once per check and store `target_release`, `target_date` and `since`. Once the release is known this lookup never runs again.
- Per entry, in name order. No request at all when the entry is `done`, is a patched Bevy crate, has a path pin, or already has `ready`. Otherwise call `crate_info`; walk its versions from the newest down to and including our pinned version (all of the newest six when the pin is not among them); call `release_requirement` only for versions missing from `releases`; set `newest`, `repository`, and `ready` to the newest walked version for which `requirement_on_target` is `True`.
- A `FetchFailure` in one entry sets its `error` to the failure's text and leaves every other field of that entry as it was, `checked` included. The other entries still run. Success sets `error` to `""` and `checked` to `now`.
- `record["checked"]` is set to `now` when the pass ends, whether or not entries failed.

`report.py`: `state` gains `release ready` (not `done`, `ready` set), ahead of `not checked`; an entry with `checked` set, no `ready` and no path pin reads `no port yet` in this phase. The crates.io cell reads `<newest> on Bevy <requirement>`, with ` → <ready> on Bevy <its requirement>` appended when `ready` differs from `newest`, `—` for a patched Bevy crate or a path pin, and `not checked` before the first read. An entry with `error` appends `; failed <HH:MM zone>: <error>` to that cell. Next: `bump to <ready>` for `release ready`, `wait` for `no port yet`. The second line of `table` becomes `Bevy <target>: released as <version> on <date>.` or `Bevy <target>: not released yet.`, followed by ` Upstream checked <HH:MM zone>.`; when `checked` is older than eight hours or any entry has `error`, that sentence reads ` Upstream last checked <YYYY-MM-DD HH:MM zone> (stale).` instead.

`bevy_upgrade.py` gains `check`: re-read pins, `run_check` with `http_fetch`, save, print the table. `start` runs the same check after it writes the record. `check` exits 1 when every entry that made a request failed. The command file gains `check` and the stale rule: a report says `stale` when the table does.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — new: the fetcher and the crates.io lookups.
- `scripts/bevy_upgrade/check.py` — new: one check pass and its skip rules.
- `scripts/bevy_upgrade/record.py` — the upstream fields, their defaults and `requirement_on_target`.
- `scripts/bevy_upgrade/report.py` — `release ready`, the crates.io cell, the second line.
- `scripts/bevy_upgrade/bevy_upgrade.py` — the `check` verb; `start` runs it.
- `scripts/bevy_upgrade/test_upstream.py` — new: each lookup against canned response bodies.
- `scripts/bevy_upgrade/test_check.py` — new: `run_check` with a fetcher that records every URL.
- `scripts/bevy_upgrade/test_record.py` — `requirement_on_target` and loading a first-phase record.
- `scripts/bevy_upgrade/test_report.py` — the new state, cells and stale line.
- `commands/showrunner/bevy_upgrade.md` — the `check` verb and the stale rule.

**Seats:** 1 writer + 1 tester — `check.py` calls `upstream.py` and writes `record.py` fields, so one writer holds the modules.
- `impl` — `upstream.py`, `check.py`, `record.py`, `report.py`, `bevy_upgrade.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — `test_upstream.py`, `test_check.py`, and the additions to `test_record.py` and `test_report.py`, from the Spec. The fetcher in every test is a function over a dict of URL to body that records each URL asked for; an unknown URL raises `FetchFailure`.

**Constraints from prior phases:** the first phase built `read_pins(checkout) -> Pins` with `bevy_names`, the `Record` and `Entry` types with `load`, `save`, `locked`, `apply_pins` and `on_target`, `report.state` and `report.table`, and the CLI's `--production` resolution and refusal form. The record file is `<state dir>/<slug>.json`. Tests set `BEVY_UPGRADE_STATE_DIR`.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. `test_check.py` asserts the URLs requested: none for a `done` entry, a patched Bevy crate, or an entry with `ready`; no `dependencies` URL for a version already in `releases`; one `bevy` versions URL while the target is unreleased and none after. It asserts that a failing entry keeps its facts and `checked` while the others update. `test_upstream.py` covers a yanked version, a requirement taken from `bevy_app` when `bevy` is absent, a `bevy_egui` dependency ignored, and a repository URL ending `.git`. Run once by hand against the real network and paste the table in the handoff: `BEVY_UPGRADE_STATE_DIR=<scratch> python3 scripts/bevy_upgrade/bevy_upgrade.py --production /home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md start 0.20`.

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

`record.py`: `Entry` gains `default_branch: str | None`, `branch_rev: str | None`, `branch_committed: str | None`, `branch_requirement: str | None` and `items: list[Item]`, with empty defaults filled by `load` and `apply_pins`.

`check.py`: after the crates.io step, an entry that still has no `ready` and has a `repository` reads GitHub. An entry with `ready`, or with no `repository`, makes no GitHub request and its `items` are left as they are.

- `default_branch` once: skipped when the entry already stores it.
- `branch_head` every check. `manifest_requirement` only when the head's `rev` differs from the stored `branch_rev`.
- `port_items` every check, with `record["since"]` (the record's `started` date when `since` is `None`). `pull_state` only for a `pr` whose `updated` differs from the stored item's, or that is new; an unchanged `pr` keeps its stored `state` and `head`.
- A git pin's own requirement needs no request: `Entry.bevy` already comes from the lockfile.
- The failure rule of the crates.io step covers these requests too: one `FetchFailure` leaves the whole entry as it was and sets `error`.

`report.py`: `state` is now, in order: `done`; `re-vendor`; `release ready`; `git ready` when `requirement_on_target(branch_requirement, target)` is `True`; `port in PR` when any item is a `pr` in state `open`; `not checked` when `checked` is `None`; else `no port yet`.

- Git cell: `<default_branch> @ <first 7 of branch_rev> on Bevy <branch_requirement>` (`requirement unknown` when it is `""`), then each item as `; PR #<n> <state> (<head>)` or `; issue #<n> <state>`, newest number first. `—` when the entry made no GitHub read; `not on GitHub` when a check ran and `repository` is `None`.
- Next for `git ready`: with an `approved` answer, `pin git rev <first 7 of branch_rev> (approved <at>)`; with a `declined` answer, `wait for a release (declined <at>)`; with no answer, `ask the user: git pin?`. Next for `port in PR`: `wait for PR #<n>`.

The command file gains: when a row reads `ask the user: git pin?`, ask the user once, then record the answer with `approve` or `decline`; a pin to an unmerged pull request's branch needs the same answer.

**Files:**
- `scripts/bevy_upgrade/upstream.py` — the GitHub lookups.
- `scripts/bevy_upgrade/check.py` — the GitHub step and its skip rules.
- `scripts/bevy_upgrade/record.py` — the branch and item fields and their defaults.
- `scripts/bevy_upgrade/report.py` — `git ready`, `port in PR`, the Git cell and the Next cell.
- `scripts/bevy_upgrade/test_upstream.py` — the GitHub lookups against canned bodies.
- `scripts/bevy_upgrade/test_check.py` — the GitHub step's requests and failures.
- `scripts/bevy_upgrade/test_report.py` — the new states and cells.
- `commands/showrunner/bevy_upgrade.md` — the rule for asking the user once.

**Seats:** 1 writer + 1 tester — the same four modules as the crates.io phase, each extended.
- `impl` — `upstream.py`, `check.py`, `record.py`, `report.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — the additions to `test_upstream.py`, `test_check.py` and `test_report.py`, from the Spec, with the recording fetcher the crates.io phase's tests use.

**Constraints from prior phases:** the crates.io phase built `Fetch`, `FetchFailure`, `http_fetch`, `crate_info` (which stores `repository` as `owner/name`), `run_check(record, pins, fetch, now)` with its per-entry failure rule, `requirement_on_target`, and `Record.since`. `report.state` returns `no port yet` for a checked entry with no `ready`; this phase refines it. Tests use a fetcher over a dict of URL to body that records every URL asked for.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. `test_check.py` asserts: no GitHub URL for an entry with `ready` or with no `repository`; no repository URL once `default_branch` is stored; no manifest URL while the head `rev` is unchanged; no pull URL for a `pr` whose `updated` is unchanged; a failing GitHub request leaves the entry's crates.io facts and `checked` as they were. `test_upstream.py` covers a manifest that inherits `bevy` from `[workspace.dependencies]`, a title holding `v0.20.0`, an item created before `since`, and a merged pull request. `test_report.py` covers the three Next cells of `git ready`. Run once by hand against the real network and paste the table in the handoff, with the command of the crates.io phase's gate.

### Phase 4 — The check runs every four hours and tells the showrunner only what changed  · status: todo

#### Work Order

**Goal:** Starting a watch schedules its check; a scheduled check is silent unless a dependency's state changed, and the watch closes itself when every dependency is on the target.

**Spec:**
Work only in the worktree and branch the Delegation Context names.

`scripts/bevy_upgrade/schedule.py`:

```python
def instance(slug: str) -> str: ...                           # "bevy-upgrade-<slug>"
def ensure_job(record: Record) -> None: ...
def remove_job(slug: str) -> None: ...
def next_check(slug: str) -> datetime | None: ...             # from `notifier.sh status <instance>`
def tell_showrunner(slug: str, summary: str, text: str) -> bool: ...
```

- The notifier is `BEVY_UPGRADE_NOTIFIER` when set, else `scripts/message/notifier.sh` beside this repo's `scripts/`; it runs as `zsh <notifier> …`. `send.py` is `BEVY_UPGRADE_SEND` when set, else `scripts/message/send.py`.
- `ensure_job` runs `new <instance> --every 240 --timeout 300 --run "<py> <bevy_upgrade.py> --production <doc> check --scheduled"`, each word of the run command passed through `shlex.quote`, `<py>` being `scripts/lib/py`. `new` keeps an existing instance's clock, so calling it again is safe.
- `tell_showrunner` sends to `showrunners.current_address(slug)` with `--from bevy-upgrade --key bevy-upgrade-<slug> --summary <summary> --text <text>`. It returns `False`, sending nothing, when the address is empty or `send.py` exits non-zero.

`record.py`: `Record` gains `announced: dict[str, str]`, each crate's digest as the showrunner last saw it, default `{}`.

`report.py`:

```python
def digest(entry: Entry, target: str) -> str: ...   # state, ready, and each item's number and state
def changes(record: Record) -> list[str]: ...
def line(record: Record, zone: ZoneInfo, now: datetime, next_due: datetime | None) -> str: ...
```

- `changes` compares each entry's `digest` with `record["announced"]` and returns one line per difference: `- <crate>: <old state> → <new state>` when the state word changed, with ` (<ready> on Bevy <requirement>)` after `release ready` and ` (<default_branch> @ <rev 7>)` after `git ready`; `- <crate>: PR #<n> <old> → <new>` or `- <crate>: new PR #<n> <state>: <title>` for an item; `- <crate>: new dependency` and `- <crate>: no longer a dependency` for entries that came or went. An empty `announced` (a watch that has never reported) gives no lines.
- `line` is one sentence for a report: `Bevy <target> upgrade: <d> of <n> done`, then each other state with a non-zero count in `STATE_ORDER` as `, <count> <state>`, with ` (<k> waiting for your answer)` after `git ready` when k entries there have no answer, then `; checked <HH:MM zone>` and `, next check <HH:MM zone>` when `next_due` is known. When the table would say stale, it ends `; upstream last checked <YYYY-MM-DD HH:MM zone> (stale)` instead.

`bevy_upgrade.py`:

- `start` calls `ensure_job` after its first check. `status`, `check` and `line` call it too whenever the record is open, so a job that went missing comes back.
- `status` and `check` print `Changed since the last report:` and the `changes` lines above the table when there are any, then store every entry's `digest` in `announced`. `line` prints only the sentence and never touches `announced`.
- `check --scheduled` prints nothing. It re-reads pins, runs the check, and saves. With `changes`, it sends `tell_showrunner(slug, "Bevy <target> upgrade: <N> changes", <the change lines, then the line sentence, then "Full table: /showrunner:bevy_upgrade">)` and stores the digests in `announced` only when the send returned `True`, so a change found while the showrunner is away is sent by a later check.
- When every entry is `done` after a pins read, any `check` closes the watch: it sets `closed`, removes the job, and (scheduled) sends `Bevy <target> upgrade: every dependency is on Bevy <target>. The watch is closed.` or (by hand) prints that sentence above the table.
- `stop` sets `closed`, removes the job and prints the table. On a closed record `status` prints the table under `This watch closed <YYYY-MM-DD>.` and `check` refuses with `bevy_upgrade: the watch is closed; start a new one`.
- `line` prints nothing and exits 0 when the production has no record or the record is closed.

The command file gains `stop`, `line`, and: the scheduled check writes to this session only when something changed; show those lines to the user in the next reply.

**Files:**
- `scripts/bevy_upgrade/schedule.py` — new: the notifier job and the message to the showrunner.
- `scripts/bevy_upgrade/record.py` — the `announced` field.
- `scripts/bevy_upgrade/report.py` — `digest`, `changes` and `line`.
- `scripts/bevy_upgrade/bevy_upgrade.py` — `stop`, `line`, `check --scheduled`, the job upkeep and the closing rule.
- `scripts/bevy_upgrade/test_schedule.py` — new: the notifier and send commands, through stub scripts.
- `scripts/bevy_upgrade/test_report.py` — `digest`, `changes` and `line`.
- `scripts/bevy_upgrade/test_cli.py` — `stop`, `line`, and `check --scheduled` with a fixture fetcher.
- `commands/showrunner/bevy_upgrade.md` — `stop`, `line`, and what a scheduled message means.

**Seats:** 1 writer + 1 tester — the CLI ties the schedule, the changes and the closing rule together.
- `impl` — `schedule.py`, `record.py`, `report.py`, `bevy_upgrade.py`, `commands/showrunner/bevy_upgrade.md`.
- `test` — `test_schedule.py` and the additions to `test_report.py` and `test_cli.py`, from the Spec. The notifier and `send.py` stubs append their arguments to a file the test reads. For CLI tests of `check`, `bevy_upgrade.py` reads `BEVY_UPGRADE_FIXTURES`: when set, the fetcher serves each URL from that JSON file of URL to body and fails on any other URL.

**Constraints from prior phases:** `run_check(record, pins, fetch, now)` updates the record and never raises for a failed lookup. `report.state` returns all seven states and `STATE_ORDER` orders them. `Entry.items` holds `number`, `kind`, `state`, `title`. `Entry.answer` is `None` or holds `answer`, `at`, `words`. The stale rule is the table's: `checked` older than eight hours, or any entry with `error`. Every CLI verb re-reads pins before it prints.

**Acceptance gate:** the Delegation Context **Test** and **Lint** commands pass. The tests cover: the exact `new` command and its quoting for a doc path with a space; `next_check` read from a stub's `next_due=` line and `None` for exit 1; a scheduled check with no change sends nothing; with a change it sends once and a second run sends nothing; with an empty address `announced` stays and the next run sends; `status` clears pending changes; `line` leaves them; all entries `done` closes the watch and removes the job; `stop`; `check` refused on a closed record. `bash -n` is not needed (no shell file changes).

### Phase 5 — The dailies carries the upgrade as a topic read from the record  · status: todo

#### Work Order

**Goal:** Every dailies of a production with an open watch shows one `Bevy upgrade` topic whose text is the record's line, and it needs the user only while a git pin waits for their answer.

**Spec:**
Work only in the worktree and branch the Delegation Context names. This phase edits three files owned by other units (the showrunner allowed it; name them in the checkpoint notice as `also touches`).

`bevy_upgrade.py`: `line --json` prints one JSON object, `{"update": "<the line sentence>", "needs_user": <true when an entry is git ready with no answer>}`, or nothing when there is no open record.

`scripts/production/dailies_input.py`, directly after the review watch block (the `report("review watch", …)` call near line 861):

- Run `"<python>" <script> --production <production doc> line --json`, where `<script>` is `DAILIES_BEVY_UPGRADE` when set, else `scripts/bevy_upgrade/bevy_upgrade.py` beside this repo's `scripts/production/`. Timeout 30 s.
- Exit 0 with a JSON object: append `{"title": "Bevy upgrade", "update": <update>, "eta": "no ETA stated yet", "needs_user": <needs_user>}` to `topics_out`, then `report("bevy upgrade", "ok", <update>)`.
- Exit 0 with no output: no topic; `report("bevy upgrade", "ok", "no open watch")`.
- Any other outcome (non-zero exit, timeout, output that is not that object): append the topic with update `the record could not be read: <first stderr line or the reason>` and `needs_user` false, and `report("bevy upgrade", "failed", <the same text>)`. The dailies itself never fails because of this topic.

`commands/showrunner/dailies.md`, in **Gather**, after the Review watch item: one item saying the input builder adds the `Bevy upgrade` topic from the record while a watch is open, and that nothing else in the report states the upgrade's status.

**Files:**
- `scripts/bevy_upgrade/bevy_upgrade.py` — `line --json`.
- `scripts/bevy_upgrade/test_cli.py` — `line --json` with and without an unanswered git pin, and with no record.
- `scripts/production/dailies_input.py` — the topic, after the review watch block.
- `scripts/production/test_dailies_input.py` — the topic present, absent and failed, through a stub script.
- `commands/showrunner/dailies.md` — the Gather item.

**Seats:** 2 writers — the split is by directory, and each writer tests its own side.
- `impl` — `scripts/bevy_upgrade/bevy_upgrade.py`, `scripts/bevy_upgrade/test_cli.py`.
- `test` opens as impl — `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/dailies.md`. It codes against the JSON object above and a stub script set through `DAILIES_BEVY_UPGRADE`, the way `watch_stubs` in `test_dailies_input.py` replaces `review_regime.py`.

**Constraints from prior phases:** `bevy_upgrade.py line` prints one sentence for an open record and nothing otherwise, and never changes `announced`. `report.line(record, zone, now, next_due)` builds that sentence. A `git ready` entry's `answer` is `None` until the user approves or declines.

**Acceptance gate:** `python3 -m unittest discover -s scripts/bevy_upgrade -p 'test_*.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'` pass. `basedpyright scripts/bevy_upgrade scripts/production/dailies_input.py scripts/production/test_dailies_input.py` prints `0 errors, 0 warnings, 0 notes`. The dailies tests cover the topic with `needs_user` true and false, no topic for empty output, and the failed form for a non-zero exit, with the report still built. Before the checkpoint notice, trial-merge the owner's tip per the production contract (item 9) and run the dailies tests.

## Close-out for the showrunner

Production-level steps no phase can run, because they act in another production's session or on its state:

- After the scheduling phase is on main, the hana showrunner runs `/showrunner:bevy_upgrade start 0.20`, then `approve bevy_enhanced_input` with the user's words from **Source**, then `note` for what no lookup finds: bevy_tween pull request 97's CI fails only in its `bevy_tween_lookup_curve` sub-crate, which pins bevy_lookup_curve 0.12, and issue 91 asks for a crates.io release.
- The hana showrunner then removes the hand-written watch: `zsh ~/.claude/scripts/message/notifier.sh remove bevy020-watch` and the directory `~/.local/state/bevy020-watch/`.
- The hand-written watch listed five crates. Hana's lockfile has nine third-party crates on Bevy and one patched Bevy crate; `bevy_egui`, `bevy_time_runner`, `moonshine-save` and `moonshine-util` were not watched.
