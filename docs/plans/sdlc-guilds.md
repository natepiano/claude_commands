# sdlc-guilds

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A lasting, resumable designer role and reviewer role that a unit director or a showrunner holds, a designer guild and a reviewer guild that share tips and common documents over our messaging, and named roles throughout.

> **As-built disposition: create** — `docs/as-built/guilds.md`

> **Production: sdlc** — unit `guilds-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 EDT

a designer role is something that we want to, you know, be able to resume and keep around under a unit director. It could also be a role under a showrunner that you keep around. So it can and probably should be both. ... we need to have like a guild of designers that needs to be designed. And we need to keep Cargo Liner notified so Cargo Handler can help us visualize it with, you know, simple text. And I think we also need to have a guild for reviewers, which needs to be designed and basically works the same way. So the designers can, you know, focus on the design skill and reviewers can focus on the review skill, but share tips and tricks and common documentation.

For the guild work ... make sure there's like a guild mechanism, probably uses our messaging infrastructure with these roles. And, you know, we have to cover the naming conventions, and it should be probably all named roles.

I like goddesses of wisdom and judgement for the chief designers and reviewers. Probably each is a top level showrunner.

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

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

## Decisions

- **Phase 1 delivers the Astra edges cargo-liner asked for, before any design** (showrunner, 2026-10-20 16:48: "astra edge = guilds-unit first phase, target before 21:45 EDT"). It records which session holds an Astra role so cargo-handler shows it; it changes no process, so it needs no question.
- **Every change to a role, a responsibility, a name or the process is a question for the user** (user, 2026-10-20). Phase 2 writes the design and ends with the questions; Phases 3–5 each carry a Pending decision and do not dispatch before the user's answers (plan author).
- **The design is written by one Astra designer and reviewed by Astra, at least 2 rounds and at most 3, each round saved** (user's rule to fps/frame-time-unit, 2026-10-20 16:19; showrunner's answer to experiments-unit, 16:48). The unit director briefs, passes each review and judges. The tester slot stays closed in that phase: the rule allows one designer and one reviewer (plan author, from the user's rule).
- **One design doc**: `docs/plans/sdlc-guilds-design.md` (plan author). It becomes `docs/as-built/guilds.md` at run end.
- **The edge role ids `astra-designer` and `astra-reviewer` name the Codex role a session holds** (cargo-liner's Phase 21 contract). The guild heads use the same ids until the user picks the goddess names (production doc, Guild heads). The design doc names both uses apart and asks the user for the names through cargo-liner (cargo-liner picks role names with the user, production rules).
- **This unit designs the guilds and the roles; it does not run the guild heads** (production doc, Guild heads). Starting a head is a question in Phase 2.
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

#### Work Order

**Goal:** `docs/plans/sdlc-guilds-design.md` defines both roles under a unit director and under a showrunner, the naming rules, and the guild mechanism, and ends with the user's questions.

**Spec:**
- The designer is one `gpt-6-astra` xhigh Codex thread: copy `~/.claude/config/agents.conf` to `$SESSION_DIR/agents_astra.conf` with `[delegate.codex] impl=gpt-6-astra:xhigh` and `fix=gpt-6-astra:xhigh`, launch with `AGENTS_CONFIG_FILE=$SESSION_DIR/agents_astra.conf`, resume each revision with `implement.sh --to <impl worker>` (as experiments-unit did). Once Phase 1 has merged, `astra_roles.py hold designer` for it; the doc needs no Phase 1 code, so the two phases run side by side (showrunner, 2026-10-20). Review is `/astra_review docs/plans/sdlc-guilds-design.md <focus>`, at least 2 rounds and at most 3; the designer answers each finding in the same thread with what it changed or why it disagrees; the unit director rules on disagreements from the files cited. Copy the doc after each round to `$SESSION_DIR/design/round<N>.md`.
- Sections of the design doc:
  - **Lessons.** What fps/frame-time-unit, experiments-unit and hierarchies-unit learned, each with its source file and time. At least: the berth refusal of a designer outside a phase reservation; steering new scope into a running designer turn; the review pool as the model for a lasting thread outside the run roots; that a Codex member between turns takes no peer message. Before the gate, read the experiments notes again for section (d) and the fps designer folder for its review rounds, and add what is new.
  - **Roles.** For the designer and for the reviewer: what it does and does not do; who holds it (a unit director, a showrunner, or both, as the user asked); how it opens, resumes, pauses for an account move and ends; where its thread record lives so it outlives one run; its model and effort, from `config/agents.conf` rather than hardcoded; how its output file is reserved so the berth hook lets it write; how many of each per design (the user's rule: one each); how the reviewer differs from the review pool's `code` and `ui` reviewers and from one-turn `/astra_review`.
  - **Names.** The naming rule for each role instance and for each guild, on the model of `<area>-unit`; the two uses of `astra-designer` and `astra-reviewer` named apart; the goddess names for the heads left to the user through cargo-liner. Read roles-unit's plan (`docs/plans/sdlc-roles.md` on `origin/sdlc-roles`) first and use its names where it has them.
  - **Guilds.** Membership: where it lives (a machine record, like `~/.local/state/showrunner/<slug>/parent`), who joins and leaves it and when. Shared documents: where the common tips and each guild's own tips live, how a member's tip reaches the head and how the head's change reaches every member (the review pool's `set-instructions` route). Messaging: a guild group in `broadcast.py` beside `WorkersOfUnit`, and how a message reaches a Codex member that is between turns (through its holder). The head's duties, its relation to this unit and to the sdlc showrunner (findings come back through the showrunner). The data cargo-handler needs for a simple-text guild view (one `--json` command, its fields).
  - **Command changes.** For `commands/unit/direct.md`, `commands/showrunner/produce.md`, `docs/production_format.md`, `commands/message.md` and `codex/AGENTS-messaging.md`: the section that changes and what it says, one line each.
  - **Choices.** Each open choice the sections leave (at least: designer under a unit, a showrunner, or both by default; reviewer as a new type in the review pool or a separate store; one guild record for both guilds or one each; heads started now or later), with two or more answers, what each costs, and the one recommended.
  - **Steps.** Phases 3–5 below, each with what changes, how to undo it, and which question it waits on.
  - **Questions for the user.** One plain line each, with the recommended answer.
- Before the gate, the unit director runs `/adversarial` on the Roles and Guilds sections and folds the findings in.

**Files:**
- `docs/plans/sdlc-guilds-design.md` — new

**Workers:** `1 writer` — one doc, and the user's rule allows one designer and one Astra reviewer per design, so the tester slot is not opened.
- `impl` — `docs/plans/sdlc-guilds-design.md`, as the Astra designer above

**Constraints from prior phases:** the Astra role record is in place (commit `cf9524bb`). The unit director records the designer for cargo-handler with `python3 scripts/astra/astra_roles.py hold designer --session-dir "$SESSION_DIR" --worker <impl worker> --manager <director session id>` and removes it with `astra_roles.py release <thread id>`. A mesh hold also goes when the worker's roster status is `ended` or `failed` or the run folder is gone; `astra_review.py` holds its own `astra-reviewer` record while its `codex exec` runs. The Lessons section adds: a `MeshWorker` hold cannot outlive its run folder, so a lasting designer or reviewer needs a record tied to its lasting thread store.

**Acceptance gate:** the doc has the eight sections; every lesson and every claim about code names its file; at least 2 review rounds ran and each is saved in `$SESSION_DIR/design/`; the adversarial findings are folded in; the merge gate green. At phase end the unit director sends the showrunner the Questions section as one message for the user, sends the Guilds section to cargo-liner (`showrunners.py address remove-running-section`), and asks the showrunner to pass the doc to hierarchies-unit and roles-unit.

### Phase 3 — The designer and reviewer roles open, resume and outlive a run, under a unit director or a showrunner  · status: todo
**Depends on:** Phase 2

**Pending decision: the user's answers to the Phase 2 questions on the roles.**
- **Actual problem:** a designer or Astra reviewer is started by hand, keeps its thread id in notes, and ends with its run; a designer outside a phase cannot save its work.
- **What exists now:** `codex_mesh.py` threads in run folders; `review_pool.py` threads in `~/.local/state/review-pool/`; `astra_roles.py` records (Phase 1).
- **What should change:** one launcher opens, resumes, pauses and ends a named designer or reviewer for a unit director or a showrunner, keeps its thread outside the run roots, reserves its output file, and records the role for the census.
- **Recommendation:** the design doc's Roles section and its recommended answers.

#### Work Order

**Goal:** a unit director or a showrunner opens a named designer or reviewer once and resumes the same thread across phases, runs and account moves.

**Spec:** filled from the approved Roles and Names sections at Phase 2's phase review, with the final file list. It covers at least: the launcher and its verbs; the thread store and its lock; model and effort from `config/agents.conf`; the reserved output file; `/astra_review` resuming a held reviewer thread when one exists; the shutdown and account-move resume route; the `astra_roles.py` record kept in step.

**Files:**
- `scripts/guilds/roles.py` — new: the launcher (name fixed by the approved Names section)
- `scripts/guilds/test_roles.py` — new
- `scripts/astra/astra_roles.py` — records kept in step with the launcher
- `scripts/astra/astra_review.py` — resumes a held reviewer thread
- `scripts/astra/test_astra_roles.py` — the lasting-thread lifetime
- `scripts/astra/test_astra_review.py` — a held reviewer thread resumed
- `config/agents.conf` — designer and reviewer model and effort
- `pyrightconfig.json` — one `executionEnvironments` entry for `scripts/guilds`

**Workers:** `1 writer + 1 tester` — the code and its tests split cleanly; in the Astra-record work the two owner sets were independent and the test lane was real.
- `impl` — `scripts/guilds/roles.py`, `scripts/astra/astra_roles.py`, `scripts/astra/astra_review.py`, `config/agents.conf`; hub: `pyrightconfig.json` (one entry; another unit may also add one)
- `test` — `scripts/guilds/test_roles.py`, `scripts/astra/test_astra_roles.py`, `scripts/astra/test_astra_review.py`, written from the Spec alone

**Constraints from prior phases:**
- The Astra role record (`scripts/astra/astra_roles.py`) is one file per thread, `~/.local/state/astra/roles/<thread id>.json` (override `ASTRA_ROLES_STATE`), written by temp file and rename. On disk it is `RoleRecord`, exactly the keys `role` (`designer` | `reviewer`), `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, with nulls for the other lifetime. `_parse_record` refuses a missing or extra key, a wrong type, or a `thread_id` that differs from the file name.
- In the library a record is `HeldRole` (`live_roles()` returns `LiveRole`, which adds `state: Literal["busy", "idle"]`) with `lifetime: MeshWorker | ExecProcess`: `MeshWorker(session_dir, worker)` or `ExecProcess(pid)`, both frozen dataclasses. Functions: `hold(role, thread_id="", codex_home="", *, session_dir=None, worker=None, pid=None, manager=None) -> HeldRole` (raises `ValueError`; `manager` defaults to `$CLAUDE_CODE_SESSION_ID`), `release(thread_id) -> None` (a missing record is not an error), `live_roles() -> list[LiveRole]`. CLI: `hold designer|reviewer`, `release <thread id>`, `list [--json]`; exit 2 with one stderr line on a refusal.
- `live_roles()` removes a `MeshWorker` record when its run folder is gone, the roster has no such worker, the roster thread differs, or the status is `ended` or `failed`; it removes an `ExecProcess` record when its pid is gone. It keeps a record whose roster is unreadable and prints one stderr line naming the file. `state` is `busy` for roster status `starting`, `running` or `waiting_capacity`, or a running pid; else `idle`.
- A lasting designer or reviewer thread outlives its run folder, so it cannot use `MeshWorker`, and outlives one process, so it cannot use `ExecProcess`. It needs a third lifetime variant tied to the thread store, with its own on-disk keys added to `RoleRecord` and to `_parse_record`'s key check; the variant names what keeps the hold alive (type design contract).
- `astra_review.py` `run_review()` holds an `ExecProcess` reviewer record for the life of its `codex exec` only when `$CLAUDE_CODE_SESSION_ID` is set, after it reads `session id: <thread>` from `codex.log` (at most `THREAD_HEADER_TIMEOUT` = 30 s), with `codex_home` from `accounts.py env workers`, and releases it in a `finally`. A resumed held reviewer keeps one record per thread id: the file name is the thread id.
- `production_tree.py --json` prints one `AstraEdge` per live record (`manager`, `member` = thread id, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) and drops the Codex worker edge for that thread (`worker_edges(running, astra_threads)`). cargo-handler reads these fields; they stay as they are.
- Scripts import the record module as `from scripts.astra import astra_roles` after `sys.path.insert(0, <repo root>)` (as `astra_review.py` and `production_tree.py` do).
- Tests: never name a test helper `run` (it hides `unittest.TestCase.run`); point `ASTRA_ROLES_STATE` and `CLAUDE_CODE_SESSION_ID` at fixtures. basedpyright exits 3 for the missing `.venv`; only its footer counts.

**Acceptance gate:** the scripts' tests and the merge gate green; `basedpyright` clean on every changed folder; live: a designer opened for this unit, ended run, resumed on the same thread id, and shown once in `production_tree.py --json`.

### Phase 4 — Each guild has members, shared documents and a message group  · status: todo
**Depends on:** Phase 3

**Pending decision: the user's answers to the Phase 2 questions on the guilds.**
- **Actual problem:** designers and reviewers learn alone; nothing shares their tips or reaches them as a group, and cargo-handler has no guild data to show.
- **What exists now:** fixed broadcast groups in `broadcast.py`; standing prompts per review type in `config/review_pool/`; no guild record.
- **What should change:** a guild record of members, a common document and one per guild that reach every member, a guild message group, and a `--json` view for cargo-handler.
- **Recommendation:** the design doc's Guilds section and its recommended answers.

#### Work Order

**Goal:** a message to a guild reaches each member, a change to a guild document reaches each member's standing instructions, and one command prints the guilds and their members as JSON.

**Spec:** filled from the approved Guilds section at Phase 3's phase review. It covers at least: the membership record and its verbs; join on role open and leave on role end (Phase 3); the shared documents and their path; the `broadcast.py` guild group with its `selects`, `ending`, `_group_name` and `_remote_arguments` cases; delivery to a Codex member between turns through its holder; the JSON view and its fields, sent to cargo-liner.

**Files:**
- `scripts/guilds/guilds.py` — new: membership record and `--json` view
- `scripts/guilds/test_guilds.py` — new
- `scripts/production/broadcast.py` — the guild group
- `scripts/production/test_broadcast.py` — the guild group
- `docs/guilds/common.md`, `docs/guilds/design.md`, `docs/guilds/review.md` — new: the shared documents
- `scripts/production/codex_winddown.py` — only if the `broadcast()` signature changes (`docs/as-built/session-roster.md:377`)

**Workers:** `2 writers` — the record and JSON view, and the broadcast group, on disjoint files.
- `impl` — `scripts/guilds/guilds.py`, `scripts/guilds/test_guilds.py`, `docs/guilds/*.md`
- `test` — opens as impl — `scripts/production/broadcast.py`, `scripts/production/test_broadcast.py`, `scripts/production/codex_winddown.py`

**Constraints from prior phases:**
- Phase 3's role launcher calls join and leave.
- A held designer or reviewer already shows in `production_tree.py --json` as an `astra-designer` or `astra-reviewer` edge whose `member` is its Codex thread id, with `manager`, `codex_home` and `state` (`scripts/astra/astra_roles.py` records, read by `live_roles()`). The guild view names each member by that same thread id, so cargo-handler can join the two.
- Tests run as `python3 -m unittest scripts.production.test_<name>` from the worktree root; `scripts/production/test_production_tree.py` shows the import form that works there. Never name a test helper `run` (it hides `unittest.TestCase.run`).

**Acceptance gate:** the tests and the merge gate green; `basedpyright` clean; live: a guild message reaches a held designer through its holder, and the JSON view lists it. At phase end the unit director sends cargo-liner the JSON command and its fields.

### Phase 5 — The command contracts name the roles and the guilds  · status: todo
**Depends on:** Phase 4

**Pending decision: the user's answers to the Phase 2 questions on the command changes.**
- **Actual problem:** `/unit:direct`, `/showrunner:produce`, the production format and the messaging rules do not know the roles or the guilds, so no director or showrunner uses them.
- **What exists now:** the Command changes section of the design doc; the hub files and their owners in the production doc.
- **What should change:** each contract says when to open and resume a designer or reviewer, how to use its guild, and the new words.
- **Recommendation:** the design doc's Command changes section.

#### Work Order

**Goal:** each named contract says when a unit director or showrunner uses a designer, a reviewer and their guild, and the production format defines the new words.

**Spec:** filled from the approved Command changes section at Phase 4's phase review, with the final file list. Each hub-file edit goes through cargo-berth's normal flow and is named in the checkpoint notice as `also touches <path> (owner <unit>), tested against <owner tip>` (`docs/production_format.md` <ProductionUnit/> item 9): `commands/unit/direct.md` (phases-unit), `docs/production_format.md` and `commands/showrunner/produce.md` (roles-unit). `commands/message.md` and `codex/AGENTS-messaging.md` change word for word together.

**Files:**
- `commands/unit/direct.md` — hub (phases-unit)
- `commands/showrunner/produce.md` — hub (roles-unit)
- `docs/production_format.md` — hub (roles-unit): the new words
- `commands/message.md`, `codex/AGENTS-messaging.md` — guild messaging, word for word
- `commands/guild/announce.md` — new, if the approved design keeps a guild command

**Workers:** `2 writers` — the unit contract, and the showrunner contract with the format and messaging text, on disjoint files.
- `impl` — `commands/unit/direct.md`; hub: `commands/unit/direct.md` (owner phases-unit)
- `test` — opens as impl — `commands/showrunner/produce.md`, `docs/production_format.md`, `commands/message.md`, `codex/AGENTS-messaging.md`, `commands/guild/announce.md`; hub: `commands/showrunner/produce.md`, `docs/production_format.md` (owner roles-unit)

**Constraints from prior phases:** the launcher (Phase 3) and the guild commands (Phase 4) as built. The Astra role record CLI is `python3 scripts/astra/astra_roles.py hold designer|reviewer (--session-dir <run> --worker <name> | --thread <id> --codex-home <dir> --pid <pid>) [--manager <id>]`, `release <thread id>` and `list [--json]`; a contract that names it uses these verbs. `/astra_review` holds its own `astra-reviewer` record while its `codex exec` runs; its exit codes and output did not change.

**Acceptance gate:** the merge gate green; each hub file trial-merged against its owner's tip; each changed contract names the launcher and guild commands by their real verbs. At phase end the unit director sends cargo-liner the final role and guild design (production Close-out).
