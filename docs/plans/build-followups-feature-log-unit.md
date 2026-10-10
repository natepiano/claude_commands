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
- **Backfill procedure (Phases 4–9; proposal "Backfill" and "Current-behavior check"):**
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
| G2 | Phase 10 | live logging has written a real `@Unreleased` entry: an `add` or `supersede` without `--backfill` for a published crate | `events.jsonl` holds an `add` or `supersede` event with `"backfill": false` and a `release` value ending `@Unreleased` |

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

#### Work Order

**Goal:** a Hana merge proposes its entry beside its change-review row and writes it only when the row closes `works` or `fine`; a push to a default branch and a natedev rebuild each close out with an entry or a stated reason; a call made on the Mac writes natedev's vault; `digest` reports the silent entries on request.

**Spec:**

`scripts/feature_log/feature_log.py` additions:

- **One vault.** When the short host name (`socket.gethostname()` up to the first `.`) is not `natedev` and `FEATURE_LOG_VAULT` is unset, `main` runs `["ssh", "natedev", "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py " + shlex.join(argv)]`, passes its output through and exits with its status (my call: natedev's vault is the one Obsidian has open, and the Mac clone is 25 days behind; one function to change if the user wants another home). `project-of` never forwards: it reads only the caller's checkout, so it runs where it is called. The runner is a parameter, so tests assert the command without running `ssh`.
- **Events, one type per verb.** Phase 1 writes one `FeatureLogEvent` shape for every verb, with empty `old` and `fields` where they mean nothing. Replace it with one TypedDict per verb, discriminated by `verb` and holding only its own fields: `AddEvent` (`at`, `verb: "add"`, `project`, `title`, `kind`, `summary`, `release`, `backfill`), `SupersedeEvent` (the same with `verb: "supersede"` and `old`), `AmendEvent` (`at`, `verb: "amend"`, `project`, `title`, `fields`, `backfill`); `FeatureLogEvent` is their union, and the reader narrows on `verb`. `amend` gains `--backfill`, which its event records (Phase 1 always wrote `false`).
- **Event repair (a Phase 1 defect).** A note is written before its event is appended, so a process killed between the two leaves a note with no event, and the retry prints `unchanged` without writing one. A repeat whose result is already fully written appends its event when `events.jsonl` holds no event of that verb, project and title, then prints `unchanged`.
- `propose --review ROW` with every `add` argument, plus `--old T …` when the result replaces live entries. `ROW` matches `^[AC]\d+$`. It runs every `add` / `supersede` validation, including the title, link and `--old` checks against the vault, writes no note, and saves `state_dir()/"proposals"/<ROW>.json`. It saves `completed` resolved (today when not given), so a row landed on a later day keeps the merge's date. The file is one of three TypedDicts discriminated by `status`, each holding only its own fields: `OpenProposal` (`row`, `verb: "add"|"supersede"`, `args`, `status: "open"`, `at`), `LandedProposal` (the same with `status: "landed"` and `note`: the title), `DroppedProposal` (the same with `status: "dropped"` and `verdict`); `args` is a `ProposalArgs` TypedDict of the saved `add` arguments with `old`. A repeat replaces an open proposal (the corrected-fields path); a landed or dropped one fails.
- `add --review ROW` / `supersede --review ROW` land the open proposal with its saved arguments. The verb must match the proposal's (a mismatch fails, naming the right verb). Success writes a `LandedProposal` and prints `[[<title>]]` for the Reviewed row. A landed proposal prints `unchanged`. No proposal fails with `no proposal for <ROW>; run propose first`.
- `drop --review ROW --verdict TEXT` closes an open proposal without a note, writing a `DroppedProposal`; a repeat prints `unchanged`.
- `digest [--since YYYY-MM-DD]` prints the events after the cursor (or from `--since`), grouped by project in the order of `PROJECTS`: `+ <title> — <summary>` for an add, `→ <old> ⇒ <new>` for a supersede, `~ <title> (<fields>)` for an amend. Events with `backfill: true` are not listed (the backfill phases report their own counts). The cursor in `state_dir()/"digest.json"` is `{"line": <events.jsonl lines consumed>, "at": "<time of that digest>"}`: a position, so events written in the same second are each listed once. It ends `<n> changes since <date HH:MM PDT>` and advances the cursor past every event read, backfill ones included (not with `--since`). With none: `feature log: nothing new since <…>`.
- `project-of [PATH]` maps `git -C PATH remote get-url origin` (`git@github.com:<slug>` or `https://github.com/<slug>`, with or without `.git`) to the project whose `repo` is that slug and prints it, or `none`; exit 0 either way.

`commands/showrunner/produce.md` (proposal "Hana units"), applied when the production's repository is a Hana checkout (`~/rust/hana` or `~/rust/hana_catalyst`):

- `<ChangeReviewFeatureLog/>`, a step of `<MergeCheckpoint/>` after `merge_checkpoint.py` has accepted the merge (step 4), before step 5: (1) for each distinct user-visible result of the merge, add a `## To review` row to `~/rust/hanadocs/change-review.md` (next number; merge date; Mac build when it passes; how to check — a behavioral check for a result that is not visual); internal-only work gets no row, and the production log gets `feature log: none — <reason>`; (2) beside each row, `feature_log.py propose --review <row>` with project `hana`, crates, kind, title, summary, behavior, see, the merge sha as `--link`, evidence (`Showrunner merge <sha>, from checkpoint <sha>.`), `--production <slug>`, `--unit <unit>`, and `--old` for each live entry it replaces (found with `live`); (3) the merge is not complete until every row has a valid proposal or the log holds the no-entry reason.
- `<CloseChangeReview/>`, when the user answers a row: a verdict beginning `works` or `fine` → `add --review <row>` (or `supersede --review <row>` when the proposal names `--old`); a missing proposal is proposed first; a wrong meaning is fixed by rerunning `propose`; a script failure leaves the row open until the call is fixed. Any other verdict → `drop --review <row> --verdict "<verdict>"`; the fixing merge gets a new row and a new proposal. Then move the row to `## Reviewed` with its verdict and date; a landed row's Change cell may carry the printed `[[<title>]]`. No message reports the entry.

`commands/validate_and_push.md`: `<FeatureLogCloseOut/>` (proposal "Single-session projects"), after "All green" and before the success summary, also after `push_pr_branch_and_merge.sh` lands the PR. When `feature_log.py project-of .` prints `cargo-liner`, `bevy_brp`, `obsidian_knife` or `nateroids` and the commit landed on the default branch, the session that did the work checks `live` and runs `add`, `supersede`, or records `none — <reason>`. The summary block gains `Feature log    added <title> | superseded <old> → <title> | none — <reason>`. The skill is not complete until the script succeeds or the reason is recorded. Hana results close at change review and natedev's in its README, so both print nothing here.

`commands/feature_log.md` (new; my call: a user asking for "feature log digest" in any session needs a command to find): `/feature_log digest` runs `digest`; `/feature_log live <project> [--crate …]` runs `live`; one line points writers to the three close-out steps.

natedev's README (sent to natedev to commit in `/etc/nixos`; this unit does not edit it): a sixth per-change-loop step after line 87 — **Feature log close-out.** After `rebuild` succeeded and the activated revision (the last line of `~/.local/state/rebuild/generations.log`, without `-dirty`) is on `origin/main`, the session that made the change runs `~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py add --project natedev …` (or `supersede`), or states `feature log: none — <reason>`, before it calls the change complete. `rebuild` and `update` never call the script; a failed activation, a dirty revision, `boot` and `test` never reach this step.

**Files:**
- `scripts/feature_log/feature_log.py` — forwarding, event types, `amend --backfill`, event repair, `propose`, `drop`, `--review`, `digest`, `project-of`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/showrunner/produce.md` — `<ChangeReviewFeatureLog/>` and `<CloseChangeReview/>` (also touches; owner stalls-unit, run done).
- `commands/validate_and_push.md` — `<FeatureLogCloseOut/>`.
- `commands/feature_log.md` — new.

**Seats:** `1 writer + 1 tester` — one script and three short command edits that follow it; the subcommand rules are concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`, `commands/showrunner/produce.md`, `commands/validate_and_push.md`, `commands/feature_log.md`, and the README step's text in the phase report.
- `test` — `scripts/feature_log/test_feature_log.py`: forwarding builds the `ssh natedev` command on another host and stays local on natedev or with `FEATURE_LOG_VAULT`; `project-of` on another host runs locally and builds no `ssh` command; each verb writes only its own event fields, and `amend --backfill` records `backfill: true`; a note whose event was removed gets it back from an exact repeat, which still prints `unchanged`, and a second repeat appends nothing; `propose` writes no note and refuses a landed row; `add --review` writes exactly one note and its repeat writes none; a row proposed one day and landed the next keeps the proposal's completed date, and a replay on a third day prints `unchanged`; a verb mismatch fails; `drop` writes none; `digest` lists adds, supersedes and amends by project, lists two events from the same second once each, leaves out backfill events while its cursor passes them, advances its cursor, and prints the empty line; `project-of` maps both URL forms and prints `none` for an unknown slug.

**Constraints from prior phases:** Phase 1 built `feature_log.py` with `add`, `supersede`, `amend`, `live`, `check`; paths come from the public functions `vault_dir()` and `state_dir()` (read from the environment on every call; there are no `VAULT`/`STATE` constants), every write runs under the public `locked()` context manager (flock on `state_dir()/"lock"`), and each write appends a `state_dir()/"events.jsonl"` line. Phase 1's events share one `FeatureLogEvent` shape (`at`, `verb`, `project`, `title`, `kind`, `summary`, `release`, `backfill`, `old`, `fields`) built by `_event` and written by `_append_event` after `_write_note`; `_run_amend` always passes `backfill=False`. A repeat is decided by `_same_repeat` and prints `unchanged: <path>`; a link a live entry holds is refused by `_reject_duplicate_link` unless the holder is among `--old` or `--separate-result` is given. `main(argv) -> int` returns 2 with one `feature_log: <reason>` stderr line on a refusal. The script must stay importable on Python 3.10. No live `events.jsonl` exists yet, so the event shape changes with no migration.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live, with `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` on temp dirs: `propose --review C900` writes no note; `add --review C900` writes one and its repeat none; `propose --review C901` then `drop` writes none; `digest` lists only C900's entry. From the Mac, a copy of the worktree's `feature_log.py` run as `live --project natedev` forwards to natedev and prints its list, and run as `project-of ~/rust/cargo-liner` prints `cargo-liner` without reaching natedev.

### Phase 3 — Every backfill source record sits in a frozen ledger  · status: todo

#### Work Order

**Goal:** `inventory.py collect` freezes each project's measured source pools (proposal "Backfill" table) into one ledger per project, and `next`, `show`, `mark` and `status` give each record exactly one disposition.

**Spec:**

`scripts/feature_log/inventory.py` (new), read-only on every repository; the ledger is `state_dir()/"backfill"/<project>.jsonl`, written inside `locked()` by temp file and `os.replace`.

A record's claim and its outcome are tagged states, so no combination of loose optional fields can contradict itself:

```python
class Unclaimed(TypedDict):
    state: Literal["unclaimed"]

class ClaimedBy(TypedDict):
    state: Literal["claimed"]
    seat: str

ClaimState = Unclaimed | ClaimedBy

class Undisposed(TypedDict):
    state: Literal["undisposed"]

class NoteOutcome(TypedDict):     # the record's result lives in a note
    state: Literal["represented", "combined"]
    note: str                     # the note's title

class NoNoteOutcome(TypedDict):   # the record gets no note
    state: Literal["internal", "not-current", "unresolved"]
    reason: str

ReviewOutcome = Undisposed | NoteOutcome | NoNoteOutcome

class BackfillSourceRecord(TypedDict):
    id: str            # "<project>:<kind>:<key>"; key = sha, tag, "<path>#<phase heading>", "<path>", "<changelog path>@<version>#<n>"
    project: str
    repo: str          # absolute checkout path
    ref: str           # the 40-hex commit frozen by collect
    kind: Literal["phase", "asbuilt", "changelog", "tag", "merge", "commit"]
    pool: Literal["structured", "commits"]
    title: str         # phase heading, file's first heading, bullet text, tag name, or subject
    date: str          # YYYY-MM-DD: commit or tag date; for a file record, the last commit touching it at ref
    source: str        # "<path>:<line>" or sha
    claim: ClaimState
    outcome: ReviewOutcome
```

Sources, each read at the ref `collect` freezes (`git rev-parse <branch>`):

- **phase** — every `#### As-built` block in a tracked `docs/**/*.md` (`git ls-tree -r --name-only <ref> -- docs`); its title is the nearest `### Phase …` heading above it.
- **asbuilt** — a tracked `docs/**/as-built/*.md` holding no `#### As-built` block (one that holds blocks yields only its phase records).
- **changelog** — each top-level `- ` line under a `## [<version>]` or `## [Unreleased]` heading of the project's changelogs.
- **tag** — `git tag --merged <ref>`.
- **merge** — `git rev-list --first-parent --merges <range>`.
- **commit** — `git rev-list --no-merges <range>`.

Pools (2026-10-10 counts from the proposal, which `collect` prints beside its own):

| Project | structured | commits |
| --- | --- | --- |
| hana | `~/rust/hana_catalyst` `init/catalyst`: phase + asbuilt (200 phase records in 58 files) | `~/rust/hana` `main` merges (124) and `~/rust/hana_catalyst` `origin/main..init/catalyst` merges (285), one record per sha |
| natedev | — | `/etc/nixos` `main`: commits (973), merges (1), tags (1) |
| cargo-liner | `main`: asbuilt + phase (15 files, 11 records), changelog in `crates/*/CHANGELOG.md` (755 bullets), tags (30) | `main`: commits (2,156), merges (76) |
| bevy_brp | `main`: asbuilt + phase (4 files), changelog in `mcp/CHANGELOG.md`, `mcp_macros/CHANGELOG.md`, `extras/CHANGELOG.md` (205 bullets), tags (25) | `main`: commits (2,558), merges (12) |
| obsidian_knife | — | `main`: commits (562), merges (2) |
| nateroids | `main`: asbuilt + phase (2 files) | `main`: commits (553), merges (2) |

CLI:
- `collect --project P` — writes the ledger when absent and prints counts per pool and kind beside the proposal's; when the ledger exists it only prints them (a ledger is frozen once).
- `next --project P --pool POOL [--kind K] [--path PREFIX …] [--claim SEAT] [--limit N]` — undisposed records that are unclaimed or claimed by `SEAT`, oldest first: `<id> | <date> | <kind> | <title> | <source>`. `--path` (repeatable, any one matching) keeps records whose `source` path starts with a prefix; a sha source matches none. `--claim` sets `ClaimedBy(SEAT)` on the listed records. `--limit 0` lists them all.
- `show ID` — the record and its source text: the phase block, the bullet with its version heading, or the commit message with `git show --stat`.
- `mark ID… [--ids-from FILE] [--seat SEAT] --as D (--note TITLE | --reason TEXT)` — `represented` and `combined` need `--note` naming a note that exists in the project's folder; the other three need `--reason`. A record claimed by a seat other than `--seat` is refused, naming its seat. Re-marking replaces the outcome.
- `status --project P [--pool POOL]` — counts per disposition and undisposed, and the notes the ledger names; exit 1 while any record is undisposed.

**Files:**
- `scripts/feature_log/inventory.py` — new.
- `scripts/feature_log/test_inventory.py` — new.

**Seats:** `1 writer + 1 tester` — one new script; its record kinds and commands are concrete enough to test first.
- `impl` — `scripts/feature_log/inventory.py`.
- `test` — `scripts/feature_log/test_inventory.py`, on temp git repos it builds: each record kind (a phase block under its heading, an as-built file without blocks, changelog bullets per version and Unreleased, tags, first-parent merges, non-merge commits); a range excludes `origin/main`; `collect` twice leaves the ledger byte-identical; `next --claim` hides one seat's records from the other; `next --path` keeps only records under its prefixes; `mark` refuses a missing note or reason and a record another seat claimed; no record holds both a note and a reason; `status` exits 1 until all are marked.

**Constraints from prior phases:** Phase 1: notes live at `vault_dir()/"feature-log"/<project>/<title>.md`; import `vault_dir`, `state_dir` and `locked` from `feature_log` (functions read per call; `locked()` is the flock on `state_dir()/"lock"`). Phase 2: none bind this phase.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live: `collect` for all six projects; the phase report gives each pool's count beside the proposal's, with the frozen refs, and names the cause of each difference (commits since 2026-10-10).

### Phase 4 — Hana's structured phase records are in the log  · status: todo

#### Work Order

**Goal:** every record of hana's structured pool has a disposition, and each surviving result is one live note that is true on `init/catalyst` today.

**Spec:** Delegation Context → Backfill procedure, for `--project hana --pool structured`. Hana entries carry `production` (the accepting production's slug, when the record names one) and `unit` (the unit slug from the plan's production header). Current surface for the audit (proposal "Current-behavior check"): current as-built docs, player controls, tool definitions, public crate READMEs and APIs. The running-app check of Hana's visible behavior is in Phase 8, which finishes Hana's audit.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by source path, so each phase block's file stays with one seat: sort hana's structured source files by path and give each seat the directory prefixes holding about half the records. Phase 3's phase review writes the two prefix lists here from the frozen ledger.
- `impl` — claims as `a` with `--path` for the first prefix list.
- `test` — opens as impl; claims as `b` with `--path` for the second prefix list.

**Constraints from prior phases:** Phase 1: `feature_log.py add|supersede|amend|live|check`, `--backfill` (no derived `@Unreleased`), `--separate-result`. Phase 3: `inventory.py next|show|mark|status` and the frozen hana ledger.

**Acceptance gate:** `inventory.py status --project hana --pool structured` exits 0; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits hana's live entries (`feature_log.py live --project hana`) against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition.

### Phase 5 — cargo-liner's as-built files and changelogs are in the log  · status: todo

#### Work Order

**Goal:** every record of cargo-liner's structured pool has a disposition, and each surviving result is one live note that is true on `main` today.

**Spec:** Delegation Context → Backfill procedure, for `--project cargo-liner --pool structured`. As-built records first, then the 755 changelog bullets: duplicates across crates and releases combine into one note; tags and landed commits give dates and links; commits are opened only where those records are unclear. A note gets a historical `crate@version` only when one changelog bullet maps to it exactly; never rewrite an old changelog. `cargo-berth-test-support` is excluded. Current surface: each current crate README, CLI help (`cargo mend --help` and the others, installed binaries), public API, the current Unreleased sections, and user-facing tests.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by crate, by record path.
- `impl` — claims as `a`: `--path crates/cargo-mend --path crates/cargo-port --path crates/tui_pane`.
- `test` — opens as impl; claims as `b`: `--path crates/cargo-berth --path crates/cargo-tile --path crates/cargo-handler --path docs` (the as-built and phase records), then `--kind tag`.

**Constraints from prior phases:** Phase 1: `feature_log.py` verbs and `--backfill`; published cargo-liner crates are `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane`. Phase 3: the frozen cargo-liner ledger.

**Acceptance gate:** `inventory.py status --project cargo-liner --pool structured` exits 0; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits cargo-liner's live entries against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition.

### Phase 6 — bevy_brp's structured records and nateroids' as-built files are in the log  · status: todo

#### Work Order

**Goal:** every record of bevy_brp's structured pool and of nateroids' structured pool has a disposition, and each surviving result is one live note that is true on `main` today.

**Spec:** Delegation Context → Backfill procedure, for `--project bevy_brp --pool structured` and `--project nateroids --pool structured`. bevy_brp: as-built files first, then the 205 changelog bullets, combining synchronized-crate duplicates; the 25 tags are release evidence. Current surface: the workspace README, the three published crate READMEs and public APIs, examples, and user-facing tests; test apps excluded. nateroids: the 2 as-built files seed its log; its commits are Phase 8.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by changelog.
- `impl` — claims as `a`: bevy_brp `--path docs --path mcp/CHANGELOG.md` (as-built records and `mcp` bullets).
- `test` — opens as impl; claims as `b`: bevy_brp `--path extras/CHANGELOG.md --path mcp_macros/CHANGELOG.md`, then bevy_brp `--kind tag`, then nateroids' structured pool.

**Constraints from prior phases:** Phase 1: published bevy_brp crates are `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros`. Phase 3: the frozen bevy_brp and nateroids ledgers.

**Acceptance gate:** `inventory.py status --project bevy_brp --pool structured` and `--project nateroids --pool structured` exit 0; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits bevy_brp's live entries against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition for each project.

### Phase 7 — natedev and obsidian_knife are logged from their commits  · status: todo

#### Work Order

**Goal:** every commit, merge and tag in natedev's and obsidian_knife's pools has a disposition, and each project's current surface has one live entry per qualifying behavior.

**Spec:** Delegation Context → Backfill procedure, for `--pool commits` of both. natedev (`crate: []`): `machines/configuration overview.md` in the vault defines the feature boundaries; walk the 973 commit subjects backwards and combine the commits that built one operator-visible result; `--link` is the nixos commit, and `See it` is an operator command or machine state. Current surface: the overview, active `/etc/nixos` modules, installed operator commands, the rebuilt machine state. obsidian_knife: review all 562 subjects, open a diff only for a possible user-visible result, combine repair and follow-up commits into one note. Current surface: the README, example config, CLI behavior, reports, user-facing tests. Both projects' audits finish here.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/natedev/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/obsidian_knife/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — one project each.
- `impl` — claims as `a`: natedev.
- `test` — opens as impl; claims as `b`: obsidian_knife.

**Constraints from prior phases:** Phase 1: natedev notes take no `--crate`; obsidian_knife's one crate is `obsidian_knife`, unpublished. Phase 3: the frozen natedev and obsidian_knife ledgers.

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits each project's live entries against its current surface and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, and the measured review time.

### Phase 8 — nateroids' commits and Hana's merges are logged, and both audits finish  · status: todo

#### Work Order

**Goal:** every record of nateroids' and hana's commit pools has a disposition, and both projects' current surfaces have one live entry per qualifying behavior, checked in the running apps.

**Spec:** Delegation Context → Backfill procedure, for `--pool commits` of both. nateroids: review the 553 subjects and combine commits by playable result. hana: the 124 `main` merges and 285 `init/catalyst` merges, looking only for results no phase record covered (Phase 4). Current surfaces: hana — as-built docs, player controls, tool definitions, public crate READMEs and APIs, and the running app; nateroids — as-built docs, controls, playable behavior, README, and a play check. Visible behavior that docs, definitions and tests leave unsettled is checked in one batched run of each app, launched with the BRP tools on the unit's port 15798 (production doc 31df9c1) from a detached worktree at the phase's audit ref (Backfill procedure step 3).

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — one project each.
- `impl` — claims as `a`: hana.
- `test` — opens as impl; claims as `b`: nateroids.

**Constraints from prior phases:** Phase 4 logged hana's structured pool; Phase 6 seeded nateroids from its as-built files. Phase 3: the frozen ledgers.

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits each project's live entries against its current surface, using the running-app check above, and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, and the measured review time.

### Phase 9 — cargo-liner's and bevy_brp's commits are dispositioned, and both audits finish  · status: todo

#### Work Order

**Goal:** every commit and merge in cargo-liner's and bevy_brp's pools has a disposition, and both projects' current surfaces have one live entry per qualifying behavior.

**Spec:** Delegation Context → Backfill procedure, for `--pool commits` of both. Most commits belong to a result already logged (`combined`) or are internal; inspect a commit only where Phases 5 and 6 left a gap, and mark the rest in bulk with `mark --ids-from`. Current surfaces as in Phases 5 and 6, now complete: every qualifying behavior has one live entry, including behavior the changelogs missed.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — one project each.
- `impl` — claims as `a`: cargo-liner.
- `test` — opens as impl; claims as `b`: bevy_brp.

**Constraints from prior phases:** Phase 5 and Phase 6 logged the structured pools of both projects. Phase 3: the frozen ledgers.

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. The phase's blind review (Backfill procedure step 8) audits each project's live entries against its current surface and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, the measured review time, and the backfill's total notes across the six projects.

### Phase 10 — A published crate's changelog section comes from the log  · status: todo

**Blocked by:** G2 — live logging has written a real `@Unreleased` entry for a published crate.

#### Work Order

**Goal:** `feature_log.py changelog` prints a Keep a Changelog section for one published crate and version from the stored summaries, unchanged, and `/release` shows it beside the hand-written section and stamps the version into the log after publishing.

**Spec:** (proposal "Changelog generation")

- `changelog --crate X --version V` (V a version or `Unreleased`): selects every note, live or superseded, whose `crate` holds X and whose `release` holds `X@V`; prints `## [V]`, then one `### <heading>` per non-empty heading in the order Added, Changed, Deprecated, Removed, Fixed, Security (`feature` → Added, `fix` → Fixed, `change` and `perf` → Changed, `deprecation` → Deprecated, `removal` → Removed, `security` → Security), each followed by `- <summary>` lines sorted by summary. An unpublished crate fails with exit 2 and prints nothing.
- `changelog --crate X --version Unreleased --compare PATH` — compares that output with the `## [Unreleased]` section of the CHANGELOG at PATH (bullet sets per heading); prints `match` (exit 0) or the differences (exit 1). PATH is read on the calling host: where `main` forwards to natedev, it forwards `changelog` without `--compare` and compares the returned section locally.
- `release --crate X --version V` — replaces `X@Unreleased` with `X@V` in every live note holding it, and drops `X@Unreleased` from superseded notes (my call: a behavior replaced before a release never shipped, so the generator needs no state filter); appends one `ReleaseEvent` (`at`, `verb: "release"`, `project`, `crate`, `version`, `titles`: the notes it changed), which joins the `FeatureLogEvent` union; a repeat prints `unchanged`. `digest` shows a release event as `= X@V (<n> notes)`.
- `commands/release.md`, in both changelog-finalizing blocks — `<FinalizeOnBase/>` (at 485) and `<FinalizeOnHotfixBranch/>` (at 869), beside the `## [Unreleased]` display: for each published crate being released, run `changelog --compare` and show `match` or the differences to the release owner, who reconciles them with `amend` or in the changelog before finalizing. A crate whose output differs is reported, not blocked (the proposal: connect a crate's release path only after its output matches).
- `commands/release.md`, a stamp step after `<PublishPhases/>` in all three mode sequences (293-317), skipped on a dry run: for each crate STEP 6 published, `release --crate <crate> --version ${VERSION}`. It runs only after publishing succeeded, so a failed or dry-run release stamps nothing.

**Files:**
- `scripts/feature_log/feature_log.py` — `changelog`, `release`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/release.md` — `<FinalizeOnBase/>`, `<FinalizeOnHotfixBranch/>`, and the stamp step after `<PublishPhases/>` (no owner in the production).

**Seats:** `1 writer + 1 tester` — one script change and one command step; the heading map and selection rules are concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`, `commands/release.md`.
- `test` — `scripts/feature_log/test_feature_log.py`: fixture notes covering every kind, a result shared by two crates, a superseded entry with a released version (kept) and one with `@Unreleased` (dropped by `release`), and an unpublished crate (no output); the proposal's cargo-mend 0.23.0 example prints exactly its section; `--compare` matches and differs, and on another host forwards `changelog` without `--compare` and compares locally; `release` and its repeat; `digest` shows the release event.

**Constraints from prior phases:** Phase 1: notes carry `release` values `crate@Unreleased` (derived without `--backfill`) or `crate@version`. Phase 2: `FeatureLogEvent` is a union of per-verb TypedDicts discriminated by `verb`; `digest` reads events by line position and leaves out backfill events; every verb except `project-of` forwards to natedev from another host. Phases 4–9: backfilled notes carry a historical version only on an exact changelog-bullet match.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live: `changelog --compare` against each published crate's current `CHANGELOG.md`; the phase report names the crates that match and the differences of the rest.
