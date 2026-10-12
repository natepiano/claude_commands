# Feature log: five crates' pending changelog bullets are copied into the log

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Copies the hand-written `## [Unreleased]` bullets of cargo-tile, tui_pane, bevy_brp_mcp, hana_clerestory and hana_rigging into the feature log as pending entries, one per bullet, so `/release` compares to `match` and stamps their next version.

> **As-built disposition: amend** — `docs/as-built/feature-log.md`

> **Production: build-followups** — unit `feature-log-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 10:19 PDT

From the user (via the showrunner): yes feature log should copy those notes into the log

The showrunner's assignment, 2026-10-12 10:20 PDT (a peer's coordination, not the user's words): build it today as a new run; one pending entry per bullet, under its heading, with the bullet's text as the summary; where a backfilled entry already describes the same change, keep one entry and make the comparison print `match`; done when all five crates print `match`.

## Delegation Context

- **Project:** `~/.claude` config repo plus the feature log in the hanadocs vault. `docs/as-built/feature-log.md` describes the system this plan extends; read it for the note format, every verb and the backfill rules. Worktree `/home/natepiano/worktrees/claude-build-followups-feature-log-unit`, branch `build-followups-feature-log-unit` (unit `feature-log-unit-unit`); name both in every dispatch.
- **Project started:** 2026-10-12T17:35:38.881+00:00
- **Stack:** Python standard library only; `feature_log.py` and `inventory.py` stay importable on Python 3.10, and the tests run on natedev's `python3` 3.13. No YAML library: the script has its own frontmatter parser and writer. Markdown command files follow `commands/succinct_style.md`.
- **Layout:**
  - `scripts/feature_log/` — `feature_log.py`, `inventory.py`, `test_feature_log.py`, `test_inventory.py`.
  - `commands/release.md` — only the two compare steps (lines 486-488 and 875-877).
  - Vault, natedev: `/home/natepiano/rust/hanadocs/feature-log/{hana,cargo-liner,bevy_brp}/`, written through `feature_log.py` only.
  - State, natedev: `~/.local/state/claude/feature-log/` — `lock`, `events.jsonl`, `backfill/<project>.jsonl` (the frozen ledgers, changed through `inventory.py mark` only).
  - Evidence, read-only (the design the user approved on 2026-10-10 makes project repositories evidence): `~/rust/cargo-liner`, `~/rust/bevy_brp`, `~/rust/hana_catalyst`, `~/rust/hana`.
- **Key files:**
  - `docs/as-built/feature-log.md` — the system as built. Sections used here: "Note format", "`feature_log.py`", "Changelog export and release stamp", "`inventory.py` and the frozen ledgers", "Calibration and gotchas".
  - `scripts/feature_log/feature_log.py` — `_validate_release_values` 730; `_updated_replacement` 1287; `_apply_supersede` 1295-1335 (the old-note check 1316-1321 refuses `old note <title> is not live`); `_run_amend` 1669 (the `Unreleased` refusal 1753-1755); `CHANGELOG_HEADINGS` and `KIND_HEADING` 1830-1839; `_changelog_request` 1849 (`RenderSection | CompareSection`); `_select_changelog` 1870; `_section_bullets` 1890-1929; `_run_changelog` 1932-1990 (the heading loop 1965-1980); `_add_new_note_arguments` 2231; the `supersede` parser 2258-2260; the `amend` parser 2272-2286.
  - `scripts/feature_log/test_feature_log.py` — `FeatureLogTests` 314, 90 cases on a temp vault and temp state.
  - `scripts/feature_log/inventory.py` — `show ID`, `mark ID… --seat NAME --as represented --note TITLE`.
  - `commands/release.md` — 486-488 (`<FinalizeOnBase/>`) and 875-877 (`<FinalizeOnHotfixBranch/>`): the compare command and the sentence "The release owner reconciles with `amend` or changelog edits".
- **Test lanes:** `scripts/feature_log/` → `test_feature_log.py` (beside the code; imports its sibling by bare name). Phase 2 has none.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` from the worktree root.
- **Lint:** `basedpyright scripts/feature_log`: a pass ends `0 errors, 0 warnings, 0 notes` (it exits 3: `pyrightconfig.json` names a `.venv` no checkout has). Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Facts (measured 2026-10-12 on natedev):**
  - Pending bullets, by top-level bullet of `## [Unreleased]`:

    | Crate | Project | Repository and accepted branch | CHANGELOG path | Bullets | Live notes naming the crate |
    | --- | --- | --- | --- | --- | --- |
    | `cargo-tile` | `cargo-liner` | `~/rust/cargo-liner` `main` (`243e2cb7`) | `crates/cargo-tile/CHANGELOG.md` | 163: Added 48, Changed 56, Fixed 53, Notes 6 | 59 |
    | `tui_pane` | `cargo-liner` | same | `crates/tui_pane/CHANGELOG.md` | 23: Added 3, Changed 11, Fixed 9 | 72 |
    | `bevy_brp_mcp` | `bevy_brp` | `~/rust/bevy_brp` `main` (`49089a51`) | `mcp/CHANGELOG.md` | 1: Fixed 1 | 48 |
    | `hana_clerestory` | `hana` | `~/rust/hana_catalyst` `init/catalyst` (`bd1bb0a9`) | `crates/hana_clerestory/CHANGELOG.md` | 31: Added 10, Changed 7, Removed 4, Fixed 10 | 6 |
    | `hana_rigging` | `hana` | same | `crates/hana_rigging/CHANGELOG.md` | 5: Added 2, Changed 3 | 18 |

    217 bullets sit under the six change headings. `~/rust/hana` `main` holds the same two hana sections. No bullet has a sub-bullet; the longest is 2,391 characters. No live note of these crates is pending.
  - The cargo-liner ledger (`~/.local/state/claude/feature-log/backfill/cargo-liner.jsonl`, frozen at `9301628`) holds one `changelog` record per bullet that existed then, id `cargo-liner:changelog:crates/<crate>/CHANGELOG.md@Unreleased#<n>`, `title` the bullet's full text, `outcome.note` the note the backfill chose. cargo-tile: 162 records (combined 119, represented 40, internal 2, not-current 1) naming 54 notes; tui_pane: 18 records (combined 15, represented 3) naming 12 notes. All 180 are claimed by seat `b`. 1 cargo-tile bullet and 5 tui_pane bullets are newer than the ledger. hana and bevy_brp have no such records.
  - Of the 58 notes these records name: 25 are named by exactly one bullet, 33 by two to thirteen; 8 are named by bullets of both crates; none holds a `release` value.
  - cargo-tile's `### Notes` section holds six bullets of prose about the capture shim. The ledger marks the first (`Installing the shim is always explicit, never done on startup…`) `not-current`: startup now installs the shims.
  - Two cargo-tile bullets are `internal` in the ledger (config handling and the tile grid moved into `tui_pane`); both statements are true.
- **Invariants:**
  - `feature_log.py` is the only writer of notes; no note is written or edited by hand (as-built "Invariants").
  - A pending entry's `summary` is its changelog bullet's text, unchanged, and its `kind` maps to the bullet's heading (the showrunner's assignment, on the user's instruction in Source).
  - A note is true at the audit ref; no entry states what the code does not do (as-built "Invariants").
  - Project repositories are never edited. A changelog correction belongs to the crate's release owner (as-built "Invariants": the log never rewrites a changelog).
  - `feature_log.py release` and `changelog --save` never run against the live vault here: the stamp follows a real publish in `/release` (as-built "Invariants").
  - Tests never touch the live vault or state: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, and no test runs `ssh`. Nothing else sets those two variables.
  - Python: no `Any`, a `TypedDict` per JSON layout, no file-level ignores.
  - Seats run no cargo command, no `verify.sh`, no `nixos-rebuild`, `sudo` or `systemctl` change (the unit director's rule for this plan: bullets are checked by reading code and tests, and builds stay free for other sessions).
- **Pending procedure (Phase 2):**
  1. Run the worktree's script, which carries Phase 1's verbs: `python3 scripts/feature_log/feature_log.py …` and `python3 scripts/feature_log/inventory.py …` from the worktree root. The copy under `~/.claude` lacks them until the showrunner merges.
  2. When the phase starts, read each audit ref with `git -C <repo> rev-parse <branch>` and save each changelog at that ref to a temp file (`git -C <repo> show <ref>:<path>`). Never read a working tree: another session may be editing it. Name the refs in the phase report.
  3. The worklist for a crate is what `changelog --crate <crate> --version Unreleased --compare <saved file>` prints under `Only in changelog:`. Each `- ` line is one bullet, with wrapped lines already joined by single spaces: that text is the entry's summary.
  4. Check the bullet against the code and its tests at the audit ref. True: continue. False or overstated: write no entry with that text. Put it on the corrections list (crate, heading, bullet, what the code does, the wording that would be true). Where part of it is true, log that part as a pending entry with a true summary; the comparison then shows both sides until the changelog is corrected.
  5. Find the live entry that describes the bullet's change. An entry describes it when the bullet could stand as that entry's summary; an entry that only mentions the change in passing does not. cargo-liner: the bullet's ledger record names the backfill's choice in `outcome.note` (read the ledger file, or `inventory.py show <id>`). Otherwise `live --project <p> --crate <crate> --grep <word>`.
  6. Write exactly one live pending entry per bullet:
     - **One entry, one bullet** (the entry describes this bullet's change and no other pending bullet's): `amend --backfill --project <p> --title "<title>" --summary "<bullet>" --kind <kind> --release <crate>@Unreleased --evidence "Pending bullet: <path>, Unreleased, <heading>."`.
     - **No entry**: `add --backfill --release <crate>@Unreleased` with every new-note flag.
     - **One entry, several bullets**: one new entry per bullet, each written with `supersede --backfill --split --old "<broad title>" --release <crate>@Unreleased …`, so the broad entry ends superseded by all of them. Before the first, read the broad entry: behavior it states that no pending bullet states gets its own entry the same way, without `--release`. Siblings that share a commit pass `--separate-result`.
     - Never change the summary or kind of a note that holds an exact `crate@version`: its summary is that release's bullet. A pending bullet about it is a later change and gets its own entry.
  7. New-entry fields. `--kind`: Added → `feature`; Changed → `change`, or `perf` when the bullet is about speed; Deprecated → `deprecation`; Removed → `removal`; Fixed → `fix`; Security → `security`. `--title`: new in the project, specific, 50 characters or fewer after normalization. `--crate`: the bullet's crate plus any other crate that owns the behavior. `--release`: `<crate>@Unreleased` only; a second crate is pending only when its changelog holds the same bullet under the same heading. `--behavior`, `--see`, `--evidence`: per the note format; Evidence names the bullet (`<path>, Unreleased, <heading>`) and the commit. `--link` and `--completed`: the accepting commit and its date, by the backfill rules in the as-built: the earliest commit on `git rev-list --first-parent <audit ref>` that descends from the commit that made the behavior true, or that commit when it is on the line. hana links take the form `https://github.com/natepiano/hana/commit/<sha>` with the sha from `~/rust/hana_catalyst`; hana entries pass no `--production` or `--unit`.
  8. Pass every summary as one argv element: from Python `subprocess.run([...])`, or a shell variable filled by a quoted heredoc. A double-quoted shell string runs the backticks most bullets contain.
  9. cargo-liner ledger: after a bullet's entry exists, `inventory.py mark <id> --seat b --as represented --note "<entry title>"` for that bullet's record. Other records stay as they are; a record may keep naming a superseded note.
  10. cargo-tile's `### Notes` bullets are prose, not entries, and Phase 1 leaves that heading out of the comparison. Check each against the code: state a true one in the behavior text of the entry it explains (`amend --behavior`), and put a false one on the corrections list.
  11. Repeat step 3 until the crate prints `match`, or prints only bullets on the corrections list. Post the corrections list on the board as soon as it has an item and put it in the summary.

## Phases

### Phase 1 — An entry can be marked pending or split, and the comparison covers only change headings  · status: done

#### As-built

- **Mark pending.** `amend --backfill --release X@Unreleased` marks a live note pending for X, a published crate in the note's `crate` list (`_validate_release_values`). The same call accepts `--summary` and `--kind`, so a backfilled note becomes a pending entry whose summary is its changelog bullet in one write. The `AmendEvent` carries `backfill: true` and names the value in `releases_added`; it is written before the note. A value already present prints `unchanged` and adds no event. `--release` on `amend` still needs `--backfill`, and exact historical versions behave as before.
- **Refusals, checked before any write** (exit 2, one stderr line `feature_log: <reason>`, nothing written): a superseded note (`a superseded note cannot be pending`); a note whose `release` holds an exact version of X (`<title> already holds X@<version>`); one call naming `X@Unreleased` and an exact version of X (`--release cannot add both X@Unreleased and X@<version>`).
- **Split.** `supersede --split` adds the new note to the `replaced_by` of an `--old` note that is already superseded; the old note stays superseded and the call prints `superseded: <old> → <new>`. With a live `--old` note, `--split` changes nothing. Without `--split` the refusal `old note <title> is not live` stands, so a live writer cannot replace a note someone else already replaced. A repeat prints `unchanged:` for the new note and adds no event. `--split` is refused with `--review`, and `propose` does not take it. The parser turns the flag into `ReplacementPolicy = ReplaceLiveNotes | ExtendReplacementSet`, as `--compare` becomes `RenderSection | CompareSection`; no bool is handed down. `SupersedeEvent` keeps its keys, and `check` still requires every `replaced_by` target to exist.
- **Compare only change headings.** `changelog --compare` compares the six `CHANGELOG_HEADINGS` and bullets under no heading (printed as `Uncategorized`). Bullets under any other named heading of the hand-written section are not compared and are not a difference: Keep a Changelog has six headings and each kind maps to one, so no entry can print under another. When any exist, the last stdout line is `not compared: <heading> (<n> bullets)`, `(1 bullet)` in the singular, several headings joined by `, ` in sorted order, after `match` or the differences. The exit status and `--save` are unchanged, and the off-host path prints the same line.
- **`commands/release.md`**, both compare steps: the release owner reconciles with `amend` or changelog edits, marks an existing entry pending with `amend --backfill --release {crate_name}@Unreleased` (adding `--summary` if the wording differs), and a `not compared:` line lists hand-written headings outside the six and never prevents a `match`.

**Files:**
- `scripts/feature_log/feature_log.py` — `_run_amend`, `_apply_supersede`, `_run_supersede`, the `supersede` parser, `_section_bullets`, `_run_changelog`.
- `scripts/feature_log/test_feature_log.py` — cases for each rule above; every refusal case asserts the vault, `events.jsonl` and temp files are unchanged.
- `commands/release.md` — the two compare steps.

**Binds later work:** The phase titled "The five crates' pending changelog bullets are in the log" uses `amend --backfill --release <crate>@Unreleased` with `--summary` and `--kind` in the same call, `supersede --backfill --split --old <title>`, and the `not compared:` line of `changelog --compare`. One `amend` call that names `<crate>@Unreleased` and an exact version of the same crate is refused.

**Gotchas:**
- `amend` can still add an exact version of X to a note that already holds `X@Unreleased`; only the same-call mix and the stored-version case are refused.
- `_section_bullets` keeps every heading's bullets; the compare loop picks the six and the empty heading.

### Phase 2 — The five crates' pending changelog bullets are in the log  · status: done

#### As-built

- Every true bullet under a change heading in the `## [Unreleased]` sections of cargo-tile (157), tui_pane (23), hana_clerestory (31), hana_rigging (5) and bevy_brp_mcp (1) is the summary of exactly one live pending entry (`release: <crate>@Unreleased`): 217 entries. They stay pending until the crate's release runs `feature_log.py release`, which stamps them. No source file of this repository changed.
- `changelog --crate <crate> --version Unreleased --compare <file>` prints `match` for all five: cargo-tile and tui_pane against cargo-liner `afcfc2e9`, hana_clerestory and hana_rigging against hana_catalyst `ac040ff3f`, bevy_brp_mcp against bevy_brp `49089a51`. cargo-tile's output ends `not compared: Notes (6 bullets)`. `feature_log.py check` prints `ok: 928 notes`; `inventory.py status --project cargo-liner` exits 0 with nothing undisposed or unresolved.
- A backfilled note that covers one bullet is amended in place and keeps its title, link and evidence. A broad note that covers several is superseded by a split into one entry per bullet (`supersede --split`: `state: superseded`, `replaced_by` lists every replacement, and `check` requires each target to exist). cargo-liner, both crates: 25 amended, 155 added, 34 split. hana: 1 amended, 35 added, 1 split into 3. bevy_brp: 1 added.
- A split remainder is an entry with no release that keeps a statement of a split note no pending bullet carries: 7 exist, 6 in cargo-liner and 1 in hana (one of the three replacements of its split note). `live` lists them and no release section does; one gains a release only when a later changelog bullet names its behavior.
- 28 bullets were false or overstated against code at the audit refs (cargo-liner `243e2cb7`, hana_catalyst `1e893c41`, bevy_brp `49089a51`): 19 in cargo-tile and tui_pane, 9 in the hana crates. The crates' owners corrected all 28 in their changelogs, so the comparison refs above hold the corrected text, and no entry carries a false line.
- cargo-tile's five true Notes bullets are in the behavior text of the entries they describe; the sixth was false at the audit ref, is in no entry, and its correction belongs to the crate's owner. The two true cargo-tile bullets the ledger classes `internal` each have an entry of kind `change`.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/`, `…/hana/`, `…/bevy_brp/` — the notes, written only through `feature_log.py`.
- `~/.local/state/claude/feature-log/backfill/cargo-liner.jsonl` — the pending bullets' ledger records, marked `represented` with the entry title through `inventory.py mark`.

**Gotchas:**
- A correction sent to a changelog owner carries the entry's summary character for character, copied from the comparison's `Only in feature log:` line; a paraphrase leaves the pair printing.
- When a broad note is split, every sentence of the old note appears in a replacement or in a no-release remainder; the check is the old note's sentences against the union of its replacements.
- A fix shared by two crates whose changelogs word it differently has one entry per crate (cargo-tile and tui_pane border motion); one entry is pending for two crates only when both changelogs hold the same bullet under the same heading.
- A summary can repeat its bullet's overstatement even when the bullet was read against code; the correction goes into the entry and the changelog together.

**Ruled out:**
- Merging the two border-motion entries into one entry pending for both crates: the crates publish different bullets, and the owner keeps both.
- Running builds or the apps to verify a bullet: a bullet is verified by reading code and tests at a fixed commit.

