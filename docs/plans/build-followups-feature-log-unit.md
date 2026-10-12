# Feature log: one note per current result, written only by a script

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Builds `~/rust/hanadocs/proposals/feature-log.md`: six project folders and seven Bases in the vault, `scripts/feature_log/feature_log.py` as the only writer of feature notes, the close-out steps in `/showrunner:produce`, `/validate_and_push` and natedev's per-change loop, a backfill of all six projects through a read-only inventory, then the changelog export.

> **As-built disposition: create**

> **Production: build-followups** — unit `feature-log-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-10 12:39 PDT

feature log yes (the user to hana, 2026-10-10): build ~/rust/hanadocs/proposals/feature-log.md, phased as its Phases section says. Vault folders and Bases go in ~/rust/hanadocs; the script and command steps go in ~/.claude through this unit's branch. Tell the showrunner when the /showrunner:produce step (Phase 2) is live; hana starts using it then.

## Delegation Context

- **Project:** `~/.claude` config repo plus the hanadocs vault. This plan builds the design in `/home/natepiano/rust/hanadocs/proposals/feature-log.md` (the user, 2026-10-10: "feature log yes"). Worktree `/home/natepiano/worktrees/claude-build-followups-feature-log-unit`, branch `build-followups-feature-log-unit` (unit `feature-log-unit-unit`); name both in every dispatch.
- **Project started:** 2026-10-10T20:03:04.331+00:00
- **Stack:** Python standard library only; the scripts stay importable on Python 3.10 (the floor `scripts/lib/py` accepts, which the Mac runs them under), and the tests run only on natedev's `python3` 3.13, so they may use 3.12 names such as `typing.override` as other tests here do (`argparse`, `fcntl`, `json`, `os`, `re`, `shlex`, `socket`, `subprocess`, `datetime`, `zoneinfo`, `unittest`); no YAML library is installed (`import yaml` fails under `scripts/lib/py`), so the script reads and writes frontmatter with its own line parser. Obsidian 1.14 Bases (`.base` YAML). Markdown command files, written per `commands/succinct_style.md`.
- **Layout:**
  - `scripts/feature_log/` (new) — `feature_log.py`, `inventory.py`, `test_feature_log.py`, `test_inventory.py`.
  - `commands/showrunner/produce.md` (only `<ChangeReviewFeatureLog/>` and `<CloseChangeReview/>`), `commands/validate_and_push.md` (only `<FeatureLogCloseOut/>`), `commands/feature_log.md` (new), `commands/release.md` (changelog export only).
  - Vault, natedev: `/home/natepiano/rust/hanadocs/feature-log/{hana,natedev,cargo-liner,bevy_brp,obsidian_knife,nateroids}/`, `feature-log.base`, and `feature-log - <project>.base` for each project, at the vault root.
  - State, natedev: `~/.local/state/claude/feature-log/` (`XDG_STATE_HOME` moves it) — `lock`, `events.jsonl`, `digest.json`, `proposals/<row>.json`, `backfill/<project>.jsonl`.
  - Through natedev (the showrunner session) only: `/etc/nixos/README.md`, the per-change loop.
  - Evidence, read-only: `~/rust/hana`, `~/rust/hana_catalyst`, `~/rust/cargo-liner`, `~/rust/bevy_brp`, `~/rust/obsidian_knife`, `~/rust/nateroids`, `/etc/nixos`.
- **Key files:**
  - `/home/natepiano/rust/hanadocs/proposals/feature-log.md` — the approved design. Sections cited by name below: "What gets logged", "Current truth and replacement", "Storage", "Frontmatter", "Scripted writing", "Changelog generation", "Bases", "How an entry reaches the log", "Backfill", "Current-behavior check".
  - `scripts/whoami/agent_notes.py` — `read_note` 68; `set_fields` 89-107 writes a temp `.<name>.tmp` in the same vault folder, then `os.replace`; `_rewrite` 110-125. The vault-writing pattern.
  - `scripts/mac_test/mac_test.py` — `locked()` flock context manager 338-347; `write_json` temp file + `os.replace` 572-586; `add_subparsers` 1646-1667, `main` 1671. The CLI pattern.
  - `scripts/prioritize/renumber.py` — `parse_frontmatter` 231 and its duplicate-key check `_one_field` 256: a line-based frontmatter parser to follow.
  - `scripts/lib/py` — the launcher; every call is `~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py …`.
  - `/home/natepiano/rust/hanadocs/issues.base`, `/home/natepiano/rust/hanadocs/issues - hana.base` — Base conventions: `views:` of `type: table` with `name`, `filters: and: […]`, `order: […]`, `sort: [{property, direction}]`, `columnSize: {note.<prop>: <px>}`; project filter `project.contains(link("hana"))`.
  - `/home/natepiano/rust/hanadocs/change-review.md` — kept by hand by the hana showrunner. `## To review`: `| # | Change | Merged | On the Mac build | How to check |`; `## Reviewed`: `| # | Change | Verdict | Date |`. Numbers `A<n>` / `C<n>` only go up. Verdicts seen: `works`, `works; …`, `fine`, `fine for now`, `done`, `fixed`, `defect: …`, `feedback: …`, `change requested: …`. No script edits it.
  - `commands/showrunner/produce.md` — `<MergeCheckpoint>` 354-438 (step 3 design check 378-395; step 4 `merge_checkpoint.py` 396-417; step 6 after the push 426-437); the production log helper `update_registration.py log "<event>"` (`<Throughout>` 57-63). It has no change-review text yet; the rule lives in hana project memory (`~/.claude/projects/-home-natepiano-rust-hana/memory/change-review-list.md`).
  - `commands/validate_and_push.md` — `<WatchCI>` 36-138; all green 132-137; the `Validate And Push Complete` summary block 140-154; exit 2 hands off to `push_pr_branch_and_merge.sh` 17-32.
  - `/etc/nixos/README.md` — per-change loop 56-87: step 4 `rebuild` 80-84, step 5 "Verify the live effect and log it" 85-87; each activation is a line in `~/.local/state/rebuild/generations.log` (README 562-565), with a `-dirty` revision for a dirty tree.
  - `commands/release.md` — the mode sequences 293-317 (STEP 4 runs `<FinalizeOnBase/>` in normal and isolated mode, `<FinalizeOnHotfixBranch/>` in hotfix mode; STEP 6 runs `<PublishPhases/>` in all three); `<FinalizeOnBase/>` 476-574 (shows `## [Unreleased]` at 485; `finalize_changelogs.sh` at 508); `<FinalizeOnHotfixBranch/>` 860-965 (shows `## [Unreleased]` at 869); `<PublishPhases/>` 589-725, where publishing happens and a dry run stops; `scripts/release/finalize_changelogs.sh` 5, 27-37.
  - `pyrightconfig.json` — one `executionEnvironments` entry per scripts directory.
- **Test lanes:** `scripts/feature_log/` → `test_feature_log.py`, `test_inventory.py` (new; beside the code; each imports its sibling by bare name). The backfill phases have none.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` from the worktree root.
- **Lint:** `basedpyright scripts/feature_log`: a pass ends `0 errors, 0 warnings, 0 notes` (it exits 3: `pyrightconfig.json` names a `.venv` no checkout has). Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Facts (checked 2026-10-10):**
  - The vault of record is natedev's `/home/natepiano/rust/hanadocs`: Obsidian has it open there, and `change-review.md` and `machines/` exist only there. The Mac clone (`/Users/natemccoy/rust/hanadocs`) sits at 8e251f7 (2026-09-15) and syncs only through git. The Mac reaches natedev with `ssh natedev`.
  - The vault is a git repo (`main`, ahead of origin, dirty with the user's edits). Agents do not commit it (`commands/issue.md:143`); no phase commits or pushes it.
  - Obsidian Linter runs on save in the editor only (`lintOnFileChange: false`): `yaml-key-sort` ascending, `sort-yaml-array-values` ascending, `compact-yaml`, and `date_created` / `date_modified` as `"[[YYYY-MM-DD]]"` from the file system in local time. natedev's zone is America/New_York.
  - Templater has a folder template for `issues/` only; none is added for `feature-log/`.
  - natedev's Obsidian (1.14.4) ships `obsidian-cli` (`/run/current-system/sw/bin/obsidian-cli`) with `base:query` and `base:views`. Until the user turns it on it answers `Command line interface is not enabled. Please turn it on in Settings > General > Advanced.` (Gate G1).
  - `[[hana]]` resolves to `topics/software/hana.md` and `[[nateroids]]` to `topics/software/nateroids.md`; no note exists for natedev, cargo-liner, bevy_brp or obsidian_knife.
  - Origins: `~/rust/hana` and `~/rust/hana_catalyst` → `natepiano/hana`; the others → `natepiano/{cargo-liner,bevy_brp,obsidian_knife,nateroids}`; `/etc/nixos` → `natepiano/nixos`. Every default branch is `main`. The accepted Hana line is `~/rust/hana_catalyst` `init/catalyst`, the hana production's merge branch.
- **Invariants:**
  - `feature_log.py` is the only writer of notes under `feature-log/` (proposal "Scripted writing"). No phase writes or edits a note by hand. No phase adds a feature template, Git hook, Claude settings hook, post-push hook or rebuild hook (proposal "Noise and repair").
  - No path sends a message per proposal or entry; `digest`, on request, is the only report.
  - Project repositories are evidence: they are read at a recorded ref (`git -C <repo> show <ref>:<path>`, `git log`, `git ls-tree`), never edited.
  - Tests never touch the live vault or state: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, and no test runs `ssh`.
  - Python: no `Any`, a `TypedDict` per JSON shape, no file-level ignores.
  - Times shown to the user are America/Los_Angeles.
- **Backfill procedure (Phases 4 and 6–10; proposal "Backfill" and "Current-behavior check"):**
  1. A seat works only the records it claims: `inventory.py next --project <p> --pool <pool> --claim <seat> [--kind <k>] [--path <prefix>…] [--limit 25]`, then `inventory.py show <id>` for a record's source text.
  2. Decide whether the record holds a user-visible result (proposal "What gets logged": the seven kinds). Refactor, test, build, CI and docs-only work is `internal` and gets no note.
  3. Check current behavior at the phase's audit ref: the tip of the accepted branch (hana: `~/rust/hana_catalyst` `init/catalyst`; the others `main`), read with `git rev-parse` when the phase starts and named in the phase report beside the ledger's frozen ref. The frozen ref decides which records exist; the audit ref decides what is true now. Current docs and public interfaces first, then tests, then implementation; run it when practical. A behavior that no longer exists is `not-current`. A replaced behavior is written only in its current form.
  4. Before writing, `feature_log.py live --project <p> [--crate <c>] [--area <a>] --grep <word>` finds the entries the result may affect. Still wholly true: mark the record `combined` into that entry and `amend --backfill --evidence` it. Replaced: `supersede --backfill`. New: `add --backfill`. A live entry that already describes a newer form than the audit ref shows (live logging wrote it after the ref) is current truth: mark the record `combined` into it and write nothing.
  5. `--backfill` calls pass `--completed` (the date of the accepting merge, tag or commit) and `--link` (that commit). A link a live entry already holds is refused unless that entry is the one being replaced (`supersede --old`) or the new result is a distinct one from the same commit (`--separate-result`). `--release crate@version` is passed only when exactly one existing changelog bullet maps to the entry; otherwise `release` stays `[]`. A summary for a published crate reads unchanged as a changelog bullet.
  6. Mark every record: `inventory.py mark <id>… --seat <seat> --as represented|combined --note "<title>"`, or `--as internal|not-current|unresolved --reason "<why>"`. `unresolved`: the sources cannot support a true summary, completion date and `See it`; no note is written.
  7. The two seats of a phase claim disjoint records, by project, `--kind` or `--path` prefix as the Work Order's **Seats** name them. A result found by both is written once by the first and `combined` by the second.
  8. The phase's blind review is the audit (proposal "Current-behavior check"): its reviewers write no notes, compare the project's live entries (`feature_log.py live --project <p>`) with the current surface at the audit ref, and report each qualifying behavior that has no live entry.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 1's live Base check (its code and tests go ahead) | the user turns on Obsidian on natedev → Settings → General → Advanced → Command line interface | `obsidian-cli help` lists commands instead of the "not enabled" line |
| G2 | Phase 11 | live logging has written a real `@Unreleased` entry: an `add` or `supersede` without `--backfill` for a published crate | `events.jsonl` holds an `add` or `supersede` event with `"backfill": false` and a `release` value ending `@Unreleased` |

After Phase 2 reaches `~/.claude` main, the unit director tells the showrunner that the `/showrunner:produce` step is live, so hana starts using it (the user's words in Source), and sends natedev the README step that Phase 2's Spec gives.

## Phases

### Phase 1 — The vault holds six folders and seven Bases, and one script writes every note  · status: done

#### As-built

- `scripts/feature_log/feature_log.py` writes and reads feature notes through `add`, `supersede`, `amend`, `live` and `check`. `main(argv: list[str]) -> int` turns every `FeatureLogError` into exit 2 and one stderr line `feature_log: <reason>`, with nothing written. `PROJECTS: dict[str, ProjectSpec]` (`repo`, `crates`, `published`), `KINDS` and the `Note` TypedDict follow the proposal's "Storage" and "Frontmatter"; `Note` is the frontmatter boundary shape, so `area` and `production` are `str | None` to mirror whether the key is present.
- Paths are functions that read the environment on each call: `vault_dir()` (`FEATURE_LOG_VAULT`, else `~/rust/hanadocs`) and `state_dir()` (`$XDG_STATE_HOME/claude/feature-log`, else `~/.local/state/claude/feature-log`). `today()` is the machine-local date. `locked()` is a public context manager holding an exclusive `fcntl.flock` on `state_dir() / "lock"`.
- Every write runs under `locked()`, writes `.<name>.tmp` in the target folder and `os.replace`s it, then appends one `FeatureLogEvent` line (`at` in America/Los_Angeles, `verb`, `project`, `title`, `kind`, `summary`, `release`, `backfill`, `old`, `fields`) to `state_dir() / "events.jsonl"` through `_event(verb, note, *, backfill, old, fields)` and `_append_event(event)`.
- Frontmatter has its own writer (`render_note`) and line parser (`parse_note`): ascending key order, `[]` for an empty list, block items otherwise, links and dates as `"[[x]]"`, and a string bare only when YAML reads it back unchanged (`SAFE_SCALAR_RE`, no leading character from `UNSAFE_BARE_SCALAR_PREFIXES`), else `json.dumps`. `behavior` and `see` may not hold a `## See it` or `## Evidence` line. Writes and `check` share `validate_note` and `normalize_title` (the `commands/issue.md:48` rule, 50 characters at most). `check` prints `ok: <n> notes`, or one `<path>: <reason>` per error with unreadable notes reported and no traceback.
- `live --project P [--crate] [--area] [--kind] [--grep]` prints one line per live note, newest `completed_date` first: `<completed> <kind> <title> — <summary> [<crates>] (<area>)`; `--grep` matches title, summary and behavior, case-insensitive.
- Repeats: `_same_repeat` compares a repeat with the stored note apart from `date_created` and `date_modified`, and apart from `completed_date` when the repeat omits `--completed`; equal prints `unchanged`, different fails with `use amend`. `_reject_duplicate_link` refuses a live note's link under another title unless `--separate-result`, and lets `supersede` reuse the link of its own `--old` notes (a retitle). A repeated `supersede` finishes any `--old` still live.
- Vault (not in git): six empty `feature-log/<project>/` folders; `feature-log.base` with **Latest**, **By project**, **By crate**, **By kind** and **Superseded**, the "By" views grouped by `groupBy: {property: note.<prop>, direction: ASC}`; six `feature-log - <project>.base` with **Current** and **Superseded**, filtered on `project.contains(link("<project>"))`.

**Files:**
- `scripts/feature_log/feature_log.py` — the only writer of feature notes: the CLI plus the importable `vault_dir`, `state_dir` and `locked`.
- `scripts/feature_log/test_feature_log.py` — 28 tests on temp vault and state dirs: the proposal's exact layouts, every rejection, repeats, supersede after an interrupted run, amend, live and check.
- `pyrightconfig.json` — an `executionEnvironments` entry for `scripts/feature_log`.
- `/home/natepiano/rust/hanadocs/feature-log/`, `feature-log.base` and the six `feature-log - <project>.base` files — vault config, not committed.

**Binds later work:** `vault_dir()`, `state_dir()` and `locked()` are the import surface for the close-out, digest and ledger scripts; no later script reads a path constant. The close-outs and digest extend `_event`, `_append_event`, `_same_repeat` and `_reject_duplicate_link`. The link rule (one live note per link unless `--separate-result`; a `supersede` may reuse its `--old` link) is the one the Backfill procedure step 5 states.

**Gotchas:**
- The script stays importable on Python 3.10 (the Mac's `scripts/lib/py`); the tests run on natedev's 3.13.
- A note is written before its event, so a kill between the two leaves a note with no event, and a retry prints `unchanged` without appending one; the close-outs' event repair on repeat closes this.
- `obsidian-cli base:query` output flattens grouped views; a group check reads its JSON output.
- An unresolved project link (`[[natedev]]`) still matches `project.contains(link("natedev"))`.

**Ruled out:**
- Semantic types for `Note`'s `area` and `production`: `Note` mirrors key presence at the frontmatter boundary.
- A recovery journal or operation id for the note-without-event gap: a repeat that appends the missing event is simpler.
- `file.inFolder("feature-log/<project>")` filters in the project Bases: the link filter matches unresolved project links.
- A tester seat or cross-audit seats in the backfill phases: `inventory.py next --path` makes path splits claimable by two writers, and each phase's blind review is the audit.

### Phase 2 — Hana, single-session and natedev close-outs write entries, and a digest reports them  · status: done

#### As-built

- **One vault.** `main(argv, ssh_runner: SshRunner = _run_ssh) -> int` is the public entry. When the short host name is not `natedev` and `FEATURE_LOG_VAULT` is unset, every verb except `project-of` runs `ssh natedev "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py " + shlex.join(argv)` and returns its status; natedev's vault is the one Obsidian has open. `_run_ssh` and `_project_of` are private.
- **Events, one TypedDict per verb.** `FeatureLogEvent` is the union of `AddEvent` (`at`, `verb`, `project`, `title`, `kind`, `summary`, `release`, `backfill`), `SupersedeEvent` (the same plus `old`) and `AmendEvent` (`at`, `verb`, `project`, `title`, `fields`, `backfill`), narrowed on `verb`. A repeat whose result is already written appends its event when `events.jsonl` holds none of that verb, project and title, then prints `unchanged`; this repairs a note whose event was lost between `_write_note` and `_append_event`.
- **Review rows.** `propose --review ROW` (`^[AC]\d+$`) runs every `add`/`supersede` validation, writes no note, and saves `state_dir()/proposals/<ROW>.json`: `OpenProposal`, `LandedProposal` (`note`) or `DroppedProposal` (`verdict`), discriminated by `status`, each with `row`, `verb` (`supersede` when `--old` is given), `args: ProposalArgs` and `at`. `completed` is saved resolved; a repeat replaces an open proposal and keeps its `completed` unless `--completed` is passed; a landed or dropped proposal refuses it. `add --review ROW` / `supersede --review ROW` land the open proposal with its saved arguments and print `[[<title>]]`; a verb mismatch fails naming the right verb, a landed row prints `unchanged`, and a missing one fails `no proposal for <ROW>; run propose first`. `drop --review ROW --verdict TEXT` closes an open proposal without a note; a repeat prints `unchanged`.
- **`digest [--since YYYY-MM-DD]`** lists events by project in `PROJECTS` order (`+ <title> — <summary>`, `→ <old> ⇒ <new>`, `~ <title> (<fields>)`), leaving out `backfill: true` events, and ends `<n> changes since <date HH:MM PDT>` or `feature log: nothing new since <…>`. Without `--since` it reads events and the `state_dir()/digest.json` cursor `{"line", "at"}` and writes the advanced cursor (past backfill events too) under one `locked()`; the cursor is a line position, so events in the same second list once each. `--since` reads without the lock and moves no cursor.
- **`project-of [PATH]`** maps `origin` (`git@github.com:<slug>` or `https://github.com/<slug>`, with or without `.git`) to the project whose `repo` is that slug, else `none`, exit 0; it runs locally on every host.
- **Close-outs.** `<ChangeReviewFeatureLog/>` in `<MergeCheckpoint/>` (Hana checkouts only, after `merge_checkpoint.py` accepts the merge) adds one `## To review` row per user-visible result with a `propose --review` beside it, or logs `feature log: none — <reason>` for internal-only work. `<CloseChangeReview/>` lands a `works`/`fine` verdict with `add`/`supersede --review` and drops any other. `<FeatureLogCloseOut/>` in validate_and_push.md writes an entry or `none — <reason>` for `cargo-liner`, `bevy_brp`, `obsidian_knife` and `nateroids` after a default-branch landing, and omits the `Feature log:` summary line for `hana`, `natedev` or `none`. natedev's README per-change loop step 6, in `/etc/nixos`, closes out after a `rebuild` whose clean revision is on `origin/main`.

**Files:**
- `scripts/feature_log/feature_log.py` — forwarding, per-verb event types, `amend --backfill`, event repair, `propose`, `drop`, `--review`, `digest`, `project-of`.
- `scripts/feature_log/test_feature_log.py` — cases for each of the above.
- `commands/showrunner/produce.md` — `<ChangeReviewFeatureLog/>` and `<CloseChangeReview/>`.
- `commands/validate_and_push.md` — `<FeatureLogCloseOut/>` and the `Feature log:` summary line.
- `commands/feature_log.md` — `/feature_log digest [--since]` and `/feature_log live <project> [--crate …]`; points writers to the three close-outs.

**Binds later work:** off natedev every `feature_log.py` call forwards to natedev over ssh unless `FEATURE_LOG_VAULT` is set; `amend --evidence` skips evidence already stored, so a repeated amend prints `unchanged`; `amend --backfill` records `backfill: true`.

**Gotchas:** duplicate-link refusal fires at landing, not at `propose` (no note exists until then), so produce.md's recipe passes `--separate-result` on every row when one merge has several results. A test or tool run off natedev without `FEATURE_LOG_VAULT` reaches natedev's live vault.

### Phase 3 — Every backfill source record sits in a frozen ledger  · status: done

#### As-built

- **Ledger:** `inventory.py` reads every repository read-only (git runs with `GIT_OPTIONAL_LOCKS=0`) and keeps one ledger per project at `state_dir()/"backfill"/<project>.jsonl`, one JSON object per line with sorted keys, written inside `locked()` through `.<name>.tmp`, `fsync` and `os.replace`.
- **Record types:** `BackfillSourceRecord` holds `id` (`<project>:<kind>:<key>`), `project`, `repo` (absolute), `ref` (40-hex frozen tip), `kind: RecordKind` (`phase`, `asbuilt`, `changelog`, `tag`, `merge`, `commit`), `pool: Pool` (`structured`, `commits`), `title`, `date` (`YYYY-MM-DD`), `source` (`<path>:<line>` or a sha), `claim: ClaimState = Unclaimed | ClaimedBy` (`seat`) and `outcome: ReviewOutcome = Undisposed | NoteOutcome | NoNoteOutcome`. `NoteOutcome` (`represented`, `combined`) carries `note`; `NoNoteOutcome` (`internal`, `not-current`, `unresolved`) carries `reason`; no record holds both. Every ledger read checks the exact field set, each tagged state, nonempty claim and outcome text, the ref and date shape, and unique ids.
- **Source table:** `SOURCES: SourceTable = dict[str, ProjectSources]`; `ProjectSources(sources, proposal_counts)`; `RepositorySource = DocumentSource | ChangelogSource | TagSource | HistorySource`; `HistorySource.history` is `EntireHistory()` or `AfterRef(excluded_ref)`. `ProposalCounts = dict[Pool, dict[ProposalCategory, int]]` labels phase and asbuilt counts apart. `main(argv, sources=SOURCES) -> int` takes the table as a parameter.
- **Document sources,** read at the frozen branch tip: `phase` is each `#### As-built` block outside fenced code in a `docs/**/*.md` listed by `git ls-tree -r -z --name-only <ref> -- docs`, titled by the nearest `### Phase …` heading above it (none above refuses `collect`), key `<path>#<heading>`. `asbuilt` is a file whose parent directory is named `as-built` and holds no block, titled by its first heading, key `<path>`. `changelog` is each top-level `- ` line under a `## [<version>]` heading (`[Unreleased]` included), key `<path>@<version>#<n>`, files picked by globs matched component by component with equal component counts. A file record's date is the last commit touching it at the ref. All selected file text comes through one `git cat-file --batch -z` process; a missing blob, non-UTF-8 text or a short reply refuses `collect`.
- **History sources:** `tag` is `for-each-ref --merged=<ref> refs/tags` with the peeled sha as source, its pool set per source (natedev's sits in `commits`); `merge` is `git log --first-parent --merges` and `commit` is `git log --no-merges`, over the whole history or `<excluded>..<ref>`. A merge or commit sha seen in two sources keeps its first record; any other duplicate id refuses `collect`. Records sort by `(date, id)`.
- **`collect --project P`:** with a ledger present, reads it under the lock and only prints. Otherwise collects outside the lock, then writes under it only if the ledger is still absent (a concurrent first `collect` wins). Prints each pool and kind beside the proposal's figure (`not measured` for a category the proposal never measured), the structured pool's documents and files, and one `ref: <repo> <sha>` line per frozen tip.
- **`next --project P --pool POOL [--kind K] [--path PREFIX…] [--claim SEAT] [--limit N]`:** undisposed records unclaimed or claimed by `SEAT`, oldest first, as `<id> | <date> | <kind> | <title> | <source>`. `--path` repeats and takes several prefixes; a record matches when the path part of its `source` starts with any one, and a sha source matches none. `--claim` sets `ClaimedBy(SEAT)` on the listed records under the lock. `--limit` defaults to 25; `0` lists all.
- **`show ID`:** the record as one JSON line, then its source text at the frozen ref: the phase block, the bullet under its version heading, the whole as-built file, or `git show --stat` for a tag, merge or commit.
- **`mark ID… [--ids-from FILE] [--seat SEAT] --as D (--note TITLE | --reason TEXT)`:** `represented` and `combined` need `--note` naming an existing `vault_dir()/"feature-log"/<project>/<TITLE>.md`; `internal`, `not-current` and `unresolved` need `--reason`. A record claimed by a seat other than `--seat` is refused, naming that seat. IDs may span projects; every ID is checked before any write, so a refusal changes nothing. Re-marking replaces the outcome.
- **`status --project P [--pool POOL]`:** `--pool` resolves at the argparse boundary into a `tuple[Pool, ...]` (both pools when omitted); prints counts for undisposed and each disposition, then the notes the ledger names.
- **Exit codes:** `0` on success; `1` from `status` while any selected record is undisposed; `2` for every refusal (argument error, `InventoryError`, `OSError`, source text that no longer parses to the record), with `inventory: <reason>` on stderr.

**Files:**
- `scripts/feature_log/inventory.py` — source table, ledger and the five verbs.
- `scripts/feature_log/test_inventory.py` — tests on temp git repos: each record kind, an excluded range, a repeat `collect` leaving the ledger byte-identical, seat claims, path prefixes, `mark` refusals and `status` exit codes.

**Binds later work:**
- Ledgers are frozen as of 2026-10-10 at hana `ce51498` + hana_catalyst `2408021`, `/etc/nixos` `9d99e9e`, cargo-liner `9301628`, bevy_brp `e65064e`, obsidian_knife `5df4349`, nateroids `c7d6a51`; `collect` only prints once a ledger exists.
- Hana's structured pool holds 261 records (203 phase across 14 plan docs, 58 asbuilt) and splits 125 / 136 by source path prefix. Counts above the proposal's are work landed after its measurement (hana +3 phase, +7 hana_catalyst merges; natedev +3 commits).
- `next --path` matches a prefix of the record's source path; `next --claim <seat>` claims under the lock, after which another seat's `next` skips those records and its `mark` is refused.
- `mark` dispositions: `represented` and `combined` with `--note` (the note must already exist), `internal`, `not-current` and `unresolved` with `--reason`.
- `status` exits 1 while any record is undisposed and 0 once every record carries a disposition.

**Gotchas:**
- `git ls-tree` without `-z` quotes non-ASCII paths, which then silently fall out of path filters; `fnmatch` `*` crosses `/`, so a whole-path changelog glob matches nested files.
- The proposal's hana "58 files" are as-built files and its "200 records" are phase records, which live in plan docs rather than those files; at the proposal's measured ref the counts are exactly 200 and 58.

### Phase 4 — Hana's structured phase records are in the log  · status: done

#### As-built

- **Data only:** Hana's notes and ledger dispositions are written through the existing `feature_log.py` and `inventory.py` verbs; no code changes, and the vault is never committed.
- **Dispositions:** all 261 of Hana's structured records carry one: represented 136, combined 103, internal 21, not-current 1; `inventory.py status --project hana --pool structured` exits 0.
- **Notes:** `feature_log.py check` reports 123 live Hana notes: 118 named by the ledger plus 5 for current behaviors with no structured record (mesh outlines with controlled overlap, repairing waiting monitor outputs from the palette, wheel orbit/pan/zoom, the Fly Over Scene tour, the step sequencer).
- **Audit:** the live notes match the current surface at hana_catalyst audit ref `9764f5eda7d4` (as-built docs, controls, tool definitions, crate READMEs). The jack panels note states a 0.75 default opacity, blended; the image batching record (clipping borders stay visible over coplanar images) is `represented`.

**Files:** (no repository file changes)
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — the 123 live Hana notes (vault, never committed).
- `~/.local/state/claude/feature-log/backfill/hana.jsonl` — the frozen Hana ledger, every structured record's disposition included.

**Binds later work:**
- Hana's structured pool is fully dispositioned: `status --project hana --pool structured` exits 0.
- The five record-less notes are where the merges that built them mark `combined` in "nateroids' commits and Hana's merges are logged, and both audits finish".
- Merges already held as a live note's frontmatter `link` are coverage candidates: `d2569c62`, `f9ef2a30` (backing three notes), `785db466`. `c7ad7842`, `54f2946d` and `1bbc6520` are direct first-parent commits that need no mark.
- Accepting-merge rule: a commit's accepting ref is the earliest first-parent commit on the accepted line that descends from it (`--ancestry-path` intersected with `--first-parent`), or the commit itself when it is on that line. `git log --first-parent --merges --ancestry-path <c>..<audit>` alone returns nothing for work that entered on a merge's second parent (subtree import `785db466`, workspace consolidation `f9ef2a30`).
- Three notes ("idle scenes spend work on changed tools", "new tools avoid rebuilding the whole scene", "frame time shows main and render costs") link source-file commit `6024084a3`, not their accepting merges `34b62541f`, `eea40e7dc`, `f8ac6c24d`, because `amend` has no link field; "A backfilled note's link, date and release can be corrected" relinks them.

**Gotchas:**
- `feature_log.py` refuses titles over 50 characters after normalization: shorten the title and continue.
- `amend` takes no `--production`/`--unit`; those belong to `add` and `supersede`.
- Several results can share one accepting link, so a second note on the same link needs `--separate-result`.
- Behaviors from commit-only work (no phase block, no as-built file) have no structured record; only the surface audit finds them.

### Phase 5 — A backfilled note's link, date and release can be corrected  · status: done

#### As-built

- **Accepting ref:** `amend --backfill --link <commit> --completed <YYYY-MM-DD>` (both or neither) replaces a note's `link` (canonicalized to `https://github.com/<repo>/commit/<sha>`) and `completed_date` in place. `_amend_provenance` resolves the arguments into `AcceptingProvenanceAmendment = KeepAcceptingProvenance | CorrectAcceptingProvenance` (`link`, `completed`). Without `--backfill`, `--link` and `--completed` are refused, and `--separate-result` without `--link` is refused. Every correction runs `_reject_duplicate_link`, as at `add`: a link another live note holds is refused unless `--separate-result`.
- **Historical releases:** `amend --backfill --release crate@version` (repeatable) adds exact membership. A crate absent from the note's `crate` list or not published, `Unreleased`, or a malformed value is refused; `--release` without `--backfill` is refused; a value already present prints `unchanged`.
- **Events:** `FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent`. `AmendEvent` adds `old_link`, `new_link`, `old_completed`, `new_completed` and `releases_added` to the six `LegacyAmendEvent` keys (`at`, `verb`, `project`, `title`, `fields`, `backfill`); older amend events parse as `LegacyAmendEvent`, and `digest` reads both.
- **Write order:** an amendment that changes link, completed date or releases appends its event first (`_journal_accepting_correction`), then writes the note; a retry whose event equals the latest amend event for that note, `at` aside, writes the note without a second event. Every other amendment writes the note, then its event.
- **Versions:** `VERSION_RE` refuses an empty prerelease identifier (`1.2.3-..`) at `add`, `supersede` and `amend` alike.
- **`live`** ends each line with the note's link: `<completed> <kind> <title> — <summary> [<crates>] (<area>) <link>`.
- **Vault:** the Hana notes "idle scenes spend work on changed tools", "new tools avoid rebuilding the whole scene" and "frame time shows main and render costs" link their accepting merges `34b62541f`, `eea40e7dc` and `f8ac6c24d`, each with that merge's date; `check` reports 123 Hana notes.

**Files:**
- `scripts/feature_log/feature_log.py` — `amend` link, date and release corrections, the amend event journal, `VERSION_RE`, the `live` link column.
- `scripts/feature_log/test_feature_log.py` — 58 tests, the amend correction, refusal, retry and version cases among them.
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — the three relinked notes (vault, never committed).

**Binds later work:**
- A backfill correction amends the existing note, never writes a second one: `amend --backfill --link <commit> --completed <date>` for its accepting ref and date, `--release crate@version` for exact historical membership; a link another live note holds needs `--separate-result`.
- For "A published crate's changelog section comes from the log": `FeatureLogEvent` holds `AmendEvent` beside `LegacyAmendEvent`, and every writer refuses a version with an empty prerelease identifier.

**Gotchas:** link, date and release amendments write event-then-note, the reverse of every other write: after a kill between the two, the event exists and the note lags until a retry writes it.

**Ruled out:**
- Exempting a date-only correction from the duplicate-link refusal when the note keeps a link another live note shares: such a correction passes `--separate-result` again.

### Phase 6 — cargo-liner's as-built files and changelogs are in the log  · status: done

#### As-built

- cargo-liner's frozen structured ledger (as-built files, changelog bullets, tags; `cargo-berth-test-support` excluded) is fully disposed: `inventory.py status --project cargo-liner --pool structured` exits 0 over 811 records — represented 277, combined 458, internal 55, not-current 21, unresolved 0. `feature_log.py check` prints `ok` (415 notes in all projects).
- Duplicate bullets across crates and releases are one note; a note carries a historical `crate@version` only when exactly one changelog bullet maps to it; no old changelog is rewritten.
- Live notes are true against cargo-liner's surface at the frozen ref: READMEs, CLI verbs and flags (from argument definitions at that ref when the installed binary's version differs), public API and Unreleased sections. Behavior the changelogs omit has notes from that surface: cargo-berth's checkpoint-and-integrate workflow, `recover --recovered` / `--abandon --why`, `renew`, `integrate --force --why`, cargo-port's focused-pane copying with tui_pane's clipboard registration, and tui_pane's actionable toasts.
- A commit that entered `main` on a merge's second parent links and dates to its earliest first-parent descendant: 48 cargo-mend notes link cargo-mend's graft commit, and 2026-08-30 work carries 2026-09-03.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — 292 note files, 288 live, written only through `feature_log.py`.

**Gotchas:**
- `git log -S` on a bullet's current wording finds the last commit that rewrapped or reworded it, not the one that added it; the older wording is searched at that commit's parent. The changelog rewrap `a5f4bf0b` (2026-09-16) is the known case: 48 notes link the pre-rewrap match, and one links the rewrap, which genuinely added that bullet. Seven cargo-port notes link `d9558a79`, a summary rewrite later than the behavior.
- Changelogs omit CLI verbs and flags and some public API, so coverage needs each crate's argument definitions and public API read beside its changelogs.
- One `live --grep` refusal ("missing opening frontmatter delimiter") did not reproduce; note temp files (`.<name>.tmp`) are never globbed.

### Phase 7 — bevy_brp's structured records and nateroids' as-built files are in the log  · status: done

#### As-built

- bevy_brp's frozen structured ledger (as-built files, 205 changelog bullets across `extras`, `mcp` and `mcp_macros`, 25 tags) is fully disposed: `inventory.py status --project bevy_brp --pool structured` exits 0 over 234 records — represented 68, combined 78, internal 70, not-current 18, unresolved 0. `feature_log.py check` prints `ok` (490 notes in all projects).
- A synchronized extras method and its MCP wrapper tool share one note carrying both crates and both exact releases. 24 tags are internal release evidence; v0.18.0 is represented by the repair note "keep real time examples from dividing by zero" (d6ad93da).
- nateroids' structured pool (2 as-built files) is disposed (`--pool structured` exits 0, represented 2) and seeds 4 notes: "trigger game and camera commands through brp", linking 27f9a238 (2026-03-02, the event layer) and describing the current EscapeEvent form from 87df75a9 (2026-04-19, inspector-first Escape); and three star notes accepted by 23e54104 (stable tiny stars, shared GPU twinkle data, speed change without phase jumps).
- The tracing note links 470f9d95, the earliest first-parent descendant of its off-line introducing commit c617d8c6; it is the only such link among the 71 bevy_brp notes.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — 71 live notes, written only through `feature_log.py`.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — 4 notes, written only through `feature_log.py`.

**Binds later work:**
- nateroids' commit backfill: 87df75a9 combines into "trigger game and camera commands through brp" (link 27f9a238); 23e54104 combines into the three star notes, and its trackpad controls have no note yet. The project gate is `inventory.py status --project nateroids` with no `--pool`, which exits 1 while its 555 commit records are undisposed.
- bevy_brp's commit backfill: pre-workspace history is accepted by its subtree imports, c0839f61 (`extras/`) and 344e15aa (`mcp/`), both 2025-06-27; the 71 live notes are the combine targets, and `live --project bevy_brp` prints each link; 2,570 commit records remain undisposed.

**Gotchas:**
- A changelog bullet, commit subject or doc comment can overstate behavior; notes describe the code at the audit ref. extras 0.18.1's bullet and doc comment say type_text types one character per frame, while the queue presses on one frame and releases on the next (one character every two frames).
- Error-behavior claims hold only for the paths the code marks: a discovery miss and a handler error name their stage; transport and HTTP failures use the generic error.
- An introducing commit can sit off the audit ref's first-parent line outside any subtree import or consolidation; a link is valid only when `git rev-list --first-parent <audit ref>` contains it.

**Ruled out:**
- A separate note for 87df75a9's inspector-first Escape: the command-event note already describes it.

### Phase 8 — natedev and obsidian_knife are logged from their commits  · status: done

#### As-built

- natedev's frozen commit pool (976 commits, 1 merge, 1 tag, audited at `/etc/nixos` `9d99e9e`, the ledger's frozen ref) is fully disposed: `inventory.py status --project natedev` exits 0 over 978 records — represented 106, combined 182, internal 674, not-current 16, unresolved 0. Its 121 live notes carry `crate: []` and no release; each links a first-parent nixos commit, takes its feature boundary from the vault's `machines/configuration overview.md`, and names an installed operator command or machine state as See it.
- obsidian_knife's ledger (audited at `5df4349`, its frozen ref) is fully disposed: `inventory.py status --project obsidian_knife` exits 0 over 564 records — represented 27, combined 104, internal 408, not-current 25, unresolved 0. Its 28 live notes carry crate `obsidian_knife` and no release (unpublished); repair and follow-up commits combine into the note they complete.
- Both projects' audits are finished, and neither has a release value, so no changelog section selects their notes. `feature_log.py check` prints `ok` (639 notes in all projects).
- A commit with several distinct results has one note per result (`add --backfill --separate-result`): its ledger record is `represented` into one note and each sibling cites the commit in Evidence. nixos 1981b2a, f84bbf2, 504ae96 and 6e6b951 each carry two or three notes.
- Measured: natedev 22.45 min, obsidian_knife 24.0 min, blind review 21 review-seat minutes, one repair round about 6 min.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/natedev/` — 121 live notes, written only through `feature_log.py`.
- `/home/natepiano/rust/hanadocs/feature-log/obsidian_knife/` — 28 live notes, written only through `feature_log.py`.

**Gotchas:**
- The ledger holds one target per record, so a second result of a commit already marked into another note or `internal` has no record of its own and no status count reports it missing; each commit's diff is read for every result it carries.
- A note stating specific values links the first-parent commit that reached them: "keep ghostty settings writable after setup" links 7e29ee325 (opacity 0.5, blur 1), and 03a6de0f, which started the feature at other values, is combined into it.
- A See it command resolves with `command -v`: natedev installs `plasma-review`, not `plasma-diff`.
- Notes describe the code, and the code differs from the obvious reading: obsidian_knife's phantom-link alias lookup keys on the missing target's name (case-insensitive) and keeps the display label; an image hashing error passes through `.ok()?` in `image_processing.rs` and omits that image instead of stopping analysis.
- `agent-sessions-snapshot --help` ignores the flag and runs a snapshot, regenerating the vault's generated agent-session pages; a command's source is read before the command runs.

### Phase 9 — nateroids' commits and Hana's merges are logged, and both audits finish  · status: done

#### As-built

- hana's frozen merge pool (124 `main` merges and 292 `init/catalyst` merges, audited at hana_catalyst `bcd669367`) is fully disposed: represented 13, combined 273, internal 127, not-current 3. `inventory.py status --project hana` exits 0 over 677 records in both pools — represented 149, combined 376, internal 148, not-current 4, unresolved 0. The merges add 20 notes and the review's coverage repair 9 more, for 152 live hana notes.
- Picture and control-value delays are separate notes: "delay images through frames and feedback" (render-frame pictures) and "retain control values for graph feedback" (control-clock scalars).
- nateroids' frozen commit pool (553 commits, 2 merges) is fully disposed: `inventory.py status --project nateroids` exits 0 over both pools; the 555 commit records are represented 37, combined 154, internal 354, not-current 10, unresolved 0. 37 new notes join the 4 from its as-built files, for 41 live notes; 23e54104's trackpad controls are "orbit with forwarded trackpad scroll events".
- Both audits are finished: a blind review with contract and adversary lenses read every live hana and nateroids note against its project's current surface. `feature_log.py check` prints `ok` (705 notes in all projects).

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — 152 live notes, written only through `feature_log.py`.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — 41 live notes, written only through `feature_log.py`.

**Binds later work:** 35 hana notes from Phase 4 still link commits off every first-parent line of hana_catalyst `init/catalyst` (`afd2dde8`, `fb96ceed`, `0fff06f9`, `40e58e32`, `435d78c3`, `b48e6ec6`); "cargo-liner's and bevy_brp's commits are dispositioned, and both audits finish" relinks each to the commit on `git rev-list --first-parent bcd669367` that accepted the implementation commit completing its result.

**Gotchas:**
- Results that docs and controls never mention sit in public APIs and registries: stock tool catalogs (`stock_tools()`), derive macros, registration functions, picture and value delays. A coverage sweep reads each crate's public API and registries, not only its README and docs.
- Codex seats have no BRP tools: an app check runs by hand from a detached worktree at the audit ref (`BRP_EXTRAS_PORT=15798 cargo run`, curl JSON-RPC to `http://127.0.0.1:15798`); two seats sharing the port hand it over on the board.
- A `live` read can fail once with "missing opening frontmatter delimiter" when it catches a note mid-rewrite while Obsidian runs on natedev; `feature_log.py` writes are atomic.

**Ruled out:**
- Storing the rendered changelog section with the reviewed titles and refusing drift before `release` stamps: an amend a minute after stamping drifts the same way, so the check guards no lasting invariant.

### Phase 10 — cargo-liner's and bevy_brp's commits are dispositioned, and both audits finish  · status: done

#### As-built

- cargo-liner's ledger (audited at `9301628`, its frozen ref) is fully disposed: `inventory.py status --project cargo-liner` exits 0 over 3,043 records — represented 286, combined 1,353, internal 1,362, not-current 42, unresolved 0. It has 298 live notes.
- bevy_brp's ledger (frozen at `e65064e`, audited at `49089a51`) is fully disposed: `inventory.py status --project bevy_brp` exits 0 over 2,804 records — represented 70, combined 364, internal 2,305, not-current 65, unresolved 0. It has 73 live notes.
- The 35 hana notes that linked docs checkpoints off hana_catalyst's first-parent line now link the commit on `git rev-list --first-parent bcd669367` that accepted their implementation commit, with that commit's date as `completed_date`; Evidence names both commits. Every one of the 152 live hana notes links that line.
- Both audits are finished: a blind review with contract and adversary lenses read every live cargo-liner and bevy_brp note against its project's current surface. The backfill is complete: `feature_log.py check` prints `ok` (717 notes in all six projects).

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — 298 live notes, written only through `feature_log.py`.
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — 73 live notes, written only through `feature_log.py`.
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — 152 live notes; the 35 relinks written through `feature_log.py amend`.

**Binds later work:** no note carries `@Unreleased` (`--backfill` refuses it), and historical release values are exact versions only (cargo-liner 91, bevy_brp 66). Until live logging writes a crate's entries, "A published crate's changelog section comes from the log" prints only `## [Unreleased]` for it, and `--compare` reports every hand-written Unreleased bullet as a difference.

**Gotchas:**
- A result built in a commit marked into a broader note or `internal` stays missing until its code is read: 47eaf59f ("perf: off-thread lint history load") also fixed the sync-notification startup race. Each commit's diff is read for results its subject does not name.
- A docs-move or docs checkpoint commit can hold a note's link; the link is the commit that made the stated behavior true (the exact-file claim note links 268e8792, not docs-move 51042e15).

### Phase 11 — A published crate's changelog section comes from the log  · status: done

#### As-built

- `feature_log.py changelog --crate X --version V` (V a version or `Unreleased`) selects the notes whose `crate` holds X and whose `release` holds `X@V` — every note, live or superseded, for a released version; live notes only for `Unreleased` — and prints `## [V]`, then one `### <heading>` per non-empty heading in the order Added, Changed, Deprecated, Removed, Fixed, Security, each followed by `- <summary>` lines sorted by summary. Heading map: `feature` → Added, `fix` → Fixed, `change` and `perf` → Changed, `deprecation` → Deprecated, `removal` → Removed, `security` → Security. Exits: 0 rendered section or `match`, 1 differences, 2 unpublished crate (no output) or failed forwarded read.
- `changelog --crate X --version Unreleased --compare PATH` compares bullet sets per heading with the `## [Unreleased]` section of the CHANGELOG at PATH, read on the calling host, and prints `match` or the differences. argparse turns the optional `--compare` into `RenderSection` or `CompareSection` at the boundary. Off natedev it makes one forwarded read, `changelog --crate X --version V --selection` (internal: the rendered section and the selected note titles as JSON), through an injectable reader that returns `ChangelogSelection(section, titles)` or `ForwardFailed(exit, stderr)`, and compares locally; a failed read exits 2 with the remote error.
- `--save FILE` removes FILE before comparing and, on a `match`, writes the selection's titles as `ReviewedReleaseContents` on the calling host, so only the current comparison's match leaves a file and a file left by an aborted earlier release never makes a crate eligible to stamp.
- `release --crate X --version V --contents FILE` reads and validates FILE on the calling host (titles sorted and unique; every named note exists and has a pending or stamped release for X) and from another host forwards the titles as JSON on stdin, never the path. It replaces `X@Unreleased` with `X@V` in exactly the notes FILE's `ReviewedReleaseContents` names, including one superseded after the comparison; drops `X@Unreleased` from every superseded note the contents do not name; leaves live unnamed notes at `X@Unreleased`; and appends one `ReleaseEvent` (`at`, `verb: "release"`, `project`, `crate`, `version`, `titles`: the notes it changed), a member of the `FeatureLogEvent` union. It prints `released X@V (<n> notes)`, or `unchanged` on a repeat with the same contents, and refuses a named note that does not exist, a named note with no pending or stamped release for the crate, and a version already stamped with different contents.
- `digest` shows a release event as `= X@V (<n> notes)`.
- `/release` runs `changelog --compare` for each published crate being released, in `<FinalizeOnBase/>` and `<FinalizeOnHotfixBranch/>` beside the `## [Unreleased]` display, with `{crate_name}` read from `{crate_dir}/Cargo.toml` (the publish loop's `${PACKAGE_NAME}` does not exist in those blocks). It shows `match` or the differences to the release owner, who reconciles with `amend` or a changelog edit; a `match` saves `~/.local/state/claude/feature-log/release/<crate>.json` with `--save`; a crate that still differs is reported, not blocked, and has no saved contents; an exit 2 for a crate the log does not track is reported too. The stamp step in `<PublishPhases/>`, reached by all three mode sequences, runs `release --crate <crate> --version ${VERSION} --contents <saved file>` right after each crate's publish succeeds, only for a crate with saved contents and never on a dry run; a later failed publish leaves earlier stamps in place, and a retry's repeat prints `unchanged`.
- The live comparison on natedev covers 13 published crates: 7 match with nothing on either side (hana_kana, hana_lagrange, hana_liminal, cargo-mend, cargo-port, bevy_brp_extras, bevy_brp_mcp_macros); hana_rubric differs by 1 logged entry its changelog lacks; cargo-tile (163), hana_clerestory (31), tui_pane (23), hana_rigging (5) and bevy_brp_mcp (1) differ by hand-written bullets with no logged entries.

**Files:**
- `scripts/feature_log/feature_log.py` — `changelog`, `release`, the `ReleaseEvent` line in `digest`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/release.md` — the compare step in `<FinalizeOnBase/>` and `<FinalizeOnHotfixBranch/>`, the stamp step in `<PublishPhases/>`.

**Gotchas:**
- The saved contents hold titles only, so `release` cannot date a supersede: a note added and superseded between the comparison and the stamp loses `X@Unreleased` at that stamp and is counted in the release event's `titles`. No changelog section differs, because an `Unreleased` section reads live notes only and the note never gets a version.
- The saved contents file lives only on the calling host; `release` never forwards its path.
- A crate whose changelog has hand-written Unreleased bullets and no logged entries never matches, so it is never stamped until those bullets are logged or released.

**Ruled out:** recording the comparison boundary in the saved contents so a later supersede could be dated — both rules print the same sections, and the note never gets a version under either.

