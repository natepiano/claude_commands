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

### Phase 1 — An entry can be marked pending or split, and the comparison covers only change headings  · status: todo

#### Work Order

**Goal:** `feature_log.py` can mark an existing live note pending for a published crate, replace one note with several, and compare a hand-written section on the six change headings alone.

**Spec:**

- **Mark pending.** `amend --backfill --release X@Unreleased` is accepted; today `_run_amend` refuses it at 1753-1755 (`--release requires an exact historical version, not Unreleased`). Checked before any write: the note is live (a superseded note is refused: `a superseded note cannot be pending`), and its `release` holds no exact version of X (refused: `<title> already holds X@<version>`). Unchanged: X is published and in the note's `crate` list (`_validate_release_values`); `--release` on `amend` needs `--backfill`; a value already present prints `unchanged`; the `AmendEvent` carries `backfill: true` and names the value in `releases_added`, written before the note as for other release amendments. One call may also pass `--summary` and `--kind`, so a backfilled note becomes a pending entry whose summary is its changelog bullet in a single write. Exact historical versions behave as before.
- **Split.** `supersede` takes `--split` on a direct call only: it is refused with `--review`, and `propose` does not take it. With `--split`, an `--old` note that is already superseded gains the new note in `replaced_by`, stays superseded, and prints `superseded: <old> → <new>` as a live one does; a live `--old` note behaves as today. Without `--split`, today's refusal stands (`old note <title> is not live`): it stops a live writer from replacing a note someone else already replaced. A repeat prints `unchanged:` for the new note, changes nothing and adds no event. `SupersedeEvent` keeps its keys. argparse turns the flag into a two-variant type at the boundary, as `--compare` becomes `RenderSection | CompareSection`; no bool is passed down. `check` is unchanged: every `replaced_by` target must exist.
- **Compare only change headings** (the unit director's call, 2026-10-12: Keep a Changelog has six headings and each kind maps to one, so no entry can ever print under another; cargo-tile's `### Notes` holds six bullets of prose). `changelog --compare` compares the six `CHANGELOG_HEADINGS`, and bullets under no heading (still printed as `Uncategorized`). Bullets under any other named heading of the hand-written section are not compared and are not a difference. When any exist, the last stdout line names them: `not compared: <heading> (<n> bullets)`, `(1 bullet)` in the singular, several headings joined by `, ` in sorted order. It follows `match` or the differences. The exit status and `--save` do not change, and the off-host path prints the same line.
- **`commands/release.md`**, both compare steps (488 and 877), per `commands/succinct_style.md`: the release owner marks an existing entry pending with `amend --backfill --release {crate_name}@Unreleased` (with `--summary` when the wording differs), and a `not compared:` line lists hand-written headings outside the six and never prevents a `match`.

**Files:**
- `scripts/feature_log/feature_log.py` — `_run_amend`, `_apply_supersede` and the `supersede` parser, `_run_changelog`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/release.md` — the two compare steps (no owner in the production; the earlier feature-log run edited the same steps).

**Seats:** `1 writer + 1 tester` — one script and one command file; each rule above is concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`, `commands/release.md`.
- `test` — `scripts/feature_log/test_feature_log.py`: marking a live backfilled note pending; summary, kind and release in one call; the event's keys and `releases_added`; each refusal (superseded note, a note holding `X@1.2.3`, an unpublished crate, a crate the note lacks, no `--backfill`) leaving vault and events unchanged; a repeat printing `unchanged` with no second event; a note marked this way appearing in `changelog --version Unreleased`, matching a changelog that holds its summary, and being stamped by `release`; a note superseded by B, then `supersede --split --old` with C, ending superseded with both in `replaced_by` and C live; the same call without `--split` still refused; a `--split` repeat; `--split` with a live old note; `--split` with `--review` refused; three replacements sharing a link through `--separate-result`; `check` printing `ok` after a split; a `### Notes` heading producing no difference and the `not compared:` line after `match`, in the singular, after differences with exit 1, with `--save` writing its file, and from another host; a bullet under no heading still a difference.

**Constraints from prior phases:** none. From the earlier feature-log run (as-built): every refusal exits 2 with one stderr line `feature_log: <reason>`, no stdout and nothing written; a refusal test asserts the vault, `events.jsonl` and temp files are unchanged; `events.jsonl` lines already on disk stay readable, so no event gains or loses a key here; forwarding is tested through the injected `ssh_runner`, `forwarded_reader` and `release_forwarder`, never `ssh`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` ends `0 errors, 0 warnings, 0 notes`.

### Phase 2 — The five crates' pending changelog bullets are in the log  · status: todo

#### Work Order

**Goal:** every true bullet under a change heading in the `## [Unreleased]` sections of cargo-tile, tui_pane, bevy_brp_mcp, hana_clerestory and hana_rigging is the summary of exactly one live pending entry, and `changelog --compare` prints `match` for each crate.

**Spec:** Delegation Context → Pending procedure, for the five crates in the Facts table. The backfill wrote one note per result, which is coarser than these changelogs: 33 cargo-liner notes cover two to thirteen pending bullets each. Each such note is replaced by one entry per bullet (procedure step 6, third case), so afterwards one change has one live entry and that entry is one bullet. A note that covers a single bullet is amended in place and keeps its title, link and evidence. The two cargo-tile bullets the ledger calls `internal` are true statements the author chose to publish: each gets an entry (`change`). The `not-current` Notes bullet goes on the corrections list. The 8 notes named by bullets of both cargo-tile and tui_pane are split by both seats, each writing its own crate's entries with `--split`; `impl` decides what such a note states beyond the bullets and writes that entry. Nothing here runs `release` or `--save`.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — notes, through `feature_log.py` only.
- `~/.local/state/claude/feature-log/backfill/cargo-liner.jsonl` — the pending bullets' records, through `inventory.py mark` only.

**Seats:** `2 writers` — by crate; the work has no test lane.
- `impl` — cargo-tile: its 157 bullets under change headings and the six Notes bullets.
- `test` — opens as impl; tui_pane (23), hana_clerestory (31), hana_rigging (5), bevy_brp_mcp (1).

**Constraints from prior phases:** Phase 1: `amend --backfill --release X@Unreleased` marks a live note pending and takes `--summary` and `--kind` in the same call; it refuses a superseded note and a note that holds an exact version of X. `supersede --split --old <title>` adds a replacement to a note already superseded. `changelog --compare` leaves named headings outside the six out and lists them on a last `not compared:` line. From the earlier feature-log run (as-built "Calibration and gotchas"): `git log -S` on a bullet's current wording finds the last commit that rewrapped it, not the one that added it (cargo-liner's changelog rewrap `a5f4bf0b`, 2026-09-16); search the older wording at that commit's parent. A commit that entered the accepted line on a merge's second parent links and dates to its earliest first-parent descendant. A bullet, commit subject or doc comment can overstate; an entry describes the code. A `See it` command resolves with `command -v`, and a command's source is read before it is run. `amend` takes no `--production` or `--unit`. A title over 50 characters is refused: shorten it. A `live` read can fail once with `missing opening frontmatter delimiter` while Obsidian rewrites a note: read again. hana's showrunner writes hana notes through the same lock during the phase; an entry it adds for one of these crates is current truth and is left alone.

**Acceptance gate:** for each of the five crates, `python3 scripts/feature_log/feature_log.py changelog --crate <crate> --version Unreleased --compare <changelog saved at the audit ref>` prints `match` (cargo-tile's is followed by `not compared: Notes (6 bullets)`), or prints only bullets on the corrections list, which the unit director sends to the showrunner at once. `feature_log.py check` prints `ok`. `inventory.py status --project cargo-liner` exits 0. The phase's blind review is the audit: its reviewers write no notes; they read a sample of each seat's entries against the code at the audit ref, look for a change that has two live entries, and confirm that every statement of each superseded note is carried by its replacements. The phase report gives, per crate, the audit ref, entries amended, entries added, notes superseded by a split, and the corrections list.
