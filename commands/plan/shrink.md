---
description: Shrink the completed phases of an in-flight plan doc — rewrite each `done` phase's Work Order, Retrospective, and Review blocks into a short as-built record of what actually shipped, dropping decision debate and process narration. The live (`todo`) zone and Delegation Context are never touched.
---

# Plan Shrink

**Purpose:** A phased plan implemented over many days grows an archive zone that
outweighs the work left to do. Every `done` phase carries the Work Order it was
dispatched with (written *before* the code existed), a Retrospective, and a
`Phase N Review` block recording an architect pass. That material is read again
on every `/plan:delegate` and `/plan:phase_review` run, and carried through every
context compaction, long after it stops informing anything.

This command rewrites each `done` phase into an **as-built record**: what the
phase shipped, stated in the present tense, corrected to what actually landed.
Deliberation goes ("this was decided this way, that was decided that way"),
process narration goes, prospective phrasing goes. Facts survive.

**Usage:** `/plan:shrink [plan-doc-path] [--phases <ids>]`

**Argument:** the in-flight plan doc. **Omitting it is the normal case** — with no
argument this command shrinks the plan the current `/plan:delegate` run is
executing, resolved from the run record rather than from memory. See
`<ResolvePlan/>`.

**`--phases <ids>`** (optional) comma-separated phase identifiers (`1,4b,7`) to
limit the run to those phases. Without it, every `done` phase is shrunk.

**When to run it:** whenever the archive zone dominates the doc — a rough trigger
is the doc past ~1,500 lines or `done` phases past half its length. Natural
cadence is every few completed phases. It is safe to run mid-project; it is also
worth running immediately before `/plan:to_as_built`, which gets its input
pre-digested.

This command does not change code and does not commit.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 0:** Execute <ResolvePlan/>
**STEP 1:** Execute <Locate/>
**STEP 2:** Execute <IndexLiveDependencies/>
**STEP 3:** Execute <Shrink/>
**STEP 4:** Execute <Splice/>
**STEP 5:** Execute <Verify/>
**STEP 6:** Execute <Report/>

<ArchiveOnly/> is an invariant, not a step: it binds every step.
</ExecutionSteps>

---

<ArchiveOnly>
**Shrinking touches `done` phases and nothing else.** Not the title, not the
status line, not the `As-built disposition` line, not `## Delegation Context`
(including `Project started`, which `/plan:delegate` treats as authoritative),
not `## Gates` or any other doc-level section, and above all not a single byte of
any `todo` phase. The live zone is the dispatch contract `/plan:delegate` reads;
this command has no opinion about it.

**Never renumber, merge, split, delete, or reorder a phase.** Identifiers are
load-bearing: a `done` phase's number is baked into its checkpoint commit message
and referenced by later phases' **Constraints from prior phases**. Phases keep
their position in the doc. Identifiers appearing with letter suffixes (`4b`,
`11a`) in older plans are copied as found — this command is not the place to fix
numbering.

**The phase heading line is copied verbatim** — identifier, title, and
`· status: done (<commit>)` — because the title is the doc's anchor and the
commit is its only link to `git log`.
</ArchiveOnly>

---

<ResolvePlan>
This command exists for long-running plans, so the context that would "remember"
which plan is in flight is exactly the context most likely to have been compacted
away. Do not rely on it. `/plan:delegate` records the plan doc durably at run
start; read that record.

**Resolution order — stop at the first that produces a path:**

1. **The argument**, when one is given.

2. **The delegate run active in this session.** `/plan:delegate` writes
   `/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}` containing the run's
   session directory, whose basename is the run id:

   The marker is keyed by Claude session id, so a hit means *this* session's run —
   the currently executing plan, unambiguously.

3. **The most recent run in this working directory**, when no marker exists
   (the run already ended, or this is a fresh session in the same repo). State
   the run's timestamp when falling back to this, so a stale match is visible:
   `No active run — using the plan from the <timestamp> run in this directory:
   <path>.`

   Arms 2 and 3 are one lookup. `Write` it to `${TMPDIR}/resolve_plan.py` and run
   it; it prints `<abs plan path>\t<run timestamp>\t<working dir>`, or
   `no run record`:

   ```python
   import json, os, pathlib
   sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
   marker = pathlib.Path(f"/tmp/claude/delegate/active/{sid}")
   runs = pathlib.Path.home() / ".local/state/plan-delegate/runs"
   active = runs / f"{pathlib.Path(marker.read_text().strip()).name}.jsonl" \
       if sid and marker.is_file() else None

   def started(rec):
       for line in rec.read_text().splitlines():
           e = json.loads(line)
           if e.get("event_type") == "run_started":
               return e

   cands = ([active] if active and active.is_file() else []) + \
       sorted(runs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
   for i, rec in enumerate(cands):
       e = started(rec)
       if not e or not e.get("plan_doc"):
           continue                                   # adhoc run, no plan doc
       wd, doc = e.get("working_dir", ""), e["plan_doc"]
       if (active is not None and i == 0) or wd == os.getcwd():
           print(("" if doc.startswith("/") else wd + "/") + doc,
                 e["timestamp"], wd, sep="\t")
           break
   else:
       print("no run record")
   ```

   The `(active is not None and i == 0)` guard is load-bearing: the marker's run
   is authoritative regardless of directory, but without a marker only a run whose
   `working_dir` **is** the cwd may match. Dropping the guard makes the newest run
   on the machine win from any directory.

4. **A single plan doc in conversation**, if the durable lookup found nothing.

5. **Ask.** Never guess, and never shrink a doc the user did not name or that
   the run record did not produce.

**Sanity-check whatever came back** before continuing: the path exists and its
head carries a `## Delegation Context` section. A plan doc that fails this is not
delegate-ready and this command does not apply to it — say so and stop.

**Running mid-run is fine** — shrinking only touches `done` phases, and the
phase `/plan:delegate` is working is `todo`. Two consequences to state in one line
when a run is active: any copy of the doc already in the orchestrator's context is
now stale (a later `Edit` against pre-shrink text will fail loudly, not
silently), and the next checkpoint commit will carry the shrink along with its
phase.
</ResolvePlan>

---

<Locate>
Execute `<ResolvePlan/>` to get the plan doc path.

Read `~/.claude/docs/delegate_plan_format.md` — the shared contract this command
rewrites the archive half of.

**Do not read the plan doc.** Reading it is the cost this command exists to
remove; pulling 3,000 lines into the orchestrator to save the user 3,000 lines is
self-defeating. Map its structure instead:

```
wc -l <plan>
grep -nE '^#{2,4} ' <plan>
```

From the heading map alone, classify:

- **Phase headings** — lines matching `^#{3,4}\s+Phase\b.*·\s*status:\s*(done|todo)`.
  The `· status:` marker is what distinguishes a phase heading from a
  `### Phase N Review` heading, which sits at the same level. Match on it, never
  on the `Phase` prefix alone.
- **Section headings** — `^## `.
- **Phase-body headings** — `Work Order`, `Retrospective`, `Phase <id> Review`,
  `As-built`, at any level.

For each `done` phase compute its **span**: `start` = its heading line;
`end` = the line before the next phase heading or the next `^## ` heading,
whichever comes first, else EOF.

Then:

- **Skip already-shrunk phases.** A `done` phase whose body has an `As-built`
  heading and no `Work Order` / `Retrospective` / `Phase N Review` heading is
  already shrunk. Excluded unless `--phases` names it explicitly.
- **Nothing to do** — no shrinkable `done` phase → say so in one line and stop.
- **No `todo` phases remain** — say so in one line, note that `/plan:to_as_built`
  is the finishing move, and continue; shrinking makes that command's job
  cheaper. Do not stop to ask.

Set **${SCRATCH}** to the session scratchpad directory (or `${TMPDIR}` if the
session has none). Every intermediate file this command writes lives there and
none of them belong in the user's repo.

**Back up.** `cp <plan> "${SCRATCH}/$(basename <plan>).pre-shrink.md"` and
keep the path for `<Report/>`. Also run `git status --porcelain -- <plan>` in its
repo; if the doc is tracked and clean, `git checkout -- <plan>` is the cleaner
undo and `<Report/>` names it.

State one line: `Shrinking <relative/path> — N done phases, <lines> of archive.`
</Locate>

---

<IndexLiveDependencies>
**Goal:** the retention floor — what the remaining phases still need from the
completed ones. Shrinking may drop anything, *except* something the live zone
would then have to re-derive from the code. This step establishes that floor
before a single line is rewritten.

Skip this step entirely when no `todo` phase remains; the floor is empty.

Dispatch ONE `Explore` (or `general-purpose`) subagent. Its prompt must include:

- The absolute plan-doc path and the line ranges of the **`todo` phases** and
  `## Delegation Context` (from `<Locate/>`'s heading map). Directive: read those
  ranges only — not the `done` phases.
- The identifiers and titles of the `done` phases being shrunk.
- The job: for each `done` phase, list every concrete fact the live zone depends
  on it for — a type or signature a later **Spec** names, a rule a later
  **Constraints from prior phases** cites, a file/line ref, an invariant, a
  decision a later phase builds on. Quote the referencing phase.
- Also return `anchors`: any in-doc link or cross-reference that targets a `done`
  phase heading by title.
- Output format, terse, one block per done phase:

  ```
  Phase 4b: `CommandRegistry::resolve(&self, CommandId) -> Option<&Command>` (Phase 12 Spec);
            validation runs at startup, not first use (Phase 15 Constraints)
  Phase 6:  none
  ```

Capture it as ${FLOOR}. It is small; it goes into every later prompt.
</IndexLiveDependencies>

---

<Shrink>
**Goal:** one replacement block per `done` phase, produced without the archive
text ever entering the orchestrator's context.

Group the `done` phases into **chunks** of at most 6 phases or ~1,000 original
lines, whichever comes first. Dispatch one `general-purpose` subagent per chunk,
**in parallel** — each writes only its own scratch files and never touches the
plan doc, so there is no write race.

Each prompt must include:

- The absolute plan-doc path and, for each phase in the chunk, its identifier,
  heading line, and exact span (`start`–`end`).
- A directive to read **only those spans** (`Read` with `offset`/`limit`), never
  the whole doc.
- The ${FLOOR} entries for the phases in the chunk, stated as a hard floor:
  every listed fact must survive, verbatim where it is a signature or a name.
- `<KeepDrop/>` below, in full.
- Output: for each phase, `Write` the replacement block to
  `${SCRATCH}/shrink_<id>.md`. Its **first line is the phase heading copied
  byte-for-byte** from the original. No file may contain a `#### Work Order`,
  `### Retrospective`, or `Phase <id> Review` heading.
- Return, per phase: `phase <id>: <orig lines> → <new lines>` plus a one-clause
  note for anything kept that reads like narration and why it was load-bearing.

<KeepDrop>
The replacement block reads as a record of code that exists. Present tense,
declarative, no first person, no reference to the plan, the delegate, the
reviewer, or the sequence of attempts.

```markdown
### Phase <id> — <title>  · status: done (`<commit>`)

#### As-built

<2–6 sentences or bullets: what this phase shipped as it now exists — concrete
types, signatures, modules, behavior. The Work Order's Spec corrected by the
Retrospective's deviations: where the two disagree, what shipped wins.>

**Files:**
- `<path>` — <what it holds now>

**Binds later work:** <facts the remaining phases depend on — the ${FLOOR} entries.
Omit the line if none.>

**Gotchas:** <durable traps: calibration constants, invariants enforced by
construction, environment or tooling constraints that still bite. Omit if none.>

**Ruled out:** <one clause each — proposals that were considered and rejected and
would otherwise be re-proposed. Omit if none.>
```

**Keep:**

- What exists now: types, signatures, module paths, file roles, observable behavior.
- The **rule** a decision produced, with at most one clause of why, attached to
  the thing it governs: "`Modifiers` is a bitflags set, not four bools —
  `Keystroke` is the matcher's hash key, so `Eq`/`Hash` are single-op."
- Invariants and the mechanism enforcing them ("non-empty by construction:
  private field, fallible constructor").
- Durable gotchas from the Retrospective's **Surprises**: anything that still
  bites someone touching this code.
- Deviations from the Work Order, folded into the as-built statement — not
  narrated as deviations. The Work Order's plan for a file it never created is
  simply absent from the block.
- Rejected proposals, one clause each, under **Ruled out** — the plan format
  records them so later passes do not relitigate them, and that reason survives
  the shrink.

**Drop:**

- **`Phase N Review` blocks, entirely.** Finding counts, how many were mechanical,
  which became decisions, who approved what. Salvage only the rejections
  (→ **Ruled out**) and any stated plan edit that is not already visible in the doc.
- **Retrospective `What worked`** — process affirmation. Salvage only a design
  fact stated nowhere else.
- **Retrospective `Implications for remaining phases`** — `/plan:phase_review`
  already forward-propagated these into the later Work Orders. Keep only what
  ${FLOOR} lists, under **Binds later work**. When no `todo` phase remains, the
  whole subsection is dead.
- **Work Order scaffolding:** **Goal**, **Acceptance gate**, and **Constraints
  from prior phases**. The gate has been passed; the tests it names live in the
  repo; the constraints came from earlier phases that are themselves shrunk.
- **Prospective and instructional phrasing.** "Create `foo/mod.rs`", "add
  `mod bar;`", "write our own test cases", "check whether the prelude needs
  updating" → rewrite as what now exists, or drop when purely instructional.
- **Justification essays inside Spec.** Collapse an argument to the rule plus one
  clause. Alternatives weighed, tradeoffs enumerated, and reasoning that only
  supported a conclusion go with the argument.
- **Resolved process incidents** that cannot recur — a build that was blind
  because of an ambiguous package spec since fixed, a gate line that was wrong
  and was corrected. Keep the durable residue if there is one ("CI is
  Ubuntu-only, so the macOS half of the platform tests never runs"), drop the
  incident.
- **Resolved `**Pending decision:**` blocks** left in a `done` phase — the
  outcome is in the shipped code.
- Delegate/review vocabulary, pass numbers, session references, commit hashes in
  prose (the heading carries the one that matters).

**Calibration:** a shrunk phase typically lands at 10–25% of its original
length, often under 25 lines. Coming back over ~40% means narration survived —
shrink again. But brevity is not the goal: removing narration is. Never drop a
signature, an invariant, or a gotcha to hit a number.
</KeepDrop>
</Shrink>

---

<Splice>
Assemble the doc deterministically. The orchestrator does not read or rewrite the
plan by hand — a script replaces each span bottom-up, so earlier line numbers stay
valid as later spans shrink.

`Write` a manifest to `${SCRATCH}/shrink_manifest.json`:

```json
{"plan": "<abs plan path>",
 "spans": [{"id": "4b", "start": 637, "end": 738, "file": "<SCRATCH>/shrink_4b.md"}]}
```

`Write` `${SCRATCH}/splice.py`:

```python
import json, pathlib, re, sys

m = json.loads(pathlib.Path(sys.argv[1]).read_text())
plan = pathlib.Path(m["plan"])
lines = plan.read_text().splitlines(keepends=True)
head = re.compile(r"^#{2,4}\s")
phase = re.compile(r"^#{3,4}\s+Phase\b.*·\s*status:\s*done")

for s in sorted(m["spans"], key=lambda s: s["start"], reverse=True):
    start, end = s["start"], s["end"]
    original = lines[start - 1]
    assert phase.match(original), f"span {s['id']} does not start at a done-phase heading"
    assert end >= len(lines) or head.match(lines[end]), f"span {s['id']} does not end at a heading"
    body = pathlib.Path(s["file"]).read_text().splitlines(keepends=True)
    assert body and body[0].rstrip("\n") == original.rstrip("\n"), \
        f"span {s['id']} replacement does not open with the original heading"
    lines[start - 1 : end] = ["".join(body).rstrip("\n") + "\n\n"]

plan.write_text("".join(lines))
print(f"spliced {len(m['spans'])} phases")
```

Run `python3 ${SCRATCH}/splice.py ${SCRATCH}/shrink_manifest.json`.

The three assertions are the structural guarantee: a span that does not begin at
a `done` phase heading, does not end at a heading boundary, or whose replacement
does not open with that exact heading aborts the whole splice before anything is
written. Everything outside the listed spans — the live zone, Delegation Context,
every other section — is untouched by construction.

If an assertion fires, fix the span (usually a boundary miscomputed from the
heading map) and re-run. Do not edit the plan by hand to work around it.
</Splice>

---

<Verify>
Shrinking is lossy on purpose, so it gets one check that it was lossy only where
intended.

**Structural, orchestrator-run, no tokens:**

```
grep -nE '^#{2,4} ' <plan>
wc -l <plan>
```

Diff the phase-heading list against `<Locate/>`'s: identifiers, titles, statuses,
and order must be identical, and no `todo` phase's span may have moved in
content — only in line number.

**Content, subagent:** dispatch ONE `general-purpose` subagent with ${FLOOR}, the
plan path, and the new spans of the shrunk phases. Its job: confirm every
floor item survives in the phase that owns it, verbatim for signatures and type
names. Return `missing` — floor item, owning phase, one line — and nothing else.

For each `missing` item, patch it into that phase's **Binds later work** from the
backup. If more than a couple of items are missing, restore the backup and re-run
`<Shrink/>` with the floor stated more forcefully; a shrink that loses the
floor is not worth hand-repairing.
</Verify>

---

<Report>
Produce a succinct markdown table:

```markdown
| Area | Result |
| --- | --- |
| Shrunk | <N done phases; <before> → <after> lines (<pct>% smaller)> |
| Live zone | <count of todo phases, untouched> |
| Dropped | <what classes of content went: review blocks, retrospective process notes, work-order scaffolding> |
| Preserved | <floor items carried forward; gotchas and ruled-out decisions retained> |
| Repaired | <floor items patched back after verification, or None> |
| Undo | <`git checkout -- <plan>`, or the backup path> |
```

Then stop.
</Report>

---

## Rules

- With no argument, the plan is the one the current `/plan:delegate` run is
  executing, read from the durable run record — not inferred from conversation.
  See `<ResolvePlan/>`; the context that would remember it is the context this
  command exists to stop bloating.
- `done` phases only. The live zone, `## Delegation Context`, and every other
  doc-level section are untouched — see `<ArchiveOnly/>`.
- Never renumber, merge, split, delete, or reorder a phase; heading lines are
  copied byte-for-byte, commit hash included.
- The orchestrator never reads the plan doc's archive text. Structure comes from
  a heading map, rewriting is offloaded to chunk subagents, and assembly is a
  script — that is the whole point of the command.
- Establish the retention floor (`<IndexLiveDependencies/>`) **before** rewriting.
  A fact the remaining phases still depend on is not narration, however it is
  phrased.
- Rejected decisions survive as one-clause **Ruled out** lines. The plan format
  records them so later passes do not relitigate them; a shrink that drops them
  reopens settled ground.
- Back up before splicing, and name the undo in the report.
- Do not change code and do not commit.
- After the last phase ships, `/plan:to_as_built` is still the finishing move.
  This command shrinks the archive; it does not convert the plan into a reference
  doc, and it does not reconcile sibling docs.
