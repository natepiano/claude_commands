# sdlc-guilds

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A lasting, resumable designer role and reviewer role that a unit director or a showrunner holds, and four guilds (designers, reviewers, showrunners, units) whose heads keep tips in `~/rust/nate_style/guilds` and hear from members when set events fire.

> **As-built disposition: create** — `docs/as-built/guilds.md`

> **Production: sdlc** — unit `guild-process-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 EDT

a designer role is something that we want to, you know, be able to resume and keep around under a unit director. It could also be a role under a showrunner that you keep around. So it can and probably should be both. ... we need to have like a guild of designers that needs to be designed. And we need to keep Cargo Liner notified so Cargo Handler can help us visualize it with, you know, simple text. And I think we also need to have a guild for reviewers, which needs to be designed and basically works the same way. So the designers can, you know, focus on the design skill and reviewers can focus on the review skill, but share tips and tricks and common documentation.

For the guild work ... make sure there's like a guild mechanism, probably uses our messaging infrastructure with these roles. And, you know, we have to cover the naming conventions, and it should be probably all named roles.

I like goddesses of wisdom and judgement for the chief designers and reviewers. Probably each is a top level showrunner.

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

2026-10-21, the user's guilds review with the sdlc showrunner (relayed 2026-10-21; table copied from `docs/plans/sdlc-production.md`, Review decisions, guilds-unit). The user's model: "the goal of a guild is to help guild members do their jobs better. Showrunners and unit directors share behavior and get guild functionality too, not only designers and reviewers. Heads do not read every member turn: that costs too much. Set events make members write to their guild or head. The user sends all guild feedback through the head. Each skill that starts a member role carries short guidance on how to work with the guild and its head." The user: "resume your work with the revisions we made."

| # | Decision |
| --- | --- |
| G1 | Four guilds: designers, reviewers, showrunners, units. Workers have no guild (Codex code and test threads, pooled code and UI reviewers); they get better guidance through their unit director's work orders. |
| G1b | Each head is a unit director, one per guild, under one lasting top-level `guilds` showrunner, not under sdlc. In herdr: a top-level guilds space, the guilds showrunner in the first tab, then the four heads in alphabetical order. This replaces "each head is a top-level showrunner". |
| G2 | Heads do not read every member turn. They read turns when an event fires (G3). Once a day they read the records already kept: repair rounds per phase (`rounds.py`), time per phase step (`phase_process.py`) and review findings. They read turns only where a record looks bad. |
| G3 | A member sends one short message to its head only, never to the whole guild, when: the user corrects it; a phase reaches repair round 4; the same review finding comes back twice; it works around a tool fault or a rule that slowed it; it finds a way that saved real time. The head decides what goes to the whole guild. |
| G3b | Tips live in `~/rust/nate_style/guilds/<guild>/`, one tip per markdown file like the rust rules; shared tips in `~/rust/nate_style/guilds/common/`. Each guild gets a base folder with Obsidian views by tag, like `rust_base`. The head adds and edits tip files. A member loads its guild's tips when its skill starts. Tips reach both machines through git. Drop the copy step, the publication manifests and receipts, and `docs/guilds` in `~/.claude`. |
| G4 | The user gives guild feedback in the head's tab or through any showrunner, which passes it on. The head writes it as a tip file. |
| G5 | `/showrunner:produce`, `/unit:direct` and the designer and reviewer start prompts each get a short guild block: who your head is, load your guild's tips, the G3 events, and how to write to the head. |
| G6 | The heads live in the guilds herdr space on natedev. |
| G7 | The top level is named `guilds`. The four guilds and their head units are named `designers`, `reviewers`, `showrunners` and `units`. Goddess names are on hold. G1 and G7 settle the old questions A and B. |
| G8 | Keep phase 1. Revise the phase 2 design doc: keep its lasting-role half, change its guild half. |
| G9 | New phases: 3 revise the design doc and review it; 4 the tip folders and views in `nate_style/guilds`, plus the guild block and the G3 events in each starting skill; 5 the guilds production doc, then the showrunner starts the guilds showrunner as top level with its four head units, each with its daily record read; 6 the lasting designer and reviewer threads (the old phase 3). sdlc's guild-process unit director owns the guild process; the guilds showrunner runs each guild by that process. |
| G10 | hierarchies-unit moves into sdlc as its own unit beside guild-process-unit. Read its findings: its "record waits" step feeds the heads' events. |

2026-10-20 16:19 EDT, to fps/frame-time-unit (hana), as its handoff records it: "Astra is the DESIGNER, not me. ONE Codex session gpt-6-astra xhigh writes + revises in the same thread; optionally ONE Astra reviewer session (same model/effort, one thread). No more than one of each. My part: brief designer, pass each review, judge when good. At least 2 review rounds. Save each round."

## What exists today (read 2026-10-20 16:40–16:55 EDT by the plan author)

- **No designer role exists.** `commands/unit/direct.md`, `commands/showrunner/produce.md` and `docs/production_format.md` name no designer; "the unit director owns design" (`direct.md:7`). The showrunner's design check is a fresh `/ux_eval` helper each time (`produce.md:698-736`).
- **Astra review is one turn.** `scripts/astra/astra_review.py` runs one read-only `codex exec` turn on `gpt-6-astra` xhigh (hardcoded, `:20-23`) in the workers account; it keeps no thread id and cannot resume. Its run folder is `/tmp/claude/astra/<stamp>-<pid>/`; `codex.log` prints `session id: <thread>` in its header.
- **The review pool is the one lasting, resumable named reviewer.** `scripts/direct/review_pool.py` keeps one permanent Codex thread per unit and type (`code`, `ui`) in `~/.local/state/review-pool/<slug>/`, outside the run roots, with a standing prompt in `config/review_pool/<type>.md` that reaches a live thread through `codex_mesh.py set-instructions`. Its reviewers are `code1`, `ui2`; the word `review` is theirs.
- **Thread lifecycle** is `scripts/agents/codex_mesh.py`: `start [--resident]`, `follow --to`, `stop --pause`, `reopen`, `resume-account-move`, `end`; the roster is `<session dir>/mesh_roster.json`, the server record `mesh_server.json` (holds `codex_home`). A Codex worker between turns takes no peer message; only its holder resumes it (`commands/message.md:35`).
- **Messaging groups** are a closed union in `scripts/production/broadcast.py:35-79` (`AllRoles`, `Showrunners`, `UnitDirectors`, `Agents`, `Workers`, `WorkersOfUnit`, `TopLevel`), chosen from records, never from names. Roles in `scripts/message/roster.py:49` are `showrunner`, `unit director`, `worker`, `freestanding`.
- **cargo-handler's census** reads `scripts/production/production_tree.py --json` (`{"schema":1,"edges":[{manager,member,role}]}`, roles `showrunner|unit|impl|test|review|fix`) and drops an unknown role. `config/statusline.conf` holds `role.*` colors.
- **Two Astra designers run by hand today, and neither shows in cargo-handler:**
  - fps/frame-time-unit (hana): mesh worker `fps-metrics-designer` started with `codex_mesh.py start --resident` outside any phase. Its roster role is empty and its name ends `-designer`, so `production_tree.py` drops it. Its draft is not saved: the cargo-berth edit hook refused the write because the file "belongs to neither open phase" (reply 1, 16:30).
  - experiments-unit (cargo-liner): worker `cargo-liner-experiments-impl`, opened by `implement.sh` with an `AGENTS_CONFIG_FILE` that sets `impl` and `fix` to `gpt-6-astra:xhigh`; resumed with `implement.sh --to`; `/astra_review` per round, at most 3. Its files are in the phase reservation, so it writes. Notes for this unit: `~/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md` (section (d) comes when its design phase ends, about 20:30 EDT).
- **cargo-liner's handler Phase 21** (`~/rust/cargo-liner-handler/docs/cargo-port/remove-running-section-handler-footer-colors.md:632-663`) waits on two things from `~/.claude` main, target 21:45 EDT (sdlc log 16:48): one `production_tree.py --json` edge per Astra session while it holds the role, `{"manager": <Claude session id>, "member": <Codex thread id>, "role": "astra-designer" | "astra-reviewer", "codex_home": <CODEX_HOME>, "state": "busy" | "idle"}`, and the keys `role.astra-designer=pink`, `role.astra-reviewer=bright-green`. Its guild view waits on where guild membership lives.
- **hierarchies-unit** (build-followups, `docs/plans/build-followups-hierarchies.md` at `4b477220`) lists the chief designer and chief reviewer as planned top-level showrunners, peers in a flat organization, and leaves the role design to this unit. Its constraints C39 (no downgrade of a judgment role on speed or tokens), C40 (Codex workers kept across phases and account moves) and C41 (pooled reviewers kept for later runs) bind these roles.

Read 2026-10-21 by the plan author:

- **hierarchies-unit's design** (`~/worktrees/claude-build-followups-hierarchies/docs/plans/build-followups-hierarchies-design.md` at `86ba29d3`) still names the chief designer and chief reviewer as top-level peers (C03, Map → Roles, Productions); G1b replaces that. Its Step 2 "Record waits and delivery" (Q8) proposes typed wait events in `scripts/direct/progress_history.py` (phase, owner, UTC start/end, one of WaitingForQuotaRefusal, WaitingForMemoryOrSlot, WaitingForMergeGate, WaitingForUser, WaitingForSupplier, ReadyWithoutCapacity; missing history is HistoryNotRecorded) and merge receipts. Those records are a source for the heads' daily read (G2, G10).
- **The records a head reads daily** exist: `scripts/history/rounds.py` (repair rounds per phase), `scripts/direct/phase_process.py` (time per phase step), the findings ledgers (`scripts/direct/findings.py`, one per phase in each run's session dir).
- **`~/rust/nate_style`** is its own GitHub repo (`git@github.com:natepiano/nate_style.git`, branch `main`, no merge branch). It holds `rust/` (54 rule files), `message/`, `prompt/`, and base folders `rust_base/`, `prompt_base/` of Obsidian `.base` views, one per tag (`views: - type: table … filters: and: - file.hasTag("<tag>")`). A rule file has YAML front matter (`tier`, `family`, `date_created`, `date_modified`, `tags`, `mechanism`, …) then one `##` heading and its text. Its working tree has an uncommitted `review-charter.md` change and an untracked `.obsidian/` that this unit does not own.
- **No designer or reviewer start skill exists.** A lasting designer's opening prompt is written per design by its holder (phase 2's was `phases/2/` in this run); `/astra_review` (`commands/astra_review.md`) starts the Astra reviewer turn.

## Decisions

- **Phase 1 delivers the Astra edges cargo-liner asked for, before any design** (showrunner, 2026-10-20 16:48: "astra edge = guilds-unit first phase, target before 21:45 EDT"). It records which session holds an Astra role so cargo-handler shows it; it changes no process, so it needs no question.
- **Every change to a role, a responsibility, a name or the process is a question for the user** (user, 2026-10-20). Phase 2 wrote the design and its questions; the user answered them in the 2026-10-21 review (G1–G10, Source). A new process question the revision raises goes to the showrunner, which brings it to the user (plan author).
- **The showrunner settled the Phase 2 lasting-role questions on 2026-10-20** (peer decision under the production rules): Q1 both holders, one per design; Q2 a separate Astra store on the mesh lifecycle; Q5 the production prefix, adopted mesh names kept as aliases; Q8 names now; Q12 pending records, no automatic wake; Q14 one natedev registry, each pair on its holder's host; Q16 adopt idle designers with an explicit ownership transfer. Q9, Q10, Q11 and Q15 were guild-head and publication answers that G1b and G3b replace.
- **Phase 3 revises the design with the same designer thread** (`claude-build-followups-sdlc-guilds-design`, resumed with `implement.sh --to`) and the Astra review rule above; a simplicity pass precedes the last round (experiments-unit lesson, showrunner 2026-10-20) (plan author).
- **Phase 4 commits and pushes in `~/rust/nate_style` only after the showrunner widens this unit's Owns cell**; the unit director sends the commit and push method first (showrunner, 2026-10-21).
- **The held Phase 2 messages to cargo-liner, roles-unit and phases-unit are not sent**; new versions go after the Phase 3 design doc lands (showrunner, 2026-10-21).
- **The design is written by one Astra designer and reviewed by Astra, at least 2 rounds and at most 3, each round saved** (user's rule to fps/frame-time-unit, 2026-10-20 16:19; showrunner's answer to experiments-unit, 16:48). The unit director briefs, passes each review and judges. The tester slot stays closed in that phase: the rule allows one designer and one reviewer (plan author, from the user's rule).
- **One design doc**: `docs/plans/sdlc-guilds-design.md` (plan author). It becomes `docs/as-built/guilds.md` at run end.
- **The edge role ids `astra-designer` and `astra-reviewer` name the Codex role a session holds** (cargo-liner's Phase 21 contract). They stay for the per-design threads; the heads are named `designers`, `reviewers`, `showrunners` and `units`, and goddess names are on hold (G7; production doc, Review decisions, R3).
- **This unit designs the guild process and the roles; it does not run the guild heads** (production doc, Guild heads; G9: the guilds showrunner runs each guild by this unit's process). Phase 5 writes the guilds production doc; the sdlc showrunner starts the guilds showrunner and its heads from it.
- **Each guild design change goes to cargo-liner** by `SendMessage` to `showrunners.py address remove-running-section`, and the design goes to hierarchies-unit and roles-unit through the sdlc showrunner (production rules).

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan adds the designer and reviewer roles and the two guilds.
- **Project started:** 2026-10-20T20:59:31.543+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:**
  - `scripts/astra/` — `astra_review.py`, `astra_roles.py` (new), their tests
  - `scripts/production/production_tree.py` — the census edges cargo-handler reads
  - `config/statusline.conf`, `config/README.md` — role colors and their key format
  - `docs/plans/sdlc-guilds-design.md` — the design doc (new)
  - `docs/plans/guilds-production.md` — the guilds production doc (new, Phase 5)
  - outside the repository, Phase 4: `~/rust/nate_style/guilds/` — the tip folders and base views (its own GitHub repo, branch `main`)
  - read only, outside the repository: `~/.local/state/review-pool/`, `/tmp/claude/astra/`, the run roots' `mesh_roster.json` and `mesh_server.json`, `~/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md`, the fps designer's folder `/tmp/claude/delegate/f6cf2c4f-858a-494f-bf69-6133ba872601/design/`
- **Key files:**
  - `scripts/astra/astra_review.py` — one Astra review turn
  - `scripts/production/production_tree.py` — census edges, schema 1
  - `scripts/agents/codex_mesh.py` — thread lifecycle and roster
  - `scripts/direct/review_pool.py` — lasting reviewers per unit, state outside the run roots
  - `scripts/production/broadcast.py` — message groups
  - `scripts/message/roster.py`, `scripts/message/send.py` — who exists and how to reach them
  - `scripts/accounts/accounts.py` — `role workers`, `env workers` (prints `CODEX_HOME=<dir>`)
  - `scripts/lib/unit_runs.py` — run roots and a run's director
  - `commands/unit/direct.md`, `commands/showrunner/produce.md`, `docs/production_format.md` — the role contracts (hub files, owners in the production doc)
  - `~/.claude/docs/decision_criteria.md` — what reaches the user
- **Test lanes:** `scripts/astra/` and `scripts/production/` — `test_*.py` beside each script.
- **Test:** `python3 -m unittest discover -s scripts/astra -p 'test_*.py'` and `python3 -m unittest scripts.production.test_production_tree`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` from the worktree root (production doc, Merge tests; memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/astra scripts/production` passes when its output ends `0 errors, 0 warnings, 0 notes`; the exit status says nothing.
- **Invariants:**
  - `production_tree.py --json` never prompts and answers well under a second; cargo-handler polls it every 30 s (`production_tree.py` docstring). It reads only local records, nothing over ssh.
  - An unknown or unreadable record leaves its own edge out and says why on stderr; every other edge stays.
  - Tests use fixtures and environment overrides; they never read the real `~/.local/state`, `/tmp/claude` or a live Codex.
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - User-facing times use `scripts/lib/user_zone.py` (America/New_York, user 2026-10-12); stored stamps stay UTC.
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — cargo-handler shows each Astra designer and Astra reviewer under the session that holds it  · status: done
**Depends on:** none

#### As-built

- `scripts/astra/astra_roles.py` records which Claude session holds each Astra thread: one file per thread, `~/.local/state/astra/roles/<thread id>.json` (override `ASTRA_ROLES_STATE`), written by temp file and rename. On disk `RoleRecord` has exactly `role` (`designer` | `reviewer`), `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, with nulls for the other lifetime. In memory `HeldRole` / `LiveRole` (adds `state: Literal["busy", "idle"]`) carry `lifetime: MeshWorker | ExecProcess` — the nulls stay at the JSON boundary.
- `hold(role, thread_id="", codex_home="", *, session_dir=None, worker=None, pid=None, manager=None) -> HeldRole` (raises `ValueError`; a mesh hold reads the thread from `mesh_roster.json` and `codex_home` from `mesh_server.json`; `manager` defaults to `$CLAUDE_CODE_SESSION_ID`, empty is refused), `release(thread_id) -> None`, `live_roles() -> list[LiveRole]`. CLI `hold designer|reviewer`, `release <thread id>`, `list [--json]`; a refusal exits 2 with one line.
- `live_roles()` removes a mesh record when its run folder is gone, the roster lacks the worker, the roster thread differs, or the status is `ended`/`failed`, and an exec record when its pid is gone. `busy` is status `starting`, `running`, `waiting_capacity` or a running pid; else `idle`. A malformed record or unreadable roster is one stderr line naming the file; the record stays and every other record is returned.
- `astra_review.py` `run_review()` holds an `ExecProcess` reviewer record for its `codex exec` when `$CLAUDE_CODE_SESSION_ID` is set, after reading `session id:` from `codex.log` (30 s limit), with `codex_home` from `accounts.py env workers`; it releases in `finally`. Exit codes and output are unchanged.
- `production_tree.py --json` adds `AstraEdge` (`manager`, `member` = thread id, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) per live record and drops the Codex worker edge for that thread. `statusline.conf` has `role.astra-designer=pink`, `role.astra-reviewer=bright-green`.

**Files:**
- `scripts/astra/astra_roles.py` — the Astra role record, library and CLI
- `scripts/astra/astra_review.py` — reviewer record during its `codex exec`
- `scripts/production/production_tree.py` — Astra edges, one edge per thread
- `config/statusline.conf`, `config/README.md` — the two role colors
- `scripts/astra/test_astra_roles.py`, `scripts/astra/test_astra_review.py`, `scripts/production/test_production_tree.py` — their tests

**Binds later work:** cargo-handler reads the `AstraEdge` fields, which stay. The role launcher needs a third lifetime variant tied to its thread store, added to `RoleRecord` and to `_parse_record`'s exact key check, because a `MeshWorker` hold ends with its run folder and an `ExecProcess` hold with its pid. The record file name is the thread id, so one thread has one record; the guild view names members by that thread id.

**Gotchas:** Scripts import the module as `from scripts.astra import astra_roles` after `sys.path.insert(0, <repo root>)`. A test helper named `run` hides `unittest.TestCase.run`. basedpyright exits 3 for the missing `.venv`; only its footer counts.

**Ruled out:** nullable `session_dir` / `worker` / `pid` on the in-memory types — the lifetime union replaces them; keeping a mesh record whose run folder or roster worker is gone.

### Phase 2 — The design doc defines the designer and reviewer roles, their names and the two guilds  · status: done
**Depends on:** none

#### As-built

- `docs/plans/sdlc-guilds-design.md` is a design that authorizes no role change, rename, head launch or implementation phase. Sections: Lessons, Roles, Names, Guilds, Command changes, Choices, Steps, Questions for the user (Q1, Q2, Q4–Q12, Q14–Q16, each with answers and a recommendation; roles-unit Q1, Q3 and Q5 cited, not asked again).
- It proposes one `gpt-6-astra:xhigh` Astra designer and at most one Astra reviewer per design, held by one unit director or showrunner, in a role store outside the run roots under a natedev ownership authority; launcher `scripts/guilds/roles.py`; instances `<production>-<area>-astra-designer` / `-astra-reviewer`; guilds `design-guild` and `review-guild`, membership derived from the role records; census `scripts/guilds/guilds.py list --json`.

**Files:**
- `docs/plans/sdlc-guilds-design.md` — the design and the user's questions

**Binds later work:** the lasting-roles, guild-membership and command-contract Work Orders take their Spec from this doc, as the user's answers settle it. Every `commands/unit/direct.md` change is a request to phases-unit.

**Gotchas:** `scripts/astra/astra_review.py` starts a fresh `codex exec` thread every call, so per-round review does not prove one reviewer per design.

**Ruled out:** a head file and head discovery in this version; join/leave membership verbs; a `/guild:announce` wrapper; raw `stop --pause` for held roles; a design type inside the review pool; the three-round review ceiling outside this unit; host transfer or authority failover.

### Phase 3 — The design doc defines four guilds, their heads, events and tips, and keeps the lasting roles  · status: done
**Depends on:** Phase 2

#### As-built

- `docs/plans/sdlc-guilds-design.md` (253 lines) is the final design for the tips, guilds-production and lasting-role phases. Sections: Lessons, Roles (with the settled Q1, Q2, Q5, Q8, Q12, Q14, Q16 answers in place), Names, Guilds (Guilds and heads, Events, Daily read, Tips, Guild block, Data for cargo-handler), Command changes, Steps, Questions for the user (none).
- Four guilds `designers`, `reviewers`, `showrunners`, `units`; membership comes from the role a session already holds; workers and pooled reviewers have no guild. Each head is a unit director with that ID under the top-level `guilds` production on natedev, found through `message/roster.py --json`.
- New names the design fixes: `scripts/guilds/daily_read.py`, `config/guilds/designer.md`, the `<!-- astra_work {JSON} -->` work header, `guilds/<id>_base/events.base` and `tools.base`, the tips lock `<git-common-dir>/guild-tips.lock`.

**Files:**
- `docs/plans/sdlc-guilds-design.md` — the final design

**Binds later work:** the tips, guilds-production and lasting-role Work Orders take their Spec from this doc's sections; every `commands/unit/direct.md` change is a request to phases-unit and every `commands/showrunner/produce.md` change goes to roles-unit.

**Gotchas:** a new script folder runs in the merge gate only when listed in `scripts/production/merge_gate.py` `TEST_SUITES`; the berth patch check covers `apply_patch` only; the standard library has no YAML parser.

**Ruled out:** `design-guild` / `review-guild`, `scripts/guilds/guilds.py` publication, `broadcast.py --guild`, manifests, receipts, delivery marks and a `docs/guilds` copy; a separate guild member list; a rename registry; a review-result ID in the findings ledger.

### Phase 4 — Each guild has a tip folder and views, and each starting skill carries the guild block  · status: todo
**Depends on:** Phase 3

**Settled 2026-10-21 (showrunner): this unit edits all four starting-skill files** — `commands/showrunner/produce.md`, `commands/unit/direct.md`, `commands/astra_review.md` with `scripts/astra/astra_review.py`, and the new `config/guilds/designer.md`. The guild block goes in as one self-contained section per file and nothing else in the file changes; roles-unit and phases-unit are paused, and the showrunner tells them what was added.

**Settled 2026-10-21 (showrunner):** the Owns cell covers `~/rust/nate_style/guilds/` as a separate repo. Commit on its `main` after the phase gates, staging exact paths only (never `review-charter.md` or `.obsidian/`), `git pull --ff-only` first, push fast-forward only; each checkpoint notice names the `nate_style` commit.

#### Work Order

**Goal:** `~/rust/nate_style/guilds/` holds the four guild tip folders, `common/`, five seed tips and eight tag views; the four skills that start a member role carry the exact guild block and load their tips through one locked pull; each Astra brief and one-turn review saves a work header that names its run and phase.

**Spec:** `docs/plans/sdlc-guilds-design.md` (final; read, do not edit), sections Tips, Guild block, Events, Daily read (the `astra_work` header paragraphs), Command changes and Steps → Phase 4:
- **Tip folders and seed tips.** Folders `~/rust/nate_style/guilds/designers/`, `reviewers/`, `showrunners/`, `units/`, `common/`. One file per tip in the form of `~/rust/nate_style/rust/always-use-nextest.md`: YAML front matter with `tags`, `date_created: "[[YYYY-MM-DD]]"`, `date_modified`; one `##` heading; short text; a `Source:` line. Every seed carries `guild-event`, its `guild/<id>` tag and its `head/<id>` owner tag. Five seeds:
  - `designers/same-thread.md` — revise the design in the same thread; answer each finding with a change or a cited disagreement. Tags `guild/designers`, `head/designers`.
  - `reviewers/cite-evidence.md` — each defect cites a file and line or a section. Tags `guild/reviewers`, `head/reviewers`.
  - `showrunners/relay-corrections.md` — pass the user's guild feedback to that guild's head (G4). Tags `guild/showrunners`, `head/showrunners`.
  - `units/report-round-four.md` — report to the head when `findings.py gate` first returns `verdict=dispatch` with `round=4` for a phase instance (G3). Tags `guild/units`, `head/units`.
  - `common/report-evidence.md` — the Tips example in the design, word for word (tags `guild/common`, `guild-event`, `head/units`).
- **Views.** `guilds/designers_base/`, `reviewers_base/`, `showrunners_base/`, `units_base/`, each with `events.base` and `tools.base` (eight files). A view is a `type: table` with columns `file.name`, `tags`, `date_created`, `date_modified`, sorted by `file.name` ASC, as `~/rust/nate_style/rust_base/naming.base`. Its filter, for `events` in `designers_base/` (`tools` uses `guild-tool`; it starts empty):

  ```yaml
  filters:
    and:
      - file.hasTag("guild-event")
      - or:
          - file.hasTag("guild/designers")
          - file.hasTag("guild/common")
  ```
- **`scripts/guilds/tips.py`** (new): the Tips Git rule. The checkout is `~/rust/nate_style`, overridden by `GUILD_TIPS_REPO` in tests. The lock is `fcntl.flock` on `<git rev-parse --git-common-dir>/guild-tips.lock`, held only for the Git commands, released on every failure, never held during an LLM turn.
  - `tips.py load --guild <designers|reviewers|showrunners|units>`: under the lock, `git pull --ff-only` and resolve `HEAD` to one commit. Print `commit <sha>`, then each `guilds/common/*.md` and `guilds/<guild>/*.md` read from that commit (`git ls-tree`, `git show <sha>:<path>`), each headed by its path. A lock or pull failure prints one stderr line and uses the last local commit, which it names. No `guilds/` at that commit (no baseline) exits 2: `restore the guild tips baseline before starting`.
  - `tips.py publish --head <id> --from <folder>`: the folder holds the prepared tip text at paths relative to `guilds/`. One bounded command: lock; refuse a staged index, an uncommitted edit on a target path, or a target the head does not own (`guilds/<head>/`, `guilds/<head>_base/`, or a `guilds/common/` file tagged `head/<head>`); `git pull --ff-only`; copy the prepared files; `git add` those exact paths; commit on `main`; push. On divergence at pull or push, `git fetch`; only with a clean tracked tree and only this edit's unpublished commits, rebase them onto `origin/main` and retry one normal push. On conflict, `git rebase --abort`, keep the local commit, exit 1 and say to report to the guilds showrunner. Dirty or foreign unpublished work exits 1 and names it. Never stage, stash or reset another path; never force push. Print the commit only after the push succeeds.
- **`scripts/guilds/astra_work.py`** (new): the saved assignment header, the first line of a brief or prompt: `<!-- astra_work {JSON} -->` with keys `schema` (1), `role` (`astra-designer` | `astra-reviewer`), `member`, `document`, `assigned_at_utc`, `reply_path`, `work`. `work.kind` selects `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)` or `StandaloneWork`, frozen dataclasses with no nullable phase fields.
  - `phase_work(run_directory, phase_id) -> PhaseWork` copies `run_id` and `history_file` from `<run>/progress_history_state.json` (written by `scripts/direct/progress_history.py:1234`, `_start_run`) and `phase_instance_id` from its open phase with that id (as `scripts/direct/findings.py:359`, `_phase_identity`, reads it). A phase that does not resolve raises `ValueError`.
  - `header_line(work: AstraWork) -> str`; `read_header(path) -> AstraWork | HeaderMissing | HeaderInvalid`, for the daily reader.
  - CLI `astra_work.py brief --run-directory <dir> --phase <id> --member <mesh name> --document <path> --round <N> --text-file <brief>` writes `<run>/design/phase<id>/round<N>_brief.md`, header first, and refuses to overwrite; its `reply_path` is `round<N>_reply.md` beside it.
- **`scripts/astra/astra_review.py`**: `main` (`:171`) takes optional `--run-directory <dir> --phase <id>`, both or neither, before `<document> [focus...]`. With them the header's `work` is `PhaseWork`; without, `StandaloneWork`. A supplied phase that does not resolve exits 2 with one line. `prompt.md` begins with the header (`role` `astra-reviewer`, `member` the run folder, `reply_path` its `review.md`). `review_prompt` (`:45`) appends the guild block for `reviewers` and the tip text that `tips.py`'s load function returns for `reviewers`. Exit codes, printed output and the `ExecProcess` hold stay as they are.
- **Guild block** — the Guild block text in the design, word for word, with only `{guild}` replaced, then one line after it, outside the block: `Load tips: ~/.claude/scripts/lib/py ~/.claude/scripts/guilds/tips.py load --guild <id>`. A Codex holder puts the expanded block and the tip text in its worker's prompt.
  - `commands/unit/direct.md` (hub, owner phases-unit; change request): `units` block at the opening, before phase selection (after `:25–26`). In Synthesize / FixDispatch: when `findings.py gate` first returns `verdict=dispatch` with `round=4` for a phase instance, or a finding title repeats under the Events rule, send the units report; the gate outcome does not change. In LaunchImplementation: save each Astra designer brief with `astra_work.py brief` before dispatch, and its reply as `round<N>_reply.md`, without overwrite.
  - `commands/showrunner/produce.md` (hub, owner roles-unit): `showrunners` block at the opening, before `<ExecutionSteps>` (`:328`).
  - `commands/astra_review.md`: `reviewers` block; Usage adds `[--run-directory <dir> --phase <id>]`, and a holder passes both for phase work.
  - `config/guilds/designer.md` (new): the designer opening prompt — the Astra designer row of the design's Roles table (Does / Does not), then a `## Guild` section with the `designers` block. A holder includes its text in each brief.
- **Suite and lint:** add `"guilds"` to `TEST_SUITES` in `scripts/production/merge_gate.py:47`; add a `scripts/guilds` entry to `pyrightconfig.json` `executionEnvironments` (root `scripts/guilds`; `extraPaths` `.`, `scripts/guilds`, `scripts/direct`, `scripts/lib`).

**Files:**
- `~/rust/nate_style/guilds/designers/same-thread.md`, `reviewers/cite-evidence.md`, `showrunners/relay-corrections.md`, `units/report-round-four.md`, `common/report-evidence.md` — new: the five seed tips
- `~/rust/nate_style/guilds/designers_base/`, `reviewers_base/`, `showrunners_base/`, `units_base/` — new: `events.base` and `tools.base` in each
- `scripts/guilds/tips.py` — new: the tips lock, `load` and `publish`
- `scripts/guilds/astra_work.py` — new: the `astra_work` header, `phase_work`, `read_header`, `brief`
- `scripts/astra/astra_review.py` — run/phase inputs, the saved header, the reviewers block and tips in the prompt
- `commands/astra_review.md` — the reviewers block and the run/phase inputs
- `config/guilds/designer.md` — new: the designer opening prompt with the designers block
- `commands/unit/direct.md` — hub (owner phases-unit): the units block, the round-4 and repeat reports, saved designer briefs
- `commands/showrunner/produce.md` — hub (owner roles-unit): the showrunners block
- `scripts/production/merge_gate.py` — `"guilds"` in `TEST_SUITES`
- `pyrightconfig.json` — the `scripts/guilds` entry
- `scripts/guilds/test_tips.py`, `scripts/guilds/test_astra_work.py` — new; `scripts/astra/test_astra_review.py` — new cases

**Workers:** `1 writer + 1 tester` — the two new scripts and the `astra_review.py` change have a real test lane (`test_*.py` beside each script), and the Spec gives each behavior; the tips, views and command text go with the writer.
- `impl` — the `nate_style` tips and views, `scripts/guilds/tips.py`, `scripts/guilds/astra_work.py`, `scripts/astra/astra_review.py`, `commands/astra_review.md`, `config/guilds/designer.md`, and `commands/unit/direct.md` / `commands/showrunner/produce.md` only as the showrunner's answer to the Pending decision assigns them; hub: `scripts/production/merge_gate.py` (one tuple entry), `pyrightconfig.json` (one entry; another unit may also add one)
- `test` — `scripts/guilds/test_tips.py` (fixture Git repositories with a bare origin: two head edits plus a load, a rejected push and its rebase, a rebase conflict, unrelated dirty work, a failed pull, a missing baseline, a target the head does not own), `scripts/guilds/test_astra_work.py` (header round trip, phase resolution, an unresolved phase, no overwrite), and new cases in `scripts/astra/test_astra_review.py` (phase header, standalone header, invalid phase exits 2, block and tips in the prompt), from the Spec alone

**Constraints from prior phases:**
- The design doc is final; this phase does not edit it. Phase 1's `AstraEdge` fields and the `astra-designer` / `astra-reviewer` ids stay.
- `astra_review.py` starts a fresh `codex exec` thread each call and holds an `ExecProcess` reviewer record only during it (`run_review`, `:133`); held reviewers come with the lasting roles, not here.
- Scripts import repository modules as `from scripts.<folder> import <module>` after `sys.path.insert(0, <repo root>)` (`astra_review.py:23`). Never name a test helper `run`. Tests use fixtures and the overrides `GUILD_TIPS_REPO`, `ASTRA_REVIEW_DIR`, `ASTRA_REVIEW_ACCOUNTS`, `ASTRA_ROLES_STATE`, never the real `~/rust/nate_style`, `/tmp/claude` or a live Codex.
- `merge_gate.py` runs only the folders in `TEST_SUITES` (`:47`) and refuses a listed folder with no `test_*.py`, so the suite entry and the first test module land together.
- The standard library has no YAML parser; scripts and tests stay standard library only. The YAML check is the director's `uv run` line below.
- In `nate_style`, a rule file has YAML front matter then one `##` heading; a `.base` view filters by `file.hasTag("<tag>")` (What exists today). Its working tree has a modified `review-charter.md` and untracked `.obsidian/` and `rust/public-failures-return-named-errors.md`; this unit owns none of them and never stages them (Settled block).
- Each hub edit is named in the checkpoint notice as `also touches <path> (owner <unit>), tested against <owner tip>` (`docs/production_format.md` <ProductionUnit/> item 9).
- basedpyright exits 3 for the missing `.venv`; only its footer counts.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/guilds -p 'test_*.py'` and `python3 -m unittest discover -s scripts/astra -p 'test_*.py'` green from the worktree root; `basedpyright scripts/guilds scripts/astra` ends `0 errors, 0 warnings, 0 notes`; the merge gate green.
- Director: `uv run --no-project --with pyyaml python3 -c` parses each `guilds/*_base/*.base` and each tip's front matter; each tip has `tags`, `date_created`, `date_modified` and one `##` heading; each of the four sites carries the block word for word for its guild. Hub edits are trial-merged against their owners' tips.
- The `nate_style` commit follows the Settled block and is named in the checkpoint notice.
- Live (director): a new session that loads `/unit:direct` names its head (the `units` unit director in `guilds`) and lists the `common/` and `units/` tips; the head need not be running.


### Phase 5 — The guilds production doc, its four head units and their daily read  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** the sdlc showrunner can start the top-level `guilds` showrunner and its four head unit directors from one production doc, and each head prints one day of its members' records with `daily_read.py`.

**Spec:** `docs/plans/sdlc-guilds-design.md` (final), sections Guilds and heads, Daily read, Command changes (the StandingPlanRefill and `docs/production_format.md` rows) and Steps → Phase 5; `docs/production_format.md` for the production doc form:
- **`docs/plans/guilds-production.md`** (new): top-level production `guilds` on natedev with no parent (`showrunners.py parent guilds` prints none). herdr space `guilds`: the guilds showrunner in tab 1, then units `designers`, `reviewers`, `showrunners`, `units`, in that order. Each unit row names its standing plan. The showrunner keeps the heads and four daily timers running (`scripts/message/notifier.sh`, `:18` `usage`, `:77` `schedule`; `--every 1440`), resolves the current head sessions and replaces stale timer targets after a restart, relays the user's guild feedback to the head (G4), and reports the heads' work. Rules: heads own tips, not assignments, acceptance, model choices or gates; a head sends process proposals through guilds to sdlc for guild-process-unit (G9); no goddess names (G7).
- **Four standing plans** `docs/plans/guilds-designers.md`, `guilds-reviewers.md`, `guilds-showrunners.md`, `guilds-units.md` (new), each with the header `> **Standing:** guild-head`. Each names its guild and its members (Astra designers; Astra reviewers; `/showrunner:produce` sessions; `/unit:direct` sessions) and the loop: read a member report or user feedback; check its cited evidence; write or amend a tip with `tips.py publish --head <id>`; reply with the tip path or a reason to decline. At each daily check-in: run `daily_read.py` for the uncovered interval on natedev and once on the Mac, combine the host-labelled results, and record the covered interval in the daily report. A duplicate check-in does no second read; after downtime, cover the gap once and state its length. Read turns only for flagged rows. A head runs no phased build, dispatches no worker and shrinks nothing.
- **`commands/unit/direct.md`** (hub, owner phases-unit; change request): at the opening (`:25–26`) and in `<StandingPlanRefill>` (`:1471`), a plan headed `**Standing:** guild-head` runs that loop instead of ticket dispatch, with no phase, checkpoint or shrink (Map standing-plan branch after row 22).
- **`docs/production_format.md`** (hub, owner roles-unit), Words and `<ProductionUnit/>`: four head units under `guilds`, their unsuffixed IDs and the standing-loop exception; worker IDs stay.
- **`scripts/production/add_unit.py`**: `launch_request` (`:286`) keeps the exact IDs `designers`, `reviewers`, `showrunners`, `units`, with no `-unit` suffix (`:359`), only for production `guilds`; every other production keeps `<name>-unit`. `row_is_present` (`:381`) finds those IDs on restart.
- **`scripts/guilds/daily_read.py --guild <id> --since <UTC> --until <UTC>`** (new): local records only, no network; it reads no turns and writes no history.
  - Repair rounds: reuse `scripts/history/rounds.py:224` (`load_phases`) and `:219` (`phase_rounds`); join by run and phase-instance ID; an ambiguous instance stays unassigned; flag mixed records and rounds beyond three (`:283–285`).
  - Step time: reuse `scripts/direct/phase_process.py:258` (`read_events`), `:293` (`phase_boundaries`), `:341` (`allocated_step_seconds`), `:334` (`summarize`). Parallel steps share wall time; unmatched intervals stay unknown (`:313`, `step_intervals`); add no fix time.
  - Findings: active and archived `findings_state*.json` and run events (`scripts/direct/findings.py:280` `_state_path`, `:284` `_parked_path`, `:303` `_write_state`, `:399` `_append_event`); join verdicts to titles by phase instance and finding ID; keep source paths. Repeat rule (Events): normalize title case and spaces; count matching `finding_opened` titles in different `(phase_instance_id, round)` groups, or a `still_open` / `reopened` verdict after `finding_batch_landed` names that finding; one group counts once across lenses; missing phase/round or repair order gives `RepeatNotProven`; cite both event IDs.
  - Waits and merge receipts: hierarchies-unit's records when they exist; missing gives `HistoryNotRecorded` or `ReceiptMissing`, never inferred waiting or delivery.
  - Members: `units` reads directors' runs under `scripts/lib/unit_runs.py:44` (`run_roots`); `showrunners` reads productions' runs and merge/wait records; `designers` and `reviewers` read `astra_work` headers (`astra_work.read_header`) at `<run>/design/phase*/round*_brief.md` and `/tmp/claude/astra/*/prompt.md`, and `<roles-root>/<design_id>/prompts/*.md` when that root exists. Join header phase IDs to ledgers and history, filter to the interval, de-duplicate phase totals. A missing header gives `MembershipNotRecorded`; missing output leaves the outcome unknown; standalone reviews have no phase metrics; removed evidence gives partial coverage. Never assign implementation or pool reviews to Astra.
  - "Looks bad": at least four repair rounds; the repeat rule; a mixed phase record; allocated step time above its p90 over the preceding 14 complete days on that host with at least 20 samples (fewer samples: no speed verdict); an open wait of at least 24 hours; a claimed merge without its receipt once writers exist. Print the actual threshold and sample count.
  - Output: host, guild, UTC interval and local times (`scripts/lib/user_zone.py:42`, `user_zone`), member/run coverage, counts, repair rounds, repeated titles, step seconds with threshold and sample count, waits, merge receipts and problems. Each flagged row names its source and phase/turn reference. Absent records are named states, never a bare `None`.

**Files:**
- `docs/plans/guilds-production.md` — new: the guilds production doc
- `docs/plans/guilds-designers.md`, `docs/plans/guilds-reviewers.md`, `docs/plans/guilds-showrunners.md`, `docs/plans/guilds-units.md` — new: each head's standing plan
- `commands/unit/direct.md` — hub (owner phases-unit): the `guild-head` standing branch
- `docs/production_format.md` — hub (owner roles-unit): the four head units and their IDs
- `scripts/production/add_unit.py` — exact G7 IDs for production `guilds`
- `scripts/guilds/daily_read.py` — new: the daily read
- `scripts/guilds/test_daily_read.py` — new; `scripts/production/test_add_unit.py` — new cases

**Workers:** `1 writer + 1 tester` — the reader and the launcher change have test lanes (`scripts/guilds/`, `scripts/production/`), and the Spec gives the inputs, rules and output; the docs go with the writer.
- `impl` — `scripts/guilds/daily_read.py`, `scripts/production/add_unit.py`, the five plan docs, and `commands/unit/direct.md` / `docs/production_format.md` as the showrunner assigns hub writers
- `test` — `scripts/guilds/test_daily_read.py` (temporary-folder fixtures: populated designer and reviewer headers joined to rounds, step times and findings after holds end; standalone work; both hosts; missing evidence; same-round duplicate lenses; post-repair verdicts; ambiguous repeats; parallel steps; sparse baselines; absent hierarchy records) and `scripts/production/test_add_unit.py` cases (launch, adopt existing rows, restart and roster lookup under each exact ID; other productions keep `<name>-unit`), from the Spec alone

**Constraints from prior phases:**
- From the tips phase: `scripts/guilds/tips.py` (`load --guild <id>`, `publish --head <id> --from <folder>`, lock at `<git-common-dir>/guild-tips.lock`); `scripts/guilds/astra_work.py` (`AstraWork`, `PhaseWork`, `StandaloneWork`, `read_header(path) -> AstraWork | HeaderMissing | HeaderInvalid`); tip folders under `~/rust/nate_style/guilds/` with `guild/<id>` and `head/<id>` tags; `"guilds"` is in `merge_gate.py` `TEST_SUITES` and `pyrightconfig.json` has the `scripts/guilds` entry (add `scripts/history` and `scripts/production` to its `extraPaths` if the reader imports them).
- The heads are unit directors, not showrunners (G1b), on natedev (G6), with no goddess name (G7). The daily read selects evidence; it adds no stop rule and changes no model.
- Astra holds cannot supply history: `scripts/astra/astra_review.py:164–168` releases the reviewer hold, and `scripts/astra/astra_roles.py:199` (`live_roles`) removes ended holds.
- The hub writer for `commands/unit/direct.md` and `docs/production_format.md` follows the showrunner's answer on starting-skill files; each hub edit is named in the checkpoint notice as `also touches <path> (owner <unit>), tested against <owner tip>`.
- Stored stamps stay UTC; user-facing times use `scripts/lib/user_zone.py`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/guilds -p 'test_*.py'` and `python3 -m unittest scripts.production.test_add_unit` green from the worktree root; `basedpyright scripts/guilds scripts/production` ends `0 errors, 0 warnings, 0 notes`; the merge gate green; hub edits trial-merged against their owners' tips. Live (director): `daily_read.py --guild units` prints this run's day from this run's ledgers. At phase end the unit director sends the showrunner the production doc path to start the guilds showrunner; after launch the guilds showrunner checks the tabs, the head lookup, one report and the four timers.


### Phase 6 — The designer and reviewer roles open, resume and outlive a run, under a unit director or a showrunner  · status: todo
**Depends on:** Phase 4 (its designer opening prompt and tip baseline; Astra review round 1, 2026-10-21)

The Phase 2 role questions are settled (Decisions: Q1, Q2, Q5, Q8, Q12, Q14, Q16).

#### Work Order

**Goal:** a unit director or a showrunner opens a named designer or reviewer once and resumes the same thread across phases, runs, account moves, restarts and unit renames.

**Spec:** `docs/plans/sdlc-guilds-design.md` (final), sections Roles (Durable identity, model and access; Lifecycle and recovery; Adoption and file ownership), Names, Data for cargo-handler, Command changes and Steps → Phase 6; those sections name each function this phase changes, with line refs:
- **Store and authority.** Role store `~/.local/state/guilds/roles/` on the pair's host (`GUILD_ROLES_STATE` in tests): one directory per immutable UUID `design_id` with `design.json`, a lock, `mesh/`, and saved prompts and replies; role IDs `<design_id>/designer`, `<design_id>/reviewer`. One natedev authority, pinned in `config/guilds.conf` on both hosts, alone writes `~/.local/state/guilds/ownership/` (`GUILD_OWNERSHIP_STATE`), mapping `(repository_id, document_path)` to `DesignPlacement(design_id, host, generation)` under a short lock; reach it over the SSH pattern of `scripts/message/send.py:1096` (`remote`). Holder is `UnitHolder(production_slug, unit_name)` or `ShowrunnerHolder(production_slug)`; binding `DesignUnbound` or `DesignBoundToPhase(run_directory, phase, repository, output_paths)`; lifecycle `RoleNotOpened`, `RoleOpen`, `RolePausedForMove`, `RoleNeedsRecovery`, `RoleEnded`; access `PhaseBoundDesigner(binding_generation, launch_sandbox)` or `ReadOnlyReviewer`. Convert nullable external fields to these at the reader boundary.
- **Launcher** `scripts/guilds/roles.py`: `open`, `adopt`, `resume`, `rebind`, `end`, `list --json`, with the Lifecycle table's required result for first open or retry, new scope in a turn, phase or run end, account move, holder restart or transfer, crash and end. `open` on `RoleEnded` refuses: `role ended; this design identity cannot be reopened.` A new designer prompt starts from `config/guilds/designer.md` plus `tips.py load --guild designers`; a reviewer prompt gets the `reviewers` block and tips. Each prompt is saved in `<roles-root>/<design_id>/prompts/*.md` before dispatch with its `astra_work` header (`astra_work.header_line`), and kept across unbind and end. `list --json` adds local durable roles, holder reach and paused/recovery states without network calls; an unknown record is a listed problem.
- **Model:** `config/agents.conf` rows `[guild.codex] designer=gpt-6-astra:xhigh`, `reviewer=gpt-6-astra:xhigh`, `[assignments] guild=codex`, resolved through `scripts/agents/agents_config.sh:285` (`agents_resolve`); a missing or invalid row refuses launch; never hardcoded, no fallback.
- **Mesh** (`scripts/agents/codex_mesh.py`): explicit access policy replaces the `instructions_file` inference (`:1027` `_seat_sandbox`, `:1046` `_resume_params`); atomic instruction-and-turn claim in `:1065` (`_claim_follow`) under lock order design, pending, roster, server, with no SSH, authority call or LLM wait under these locks; strict same-thread resume (`:3752` `_resume_worker_thread` reports `RoleNeedsRecovery` for held roles); held-role `set-instructions` refuses (`:6531`); raw `stop --pause` refuses held roles (`:4503`, `:4569`); park queued text under the pending lock before any server stop (`:1545`, `:3787`); durable-mesh discovery and holder resolution in `:6343` (`retire_workers_servers`), with busy work saved by `:6282` before interruption and recovered by `:6128`; `command_end` (`:4308`) keeps marking a lost thread's roster ended before server contact.
- **Server environment refresh:** record the validated binding generation and resolved holder session with each server. `ensure_server` (`:1804`, reuse at `:1823`) reuses a healthy same-account server and `_server_environment` (`:1839`) runs only in `_start_server` (`:1855`); the berth hook prefers an existing board folder (`scripts/hooks/pre-tool-use-berth.py:43`, `:54`). On any mismatch, park queued text and release the old server before dispatch; uncertain release gives `RoleNeedsRecovery`. Start the replacement with the current binding's run environment and clear inherited run fields when unbound. This applies to rebind, unbind, holder restart and transfer, also when the account and the old run folder stay.
- **Pending role messages (Q12):** one record per message under the role directory (ID, source, text), added to an authorized assignment, cleared only after that turn's acceptance is known; `command_send` (`:4152`) queues and `_start_turn` (`:4082`) submits. Successful send means queued, not read. No automatic wake.
- **File ownership:** `pre-tool-use-berth.py:76` (`decide`) checks the binding before attribution (`scripts/direct/phase_reservation.py:260`, `attribute`): normalize paths, resolve symlinks, check both rename paths, reject the whole patch for a stale binding, an unlisted path or an unreadable ledger. This covers `apply_patch` only (`decide` skips other tools at `:85`; writers have full access, `codex_mesh.py:1039`); designers are told to edit by patch. A showrunner may hold a read-only role but cannot launch an unbound writer until phases-unit's production-design phase exists.
- **Adoption (Q16):** the five Adoption steps: `AdoptionReserved`, `AdoptionRequired(source)`, the `TransferringRole(adoption_id, destination)` fence that every old follow, reopen, move, cleanup and `implement.sh --to` route honors, release by `codex_mesh.py:3716`, same thread and mesh name at the destination (never thread start or fork; `:4458` `command_reopen` is not a transfer), permanent source redirect, retries by the same adoption ID.
- **Integrations:** `scripts/agents/mesh_runs.py:105` and `scripts/message/roster.py:1243` discover durable roots; `scripts/accounts/accounts.py:3344` (`_swap_workers`) moves held roles; `scripts/direct/end_session.sh:83` unbinds held designs and keeps their threads; `scripts/production/retire_unit.py:590` ends a retired unit's roles after their turns finish; `scripts/astra/astra_review.py` routes a held document to its sole reviewer, reports busy or unavailable, and keeps the one-turn case; `scripts/production/rename_unit.py:436` (`_move_state`) rebinds each held design before relaunch (live holder, authority checkout mapping, binding paths, launch cwd; design and thread IDs and aliases stay; idle roles only; failure blocks relaunch and retries through the rename journal) and `:717` (`_state_failures`) checks those live fields; `scripts/production/unit_rename_sweep.py:70` (`_history`) and `:98` (`sweep`) treat saved guild prompts and replies as history and mesh aliases as identity, and still check live routes.
- **Census:** `scripts/astra/astra_roles.py` adds a `HeldDesignRole` lifetime tied to the durable instance, in `RoleRecord` (`:23`), the exact-key `_parse_record` (`:165`), the writer and `live_roles` (`:199`); a missing run folder cannot end it. `production_tree.py:255` (`astra_edges`) still emits one edge per thread; schema stays 1.
- **Command text:** `commands/unit/direct.md` rows LongLivedWorkers, DelegatedPhaseReservationContract, LaunchImplementation (configured same-thread designer and access policy), DualReview (one Astra reviewer thread across rounds), FixDispatch (same writer, short cited rulings) and PhaseCleanup / FinalGate / RunSummary (unbind after release; keep roles until explicit end) — change requests to phases-unit; `commands/showrunner/produce.md` production-design holder contract with visual checks kept — roles-unit; `commands/astra_review.md` held-thread selection, busy refusal and the one-turn case; `commands/message.md` and `codex/AGENTS-messaging.md` Outcomes and recovery: the holder route and pending role messages.
- **Undo:** disable new open and adoption, finish active turns; keep placements, fences, durable records, pending text and evidence; release servers by the safe rules, never raw `stop --pause`; keep adopted ownership at its destination unless reverse transfer proves no destination writer; restore old command text only when it cannot bypass fences; keep unfinished reservations; never replace a thread silently.

**Files:**
- `scripts/guilds/roles.py` — new: role store, ownership-authority client, `open`, `adopt`, `resume`, `rebind`, `end`, `list --json`
- `scripts/guilds/test_roles.py` — new
- `config/guilds.conf` — new: the natedev ownership authority pin
- `scripts/astra/astra_roles.py` — the `HeldDesignRole` lifetime
- `scripts/astra/astra_review.py` — finds and resumes the held reviewer; busy refusal; the one-turn case
- `commands/astra_review.md` — Usage and Steps: held-thread selection, busy refusal, the one-turn case
- `scripts/agents/codex_mesh.py` — access policy, atomic claim, strict resume, server environment refresh, durable-mesh discovery in account moves and cleanup, adoption fences, held-role refusals
- `scripts/agents/mesh_runs.py` — durable role roots in `_roster_folders`; detached results reach a held role
- `scripts/accounts/accounts.py` — `_swap_workers` moves held roles
- `scripts/hooks/pre-tool-use-berth.py` — `decide` checks every edited path against the binding's `output_paths`
- `scripts/message/roster.py` — discovers durable roles
- `scripts/message/send.py` — only if `roles.py` reuses its SSH `remote` route to reach the authority
- `scripts/direct/end_session.sh` — unbinds held designs and keeps their threads
- `scripts/direct/implement.sh` — `--to` honors adoption fences
- `scripts/production/retire_unit.py` — ends a retired unit's held roles
- `scripts/production/rename_unit.py` — rebinds held designs in `_move_state`; checks them in `_state_failures`
- `scripts/production/unit_rename_sweep.py` — saved guild prompts and replies are history; aliases are identity
- `config/agents.conf` — `[guild.codex]` rows and the `guild` assignment
- `pyrightconfig.json` — extend the `scripts/guilds` entry's `extraPaths` for the folders `roles.py` imports
- `commands/unit/direct.md` — hub (owner phases-unit): the lasting-role rows above
- `commands/showrunner/produce.md` — hub (owner roles-unit): the production-design holder contract
- `commands/message.md`, `codex/AGENTS-messaging.md` — the holder route and pending role messages
- `scripts/astra/test_astra_roles.py`, `scripts/astra/test_astra_review.py`, `scripts/agents/test_codex_mesh.py`, `scripts/agents/test_mesh_runs.py`, `scripts/accounts/test_accounts.py`, `scripts/hooks/test_berth_hook.py`, `scripts/message/test_roster.py`, `scripts/direct/test_end_session.py`, `scripts/direct/test_implement_launcher.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_production_tree.py` — their tests; `scripts/production/test_unit_rename_sweep.py` — new

**Workers:** `1 writer + 1 tester` — the code and its tests split cleanly; in the Astra-record work the two owner sets were independent and the test lane was real, and each changed script has its `test_*.py` beside it.
- `impl` — `scripts/guilds/roles.py`, `config/guilds.conf`, `scripts/astra/astra_roles.py`, `scripts/astra/astra_review.py`, `commands/astra_review.md`, `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py`, `scripts/accounts/accounts.py`, `scripts/hooks/pre-tool-use-berth.py`, `scripts/message/roster.py`, `scripts/message/send.py`, `scripts/direct/end_session.sh`, `scripts/direct/implement.sh`, `scripts/production/retire_unit.py`, `scripts/production/rename_unit.py`, `scripts/production/unit_rename_sweep.py`, `config/agents.conf`, `commands/message.md`, `codex/AGENTS-messaging.md`, and `commands/unit/direct.md` / `commands/showrunner/produce.md` as the showrunner assigns hub writers; hub: `pyrightconfig.json` (one entry; another unit may also edit it)
- `test` — `scripts/guilds/test_roles.py`, `scripts/production/test_unit_rename_sweep.py` and the twelve existing test files above, written from the Spec alone

**Constraints from prior phases:**
- The Astra role record (`scripts/astra/astra_roles.py`) is one file per thread, `~/.local/state/astra/roles/<thread id>.json` (override `ASTRA_ROLES_STATE`), written by temp file and rename. On disk it is `RoleRecord`, exactly the keys `role` (`designer` | `reviewer`), `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, with nulls for the other lifetime. `_parse_record` refuses a missing or extra key, a wrong type, or a `thread_id` that differs from the file name.
- In the library a record is `HeldRole` (`live_roles()` returns `LiveRole`, which adds `state: Literal["busy", "idle"]`) with `lifetime: MeshWorker | ExecProcess`: `MeshWorker(session_dir, worker)` or `ExecProcess(pid)`, both frozen dataclasses. Functions: `hold(role, thread_id="", codex_home="", *, session_dir=None, worker=None, pid=None, manager=None) -> HeldRole` (raises `ValueError`; `manager` defaults to `$CLAUDE_CODE_SESSION_ID`), `release(thread_id) -> None` (a missing record is not an error), `live_roles() -> list[LiveRole]`. CLI: `hold designer|reviewer`, `release <thread id>`, `list [--json]`; exit 2 with one stderr line on a refusal.
- `live_roles()` removes a `MeshWorker` record when its run folder is gone, the roster has no such worker, the roster thread differs, or the status is `ended` or `failed`; it removes an `ExecProcess` record when its pid is gone. It keeps a record whose roster is unreadable and prints one stderr line naming the file. `state` is `busy` for roster status `starting`, `running` or `waiting_capacity`, or a running pid; else `idle`.
- A lasting designer or reviewer thread outlives its run folder, so it cannot use `MeshWorker`, and outlives one process, so it cannot use `ExecProcess`. `HeldDesignRole` is the third variant, tied to the durable instance, with its own on-disk keys added to `RoleRecord` and to `_parse_record`'s key check.
- `astra_review.py` `run_review()` holds an `ExecProcess` reviewer record for the life of its `codex exec` only when `$CLAUDE_CODE_SESSION_ID` is set, after it reads `session id: <thread>` from `codex.log` (at most `THREAD_HEADER_TIMEOUT` = 30 s), with `codex_home` from `accounts.py env workers`, and releases it in a `finally`. A resumed held reviewer keeps one record per thread id: the file name is the thread id.
- `production_tree.py --json` prints one `AstraEdge` per live record (`manager`, `member` = thread id, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) and drops the Codex worker edge for that thread (`worker_edges(running, astra_threads)`). cargo-handler reads these fields; they stay as they are.
- From the tips phase: `config/guilds/designer.md` is the designer opening prompt with its `## Guild` section; `scripts/guilds/tips.py load --guild <id>` returns tips from one locked pull; `scripts/guilds/astra_work.py` writes and reads the `<!-- astra_work {JSON} -->` header (`PhaseWork` | `StandaloneWork`); `astra_review.py` takes `--run-directory` / `--phase` and saves that header in `prompt.md`; `"guilds"` is in `merge_gate.py` `TEST_SUITES` and `pyrightconfig.json` has a `scripts/guilds` entry. The daily reader reads `<roles-root>/<design_id>/prompts/*.md` headers, so durable prompts keep that header form.
- Scripts import repository modules as `from scripts.<folder> import <module>` after `sys.path.insert(0, <repo root>)` (as `astra_review.py` and `production_tree.py` do).
- Tests: never name a test helper `run` (it hides `unittest.TestCase.run`); point `ASTRA_ROLES_STATE`, `GUILD_ROLES_STATE`, `GUILD_OWNERSHIP_STATE` and `CLAUDE_CODE_SESSION_ID` at fixtures. basedpyright exits 3 for the missing `.venv`; only its footer counts.
- The design doc's Roles section states today's code limits this phase removes: `_seat_sandbox` treats any `instructions_file` as a read-only reviewer; `_resume_worker_thread` can start a replacement thread; `command_stop` and `_pause_roster` drop server records and pending text; `ensure_server` reuses a server whose run environment was set at its start; `attribute` gives every supplied path to a sole open phase; `_roster_folders` lists run roots and review-pool meshes only; `astra_review.py` starts a fresh `codex exec` thread every call, with no resume argument; `rename_unit.py` updates the review pool and the newest run but no role store.
- Roles, Names and the guild word list follow roles-unit's design (`origin/sdlc-roles`, `docs/plans/sdlc-roles-design.md`, D30–D32, D51–D52 and its Term table), with the differences the design's Command changes section lists. The hub writer for `commands/unit/direct.md` and `commands/showrunner/produce.md` follows the showrunner's answer on starting-skill files; each hub edit is named in the checkpoint notice.

**Acceptance gate:** the tests named in Files and the merge gate green; `basedpyright` clean on every changed folder (footer `0 errors, 0 warnings, 0 notes`). The checks in Steps → Phase 6 each have a test: two-host opens, aliases, lost acknowledgement, unknown creation, unreachable authority, host mismatch and open-after-end make no second pair; busy and idle moves without an existing marker keep queued text; rebind from run A to run B with A's folder present and the holder and account unchanged makes the next turn and patch check use B; a unit rename, its retry, then a resume of both original threads at the new paths; stale live fields fail the sweep while saved prompts, replies and aliases pass; end from `RoleNeedsRecovery` with a lost-thread fixture leaves the roster ended even if server cleanup returns 1. Live (director): adopt an idle designer, end its old run, resume that thread with its binding, and show one Astra edge in `production_tree.py --json`; a held reviewer resumes its thread and stays read-only. At run closeout the design becomes `docs/as-built/guilds.md`.
