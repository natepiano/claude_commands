# Production — sdlc

> **Status: PRODUCTION — planned.** Our software development life cycle process: how we prepare design and documentation, how the design and documentation process runs, how showrunners, unit directors and workers share the work to implement it, and the names of things (the user, via the showrunner, 2026-10-20).

## Production Context

- **Source plans:** none yet. The user set up this production on 2026-10-20, via the showrunner, in their own words; /producer:to_production did not split it. Each unit writes its first plan, and its row below then names it.
- **Repository:** `/home/natepiano/.claude`
- **Merge branch:** `sdlc` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-build-followups-sdlc` — the sdlc session's worktree, where `/showrunner:promote_showrunner` resumes it; on `sdlc` from launch
- **Showrunner session:** sdlc
- **Log:** `docs/plans/sdlc-log.md` — git-excluded; one line per event
- **Updates:** on demand (user, 2026-10-13: no timer unless asked); each update reports every unit in full
- **Merge tests:** `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` and `basedpyright scripts/direct scripts/production` (pass is its `0 errors, 0 warnings` line), run in the showrunner checkout
- **Push:** git
- **Promote:** ~/.claude, mac ~/.claude
- **Capacity:** natedev has 32 cores and 60 GB of memory; four units. The work is mostly documents and Python scripts, so the load is low. Its seats share natedev's build slots with build-followups.
- **Change review:** `~/rust/hanadocs/change-review/change-review - sdlc.md` — sdlc is a top-level showrunner, so the list is its own; the file is made on its first row
- **Review pool:** code
- **Proposals:** `~/rust/hanadocs/issues`

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| guilds-unit | `docs/plans/sdlc-guilds.md` (to be written) | `/home/natepiano/worktrees/claude-sdlc-guilds` | `sdlc-guilds` | — | `docs/plans/sdlc-guilds.md`; more when the plan is written |
| phases-unit | `docs/plans/sdlc-phases.md` (to be written) | `/home/natepiano/worktrees/claude-sdlc-phases` | `sdlc-phases` | — | `docs/plans/sdlc-phases.md`; more when the plan is written |
| fix-rounds-unit | `docs/plans/sdlc-fix-rounds.md` (to be written) | `/home/natepiano/worktrees/claude-sdlc-fix-rounds` | `sdlc-fix-rounds` | — | `docs/plans/sdlc-fix-rounds.md`; more when the plan is written |
| roles-unit | `docs/plans/sdlc-roles.md` (to be written) | `/home/natepiano/worktrees/claude-sdlc-roles` | `sdlc-roles` | — | `docs/plans/sdlc-roles.md`; more when the plan is written |

Scope of each unit:

- **guilds-unit** — a persistent, resumable designer role and a persistent, resumable reviewer role. Each can work under a unit director and under a showrunner ("it can and probably should be both"). It also designs a designer guild and a reviewer guild that share tips, tricks and common documentation: designers focus on design skill, reviewers on review skill. The guilds use our messaging system. Every role is a named role. The unit also writes the changes to `/unit:direct` and the showrunner commands.
- **phases-unit** — the phase process: how `/unit:direct` phases run from start to end (Work Orders, reviews, repair rounds, checkpoints, shrink, as-built). Also how design and documentation docs are made before a plan exists.
- **fix-rounds-unit** — finds the phases with the most repair rounds and studies the highest counts first to measure the effect. Then it proposes changes that limit repair rounds and still meet the coding goals ("so we know how to stop ourselves and still get the work done").
- **roles-unit** — the responsibilities of showrunners, unit directors and workers, and the names of things. It works with hierarchies-unit (build-followups) and does not do the same work.

### Guild heads

The user, via the showrunner, 2026-10-20: "I like goddesses of wisdom and judgement for the chief designers and reviewers. Probably each is a top level showrunner."

- The chief designer (head of the designer guild) and the chief reviewer (head of the reviewer guild) are each a top-level showrunner. They are peers of the sdlc showrunner and of natedev, in a flat organization. They are not units of sdlc.
- Their names come from goddesses: a goddess of wisdom for the chief designer, a goddess of judgement for the chief reviewer. cargo-liner settles the names with the user. Until then, the internal ids are `astra-designer` and `astra-reviewer`.
- guilds-unit designs the guilds and the roles. It does not run the two heads. When the heads exist, they run their guilds from guilds-unit's design, and they send their findings about the design back to guilds-unit through the sdlc showrunner.

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |
| `commands/unit/direct.md` | phases-unit | guilds-unit (the designer and reviewer roles); fix-rounds-unit (the repair round rules) |
| `docs/production_format.md` | roles-unit | guilds-unit (role names); fix-rounds-unit (<ProductionUnit/> item 7) |
| `commands/showrunner/produce.md` | roles-unit | guilds-unit (the designer and reviewer roles under a showrunner) |

These three files are also hub files in build-followups. An edit to one of them goes through cargo-berth's normal flow, and the checkpoint notice names the build-followups owner.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |

No gates yet. The plans are not written. When a plan needs another unit's checkpoint, the gate goes here.

## Close-out

- Send cargo-liner the final guild design and the role names the user chose, so its handler unit shows them, and the internal ids `astra-designer` and `astra-reviewer` are replaced everywhere.
- Each unit's as-built doc is in `docs/as-built/`, and the docs it replaces are deleted or amended.
- Tell the natedev showrunner about each change to a build-followups hub file once it is on main.

## Production rules

- **A flat organization** (the user, via the showrunner, 2026-10-20: "I want a flat organization"). sdlc is a top-level production, a peer of build-followups, with no showrunner above it. `showrunners.py parent sdlc` prints no parent. If a promotion attached it to another showrunner, take it out with `/showrunner:attach`.
- **Process redesigns go to the user as questions** (the user, via the showrunner, 2026-10-20: "those should come back to me as questions"). Promotions, unit renames, changes to responsibilities and changes to the process are the user's decisions. The showrunner and the units do not make them alone. A unit sends the question, the answers it can build and the one it recommends to the showrunner, and the showrunner gives it to the user.
- **cargo-liner gets the guild design on every change** (the user, via the showrunner, 2026-10-20). Each time the guild design changes, guilds-unit sends it to cargo-liner, the showrunner of remove-running-section, so that its handler unit shows the design in simple text. cargo-liner owns the `production_tree.py --json` edges for the Astra designer and reviewer records and the `statusline.conf` keys. cargo-liner picks the role names with the user.
- **Coordinate with three units in other productions** (the user, via the showrunner, 2026-10-20):
  - hierarchies-unit (build-followups) designs the best hierarchy for fast delivery within the user's constraints. roles-unit and guilds-unit send it their designs and read its plan before they write theirs. They do not do the same work.
  - fps/frame-time-unit (hana) runs the first Astra designer by hand. guilds-unit reads what it learns before it writes the designer role.
  - experiments-unit (cargo-liner) starts with a hand-resumed Astra thread and `/astra_review`. guilds-unit reads what it learns before it writes the reviewer role.
- **No CI on this repo** (natedev, 2026-10-04, for `~/.claude`). `~/.claude` has no workflows, so the Merge tests replace `verify.sh test`, and <CIPoint/>, the smoke launch, the Mac run and `validate_and_push.sh` do not apply.
- **Each merged phase goes to main at once** (natedev, 2026-10-04, for `~/.claude`), because `~/.claude` main is the live configuration. After a merge's tests pass: merge `main` into `sdlc` if main moved, push `sdlc`, fast-forward `~/.claude` main to it (`git -C ~/.claude merge --ff-only <sha>`; other sessions' uncommitted files there stay untouched), and push main only when `origin/main..main` held no other session's commits before the fast-forward. Then the Mac pulls: `ssh mac 'cd ~/.claude && git pull --ff-only natedev:/home/natepiano/.claude main'`.
