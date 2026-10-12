# Multiply-add hook reach

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Makes the multiply-add edit hook report the float `a * b + c` shapes hana's clippy steps really fail on, only on the lines an edit wrote, with a conditional message, then measures that those failures leave hana's clippy steps.

> **As-built disposition: amend** — `docs/as-built/build-followups-mul-add.md`

> **Production: build-followups** — unit `mul_add-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via the showrunner, 2026-10-12, on the two add-ons left by the first run (`docs/as-built/build-followups-mul-add.md`): on "why the multiply-add hook misses most of hana's warnings", "agreed" to have mul_add investigate it now; on "let ... else with a semicolon in its first part", "yes" to have mul_add fix it after the investigation.

## Measured cause (unit director, 2026-10-12)

Window: 2026-10-06 22:44:05 PDT to 2026-10-12 08:02 PDT. 1,015 hana clippy steps, 321 failed, 88 with a `clippy::suboptimal_flops` multiply-add warning (8.67 per 100; the limit is 1.979), 207 warnings, 168 distinct. 181 of the 207 came from Codex seats through `verify.sh`, where the hook fires, so wiring is not the cause.

The detector reports only what it can prove is `f32`/`f64` from the same file. It reports 7 of the 168. What rejected the others:

| Rejecting rule | Distinct warnings | Example |
| --- | ---: | --- |
| Product operand is a field declared in another file | 54 | `metrics.button.height * 0.5` |
| Product operand is a call, a path or an index | 27 | `hana_organon::CUBE_SIZE * 0.5`, `bounds.height() * 0.5` |
| Addend is a field declared in another file | 18 | `control.min.y + 32.0 * scale` |
| Addend is not a plain name | 12 | `bounds.centre().y - bounds.height() * 0.5` |
| Product chain or cast | 10 | `index as f32 * 54.0 + 28.0` |
| Inside a macro such as `assert!` | 9 | `assert!(x > first.y + row_height() * 0.5)` |
| No float literal and no type in the file | 8 | `clip.x - whole_edge * clip.w` |
| Addend is itself a product | 8 | `CUBE_SIZE * 2.0 + GAP * 2.0` |
| Other shape limits | 15 | multi-line sums, two-step chains |

A prototype that reports every multiply-add shape and skips sums with integer or vector evidence reports 156 of the 168 and leaves 12 of the 88 failed steps with an unreported warning (1.18 per 100). On lint-clean hana main (1,395 checked files, 746 existing `mul_add` calls) the same rule reports 191 shapes when every line counts as written; all are false, since that tree passes clippy. Today's detector reports 0 there.

Evidence, outside the repository, read-only: `~/.local/state/mul-add-hook/reach-2026-10-12/`.

- `diags.json` — the 207 warnings (step, time, seat, path, line, source lines, clippy's suggestion). `extract.py` rebuilds it from the buildlog index.
- `hana_main/` — the lint-clean hana checkout (183 MB, no `.git`).
- `proto.py` — the prototype shape finder; `measure2.py` holds its vector-word rule.
- `gate.py` — the gate measurement: `python3 ~/.local/state/mul-add-hook/reach-2026-10-12/gate.py scripts/hooks` from the worktree root. It calls `mul_add_lib.mul_add_findings` (or `float_mul_add_findings` while that is the name) on each distinct warning placed in a stub function, and on every file of `hana_main`. Today it prints `recall: 7 of 168`, `replay: 83 of 88 … 8.18 per 100`, `false alarms: 0`, in about 10 s.
- `remeasure/` — the first run's re-measure scripts (`clippy_fail_lints.py`, `cost.py`, `loop.py`, `totals.py`, `derive.py`).

## Decisions (unit director, 2026-10-12, under the decision criteria)

- **Report what looks like a multiply-add unless something disproves it.** The message is a condition, because the editing agent knows the types and the hook does not. Risk: on changed lines about one alert in five is false, mostly vector arithmetic. One revert undoes it.
- **Report only on lines the edit wrote.** Old vector arithmetic in a file is never re-reported by a later edit elsewhere in that file.
- **The let-else add-on is resolved by removal.** The pattern-binding, destructuring and let-else exclusions go away with the proof rule, so no let-else rule remains to get wrong.
- **Two code phases.** The written-lines filter lands first, because each merged phase reaches the live configuration at once and the wider detector is too noisy without it.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan changes the multiply-add PostToolUse hook. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/hooks/fn_length_lib.py` — shared by the function-length and multiply-add hooks: the edit parser (`AppliedRustEdits`, `IgnoredEdit`, `applied_patch_rust_files`, `applied_rust_edits`, lines 66–125), the Rust scanner (`tokenize_rust`, `pair_delimiters`), the Cargo lint reader
  - `scripts/hooks/mul_add_lib.py` — the detector: lint scope (`suboptimal_flops_scope`), `_visible`, `_functions`, `_atom`, `_find_in_body` (lines 290–448), the finding type and `float_mul_add_findings` (lines 451–495)
  - `scripts/hooks/post-tool-use-mul-add.py` — the hook entry for Claude `Edit|MultiEdit|Write` and Codex `apply_patch` payloads; builds the block and appends `blocks.jsonl`
  - `scripts/hooks/test_mul_add.py` — its tests (`MulAddHookTests`: a temporary workspace that denies `nursery`, `write_source`, `run_hook`, `decision`, `assert_passes`)
  - `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/test_fn_length.py` — the function-length hook and its tests; they share the edit parser
  - `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py` — install and trust the two Codex hooks; not edited by this plan
  - outside the repository: `~/.local/state/mul-add-hook/blocks.jsonl` (the block log) and the evidence folder above
- **Key files:** `docs/as-built/build-followups-mul-add.md` (hook flow, invariants, exemptions, measured outcome and its method); `docs/as-built/build-followups-fn-length-hook.md` (payload shapes for both agents, the Codex hook facts); `~/.local/state/mul-add-hook/reach-2026-10-12/proto.py`, `measure2.py` and `gate.py` (read-only).
- **Test lanes:** `scripts/hooks/` — `test_*.py` beside the scripts.
- **Build:** none in the repository.
- **Test:** from the worktree root: `python3 -m unittest discover -s scripts/hooks -p 'test_mul_add.py'`; `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'`; `python3 -m unittest discover -s scripts/hooks -p 'test_codex_hooks.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing). Run it once after the edits, fix what it names, and never re-run it on an unchanged tree.
- **Style:** none — not Rust in the repository.
- **Invariants:**
  - The hook never blocks on doubt: no `Cargo.toml`, unreadable TOML, an unbalanced scan, an unknown payload or an internal error passes the edit with at most one `systemMessage` line (`mul_add hook error: <type>: <message>`), exit 0 (as-built).
  - Tests never write the real `~/.codex`, `~/.local/state/mul-add-hook` or `~/.local/state/buildlog`, never run the real `codex` or `cargo`, and never read the live `~/rust/hana` working tree or the evidence folder: fixtures are synthetic (as-built). `gate.py` is a measurement a seat runs by hand, not a test.
  - `clippy::suboptimal_flops` stays enabled in Cargo (the user, 2026-10-06).
  - One parser serves both agents: `applied_rust_edits` in `fn_length_lib.py` (as-built). That file is a hub file this unit may edit (production doc, Hub files). The function-length hook's behavior does not change and `test_fn_length.py` passes.
  - Scan every file of an edit before logging any block (as-built).
  - Python is typed throughout with no `Any` and no file-level type ignores (the user's `CLAUDE.md`).
  - The words in `~/rust/nate_style/rust/forbidden-words.md` never appear in code, comments or docs (the user's `CLAUDE.md`).
  - `~/.claude` main is the live configuration; each merged phase reaches it at once (production rule), so each phase is safe alone.
  - Times carry their zone: this plan states PDT; buildlog stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 3 | Phase 2's checkpoint in the live configuration | `git -C ~/.claude merge-base --is-ancestor <Phase 2 checkpoint> HEAD` succeeds |
| G2 | Phase 4 | 96 hours after T_reach (Phase 3's As-built) | the clock |

## Phases

### Phase 1 — Report only on lines the edit wrote  · status: todo

#### Work Order

**Goal:** The multiply-add hook blocks only for an expression on a line the edit wrote; an old expression elsewhere in the edited file passes.

**Spec:**

Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`.

1. **The parser carries what each edit wrote** (`scripts/hooks/fn_length_lib.py`). `AppliedRustEdits` gains one slot, parallel to `files`:

   ```python
   replacements: tuple[tuple[tuple[str, str], ...], ...]
   ```

   For each file, the `(old, new)` text pairs the edit applied, in payload order. `applied_rust_edits` fills it:
   - Claude `Edit`: one pair `(tool_input.old_string, tool_input.new_string)`.
   - Claude `MultiEdit`: one pair per entry of `tool_input.edits`, from its `old_string` and `new_string`.
   - Claude `Write`: one pair `("", tool_input.content)`.
   - Codex `apply_patch` (`tool_input.command`): under `*** Add File: p`, one pair `("", <the `+` lines without their first character, joined by "\n">)`. Under `*** Update File: p`, one pair per chunk. A chunk is a run of lines that begin with a space, `-` or `+`, or are empty; it ends at a line beginning `@@` or `*** `, or at the end of the patch. `old` is its space and `-` lines, `new` its space and `+` lines, each without the first character, joined by `"\n"`; an empty line is an empty context line. `*** Move to:` carries the section's pairs to the new path. `*** Delete File:` carries none. A path named by two sections has one entry in `files` and the pairs of both.
   - A pair whose field is missing or not a string is left out. A file with no pair still appears in `files`, with `()`.

   `applied_patch_rust_files` keeps its signature and result; build the pairs beside it and share its header reading, so the headers are read in one place.

2. **Written lines** (`scripts/hooks/fn_length_lib.py`):

   ```python
   def written_lines(source: str, replacements: tuple[tuple[str, str], ...]) -> frozenset[int]:
       """1-based numbers of the lines of source that the edit wrote."""
   ```

   For each pair `(old, new)`: drop from `new` the longest prefix it shares with `old`, then the longest suffix it shares with what is left of `old`; the rest is the core, at offset `p` in `new`. An empty core writes no line. For each occurrence of `new` in `source` (left to right, not overlapping), every line touched by `[at + p, at + p + len(core))` is written. When `new` does not occur in `source`, every line of `source` whose stripped text is non-empty and equals the stripped text of a line of `new` that is not a line of `old` is written. Known limit, accepted: unchanged lines between two changed parts of one pair count as written.

3. **The detector filters by line** (`scripts/hooks/mul_add_lib.py`):

   ```python
   def float_mul_add_findings(
       rs_file: os.PathLike[str] | str, written: frozenset[int] | None = None
   ) -> tuple[SuboptimalFlopsLintScope, list[FloatMulAddFinding]]:
   ```

   `None` keeps every finding. Otherwise a finding is kept when any source line from the first to the last line of its expression is in `written`. Nothing else in the detector changes in this phase.

4. **The hook passes them** (`scripts/hooks/post-tool-use-mul-add.py`). For each file, after reading `source`: `written = written_lines(source, <that file's pairs>)`; with no written line, skip the file before the detector is imported; otherwise call `float_mul_add_findings(file, written)`. The block text, the log record and the error path do not change.

5. **Tests** (`scripts/hooks/test_mul_add.py`; parser cases beside the existing `apply_patch` header test). `run_hook` sends, by default, a `Write` payload whose `content` is the file's text, so every existing test still treats the whole file as written. New cases, each through the real hook process unless it names a function:
   - an `Edit` whose `new_string` is a whole new line with a provable float multiply-add blocks;
   - an `Edit` elsewhere in a file that already holds such a line passes, and writes no log line;
   - an `Edit` that replaces part of a line (`old_string` `0.5`, `new_string` `a * 0.5`) and so makes a multiply-add blocks;
   - an `Edit` whose `old_string` and `new_string` share the line holding an old multiply-add as context passes;
   - a `MultiEdit` blocks only for the entries that wrote one;
   - an `apply_patch` update blocks for a `+` line and passes when the old multiply-add is a context line;
   - an `apply_patch` add blocks; a move carries its pairs to the new path;
   - `written_lines`: an empty core writes nothing; two occurrences of `new` both count; `new` absent from `source` falls back to line text;
   - a payload with no `new_string` passes.

   Each test is named for the behavior it pins.

**Files:**
- `scripts/hooks/fn_length_lib.py` — `AppliedRustEdits.replacements`, the pairs in `applied_rust_edits`, `written_lines`
- `scripts/hooks/mul_add_lib.py` — the `written` parameter and the line filter
- `scripts/hooks/post-tool-use-mul-add.py` — compute and pass the written lines
- `scripts/hooks/test_mul_add.py` — the `Write` default in `run_hook` and the new cases
- `scripts/hooks/test_fn_length.py` — only if a case there builds `AppliedRustEdits` by hand

**Seats:** 1 writer + 1 tester — the code and its tests are separate files.
- `impl` — `scripts/hooks/fn_length_lib.py`, `scripts/hooks/mul_add_lib.py`, `scripts/hooks/post-tool-use-mul-add.py`; hub: `scripts/hooks/fn_length_lib.py` (both hooks import it)
- `test` — `scripts/hooks/test_mul_add.py` and `scripts/hooks/test_fn_length.py`: the cases in Spec item 5, written from the signatures in items 1–3

**Constraints from prior phases:** none.

**Acceptance gate:**
- the three Test commands pass;
- `basedpyright` on each changed file ends `0 errors, 0 warnings, 0 notes`;
- `python3 ~/.local/state/mul-add-hook/reach-2026-10-12/gate.py scripts/hooks` still prints `recall: 7 of 168` and `false alarms: 0` (it passes no written lines, so nothing moves);
- the existing scaling test `test_many_findings_scan_scales_with_file_length` passes unchanged.

### Phase 2 — Report every likely multiply-add  · status: todo

#### Work Order

**Goal:** On the lines an edit wrote, the hook reports the multiply-add shapes hana's clippy steps fail on, with a conditional message, and stays quiet for integer and vector arithmetic it can recognize.

**Spec:**

Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`.

The detector changes from "report what is proved float" to "report what looks like a multiply-add unless something disproves it". `~/.local/state/mul-add-hook/reach-2026-10-12/proto.py` is the measured prototype of items 1 and 2; read it, then build the rule into `scripts/hooks/mul_add_lib.py` on the shared scanner. Do not import the prototype.

1. **Shape.** Scan each function body `_functions` returns, with comments and strings blanked. A candidate is a product chain that is a direct operand of binary `+` or `-`, or the whole right side of `+=` or `-=`. In a run of `*`, `/`, `%` at one nesting depth the last operator must be `*`. An operand may be a literal, a name, a path (`a::B`), a field chain, a call, a method chain, an index, a cast (`index as f32`), a `?`, a parenthesized group, or any of these under unary `-`, `&` or `*`. Expressions inside macro invocation arguments (`assert!(…)`, `debug_assert!(…)`, `vec![…]`) are candidates; `macro_rules!` definitions are not. One finding per sum: when both sides of one `+` or `-` are products, the left product is the receiver side, as clippy chooses.

2. **Disproved, so skipped.** Evidence is read from the text of the whole sum.
   - Float evidence: a float literal token, or a match of
     `\bas\s+f(?:32|64)\b|\bf(?:32|64)::|\.to_f(?:32|64)\s*\(|\.(?:sin|cos|tan|sqrt|powi|powf|to_radians|to_degrees|sin_cos|atan2|exp|ln|hypot|recip|fract|signum|lerp)\s*\(|\b(?:PI|TAU|FRAC_PI_\d|EPSILON)\b`.
   - Integer evidence: an integer literal that is a factor of the product or a term of the sum (not inside `[...]` or call arguments, and not a tuple field such as `.0`), or a match of `\bas\s+(?:usize|isize|[iu](?:8|16|32|64|128))\b|\.(?:len|count)\s*\(\s*\)`. A sum with integer evidence and no float evidence is skipped.
   - Vector evidence, which skips the sum whatever else it holds: a match of the `VECTOR` pattern in `measure2.py` (copy it verbatim); or a factor or term that is a bare name whose annotation in the function (a parameter or `let name: T`) is a type other than `f32` or `f64`; or a bare name bound by `let` from a vector constructor (today's `_VECTOR_BINDING` with `_VECTOR_VALUE`); or a bare name that is a file `const` or `static` typed other than `f32` or `f64` (today's `_FILE_VALUE`). A name with no annotation disproves nothing.
   - The `VECTOR` pattern is a starting rule. Narrow it to the end of an operand, or widen it, when the gates below hold or improve; name each change in the report.

3. **Kept exemptions**, as today: lint scope and the no_std root check, `allow`/`expect` attributes, const items, `const fn`, const blocks, static initializers, a sum that is a `.sqrt()` receiver, and today's rule for an unsuffixed literal receiver.

4. **Removed:** the same-file field proof (`_NAMED_FIELDS`, `_SCALAR_TYPE`, the `scalar_fields` and `nonfloat_fields` sets), `_float_atom`, `_literal_compatible`, and the pattern-binding, destructuring and let-else exclusions (`_PATTERN_BINDING`, `_DESTRUCTURED_PATTERN`, `_LET_ELSE`, `_COMPOUND_PATTERN`, `_has_let_else`), with every test that pins them. A name of unknown type no longer silences a report.

5. **Names.** The result no longer claims float: `FloatMulAddFinding` becomes `MulAddFinding` and `float_mul_add_findings` becomes `mul_add_findings`, with the same parameters and fields. Their only callers are the hook entry and the tests.

6. **Rewrite text**, clippy's form: `a.mul_add(b, c)`; `c - a * b` gives `a.mul_add(-b, c)`; `a * b - c` gives `a.mul_add(b, -c)`; `x += a * b` gives `x = a.mul_add(b, x)`. The receiver is the product's left side: a chain `a * b * c + d` gives `(a * b).mul_add(c, d)`. Parenthesize a receiver that is a cast, a unary or a binary expression, and a negated operand that is not a single name, literal, call or field chain. `expr` and `rewrite` are source text with each run of whitespace collapsed to one space.

7. **Message** (`scripts/hooks/post-tool-use-mul-add.py`). One description per finding:

   `<shown>:<line>: if <expr> is f32 or f64 arithmetic, write <rewrite> (clippy::suboptimal_flops)`

   `reason` is the descriptions joined by `"; "`, then `". Leave integer and vector arithmetic as it is. The edit was applied."`. `additionalContext` is the descriptions joined by newlines. `systemMessage` is `mul_add: <file> has <N> possible multiply-add expression(s)`, or `mul_add: <N> possible multiply-add expression(s) in <files>` for several files. `decision`, `continue`, the one-block-per-call rule and the log record are unchanged. The module docstring says "likely" where it said "provable".

8. **Tests** (`scripts/hooks/test_mul_add.py`). Replace the proof tests with:
   - one case per rejected class in the plan's "Measured cause" table, using its example, each reported with the expected rewrite;
   - integer evidence skipped (`i * 3 + 1`, `rows.len() * width + pad` with no float evidence); an index literal or a tuple field is not integer evidence (`row.0 + GAP * scale` is reported);
   - vector evidence skipped: a `Vec3` path in the sum, a bare name annotated `Vec2`, a name bound from `Vec3::new`, a file `const` typed `Vec2`;
   - a name bound by `for`, `if let`, a closure parameter, a `match` arm and a `let … else` whose first part holds a `;` are each reported;
   - both products in `a * 2.0 + b * 2.0` give one finding;
   - a sum inside `assert!` is reported; one inside `macro_rules!` is not;
   - every kept exemption still passes;
   - the exact `reason`, `additionalContext` and `systemMessage` for one finding and for two files;
   - the Phase 1 written-line cases still pass with the new shapes.

   Each test is named for the behavior it pins.

**Files:**
- `scripts/hooks/mul_add_lib.py` — the shape finder, the evidence rules, the rewrite text, the renames; the proof code removed
- `scripts/hooks/post-tool-use-mul-add.py` — the conditional message and the renamed call
- `scripts/hooks/test_mul_add.py` — the tests in Spec item 8

**Seats:** 1 writer + 1 tester — the code and its tests are separate files.
- `impl` — `scripts/hooks/mul_add_lib.py`, `scripts/hooks/post-tool-use-mul-add.py`
- `test` — `scripts/hooks/test_mul_add.py`: the cases in Spec item 8, written from items 1–7

**Constraints from prior phases:** Phase 1 added `AppliedRustEdits.replacements` and `written_lines(source, replacements)` in `scripts/hooks/fn_length_lib.py`, the `written: frozenset[int] | None = None` parameter of the findings function, and the `Write` default in the tests' `run_hook`. Keep all four; this phase does not edit `fn_length_lib.py`.

**Acceptance gate:**
- the three Test commands pass;
- `basedpyright` on each changed file ends `0 errors, 0 warnings, 0 notes`;
- `python3 ~/.local/state/mul-add-hook/reach-2026-10-12/gate.py scripts/hooks` from the worktree root prints:
  - `recall:` at least 152 of 168 (the prototype: 156);
  - `replay:` at most 15 of 88 failed steps (the prototype: 12; 20 is the 1.979 limit);
  - `false alarms:` at most 200 (the prototype: 191); aim for 120 or fewer;
  - and finishes within 20 s on natedev (today: about 10 s);
- the report lists each unreported real warning with the rule that skipped it, and the classes of the remaining false alarms;
- `test_many_findings_scan_scales_with_file_length` passes.

### Phase 3 — Live check  · status: todo

**Blocked by:** G1 — Phase 2's checkpoint is in `~/.claude` on natedev.

#### Work Order

**Goal:** A real Claude edit and a real Codex `apply_patch` on natedev each receive the conditional block, and an edit that leaves such a line untouched receives none.

**Spec:**

Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. The scratch crate lives in the seat's scratch folder, outside every repository.

1. Make a scratch workspace: `Cargo.toml` with `[workspace] members = ["crate"]` and `[workspace.lints.clippy] nursery = "deny"`; `crate/Cargo.toml` with `[lints] workspace = true`; `crate/src/lib.rs` with one function holding `let top = 1.0_f32;`.
2. Codex: with `apply_patch`, add the line `let centre_row = top + metrics.button.height * 0.5;`. The seat receives one block whose text holds `if top + metrics.button.height * 0.5 is f32 or f64 arithmetic, write metrics.button.height.mul_add(0.5, top) (clippy::suboptimal_flops)`.
3. Codex: with a second `apply_patch`, change the `let top` line only. No block arrives.
4. Claude: the unit director repeats steps 2 and 3 in its own scratch crate with `Edit`, and records what it received.
5. `python3 scripts/hooks/codex_hooks.py check` exits 0.
6. `~/.local/state/mul-add-hook/blocks.jsonl` gained one record per block, with `agent` `codex` and `claude`.
7. Record T_reach: the committer time, in PDT, of the commit that brought Phase 2's checkpoint into `~/.claude` (`git -C ~/.claude log -1 --format=%cI <that commit>`).
8. Write the result under "Measured outcome" in `docs/as-built/build-followups-mul-add.md`: a short "Detector reach, live check" paragraph with T_reach, both agents' block text and the two no-block results.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — the "Detector reach, live check" paragraph

**Seats:** 1 writer — one doc paragraph and one live check; nothing splits.
- `impl` — `docs/as-built/build-followups-mul-add.md`, and the Codex half of the live check
- `test` — opens as impl; no file

**Constraints from prior phases:** Phase 2 renamed the entry to `mul_add_findings` and set the message text in Spec step 2 above. Phase 1 made the hook report only on written lines, which step 3 checks.

**Acceptance gate:** steps 2–6 observed and recorded; the doc paragraph holds T_reach with its zone.

### Phase 4 — Re-measure  · status: todo

**Blocked by:** G2 — 96 hours after T_reach.

#### Work Order

**Goal:** The measured rate of hana clippy steps that fail with a multiply-add warning, over the 96 hours after T_reach, is recorded against its limits.

**Spec:**

Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add`, branch `build-followups-mul-add`. Read-only measurement; saved output stays under a few MB.

1. Window: T_reach to T_reach + 96 h. Source: the buildlog index `~/.local/state/buildlog/index.sqlite` (table `steps`) and its gzip logs. Method and scripts: the "Measured outcome" section of `docs/as-built/build-followups-mul-add.md`, with `~/.local/state/mul-add-hook/reach-2026-10-12/remeasure/` (`clippy_fail_lints.py`, `cost.py`, `loop.py`, `totals.py`, `derive.py`) and `extract.py` beside it. Copy the scripts to scratch before changing a window constant; never edit the evidence folder.
2. Report, for `clippy::suboptimal_flops`:
   - failed steps per 100 hana clippy steps: limit 1.979 (baseline 7.916; first re-measure 8.126; since go-live 8.67);
   - sole-cause seat-hours per day: limit 0.100 (baseline 0.402; first re-measure 0.576);
   - of the window's warnings, how many had an earlier block for the same file and expression in `~/.local/state/mul-add-hook/blocks.jsonl`, how many had none, and for those with none, which rule skipped them (run the detector on a stub, as `gate.py` does);
   - blocks in the window: total, by agent, and how many were followed by a `mul_add` on that line in the next commit touching the file (a sample of 30 is enough).
3. Each limit gets PASS or FAIL. A FAIL names its largest remaining cause with counts.
4. Write the result as a "Detector reach, re-measure" table and paragraph under "Measured outcome" in `docs/as-built/build-followups-mul-add.md`.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — the "Detector reach, re-measure" table and paragraph

**Seats:** 1 writer — one measurement and one doc section; nothing splits.
- `impl` — `docs/as-built/build-followups-mul-add.md`
- `test` — opens as impl; no file

**Constraints from prior phases:** T_reach is in Phase 3's As-built. Phase 2 changed the block's wording, so match blocks to warnings by `file` and `expression` in `blocks.jsonl`, never by message text.

**Acceptance gate:** both limits reported with PASS or FAIL from the 96-hour window; every number traces to a query the report names.

## Out of scope

- Running `cargo clippy --fix` inside the lint step. It would be exact and needs no agent turn, but it rewrites files during a lint step and lives in `scripts/delegate/verify.sh`, which this plan's author left to its owning units. Raise it with the showrunner if Phase 4 fails.
- Cross-file type inference.
