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
- `production_tree.py --json` adds `AstraEdge` (`manager`, `member` = thread id, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) per live record and drops the Codex worker edge for that thread. `statusline.conf` has `role.astra-designer=mauve`, `role.astra-reviewer=bright-green`.

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

### Phase 4 — Each guild has a tip folder and views, and each starting skill carries the guild block  · status: done
**Depends on:** Phase 3

#### As-built

- `~/rust/nate_style` commit `5c3ec52b` (on `origin/main`) holds `guilds/designers/`, `reviewers/`, `showrunners/`, `units/` and `common/` with five seed tips (`same-thread.md`, `cite-evidence.md`, `relay-corrections.md`, `report-round-four.md`, `common/report-evidence.md`). Each tip has flow `tags: [guild/<id>, guild-event, head/<id>]`, `date_created`, `date_modified`, one `##` heading and a `Source:` line. `guilds/<id>_base/events.base` and `tools.base` are eight table views that filter `guild-event` or `guild-tool` and `guild/<id>` or `guild/common`.
- `scripts/guilds/tips.py`: `GUILDS`, `GUILD_BLOCK`, `guild_block(guild) -> str`, `load(guild) -> str`, `publish(head, folder) -> str`, `BaselineMissing(ValueError)`. The private `_lock(repo, timeout)` is `fcntl.flock` on `<git-common-dir>/guild-tips.lock` with a monotonic deadline: `LOAD_LOCK_TIMEOUT` (10 s) for load, `GIT_TIMEOUT` (60 s) for publish and each Git command. `load` pulls `--ff-only` once, then prints `commit <sha>` and `## guilds/<path>` with the text of each `guilds/common/*.md` and `guilds/<guild>/*.md` at that commit; a lock or pull failure prints one `tips:` stderr line and uses the local commit; no `guilds/` at `HEAD` raises `BaselineMissing`. `publish` runs on `main` only; it refuses a staged index, an uncommitted or ignored target, a path the head does not own (`<head>/`, `<head>_base/`, or a `common/` file whose only `head/` tag is `head/<head>`, YAML comments ignored) and foreign unpublished commits; on divergence it rebases only its own commit and retries one push; on a rebase conflict it aborts and keeps the local commit; it prints the commit after the push. CLI `load --guild`, `publish --head --from`: exit 2 for `BaselineMissing`, exit 1 for every other error.
- `scripts/guilds/astra_work.py`: frozen `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)`, `StandaloneWork()`, `AstraWork(role, member, document, assigned_at_utc, reply_path, work)` (`schema` 1), `HeaderMissing(path)`, `HeaderInvalid(path, reason)`. `phase_work(run_directory, phase_id) -> PhaseWork` reads `progress_history_state.json` (`open_phases` and the `active` `phase`; `ValueError` unless exactly one matches). `header_line(work) -> str`; `read_header(path)` checks the exact key set, `schema` 1 and a UTC stamp. CLI `brief --run-directory --phase --member --document --round --text-file` writes `<run>/design/phase<id>/round<N>_brief.md` by exclusive create and refuses when `round<N>_reply.md` exists (exit 2).
- `scripts/astra/astra_review.py` `main` takes `--run-directory <dir> --phase <id>` (both or neither) before the document. The first line of `prompt.md` is the header (`astra-reviewer`, `member` the run folder, `reply_path` its `review.md`). `review_prompt` appends `## Guild` with `tips.guild_block("reviewers")`, the load line, the Events pointer and `tips.load("reviewers")`. Exit 2 also covers an unresolved phase and `BaselineMissing`; exit 1 also covers a tips read error.
- Each starting skill has one `<Guild>` section: the design's Guild block for its guild, `Load tips: ~/.claude/scripts/lib/py ~/.claude/scripts/guilds/tips.py load --guild <id>`, and `Use Events in ~/.claude/docs/plans/sdlc-guilds-design.md for report detection and form.` `commands/unit/direct.md:25–52` adds the round-4 and repeated-title reports and the saved designer briefs and replies; `commands/showrunner/produce.md:328–336` adds the G4 relay; `commands/astra_review.md:7–24` holds Usage and the Step 1 / Step 3 flag and exit-2 rules; `config/guilds/designer.md` is the designer opening prompt (Does / Does not, then `## Guild`).

**Files:**
- `~/rust/nate_style/guilds/` — the five seed tips and eight views
- `scripts/guilds/tips.py` — the tips lock, `load`, `publish`, `guild_block`
- `scripts/guilds/astra_work.py` — the `astra_work` header, `phase_work`, `read_header`, `brief`
- `scripts/astra/astra_review.py` — run/phase inputs, the saved header, the reviewers block and tips in the prompt
- `commands/unit/direct.md`, `commands/showrunner/produce.md`, `commands/astra_review.md`, `config/guilds/designer.md` — one `<Guild>` section each
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — `"guilds"` suite; `pyrightconfig.json` — the `scripts/guilds` entry
- `scripts/guilds/test_tips.py`, `scripts/guilds/test_astra_work.py`, `scripts/astra/test_astra_review.py` — their tests

**Binds later work:** the head loop publishes through `tips.py publish` and handles its exit-1 refusals; the daily reader takes `read_header` states and joins on `run_id` and `phase_instance_id`; the role launcher builds a designer prompt from `config/guilds/designer.md` plus `tips.load("designers")` and a reviewer prompt from `review_prompt`, and writes its own headed prompt files; the five Events pointers name `~/.claude/docs/plans/sdlc-guilds-design.md` until the as-built move.

**Gotchas:** `merge_gate.py` `TEST_SUITES` and `test_merge_gate.py` `EXPECTED_SUITES` change together. `git status --porcelain` omits ignored files unless given `--ignored`. Every `/astra_review` pulls `~/rust/nate_style`, so a `guilds/` change reaches `origin/main` before `~/.claude` merges. The `nate_style` working tree holds `review-charter.md`, `.obsidian/` and untracked rule files that no guild command stages: commit exact paths, `pull --ff-only` first, push fast-forward only.

**Ruled out:** a non-blocking tips lock that fails a publish at once while a load holds it; a relative design-doc path in the guild sites.

### Phase 5 — The guilds production doc, its four head units and their daily read  · status: done
**Depends on:** Phase 4

#### As-built

- `docs/plans/guilds-production.md` is the top-level `guilds` production on natedev with no parent: merge branch `guilds`, showrunner checkout `/home/natepiano/worktrees/claude-guilds-trunk`, merge tests `python3 -m unittest discover -s scripts/guilds -p test_*.py`, and Units rows `designers`, `reviewers`, `showrunners`, `units` in herdr tab order after the showrunner. Its rules resolve each head afresh from `message/roster.py --json` and keep four timers `guilds-<guild>-daily` (`notifier.sh new … --every 1440 --to session:<id>`), recreated after a head restart.
- `docs/plans/guilds-<guild>.md` (four) carry `> **Standing:** guild-head` and sections Duty, Publish (the `tips.py publish --head <guild> --from <folder>` contract and the seed tip form) and Daily check-in: covered UTC intervals, host-labelled results and missing evidence go in a git-excluded `daily-report.md` at the worktree root; natedev runs `daily_read.py`, the Mac runs it once through its roster session.
- `commands/unit/direct.md` `<GuildHeadStanding>` (`:54–73`) takes precedence at startup, restart, compaction, phase selection and `<StandingPlanRefill>` entry: a guild-head plan runs its report/tip/reply and daily loop with no refill, phase, worker, checkpoint, shrink, FinalGate or RunSummary. `docs/production_format.md` `<GuildHeads>` (`:23–38`) states the four unsuffixed head IDs and the same exception.
- `scripts/production/add_unit.py`: `GUILDS_PRODUCTION = "guilds"`, `GUILD_HEAD_IDS` (`:77–78`); `launch_request` (`:361`) keeps those exact IDs only in production `guilds`; every other name gets `-unit`.
- `scripts/guilds/daily_read.py --guild <id> --since <UTC> --until <UTC> [--json]`: frozen `LocalSources(run_roots, history_root, astra_root, roles_root, host)` with `configured()` (`GUILD_DAILY_RUN_ROOTS`, `GUILD_DAILY_HISTORY_ROOT`, `GUILD_DAILY_ASTRA_ROOT`, `GUILD_DAILY_ROLES_ROOT`, `GUILD_DAILY_HOST`); `read_daily(guild, since, until, sources) -> dict[str, object]` (`ValueError`, CLI exit 2, for an unknown guild or a non-UTC or empty interval). It joins `astra_work` headers (`design/phase*/round*_brief.md`, `<astra_root>/*/prompt.md`, `<roles_root>/*/prompts/*.md`) to `rounds.load_phases` / `phase_rounds`, `phase_process` step allocation with a 14-complete-day p90 (20 samples minimum) and findings ledgers; `repeated_titles` applies the Events repeat rule (`RepeatedTitle` / `RepeatNotProven`, both event IDs cited). Flags: `RepairRoundsBeyondThree`, `MixedPhaseRecord`, `SlowStep`, `RepeatedTitle`. Waits and receipts are one fixed row each, `HistoryNotRecorded` / `ReceiptMissing` with source `WriterNotRecorded`; a guild with no interval member gets `MembershipNotRecorded` naming its read roots.

**Files:**
- `docs/plans/guilds-production.md` — the guilds production doc
- `docs/plans/guilds-designers.md`, `guilds-reviewers.md`, `guilds-showrunners.md`, `guilds-units.md` — the head standing plans
- `commands/unit/direct.md` — `<GuildHeadStanding>`; `docs/production_format.md` — `<GuildHeads>`
- `scripts/production/add_unit.py` — exact head IDs in production `guilds`
- `scripts/guilds/daily_read.py` — the daily read; `pyrightconfig.json` — `scripts/history` in the `scripts/guilds` extraPaths
- `scripts/guilds/test_daily_read.py`, `scripts/production/test_add_unit.py` — their tests

**Binds later work:** the lasting-role launcher saves prompts at `<roles_root>/<design_id>/prompts/*.md` with absolute `document` and `run_directory`, and each reply at its header `reply_path`, which the reader checks for `OutputRecorded`. The Events pointers in the four standing plans (`:11`) and the production doc's design paths (`:7`, `:50`) move at the as-built move.

**Gotchas:** every progress-history event has `skill` `plan-delegate` and no showrunner writes run history, so the reader's showrunner `skill` selector (`:310–312`) matches fixtures only. Old headerless `/tmp/claude/astra/*/prompt.md` files give one `MembershipNotRecorded` problem each on every designers or reviewers read, whatever the interval.

**Ruled out:** reader parsing of hierarchy wait/receipt feeds before their writers exist; a recursive showrunner-state scan; a `production_root` source.

### Phase 6 — The designer and reviewer roles open, resume and outlive a run, under a unit director or a showrunner  · status: done
**Depends on:** Phase 4 (its designer opening prompt and tip baseline; Astra review round 1, 2026-10-21)

#### As-built

- `scripts/guilds/roles.py` keeps one designer and one reviewer per design. Store: `GUILD_ROLES_STATE`, else `~/.local/state/guilds/roles/`; one directory per UUID `design_id` with `design.json`, a lock, `mesh/` and `prompts/`; role IDs `<design_id>/designer|reviewer`. Frozen types `UnitHolder(production_slug, unit_name)` | `ShowrunnerHolder(production_slug)`, `DesignUnbound` | `DesignBoundToPhase(run_directory, phase, repository, output_paths)`, `PhaseBoundDesigner(binding_generation, launch_sandbox)` | `ReadOnlyReviewer`, `RoleNotOpened` | `RoleOpen` | `RolePausedForMove` | `RoleNeedsRecovery` | `RoleEnded`, `DesignPlacement(design_id, host, generation)`, `HeldDesign`, `PendingMessage`, `HolderReach` | `HolderUnavailable`; refusals raise `RoleRefused` (`ValueError`), CLI exit 2. Functions: `open_role`, `adopt`, `resume`, `recover`, `rebind`, `end`, `queue_message`, `pending_messages`, `list_roles`, `held_document`, `unbind_run`, `end_holder`, `check_patch`, `rename_holder`, `rename_failures`, `transfer_route`, `durable_meshes`, `register_repository`, `authority_request`. CLI verbs: `open`, `adopt`, `resume`, `end`, `recover`, `rebind`, `unbind-run`, `list --json`, `authority`, `register`.
- Ownership: `config/guilds.conf` pins `[ownership] host=natedev`; only that host writes `GUILD_OWNERSHIP_STATE` (else `~/.local/state/guilds/ownership/`). `[roles] new_open=false` refuses every new open and adoption and keeps existing identities. The model comes from `agents_resolve "guild.<role>"` (family `codex`); a missing or invalid row refuses the launch.
- Prompts: a designer prompt is `config/guilds/designer.md` plus `tips.load("designers")`; a reviewer prompt is `astra_review.review_prompt`. Each is saved by exclusive create in `prompts/` with an `astra_work.header_line` first line, and each reply goes to its header `reply_path`. `open` on `RoleEnded` refuses: `role ended; this design identity cannot be reopened.`
- `scripts/agents/codex_mesh.py`: the roster `access_policy` sets the sandbox; `_claim_follow` runs under `_design_claim_lock`; `_role_fence` makes every old route (follow, reopen, account-move resume) refuse a `TransferringRole` entry; resume keeps the same thread or gives `RoleNeedsRecovery`; raw `stop --pause`, raw `stop` and `set-instructions` refuse held roles; `_guild_context` is compared with the server's saved context and a mismatch runs `_release_held_server`; `held_thread_end_confirmed(session_dir, thread_id)` is the proof `roles.end` needs after uncertain cleanup; `retire_workers_servers` adds `roles.durable_meshes()`, so `accounts.py` `_swap_workers` moves held roles with no change of its own.
- Integrations: `astra_roles.py` `HeldDesignRole(design_id, role_id)` (a durable record adds the keys `design_id`, `role_id`); `astra_review.py` `held_review` sends a held document to its sole reviewer through `roles.resume`, refuses busy or unavailable, and keeps the one-turn `codex exec` path for other documents; the berth hook calls `roles.check_patch` with `GUILD_BINDING_GENERATION`; `mesh_runs.py` lists durable meshes and queues a held role's detached result with `roles.queue_message` (stable ID, no wake); `roster.py` `_durable_roles` lists held roles; `end_session.sh` runs `roles.py unbind-run` per run; `implement.sh --to` refuses a fenced or held entry; `retire_unit.py` calls `roles.end_holder`; `rename_unit.py` calls `roles.rename_holder` in `_move_state` and `roles.rename_failures` in `_state_failures`; `unit_rename_sweep.py` reads saved prompts and replies as history and aliases as identity.
- `daily_read.py` selects `units` runs by `progress_history.SKILL_NAME`; `showrunners` gives no phase rows and one `HistoryNotRecorded` / `WriterNotRecorded` problem; a headerless `<astra_root>/*/prompt.md` older than `since` is skipped. `add_unit.py` keeps the ID of an exact `Unit` row match in any production, else `<name>-unit`.
- Command text: `commands/unit/direct.md` `<HeldAstraRoles>`, `commands/showrunner/produce.md` `<HeldProductionDesign>`, `commands/message.md` and `codex/AGENTS-messaging.md` `<HeldRoleMessages>`, `commands/astra_review.md` Steps (held-thread selection, busy refusal, the one-turn case).
- Live check not yet run: the installed `~/.claude/scripts` has no `roles.py` until this branch merges. After merge and install: adopt an idle designer, end its old run, resume that thread with its binding, and see one Astra edge in `production_tree.py --json`; resume a held reviewer and confirm it stays read-only.

**Files:**
- `scripts/guilds/roles.py` — role store, authority client, launcher CLI; `config/guilds.conf` — authority pin and `new_open`
- `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py`, `scripts/message/roster.py` — held-role mesh behavior and discovery
- `scripts/astra/astra_roles.py`, `scripts/astra/astra_review.py` — census variant and held reviewer route
- `scripts/hooks/pre-tool-use-berth.py` — binding check before attribution
- `scripts/direct/end_session.sh`, `scripts/direct/implement.sh`, `scripts/production/retire_unit.py`, `scripts/production/rename_unit.py`, `scripts/production/unit_rename_sweep.py` — lifecycle integrations
- `scripts/guilds/daily_read.py`, `scripts/production/add_unit.py` — run selector and unit ID rule
- `pyrightconfig.json` — `scripts/guilds` extraPaths add `scripts/agents`, `scripts/astra`, `scripts/production`, `scripts/message`
- `scripts/guilds/test_roles.py`, `scripts/production/test_unit_rename_sweep.py` (new) and the tests beside each changed script

**Binds later work:** the run closeout moves the design to `docs/as-built/guilds.md` and, in the same commit, moves each Events pointer and design path to `~/.claude/docs/as-built/guilds.md`: `commands/unit/direct.md:30`, `commands/showrunner/produce.md:333`, `commands/astra_review.md:13`, `config/guilds/designer.md:15`, `scripts/astra/astra_review.py:74` (`review_prompt`), `docs/plans/guilds-designers.md:11`, `docs/plans/guilds-reviewers.md:11`, `docs/plans/guilds-showrunners.md:11`, `docs/plans/guilds-units.md:11`, and the design paths in `docs/plans/guilds-production.md:7` and `:50`. A showrunner may hold a read-only role but cannot launch an unbound writer until phases-unit's production-design phase exists.

**Gotchas:** the server context comparison uses JSON-normalized values; a tuple `output_paths` against the saved JSON list never matches, and the release then takes the pending lock a second time. The adopt commit writes `adoption.json` before it removes `adoption_required`, so a same-ID retry completes after a failure at any step. Account-move recovery checks the saved header before it claims the roster entry. The berth check covers `apply_patch` only, so designers edit by patch.

**Ruled out:** a module function named `open` (it hides the builtin; the CLI verb stays `open`); an `accounts.py` change for held roles.
