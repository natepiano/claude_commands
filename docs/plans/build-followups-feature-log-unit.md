# Feature log: one note per current result, written only by a script

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Builds `~/rust/hanadocs/proposals/feature-log.md`: six project folders and seven Bases in the vault, `scripts/feature_log/feature_log.py` as the only writer of feature notes, the close-out steps in `/showrunner:produce`, `/validate_and_push` and natedev's per-change loop, a backfill of all six projects through a read-only inventory, then the changelog export.

> **As-built disposition: create**

> **Production: build-followups** — unit `feature-log-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-10 12:39 PDT

feature log yes (the user to hana, 2026-10-10): build ~/rust/hanadocs/proposals/feature-log.md, phased as its Phases section says. Vault folders and Bases go in ~/rust/hanadocs; the script and command steps go in ~/.claude through this unit's branch. Tell the showrunner when the /showrunner:produce step (Phase 2) is live; hana starts using it then.

## Delegation Context

- **Project:** `~/.claude` config repo plus the hanadocs vault. This plan builds the design in `/home/natepiano/rust/hanadocs/proposals/feature-log.md` (the user, 2026-10-10: "feature log yes"). Worktree `/home/natepiano/worktrees/claude-build-followups-feature-log-unit`, branch `build-followups-feature-log-unit` (unit `feature-log-unit-unit`); name both in every dispatch.
- **Stack:** Python 3.10+ standard library only (`argparse`, `fcntl`, `json`, `os`, `re`, `shlex`, `socket`, `subprocess`, `datetime`, `zoneinfo`, `unittest`); no YAML library is installed (`import yaml` fails under `scripts/lib/py`), so the script reads and writes frontmatter with its own line parser. Obsidian 1.14 Bases (`.base` YAML). Markdown command files, written per `commands/succinct_style.md`.
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
  - `commands/release.md` — STEP 4 477-510 (shows `## [Unreleased]` at 485; `finalize_changelogs.sh` at 508); `scripts/release/finalize_changelogs.sh` 5, 27-37.
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
  1. A seat works only the records it claims: `inventory.py next --project <p> --pool <pool> --claim <seat> [--kind <k>] [--limit 25]`, then `inventory.py show <id>` for a record's source text.
  2. Decide whether the record holds a user-visible result (proposal "What gets logged": the seven kinds). Refactor, test, build, CI and docs-only work is `internal` and gets no note.
  3. Check current behavior at the ledger's frozen ref of the default branch (hana: `init/catalyst`): current docs and public interfaces first, then tests, then implementation; run it when practical. A behavior that no longer exists is `not-current`. A replaced behavior is written only in its current form.
  4. Before writing, `feature_log.py live --project <p> [--crate <c>] [--area <a>] --grep <word>` finds the entries the result may affect. Still wholly true: mark the record `combined` into that entry and `amend --evidence` it. Replaced: `supersede --backfill`. New: `add --backfill`.
  5. `--backfill` calls pass `--completed` (the date of the accepting merge, tag or commit) and `--link` (that commit). `--release crate@version` is passed only when exactly one existing changelog bullet maps to the entry; otherwise `release` stays `[]`. A summary for a published crate reads unchanged as a changelog bullet.
  6. Mark every record: `inventory.py mark <id>… --as represented|combined --note "<title>"`, or `--as internal|not-current|unresolved --reason "<why>"`. `unresolved`: the sources cannot support a true summary, completion date and `See it`; no note is written.
  7. The two seats of a phase claim disjoint records. A result found by both is written once by the first and `combined` by the second.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 1's live Base check (its code and tests go ahead) | the user turns on Obsidian on natedev → Settings → General → Advanced → Command line interface | `obsidian-cli help` lists commands instead of the "not enabled" line |
| G2 | Phase 10 | live logging has written a real `@Unreleased` entry: an `add` or `supersede` without `--backfill` for a published crate | `events.jsonl` holds an event with `"backfill": false` and a `release` value ending `@Unreleased` |

After Phase 2 reaches `~/.claude` main, the unit director tells the showrunner that the `/showrunner:produce` step is live, so hana starts using it (the user's words in Source), and sends natedev the README step that Phase 2's Spec gives.

## Phases

### Phase 1 — The vault holds six folders and seven Bases, and one script writes every note  · status: todo

#### Work Order

**Goal:** `feature_log.py add`, `supersede`, `amend`, `live` and `check` write and read feature notes with every mechanical field derived and every bad call refused, and the seven Bases show live and superseded entries per project.

**Spec:**

`scripts/feature_log/feature_log.py` (new). Paths: `VAULT = Path(os.environ.get("FEATURE_LOG_VAULT") or Path.home() / "rust/hanadocs")`; a note is `VAULT / "feature-log" / <project> / f"{title}.md"`; `STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "claude/feature-log"`. `today()` returns the machine's local date (the Linter's date); tests patch it.

Projects (proposal "Storage", measured 2026-10-10), one constant table:

```python
class ProjectSpec(NamedTuple):
    repo: str                    # GitHub slug for --link
    crates: frozenset[str]       # workspace package names; empty for natedev
    published: frozenset[str]    # crates confirmed on crates.io

PROJECTS: dict[str, ProjectSpec] = {
    "hana": ProjectSpec("natepiano/hana", frozenset({
        "fairy_dust", "hana", "hana_animation", "hana_catalyst", "hana_clerestory", "hana_conduit",
        "hana_diegetic", "hana_effects", "hana_kana", "hana_lading", "hana_lagrange", "hana_liminal",
        "hana_metrics", "hana_mimesis_tools", "hana_organon", "hana_origami", "hana_prosody",
        "hana_rigging", "hana_rigging_scripted", "hana_rubric", "hana_sdf", "hana_tool_macros",
        "hana_tool_schema", "hana_tools", "hana_valence", "hana_video", "hana_works"}),
        frozenset({"hana_clerestory", "hana_kana", "hana_lagrange", "hana_liminal", "hana_rigging", "hana_rubric"})),
    "natedev": ProjectSpec("natepiano/nixos", frozenset(), frozenset()),
    "cargo-liner": ProjectSpec("natepiano/cargo-liner", frozenset({
        "cargo-berth", "cargo-berth-test-support", "cargo-handler", "cargo-mend", "cargo-port", "cargo-tile", "tui_pane"}),
        frozenset({"cargo-mend", "cargo-port", "cargo-tile", "tui_pane"})),
    "bevy_brp": ProjectSpec("natepiano/bevy_brp", frozenset({
        "bevy_brp_extras", "bevy_brp_mcp", "bevy_brp_mcp_macros", "bevy_brp_test_apps", "test-app-a", "test-app-b"}),
        frozenset({"bevy_brp_extras", "bevy_brp_mcp", "bevy_brp_mcp_macros"})),
    "obsidian_knife": ProjectSpec("natepiano/obsidian_knife", frozenset({"obsidian_knife"}), frozenset()),
    "nateroids": ProjectSpec("natepiano/nateroids", frozenset({"nateroids"}), frozenset()),
}
KINDS = ("feature", "fix", "change", "perf", "deprecation", "removal", "security")
```

A note's fields:

```python
class Note(TypedDict):
    title: str                   # = file name without .md
    project: str
    kind: str
    summary: str
    area: str | None             # bare name; written "[[name]]"
    completed_date: str          # YYYY-MM-DD
    crate: list[str]
    link: str                    # https://github.com/<repo>/commit/<40 hex>
    production: str | None       # hana only
    unit: list[str]              # hana only
    release: list[str]           # "<crate>@Unreleased" | "<crate>@<version>"
    replaced_by: list[str]       # titles of the replacing notes
    state: Literal["live", "superseded"]
    date_created: str
    date_modified: str
    behavior: str
    see: str
    evidence: list[str]
```

Written layout — the proposal's "Hana production example" byte for byte in its frontmatter: keys in ascending order (`area, completed_date, crate, date_created, date_modified, kind, link, production, project, release, replaced_by, return, state, summary, tags, unit`); `area`, `production` and `unit` only when set; an empty list as `[]`; `tags: [feature-log]`; any other non-empty list as block items `  - <item>` sorted ascending; links and dates as `"[[x]]"`; `return` = `"[[feature-log - <project>.base]]"` then `"[[feature-log.base]]"`; `link` bare; any other string bare when it matches `^[A-Za-z0-9][^:#"'\n]*$` without a trailing space, else `json.dumps` (a valid YAML double-quoted string). The body is the behavior paragraph, a blank line, `## See it`, a blank line, the check, a blank line, `## Evidence`, a blank line, one `- <line>` per evidence item, and a final newline.

The parser reads only what this writer and the Linter produce: `key: value`, `key: "…"`, `key: '…'`, `key: [a, b]`, `key: []`, and `key:` followed by `  - item` lines. A duplicate key, an unknown key or any other form is an error.

Subcommands (each prints one line per file it writes: `added: <vault-relative path>`, `superseded: <old> → <new>`, `amended: <path> (<fields>)`, `unchanged: <path>`):

- `add --project P --title T --kind K --summary S --behavior B --see C --link L --evidence E [--evidence …] [--crate X …] [--area A] [--completed YYYY-MM-DD] [--production SLUG] [--unit U …] [--release X@V …] [--backfill] [--separate-result]`
- `supersede --old T_OLD [--old …]` plus every `add` argument.
- `amend --project P --title T` with any of `--summary --behavior --see --area --kind --completed`, `--crate X …` (replaces the list; release items of removed crates drop), `--evidence E …` (appends). It never changes the title, link, state or `replaced_by`.
- `live --project P [--crate X] [--area A] [--kind K] [--grep TEXT]` — one line per live note, newest `completed_date` first: `<completed> <kind> <title> — <summary> [<crates>] (<area>)`. `--grep` matches title, summary and behavior, case-insensitive. This is how an agent "opens the project's live Base" (proposal "Current truth and replacement").
- `check` — validates every note in the six folders: it parses, required keys are present, values have their forms, `state: live` has `replaced_by: []` and `superseded` has at least one, every `replaced_by` target exists in the same project, the file name equals the normalized title, and `tags` and `return` are exact. Prints `ok: <n> notes` (exit 0) or one `<path>: <reason>` per error (exit 1).

Derived fields: the file name is the title lowercased, `-` turned into a space, every character outside `a-z0-9 _` dropped, runs of spaces collapsed and ends stripped (the `commands/issue.md:48` rule; `_` is kept for crate names). `--link` takes a 40-hex sha (expanded to `https://github.com/<repo>/commit/<sha>`) or that URL. `date_created` and `date_modified` are `today()`; `completed_date` defaults to it. `state: live`, `replaced_by: []`, `tags`, `return` and `project` are fixed. Without `--backfill`, `release` is `<crate>@Unreleased` for each `--crate` in the project's published set; with `--backfill`, only `--release` values. `--release` values are added to the derived ones.

Rejections (exit 2, one stderr line `feature_log: <reason>`, nothing written): an unknown project or kind; a crate outside the project; no crate for a project other than natedev, or any crate for natedev; a release whose crate is not in `--crate` or not published, or whose version is neither `Unreleased` nor `\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?`; an empty summary, behavior, see or link, or no `--evidence`; a summary starting `- ` or holding a newline; `--production` or `--unit` outside hana; a title that normalizes to empty or to more than 50 characters; a file of that name in the project with a different link (make the title more specific); a live note in the project with this link under another title, unless `--separate-result` (one accepted change, several results); an `--old` that is not a live note of the project, or equals the new title.

Repeats are safe: a note with the same title and link and the same fields prints `unchanged`; with other fields the call fails with `use amend`. A repeated `supersede` finishes any `--old` not yet superseded.

`supersede` under one lock: writes the new note, then for each `--old` sets `state: superseded`, adds `[[<new title>]]` to `replaced_by` (sorted, no duplicates) and sets `date_modified`.

Every write command takes an exclusive `fcntl.flock` on `STATE/lock`, writes each file as `.<name>.tmp` in its target folder and `os.replace`s it, creates folders on first use, and appends one line to `STATE/events.jsonl`: `{"at": "<ISO-8601>", "verb": "add|supersede|amend", "project", "title", "kind", "summary", "release": [...], "backfill": bool, "old": [...], "fields": [...]}` (`fields` names what an amend changed).

Vault files (vault config, not notes; the impl seat writes them):

- `feature-log/<project>/` for the six projects, empty.
- `feature-log.base` with the proposal's "Bases" views: **Latest**, **By project**, **By crate**, **By kind**, **Superseded**. Every filter starts `file.hasTag("feature-log")`; the four live views add `state == "live"`, **Superseded** adds `state == "superseded"`. Live views show `file.name, summary, kind, crate, completed_date, project, area, link, production, unit`; **Superseded** shows `file.name, summary, kind, crate, completed_date, project, area, link, replaced_by, release`. Every view sorts `completed_date` DESC; the three "By" views group by `note.project`, `note.crate` and `note.kind` with the table view's group-by key, whose exact spelling the seat takes from `obsidian-cli help` (G1) or Obsidian's Bases documentation.
- `feature-log - <project>.base` for each project: **Current** (`file.hasTag("feature-log")`, `project.contains(link("<project>"))`, `state == "live"`; `crate`, `area` and `kind` visible) and **Superseded** (`state == "superseded"`; shows `replaced_by`). The six files differ only in the project link.
- No `columnSize`; Obsidian writes it when the user resizes a column.

Live Base check (after G1), with `XDG_STATE_HOME` set to a temp dir so the state and events stay out of the real log: write through the script into the real vault `natedev` note `feature log check one`, supersede it with `feature log check two`, amend two's evidence, and write `hana` note `feature log check three` (each `--link` a 40-hex sha of zeros ending in its number). `obsidian-cli base:query` (syntax from `obsidian-cli help`) then shows: **Latest** and natedev **Current** list `two` and not `one`; natedev **Superseded** lists `one` with `replaced_by` `[[feature log check two]]`; hana **Current** lists only `three`; natedev **Current** does not list `three`. Then remove the three fixture files with `rm`: they are fixtures, not results. If an unresolved project link (`[[natedev]]`) fails `project.contains(link(…))`, every project Base filters on `file.inFolder("feature-log/<project>")` instead (my call: the folder is the project key; one line per file to revert).

**Files:**
- `scripts/feature_log/feature_log.py` — new.
- `scripts/feature_log/test_feature_log.py` — new.
- `pyrightconfig.json` — a `{"root": "scripts/feature_log", "extraPaths": ["scripts/feature_log"]}` entry, only if basedpyright cannot resolve the test's import without it.
- `/home/natepiano/rust/hanadocs/feature-log/` — the six project folders.
- `/home/natepiano/rust/hanadocs/feature-log.base` — new.
- `/home/natepiano/rust/hanadocs/feature-log - hana.base`, `/home/natepiano/rust/hanadocs/feature-log - natedev.base`, `/home/natepiano/rust/hanadocs/feature-log - cargo-liner.base`, `/home/natepiano/rust/hanadocs/feature-log - bevy_brp.base`, `/home/natepiano/rust/hanadocs/feature-log - obsidian_knife.base`, `/home/natepiano/rust/hanadocs/feature-log - nateroids.base` — new.

**Seats:** `1 writer + 1 tester` — one new script and its vault files; the Spec's layout and rejections are concrete enough to test before the code exists.
- `impl` — `scripts/feature_log/feature_log.py`, the vault folders and Bases, `pyrightconfig.json`.
- `test` — `scripts/feature_log/test_feature_log.py`: the exact frontmatter bytes of the proposal's two examples (Hana with `production` and `unit`; natedev with `crate: []`) and the body layout; every rejection above, each leaving the temp vault unchanged; derived release values with and without `--backfill`; file-name normalization and the 50-character limit; repeats print `unchanged`, a changed repeat fails; `--separate-result`; `supersede` and its repeat after an interrupted run (old note still live); `amend` field rules; `live` filters and order; `check` on a hand-broken fixture (missing key, unknown key, live with `replaced_by`, dangling `replaced_by`); no `.tmp` left behind; one `events.jsonl` line per write.

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live: `feature_log.py check` on the real vault prints `ok: 0 notes` before the fixtures and after their removal. After G1, the Live Base check above passes; the checkpoint notice carries the `obsidian-cli base:query` output for each view checked in place of shots.

### Phase 2 — Hana, single-session and natedev close-outs write entries, and a digest reports them  · status: todo

#### Work Order

**Goal:** a Hana merge proposes its entry beside its change-review row and writes it only when the row closes `works` or `fine`; a push to a default branch and a natedev rebuild each close out with an entry or a stated reason; a call made on the Mac writes natedev's vault; `digest` reports the silent entries on request.

**Spec:**

`scripts/feature_log/feature_log.py` additions:

- **One vault.** When the short host name (`socket.gethostname()` up to the first `.`) is not `natedev` and `FEATURE_LOG_VAULT` is unset, `main` runs `["ssh", "natedev", "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py " + shlex.join(argv)]`, passes its output through and exits with its status (my call: natedev's vault is the one Obsidian has open, and the Mac clone is 25 days behind; one function to change if the user wants another home). The runner is a parameter, so tests assert the command without running `ssh`.
- `propose --review ROW` with every `add` argument, plus `--old T …` when the result replaces live entries. `ROW` matches `^[AC]\d+$`. It runs every `add` / `supersede` validation, including the title, link and `--old` checks against the vault, writes no note, and saves `STATE/proposals/<ROW>.json`: `{"row", "verb": "add"|"supersede", "args": {…}, "status": "open", "at"}`. A repeat replaces an open proposal (the corrected-fields path); a landed or dropped one fails.
- `add --review ROW` / `supersede --review ROW` land the open proposal with its saved arguments. The verb must match the proposal's (a mismatch fails, naming the right verb). Success sets `status: landed` and `note: <title>` and prints `[[<title>]]` for the Reviewed row. A landed proposal prints `unchanged`. No proposal fails with `no proposal for <ROW>; run propose first`.
- `drop --review ROW --verdict TEXT` closes an open proposal without a note (`status: dropped`, `verdict`); a repeat prints `unchanged`.
- `digest [--since YYYY-MM-DD]` prints the events after the cursor in `STATE/digest.json` (or from `--since`), grouped by project in the order of `PROJECTS`: `+ <title> — <summary>` for an add, `→ <old> ⇒ <new>` for a supersede, `~ <title> (<fields>)` for an amend; it ends `<n> changes since <date HH:MM PDT>` and advances the cursor (not with `--since`). With none: `feature log: nothing new since <…>`.
- `project-of [PATH]` maps `git -C PATH remote get-url origin` (`git@github.com:<slug>` or `https://github.com/<slug>`, with or without `.git`) to the project whose `repo` is that slug and prints it, or `none`; exit 0 either way.

`commands/showrunner/produce.md` (proposal "Hana units"), applied when the production's repository is a Hana checkout (`~/rust/hana` or `~/rust/hana_catalyst`):

- `<ChangeReviewFeatureLog/>`, a step of `<MergeCheckpoint/>` after `merge_checkpoint.py` has accepted the merge (step 4), before step 5: (1) for each distinct user-visible result of the merge, add a `## To review` row to `~/rust/hanadocs/change-review.md` (next number; merge date; Mac build when it passes; how to check — a behavioral check for a result that is not visual); internal-only work gets no row, and the production log gets `feature log: none — <reason>`; (2) beside each row, `feature_log.py propose --review <row>` with project `hana`, crates, kind, title, summary, behavior, see, the merge sha as `--link`, evidence (`Showrunner merge <sha>, from checkpoint <sha>.`), `--production <slug>`, `--unit <unit>`, and `--old` for each live entry it replaces (found with `live`); (3) the merge is not complete until every row has a valid proposal or the log holds the no-entry reason.
- `<CloseChangeReview/>`, when the user answers a row: a verdict beginning `works` or `fine` → `add --review <row>` (or `supersede --review <row>` when the proposal names `--old`); a missing proposal is proposed first; a wrong meaning is fixed by rerunning `propose`; a script failure leaves the row open until the call is fixed. Any other verdict → `drop --review <row> --verdict "<verdict>"`; the fixing merge gets a new row and a new proposal. Then move the row to `## Reviewed` with its verdict and date; a landed row's Change cell may carry the printed `[[<title>]]`. No message reports the entry.

`commands/validate_and_push.md`: `<FeatureLogCloseOut/>` (proposal "Single-session projects"), after "All green" and before the success summary, also after `push_pr_branch_and_merge.sh` lands the PR. When `feature_log.py project-of .` prints `cargo-liner`, `bevy_brp`, `obsidian_knife` or `nateroids` and the commit landed on the default branch, the session that did the work checks `live` and runs `add`, `supersede`, or records `none — <reason>`. The summary block gains `Feature log    added <title> | superseded <old> → <title> | none — <reason>`. The skill is not complete until the script succeeds or the reason is recorded. Hana results close at change review and natedev's in its README, so both print nothing here.

`commands/feature_log.md` (new; my call: a user asking for "feature log digest" in any session needs a command to find): `/feature_log digest` runs `digest`; `/feature_log live <project> [--crate …]` runs `live`; one line points writers to the three close-out steps.

natedev's README (sent to natedev to commit in `/etc/nixos`; this unit does not edit it): a sixth per-change-loop step after line 87 — **Feature log close-out.** After `rebuild` succeeded and the activated revision (the last line of `~/.local/state/rebuild/generations.log`, without `-dirty`) is on `origin/main`, the session that made the change runs `~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py add --project natedev …` (or `supersede`), or states `feature log: none — <reason>`, before it calls the change complete. `rebuild` and `update` never call the script; a failed activation, a dirty revision, `boot` and `test` never reach this step.

**Files:**
- `scripts/feature_log/feature_log.py` — forwarding, `propose`, `drop`, `--review`, `digest`, `project-of`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/showrunner/produce.md` — `<ChangeReviewFeatureLog/>` and `<CloseChangeReview/>` (also touches; owner stalls-unit, run done).
- `commands/validate_and_push.md` — `<FeatureLogCloseOut/>`.
- `commands/feature_log.md` — new.

**Seats:** `1 writer + 1 tester` — one script and three short command edits that follow it; the subcommand rules are concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`, `commands/showrunner/produce.md`, `commands/validate_and_push.md`, `commands/feature_log.md`, and the README step's text in the phase report.
- `test` — `scripts/feature_log/test_feature_log.py`: forwarding builds the `ssh natedev` command on another host and stays local on natedev or with `FEATURE_LOG_VAULT`; `propose` writes no note and refuses a landed row; `add --review` writes exactly one note and its repeat writes none; a verb mismatch fails; `drop` writes none; `digest` lists adds, supersedes and amends by project, advances its cursor, and prints the empty line; `project-of` maps both URL forms and prints `none` for an unknown slug.

**Constraints from prior phases:** Phase 1 built `feature_log.py` with `add`, `supersede`, `amend`, `live`, `check`, the `STATE/lock`, and `STATE/events.jsonl` lines carrying `backfill`; `digest` reads those lines.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live, with `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` on temp dirs: `propose --review C900` writes no note; `add --review C900` writes one and its repeat none; `propose --review C901` then `drop` writes none; `digest` lists only C900's entry. From the Mac, a copy of the worktree's `feature_log.py` run as `live --project natedev` forwards to natedev and prints its list.

### Phase 3 — Every backfill source record sits in a frozen ledger  · status: todo

#### Work Order

**Goal:** `inventory.py collect` freezes each project's measured source pools (proposal "Backfill" table) into one ledger per project, and `next`, `show`, `mark` and `status` give each record exactly one disposition.

**Spec:**

`scripts/feature_log/inventory.py` (new), read-only on every repository; the ledger is `STATE/backfill/<project>.jsonl`, written under `STATE/lock` by temp file and `os.replace`.

```python
class Record(TypedDict):
    id: str            # "<project>:<kind>:<key>"; key = sha, tag, "<path>#<phase heading>", "<path>", "<changelog path>@<version>#<n>"
    project: str
    repo: str          # absolute checkout path
    ref: str           # the 40-hex commit frozen by collect
    kind: Literal["phase", "asbuilt", "changelog", "tag", "merge", "commit"]
    pool: Literal["structured", "commits"]
    title: str         # phase heading, file's first heading, bullet text, tag name, or subject
    date: str          # YYYY-MM-DD: commit or tag date; for a file record, the last commit touching it at ref
    source: str        # "<path>:<line>" or sha
    owner: str | None
    disposition: Literal["represented", "combined", "internal", "not-current", "unresolved"] | None
    note: str | None
    reason: str | None
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
- `next --project P --pool POOL [--kind K] [--claim SEAT] [--limit N]` — undisposed records that are unowned or owned by `SEAT`, oldest first: `<id> | <date> | <kind> | <title> | <source>`; `--claim` sets `owner` on the listed records.
- `show ID` — the record and its source text: the phase block, the bullet with its version heading, or the commit message with `git show --stat`.
- `mark ID… [--ids-from FILE] --as D (--note TITLE | --reason TEXT)` — `represented` and `combined` need `--note` naming a note that exists in the project's folder; the other three need `--reason`. Re-marking replaces the disposition.
- `status --project P [--pool POOL]` — counts per disposition and undisposed, and the notes the ledger names; exit 1 while any record is undisposed.

**Files:**
- `scripts/feature_log/inventory.py` — new.
- `scripts/feature_log/test_inventory.py` — new.

**Seats:** `1 writer + 1 tester` — one new script; its record kinds and commands are concrete enough to test first.
- `impl` — `scripts/feature_log/inventory.py`.
- `test` — `scripts/feature_log/test_inventory.py`, on temp git repos it builds: each record kind (a phase block under its heading, an as-built file without blocks, changelog bullets per version and Unreleased, tags, first-parent merges, non-merge commits); a range excludes `origin/main`; `collect` twice leaves the ledger byte-identical; `next --claim` hides one seat's records from the other; `mark` refuses a missing note or reason; `status` exits 1 until all are marked.

**Constraints from prior phases:** Phase 1: notes live at `VAULT/feature-log/<project>/<title>.md`; `STATE` and its `lock` come from `feature_log.py` (import its `VAULT`, `STATE` and `locked`). Phase 2: none bind this phase.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live: `collect` for all six projects; the phase report gives each pool's count beside the proposal's, with the frozen refs, and names the cause of each difference (commits since 2026-10-10).

### Phase 4 — Hana's structured phase records are in the log  · status: todo

#### Work Order

**Goal:** every record of hana's structured pool has a disposition, and each surviving result is one live note that is true on `init/catalyst` today.

**Spec:** Delegation Context → Backfill procedure, for `--project hana --pool structured`. Hana entries carry `production` (the accepting production's slug, when the record names one) and `unit` (the unit slug from the plan's production header). Current surface for the audit (proposal "Current-behavior check"): current as-built docs, player controls, tool definitions, public crate READMEs and APIs. The running-app check of Hana's visible behavior is in Phase 8, which finishes Hana's audit.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by source file: records from as-built files sorting before the median path go to `impl`, the rest to `test`.
- `impl` — claims as `a`: the first half of the files.
- `test` — opens as impl; claims as `b`: the second half.

**Constraints from prior phases:** Phase 1: `feature_log.py add|supersede|amend|live|check`, `--backfill` (no derived `@Unreleased`), `--separate-result`. Phase 3: `inventory.py next|show|mark|status` and the frozen hana ledger.

**Acceptance gate:** `inventory.py status --project hana --pool structured` exits 0; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits hana's live entries (`feature_log.py live --project hana`) against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition.

### Phase 5 — cargo-liner's as-built files and changelogs are in the log  · status: todo

#### Work Order

**Goal:** every record of cargo-liner's structured pool has a disposition, and each surviving result is one live note that is true on `main` today.

**Spec:** Delegation Context → Backfill procedure, for `--project cargo-liner --pool structured`. As-built records first, then the 755 changelog bullets: duplicates across crates and releases combine into one note; tags and landed commits give dates and links; commits are opened only where those records are unclear. A note gets a historical `crate@version` only when one changelog bullet maps to it exactly; never rewrite an old changelog. `cargo-berth-test-support` is excluded. Current surface: each current crate README, CLI help (`cargo mend --help` and the others, installed binaries), public API, the current Unreleased sections, and user-facing tests.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/cargo-liner/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by crate, by record path.
- `impl` — claims as `a`: `cargo-mend`, `cargo-port`, `tui_pane` records.
- `test` — opens as impl; claims as `b`: `cargo-berth`, `cargo-tile`, `cargo-handler`, tags and workspace-level records.

**Constraints from prior phases:** Phase 1: `feature_log.py` verbs and `--backfill`; published cargo-liner crates are `cargo-mend`, `cargo-port`, `cargo-tile`, `tui_pane`. Phase 3: the frozen cargo-liner ledger.

**Acceptance gate:** `inventory.py status --project cargo-liner --pool structured` exits 0; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits cargo-liner's live entries against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition.

### Phase 6 — bevy_brp's structured records and nateroids' as-built files are in the log  · status: todo

#### Work Order

**Goal:** every record of bevy_brp's structured pool and of nateroids' structured pool has a disposition, and each surviving result is one live note that is true on `main` today.

**Spec:** Delegation Context → Backfill procedure, for `--project bevy_brp --pool structured` and `--project nateroids --pool structured`. bevy_brp: as-built files first, then the 205 changelog bullets, combining synchronized-crate duplicates; the 25 tags are release evidence. Current surface: the workspace README, the three published crate READMEs and public APIs, examples, and user-facing tests; test apps excluded. nateroids: the 2 as-built files seed its log; its commits are Phase 8.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/bevy_brp/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — split by changelog.
- `impl` — claims as `a`: bevy_brp as-built records and `mcp/CHANGELOG.md` bullets.
- `test` — opens as impl; claims as `b`: `extras/CHANGELOG.md` and `mcp_macros/CHANGELOG.md` bullets, bevy_brp tags, and nateroids' structured pool.

**Constraints from prior phases:** Phase 1: published bevy_brp crates are `bevy_brp_extras`, `bevy_brp_mcp`, `bevy_brp_mcp_macros`. Phase 3: the frozen bevy_brp and nateroids ledgers.

**Acceptance gate:** `inventory.py status --project bevy_brp --pool structured` and `--project nateroids --pool structured` exit 0; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits bevy_brp's live entries against the current surface above and finds no uncovered qualifying behavior in it. The phase report gives the note count and the count per disposition for each project.

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

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits each project's live entries against its current surface and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, and the measured review time.

### Phase 8 — nateroids' commits and Hana's merges are logged, and both audits finish  · status: todo

#### Work Order

**Goal:** every record of nateroids' and hana's commit pools has a disposition, and both projects' current surfaces have one live entry per qualifying behavior, checked in the running apps.

**Spec:** Delegation Context → Backfill procedure, for `--pool commits` of both. nateroids: review the 553 subjects and combine commits by playable result. hana: the 124 `main` merges and 285 `init/catalyst` merges, looking only for results no phase record covered (Phase 4). Current surfaces: hana — as-built docs, player controls, tool definitions, public crate READMEs and APIs, and the running app; nateroids — as-built docs, controls, playable behavior, README, and a play check. Visible behavior that docs, definitions and tests leave unsettled is checked in one batched run of each app, launched with the BRP tools on the unit's port 15798 (production doc 31df9c1) from a detached worktree at the ledger's frozen ref.

**Files:**
- `/home/natepiano/rust/hanadocs/feature-log/hana/` — notes, through `feature_log.py` only.
- `/home/natepiano/rust/hanadocs/feature-log/nateroids/` — notes, through `feature_log.py` only.

**Seats:** `2 writers` — one project each.
- `impl` — claims as `a`: hana.
- `test` — opens as impl; claims as `b`: nateroids.

**Constraints from prior phases:** Phase 4 logged hana's structured pool; Phase 6 seeded nateroids from its as-built files. Phase 3: the frozen ledgers.

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits each project's live entries against its current surface, including the running app, and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, and the measured review time.

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

**Acceptance gate:** `inventory.py status` exits 0 for both projects; `feature_log.py check` prints `ok`. A review seat that wrote no note in this phase audits each project's live entries against its current surface and finds no qualifying behavior without one live entry. The phase report gives the note count, the count per disposition, the measured review time, and the backfill's total notes across the six projects.

### Phase 10 — A published crate's changelog section comes from the log  · status: todo

**Blocked by:** G2 — live logging has written a real `@Unreleased` entry for a published crate.

#### Work Order

**Goal:** `feature_log.py changelog` prints a Keep a Changelog section for one published crate and version from the stored summaries, unchanged, and `/release` shows it beside the hand-written section and stamps the version into the log after publishing.

**Spec:** (proposal "Changelog generation")

- `changelog --crate X --version V` (V a version or `Unreleased`): selects every note, live or superseded, whose `crate` holds X and whose `release` holds `X@V`; prints `## [V]`, then one `### <heading>` per non-empty heading in the order Added, Changed, Deprecated, Removed, Fixed, Security (`feature` → Added, `fix` → Fixed, `change` and `perf` → Changed, `deprecation` → Deprecated, `removal` → Removed, `security` → Security), each followed by `- <summary>` lines sorted by summary. An unpublished crate fails with exit 2 and prints nothing.
- `changelog --crate X --version Unreleased --compare PATH` — compares that output with the `## [Unreleased]` section of the CHANGELOG at PATH (bullet sets per heading); prints `match` (exit 0) or the differences (exit 1).
- `release --crate X --version V` — replaces `X@Unreleased` with `X@V` in every live note holding it, and drops `X@Unreleased` from superseded notes (my call: a behavior replaced before a release never shipped, so the generator needs no state filter); appends one `release` event; a repeat prints `unchanged`. `digest` shows a release event as `= X@V (<n> notes)`.
- `commands/release.md` STEP 4 (at 485, beside the `## [Unreleased]` display): for each published crate being released, run `changelog --compare` and show `match` or the differences to the release owner, who reconciles them with `amend` or in the changelog before finalizing. After the crate is published, run `release --crate <crate> --version <version>`. A crate whose output differs is reported, not blocked (the proposal: connect a crate's release path only after its output matches).

**Files:**
- `scripts/feature_log/feature_log.py` — `changelog`, `release`.
- `scripts/feature_log/test_feature_log.py` — their cases.
- `commands/release.md` — STEP 4 lines (no owner in the production).

**Seats:** `1 writer + 1 tester` — one script change and one command step; the heading map and selection rules are concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`, `commands/release.md`.
- `test` — `scripts/feature_log/test_feature_log.py`: fixture notes covering every kind, a result shared by two crates, a superseded entry with a released version (kept) and one with `@Unreleased` (dropped by `release`), and an unpublished crate (no output); the proposal's cargo-mend 0.23.0 example prints exactly its section; `--compare` matches and differs; `release` and its repeat.

**Constraints from prior phases:** Phase 1: notes carry `release` values `crate@Unreleased` (derived without `--backfill`) or `crate@version`; `events.jsonl` lines carry `release` and `backfill`. Phase 2: `digest` reads events. Phases 4–9: backfilled notes carry a historical version only on an exact changelog-bullet match.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` clean. Live: `changelog --compare` against each published crate's current `CHANGELOG.md`; the phase report names the crates that match and the differences of the rest.
