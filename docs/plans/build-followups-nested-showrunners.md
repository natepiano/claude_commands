# Nested showrunners: a showrunner runs other showrunners, to any depth

> **Status: IMPLEMENTATION PLAN — Phase 1 (the tree) is built, its live attach of tool-based-ui under build-followups waiting on the showrunner's go; Phases 2–6 are todo.**

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12, the user through the showrunner: "Yes start hierarchical on showrunner-fixer".

2026-10-09, the request: "showrunners manage showrunners - ad hoc depth". A plan subagent from natedev wrote the proposal the same day, `~/.local/state/plan-backlog/2026-10-09-nested-showrunners.md`.

2026-10-12, the user at approval: "top level (L1) showrunners should make sure to use tmux to start lower level showrunners / showrunners should still be grouped in the same accounts / we need a "promote" command that turns a unit into a showrunner and closes it and restarts it in the showrunner account (unless for some reason the accounts happen to be running both roles at the time)".

## Proposal

**What the user gets.**

- The root is the user's one place to see everything: its footer and its dailies show every production below it, and every item that needs the user appears there once. The user may still work with any showrunner directly, and one below a root answers them in its own pane as a root does (user, 2026-10-12: "have them available for direct interaction from me but also have a natural home under an L1"). This replaced "only the root talks with the user", which Phase 1 built and the first live check showed to be wrong.
- A showrunner starts a lower showrunner with one command. The lower showrunner runs in its own detached tmux session, as a unit does.
- Every showrunner, at any depth, runs on the showrunner account. Unit directors stay on the unit account.
- A unit whose work has grown into a production of its own is promoted with one command: it stops, and comes back as a showrunner on the showrunner account with its conversation kept.

**Model.** A lower showrunner is a showrunner, never a unit. It has its own production doc, update timer, merge branch and units, and no row in its parent's Units table: about ten readers take a Units row to be a unit director. The tree is one stored fact per child, the file `~/.local/state/showrunner/<child slug>/parent`, holding the parent's slug. Children, the root and names are looked up from the update timers at each call. It is a state file and not a doc line, so either side attaches without committing in the other's checkout. Depth needs nothing more: a child's parent may itself have a parent.

**Routing.** Routing is already flat. Every showrunner is a top-level session with no unit mark, and each relays only to units marked with its own slug, so nesting adds no duplicate delivery.

- A broadcast already reaches every showrunner. A showrunner passes it to its own unit directors and never to a child showrunner.
- The user's words go down one level at a time, as `From the user (via <parent>): …`. A parent never types into a grandchild's pane.
- An item for the user goes from the showrunner that has it to the root, with no forwarding in between, and into that showrunner's own needs-user file.
- A question between productions (a Mac window, an order of promotions) goes to the parent.

**Reporting.** The root's footer reads every descendant's needs-user file. The parent's dailies show each child as one row: units running and held, the latest unit ETA, and `as of HH:MM` when the child's last report is old. `expand <child|all>` shows the child's units as `<child>/<unit>`.

**Merges and shared resources.** Merge branches stay separate, and a parent never merges a child's branch. The parent orders promotions to a shared destination. The lowest showrunner whose subtree holds every contender decides a contested Mac window or build hold.

**Calls made here**, each a one-line change if the user disagrees:

- The promote command is `/showrunner:promote_showrunner`, beside `/showrunner:promote_unit`, which makes a unit.
- The launch command is `/showrunner:add_showrunner`, beside `/showrunner:add_unit`.
- The proposal's three questions take its recommended answers: a phone push comes from the showrunner that owns the item, a child shows as one row until expanded, and a child in the parent's repository never merges into the parent's branch.
- The reporting phases come before launch and promote, so the first lower showrunner started by command reports to the root from its first minute. tool-based-ui attached under build-followups proves them live before then.
- The showrunner account is the main config dir's login, as `accounts.py` reports it today. This plan adds no role to `accounts.py`.

## What happens next

On the showrunner's go, the phases run in order in this worktree. Each ends with the gate, one checkpoint commit and its hash to the showrunner. Two live checks start or stop real sessions and wait on the showrunner (Gates).

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit`); name both in every dispatch.
- **Stack:** Python 3 standard library, checked by basedpyright. zsh launch strings. tmux under `systemd-run --user --scope`.
- **Key files** (as found at planning, 0f548df9, cited by name because line numbers move with every merge):
  - `scripts/production/showrunners.py`: a showrunner is an update timer dir, `~/.local/state/notifier/showrunner-<slug>`. `registered_showrunners()`, `production_of(session_id)`, `current_name(slug)`, `current_address(slug)`, `named(showrunner)`. CLI verbs `ready`, `name`, `address`, `status`, `list`. It holds no parent or child today.
  - `scripts/production/add_unit.py`: `launch_session` runs `systemd-run --user --scope … tmux new-session -d -s <name> -c <cwd> -e SHOWRUNNER_UNIT=<slug> -e SHOWRUNNER_UNIT_ID=<unit> zsh -ic "<account prefix>ENABLE_TOOL_SEARCH=true command claude … <prompt>; exec zsh"`. `launch_environment()` drops every `CLAUDE_*` variable. `tmux_live(tmux, name)`.
  - `scripts/production/unit_lookup.py`: the unit marks `PRODUCTION_MARK` and `UNIT_MARK`, `marked_units(slug)`, `tmux_binary()`.
  - `scripts/accounts/accounts.py`: `unit` is the only role. `role_account("unit")` gives `DefaultAccount` or `NamedAccount(label, dir)`; `account_prefix(selected)`; `main_config_dir()`. Its status table names the main config dir's account as the one showrunners run on.
  - `scripts/production/update_registration.py`: `register`, `outstanding_path`, `footer`. `scripts/production/dailies_render.py`: `read_outstanding`, `footer_main`. `scripts/production/dailies_input.py`: `run`, `units_from_doc`.
  - `scripts/production/production_lifecycle.py`: `open_production`, `promote_main`, `wrap`.
  - `scripts/production/tmux_names.py`: `fault(kind, old, new, settings)` tells showrunners of a machine-wide fault.
  - `scripts/shutdown/restart.py`: resumes a showrunner with the prompt `/showrunner:produce <doc> resume`, and a unit through `add_unit.py --resume`.
  - `commands/showrunner/promote_unit.md`: how a running session is stopped and resumed today: `/exit` typed into its pane, a wait until its process is gone, then the tmux session ended.
  - `scripts/production/fake_tmux.py`, `fake_showrunner.py`: what the tests use in place of tmux and a showrunner.
- **Files other units own.** Read and import only; a needed change is asked of the owner, never made here:
  - `scripts/production/add_unit.py`: the shutdown unit and two-accounts are editing it.
  - `scripts/accounts/accounts.py`: two-accounts.
  - `scripts/shutdown/`: the shutdown unit. `scripts/message/`: the shutdown session.
  - `scripts/build_hold/build_hold.py`: builder-report.
  - `docs/production_format.md` and `commands/unit/*.md`: model-study. Phase 1 adds a short passage to `production_format.md`; tell model-study the words and where they go.
- **Tests** never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, never push and never send a real message. Each state path a new function reads can be pointed at a temp dir by an environment variable or a parameter, as `NOTIFIER_STATE_DIR` is.
- **Style:** `@final` test classes, `@override setUp`, modules patched directly, tagged result types in place of booleans and `None` for outcomes.

## Gates

- **Every phase:** `basedpyright scripts/agents scripts/production scripts/build_hold scripts/message scripts/hooks scripts/whoami scripts/delegate` ends `0 errors, 0 warnings, 0 notes`, and the eight suites pass: production, message, build_hold, hooks, agents, delegate, whoami, mac_test. A phase that touches `scripts/shutdown` adds that suite.
- **G1, before Phase 4's live check:** the showrunner names a scratch production to start as a child, because the check starts a real session.
- **G2, before Phase 5's live check:** the showrunner picks the unit to promote, because the check stops a running unit.

## Phases

### Phase 1 — The tree: attach, detach, and who reports to whom  · status: done · live attach waits on the showrunner's go

#### As-built

- `scripts/production/showrunners.py`: `SHOWRUNNER_STATE_DIR` (env of that name, else `~/.local/state/showrunner`), `PARENT_FILE`. `Timer` and `_timer(instance)` hold what makes a timer a complete showrunner's, with no process started; `_registered` builds on it. `timer_slugs()`, `edges(slugs=None) -> dict[child, parent]`, `parent_of`, `children_of`, `root_of`, `highest_running(runners)`, `tree_lines(runners)`, `own_slug()`, `attach(child, parent)`, `detach(child) -> parent`, `tell(slug, key, text)`, `tell_others(texts, key)`. CLI verbs `parent|children|root <slug>`, `tree`, `attach <child> [<parent>]`, `detach <child>`.
- The lookups are named `parent_of`, `children_of` and `root_of`, not the `parent`, `children` and `root` this Work Order wrote, so a caller's own `parent` variable does not hide the function.
- A cycle written by hand counts for none of its members. That is stronger than the Work Order's "stops at a slug it has already seen": `root_of` needs no guard, and each member is a root in every reader.
- `tmux_names.fault` tells `showrunners.highest_running(...)`, not strictly roots: below a root that is down, the nearest running showrunner hears, so a fault is not lost while a root is stopped.
- `commands/showrunner/attach.md`; `produce.md` → Throughout → Nested showrunners, and three rows in the Direct table; `docs/production_format.md` → Words → Parent, child and root showrunner; `docs/as-built/showrunner-automation.md` → Nested showrunners, which each later phase extends.

**Binds later work:** `edges()` is the one reader of the tree; Phases 2 and 3 walk it for descendants. `attach(child, parent)` raises `ValueError` with the refusal, which Phase 4 calls after its launch. `own_slug()` is how a script learns which production its session runs. Tests point the tree at a temp dir with `SHOWRUNNER_STATE_DIR` and catch messages with `SHOWRUNNERS_SEND`.

**Gotchas:** `registered_showrunners()` starts one process per showrunner to find its socket; `timer_slugs()` and `edges()` start none, so call those where only the tree is needed. `detach` removes a parent file even when its edge no longer counts.

#### Work Order

**Goal:** a showrunner can be attached under another, and every script can ask for a showrunner's parent, children and root.

**Spec:**

1. `showrunners.py` gains `parent(slug)`, `children(slug)`, `root(slug)` and `tree()`. The state dir is `SHOWRUNNER_STATE_DIR`, else `~/.local/state/showrunner`. An edge whose parent or child has no update timer is ignored, so a wrapped production leaves no broken tree. `root` walks up and stops at a slug it has already seen.
2. `showrunners.py attach <child> [<parent>]` writes the edge; with no parent it uses the calling session's own production. It refuses, naming the cause: a child or parent with no update timer, a child that is its own parent, an edge that would close a cycle, and a child already attached (`<child> already reports to <parent>; detach it first`). `detach <child>` removes the edge and refuses a child that has none.
3. `showrunners.py parent|children|root <slug>` and `tree` print for scripts and for the user: `tree` prints one line per showrunner, indented by depth, with its session name or `not running`.
4. Attach and detach tell both sessions in one line each, through `message/send.py`, and say so when one of them is not running.
5. `commands/showrunner/attach.md`: `/showrunner:attach <child> [parent]` and `/showrunner:attach detach <child>`.
6. `commands/showrunner/produce.md`, Throughout: the four routing rules in Proposal → Routing, and "a child showrunner is never a unit: it has no Units row".
7. `tmux_names.fault` sends a machine-wide fault to roots only.
8. `docs/production_format.md`: one short passage saying a production may report to a parent, that the edge is state and not a doc line, and that a child is not a unit.

**Files:** `scripts/production/showrunners.py`, `test_showrunners.py`; `scripts/production/tmux_names.py` and its tests; `commands/showrunner/attach.md`; `commands/showrunner/produce.md`; `docs/production_format.md`.

**Acceptance gate:** the gate. Tests cover attach, each refusal, an edge to a production with no timer, the root walk at three levels and a cycle written by hand into the state dir. Live, with the showrunner's go: attach tool-based-ui under build-followups, read `showrunners.py tree`, and hana sends one test item to the root.

### Phase 2 — The root's count covers every item that needs the user  · status: done · live check waits on tool-based-ui being attached

#### As-built

- `scripts/production/showrunners.py`: `Place(root, below)` and `place_of(slug)`, one reading of the tree; `descendants_of(slug)`; `user_facing(slug)`, the productions whose items a showrunner counts and lists.
- `scripts/production/dailies_render.py --footer`: `--below <list>` once per lower production and `--reports-to <root>`. `footer_main(..., below=, root=)` and `footer(..., unreadable=, root=)` default to nothing, so a root with nothing attached renders the same bytes.
- `scripts/hooks/showrunner_footer.py`: `place_of`, `render_footer(..., place, ...)`, `open_questions(productions, below)`. `block_reason` reads each production's place once.
- `scripts/production/update_registration.py`: `footer` passes the lower lists and the root; new verb `open --production <doc>` (`open_items`), which prints the list a showrunner shows when the user asks what the open questions are.
- `pyrightconfig.json`: `scripts/hooks` can now see `scripts/production`.
- Spec 2 was built as the bullet `reports to <root's production slug>`, not the root's session name: the slug is what `showrunners.py address` takes, and it still reads right while the root is restarting or down. A showrunner under a root keeps its own count of its own items.
- Spec 4, and one thing the Work Order did not say: while a lower list cannot be read the root's footer never says `nothing needed`, because that list may hold an item. A due item in a lower list also drops `nothing needed` from the root's footer.
- Not built here: the dailies report still reads only its own list. Phase 3 owns the report.

**Binds later work:** Phase 3 reads the same lists for each child row's needs-user mark; `place_of(slug).below` is the list of productions to read, and `UNREADABLE_LIST` in `dailies_render.py` is the one wording for a list that cannot be read.

**Gotchas:** `scripts/production` imports the hook module at import time, so the hook imports `showrunners` late, inside `place_of`. A test that calls `showrunner_footer.open_questions` in process passes `below` itself and so never reads the live tree. The hook reads needs-user lists under `$HOME`, and the tree under `SHOWRUNNER_STATE_DIR`, so a hook test sets both.

#### Work Order

**Goal:** the user reads one count, at the root, and misses nothing a lower showrunner needs from them.

**Constraints from prior phases:** `root`, `children` and the edge file from Phase 1. The open questions rule (user, 2026-10-12, built as a follow-up between Phases 1 and 2): a reply shows what waits on the user only as `There are {n} open questions for you.`, the Stop hook checks n against the needs-user file, and the full list is given only on request. This phase was first written to print each descendant's items in the root's footer; the rule replaces that.

**Spec:**

1. Each needs-user file holds only its own showrunner's items. The root's count covers its own due items and every descendant's, and the Stop hook checks it against all of those files. No item's text is printed in a reply.
1a. On request the root lists every item in full, oldest first, each led by its owner's slug. One command prints that list (`update_registration.py open --production <doc>`), so the root reads no other showrunner's file by hand.
2. A showrunner that is not a root keeps its own footer and adds one line, `reports to <root's session name>`.
3. The user answers at the root. The root relays the answer down, and the showrunner that owns the item clears it from its own file.
4. A descendant whose file cannot be read is named in the root's footer and in the list (`<slug>: needs-user list unreadable`), never skipped in silence. The hook skips the count check while a file is unreadable.
5. `produce.md`: an item for the user goes to the root and into the owner's file, and the phone push comes from the owner with its usual key.

**Files:** `scripts/hooks/showrunner_footer.py` (`open_questions`), `scripts/production/update_registration.py`, `dailies_render.py` (`read_outstanding`, `footer_main`) and their tests; `commands/showrunner/produce.md`; `commands/showrunner/footer.md`.

**Acceptance gate:** the gate. Tests use two-level and three-level trees, an unreadable file and a root with no children, whose footer and count must not change by a character. Live: with tool-based-ui attached, an item hana records raises natedev's count by one and appears in its list under `tool-based-ui`, and both drop when hana clears it.

### Phase 3 — The parent's dailies show each child as one row  · status: done · live check waits on promotion and on tool-based-ui's next dailies

#### Work Order

**Goal:** the parent's report says how each production below it is doing without the user opening a second session.

**Constraints from prior phases:** `children` from Phase 1. Phase 2 owns needs-user; this phase reads the same files for the row's needs-user mark and prints no item twice.

**Spec:**

1. A showrunner's `dailies_input.py` run also writes `~/.local/state/showrunner/<slug>/dailies_last.json`: units running and held, the latest unit ETA, the earliest start, and the time it was written.
2. The parent shows each child as one row from that file: the counts, the ETA, a timeline bar from the earliest start to the latest ETA, and `as of HH:MM` once the file is older than two of the child's update intervals. A child with no file yet reads `no report yet`.
3. A child's own children are already in its file's counts, so depth is shown without the parent reading grandchildren.
4. `/showrunner:dailies simple expand <child|all>` prints the child's units as `<child>/<unit>` rows from the same file.

**Files:** `scripts/production/dailies_input.py`, `dailies_render.py` and their tests; `commands/showrunner/dailies.md`.

**Acceptance gate:** the gate. Tests cover a current child, a stale child, a child with no file, `expand` and a three-level tree. A production with no children renders exactly as before. Live: natedev's dailies show tool-based-ui as one row.

#### As-built

- `scripts/production/dailies_render.py`: `Child`, `ChildReport`, `NoChildReport`; `Report.children` and `Report.reports_to`, from the input keys `children` and `reports_to`; `Row.note`; `child_row`, `unit_rows`, `last_report`, `parse_child_report`, `due`, `zone_named`. The label column is as wide as the longest row name, so a report with no child keeps its nine columns.
- `scripts/production/dailies_input.py`: `--expand <child|all>`, `child_fields`, `save_last_report`, `last_report_path`. It reads the tree through `showrunners.py` and the lists through `update_registration.outstanding_file`.
- `commands/showrunner/dailies.md`: usage, the builder flag, the two input keys, and what the renderer writes for a production below.
- Spec 1: the file also carries `zone` and `interval`, which the parent needs to move times into its own zone and to tell an old report from a current one. "Held" counts units whose checkpoint is not merged, the meaning `held` already has in a report.
- Spec 2: the row sits in the chart, after the unit rows. A child that reports on demand always shows `as of HH:MM`, since nothing refreshes its file on a schedule. A child with no file reads `?  no report yet`: the `?` is the chart's mark for no ETA.
- Spec 2, the needs-user mark: `<n> open questions`, the count of due items in the child's list and the lists below it. The items are printed nowhere in the report.
- Spec 4: `expand` is taken with any length, not only `simple`. An expanded child shows its units and one row for each production below it.
- What Phase 2 left: the full report's footer now covers the lists below (`nothing needed`, the unreadable bullet) and names the root.
- Not built: a build-hold mark on a child's row. A hold is machine-wide and already in the parent's footer, and a mark from an old file could outlive the hold.

**Binds later work:** `dailies_last.json` is what a parent knows of a child between messages; Phase 5's stop and Phase 6's failure paths can read `written` and `interval` from it to tell a child that has gone quiet.

**Gotchas:** the file is local, like the tree, so both showrunners must be on one machine. A child's row reads `no report yet` until that child's builder has run once with this code.

### Phase 4 — A showrunner starts a lower showrunner in tmux, on the showrunner account  · status: done · live check is the showrunner's to run after promotion

#### Work Order

**Goal:** one command starts a child production's showrunner in a detached tmux session, on the showrunner account, already attached to its parent. User, 2026-10-12: "top level (L1) showrunners should make sure to use tmux to start lower level showrunners / showrunners should still be grouped in the same accounts".

**Constraints from prior phases:** `attach` from Phase 1. `add_unit.py` is read and imported, never edited: this phase has its own launcher and takes `launch_environment` and `tmux_live` from it.

**Spec:**

1. `scripts/production/add_showrunner.py --production <child doc> --model <id> --effort <level> [--resume <session id> --cwd <dir>] [--check]`, run by the parent. It starts `claude` in a detached tmux session under `systemd-run --user --scope`, named for the child's slug, in the child's checkout, with the prompt `/showrunner:produce <child doc>`, or `/showrunner:produce <child doc> resume` with `--resume`.
2. The session carries no unit mark. A session with one would be relayed to as a unit of the parent.
3. The account is the showrunner account at any depth: the launch drops every `CLAUDE_*` variable and adds no account prefix, so the session takes the main config dir's login whatever account the caller's units use.
4. It refuses, naming the cause, before starting anything: a doc that is not a production doc; a child that already has a running showrunner; a tmux session of that name already live; a child that is the caller's own production or one of its ancestors; no tmux.
5. After the launch it waits for the child's update timer to appear, then attaches the child to the caller's production. If no timer appears in time, it says so and leaves the session running for the caller to look at.
6. `--check` runs every refusal and starts nothing.
7. `commands/showrunner/add_showrunner.md`: `/showrunner:add_showrunner <production doc>`. `produce.md`: a showrunner starts a lower showrunner only this way, never in its own pane.
8. `--model <id>` and `--effort <level>` are required, and the command tells the parent to pass its own. No agent setting is added for showrunners: `production.director` is the only launch setting today, and a second one waits for a reason to differ from the parent.

**Files:** `scripts/production/add_showrunner.py`, `test_add_showrunner.py`; `commands/showrunner/add_showrunner.md`; `commands/showrunner/produce.md`.

**Acceptance gate:** the gate. Tests run against `fake_tmux.py`: the tmux command carries no unit mark and no account prefix, the environment holds no `CLAUDE_*`, each refusal starts nothing, and attach follows the timer. Live under G1: natedev starts the scratch production's showrunner; `showrunners.py tree` shows it under build-followups; `/showrunner:accounts` shows it on the showrunner account; the scratch production is wrapped afterwards.

#### As-built

Built as specified; the code-level account is `docs/as-built/showrunner-automation.md` → Nested showrunners → "A parent starts a child showrunner in tmux (phase 4)". Calls made while building:

- **A running showrunner is an update timer whose session is live**, not a timer alone. A timer outlives a session that ended, and that ended session is what `--resume` restarts; counting the timer would have refused every resume. The wait after the launch is for the same thing.
- **A resumed child that already reports to the caller is left as it is.** Its parent file is still stored from before, so the command prints `<child> already reports to <caller>` and sends nothing. A child that reports to another showrunner gets `attach`'s own refusal and exit 1, with its session left running.
- **Two refusals beyond the five in the spec:** the caller runs no production, and the session records cannot be read (the command cannot tell whether the child runs).
- **The ancestor check reads the tree downward** from the child: `caller in descendants_of(child)`.
- **The wait is 180 s**, changed with `ADD_SHOWRUNNER_WAIT_S`. On a timeout the command names the two commands to finish by hand.
- **Tests use their own recording stand-ins**, not `fake_tmux.py`, which answers neither `has-session` nor `new-session`. Nothing real starts and nothing real is sent.
- **G1** was settled by the showrunner as its own call (a scratch showrunner started and wrapped can be undone); it runs the live check after promotion.

**Binds later work:** Phase 5's stop finds a lower showrunner's tmux session by its slug, the name this launcher gives it. The session has no unit mark, so `unit_lookup` never lists it.

**Gotchas:** the tmux session's name is the production's slug, so a unit whose session has that same name blocks the start (`a tmux session named <slug> is already live`). The launcher needs the parent's own `CLAUDE_CODE_SESSION_ID` to know which production it runs: run from a plain shell it refuses with "this session runs no production".

### Phase 5 — Promote a unit to a showrunner  · status: done · live check is the showrunner's under G2, on a unit whose run is done

#### Work Order

**Goal:** `/showrunner:promote_showrunner <unit> <production doc>` turns a running unit director into the showrunner of its own production, under the showrunner that promoted it. User, 2026-10-12: "we need a "promote" command that turns a unit into a showrunner and closes it and restarts it in the showrunner account (unless for some reason the accounts happen to be running both roles at the time)".

**Constraints from prior phases:** `add_showrunner.py --resume` from Phase 4 does the relaunch, so the promoted session lands on the showrunner account by the same rule as any showrunner.

**Spec:**

1. The unit writes its production doc first, in the production format, and commits it. The command takes that doc and writes none.
2. **Check** (`add_showrunner.py --resume … --check` plus the checks below). It refuses, naming the cause: no live unit of that name under this production; a unit mid-turn or with a form waiting; a unit with a run open, seats live or work uncommitted, which finishes or checkpoints first; a doc that is absent, uncommitted or not a production doc; a child slug that already has a showrunner.
3. **Stop** the unit as `promote_unit` stops a session: `/exit` into its pane, wait until its process is gone, end its tmux session. Two processes on one session corrupt it, so nothing is relaunched before the process is gone.
4. **Restart** it with `add_showrunner.py --production <doc> --resume <session id> --cwd <its worktree>`. Its conversation is kept; it carries no unit mark; it runs on the showrunner account.
5. **The account.** When the unit role and the showrunners share one account at that time, the restart is the same and no account changes. The command's one-line result says which happened: `moved from <unit account> to the showrunner account`, or `same account, no move`.
6. **The parent's records.** The unit's Units row is retired with the words `promoted to showrunner of <child slug>`, in the way `add_unit.py` already reads a retired row, and committed in the parent's checkout. The unit's branch stays as it is: checkpoints already merged stay merged, and the child's merge branch is the one its own doc names.
7. If the restart fails after the stop, the command prints the one line that resumes the session as the unit it was, and leaves the Units row untouched.
8. `commands/showrunner/promote_showrunner.md`; a row for it in `produce.md`; `promote_unit.md` gains one line pointing to it for the other direction.

**Files:** `scripts/production/promote_showrunner.py`, `test_promote_showrunner.py`; `commands/showrunner/promote_showrunner.md`; `commands/showrunner/produce.md`; `commands/showrunner/promote_unit.md`.

**Acceptance gate:** the gate. Tests cover each refusal, the stop before the restart, both account cases, the retired row and the failed restart. Live under G2: the unit the showrunner picks is promoted; it answers a question about its earlier conversation; `tree` shows it under build-followups; its old Units row reads retired; the parent's units table no longer lists it.

### Phase 6 — Wrap, arbitration, restart, and the as-built  · status: todo

#### Work Order

**Goal:** the tree stays right when a production ends or the machine restarts, and the rules for shared resources are written where showrunners read them.

**Constraints from prior phases:** all five.

**Spec:**

1. `production_lifecycle.py wrap` removes the wrapped production's edge to its parent and every edge that names it as parent. It tells each orphaned child in one line that it is now a root, and tells the parent that the child wrapped.
2. A wrap is refused while the production has a running child, naming the children: wrap or detach them first.
3. `produce.md`: the lowest showrunner whose subtree holds every contender decides a contested Mac window or build hold; the parent orders promotions to a shared destination; disk room and quota decisions stay with the root.
4. Restart: read `scripts/shutdown/restart.py` and check, with a scratch child, that a shutdown and restart brings a child showrunner back in tmux, on the showrunner account, still attached. The edge is keyed by slug, so no change is expected. If one is needed it is asked of the shutdown unit, with the failing case.
5. `docs/as-built/showrunner-automation.md` gains a "Nested showrunners" section: the model, the edge file, each command, each refusal, and the calls listed in Proposal.

**Files:** `scripts/production/production_lifecycle.py`, `test_production_lifecycle.py`; `commands/showrunner/produce.md`; `docs/as-built/showrunner-automation.md`.

**Acceptance gate:** the gate. Tests cover a wrap that detaches, a wrap refused for a running child and a wrap with no edges, which must behave as today. Live: the restart check in item 4.

## Risks

- A child's row goes stale while its session is down. The row says `as of HH:MM`, and the root's footer still reads the child's needs-user file directly.
- The tree is one machine. A showrunner on the Mac cannot attach to one on natedev until the edge and the reports can be read across machines; this plan does not build that.
- A user who answers a child directly loses nothing: the files are the record, and the root reads them again at its next footer.
- `add_unit.py` is changing under two other units. This plan imports two small functions from it; a rename there breaks the import at the type check, not at run time.
