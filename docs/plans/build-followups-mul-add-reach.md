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
- `gate.py` — the gate measurement: `python3 ~/.local/state/mul-add-hook/reach-2026-10-12/gate.py scripts/hooks` from the worktree root. It calls `mul_add_lib.mul_add_findings` (or `float_mul_add_findings` while that is the name) on each distinct warning placed in a stub function, and on every file of `hana_main`. Before Phase 2 it printed `recall: 7 of 168`, `replay: 83 of 88 … 8.18 per 100`, `false alarms: 0`, in about 10 s; on Phase 2's checkpoint it prints `recall: 160 of 168`, `replay: 7 of 88 … 0.69 per 100`, `false alarms: 122 reports in 36 files`, in about 11 s.
- `remeasure/` — the first run's re-measure scripts (`clippy_fail_lints.py`, `cost.py`, `loop.py`, `totals.py`, `derive.py`).

## Decisions (unit director, 2026-10-12, under the decision criteria)

- **Report what looks like a multiply-add unless something disproves it.** The message is a condition, because the editing agent knows the types and the hook does not. Risk: on changed lines about one alert in five is false, mostly vector arithmetic. One revert undoes it.
- **Report only on lines the edit wrote.** Old vector arithmetic in a file is never re-reported by a later edit elsewhere in that file.
- **The let-else add-on is resolved by removal.** The pattern-binding, destructuring and let-else exclusions go away with the proof rule, so no let-else rule remains to get wrong.
- **Two code phases.** The written-lines filter lands first, because each merged phase reaches the live configuration at once and the wider detector is too noisy without it.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts; this plan changes the multiply-add PostToolUse hook. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Project started:** 2026-10-12T16:27:56.610+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/hooks/fn_length_lib.py` — shared by the function-length and multiply-add hooks: the edit parser (`Replacement`, `AppliedRustEdits`, `IgnoredEdit`, `applied_patch_rust_files`, `applied_rust_edits`, `written_lines`, lines 66–222), the Rust scanner (`tokenize_rust`, `pair_delimiters`), the Cargo lint reader
  - `scripts/hooks/mul_add_lib.py` — the detector: lint scope (`suboptimal_flops_scope`), `_visible`, `_functions`, the lexed sum (`ArithmeticTokens`, line 223), name evidence (`NameArithmeticEvidence`, `BareNameEvidence`, lines 312–359), the text builders (`_compact`, `_receiver`, `_negated`), `_find_in_body` (lines 438–536), the finding type `MulAddFinding` (line 50) and `mul_add_findings` with its written-line filter (lines 539–600)
  - `scripts/hooks/post-tool-use-mul-add.py` — the hook entry for Claude `Edit|MultiEdit|Write` and Codex `apply_patch` payloads; builds the block and appends `blocks.jsonl`
  - `scripts/hooks/test_mul_add.py` — its tests (`MulAddHookTests`: a temporary workspace that denies `nursery`, `write_source`, `run_hook`, `decision`, `edit_payload`, `assert_passes`, `patch`)
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

### Phase 1 — Report only on lines the edit wrote  · status: done

#### As-built

The multiply-add hook blocks only for an expression on a line the edit wrote. `applied_rust_edits` records, for each Rust file, the `(old, new)` text of every replacement the edit applied: one pair for `Edit`, one per entry for `MultiEdit`, `("", content)` for `Write`, and for `apply_patch` one pair per added file and per update chunk, carried to the new path by `*** Move to:`. `written_lines` compares `old` and `new` line by line, drops the leading and trailing lines they share, and maps the remaining non-empty lines of `new` onto every occurrence of `new` in the file; when `new` does not occur, it marks every line whose stripped text equals a line of `new` that is not a line of `old`. The detector keeps a finding when any line of its expression was written.

```python
type Replacement = tuple[str, str]  # the (old, new) text of one applied replacement
AppliedRustEdits.replacements: tuple[tuple[Replacement, ...], ...]  # parallel to files
def written_lines(source: str, replacements: tuple[Replacement, ...]) -> frozenset[int]
def float_mul_add_findings(rs_file, written: frozenset[int] | None = None)  # None keeps every finding
```

**Files:**
- `scripts/hooks/fn_length_lib.py` — `Replacement`, `AppliedRustEdits.replacements`, the patch chunk reader beside `applied_patch_rust_files`, `written_lines`
- `scripts/hooks/mul_add_lib.py` — the `written` parameter and the line filter
- `scripts/hooks/post-tool-use-mul-add.py` — the operator text check, then `written_lines`, then the detector, imported only when a line was written
- `scripts/hooks/test_mul_add.py` — `run_hook` sends a `Write` of the whole file by default; `edit_payload`; the written-line cases

**Binds later work:** the wider detector keeps `replacements`, `written_lines`, the `written` parameter and the `Write` default. The line filter counts an expression's lines from `expr`, the raw source slice, so a detector that collapses whitespace in `expr` takes the span from source positions. The live check's untouched-line step relies on a context line not counting as written.

**Gotchas:** a line ends at `"\n"` alone, and the patch reader folds `"\r\n"` to `"\n"` first; `splitlines()` splits on U+2028 and form feed inside a Rust string. Unchanged lines between two changed parts of one replacement count as written (accepted). Line numbers come from counting `"\n"` between occurrences.

**Ruled out:** comparing `old` and `new` by characters (an added line shares its indentation with its neighbour, which then counts as written); a per-character newline index of the whole file (43 ms on a large file before the cheap text check).

### Phase 2 — Report every likely multiply-add  · status: done

#### As-built

The detector reports what looks like a multiply-add unless the text disproves it. `mul_add_findings` scans each function body `_functions` returns, with comments and strings blanked, as an `ArithmeticTokens` lexed sum. A candidate is a product chain that is a direct operand of binary `+` or `-`, or the whole right side of `+=` or `-=`; in a run of `*`, `/`, `%` at one nesting depth the last operator is `*`. A product in its own parentheses is the same candidate. One finding per sum: when both sides are products, the left one is the receiver. Sums inside macro invocation arguments are candidates; `macro_rules!` definitions are not.

Evidence is read from the text of the whole sum:

- **Float:** a float literal token (`1e-3` is one literal) or a match of `_FLOAT_EVIDENCE`. It keeps a sum that also holds integer evidence.
- **Integer**, which skips a sum with no float evidence: an integer literal that is a factor or a term, outside `[...]`, call arguments and tuple fields; a match of `_INTEGER_EVIDENCE` (an integer cast, `.len()`, `.count()`); or the sum being the whole content of an index.
- **Vector**, which skips the sum whatever else it holds: a match of `VECTOR`, or a bare name that `BareNameEvidence.disproves`. A dotted `VECTOR` match followed by `()` and a scalar member (`_SCALAR_RESULT`: `.x`, `.y`, `.z`, `.w`, `.width`, `.height`, `.depth`, `.length`, `.0`, `.1`) is not evidence.

`BareNameEvidence` gives a bare name the evidence of its last `let` before the sum in the same function, an unannotated `let` included; with no such `let`, its parameter annotation; with neither, a file `const` or `static` typed other than `f32` or `f64`. A `let` inside a nested function never counts for its parent. `UNKNOWN` and `FLOAT` disprove nothing; `NONFLOAT` and `VECTOR` skip the sum.

Kept exemptions: lint scope and the no_std root check, `allow`/`expect` attributes, const items, `const fn`, const blocks, static initializers (`static mut` and block-valued ones too), a sum that is a `.sqrt()` receiver, and an unsuffixed literal receiver. A `'static` lifetime exempts nothing. The same-file field proof and the pattern-binding, destructuring and let-else exclusions are gone, so a name of unknown type no longer silences a report.

```python
class MulAddFinding:  # .line, .expr, .rewrite; built from (line, a, b, c, expr, rewrite)
class NameArithmeticEvidence(Enum):  # UNKNOWN, FLOAT, NONFLOAT, VECTOR
class BareNameEvidence:
    def disproves(self, name: str, at: int) -> bool
def mul_add_findings(
    rs_file: os.PathLike[str] | str, written: frozenset[int] | None = None,
) -> tuple[SuboptimalFlopsLintScope, list[MulAddFinding]]  # written=None keeps every line
```

`rewrite` is clippy's form: `a.mul_add(b, c)`; `c - a * b` gives `a.mul_add(-b, c)`; `a * b - c` gives `a.mul_add(b, -c)`; `x += a * b` gives `x = a.mul_add(b, x)`; `a * b * c + d` gives `(a * b).mul_add(c, d)`. `_receiver` parenthesizes a cast, a unary or a binary receiver and keeps a grouped chain as written (`((a + b) * (c + d)).mul_add(e, f)`); `_negated` parenthesizes anything but a single name, literal, call or field chain. A call with generic arguments keeps its callee whole. `expr` and `rewrite` pass through `_compact`: each run of whitespace outside string and char literals becomes one space, and comments are left out.

The hook entry prints one description per finding:

`<shown>:<line>: if <expr> is f32 or f64 arithmetic, write <rewrite> (clippy::suboptimal_flops)`

`reason` joins the descriptions with `"; "` and ends `". Leave integer and vector arithmetic as it is. The edit was applied."`; `additionalContext` joins them with newlines; `systemMessage` is `mul_add: <file> has <N> possible multiply-add expression(s)`, or `mul_add: <N> possible multiply-add expression(s) in <files>` for several files.

Measured with `python3 ~/.local/state/mul-add-hook/reach-2026-10-12/gate.py scripts/hooks`: 160 of 168 recorded warnings reported, 7 of 88 failed clippy steps left (0.69 per 100), 122 reports in 36 files of lint-clean hana main, in about 11 s.

**Files:**
- `scripts/hooks/mul_add_lib.py` — the lexed sum (`ArithmeticTokens`), the evidence patterns, `NameArithmeticEvidence` and `BareNameEvidence`, the text builders (`_compact`, `_receiver`, `_negated`), `_find_in_body`, `MulAddFinding`, and `mul_add_findings` with the written-line filter
- `scripts/hooks/post-tool-use-mul-add.py` — the conditional message and the block log record
- `scripts/hooks/test_mul_add.py` — 130 tests: one per rejected class in "Measured cause", the evidence rules, the kept exemptions, the exact message fields, the written-line cases, and ten regression cases

**Binds later work:** the live check expects the description text above, word for word. The re-measure reads `expression` in `blocks.jsonl`, which holds the collapsed `expr` from this phase on, and compares a recorded warning through `mul_add_findings` on its stub, as `gate.py` does. The eight recorded warnings still unreported are all skipped by `VECTOR`: a `.min(` or `.max(` call with an argument (three), a `.left` member (three), a `.size()` call (one), a `Transform` type token inside a generic callee (one).

**Gotchas:** block scopes inside one function are not modelled: a name's evidence is its last `let` before the sum anywhere in that function. The line filter takes an expression's last line from its source span, because `expr` no longer holds the newlines. Keeping a generic callee whole exposes its type tokens to `VECTOR`. The reports left on lint-clean code are sums the text cannot disprove: vectors, points, offsets and colors held in bare names with no annotation or constructor; integer arithmetic with no integer literal or cast in the sum; sums over references and scalar accessors such as waveform samples and layout or font metrics.

**Ruled out:** narrowing `VECTOR` for `.min(`, `.max(`, `.left` and `.size()` now (the gates pass; the re-measure decides whether it is needed); importing the measured prototype (the rule is built on the shared scanner).

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

**Constraints from prior phases:** Phase 2 renamed the entry to `mul_add_findings` and set the message text in Spec step 2 above. Phase 1 made the hook report only on written lines, which step 3 checks. The unit director ran the detector alone on step 1's crate at Phase 2's checkpoint: step 2's line gives exactly `top + metrics.button.height * 0.5` with the rewrite `metrics.button.height.mul_add(0.5, top)`, and an edit that writes only the `let top` line gives no finding. The hook reads text and never compiles, so the scratch function need not build. A `blocks.jsonl` record holds `at` (UTC), `agent`, `tool`, `cwd`, `file`, `line` and `expression`.

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

**Constraints from prior phases:** T_reach is in Phase 3's As-built. Phase 2 changed the block's wording, so match blocks to warnings by `file` and `expression` in `blocks.jsonl`, never by message text. From Phase 2 on, `expression` is the detector's `expr`: the sum's source text with each run of whitespace outside string and char literals collapsed to one space and comments left out; a record from before T_reach holds the raw source slice. To compare a clippy warning with a record, run `mul_add_lib.mul_add_findings(path)` on the warning's stub as `gate.py` does (it returns the lint scope and a list of findings with `line`, `expr` and `rewrite`) and compare `expr`. On Phase 2's checkpoint `gate.py` reports 160 of the 168 recorded warnings; the `VECTOR` pattern in `mul_add_lib.py` skips the other eight (a `.min(` or `.max(` call with an argument, three; a `.left` member, three; a `.size()` call, one; a `Transform` type token, one).

**Acceptance gate:** both limits reported with PASS or FAIL from the 96-hour window; every number traces to a query the report names.

## Out of scope

- Running `cargo clippy --fix` inside the lint step. It would be exact and needs no agent turn, but it rewrites files during a lint step and lives in `scripts/delegate/verify.sh`, which this plan's author left to its owning units. Raise it with the showrunner if Phase 4 fails.
- Cross-file type inference.
