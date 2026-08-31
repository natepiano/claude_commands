---
description: Run the Rust lint pipeline, or run its style review alone without repeating the rest of the pipeline
---

<InvocationModes>
Parse these case-insensitive whole-word control tokens before any remaining
arguments can reach `lint clippy`, then strip them from `$ARGUMENTS`:

- `style-only` sets `STYLE_ONLY = true`. This invocation runs
  <LoadOperationSwitches/> and then exactly one <StyleReview/>. It does not run
  the mend, Clippy, rustdoc, batch-fix, or formatting stages.
- `no-style` sets `NO_STYLE = true`. This invocation runs the normal pipeline
  with `LINT_OP_STYLE_REVIEW=off` for this invocation only; it does not edit
  `config/lint.conf`.
- `since <ref>` sets `STYLE_SINCE = <ref>` and consumes the argument after it.
  <StyleReview/> then reviews everything that changed from that commit forward —
  committed work included — instead of the working tree alone. Use it when the
  work under review is already committed, as /plan:delegate's branch-wide review
  is. STOP and report if `git rev-parse --verify <ref>` does not resolve.
- `no-agents` sets `NO_AGENTS = true`. This invocation runs every stage in the
  main agent: no scan agent, no fix wave, whatever `config/clippy.conf` says. It
  does not edit that file.

If both `style-only` and `no-style` are present, STOP and report that they are
mutually exclusive. `STYLE_ONLY`, `NO_STYLE`, and `NO_AGENTS` default to false;
`STYLE_SINCE` defaults to empty.
</InvocationModes>

<AutoProceed>
If $ARGUMENTS contains the token `auto-proceed` (injected by /plan:delegate and
the codex work orders it composes), this run is non-interactive: strip the token
before any remaining arguments reach `lint clippy`, and <BatchDecisionPoint/>
reports the batch then immediately executes it as **proceed** — no stop, no user
wait. Auto-proceed does NOT soften the hard stops in <RunMendFix/> or the
environmental-failure handling anywhere in this skill — those still stop.

Auto-proceed also sets `NO_AGENTS = true`. The callers that inject the token are
delegate work orders and codex sub-sessions; a codex session has no agent tool
to launch a scan or a fix wave with, and a delegate phase is already parallel at
the phase level with a reservation held over the worktree. Both run every stage
inline.
</AutoProceed>

<LoadOperationSwitches>
Run:
```bash
bash ~/.claude/scripts/lint/lint_config.sh export
```

It prints one `LINT_OP_<NAME>=on|off` line per check, read from
`~/.claude/config/lint.conf` (edited with `/lint_config`). A check whose value
is `off` is **skipped entirely** — do not run its command, do not substitute an
equivalent by hand, and do not create todos from it.

After reading the file, if `NO_STYLE = true`, set
`LINT_OP_STYLE_REVIEW=off` for this invocation. If `STYLE_ONLY = true`, ignore
the other operation switches: execute <StyleReview/> when
`LINT_OP_STYLE_REVIEW=on`, otherwise report `Style review: Off` and stop. The
global switch remains authoritative for whether a requested style-only pass is
allowed to run.

| Variable | Gates in this skill |
|---|---|
| `LINT_OP_MEND` | <RunMend/> and <RunMendFix/> |
| `LINT_OP_STYLE_REVIEW` | <StyleReview/> |
| `LINT_OP_CLIPPY` | <RunClippy/> |
| `LINT_OP_DOC` | <RunDoc/> |
| `LINT_OP_FMT` | <RunFmt/> |

These are the same switches `scripts/delegate/verify.sh` and the fix pipeline read —
one key per check, no per-consumer override — so `clippy=off` here means clippy
is off in every delegate phase too.

One interaction the switches change:

- **A disabled stage in <ReportFindings/>.** Keep its row and write `Off` in
  both Result and Action. Never report a skipped stage as clean — an unrun
  check found nothing because it did not run.

In the normal pipeline, if every check is off, print the Findings table with
all rows `Off` and stop.
If the script fails, report the error and stop; do not assume defaults.
</LoadOperationSwitches>

<LoadDelegationSettings>
Decides who runs the work — this agent, or agents it launches. Separate from
<LoadOperationSwitches/>, which decides which checks run at all.

Run:
```bash
bash ~/.claude/scripts/lint/clippy_config.sh export
```

It prints one `CLIPPY_<KEY>=<value>` line per key from
`~/.claude/config/clippy.conf`, hand-edited like `config/delegate.conf`. The
keys are `SCAN_AGENT`, `FANOUT`, `MIN_FINDINGS`, `MIN_FILES`, `MAX_AGENTS`,
`FINDINGS_PER_AGENT`, and `PROGRESS_INTERVAL_SECONDS`.

**A non-zero exit means delegation is unavailable, not that the run stops.** The
script reports every problem it found — a missing file, an unset key, a value
that is not a number, a value below its minimum — and takes no default. Set
`SCAN_AGENT=off` and `FANOUT=off` for this invocation, print the script's stderr
verbatim under a one-line heading saying the pipeline is running inline because
of it, and continue. The lint itself never depends on this file: running every
stage in this agent is what the skill did before the file existed.

If `NO_AGENTS = true`, skip the script entirely and treat both switches as off.

`SCAN_AGENT=on` selects <ScanDispatch/> over the inline stage walk.
`FANOUT=on` lets <Triage/> reach <FanOut/>. The two are independent: a scan
agent with an inline fix batch is a normal combination, and so is the reverse.
</LoadDelegationSettings>

<WaveDirectory>
Set `${WAVE_DIR}` to `/tmp/claude/clippy-<epoch seconds>` and create it, once
per invocation, before the first agent launch. Everything an agent writes and
everything supervision reads lives there: `findings.md` from <ScanPrompt/>,
`assignments.md` from <FanOut/>, `report_<n>.md` from each fixer, and
`heartbeat_<n>.log` per fixer.

It sits beside the style review's existing `/tmp/claude/style-review.diff`, and
it is scratch: never commit it, never reference it in user-facing text, and
never carry a path from it into a fix description.
</WaveDirectory>

<LoadStyleGuide>
Load the Rust style guide by running:
```bash
zsh ~/.claude/scripts/rust_style/load-rust-style.sh
```
Confirm using the script summary line. Then proceed.
</LoadStyleGuide>

<ScanDispatch>
Runs the mend, clippy, and doc stages. With `CLIPPY_SCAN_AGENT=off` this agent
runs them itself, inline, exactly as <RunMend/>, <RunMendFix/>, <RunClippy/>,
and <RunDoc/> define them, and <ScanPrompt/> does not apply.

With `CLIPPY_SCAN_AGENT=on`, launch **one** agent for all three. Not three
agents: mend, clippy, and doc each take the same `target/` lock, so parallel
scans serialize behind each other and behind rust-analyzer's check-on-save,
turning a shorter wall clock into a longer one.

What the agent buys is context, not speed. A workspace clippy run emits
hundreds of lines of cargo output to yield a dozen findings, and the reading,
the per-finding style-guide lookup, and the fix wording all happen where that
output already is.

1. Execute <WaveDirectory/>.
2. Launch one `Agent` call: `subagent_type: general-purpose`, `name:
   clippy-scan`, prompt composed per <ScanPrompt/>.
3. Tell the user in one line that the lint scan is running and that its findings
   arrive as a batch.
4. End the turn. The completion notification resumes the workflow. Do not poll
   it, and do not arm a timer: there is one agent, no partition to supervise,
   and nothing a mid-scan report could say that its findings will not.

On completion:

- **A hard stop was reported** — a reverted mend fix, or an environmental
  failure. Execute the matching stop from <RunMendFix/>, <RunMend/>,
  <RunClippy/>, or <RunDoc/> using the material the agent returned. Do not re-run
  the command to see for yourself: a reverted mend fix leaves the working tree
  at exactly the state that reproduces it, and running mend again is what
  destroys that.
- **Findings** — read `${WAVE_DIR}/findings.md` and carry its rows into
  <ReportFindings/> and <CreateBatchTodoList/>.
- **`findings.md` missing or unreadable while the agent reported findings** —
  the dispatch failed. Say so in one line and run the stages inline instead.
  Never reconstruct rows from the agent's summary text.
</ScanDispatch>

<ScanPrompt>
The scan agent is a fresh session that inherits nothing from this one — not the
loaded style guide, not `config/lint.conf`, not any contract in this file. Every
section below is composed into its prompt in full.

1. **Role.** Run the listed lint commands and report what they found. Fix
   nothing by hand. The one thing that writes to the tree is
   `lint mend --fix`, which applies its own fixes; that is the tool working, not
   the agent editing.
2. **Commands**, in this order, each exactly as written, and each with the
   sandbox disabled — a crate whose build script calls Swift Package Manager
   fails under a nested sandbox in a way that reads as a broken dependency:
   - `~/.claude/scripts/lint/lint mend` — skip when `LINT_OP_MEND=off`
   - `~/.claude/scripts/lint/lint mend --fix` — only when the check found
     fixable items
   - `~/.claude/scripts/lint/lint clippy ${ARGUMENTS}` — skip when
     `LINT_OP_CLIPPY=off`; pass the caller arguments through verbatim
   - `~/.claude/scripts/lint/lint doc` — skip when `LINT_OP_DOC=off`
   Substitute the resolved `LINT_OP_*` values from <LoadOperationSwitches/> into
   the prompt rather than naming the variables: the agent cannot read them. A
   skipped stage is reported as skipped, never as clean.
3. **Hard stops**, copied verbatim from <RunMendFix/>, <RunMend/>, <RunClippy/>,
   and <RunDoc/>, with one change in wording: where those sections say to stop
   and present material to the user, the agent stops and **returns** that
   material. It never retries a reverted mend fix, never applies one by hand,
   and never continues to the next command after a hard stop.
4. **Per finding**, before writing its row:
   - Run the `lint:` frontmatter lookup —
     `grep -l "^lint:.*\b<lint_name>\b" ~/rust/nate_style/rust/*.md docs/style/*.md 2>/dev/null`
     — and name the matched file in the Rule column, `.md` dropped. No match is
     `—`, never the word "none". Reading the matched rule file is enough; the
     agent does not load the style guide.
   - Write the fix approach as one imperative sentence naming the concrete edit.
   - Add a Note only where one sentence cannot hold it.
5. **Output.** Write the rows to `${WAVE_DIR}/findings.md` as the table in
   <BatchDecisionPoint/> — its column rules, its Notes guidance, and both of its
   worked examples of a bad and a good note, all copied verbatim into the
   prompt. Return only a summary: per-stage counts, any hard stop, and the path
   it wrote. The rows themselves stay in the file.
6. **Boundaries.** No commits, no branch, no push. Write nothing outside
   `${WAVE_DIR}` beyond what `mend --fix` rewrites on its own. Do not run any
   cargo command that is not on the list above.
</ScanPrompt>

<RunMend>
Execute: `~/.claude/scripts/lint/lint mend`

The `lint` wrapper owns the cargo-mend exception: it runs mend with
`RUSTC_WRAPPER=` because wrapper tools can otherwise receive `cargo-mend` in
the rustc position and fail before code is checked. Do not run `cargo mend`
directly from this skill.

Error Handling:
- **Environmental Issues (Stop execution):** If mend fails due to missing Cargo.toml or missing toolchain, inform user: "cargo mend cannot run - environment setup required." Then exit.

Analyze output:
- If mend reports **fixable items**, proceed to <RunMendFix/>.
- If mend reports **zero issues**, skip to <RunClippy/>.
- If mend reports **only unfixable items**, note them and skip to <RunClippy/>.
</RunMend>

<RunMendFix>
Execute: `~/.claude/scripts/lint/lint mend --fix`

The `lint` wrapper applies the same cargo-mend wrapper exception noted in
<RunMend/>.

Error Handling:
- **Fix reverted due to compiler error (HARD STOP — capture reproduction):** If
  mend applies a fix, the resulting code fails to compile, and mend reverts it,
  this is a bug in cargo mend — it should never fail on a fix it claims it can
  apply. **STOP immediately. Do NOT proceed, do NOT retry, do NOT attempt the
  fix manually.** The working tree is now back at its pre-fix state, which is
  exactly the state needed to reproduce the bug. Present the following so the
  user can copy it verbatim and reproduce / fix cargo mend:
    1. The full `~/.claude/scripts/lint/lint mend --fix` output, including
       the lint(s) mend tried to fix and the compiler error(s) that triggered
       the revert.
    2. The exact command that reproduces it: `~/.claude/scripts/lint/lint mend --fix`
    3. `git rev-parse HEAD` and `git status --short` output, confirming the
       working tree matches the reproduction state.
  Then end your turn and wait for the user. Do NOT proceed to clippy.
- **Any other failure** (non-zero exit, error output, unexpected behavior):
  **STOP immediately**. Report the error output to the user and ask what to do.
  Do NOT proceed to clippy.

If successful: Note any remaining unfixable items from the earlier `cargo mend` run, then continue with the next step in <ExecutionSteps/>.
</RunMendFix>

<RunFmt>
Execute: `~/.claude/scripts/lint/lint fmt`

This step runs whether mend fixed anything, found nothing, or had only unfixable items. `LINT_OP_FMT=off` is the only thing that skips it.

Check `git diff` after running to determine if fmt applied any changes.
- If diff is non-empty: fmt applied formatting fixes. Note this for the completion summary.
- If diff is empty: no formatting changes needed. Note this for the completion summary.
</RunFmt>

<RunClippy>
The `lint` wrapper supplies `--all-targets --all-features -- -D warnings` and
resolves package scope per run: only the workspace members the working tree
changed, widening to `--workspace` when a root manifest, lockfile, or shared
tool config changed, when nothing changed, or when over half the members did.
Pass `--workspace` or `-p <pkg>` in `$ARGUMENTS` to choose the scope yourself.

Execute: `~/.claude/scripts/lint/lint clippy ${ARGUMENTS:-}`

If $ARGUMENTS provided, use as additional flags — after removing the
`auto-proceed`, `style-only`, `no-style`, and `since <ref>` tokens, which are
mode switches, not clippy flags (see <InvocationModes/> and <AutoProceed/>).
If different base configuration needed, user can override CLIPPY_FLAGS.

Error Handling:
- **Environmental Issues (Stop execution):** If clippy fails due to missing Cargo.toml, network issues, or missing toolchain, inform user: "Clippy cannot run - environment setup required. Check for Cargo.toml and valid Rust workspace." Then exit.
- **Compilation Errors (Process as todos):** If clippy fails due to compilation errors, treat these as high-priority todos alongside any warnings found.

Capture all output for analysis - both successful warnings and compilation errors become todos.
</RunClippy>

<RunDoc>
Execute: `~/.claude/scripts/lint/lint doc`

This mirrors the cargo-port `doc` lint command. `-D warnings` promotes every
rustdoc lint (broken intra-doc links, invalid codeblock attributes, bare URLs,
unescaped backticks, …) to an error, so a non-zero exit means a doc problem.
`--no-deps` keeps dependencies' docs (and their warnings) out of the run.

Error Handling:
- **Environmental Issues (Stop execution):** If the command fails due to a missing Cargo.toml, network issues, or missing toolchain, inform user: "cargo doc cannot run - environment setup required." Then exit.
- **Doc Errors (Process as todos):** rustdoc errors become todos alongside clippy findings.

Note: `missing_docs` is a rustc compile-time lint, not a rustdoc one — it does NOT surface here. It is caught by clippy/check when configured in `[lints.rust]`.

Capture all output for analysis - rustdoc errors become todos.
</RunDoc>

<ReportFindings>
Present a summary before proceeding to todos, as a table — one row per lint
stage, no prose around it:

## Findings
| Stage | Result | Action |
|---|---|---|
| Mend | [one of: "Fixed N issues" \| "No issues found" \| "No fixable issues found" \| "Off"] [if unfixable: ", N unfixable"] | [Applied \| N todos below \| Off \| —] |
| Style review | [one of: "N violations fixed" \| "All changes conform" \| "No uncommitted changes" \| "Off"] | [Applied \| Off \| —] |
| Clippy | [one of: "N issues across M files" \| "No issues found" \| "Off"] | [N todos below \| Off \| —] |
| Doc | [one of: "N rustdoc errors across M files" \| "No issues found" \| "Off"] | [N todos below \| Off \| —] |

The **Action** column carries what used to be explanatory prose: mend and style
fixes are already applied (`Applied`), unfixable mend issues / clippy issues /
rustdoc errors become todos (`N todos below`), and a clean stage is `—`. Do not
restate any of that in sentences under the table.

A stage disabled by <LoadOperationSwitches/> reads `Off` in **both** columns.
Keep the row — dropping it, or writing "No issues found" for a stage that never
ran, hides the fact that the check was skipped.
</ReportFindings>

<CreateBatchTodoList>
When <ScanDispatch/> ran an agent, the rows already exist in
`${WAVE_DIR}/findings.md` with the `lint:` lookup done and the fix sentence
written. Read them and apply only the grouping and ordering below. Do not
re-derive a row, re-run a lookup, or reword a fix sentence to match a house
style it already follows — the scan agent was given these same rules.

Create a comprehensive todo list combining all clippy, rustdoc, AND unfixable mend issues:
- Group related issues in same function/struct into single todos when logical
- Each todo includes fix description and affected file locations
- Label each todo with its source (clippy, doc, or mend) for clarity
- For each clippy todo, run the `lint:` frontmatter lookup described in
  `<FixingGuidelines/>` and include the matched rule file in the todo (e.g.
  `rule: never-allowclippytoomanylines.md`). Only include this field when a
  matching rule file exists — omit it otherwise rather than writing "none".
- Present complete batch for user decision
- Do not plan a build per todo. Compile feedback comes from the LSP during the
  edits and at most one check for the whole batch — see <BatchExecution/> for
  the inline path and <WaveConvergence/> for the fan-out one.
</CreateBatchTodoList>

<BatchDecisionPoint>
**Auto-proceed mode (see <AutoProceed/>):** print the same Issues Found block
and per-todo details below, then immediately execute <BatchExecution/> as
**proceed**. Skip the Available Actions menu; do not end the turn.

**Otherwise this is a hard gate. STOP here. Do NOT edit any files until the user
has selected one of the Available Actions below. This applies even if there is
only one issue — the user must approve every fix before execution.**

Present the complete batch of fixes exactly as follows — **one table, one row
per todo**. No paragraph-per-issue write-ups; the columns carry the metadata.

## Issues Found
[total] issues — mend (unfixable) [mend_count] in [mend_file_count] files, clippy [clippy_count] in [clippy_file_count] files, doc [doc_count] in [doc_file_count] files
Execution: [the one line from <Triage/> — who fixes these, and why that many]

| # | Src | File:line | Lint | Rule | Fix |
|---|---|---|---|---|---|
| 1 | mend | src/project/cargo/parse.rs:78 | forbidden_pub_crate | use-narrowest-visibility | Re-export `ManifestTargets` through `cargo/mod.rs` and `project/mod.rs`, matching `GitRepoPresence`. |

Column rules:
- **#** — todo number, also the key for any Notes entry below.
- **Src** — `mend` / `clippy` / `doc`, lowercase.
- **File:line** — repo-relative path and line, nothing else.
- **Lint** — the lint or rule name that fired. For a style-review violation with
  no lint name, use the rule's short name.
- **Rule** — matched style-guide filename from the `lint:` frontmatter lookup,
  `.md` dropped for width. `—` when no rule file matches (do not write "none",
  do not leave the cell blank).
- **Fix** — the fix approach in **one sentence**, imperative, naming the concrete
  edit. This is the last column so its wrapping does not shift the others.

Anything that does not fit one sentence — a rationale, a constraint that rules
out the obvious fix, a pre-existing off-rule pattern deliberately not copied —
goes in a `### Notes` section under the table, prefixed with the row number it
belongs to (`**2.** …`). Do not inflate the Fix cell to carry it, and do not add
notes for rows that do not need one; omit the section entirely when every row is
self-explanatory.

**Notes must be readable at a glance.** Give each note a short bolded lead-in
naming what that paragraph answers, and put one idea in each paragraph. The
questions a note usually needs to answer, in this order:

1. **What is being flagged** — the specific thing in the code, with the
   surrounding pattern that makes it stand out.
2. **Why the fix is safe** (or what makes it risky) — what keeps working, and
   which call sites move.
3. **How it was verified** — which tool answered, and what the fallback covers
   if the preferred one did not.
4. **Where the rule comes from** — only when the `lint:` lookup found nothing
   and the rule had to be traced somewhere else.

Skip any of these that do not apply. A note that only needs one sentence stays
one sentence.

#### Do not write notes like this

The following is a real note that was rejected as unreadable. Every fact in it
is correct; the problem is that four unrelated findings are fused into one
unbroken block, so nothing can be located without reading all of it:

> **1.** No style-guide file carries `review-pub-mod` in its `lint:`
> frontmatter, so the mechanical lookup found nothing. `use-narrowest-visibility.md`
> still governs it in prose: "`pub mod` is forbidden by never-use-pub-mod" (that
> referenced rule file isn't present in `~/rust/nate_style/rust/`). The fix is
> zero-call-site: all five public items are already re-exported from the crate
> root at `lib.rs:80-84` (`ImageDelay`, `Outputs`, `Settings`, `State`,
> `register`), and the sole external consumer — `crates/hana/src/video_plane/mod.rs:169`
> — calls `hana_mimesis_tools::register_image_delay`, the re-export, not the
> module path. `image_delay` is also the only `pub mod` in that file; the other
> 17 are private `mod` with `pub use` re-exports. LSP `findReferences` on the
> module declaration returned nothing (rust-analyzer doesn't resolve module decls
> as symbols), so coverage here rests on ripgrep across every `.rs` in the
> workspace, which is exhaustive for a module path since it can't be reached
> through a type alias or generic dispatch.

What is wrong with it: it opens on rule provenance (the least useful fact)
instead of on what the code does; the verification caveat is buried in the last
clause; parenthetical asides carry information the reader needs; and they have
to hold four threads at once because none of them are separated or labeled.

#### Write it like this instead

> **1. What mend is flagging.** `hana_mimesis_tools/src/lib.rs` declares 18
> modules. Seventeen are private (`mod foo;`). One — `image_delay` — is
> `pub mod`. The style guide bans `pub mod`.
>
> **Why making it private is safe.** Nothing gets hidden, because the crate root
> already re-exports the five things callers need (lines 80–84): `ImageDelay`,
> `Outputs`, `Settings`, `State`, `register`. Those re-exports keep working when
> the module itself goes private. The only code outside the crate that touches
> `image_delay` is `crates/hana/src/video_plane/mod.rs:169`, and it calls
> `hana_mimesis_tools::register_image_delay` — the re-export, not the module
> path. So no caller changes.
>
> **How I checked.** LSP was the right tool but returned nothing: rust-analyzer
> doesn't treat a `mod` declaration as a findable symbol. I fell back to ripgrep
> for `image_delay` across every `.rs` file in the workspace. For a module name
> that's a complete check — unlike a type, a module path can't be reached through
> an alias or generic dispatch, so if grep doesn't see it, it isn't there.
>
> **Where the rule lives.** The skill looks up a lint name in style-guide `lint:`
> frontmatter. Nothing matches `review-pub-mod`. The ban is stated in passing
> inside `use-narrowest-visibility.md`, which cites a `never-use-pub-mod` file
> that doesn't exist in the guide directory. The rule is real; its home file is
> missing.

Same facts, same length. The difference is that a reader who only wants to know
whether the fix is safe can find that answer without reading the other three
paragraphs.

## Available Actions
- **proceed** - Fix all issues using standard clippy guidance
- **change** - Modify the approach (specify changes)
- **stop** - Cancel fixes without making changes

Please select one of the keywords above.

**proceed** routes to <Triage/>, which is where inline and fan-out separate.
The Execution line above already states which one it will be, so the user
approves the fixes and the method in the same answer. Never open a second gate
asking whether to use agents: the count is computed from
`~/.claude/config/clippy.conf`, and how already-approved work is divided is not
a decision to hand back.

**After printing this block, end your turn. Wait for the user's next message.
Do not call any Edit/Write/Bash tools until then.**
</BatchDecisionPoint>

<Triage>
Runs the moment the batch is approved, before any edit. Decides who applies the
fixes and states it in one line. It is not a gate and never asks a question.

Inline whenever any of these holds:

- `NO_AGENTS = true`, or `CLIPPY_FANOUT=off`, or <LoadDelegationSettings/>
  reported the config unusable.
- `T < CLIPPY_MIN_FINDINGS`, where `T` is the count of **contained** todos —
  those whose fix stays inside one file. Coupled todos are withheld from the
  wave by <FanOut/> and closed inline, so counting them here would size a wave
  around work no fixer receives.
- `F < CLIPPY_MIN_FILES`, where `F` is the count of distinct files those todos
  touch.
- The computed agent count is below 2.

Otherwise fan out with

```
N = min(CLIPPY_MAX_AGENTS, F / 2, ceil(T / CLIPPY_FINDINGS_PER_AGENT))
```

integer-dividing `F / 2`. The three terms answer three different limits: the
ceiling the user set, the number of file groups that can be built at all, and
the work there is to divide. The smallest wins.

The Execution line in <BatchDecisionPoint/> states the outcome and its reason in
one sentence — `4 fixers over 4 file groups`, or `inline — 7 findings, below the
12 this repo asks for`, or `inline — 34 findings but only 2 files, and a file is
never split across agents`. A reader who disagrees with the route should be able
to see which key to change without opening the config.

Route: inline goes to <BatchExecution/>, fan-out to <FanOut/>.
</Triage>

<FanOut>
**Partition by file, never by finding.** Every file carrying a todo belongs to
exactly one group, and a fixer edits only its own group. That invariant is the
only thing standing between two agents and the same file: cargo-berth does not
help here, because the pre-edit hook blocks on a different holder, and siblings
in one session share the holder and are all told the reservation is already
held.

**A fixer is given only work it can finish alone.** A todo whose fix reaches
outside one file — a rename with its call sites, a signature or error-type
change, a visibility narrowing with its re-exports — is coupled, and the Notes
on the finding row say so. Coupling that is known before launch is never handed
to the wave to negotiate: negotiation costs round trips, blocks agents mid-fix,
and is the one part of this design with no mechanical guard behind it. Settle it
in the partition instead.

1. Separate coupled todos from contained ones. For each coupled todo, place
   every file it touches in one group if the balance survives it — that is the
   best outcome, since the group then owns the whole change and no message is
   needed. Otherwise **withhold the todo from the wave entirely**: it is not
   assigned to any fixer, and <WaveConvergence/> closes it inline. A withheld
   todo leaves the signature it changes untouched for the duration of the wave,
   so no fixer's file breaks under it and no fixer waits on one.
2. Group the remaining files, balancing by todo count. Every fixer's todos are
   now contained in its own files.
3. Write `${WAVE_DIR}/assignments.md`: every group, its agent name, its files,
   and its todo numbers, plus a **Withheld** section listing each deferred todo
   and the fixer files it would have reached. **Every fixer receives the whole
   map**, not only its own row — a fixer needs to know who owns a file to ask
   for it, and needs to know a withheld todo is deliberate so it does not fix it
   as fallout or wait for someone else to.
4. Name them `clippy-fix-<wave>-1` through `clippy-fix-<wave>-N`, where `<wave>`
   is the epoch suffix of `${WAVE_DIR}`. **The wave id is what makes the name
   safe, not decoration.** A name already held by an agent from an earlier wave
   in this session is not refused — it is silently altered, and the map then
   addresses the previous wave's agents, which a send resumes from their old
   transcripts. A second /clippy run in one session hits this every time. The
   wave id cannot collide, so the name asked for is the name granted, and the
   map is correct by construction.
5. Launch all N in **one message** with `subagent_type: general-purpose`, each
   prompt composed per <FixPrompt/>. One message is what makes them concurrent.
6. **Check the name each launch returned against the name asked for.** If any
   differs, the map every fixer is holding is wrong: send each fixer the
   corrected roster before it coordinates with anyone, and rewrite
   `assignments.md` to match. Silent renaming is the only failure here that
   produces confident messages to the wrong agent. <PeerCoordinationContract/>
   already tells fixers that a correction from the coordinator outranks the map
   and to expect one, so this lands as an amendment rather than as a message
   contradicting their instructions — say plainly which names are stale and that
   they must not be messaged.
7. Continue to <WaveSupervision/>.

The peer protocol still ships in every prompt, but after this partition its job
is only what the partition could not see: a call site the finding Notes missed
and a fixer's own LSP turns up mid-fix. That is the case worth paying round
trips for, because nobody knew about it in time to route around it. A wave whose
agents are negotiating a coupling that was visible before launch is a partition
that gave away work it should have kept.
</FanOut>

<FixPrompt>
Each fixer is a fresh session that inherits nothing — not the style guide, not
the findings table, not any contract in this file. Compose every section below
into its prompt in full.

1. **Role.** Apply the fixes assigned to you. Do not ask questions; there is no
   user on the other end of this prompt.
2. **Your assignment.** Your agent name, your files, and the complete todo rows
   for them copied verbatim from the batch — number, source, file:line, lint,
   rule, fix sentence, and any Note.
3. **The full assignment map** from `${WAVE_DIR}/assignments.md`, every group.
4. **`## Fixing Guidelines`** — <FixingGuidelines/> copied verbatim.
5. **`## Peer Coordination`** — <PeerCoordinationContract/> copied verbatim.
6. **Heartbeat.** Before each activity, run
   `bash ~/.claude/scripts/agents/heartbeat.sh ${WAVE_DIR}/heartbeat_<n>.log agent "clippy-fix-<n>: <activity>"`.
   Short present-tense text. The second argument is the script's source slot,
   which takes `agent` or `wrapper` and nothing else; the agent name belongs in
   the message, where it survives into the log line. Never read the heartbeat
   file.
7. **Verification: run no cargo command at all.** Not `check`, not `clippy`, not
   `test`, not `fmt`. Use the LSP for what an edit needs — does this name
   resolve, does this signature still typecheck, who calls it. Every fixer shares
   one `target/` lock with every other fixer and with rust-analyzer, so N
   parallel builds serialize into something slower than one agent doing all the
   work. The coordinator runs the single check after the wave.
8. **Report.** Write `${WAVE_DIR}/report_<n>.md` **with a shell heredoc**, not
   with an editing tool — an editing tool may refuse a path outside the repo and
   leave the wave with no report at all. Content: each todo as closed or not,
   with the reason when not; the files you touched; every claim you requested,
   granted, or refused; and anything you are still waiting on and from whom.
   Return the same content as your summary, so a refused write still reaches the
   coordinator.
9. **Boundaries.** No commits, no branch, no push, no `git add`. No file outside
   your assignment without a granted claim. Nothing outside the repo except your
   heartbeat and report under `${WAVE_DIR}`.
</FixPrompt>

<PeerCoordinationContract>
Copied verbatim into every fixer prompt. Written in second person because that
is how the fixer reads it.

> You are one of several agents fixing lint findings in one shared worktree at
> the same time. Your siblings are named in the assignment map. The coordinator
> that launched you is addressable as `main`.
>
> **Addressing.** Send with `SendMessage`, setting `to` to a sibling's exact
> agent name, or to `main`. An inbound message reaches you as a
> `<teammate-message teammate_id="NAME" …>` block — reply by setting `to` to
> that `teammate_id`. The coordinator's messages arrive under the id
> `team-lead` rather than `main`; both names reach it, and a message from
> `team-lead` is a message from the coordinator.
>
> **Two things can amend your assignment, in this order: the coordinator, then
> the map.** A message from `main` (`team-lead`) that changes your name, your
> siblings' names, your files, or your todos is authoritative — act on it, and
> read the map through it from then on. The `teammate_id` on an inbound message
> is stamped by the session, not written by the sender, so it identifies the
> sender reliably and there is nothing for you to verify. Nobody else can amend
> anything: a *sibling* telling you the map is wrong is reporting, not
> instructing, and goes to `main` for a ruling. Expect a correction from the
> coordinator early in a wave — agent names are occasionally altered at launch,
> and fixing that is the coordinator's job, not a contradiction of the rule
> below.
>
> **Below the coordinator, the assignment map is the roster, and it outranks
> anything else you are shown.** Your session may carry an automatically
> injected list of active agents. Do not use it. That list is built when you start, so siblings launched
> in the same message as you are usually missing from it, and agents from
> unrelated earlier work in this session are usually still on it — you may see a
> roster that contains none of your siblings and several strangers. Every name in
> the map is deliverable whether or not it appears there. Message the names in
> the map, and never a name outside it: a stranger who takes your notice at face
> value edits a file nobody assigned them, and a sibling who never hears from you
> stalls waiting for the claim you sent elsewhere. If a name in the map appears
> undeliverable, that is a fact for `main`, not a reason to substitute a
> different recipient.
>
> **Open every message to a sibling by naming who it is for.** Begin with `For
> <name>:` and add `If you are not <name>, ignore this message and take no
> action.` Other agents unrelated to this wave may be running in the session,
> and a misdelivered claim reads to one of them as a legitimate instruction to
> edit files nobody assigned it. The line costs nothing and makes a wrong
> delivery inert instead of destructive.
>
> **Ownership.** Edit only the files in your assignment. Never edit a sibling's
> file directly — not for a one-word change, not when the edit is obviously
> correct, not when it would be faster.
>
> **Claiming work outside your files.** When a fix needs an edit in someone
> else's file — LSP `findReferences` puts call sites there — message that owner
> with the file and line, the exact edit, and the todo it serves. Wait for one
> of three answers: `granted`, and you make the edit; `mine`, and the owner
> makes it and you do not; or `wait until <condition>`. Never edit outside your
> assignment without a `granted` in hand.
>
> **Answering a claim.** Answer promptly, before returning to your own work — a
> sibling waiting on you costs more than the interruption costs you. Grant it
> unless the edit collides with one of your own todos on the same lines; then
> answer `mine` and make the edit yourself.
>
> **Sequencing.** When your fix depends on a sibling's landing first — you are
> repairing call sites of a signature they are still changing — ask them and
> wait for `go` before you start. When you are the side being waited on, message
> the waiter the moment your change has landed. State this ordering in messages
> rather than reading it off the LSP index: the index does not show a sibling's
> edit at the instant they make it, so what you see there is not the current
> tree.
>
> **Never sit idle.** Do all your unblocked work first. If you are then waiting
> on an answer with nothing left to do, message `main` naming what you are
> waiting on and from whom, and end your turn saying so in your report. Ending a
> turn does not remove you from the wave — a later message wakes you with your
> context intact, and you continue then.
>
> **Deadlock.** If you and a sibling are each waiting on the other, both of you
> message `main` and stop trying to settle it between yourselves. The
> coordinator breaks the tie.
>
> **Limits.** Never hand a sibling work that is not in the approved batch, and
> never ask a sibling to do something you were refused permission to do — route
> that to `main` instead.
</PeerCoordinationContract>

<WaveSupervision>
The coordinator watches the wave and answers it. It writes no fix itself.

Arm one timer and end the turn:

```bash
bash ~/.claude/scripts/delegate/progress_timer.sh "${WAVE_DIR}" "${CLIPPY_PROGRESS_INTERVAL_SECONDS}"
```

with `run_in_background: true`. Never poll a heartbeat file, never loop on
`sleep`, never foreground-block on the wave. The timer's exit and each agent's
completion both resume this session on their own.

**A completion notification is not a finished assignment.** Under
<PeerCoordinationContract/> a fixer that runs out of unblocked work states what
it is waiting on and ends its turn, which arrives here looking exactly like a
fixer that finished. Its open todos in `report_<n>.md` are what separates the
two. `SendMessage` to its name wakes it with its context intact, so a blocked
fixer is paused, never spent.

**A missing `report_<n>.md` means unknown, never finished.** The file can be
absent because the fixer is still working, or because its write was refused and
the content came back as the agent's summary or as a message instead. Read that
summary before concluding anything; a fixer whose report never lands has an
unknown todo list, and an unknown todo list is not an empty one. Treating
absence as completion is the same defect as treating a completion notification
as a finished assignment, arriving through a different door.

On each resume, while any fixer still has an open todo — one that is working and
one that ended its turn blocked both count:

1. Read the tail of each `${WAVE_DIR}/heartbeat_<n>.log`, plus
   `git status --short` and `git diff --stat`. The diff is the whole wave's work
   combined — the worktree is shared — so per-agent attribution comes from the
   heartbeats and the reports, not from the diff.
2. Print one compact table — Agent | Files | Todos closed | Activity — and one
   or two sentences on what remains. Describe what the code now does. Never
   measure the work in lines, insertions, or file counts.
3. Act on what the tick surfaced: break a deadlock two fixers reported, settle a
   refused claim, answer a fixer waiting on `main`, or stop a fixer that has left
   its assignment. Send it with `SendMessage`; guidance that stays in this report
   reaches nobody.

   **Clear the blocker and wake the fixer before you move its work.** Waking
   costs one message and keeps the agent that already read the code, holds the
   LSP answers, and knows which call sites it found. Reassigning the todo to the
   file's owner, or closing it here, throws all of that away and is the answer
   only when the fixer reported it cannot do the work at all — never when it is
   merely waiting.
4. If fixers remain, arm a fresh timer and end the turn.

This is deliberately **not** the progress contract in
`commands/plan/delegate.md`. No `progress_history.py`, no calibration, no ETA
bands, no cap stages: that machinery reads a plan document with phase headings,
and /clippy has no plan.

If `progress_timer.sh` fails — a missing or non-positive interval — report it in
one line and continue on agent-completion notifications alone. Supervision makes
the wave visible; it is not what makes it finish.
</WaveSupervision>

<WaveConvergence>
Runs once every fixer has reported **and every open todo is one no fixer can
still close**. Those are two conditions, not one: a fixer that ended its turn
waiting on a sibling has reported without finishing. Wake it per
<WaveSupervision/> and let the wave carry its own work. Starting convergence on
the reports alone ends the wave early and quietly moves parallel work back into
this agent.

1. Read every `${WAVE_DIR}/report_<n>.md`. Collect todos closed, todos left open
   with their reasons, and any claim still unanswered. An unanswered claim is a
   fixer to wake, not a remainder to absorb.
2. Close the withheld todos from <FanOut/>'s **Withheld** section, then whatever
   else is genuinely left, inline, following <FixingGuidelines/> — todos a fixer
   reported it could not do, not todos it was waiting to do. The withheld ones
   come first and are the reason this step exists: they are cross-file changes
   held back on purpose, and one agent holding the whole change applies it to
   every call site at once, with no claim to grant and no order to agree on.
3. Re-run <ScanDispatch/> **once** to confirm the batch. One scan for the whole
   wave, never one per fixer — the earlier ban on cargo in the fixers exists so
   that this is the only build the wave pays for. It is also the wave's only
   compile gate: no fixer built anything, so a todo reported closed is a claim
   about an edit, not a verified fact about the tree. Expect this scan to find
   work, and read a fixer's confident report as evidence of intent rather than
   of a tree that compiles.
4. A clean re-scan ends the batch. New findings are fixed inline, and there is
   **at most one such round and never a second fan-out**: a batch that has not
   converged after the wave plus one repair is reported as it stands, with what
   is left and why. This is the runaway ceiling from `config/delegate.conf`
   applied by hand rather than imported — /clippy has no findings ledger to
   count rounds in.
5. Continue to the format stage. The completion summary names how many fixers
   ran, what each closed, and anything still open.
</WaveConvergence>

<BatchExecution>
The inline path — this agent applies every fix itself. <Triage/> routes here
whenever it does not fan out, and <WaveConvergence/> returns here to close a
remainder.

**proceed**: Apply all fixes systematically following <FixingGuidelines/>.

**Compile feedback**: use the LSP for the answer an edit needs (does this name
resolve, does this signature still typecheck, who calls it) — it answers from an
index that is already warm and takes no build-directory lock. Reach for
`~/.claude/scripts/lint/lint check` only when the LSP cannot answer — a macro
expansion, a `cfg` arm the editor is not configured for, a trait resolution
failure it reports without explaining.

Run at most one such check for a batch, after the edits, not one per edit. A
cargo command here contends for the same `target/` lock as rust-analyzer's
check-on-save and as any build already running in this worktree, so a per-edit
build stalls behind them and blocks anything else the session has in flight.

**change**: Ask user: "What modifications would you like to the fixing approach?" Then apply their specified changes to the batch.

**stop**: Exit without applying any fixes.

After batch completion: Display summary of fixes applied and any remaining issues.
</BatchExecution>

<FixingGuidelines>
**Important rules:**
- Do not fix warnings by marking code as dead - remove dead code
- Do not fix warnings by prefixing arguments/variables with _ - remove if unused

**Never weaken a fix to avoid depending on a sibling.** Discarding an error a
sibling is introducing so your file compiles whichever order the two edits land
in, widening a type so a pending signature change cannot reach you, keeping a
conversion you would otherwise delete — each one compiles, coordinates nothing,
and leaves the codebase worse than a single agent would have. The dependency is
what the peer protocol exists to settle: ask, wait for `go`, and write the fix
the finding actually calls for. Where two fixes want the same new type, one
crate gets one definition of it, and the owner of the file it belongs in writes
it.

**Use LSP before any fix that changes a name or signature.** When the fix is a
rename, removal, visibility narrowing, or type change, run LSP `findReferences`
on the target first to enumerate every call site. ripgrep misses references
through type aliases, re-exports, and generic dispatch — LSP doesn't. Apply
the fix at every reference returned. For pure intra-function fixes (rewriting
a closure, inlining a return, removing a needless `&`) LSP is unnecessary.

**Then reread the body you just changed the signature of.** The call sites are
where the attention goes and the body is where the break usually is: narrowing
`&String` to `&str` makes the `name.clone()` inside it yield a `&str` where a
`String` is wanted, and every caller still compiles perfectly. Since no fixer
runs cargo, nothing catches this until the coordinator's scan — so the two
lines under the signature are the first thing to check, not the last.

LSP availability: `LSP` tool is loaded when `ENABLE_LSP_TOOL=1` is in env (in
your settings.json). If unreachable, fall back to ripgrep but expand the scope
to the whole crate (not just the cited file) and note the limitation in the
fix description.

**Consult the style guide per-lint before fixing.** For every clippy finding,
before proposing a fix, grep the loaded style guide for the lint name in the
`lint:` frontmatter property:

```bash
grep -l "^lint:.*\b<lint_name>\b" ~/rust/nate_style/rust/*.md docs/style/*.md 2>/dev/null
```

If a file matches, read it and apply the rule it prescribes (often the
"extract helpers / orchestrator pattern", "no bare allow", or
"test-module allow boilerplate"). Cite the rule file in your fix
description.

If no file matches, state that explicitly ("no style-guide rule governs
`<lint_name>`") and proceed with a judgment call.

Rationale: the style guide has been known to be skipped at fix time even
when loaded. The `lint:` frontmatter property is the single source of
truth that maps clippy lints to the rule that governs them.
</FixingGuidelines>

<StyleReview>
**This step runs even if mend and clippy found zero issues.** `LINT_OP_STYLE_REVIEW=off` is the only thing that skips it.

**Do this step yourself — do not delegate it to a subagent** (Task/Agent tool, Codex sub-session, etc.). Run the diff commands and the rule-by-rule walk inline in this conversation so the user can watch progress rule-by-rule instead of waiting on an unauditable subagent turn.

This is the one stage that never delegates, whatever `config/clippy.conf` says.
`CLIPPY_SCAN_AGENT` and `CLIPPY_FANOUT` do not reach it: a lint command returns
a finding a subagent can hand back intact, while the rule walk **is** the
reporting, and the user reads it as it happens.

1. Build the combined diff under review, then the additions-only text from it. **Untracked files are always included** — a new file is entirely added code, so `git diff --no-index /dev/null <file>` renders it as all-additions. Never review only tracked changes:
   ```bash
   {
     if [ -n "${STYLE_SINCE:-}" ]; then git diff "${STYLE_SINCE}"; else git diff; fi
     git ls-files --others --exclude-standard -z \
       | xargs -0 -I{} git diff --no-index /dev/null "{}" 2>/dev/null
   } > /tmp/claude/style-review.diff
   grep '^+' /tmp/claude/style-review.diff | grep -v '^+++' > /tmp/claude/style-review-additions.txt
   ```
   Export `STYLE_SINCE` from <InvocationModes/> first, empty when no `since` token was given. `git diff <ref>` compares that commit to the working tree, so one command covers committed, staged, and unstaged work in the range. The `git diff --no-index` exit status 1 (differences found) is expected, not an error. The `.diff` file (with `+++`/`@@` headers) drives the banned-words scan so findings report real source `path:line`; the `-additions.txt` (added lines only) is your reading aid for the rule walk. If `-additions.txt` is empty, report "No changes to review." — naming the range when `STYLE_SINCE` is set — and skip remaining steps.

2. If the `=== STYLE_CHECKLIST ===` section is not already in context, execute <LoadStyleGuide/> now. The checklist lists every rule by number and name.

3. **Systematic walk**: For each rule in the checklist, check the additions-only diff. Present results in a table:

   ```
   | # | Rule | Result |
   |---|---|---|
   | 1 | Rule name | Pass / **VIOLATION: description** / Skip (reason) |
   ```

   - **Pass**: additions conform to this rule
   - **VIOLATION**: describe what violates and where
   - **Skip**: rule does not apply to anything in the diff (e.g., "no module declarations changed", "no format strings", "no `#[allow]` added"). Use a short reason.

   **Banned-words rule (special handling):** When the checklist contains a "no
   banned words" / "forbidden words" rule, do **not** enumerate the stems
   yourself, do **not** write a patterns file, and do **not** build an inline
   regex — the bare stems will trip the PostToolUse hook. Instead, pipe the
   combined diff through the canonical scanner's `--diff` mode:

   ```bash
   python3 ~/.claude/scripts/hooks/banned_words_lib.py --diff < /tmp/claude/style-review.diff
   ```

   Exit 0 = Pass. Exit 1 = VIOLATION; each output line is `path:lineno: stem:
   <line>`, where `path:lineno` is the **real source location** of the added
   line (untracked files included). `--diff` scans added lines only, so
   pre-existing lines in tracked files are not re-flagged. The script path
   contains `banned_words_lib`, which the hook's introspection bypass
   recognizes, so neither the command nor its output re-trips the scanner or
   bumps counters.

4. Fix all violations found.
5. After fixes, report: "Style review complete — N violations fixed." or "Style review passed — all changes conform to the style guide."
</StyleReview>

<ExecutionSteps>
**EXECUTE THESE STAGES IN ORDER**, top to bottom. Every stage is gated by
<LoadOperationSwitches/> — a stage whose switch is `off` is skipped outright, and
the pipeline continues at the next enabled stage. Refer to a stage by its name;
the pipeline has no step numbers.

- **Parse tokens** — execute <InvocationModes/> and <AutoProceed/>: strip all control tokens before any command receives `$ARGUMENTS`.
- **Load switches** — execute <LoadOperationSwitches/>: read which stages are enabled. Never skipped.
- **Load delegation settings** — execute <LoadDelegationSettings/>: read who runs them. Never skipped, and never a reason to stop the run.
- **Style-only exit** — if `STYLE_ONLY = true`, execute <StyleReview/> when enabled, report its result, and stop. Run no other stage.
- **Lint scan** — execute <ScanDispatch/>: mend, the mend fix, clippy, and doc, in that order, in one agent or inline. <RunMend/>, <RunMendFix/>, <RunClippy/>, and <RunDoc/> define each command and its error handling whichever way it runs.
- **Style review** — execute <StyleReview/>: evaluate diff against style guide rules (loads style guide only if diff is non-empty). Runs regardless of what earlier stages found; only `LINT_OP_STYLE_REVIEW=off` skips it.
- **Findings report** — execute <ReportFindings/>: present mend, clippy, and doc summary (fmt runs later, at the format stage, and is covered in the completion summary).
- **Batch fix** — if manual mend, clippy, or doc issues found, execute <CreateBatchTodoList/> and <BatchDecisionPoint/>, then <Triage/>. Inline goes to <BatchExecution/>; a fan-out goes to <FanOut/>, <WaveSupervision/>, and <WaveConvergence/>.
- **Format** — execute <RunFmt/>: run `~/.claude/scripts/lint/lint fmt`. Runs regardless of what earlier stages found; only `LINT_OP_FMT=off` skips it. Always this agent, never a fixer — it rewrites files across the whole scope.
- **Completion summary** — report what ran and what it found, including how many fixers ran and what each closed.

Clippy and doc moved ahead of the style review when the scan became one stage.
Both only read the tree, so the diff the style review walks is the one it always
walked; `mend --fix`, the single stage that rewrites source, still runs before
it. Nothing overlaps the style review either — it edits the same files
`mend --fix` does, so the two never run at once.
</ExecutionSteps>
