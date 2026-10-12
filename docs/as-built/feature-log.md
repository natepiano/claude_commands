# Feature log

## What it is

The feature log is the current account of what six projects do: hana, natedev, cargo-liner, bevy_brp, obsidian_knife and nateroids. It keeps one Markdown note per user-visible result in the hanadocs vault. Git already records the order of the work; the log records what exists now, so a reader or an agent learns a project's behavior without reconstructing it from commits. A note is `live` while its statement is true and `superseded` once a later result replaces it; superseded notes stay as evidence and as release history. One script, `scripts/feature_log/feature_log.py`, writes every note, validates every field and refuses a bad call. Three close-out steps call it once a result is accepted, and a digest lists new entries on request. For a published crate the log renders a Keep a Changelog section, compares it with the hand-written one, and stamps the released version onto the entries that shipped.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/feature_log/feature_log.py` | The only writer of feature notes. The CLI, plus the importable `PROJECTS`, `vault_dir`, `state_dir` and `locked`. |
| `scripts/feature_log/inventory.py` | Read-only inventory of backfill evidence. Owns the frozen ledgers. |
| `scripts/feature_log/test_feature_log.py` | 90 `unittest` cases on a temp vault and temp state. |
| `scripts/feature_log/test_inventory.py` | 31 cases on temp git repositories. |
| `commands/feature_log.md` | `/feature_log digest [--since]` and `/feature_log live <project> [filters]`. Reads only. |
| `commands/showrunner/produce.md` | `<ChangeReviewFeatureLog/>` inside `<MergeCheckpoint/>`, and `<CloseChangeReview/>`: the Hana close-out. |
| `commands/validate_and_push.md` | `<FeatureLogCloseOut/>` and the `Feature log:` summary line. |
| `commands/release.md` | The compare step in `<FinalizeOnBase/>` and `<FinalizeOnHotfixBranch/>`; the stamp step in `<PublishPhases/>`. |
| `/etc/nixos/README.md` | Per-change loop step 6: the natedev close-out. Outside this repository. |
| `pyrightconfig.json` | An `executionEnvironments` entry for `scripts/feature_log`. |
| `/home/natepiano/rust/hanadocs/feature-log/<project>/` | The notes. |
| `/home/natepiano/rust/hanadocs/feature-log.base`, `feature-log - <project>.base` | The seven Bases. |
| `~/.local/state/claude/feature-log/` | State: lock, events, proposals, digest cursor, ledgers, saved release contents. |
| `/home/natepiano/rust/hanadocs/proposals/feature-log.md` | The design this implements. Read-only for agents. |

Every call is `~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py <verb> …`. Both scripts use the Python standard library only.

### Projects and crates

`PROJECTS: dict[str, ProjectSpec]` with `ProjectSpec(repo: str, crates: frozenset[str], published: frozenset[str])` is the one table of valid projects, crates and published crates.

| Project | Repo | Crates | Published |
| --- | --- | --- | --- |
| `hana` | `natepiano/hana` | 27 package names from `~/rust/hana` and `~/rust/hana_catalyst` | `hana_clerestory`, `hana_kana`, `hana_lagrange`, `hana_liminal`, `hana_rigging`, `hana_rubric` |
| `natedev` | `natepiano/nixos` | none | none |
| `cargo-liner` | `natepiano/cargo-liner` | `cargo-berth`, `cargo-berth-test-support`, `cargo-handler`, `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane` | `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane` |
| `bevy_brp` | `natepiano/bevy_brp` | `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros`, `bevy_brp_test_apps`, `test-app-a`, `test-app-b` | `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros` |
| `obsidian_knife` | `natepiano/obsidian_knife` | `obsidian_knife` | none |
| `nateroids` | `natepiano/nateroids` | `nateroids` | none |

The folder is the repository boundary; `crate` narrows ownership. `published` holds crates confirmed on crates.io on 2026-10-10. A crate that may publish but has not (`cargo-berth`, `cargo-handler`, `obsidian_knife`, `nateroids`) is not in it. A first publication or a new workspace member is an edit to `PROJECTS`.

### Vault layout

The vault of record is natedev's `/home/natepiano/rust/hanadocs`. It holds six folders, `feature-log/{hana,natedev,cargo-liner,bevy_brp,obsidian_knife,nateroids}/`, named exactly as the project keys, and seven Bases at the vault root. A note's path is `feature-log/<project>/<title>.md`. The vault is a git repository that agents never commit.

| Base | Views |
| --- | --- |
| `feature-log.base` | **Latest**, **By project**, **By crate**, **By kind** (live notes; the three "By" views use `groupBy: {property: note.<prop>, direction: ASC}`), **Superseded** |
| `feature-log - <project>.base` (six) | **Current** and **Superseded**, each with `project.contains(link("<project>"))` |

Every view is a table sorted by `completed_date` descending. Every filter starts with `file.hasTag("feature-log")`, then `state == "live"` or `state == "superseded"`. Live views show `file.name`, `summary`, `kind`, `crate`, `completed_date`, `project`, `area`, `link`, `production`, `unit`. Superseded views end with `replaced_by` and `release` in place of the last two.

### Note format

```markdown
---
area: "[[widgets]]"
completed_date: "[[2026-10-09]]"
crate:
  - hana
  - hana_catalyst
  - hana_diegetic
date_created: "[[2026-10-10]]"
date_modified: "[[2026-10-10]]"
kind: feature
link: https://github.com/natepiano/hana/commit/afb0f5e992069a3e2d948bfff093292eff8f41a4
production: tool-based-ui
project: "[[hana]]"
release: []
replaced_by: []
return:
  - "[[feature-log - hana.base]]"
  - "[[feature-log.base]]"
state: live
summary: A number row shows the live value arriving through its cable.
tags: [feature-log]
unit:
  - organon
---
<behavior>

## See it

<check>

## Evidence

- <one item per line>
```

| Field | Form | Source |
| --- | --- | --- |
| `area` | `"[[topic]]"`; optional | `--area` |
| `completed_date` | `"[[YYYY-MM-DD]]"` | `--completed`, default `today()` |
| `crate` | sorted package names; `[]` only for natedev, at least one elsewhere | `--crate` |
| `date_created`, `date_modified` | date links | `today()`; `date_modified` moves on every rewrite |
| `kind` | `feature`, `fix`, `change`, `perf`, `deprecation`, `removal`, `security` | `--kind` |
| `link` | `https://github.com/<repo>/commit/<40-hex, lowercase>` | `--link`, a 40-hex sha or that URL |
| `production` | string; Hana only; optional | `--production` |
| `project` | `"[[<project>]]"` | `--project` |
| `release` | sorted `crate@Unreleased` or `crate@version`; the crate is in `crate` and published | default, or `--release` |
| `replaced_by` | `[]` while live; links to replacements when superseded | `supersede` |
| `return` | project Base, then global Base | derived |
| `state` | `live` or `superseded` | derived |
| `summary` | one line, no leading `- `; for a published crate it reads unchanged as a changelog bullet | `--summary` |
| `tags` | `[feature-log]` | derived |
| `unit` | sorted unit slugs; Hana only; key absent when empty | `--unit` |

- The file name is the title; no `title` key exists. `normalize_title` lowercases, turns `-` into a space, drops every character outside `[a-z0-9 _]`, collapses spaces, and refuses an empty title or one over 50 characters.
- `render_note` writes keys in ascending order, `[]` for an empty list, sorted block items otherwise, and links and dates as `"[[x]]"`. A string is bare only when YAML reads it back unchanged (`SAFE_SCALAR_RE`), else `json.dumps`.
- `parse_note` is a line parser. It accepts only `FRONTMATTER_KEYS`, refuses a duplicate key, accepts single-quoted scalars and inline lists, and refuses a bare scalar YAML would change (`UNSAFE_BARE_SCALAR_PREFIXES`, `: `, ` #`, a trailing `:`).
- `behavior` and `see` may not contain a `## See it` or `## Evidence` line. Evidence is at least one single-line item.
- `Note` (a `TypedDict`) is the frontmatter boundary type: `area` and `production` are `str | None`, mirroring whether the key is present.
- `validate_note` is shared by every write and by `check`.

### State directory

`state_dir()` is `$XDG_STATE_HOME/claude/feature-log`, else `~/.local/state/claude/feature-log`. `vault_dir()` is `$FEATURE_LOG_VAULT`, else `~/rust/hanadocs`. Both read the environment on each call.

| Path | Content | Writer |
| --- | --- | --- |
| `lock` | `fcntl.flock` target | `locked()` |
| `events.jsonl` | one `FeatureLogEvent` per line, append-only | every writing verb |
| `digest.json` | `DigestCursor {"line": int, "at": str}` | `digest` without `--since` |
| `proposals/<ROW>.json` | one `FeatureLogProposal` | `propose`, `add`/`supersede --review`, `drop` |
| `backfill/<project>.jsonl` | the frozen ledger | `inventory.py` |
| `release/<crate>.json` | `ReviewedReleaseContents {"crate": str, "titles": list[str]}`, on the host that ran the comparison | `changelog --compare --save`; the path comes from `commands/release.md` |

### `feature_log.py`

```python
main(
    argv: list[str],
    ssh_runner: SshRunner = _run_ssh,
    forwarded_reader: ForwardedReader = _read_forwarded_selection,
    release_forwarder: ReleaseForwarder = _forward_release,
) -> int
```

Exits: `0` success, `unchanged` included. `1` when `check` finds errors or `changelog --compare` finds differences. `2` for every refusal: one stderr line `feature_log: <reason>`, no stdout, nothing written; argparse errors take the same path. A forwarded verb returns the ssh status.

Every write holds `locked()`, an exclusive `flock` on `state_dir()/"lock"`. A note or JSON file is written as `.<name>.tmp` in its target folder and moved with `os.replace`. Each successful write appends one event.

| Verb | Arguments | Result | Prints |
| --- | --- | --- | --- |
| `add` | new-note flags, or `--review ROW` alone | one live note | `added: <path>` or `unchanged: <path>` |
| `supersede` | new-note flags and `--old TITLE` (repeatable, at least one), or `--review ROW` alone | the replacement; each old note becomes `superseded` and gains the replacement in `replaced_by` | `added:` or `unchanged:`, then `superseded: <old> → <new>` per old note |
| `propose` | `--review ROW`, new-note flags, optional `--old TITLE` | every `add`/`supersede` check runs; no note; the proposal is saved | `proposed: <ROW> (<verb> <title>)` |
| `drop` | `--review ROW --verdict TEXT` | an open proposal closes without a note | `dropped: <ROW>` or `unchanged: <ROW>` |
| `amend` | `--project --title`, any of `--summary --behavior --see --area --kind --crate --evidence`; with `--backfill` also `--link` with `--completed`, `--release crate@version`, `--separate-result` | the note corrected in place | `amended: <path> (<fields>)` or `unchanged: <path>` |
| `live` | `--project`, optional `--crate --area --kind --grep` | live notes, newest `completed_date` first | `<completed> <kind> <title> — <summary> [<crates>] (<area>) <link>` |
| `digest` | optional `--since YYYY-MM-DD` | events by project | see Digest |
| `changelog` | `--crate --version`, optional `--compare PATH`, optional `--save FILE` | a section rendered or compared | the section, `match`, or the differences |
| `release` | `--crate --version --contents FILE` | `X@Unreleased` becomes `X@V` | `released X@V (<n> notes)` or `unchanged` |
| `project-of` | optional `PATH`, default `.` | maps `origin` to a project; exit 0 always | the project key or `none` |
| `check` | none | every note in the six folders parsed and validated; every `replaced_by` target exists | `ok: <n> notes`, or one `<path>: <reason>` per error |

New-note flags: `--project --title --kind --summary --behavior --see --link --evidence` are required; `--crate --area --completed --production --unit --release --backfill --separate-result` are optional. `--crate`, `--evidence`, `--unit` and `--release` repeat and take one or more values.

New-note rules:

- Without `--backfill`, each published crate in `--crate` adds `crate@Unreleased`. With `--backfill`, `release` holds only the `--release` values given.
- `VERSION_RE` accepts `Unreleased` or `N.N.N` with optional prerelease identifiers; an empty identifier (`1.2.3-..`) is refused by every writer.
- `--production` and `--unit` are refused outside hana. `--crate` is refused for natedev.
- `live --grep` matches title, summary and behavior, case-insensitive. The ` (<area>)` part is absent when the note has no area.
- `project-of` reads `git -C PATH remote get-url origin` and accepts `git@github.com:<slug>` and `https://github.com/<slug>`, with or without `.git`.

Repeats and the link rule:

- `_same_repeat` compares a repeat with the stored note apart from `date_created` and `date_modified`, and apart from `completed_date` when `--completed` is omitted. Equal prints `unchanged`. Different fields fail with `use amend`. The same title under another link fails with `make the title more specific`.
- `_reject_duplicate_link` allows one live note per link in a project. Another title on that link needs `--separate-result`. `supersede` may reuse the link of its own `--old` notes.
- A repeated `supersede` finishes any `--old` note still live. An `--old` note superseded by something else is refused.
- `amend --evidence` skips items already stored. `amend --crate` drops `release` values of crates that left the list.
- `amend --backfill --link <commit> --completed <date>` replaces both together (`_amend_provenance` returns `KeepAcceptingProvenance` or `CorrectAcceptingProvenance(link, completed)`) and runs the link rule again. `amend --backfill --release crate@version` adds exact versions; `Unreleased`, an unpublished crate, or a crate absent from the note is refused.

### Events

`FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent | ReleaseEvent`, narrowed on `verb`. `at` is an ISO time in America/Los_Angeles, to the second.

| Type | Keys |
| --- | --- |
| `AddEvent` | `at`, `verb`, `project`, `title`, `kind`, `summary`, `release`, `backfill` |
| `SupersedeEvent` | the same, plus `old` |
| `AmendEvent` | `at`, `verb`, `project`, `title`, `fields`, `backfill`, `old_link`, `new_link`, `old_completed`, `new_completed`, `releases_added` |
| `LegacyAmendEvent` | the first six `AmendEvent` keys; read only, for older lines |
| `ReleaseEvent` | `at`, `verb`, `project`, `crate`, `version`, `titles` |

`_parse_event` requires the exact key set for each verb. Write order is note, then event. A repeat whose note already matches appends the event when the log holds none for that verb, project and title, then prints `unchanged`. An amendment that changes link, completed date or releases reverses the order: `_journal_accepting_correction` appends the event, then the note is written; a retry whose event equals the latest amend event for that note, `at` aside, writes the note without a second event.

### Review rows

`FeatureLogProposal = OpenProposal | LandedProposal | DroppedProposal`, discriminated by `status`. Each holds `row` (`^[AC]\d+$`), `verb` (`supersede` when `--old` is given, else `add`), `args: ProposalArgs` (all 17 new-note arguments, `completed` saved resolved) and `at`. `LandedProposal` adds `note`; `DroppedProposal` adds `verdict`.

| Call | Open proposal | Landed | Dropped | None |
| --- | --- | --- | --- | --- |
| `propose --review ROW …` | replaced; `completed` is kept unless `--completed` is passed | refused | refused | saved |
| `add`/`supersede --review ROW` | lands with the saved arguments; prints the write lines, then `[[<title>]]` | `unchanged: [[<note>]]` | refused | `no proposal for <ROW>; run propose first` |
| `drop --review ROW --verdict TEXT` | becomes dropped | refused | `unchanged: <ROW>` | refused |

A verb that differs from the proposal's fails and names the right verb. `propose` also requires every `--old` note to exist and be live.

### Digest

`digest` lists events by project in `PROJECTS` order: `+ <title> — <summary>` for an add, `→ <old> ⇒ <new>` for a supersede, `~ <title> (<fields>)` for an amend, `= X@V (<n> notes)` for a release. Events with `backfill: true` are left out. It ends `<n> changes since <YYYY-MM-DD HH:MM PDT>` or `feature log: nothing new since <…>`. Without `--since` it reads events and the cursor, and writes the advanced cursor, under one `locked()`. The cursor is a line position, so it passes backfill events and lists same-second events once each. `--since` starts at midnight America/Los_Angeles, takes no lock and moves no cursor.

### Changelog export and release stamp

`changelog --crate X --version V` (`V` a version or `Unreleased`; `X` published) selects, under `locked()`, the notes whose `crate` holds `X` and whose `release` holds `X@V`. A released version takes live and superseded notes. `Unreleased` takes live notes only. It prints `## [V]`, then one `### <heading>` per non-empty heading in the order Added, Changed, Deprecated, Removed, Fixed, Security, each followed by `- <summary>` lines sorted by summary.

| Kind | Heading |
| --- | --- |
| `feature` | Added |
| `change`, `perf` | Changed |
| `deprecation` | Deprecated |
| `removal` | Removed |
| `fix` | Fixed |
| `security` | Security |

- `--compare PATH` (only with `--version Unreleased`) reads the CHANGELOG on the calling host and compares bullet sets per heading with its `## [Unreleased]` section. Order and wrapping do not count; the heading does. It prints `match`, or per heading `Only in feature log:` and `Only in changelog:` lists. argparse turns the request into `RenderSection` or `CompareSection`.
- `--save FILE` (only with `--compare`) removes FILE before comparing. On a `match` it writes the selected note titles as `ReviewedReleaseContents`.
- `--selection` is internal: the rendered section and the selected titles as JSON.
- Exits: `0` section or `match`; `1` differences; `2` unpublished crate (stderr `feature_log: crate X is not published`, no stdout), missing section, or failed forwarded read.

`release --crate X --version V --contents FILE` takes an exact version. The calling host reads FILE and checks its keys, its crate, and that titles are sorted, unique and normalized. The vault host then, under one `locked()`:

1. Refuses a named note that does not exist or holds neither `X@Unreleased` nor `X@V`.
2. If a release event for `X@V` exists: prints `unchanged` when every named note carries `X@V`, else refuses `already stamped with different contents`.
3. Replaces `X@Unreleased` with `X@V` in every named note, superseded ones included. Drops `X@Unreleased` from superseded notes the contents do not name. Leaves live unnamed notes pending.
4. Appends one `ReleaseEvent` whose `titles` are the notes it changed.

`/release` wiring: for each published crate being released, the compare step runs `changelog --crate {crate_name} --version Unreleased --compare {crate_dir}/CHANGELOG.md --save ~/.local/state/claude/feature-log/release/{crate_name}.json`, with `{crate_name}` read from `{crate_dir}/Cargo.toml`. The release owner sees `match` or the differences and reconciles with `amend` or a changelog edit. A crate that still differs is reported, not blocked, and has no saved contents. The stamp step runs `release --crate ${PACKAGE_NAME} --version ${VERSION} --contents <saved file>` right after each crate's publish succeeds, only when the saved file exists, never on a dry run. A later failed publish leaves earlier stamps; a retry prints `unchanged`.

### Host forwarding

A call is off host when the verb is not `project-of`, the short host name is not `natedev`, and `FEATURE_LOG_VAULT` is not in the environment.

| Verb off host | What runs |
| --- | --- |
| `project-of` | locally |
| `changelog` | arguments checked locally; one forwarded read, `changelog --crate X --version V --selection`, through `forwarded_reader`, returning `ChangelogSelection(section, titles)` or `ForwardFailed(exit, stderr)`; render, compare and save happen locally |
| `release` | contents read and checked locally; `release --crate X --version V --contents-stdin` forwarded through `release_forwarder` with the contents JSON on stdin |
| every other verb | `ssh natedev "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py " + shlex.join(argv)` through `ssh_runner` |

### Close-out paths

| Project | Step | Runs when | Outcome |
| --- | --- | --- | --- |
| hana | `<ChangeReviewFeatureLog/>`, then `<CloseChangeReview/>` | `merge_checkpoint.py` accepts a merge in `~/rust/hana` or `~/rust/hana_catalyst`; later, the user's change-review verdict | `propose` at merge; `add`/`supersede --review` on `works` or `fine`; `drop` on any other verdict |
| cargo-liner, bevy_brp, obsidian_knife, nateroids | `<FeatureLogCloseOut/>` in `/validate_and_push` | the commit is on the default branch and all watched CI is green; also after `push_pr_branch_and_merge.sh` | `add`, `supersede`, or `none — <reason>` in the summary's `Feature log:` line |
| natedev | `/etc/nixos/README.md` step 6 | `rebuild` succeeded and the activated revision (last line of `~/.local/state/rebuild/generations.log`, without `-dirty`) is on `origin/main` | `add --project natedev`, `supersede`, or `feature log: none — <reason>` |

- An entry exists for a result when a user or operator can state what is now possible, now works, changed, went away or became noticeably faster. Refactor, test, build, CI and docs-only work gets none; it may be evidence for an entry.
- Before writing, the agent reads `live` for affected entries. Still true: leave it. Stale wording or scope: `amend`. Replaced: `supersede --old`.
- Hana: each distinct result gets one `A<n>` or `C<n>` row under `## To review` in `~/rust/hanadocs/change-review.md` and one `propose --review <row>` with `--project hana`, every owning crate, the merge sha as `--link`, `--evidence "Showrunner merge <merge-sha>, from checkpoint <checkpoint-sha>."`, `--production`, every `--unit`, and one `--old` per replaced entry. Internal-only work logs `feature log: none — <reason>` in the production log. The showrunner keeps `change-review.md` by hand; no script edits it. A script failure leaves the row open.
- `<FeatureLogCloseOut/>` starts with `project-of .` and prints nothing for `hana`, `natedev` or `none`.

### `inventory.py` and the frozen ledgers

`main(argv: list[str], sources: SourceTable = SOURCES) -> int`. It reads each repository at a frozen branch tip and keeps one ledger per project at `state_dir()/"backfill"/<project>.jsonl`: one JSON object per line with sorted keys, written under `locked()` through a temp file, `fsync` and `os.replace`. It never forwards; it runs on natedev, where the ledgers and repositories are.

```python
RecordKind = Literal["phase", "asbuilt", "changelog", "tag", "merge", "commit"]
Pool = Literal["structured", "commits"]
ClaimState = Unclaimed | ClaimedBy          # ClaimedBy carries `seat`
ReviewOutcome = Undisposed | NoteOutcome | NoNoteOutcome
# NoteOutcome: "represented" | "combined", with `note`
# NoNoteOutcome: "internal" | "not-current" | "unresolved", with `reason`

class BackfillSourceRecord(TypedDict):
    id: str      # <project>:<kind>:<key>
    project: str
    repo: str    # absolute path
    ref: str     # 40-hex frozen tip
    kind: RecordKind
    pool: Pool
    title: str
    date: str    # YYYY-MM-DD
    source: str  # <path>:<line>, or a sha
    claim: ClaimState
    outcome: ReviewOutcome
```

`SOURCES: SourceTable = dict[str, ProjectSources]`; `ProjectSources(sources, proposal_counts)`; `RepositorySource = DocumentSource | ChangelogSource | TagSource | HistorySource`; `HistorySource.history` is `EntireHistory()` or `AfterRef(excluded_ref)`. `proposal_counts` holds the design's measured figures, printed beside the frozen counts.

| Kind | One record per | Key |
| --- | --- | --- |
| `phase` | `#### As-built` block outside fenced code in a `docs/**/*.md`, titled by the nearest `### Phase …` heading above it | `<path>#<heading>` |
| `asbuilt` | `.md` file under `docs/` whose parent directory is `as-built` and that holds no such block, titled by its first heading | `<path>` |
| `changelog` | top-level `- ` line under a `## [<version>]` heading, `[Unreleased]` included | `<path>@<version>#<n>` |
| `tag` | tag merged into the tip; source is the peeled sha | tag name |
| `merge` | `git log --first-parent --merges` | sha |
| `commit` | `git log --no-merges` | sha |

Hana reads documents at `~/rust/hana_catalyst` `init/catalyst`, every merge of `~/rust/hana` `main`, and the `init/catalyst` merges after `origin/main`. natedev's tag sits in the `commits` pool. Changelog globs match component by component (`crates/*/CHANGELOG.md` for cargo-liner; three named files for bevy_brp). A file record's date is the last commit touching the file at the tip. A merge or commit sha seen in two sources keeps its first record; any other duplicate id refuses `collect`.

| Verb | Behavior |
| --- | --- |
| `collect --project P` | With a ledger present: reads it and prints. Otherwise collects outside the lock and writes only if the ledger is still absent. Prints each pool and kind beside the design's figure, and one `ref: <repo> <sha>` line per frozen tip. |
| `next --project P --pool POOL [--kind K] [--path PREFIX…] [--claim NAME] [--limit N]` | Undisposed records, unclaimed or claimed by NAME, oldest first, as `<id> \| <date> \| <kind> \| <title> \| <source>`. `--path` matches a prefix of the source path; a sha source matches none. `--claim` stores `ClaimedBy(NAME)`. `--limit` defaults to 25; `0` lists all. |
| `show ID` | The record as one JSON line, then its source text at the frozen ref: the block, the bullet under its version heading, the whole file, or `git show --stat`. |
| `mark ID… [--ids-from FILE] [--seat NAME] --as D (--note TITLE \| --reason TEXT)` | `represented` and `combined` need `--note` naming an existing note file; `internal`, `not-current` and `unresolved` need `--reason`. A record claimed by another name is refused. Every ID is checked before any write. Re-marking replaces the outcome. |
| `status --project P [--pool POOL]` | Counts per outcome, then the notes the ledger names. |

Exits: `0` success; `1` from `status` while any selected record is undisposed; `2` for every refusal, with `inventory: <reason>` on stderr.

Rules the backfill followed, which bind any correction:

- The frozen ref decides which records exist. The audit ref, the tip of the accepted branch (hana: `hana_catalyst` `init/catalyst`; the others `main`), decides what is true now.
- `represented`: the record has its own note. `combined`: another note covers it. `internal`: no user-visible result. `not-current`: the behavior is gone at the audit ref. `unresolved`: the sources cannot support a true summary, date and check.
- A note's link is the accepting commit: the earliest first-parent commit on the accepted line that descends from the introducing commit, or that commit itself when it is on the line. A link is valid only when `git rev-list --first-parent <audit ref>` contains it. The script checks only the repo and the 40-hex form.
- A historical `crate@version` is set only when exactly one existing changelog bullet maps to the note. No old changelog is rewritten.
- A commit with several results has one note per result (`--separate-result`); its record is `represented` into one, and the siblings cite the commit in Evidence.
- A correction amends the existing note (`amend --backfill`), never writes a second one.

### Tests

`python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'`. `test_feature_log.py` pins the two example notes byte for byte, every refusal leaving vault and events unchanged with no temp file, repeats, an interrupted supersede, amend corrections and retries, exact event keys, proposals, the digest cursor and lock, every changelog kind, release stamping, and forwarding through injected callables. `test_inventory.py` pins each record kind, an excluded range, non-ASCII paths, a repeat `collect` leaving the ledger byte-identical, claims, path prefixes, `mark` refusals, `status` exits, and that no command changes a repository.

## Invariants

- `feature_log.py` is the only writer of notes under `feature-log/`. No note is created or edited by hand; a mistake is repaired with `amend` or `supersede`.
- No feature template, Git hook, Claude settings hook, post-push hook or rebuild hook writes or triggers an entry. The three close-out steps are the only triggers. `rebuild` and `update` never call the script.
- No path sends a message per proposal or entry. `digest`, on request, is the only report.
- One vault: natedev's. Off natedev nothing touches a local vault unless `FEATURE_LOG_VAULT` is set.
- Project repositories are evidence: read at a recorded ref, never edited. `inventory.py` runs git with `GIT_OPTIONAL_LOCKS=0`.
- `PROJECTS`, `vault_dir()`, `state_dir()` and `locked()` are the import surface. No other script reads a path constant. Every write to notes, events, proposals, the cursor and ledgers holds `locked()`.
- A refusal writes nothing. Repeating a writing call adds no second note and no second event.
- One live note per link per project unless `--separate-result`. A live note has `replaced_by: []`; a superseded note names a replacement that exists.
- `--link`, `--completed` and `--release` on `amend` require `--backfill`; link and date travel together.
- `events.jsonl` is append-only, and lines already on disk stay readable: a new key needs a reader that still accepts the old key set, as `LegacyAmendEvent` does.
- The changelog emits summaries unchanged. A released section keeps superseded notes. An unpublished crate gets no output.
- `release` stamps only the notes the saved contents name, only after a successful publish, never on a dry run. Only a `match` leaves saved contents. A local path is never forwarded.
- The crate's `CHANGELOG.md` stays the published record. The log never rewrites a changelog and never blocks a release.
- A ledger is frozen: `collect` never rewrites one, and only `claim` and `outcome` change. A record holds a note or a reason, never both.
- A note is true at the audit ref. Backfill invents no result.
- Tests never touch the live vault or state: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, and no test runs `ssh`.
- Python: no `Any`, a `TypedDict` per JSON layout, no file-level ignores. Both scripts stay importable on Python 3.10.
- Times shown to the user are America/Los_Angeles.

## Calibration and gotchas

Numbers:

- Title limit 50 characters after normalization. `next --limit` default 25. 13 published crates. 27 hana package names.
- 8,623 ledger records produced 717 notes.
- The frozen counts exceed the design's figures by work landed after it was measured: hana +3 phase records and +7 `hana_catalyst` merges, natedev +3 commits. The design's hana "200 records" are phase records and its "58 files" are as-built files.

The script:

- The scripts run under Python 3.10 on the Mac (`scripts/lib/py`); the tests run only on natedev's 3.13 and use 3.12 names.
- A tool or test run off natedev without `FEATURE_LOG_VAULT` reaches natedev's live vault. An empty `FEATURE_LOG_VAULT` stops forwarding and selects the local `~/rust/hanadocs`.
- A forwarded call runs natedev's deployed `~/.claude` copy, not the caller's checkout.
- Note dates use the vault host's local date (natedev is America/New_York); event times are America/Los_Angeles.
- A kill between a note and its event leaves a note with no event; the next identical call appends it. For link, date and release amendments the order is reversed: the event exists and the note lags until a retry.
- `propose` checks a link against notes already written. Two open proposals for one merge cannot see each other, so the second is refused at landing unless every row carries `--separate-result`.
- `--backfill` only omits the `@Unreleased` default; `add --backfill --release X@Unreleased` is accepted. `amend --release` refuses `Unreleased`, and no verb adds `X@Unreleased` to an existing note.
- `amend` takes no `--production` or `--unit`; they belong to `add` and `supersede`.
- A `live` read can fail once with `missing opening frontmatter delimiter` when it catches a note mid-rewrite while Obsidian runs on natedev. The script's own writes are atomic, and `.<name>.tmp` files are never globbed.
- `obsidian-cli base:query` (natedev, Obsidian 1.14.4, enabled under Settings → General → Advanced) flattens grouped views; a group check reads its JSON output. An unresolved project link such as `[[natedev]]` still matches `project.contains(link("natedev"))`.

Changelog and release:

- A crate whose changelog has hand-written Unreleased bullets and no logged entries never matches, so it is never stamped until those bullets are logged or released.
- The compare joins a wrapped bullet's lines with one space, and folds an indented sub-bullet into its parent. A summary matches only a single top-level bullet with the same text under the same heading.
- The saved contents hold titles only. A note added and superseded between the comparison and the stamp loses `X@Unreleased` at that stamp and is counted in the release event's `titles`. No changelog section differs: it never gets a version.
- The saved contents file lives only on the calling host.

Ledgers and evidence:

- `git ls-tree` without `-z` quotes non-ASCII paths, which then fall out of path filters. `fnmatch` `*` crosses `/`, so a whole-path glob matches nested files.
- A ledger record has one target. A second result of a commit marked into another note or `internal` has no record and no `status` count reports it missing; each commit's diff is read for every result (cargo-liner `47eaf59f`, a perf commit, also fixed the sync-notification startup race).
- Behavior from commit-only work has no structured record; only an audit of the current surface finds it. Changelogs omit CLI verbs, flags and some public API. Results that docs never mention sit in public APIs and registries: `stock_tools()`, derive macros, registration functions.
- A changelog bullet, commit subject or doc comment can overstate behavior; a note describes the code. `bevy_brp_extras` 0.18.1 says `type_text` types one character per frame; the queue presses on one frame and releases on the next. Error claims hold only for the paths the code marks.
- `git log -S` on a bullet's current wording finds the last commit that rewrapped it. cargo-liner's changelog rewrap `a5f4bf0b` (2026-09-16) is the known case: 48 notes link the pre-rewrap match, one links the rewrap, which added that bullet, and seven cargo-port notes link `d9558a79`, a summary rewrite later than the behavior.
- `git log --first-parent --merges --ancestry-path <c>..<audit>` alone returns nothing for work that entered on a merge's second parent (a subtree import, a workspace consolidation). An introducing commit can also sit off the first-parent line outside any import.
- A docs-move or docs checkpoint commit can hold a link by mistake; the link is the commit that made the behavior true. A note stating specific values links the first-parent commit that reached them.
- A `See it` command resolves with `command -v`: natedev installs `plasma-review`, not `plasma-diff`. Read a command's source before running it: `agent-sessions-snapshot --help` ignores the flag and regenerates the vault's agent-session pages.
- An app check at an audit ref runs from a detached worktree with `BRP_EXTRAS_PORT=15798 cargo run` and JSON-RPC over curl to `http://127.0.0.1:15798`; one app holds the port at a time.

## Why

- **One note per result, not per commit.** The log is an as-built; a second commit log in the vault would be a weaker copy of git.
- **Script-only writing.** One schema, one validator and one file-name rule make `check`, the Bases and the changelog mechanical. The agent closest to the work decides meaning; the script owns every mechanical choice.
- **Close-out steps, no hooks.** A hook cannot tell whether a result matters, and some run before CI settles. A rebuild must stay short and must not depend on the vault.
- **Log at acceptance, not at release.** Hana's acceptance is the user's change-review verdict, so `propose` holds the entry until the row closes.
- **Silent writes, digest on request.** A message per entry is noise.
- **One vault with forwarding.** Obsidian has natedev's vault open; the Mac clone syncs only through git and lacks `change-review.md`.
- **Own frontmatter parser and writer.** No YAML library is installed under `scripts/lib/py`. The output follows Obsidian Linter's form (sorted keys and arrays, compact lists, date links), so a save in the editor leaves a note valid.
- **Folder, title and link identify a note.** No generated id exists; the link rule is the duplicate check.
- **Events are separate from notes.** The digest and event repair need history the notes do not hold. An accepting correction writes its event first because the event carries the old link and date, which exist nowhere else once the note is rewritten.
- **`Unreleased` reads live notes only; a released version reads all.** A later replacement must not erase an older release's notes, and a pending entry replaced before release never shipped.
- **Compare bullet sets, save titles, stamp after publish.** Hand-written changelogs differ in order and wrapping. Publishing happens after the comparison, so the stamp covers what was compared and leaves later work pending. `--save` removes the file first so an aborted earlier release never makes a crate eligible.
- **A differing crate is reported, not blocked.** The crate changelog is the published record.
- **Frozen ledgers.** Counts and dispositions cannot shift under a re-collect. Collection runs outside the lock because it is slow and the lock serializes every note write.
- **Private by default.** Only what `inventory.py` imports is public; nothing else has a consumer.

Ruled out:

- Semantic types for `Note.area` and `Note.production`: `Note` mirrors key presence at the frontmatter boundary.
- A recovery journal or operation id for the note-without-event gap: a repeat that appends the missing event is simpler.
- `file.inFolder("feature-log/<project>")` filters in the project Bases: the link filter follows the issue Bases and matches unresolved project links.
- A separate verification pass per backfill batch: `next --path` lets two writers claim disjoint prefixes, and the audit against the current surface covers both.
- Exempting a date-only correction from the duplicate-link refusal: such a correction passes `--separate-result` again.
- A separate nateroids note for inspector-first Escape: "trigger game and camera commands through brp" already describes it.
- Storing the rendered section with the saved titles and refusing drift before the stamp: an amend a minute after stamping drifts the same way, so the check protects nothing lasting.
- Recording the comparison boundary in the saved contents to date a later supersede: both rules print the same sections, and the note never gets a version under either.
- A `status`, `name` or id field, and an `internal` or `docs` kind: `state`, the file name and the link already carry those facts, and internal work gets no entry.

## State of the data on 2026-10-12

Measured on natedev. `feature_log.py check` prints `ok: 726 notes`: the 717 the backfill wrote, plus 9 hana entries landed through change review since.

| Project | Note files | Live | Ledger records | represented | combined | internal | not-current | Frozen ref | Audit ref |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hana | 161 (152 from backfill) | 161 | 677 (261 structured, 416 merges) | 149 | 376 | 148 | 4 | hana `ce51498`, hana_catalyst `2408021` | hana_catalyst `bcd669367` |
| natedev | 121 | 121 | 978 | 106 | 182 | 674 | 16 | `/etc/nixos` `9d99e9e` | same |
| cargo-liner | 302 | 298 | 3,043 (811 structured, 2,232 commits) | 286 | 1,353 | 1,362 | 42 | `9301628` | same |
| bevy_brp | 73 | 73 | 2,804 (234 structured, 2,570 commits) | 70 | 364 | 2,305 | 65 | `e65064e` | `49089a51` |
| obsidian_knife | 28 | 28 | 564 | 27 | 104 | 408 | 25 | `5df4349` | same |
| nateroids | 41 | 41 | 557 (2 structured, 555 commits) | 39 | 154 | 354 | 10 | `c7d6a51` | not recorded |

- Every ledger is fully disposed: `inventory.py status --project <p>` exits 0 for all six, with 0 `unresolved`.
- The backfill is complete for all six projects: each was audited against its current surface.
- Every backfilled hana note links a commit on `git rev-list --first-parent bcd669367`. 48 cargo-mend notes link cargo-mend's graft commit, and its 2026-08-30 work carries 2026-09-03. bevy_brp's pre-workspace history is accepted by its subtree imports `c0839f61` (`extras/`) and `344e15aa` (`mcp/`), both 2025-06-27.
- Historical release values are exact versions only: cargo-liner 91 values on 90 notes, bevy_brp 66 values on 51 notes. natedev, obsidian_knife and nateroids carry none.
- One note is pending: hana "palette toggle rows show on or off" holds `hana_rubric@Unreleased`.
- `events.jsonl` holds 1,447 events: 715 backfill adds, 4 backfill supersedes, 719 backfill amends and 9 live adds, all hana. No release event exists, so no crate is stamped, and natedev holds no saved release contents and no `digest.json`.
- `proposals/` holds rows `C17` to `C49`: 22 open, 9 landed, 2 dropped.

Changelog comparison for the 13 published crates:

| Crate | Result |
| --- | --- |
| `hana_kana`, `hana_lagrange`, `hana_liminal`, `cargo-mend`, `cargo-port`, `bevy_brp_extras`, `bevy_brp_mcp_macros` | `match`, with nothing on either side |
| `hana_rubric` | differs: 1 logged entry its changelog lacks |
| `cargo-tile` | differs: 163 hand-written bullets with no logged entries |
| `hana_clerestory` | differs: 31 hand-written bullets |
| `tui_pane` | differs: 23 hand-written bullets |
| `hana_rigging` | differs: 5 hand-written bullets |
| `bevy_brp_mcp` | differs: 1 hand-written bullet |

Until a crate's entries are logged, `changelog` prints only `## [Unreleased]` for it and `--compare` reports every hand-written bullet as a difference.
