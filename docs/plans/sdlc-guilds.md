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

#### Work Order

**Goal:** `docs/plans/sdlc-guilds-design.md` describes the guild process the user decided on 2026-10-21 (G1–G10) and keeps its lasting-role design, so Phases 4–6 take their Spec from it.

**Spec:**
- **Keep the lasting-role half** (Lessons, Roles, Names, and the role parts of Choices, Steps and Command changes). Apply the showrunner's settled answers (Decisions): Q1 both holders, one per design; Q2 a separate Astra store on the mesh lifecycle; Q5 the production prefix, adopted mesh names kept as aliases; Q8 names now; Q12 pending records, no automatic wake; Q14 one natedev registry, each pair on its holder's host; Q16 adopt idle designers with an explicit ownership transfer. Remove each settled question from Questions for the user and state its answer where the design uses it.
- **Replace the guild half** with the G1–G10 model (Source):
  - **Guilds (G1, G7):** `designers` (Astra designers), `reviewers` (Astra reviewers), `showrunners`, `units` (unit directors). Workers — Codex code and test threads, pooled code and UI reviewers — have no guild. Say how a session knows its guild from what it already is (its skill, its role record), with no join or leave step.
  - **Heads (G1b, G6, G7):** one head per guild, each a unit director of the lasting top-level `guilds` production on natedev. herdr: a top-level `guilds` space, the guilds showrunner in tab 1, then `designers`, `reviewers`, `showrunners`, `units`. Say what each head's standing plan does (a head runs no phased build; define its loop), how a member finds its head's address (`showrunners.py address`-style lookup or the roster), and what the guilds showrunner does (relays the user, keeps heads running, reports).
  - **Events (G3):** the five events, each with its exact trigger and where a member detects it (the user's correction in the conversation; `findings.py gate` round 4; a finding title that repeats in one run's ledger; a tool fault or slow rule worked around; a saved-time way). One short message to the head only, its form and length, sent with `~/.claude/scripts/message/send.py` or `SendMessage`. A Codex member (designer, reviewer thread) sends through its holder or its own `send.py` call — say which, given that a Codex thread between turns takes no peer message (`commands/message.md:35`).
  - **Daily read (G2, G10):** once a day each head reads `scripts/history/rounds.py`, `scripts/direct/phase_process.py` and the findings ledgers for its members, plus hierarchies-unit's wait events and merge receipts when they exist (its Step 2, Q8). It reads turns only where a record looks bad; define "looks bad" with numbers from today's records. Say what runs the read (a scheduled check-in of the head session, or a script the head runs) and what it prints.
  - **Tips (G3b, G4):** `~/rust/nate_style/guilds/<guild>/` per guild and `~/rust/nate_style/guilds/common/`, one tip per markdown file in the rust-rule form (YAML front matter with `tags`, `date_created`, `date_modified`; one `##` heading; text); a base folder per guild of Obsidian `.base` views by tag, like `rust_base/`. Only the head adds and edits its tips; the user's feedback reaches the head in its tab or through any showrunner, and the head writes it as a tip. Tips reach both machines by git push and pull of `nate_style`; say who pushes and who pulls and when. A member loads its guild's tips (and `common/`) when its skill starts. No copy step, publication manifest, receipt or `docs/guilds`.
  - **Guild block (G5):** the exact short block for `/showrunner:produce`, `/unit:direct`, the designer opening prompt and the `/astra_review` reviewer prompt: who your head is, load your guild's tips, the G3 events, how to write to the head. Name where each block goes (file and section) and each file's owner from the production doc's hub table; a `commands/unit/direct.md` change goes to phases-unit and `commands/showrunner/produce.md` to roles-unit as hub contributions.
- **Rewrite Steps** to the new phases: Phase 4 tips and guild blocks, Phase 5 the guilds production doc and the daily read, Phase 6 the lasting roles. Each step keeps Changes, Checks and Undo.
- **Questions for the user** keeps only questions the revision itself raises about a role, responsibility, name or process. Each names the answers and a recommendation.
- **Cut** what the guild change makes dead: design-guild/review-guild, `scripts/guilds/guilds.py` publication, `broadcast.py --guild`, receipts, manifests, delivery marks, guild-head questions. The doc does not grow: at most its present 308 lines.
- **Cross-reads:** roles-unit's duty map (`origin/sdlc-roles`, `docs/plans/sdlc-roles-design.md`, Duty matrix, Term table) and phases-unit's Map (`origin/sdlc-phases`, `docs/plans/sdlc-phases-design.md`). Where a row differs, the design says so and why.

**Process:** the designer thread `claude-build-followups-sdlc-guilds-design` revises (resumed with `implement.sh --to`; it keeps its Phase 2 context). Then Astra review rounds through `/astra_review` (`~/.claude/scripts/lib/py ~/.claude/scripts/astra/astra_review.py <doc> <focus>`), at least 2 and at most 3, each round's doc and review saved under `${SESSION_DIR}/design/phase3/`. Give the reviewer the Key files list. Before the last round the designer runs a simplicity pass: cut each mechanism that adds a concept with no correctness gain. The designer pushes back on findings that add a concept with no correctness gain. The tester slot stays closed.

**Files:**
- `docs/plans/sdlc-guilds-design.md` — the revised design

**Workers:** `1 writer` — the Astra designer; one document.
- `design` — `docs/plans/sdlc-guilds-design.md`

**Constraints from prior phases:** the Phase 2 As-built (above): the doc authorizes no implementation by itself; `astra_review.py` starts a fresh `codex exec` thread each call. Phase 1's `AstraEdge` fields and the `astra-designer` / `astra-reviewer` ids stay.

**Acceptance gate:** at least 2 Astra rounds saved with their reviews; the last review has no open defect the director accepts; the doc is at most 308 lines; each G1–G10 decision appears where the design uses it; no forbidden word (the forbidden-words hook passes); the merge gate green (`env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only`). At phase end the unit director sends the new versions of the held messages to cargo-liner (guild view: four guilds, heads, tip folders), roles-unit (rows that differ) and phases-unit (rows that differ and `direct.md` requests).

### Phase 4 — Each guild has a tip folder and views, and each starting skill carries the guild block  · status: todo
**Depends on:** Phase 3

**Pending decision: who edits each starting-skill file.** Before dispatch the unit director sends the showrunner the list of files that get the guild block (`commands/showrunner/produce.md`, `commands/unit/direct.md`, the designer opening prompt file, `commands/astra_review.md`, as the Phase 3 design names them); the showrunner settles who edits each one.

**Settled 2026-10-21 (showrunner):** the Owns cell covers `~/rust/nate_style/guilds/` as a separate repo. Commit on its `main` after the phase gates, staging exact paths only (never `review-charter.md` or `.obsidian/`), `git pull --ff-only` first, push fast-forward only; each checkpoint notice names the `nate_style` commit.

#### Work Order

**Goal:** `~/rust/nate_style/guilds/` holds a folder per guild, a `common/` folder and a base folder of views per guild, and each skill that starts a member role tells the member its head, its tips, the five events and how to write to the head.

**Spec:** `docs/plans/sdlc-guilds-design.md`, sections Tips, Events and Guild block, and Steps → Phase 4. The Phase 3 phase review fills the exact paths and text from the revised doc.

**Files:**
- `~/rust/nate_style/guilds/designers/`, `reviewers/`, `showrunners/`, `units/`, `common/` — new; a seed tip per folder only where the design names one
- `~/rust/nate_style/guilds/` base folders — new: Obsidian `.base` views by tag, like `rust_base/`
- `commands/showrunner/produce.md` — hub (owner roles-unit): the guild block
- `commands/unit/direct.md` — hub (owner phases-unit): the guild block
- `commands/astra_review.md` — the reviewer guild block
- the designer opening prompt file the design names — the designer guild block

**Workers:** `2 writers` — the `nate_style` folders, and the skill blocks, on disjoint files.

**Constraints from prior phases:** a rule file in `nate_style` has YAML front matter then one `##` heading; a `.base` view filters by `file.hasTag("<tag>")` (What exists today). Do not stage `review-charter.md` or `.obsidian/` in `nate_style`. Each hub edit is named in the checkpoint notice as `also touches <path> (owner <unit>), tested against <owner tip>` (`docs/production_format.md` <ProductionUnit/> item 9).

**Acceptance gate:** each `.base` file parses as YAML and each tip has `tags`; each of the four starting skills has the block, word for word as the design gives it; the merge gate green; live (director): a new session that loads `/unit:direct` names its head and lists the `units` tips.

### Phase 5 — The guilds production doc, its four head units and their daily read  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** the sdlc showrunner can start the top-level `guilds` showrunner and its four head unit directors from one production doc, and each head reads its members' records once a day.

**Spec:** `docs/plans/sdlc-guilds-design.md`, sections Heads and Daily read, and Steps → Phase 5; `docs/production_format.md` for the production doc form. The Phase 3 phase review fills the exact paths from the revised doc.

**Files:**
- `docs/plans/guilds-production.md` — new: the guilds production doc (top level, herdr space `guilds`, units `designers`, `reviewers`, `showrunners`, `units` in that tab order)
- `docs/plans/guilds-designers.md`, `guilds-reviewers.md`, `guilds-showrunners.md`, `guilds-units.md` — new: each head's standing plan
- the daily-read script the design names, and its test

**Workers:** `1 writer + 1 tester` — the docs and the script, and the script's tests.

**Constraints from prior phases:** the tip folders and guild blocks as Phase 4 built them. The heads are unit directors, not showrunners (G1b). The production doc names no goddess name (G7).

**Acceptance gate:** the daily-read tests and the merge gate green; `basedpyright` clean on the script's folder; live (director): the daily read prints one day of records for one guild from this run's ledgers. At phase end the unit director sends the showrunner the production doc path to start the guilds showrunner.

### Phase 6 — The designer and reviewer roles open, resume and outlive a run, under a unit director or a showrunner  · status: todo
**Depends on:** Phase 4 (its designer opening prompt and tip baseline; Astra review round 1, 2026-10-21)

The Phase 2 role questions are settled (Decisions: Q1, Q2, Q5, Q8, Q12, Q14, Q16); the Phase 3 phase review refreshes this Work Order from the revised doc.

#### Work Order

**Goal:** a unit director or a showrunner opens a named designer or reviewer once and resumes the same thread across phases, runs and account moves.

**Spec:** `docs/plans/sdlc-guilds-design.md`, sections Roles and Names, as Phase 3 revised them with the settled answers (Decisions); those sections name each function this phase changes. Its Steps → lasting-roles text (Phase 3 renumbers it Phase 6):
- Implement the role store, registry, authority, held-review route, lifecycle, access policy, atomic claim, binding, adoption and holds. Add the `config/guilds.conf`, mesh, account, hook, roster, detached-result, cleanup, retirement, transport and `commands/astra_review.md` integrations, with tests and the `scripts/guilds` pyright entry. Coordinate the reservation bridge with phases-unit and hub changes with roles-unit; preserve the Astra edge wire IDs and color keys.
- Model and effort come from `config/agents.conf` rows `[guild.codex] designer=gpt-6-astra:xhigh`, `reviewer=gpt-6-astra:xhigh`, assignment `guild=codex`, resolved through `scripts/agents/agents_config.sh`; never hardcoded, no fallback.
- Checks: prove one pair across simultaneous two-host opens, lost acknowledgement, aliases and unreachable authority; reject holder/pair host mismatch at open or transfer and `open` after end. Preserve threads across revisions/run end, holder restart and incomplete holds. Exercise busy and idle moves **without an existing marker**, preserving queued text: mark before interrupt, release the old writer, continue interrupted work, move idle work on its next assignment. Cover paused-state entry/recovery, racing claims and failure before marker/acceptance; raw `stop --pause` refuses held roles.
- Check bound edits and refusal of unbound, stale or unlisted paths, including a sole open phase, mixed patches, symlink escape and rename destination; refusal acquires no reservation and writes nothing. Prove new-path steering cannot widen an active binding; idle rebind permits the same thread's next turn. Race follows, publication and pending sends in the stated lock order; reject held-role setters, stale generations and uncertain submissions. Preserve access and interrupted prompts; rejected claims start no turn. Check end from `RoleNeedsRecovery`: a lost-thread fixture must leave the roster ended even if server cleanup returns 1.
- Adoption fixtures cover both known launches, concurrent legacy follow, cleanup during transfer, release failure, crash boundaries and retries: same thread/name, one owner/hold, preserved messages/evidence, all old dispatches refused. Tests use isolated fixtures.
- Undo: disable new dispatch/adoption, finish active turns, and retain placements, transfer fences, durable stores and evidence. Use the safe server-release rules, never raw `stop --pause`. For an adopted role, retain destination ownership or reverse transfer only after proving it has no writer. Restore old command text only when it cannot bypass fences. Preserve unfinished reservations; never replace a thread silently.

**Files:**
- `scripts/guilds/roles.py` — new: role store, ownership-authority client, `open`, `adopt`, `resume`, `rebind`, `end`, `list --json`
- `scripts/guilds/test_roles.py` — new
- `config/guilds.conf` — new: the natedev ownership authority pin
- `scripts/astra/astra_roles.py` — a third hold lifetime tied to the role store
- `scripts/astra/astra_review.py` — finds and resumes the held reviewer; busy refusal; the one-turn case
- `commands/astra_review.md` — Usage and Steps: held-thread selection, busy refusal, the one-turn case
- `scripts/agents/codex_mesh.py` — explicit access policy, atomic instruction-and-turn claim, strict same-thread resume, durable-mesh discovery in account moves and server cleanup, adoption fences, held-role refusals
- `scripts/agents/mesh_runs.py` — durable role roots in `_roster_folders`; detached results reach a held role
- `scripts/accounts/accounts.py` — `_swap_workers` moves held roles
- `scripts/hooks/pre-tool-use-berth.py` — `decide` checks every edited path against the binding's `output_paths`
- `scripts/message/roster.py` — discovers durable roles
- `scripts/message/send.py` — only if `roles.py` reuses its SSH `remote` route to reach the authority
- `scripts/direct/end_session.sh` — unbinds held designs and keeps their threads
- `scripts/direct/implement.sh` — `--to` honors adoption fences
- `scripts/production/retire_unit.py` — ends a retired unit's held roles
- `config/agents.conf` — `[guild.codex]` rows and the `guild` assignment
- `pyrightconfig.json` — one `executionEnvironments` entry for `scripts/guilds`
- `scripts/astra/test_astra_roles.py`, `scripts/astra/test_astra_review.py`, `scripts/agents/test_codex_mesh.py`, `scripts/agents/test_mesh_runs.py`, `scripts/accounts/test_accounts.py`, `scripts/hooks/test_berth_hook.py`, `scripts/message/test_roster.py`, `scripts/direct/test_end_session.py`, `scripts/direct/test_implement_launcher.py`, `scripts/production/test_retire_unit.py` — their tests

**Workers:** `1 writer + 1 tester` — the code and its tests split cleanly; in the Astra-record work the two owner sets were independent and the test lane was real, and each changed script has its `test_*.py` beside it.
- `impl` — `scripts/guilds/roles.py`, `config/guilds.conf`, `scripts/astra/astra_roles.py`, `scripts/astra/astra_review.py`, `commands/astra_review.md`, `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py`, `scripts/accounts/accounts.py`, `scripts/hooks/pre-tool-use-berth.py`, `scripts/message/roster.py`, `scripts/message/send.py`, `scripts/direct/end_session.sh`, `scripts/direct/implement.sh`, `scripts/production/retire_unit.py`, `config/agents.conf`; hub: `pyrightconfig.json` (one entry; another unit may also add one)
- `test` — `scripts/guilds/test_roles.py` and the ten existing test files above, written from the Spec alone

**Constraints from prior phases:**
- The Astra role record (`scripts/astra/astra_roles.py`) is one file per thread, `~/.local/state/astra/roles/<thread id>.json` (override `ASTRA_ROLES_STATE`), written by temp file and rename. On disk it is `RoleRecord`, exactly the keys `role` (`designer` | `reviewer`), `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, with nulls for the other lifetime. `_parse_record` refuses a missing or extra key, a wrong type, or a `thread_id` that differs from the file name.
- In the library a record is `HeldRole` (`live_roles()` returns `LiveRole`, which adds `state: Literal["busy", "idle"]`) with `lifetime: MeshWorker | ExecProcess`: `MeshWorker(session_dir, worker)` or `ExecProcess(pid)`, both frozen dataclasses. Functions: `hold(role, thread_id="", codex_home="", *, session_dir=None, worker=None, pid=None, manager=None) -> HeldRole` (raises `ValueError`; `manager` defaults to `$CLAUDE_CODE_SESSION_ID`), `release(thread_id) -> None` (a missing record is not an error), `live_roles() -> list[LiveRole]`. CLI: `hold designer|reviewer`, `release <thread id>`, `list [--json]`; exit 2 with one stderr line on a refusal.
- `live_roles()` removes a `MeshWorker` record when its run folder is gone, the roster has no such worker, the roster thread differs, or the status is `ended` or `failed`; it removes an `ExecProcess` record when its pid is gone. It keeps a record whose roster is unreadable and prints one stderr line naming the file. `state` is `busy` for roster status `starting`, `running` or `waiting_capacity`, or a running pid; else `idle`.
- A lasting designer or reviewer thread outlives its run folder, so it cannot use `MeshWorker`, and outlives one process, so it cannot use `ExecProcess`. It needs a third lifetime variant tied to the thread store, with its own on-disk keys added to `RoleRecord` and to `_parse_record`'s key check; the variant names what keeps the hold alive (type design contract).
- `astra_review.py` `run_review()` holds an `ExecProcess` reviewer record for the life of its `codex exec` only when `$CLAUDE_CODE_SESSION_ID` is set, after it reads `session id: <thread>` from `codex.log` (at most `THREAD_HEADER_TIMEOUT` = 30 s), with `codex_home` from `accounts.py env workers`, and releases it in a `finally`. A resumed held reviewer keeps one record per thread id: the file name is the thread id.
- `production_tree.py --json` prints one `AstraEdge` per live record (`manager`, `member` = thread id, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) and drops the Codex worker edge for that thread (`worker_edges(running, astra_threads)`). cargo-handler reads these fields; they stay as they are.
- Scripts import the record module as `from scripts.astra import astra_roles` after `sys.path.insert(0, <repo root>)` (as `astra_review.py` and `production_tree.py` do).
- Tests: never name a test helper `run` (it hides `unittest.TestCase.run`); point `ASTRA_ROLES_STATE` and `CLAUDE_CODE_SESSION_ID` at fixtures. basedpyright exits 3 for the missing `.venv`; only its footer counts.
- The design doc's Roles section states today's code limits this phase removes: `codex_mesh.py` `_seat_sandbox` treats any `instructions_file` as a read-only reviewer; `_resume_worker_thread` can start a replacement thread; `command_stop` and `_pause_roster` drop server records and pending text; `phase_reservation.py` `attribute` gives every supplied path to a sole open phase; `mesh_runs.py` `_roster_folders` lists run roots and review-pool meshes only; `astra_review.py` starts a fresh `codex exec` thread every call, with no resume argument.
- Roles, Names and the guild word list follow roles-unit's design (`origin/sdlc-roles`, `docs/plans/sdlc-roles-design.md`, D30–D32, D51–D52 and its Term table); its Q1, Q3 and Q5 answers bind this phase as the design doc's Questions section says.

**Acceptance gate:** the tests named in Files and the merge gate green; `basedpyright` clean on every changed folder; live (director): adopt an idle designer, finish its old run, resume the same thread through the new role, and show one Astra edge in `production_tree.py --json`.
