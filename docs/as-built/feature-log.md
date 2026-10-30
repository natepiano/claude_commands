# Feature log

## What it is

The feature log is the current account of what seven projects do: hana, natedev, cargo-liner, bevy_brp, obsidian_knife, nateroids and claude (this repository, `~/.claude`). It keeps one Markdown note per user-visible result in the hanadocs vault. Git already records the order of the work; the log records what exists now, so a reader or an agent learns a project's behavior without reconstructing it from commits. A note is `live` while its statement is true and `superseded` once a later result replaces it; superseded notes stay as evidence and as release history. One script, `scripts/feature_log/feature_log.py`, writes every note, validates every field and refuses a bad call. Three close-out steps call it once a result is accepted, and a digest lists new entries on request. For a published crate the log renders a Keep a Changelog section, compares it with the hand-written one, and stamps the released version onto the entries that shipped.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/feature_log/feature_log.py` | The only writer of feature notes. The CLI, plus the importable `PROJECTS`, `vault_dir`, `state_dir` and `locked`. |
| `scripts/feature_log/inventory.py` | Read-only inventory of backfill evidence. Owns the frozen ledgers. |
| `scripts/feature_log/test_feature_log.py` | 124 `unittest` cases on a temp vault, temp state and temp git checkouts. |
| `scripts/feature_log/test_inventory.py` | 32 cases on temp git repositories. |
| `commands/feature_log.md` | `/feature_log digest [--since]` and `/feature_log live <project> [filters]`. Reads only. |
| `commands/showrunner/produce.md` | `<ChangeReviewFeatureLog/>` inside `<MergeCheckpoint/>`, and `<CloseChangeReview/>`: the hana and claude close-out. |
| `commands/validate_and_push.md` | `<FeatureLogCloseOut/>` and the `Feature log:` summary line. |
| `commands/release.md` | The compare step in `<FinalizeOnBase/>` and `<FinalizeOnHotfixBranch/>`; the stamp step in `<PublishPhases/>`. |
| `/etc/nixos/README.md` | Per-change loop step 6: the natedev close-out. Outside this repository. |
| `pyrightconfig.json` | An `executionEnvironments` entry for `scripts/feature_log`. |
| `/home/natepiano/rust/hanadocs/feature-log/<project>/` | The notes. |
| `/home/natepiano/rust/hanadocs/home-base/feature-log.base`, `feature-log - <project>.base` | The Bases: the global one and one per project. |
| `~/.local/state/claude/feature-log/` | State: lock, events, proposals, digest cursor, ledgers, saved release contents. |
| `~/rust/hanadocs/issues/feature log.md` | The design this implements. Read-only for agents. |

Every call is `~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py <verb> …`. Both scripts use the Python standard library only.

### Projects and crates

`PROJECTS: dict[str, ProjectSpec]` with `ProjectSpec(repo: str, crates: frozenset[str], published: frozenset[str], checkouts: tuple[Path, ...])` is the one table of valid projects, crates, published crates and local checkouts.

| Project | Repo | Crates | Published | Checkouts |
| --- | --- | --- | --- | --- |
| `hana` | `natepiano/hana` | 27 package names from `~/rust/hana` and `~/rust/hana_catalyst` | `hana_clerestory`, `hana_kana`, `hana_lagrange`, `hana_liminal`, `hana_rigging`, `hana_rubric` | `~/rust/hana`, `~/rust/hana_catalyst` |
| `natedev` | `natepiano/nixos` | none | none | `/etc/nixos` |
| `cargo-liner` | `natepiano/cargo-liner` | `cargo-berth`, `cargo-berth-test-support`, `cargo-handler`, `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane` | `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane` | `~/rust/cargo-liner` |
| `bevy_brp` | `natepiano/bevy_brp` | `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros`, `bevy_brp_test_apps`, `test-app-a`, `test-app-b` | `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros` | `~/rust/bevy_brp` |
| `obsidian_knife` | `natepiano/obsidian_knife` | `obsidian_knife` | none | `~/rust/obsidian_knife` |
| `nateroids` | `natepiano/nateroids` | `nateroids` | none | `~/rust/nateroids` |
| `claude` | `natepiano/claude_commands` | none | none | `~/.claude` |

The folder is the repository boundary; `crate` narrows ownership. A project with no crates (natedev, claude) takes none. `published` holds crates confirmed on crates.io on 2026-10-10. A crate that may publish but has not (`cargo-berth`, `cargo-handler`, `obsidian_knife`, `nateroids`) is not in it. A first publication or a new workspace member is an edit to `PROJECTS`. `checkouts` lists the project's local clones on natedev, in lookup order; a new note reads its default completed date from them (see Note format). Every write runs on natedev, a forwarded one included, so only natedev's paths count.

### Vault layout

The vault of record is natedev's `/home/natepiano/rust/hanadocs`. It holds one folder per project, `feature-log/{hana,natedev,cargo-liner,bevy_brp,obsidian_knife,nateroids,claude}/`, named exactly as the project keys, and the Bases at the vault root. A write creates a missing folder; `check` and `live` treat one as empty. The vault owner adds a new project's Base, `feature-log - claude.base` included. A note's path is `feature-log/<project>/<title>.md`. The vault is a git repository. Agents never commit their hand edits in it; a script commits the files it writes, and only those (the `hanadocs-commit.py` jobs in `/etc/nixos/modules/linux/`, and `change_review.py` for a change-review list).

| Base | Views |
| --- | --- |
| `feature-log.base` | **Latest**, **By project**, **By crate**, **By kind** (live notes; the three "By" views use `groupBy: {property: note.<prop>, direction: ASC}`), **Superseded** |
| `feature-log - <project>.base` (one per project) | **Current** and **Superseded**, each with `project.contains(link("<project>"))` |

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
| `completed_date` | `"[[YYYY-MM-DD]]"` | `--completed`, default the linked commit's committer date in the user's zone (`_commit_date`) |
| `crate` | sorted package names; `[]` only for natedev and claude, at least one elsewhere | `--crate` |
| `date_created`, `date_modified` | date links | `today()`; `date_modified` moves on every rewrite |
| `kind` | `feature`, `fix`, `change`, `perf`, `deprecation`, `removal`, `security` | `--kind` |
| `link` | `https://github.com/<repo>/commit/<40-hex, lowercase>` | `--link`, a 40-hex sha or that URL |
| `production` | string; optional; the production whose change review accepted the note | `--production` |
| `project` | `"[[<project>]]"` | `--project` |
| `release` | sorted `crate@Unreleased` or `crate@version`; the crate is in `crate` and published; no crate has both `@Unreleased` and a version | default, or `--release` |
| `replaced_by` | `[]` while live; links to replacements when superseded | `supersede` |
| `return` | project Base, then global Base | derived |
| `state` | `live` or `superseded` | derived |
| `summary` | one line, no leading `- `; for a published crate it reads unchanged as a changelog bullet | `--summary` |
| `tags` | `[feature-log]` | derived |
| `unit` | sorted unit slugs; key absent when empty | `--unit` |

- The file name is the title; no `title` key exists. `normalize_title` lowercases, turns `-` into a space, drops every character outside `[a-z0-9 _]`, collapses spaces, and refuses an empty title or one over 50 characters.
- `render_note` writes keys in ascending order, `[]` for an empty list, sorted block items otherwise, and links and dates as `"[[x]]"`. A string is bare only when YAML reads it back unchanged (`SAFE_SCALAR_RE`), else `json.dumps`.
- `parse_note` is a line parser. It accepts only `FRONTMATTER_KEYS`, refuses a duplicate key, accepts single-quoted scalars and inline lists, and refuses a bare scalar YAML would change (`UNSAFE_BARE_SCALAR_PREFIXES`, `: `, ` #`, a trailing `:`).
- `behavior` and `see` may not contain a `## See it` or `## Evidence` line. Evidence is at least one single-line item.
- `Note` (a `TypedDict`) is the frontmatter boundary type: `area` and `production` are `str | None`, mirroring whether the key is present.
- `validate_note` is shared by every write and by `check`.
- `_commit_date(project: ProjectSpec, sha: str) -> str` gives the default `completed_date`. It runs `git -C <checkout> show -s --format=%cI <sha>`, with no shell, in each of the project's `checkouts` in order. The first exit 0 gives the committer date (for a merge commit, the merge date), converted to `user_zone()` as `YYYY-MM-DD`. A missing checkout, a non-zero exit or an `OSError` is a miss, and the next checkout is tried. A given `--completed` runs no git.

### State directory

`state_dir()` is `$XDG_STATE_HOME/claude/feature-log`, else `~/.local/state/claude/feature-log`. `vault_dir()` is `$FEATURE_LOG_VAULT`, else `~/rust/hanadocs`. Both read the environment on each call.

| Path | Content | Writer |
| --- | --- | --- |
| `lock` | `fcntl.flock` target | `locked()` |
| `events.jsonl` | one `FeatureLogEvent` per line, append-only | every writing verb |
| `digest.json` | `DigestCursor {"line": int, "at": str}` | `digest` without `--since` |
| `proposals/<ROW>.json`, `proposals/<list>/<ROW>.json` | one `FeatureLogProposal` | `propose`, `add`/`supersede --review`, `drop` |
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

Every write holds `locked()`, an exclusive `flock` on `state_dir()/"lock"`. A note or JSON file uses `scripts.lib.atomic_file.write_text_atomic`: a sibling temporary file is flushed and fsynced, then moved with `os.replace`. Each successful write appends one event.

| Verb | Arguments | Result | Prints |
| --- | --- | --- | --- |
| `add` | new-note flags, or `--review ROW` alone | one live note | `added: <path>` or `unchanged: <path>` |
| `supersede` | new-note flags and `--old TITLE` (repeatable, at least one), optional `--split`, or `--review ROW` alone | the replacement; each old note becomes `superseded` and gains the replacement in `replaced_by` | `added:` or `unchanged:`, then `superseded: <old> → <new>` per old note it changes |
| `propose` | `--review ROW`, new-note flags, optional `--old TITLE` | every `add`/`supersede` check runs; no note; the proposal is saved | `proposed: <ROW> (<verb> <title>)` |
| `drop` | `--review ROW --verdict TEXT` | an open proposal closes without a note | `dropped: <ROW>` or `unchanged: <ROW>` |
| `amend` | `--project --title`, any of `--summary --behavior --see --area --kind --crate --evidence`; with `--backfill` also `--link` with `--completed`, `--release crate@version` or `crate@Unreleased`, `--separate-result` | the note corrected in place | `amended: <path> (<fields>)` or `unchanged: <path>` |
| `live` | `--project`, optional `--crate --area --kind --grep` | live notes, newest `completed_date` first | `<completed> <kind> <title> — <summary> [<crates>] (<area>) <link>` |
| `digest` | optional `--since YYYY-MM-DD` | events by project | see Digest |
| `changelog` | `--crate --version`, optional `--compare PATH`, optional `--save FILE` | a section rendered or compared | the section, `match`, or the differences; a comparison can end with a `not compared:` line |
| `release` | `--crate --version --contents FILE` | `X@Unreleased` becomes `X@V` | `released X@V (<n> notes)` or `unchanged` |
| `project-of` | optional `PATH`, default `.` | maps `origin` to a project; exit 0 always | the project key or `none` |
| `check` | none | every note in every project folder parsed and validated; every `replaced_by` target exists | `ok: <n> notes`, or one `<path>: <reason>` per error |

New-note flags: `--project --title --kind --summary --behavior --see --link --evidence` are required; `--crate --area --completed --production --unit --release --backfill --separate-result` are optional. `--crate`, `--evidence`, `--unit` and `--release` repeat and take one or more values.

New-note rules:

- `_new_note` builds the note for `add`, `supersede` and `propose`. An omitted `--completed` takes `_commit_date` of the canonical link's sha, not the logging date. `date_created` and `date_modified` are `today()`. When no checkout holds the commit, the call is refused: `feature_log: cannot date commit <sha>: no checkout of <repo> holds it; pass --completed`. When git prints a date that does not parse: `feature_log: cannot date commit <sha>: invalid committer date in <checkout>; pass --completed`. Both refusals come before any write.
- Without `--backfill`, each published crate in `--crate` adds `crate@Unreleased`, so the new entry is pending for that crate and an exact `--release` version of it in the same call is refused: `feature_log: release cannot hold both X@Unreleased and X@<version>`, where `X@Unreleased` is the default. With `--backfill`, `release` holds only the `--release` values given; a released version on a new entry takes `--backfill`.
- `VERSION_RE` accepts `Unreleased` or `N.N.N` with optional prerelease identifiers; an empty identifier (`1.2.3-..`) is refused by every writer.
- `--crate` is refused for natedev and claude: `feature_log: <project> does not accept crates`.
- `live --grep` matches title, summary and behavior, case-insensitive. The ` (<area>)` part is absent when the note has no area.
- `project-of` reads `git -C PATH remote get-url origin` and accepts `git@github.com:<slug>` and `https://github.com/<slug>`, with or without `.git`.

Repeats and the link rule:

- `_same_repeat` compares a repeat with the stored note apart from `date_created` and `date_modified`, and apart from `completed_date` when `--completed` is omitted. Equal prints `unchanged`. Different fields fail with `use amend`. The same title under another link fails with `make the title more specific`.
- `_reject_duplicate_link` allows one live note per link in a project. Another title on that link needs `--separate-result`. `supersede` may reuse the link of its own `--old` notes.
- A repeated `supersede` finishes any `--old` note still live. An `--old` note superseded by something else is refused with `old note <title> is not live`.
- `supersede --split` lifts that refusal: an `--old` note that is already superseded gains the new note in `replaced_by` and stays superseded, and the call prints `superseded: <old> → <new>`. On a live `--old` note the flag changes nothing. A repeat prints `unchanged:` for the new note and adds no event. Replacements of one note that share a link pass `--separate-result`, because the link rule exempts only the `--old` notes. `--split` with `--review` is refused (`--split cannot be combined with --review`), and `propose` does not take the flag. argparse turns it into `ReplacementPolicy = ReplaceLiveNotes | ExtendReplacementSet`.
- `amend --evidence` skips items already stored. `amend --crate` drops `release` values of crates that left the list.
- `amend --backfill --link <commit> --completed <date>` takes no commit-date default: both flags are given and replace both together (`_amend_provenance` returns `KeepAcceptingProvenance` or `CorrectAcceptingProvenance(link, completed)`) and runs the link rule again.
- `amend --backfill --release` adds `crate@version` and `crate@Unreleased` values; an unpublished crate or a crate absent from the note is refused. `X@Unreleased` marks a live note pending for X. The same call takes `--summary` and `--kind`, so a backfilled note becomes the pending entry of its changelog bullet in one write. A value already stored prints `unchanged`.

Release refusals, each checked before any write:

| Belongs to | Case | stderr |
| --- | --- | --- |
| every release list | the list holds `X@Unreleased` and an exact version of X | `feature_log: release cannot hold both X@Unreleased and X@<version>` |
| the pending mark, `amend --release X@Unreleased` | the note is superseded | `feature_log: a superseded note cannot be pending` |
| the pending mark, `amend --release X@Unreleased` | the note's stored `release` holds an exact version of X | `feature_log: <title> already holds X@<version>` |
| an exact version, `amend --release X@<version>` | the note's stored `release` holds `X@Unreleased` | `feature_log: <title> already holds X@Unreleased` |

- `_validate_release_values` checks every release list, so the first row reaches every writer: the `--release` values and `@Unreleased` defaults of `add`, `supersede` and `propose`, the saved arguments of a proposal landing through `--review`, and the `--release` values of one `amend` call. The old note of a refused `supersede` stays live.
- That row is checked after each value on its own, so an invalid value, an unpublished crate or a crate missing from `--crate` is reported first. `<version>` is the first exact version of X in the list's sorted order, which is string order.
- `validate_note` runs the same function on a stored note. `check` reports a note that holds both as `feature-log/<project>/<title>.md: release cannot hold both X@Unreleased and X@<version>` and exits 1.
- A refused `amend` journals no event and applies no other flag of the call.
- Accepted: `X@Unreleased` beside an exact version of another crate, in one call and in one note; a second exact version of X on a note that holds only exact versions of X. A pending entry gets its version from the `release` verb, which replaces `X@Unreleased` with `X@V` in one write.

### Events

`FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent | ReleaseEvent`, narrowed on `verb`. `at` is an ISO time with its offset, written in the user's zone, to the second.

| Type | Keys |
| --- | --- |
| `AddEvent` | `at`, `verb`, `project`, `title`, `kind`, `summary`, `release`, `backfill` |
| `SupersedeEvent` | the same, plus `old` |
| `AmendEvent` | `at`, `verb`, `project`, `title`, `fields`, `backfill`, `old_link`, `new_link`, `old_completed`, `new_completed`, `releases_added` |
| `LegacyAmendEvent` | the first six `AmendEvent` keys; read only, for older lines |
| `ReleaseEvent` | `at`, `verb`, `project`, `crate`, `version`, `titles` |

`_parse_event` requires the exact key set for each verb. Write order is note, then event. A repeat whose note already matches appends the event when the log holds none for that verb, project and title, then prints `unchanged`. An amendment that changes link, completed date or releases reverses the order: `_journal_accepting_correction` appends the event, then the note is written; a retry whose event equals the latest amend event for that note, `at` aside, writes the note without a second event. A pending mark is such an amendment: its event has `backfill: true` and names `X@Unreleased` in `releases_added`. A split adds no key: each call appends one `SupersedeEvent` whose `old` holds that call's `--old` titles.

### Review rows

`FeatureLogProposal = OpenProposal | LandedProposal | DroppedProposal`, discriminated by `status`. Each holds `row`, `verb` (`supersede` when `--old` is given, else `add`), `args: ProposalArgs` (all 17 new-note arguments, `completed` saved resolved: the given `--completed`, else the commit date `propose` read) and `at`. `LandedProposal` adds `note`; `DroppedProposal` adds `verdict`.

| Call | Open proposal | Landed | Dropped | None |
| --- | --- | --- | --- | --- |
| `propose --review ROW …` | replaced; `completed` is kept unless `--completed` is passed | refused | refused | saved |
| `add`/`supersede --review ROW` | lands with the saved arguments, the saved `completed` included; prints the write lines, then `[[<title>]]` | `unchanged: [[<note>]]` | refused | `no proposal for <ROW>; run propose first` |
| `drop --review ROW --verdict TEXT` | becomes dropped | refused | `unchanged: <ROW>` | refused |

A row is `A<n>` or `C<n>`, bare for a row of hana's list `~/rust/hanadocs/change-review/change-review - hana.md`, else qualified by its list: `<list>/C<n>`, where `<list>` is the showrunner `~/rust/hanadocs/change-review/change-review - <list>.md` is named for (`natedev/C3`). Every list numbers its rows from 1, so only the qualifier keeps two lists' rows apart; a qualified row is stored at `proposals/<list>/<ROW>.json`. `propose` refuses a bare row for any project but hana: `feature_log: a claude review row names its list: <list>/C3`. `add`, `supersede` and `drop` take the row as `propose` saved it.

A verb that differs from the proposal's fails and names the right verb. `propose` also requires every `--old` note to exist and be live.

### Digest

`digest` lists events by project in `PROJECTS` order: `+ <title> — <summary>` for an add, `→ <old> ⇒ <new>` for a supersede, `~ <title> (<fields>)` for an amend, `= X@V (<n> notes)` for a release. Events with `backfill: true` are left out. It ends `<n> changes since <YYYY-MM-DD HH:MM PDT>` or `feature log: nothing new since <…>`. Without `--since` it reads events and the cursor, and writes the advanced cursor, under one `locked()`. The cursor is a line position, so it passes backfill events and lists same-second events once each. `--since` starts at midnight in the user's zone, takes no lock and moves no cursor.

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
- The comparison covers the six headings of `CHANGELOG_HEADINGS` and the bullets under no heading, which print as `Uncategorized`. Bullets under any other `###` heading of the hand-written section are not compared and are not a difference. When such bullets exist, the last stdout line is `not compared: <heading> (<n> bullets)`, `(1 bullet)` for one, with several headings sorted and joined by `, `. It follows `match` or the differences on either host and changes neither the exit status nor `--save`.
- `--save FILE` (only with `--compare`) removes FILE before comparing. On a `match` it writes the selected note titles as `ReviewedReleaseContents`.
- `--selection` is internal: the rendered section and the selected titles as JSON.
- Exits: `0` section or `match`; `1` differences; `2` unpublished crate (stderr `feature_log: crate X is not published`, no stdout), missing section, or failed forwarded read.

`release --crate X --version V --contents FILE` takes an exact version. The calling host reads FILE and checks its keys, its crate, and that titles are sorted, unique and normalized. The vault host then, under one `locked()`:

1. Refuses a named note that does not exist or holds neither `X@Unreleased` nor `X@V`.
2. If a release event for `X@V` exists: prints `unchanged` when every named note carries `X@V`, else refuses `already stamped with different contents`.
3. Replaces `X@Unreleased` with `X@V` in every named note, superseded ones included. Drops `X@Unreleased` from superseded notes the contents do not name. Leaves live unnamed notes pending.
4. Appends one `ReleaseEvent` whose `titles` are the notes it changed.

`/release` wiring: for each published crate being released, the compare step runs `changelog --crate {crate_name} --version Unreleased --compare {crate_dir}/CHANGELOG.md --save ~/.local/state/claude/feature-log/release/{crate_name}.json`, with `{crate_name}` read from `{crate_dir}/Cargo.toml`. The release owner sees `match` or the differences and reconciles with `amend` or a changelog edit; an existing entry becomes pending with `amend --backfill --release {crate_name}@Unreleased`, plus `--summary` when its wording differs from the bullet. A `not compared:` line lists hand-written headings outside the six and never prevents a `match`. A crate that still differs is reported, not blocked, and has no saved contents. The stamp step runs `release --crate ${PACKAGE_NAME} --version ${VERSION} --contents <saved file>` right after each crate's publish succeeds, only when the saved file exists, never on a dry run. A later failed publish leaves earlier stamps; a retry prints `unchanged`.

A hand-written `## [Unreleased]` bullet under one of the six headings is logged as exactly one live pending entry: its summary is the bullet's text, its kind maps to the bullet's heading, and its `release` holds `<crate>@Unreleased`. The entry stays pending until the crate's release stamps it.

- A bullet no entry describes gets a new one: `add --backfill --release <crate>@Unreleased`.
- A live note that describes that one bullet is amended in place (`amend --backfill --release <crate>@Unreleased --summary … --kind …`) and keeps its title, link and evidence.
- A broad note that covers several bullets is split: each bullet's entry is written with `supersede --backfill --split --old <broad title>`, so the broad note ends superseded with every replacement in `replaced_by`.
- A statement of the broad note that no bullet carries goes into a split remainder, a replacement with no release. `live` lists it and no release section does; it gains a release only when a later changelog bullet names its behavior.
- A bullet the code contradicts gets no entry with that text. The crate's owner corrects the changelog, and the entry carries the corrected wording.
- Prose under a heading outside the six, such as cargo-tile's `### Notes`, is not an entry. A true statement there goes into the behavior text of the entry it describes.

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
| claude | the same two steps | `merge_checkpoint.py` accepts a merge in a `~/.claude` worktree (build-followups); later, the user's change-review verdict | the same, with rows qualified by the production's list: `propose --review build-followups/C<n> --project claude`, then `add --review build-followups/C<n>` or `drop --review build-followups/C<n> --verdict …` |
| cargo-liner, bevy_brp, obsidian_knife, nateroids | `<FeatureLogCloseOut/>` in `/validate_and_push` | the commit is on the default branch and all watched CI is green; also after `push_pr_branch_and_merge.sh` | `add`, `supersede`, or `none — <reason>` in the summary's `Feature log:` line |
| natedev | `/etc/nixos/README.md` step 6 | `rebuild` succeeded and the activated revision (last line of `~/.local/state/rebuild/generations.log`, without `-dirty`) is on `origin/main` | `add --project natedev`, `supersede`, or `feature log: none — <reason>` |

- An entry exists for a result when a user or operator can state what is now possible, now works, changed, went away or became noticeably faster. Refactor, test, build, CI and docs-only work gets none; it may be evidence for an entry.
- Before writing, the agent reads `live` for affected entries. Still true: leave it. Stale wording or scope: `amend`. Replaced: `supersede --old`.
- Hana: each distinct result gets one `A<n>` or `C<n>` row under `## To review` in `~/rust/hanadocs/change-review/change-review - hana.md` and one `propose --review <row>` with `--project hana`, every owning crate, the merge sha as `--link`, `--evidence "Showrunner merge <merge-sha>, from checkpoint <checkpoint-sha>."`, `--production`, every `--unit`, and one `--old` per replaced entry. Internal-only work logs `feature log: none — <reason>` in the production log. A script failure leaves the row open.
- claude: the same, with no `--crate`, the merge sha as `--link` (a `natepiano/claude_commands` commit), and each row qualified by its list, `<list>/C<n>`.
- `<FeatureLogCloseOut/>` starts with `project-of .` and prints nothing for `hana`, `natedev`, `claude` or `none`.

### `inventory.py` and the frozen ledgers

`main(argv: list[str], sources: SourceTable = SOURCES) -> int`. It reads each repository at a frozen branch tip and keeps one ledger per project at `state_dir()/"backfill"/<project>.jsonl`: one JSON object per line with sorted keys, written under `locked()` through a temp file, `fsync` and `os.replace`. It never forwards; it runs on natedev, where the ledgers and repositories are.

```python
RecordKind = Literal["phase", "asbuilt", "changelog", "tag", "merge", "commit"]
Pool = Literal["structured", "commits"]
ClaimState = Unclaimed | ClaimedBy          # ClaimedBy carries `worker`
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
| `mark ID… [--ids-from FILE] [--worker NAME] --as D (--note TITLE \| --reason TEXT)` | `represented` and `combined` need `--note` naming an existing note file; `internal`, `not-current` and `unresolved` need `--reason`. A record claimed by another name is refused. Every ID is checked before any write. Re-marking replaces the outcome. |
| `status --project P [--pool POOL]` | Counts per outcome, then the notes the ledger names. |

Exits: `0` success; `1` from `status` while any selected record is undisposed; `2` for every refusal, with `inventory: <reason>` on stderr.

Rules the backfill followed, which bind any correction:

- The frozen ref decides which records exist. The audit ref, the tip of the accepted branch (hana: `hana_catalyst` `init/catalyst`; the others `main`), decides what is true now.
- `represented`: the record has its own note. `combined`: another note covers it. `internal`: no user-visible result. `not-current`: the behavior is gone at the audit ref. `unresolved`: the sources cannot support a true summary, date and check.
- A note's link is the accepting commit: the earliest first-parent commit on the accepted line that descends from the introducing commit, or that commit itself when it is on the line. A link is valid only when `git rev-list --first-parent <audit ref>` contains it. The script checks only the repo and the 40-hex form.
- A historical `crate@version` is set only when exactly one existing changelog bullet maps to the note. No old changelog is rewritten.
- A commit with several results has one note per result (`--separate-result`); its record is `represented` into one, and the siblings cite the commit in Evidence.
- A correction amends the existing note (`amend --backfill`), never writes a second one. A broad note that several pending changelog bullets describe is the one exception: it is split, as Changelog export and release stamp describes.

### Tests

`python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'`. `test_feature_log.py` pins the two example notes byte for byte, every refusal leaving vault and events unchanged with no temp file, repeats, an interrupted supersede, splits, amend corrections and retries, the pending mark and its refusals, the pending-or-released rule in `add`, `supersede`, `amend` and `check`, the commit-date default (zone conversion, an explicit date that runs no git, the second checkout, a refusal with no writes for each new-note writer, `supersede`, and `propose` through landing), exact event keys, proposals, list-qualified review rows for claude beside a bare hana row of the same number, the digest cursor and lock, every changelog kind, the headings a comparison covers, release stamping, and forwarding through injected callables. `temporary_checkout` builds temp git repositories with fixed committer dates, `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` set to `/dev/null`; `setUp` points every project's `checkouts` at one shared temp checkout. `test_inventory.py` pins each record kind, an excluded range, non-ASCII paths, a repeat `collect` leaving the ledger byte-identical, claims, path prefixes, `mark` refusals, `status` exits, and that no command changes a repository.

## Invariants

- `feature_log.py` is the only writer of notes under `feature-log/`. No note is created or edited by hand; a mistake is repaired with `amend` or `supersede`.
- No feature template, Git hook, Claude settings hook, post-push hook or rebuild hook writes or triggers an entry. The three close-out steps are the only triggers. `rebuild` and `update` never call the script.
- No path sends a message per proposal or entry. `digest`, on request, is the only report.
- One vault: natedev's. Off natedev nothing touches a local vault unless `FEATURE_LOG_VAULT` is set.
- Project repositories are evidence: read at a recorded ref, never edited. `inventory.py` runs git with `GIT_OPTIONAL_LOCKS=0`.
- `PROJECTS`, `vault_dir()`, `state_dir()` and `locked()` are the import surface. No other script reads a path constant. Every write to notes, events, proposals, the cursor and ledgers holds `locked()`.
- A refusal writes nothing. Repeating a writing call adds no second note and no second event.
- One live note per link per project unless `--separate-result`. A live note has `replaced_by: []`; a superseded note names at least one replacement, and every note it names exists.
- A split loses nothing: every sentence of the old note is in a replacement or in a remainder that holds no release.
- `--link`, `--completed` and `--release` on `amend` require `--backfill`; link and date travel together.
- A new note's `completed_date` is the given `--completed` or the linked commit's committer date. It never falls back to the logging date: a commit no checkout holds is refused.
- `events.jsonl` is append-only, and lines already on disk stay readable: a new key needs a reader that still accepts the old key set, as `LegacyAmendEvent` does.
- The changelog emits summaries unchanged. A released section keeps superseded notes. An unpublished crate gets no output.
- A note holds a crate as pending or as released, never both. Every write refuses a release list that holds `X@Unreleased` and an exact version of X, `amend` refuses either one onto a note that stores the other, and `check` reports a stored note that holds both.
- One live pending entry per changelog bullet: no bullet is carried by two live entries of a crate, and no entry carries two bullets.
- One entry is pending for two crates only when both changelogs hold the same bullet under the same heading.
- `release` stamps only the notes the saved contents name, only after a successful publish, never on a dry run. Only a `match` leaves saved contents. A local path is never forwarded.
- The crate's `CHANGELOG.md` stays the published record. The log never rewrites a changelog and never blocks a release.
- A ledger is frozen: `collect` never rewrites one, and only `claim` and `outcome` change. A record holds a note or a reason, never both.
- A note is true at the audit ref. Backfill invents no result.
- Tests never touch the live vault, the live state or a real checkout: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, `checkouts` point at temp git repositories, and no test runs `ssh` or reaches the network.
- Python: no `Any`, a `TypedDict` per JSON layout, no file-level ignores. Both scripts stay importable on Python 3.10.
- Times shown to the user are in the user's zone (`config/user_zone.conf`, read through `scripts/lib/user_zone.py`).

## Calibration and gotchas

Numbers:

- Title limit 50 characters after normalization. `next --limit` default 25. 13 published crates. 27 hana package names.
- 8,623 ledger records produced 717 notes.
- The frozen counts exceed the design's figures by work landed after it was measured: hana +3 phase records and +7 `hana_catalyst` merges, natedev +3 commits. The design's hana "200 records" are phase records and its "58 files" are as-built files.

The script:

- The scripts run under Python 3.11 or later on the Mac (`scripts/lib/py`); the tests run only on natedev's 3.13 and use 3.12 names.
- A tool or test run off natedev without `FEATURE_LOG_VAULT` reaches natedev's live vault. An empty `FEATURE_LOG_VAULT` stops forwarding and selects the local `~/rust/hanadocs`.
- A forwarded call runs natedev's deployed `~/.claude` copy, not the caller's checkout.
- `date_created` and `date_modified` use the vault host's local date (natedev is America/New_York); a defaulted `completed_date` and event times are in the user's zone.
- A test that omits `--completed` needs a link to a real commit in a temp checkout; a made-up sha is refused.
- A note is dated from natedev's checkouts. A commit not yet fetched into any of them is refused until it is fetched or `--completed` is given.
- A kill between a note and its event leaves a note with no event; the next identical call appends it. For link, date and release amendments the order is reversed: the event exists and the note lags until a retry.
- `propose` checks a link against notes already written. Two open proposals for one merge cannot see each other, so the second is refused at landing unless every row carries `--separate-result`.
- `--backfill` only omits the `@Unreleased` default; `add`, `supersede` and `amend` each accept `--backfill --release X@Unreleased`.
- `amend` takes no `--production` or `--unit`; they belong to `add` and `supersede`.
- A `live` read can fail once with `missing opening frontmatter delimiter` when it catches a note mid-rewrite while Obsidian runs on natedev. The script's own writes are atomic, and `.<name>.tmp` files are never globbed.
- `obsidian-cli base:query` (natedev, Obsidian 1.14.4, enabled under Settings → General → Advanced) flattens grouped views; a group check reads its JSON output. An unresolved project link such as `[[natedev]]` still matches `project.contains(link("natedev"))`.

Changelog and release:

- A crate whose changelog has hand-written Unreleased bullets and no logged entries never matches, so it is never stamped until those bullets are logged or released.
- The compare joins a wrapped bullet's lines with one space, and folds an indented sub-bullet into its parent. A summary matches only a single top-level bullet with the same text under the same heading.
- `_section_bullets` keeps the bullets of every heading. The loop in `_run_changelog` picks the six and the empty heading, and the `not compared:` line counts the rest.
- A correction sent to a changelog owner carries the entry's summary character for character, copied from the comparison's `Only in feature log:` line. A paraphrase leaves both sides printing.
- A summary can repeat its bullet's overstatement even when the bullet was read against the code. The correction goes into the entry and the changelog together.
- A fix shared by two crates whose changelogs word it differently has one entry per crate: border motion has one for cargo-tile and one for tui_pane.
- A split is checked by reading the old note's sentences against the union of its replacements, remainders included.
- The saved contents hold titles only. A note added and superseded between the comparison and the stamp loses `X@Unreleased` at that stamp and is counted in the release event's `titles`. No changelog section differs: it never gets a version.
- The saved contents file lives only on the calling host.

Ledgers and evidence:

- `git ls-tree` without `-z` quotes non-ASCII paths, which then fall out of path filters. `fnmatch` `*` crosses `/`, so a whole-path glob matches nested files.
- A ledger record has one target. A second result of a commit marked into another note or `internal` has no record and no `status` count reports it missing; each commit's diff is read for every result (cargo-liner `47eaf59f`, a perf commit, also fixed the sync-notification startup race).
- A ledger record can name a superseded note. After a split only the pending bullets' records are re-marked to their entries, so other records of the broad note keep naming it; its `replaced_by` leads to the current entries.
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
- **Log at acceptance, not at release.** For hana and claude acceptance is the user's change-review verdict, so `propose` holds the entry until the row closes. A push to `~/.claude`'s default branch is not acceptance, so `<FeatureLogCloseOut/>` skips claude.
- **A row names its list.** Each production's list numbers from 1, and proposals live in one state directory, so a bare `C17` from a second list would overwrite or be refused by Hana's `C17`. Hana's rows stay bare because its proposals were written that way.
- **Silent writes, digest on request.** A message per entry is noise.
- **One vault with forwarding.** Obsidian has natedev's vault open; the Mac clone syncs only through git and lacks `change-review.md`.
- **Own frontmatter parser and writer.** No YAML library is installed under `scripts/lib/py`. The output follows Obsidian Linter's form (sorted keys and arrays, compact lists, date links), so a save in the editor leaves a note valid.
- **The completed date is the commit's date.** The log records when a result landed, not when an agent logged it. Local git needs no network and no GitHub sign-in. A silent fallback to the logging date is the defect the default replaced, so a commit no checkout holds is refused.
- **Folder, title and link identify a note.** No generated id exists; the link rule is the duplicate check.
- **Events are separate from notes.** The digest and event repair need history the notes do not hold. An accepting correction writes its event first because the event carries the old link and date, which exist nowhere else once the note is rewritten.
- **`Unreleased` reads live notes only; a released version reads all.** A later replacement must not erase an older release's notes, and a pending entry replaced before release never shipped.
- **Compare bullet sets, save titles, stamp after publish.** Hand-written changelogs differ in order and wrapping. Publishing happens after the comparison, so the stamp covers what was compared and leaves later work pending. `--save` removes the file first so an aborted earlier release never makes a crate eligible.
- **Compare only the six change headings.** Keep a Changelog has six headings and each kind maps to one, so no entry can print under another. Prose a crate keeps under a heading of its own is counted in the `not compared:` line and never held against a `match`.
- **One pending entry per changelog bullet.** A summary matches one bullet with the same text, so a broad note that covers several bullets can never match. It is split, because the log never rewrites a changelog.
- **Pending or released, not both.** A released note's summary is that release's bullet. A pending bullet about the same behavior is a later change and gets its own entry. A note that held both would print in two release sections. The rule lives in `_validate_release_values`, the one function that validates every release list, so every writer and `check` apply it with one wording.
- **`--split` is a flag, not the default.** Without it the refusal for an already superseded `--old` note stands, so a live writer cannot replace a note someone else already replaced.
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
- Running builds or the apps to verify a pending changelog bullet: a bullet is verified by reading code and tests at a fixed commit.
- A separate refusal naming `--backfill` for an exact `--release` on a new entry: one rule with one wording covers every release list.
- A `status`, `name` or id field, and an `internal` or `docs` kind: `state`, the file name and the link already carry those facts, and internal work gets no entry.

## State of the data on 2026-10-12

Measured on natedev. `feature_log.py check` prints `ok: 928 notes`: the 717 the backfill wrote, 11 hana entries landed through change review since, and 200 written for the hand-written `## [Unreleased]` bullets of five crates.

| Project | Note files | Live | Ledger records | represented | combined | internal | not-current | Frozen ref | Audit ref |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hana | 200 (152 from backfill, 37 for pending bullets) | 199 | 677 (261 structured, 416 merges) | 149 | 376 | 148 | 4 | hana `ce51498`, hana_catalyst `2408021` | hana_catalyst `bcd669367` |
| natedev | 121 | 121 | 978 | 106 | 182 | 674 | 16 | `/etc/nixos` `9d99e9e` | same |
| cargo-liner | 464 (162 for pending bullets) | 426 | 3,043 (811 structured, 2,232 commits) | 414 | 1,227 | 1,360 | 42 | `9301628` | same |
| bevy_brp | 74 (1 for a pending bullet) | 74 | 2,804 (234 structured, 2,570 commits) | 70 | 364 | 2,305 | 65 | `e65064e` | `49089a51` |
| obsidian_knife | 28 | 28 | 564 | 27 | 104 | 408 | 25 | `5df4349` | same |
| nateroids | 41 | 41 | 557 (2 structured, 555 commits) | 39 | 154 | 354 | 10 | `c7d6a51` | not recorded |

- Every ledger is fully disposed: `inventory.py status --project <p>` exits 0 for all six, with 0 `unresolved`.
- claude joined on 2026-10-15 with no notes, no backfill and no ledger; `inventory.py collect --project claude` refuses with `no source configuration for claude`.
- The backfill is complete for all six projects: each was audited against its current surface.
- Every backfilled hana note links a commit on `git rev-list --first-parent bcd669367`. 48 cargo-mend notes link cargo-mend's graft commit, and its 2026-08-30 work carries 2026-09-03. bevy_brp's pre-workspace history is accepted by its subtree imports `c0839f61` (`extras/`) and `344e15aa` (`mcp/`), both 2025-06-27.
- Historical release values are exact versions only: cargo-liner 91 values on 90 notes, bevy_brp 66 values on 51 notes. natedev, obsidian_knife and nateroids carry none.
- 218 live notes are pending. 217 carry a hand-written changelog bullet as their summary: cargo-tile 157, tui_pane 23, hana_clerestory 31, hana_rigging 5, bevy_brp_mcp 1. The other is hana "palette toggle rows show on or off", which holds `hana_rubric@Unreleased`. No note is pending for two crates.
- Of the 217, 25 are backfilled cargo-liner notes amended in place and 192 are new: cargo-liner 155, hana 36, bevy_brp 1.
- cargo-liner holds 38 superseded notes: 4 the backfill wrote, 33 broad notes split into two to fourteen replacements each, and 1 pending entry replaced by its corrected wording, "identify the backdrop window off the draw thread", which keeps `cargo-tile@Unreleased` until the cargo-tile stamp drops it. hana holds 1, split into two pending entries and a remainder.
- 7 split remainders hold no release: 6 in cargo-liner and 1 in hana.
- The pending entries were checked against code at cargo-liner `243e2cb7`, hana_catalyst `1e893c41` and bevy_brp `49089a51`. 28 bullets were false or overstated there: 19 in cargo-tile and tui_pane, 9 in the hana crates. Their owners corrected all 28 in the changelogs, so no entry carries a false line.
- cargo-tile's `### Notes` heading holds six bullets. Five are in the behavior text of the entries they describe. The sixth was false at the audit ref and is in no entry; its correction belongs to the crate's owner.
- `events.jsonl` holds 1,706 events: 758 backfill adds, 162 backfill supersedes, 775 backfill amends and 11 live adds, all hana. No release event exists, so no crate is stamped, and natedev holds no saved release contents and no `digest.json`.
- `proposals/` holds rows `C17` to `C51`: 21 open, 11 landed, 3 dropped.

Changelog comparison for the 13 published crates:

| Crate | Result |
| --- | --- |
| `hana_kana`, `hana_lagrange`, `hana_liminal`, `cargo-mend`, `cargo-port`, `bevy_brp_extras`, `bevy_brp_mcp_macros` | `match`, with nothing on either side |
| `hana_rubric` | differs: 1 logged entry its changelog lacks |
| `cargo-tile` | `match` on 157 bullets at cargo-liner `afcfc2e9`, then `not compared: Notes (6 bullets)` |
| `tui_pane` | `match` on 23 bullets at cargo-liner `afcfc2e9` |
| `hana_clerestory` | `match` on 31 bullets at hana_catalyst `ac040ff3f` |
| `hana_rigging` | `match` on 5 bullets at hana_catalyst `ac040ff3f` |
| `bevy_brp_mcp` | `match` on 1 bullet at bevy_brp `49089a51` |

The 217 entries stay pending until each crate's release stamps them.

Old claim records convert old worker field names once on their first ledger read. The CLI accepts only `--worker` when marking a claimed record.

### Rerun recovery (2026-10-29)

New amend events carry `content`, the SHA256 of the complete rendered note. Older events stay readable. An unchanged amend checks the newest event for that note against its current content. If the event is missing or holds older content, the retry appends the missing event. Digest prints and flushes the selected changes under the lock before advancing its cursor. A stop during output leaves the changes available on rerun.
