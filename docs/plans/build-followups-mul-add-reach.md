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
  - Times carry their zone: this plan states EDT (America/New_York) in its remaining work and PDT in its finished records; buildlog stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 3 | Phase 2's checkpoint in the live configuration | `git -C ~/.claude merge-base --is-ancestor <Phase 2 checkpoint> HEAD` succeeds |
| G2 | Phase 4 | 96 hours after T_reach (2026-10-12 14:13:42 EDT) | the clock reads 2026-10-16 14:13:42 EDT or later |
| G3 | Phases 6, 8 | Phase 5's checkpoint in the live configuration | `git -C ~/.claude merge-base --is-ancestor <Phase 5 checkpoint> HEAD` succeeds; T_lint is the committer time of the first `~/.claude` commit that contains it |
| G4 | Phase 8 | 24 hours after T_lint | the clock reads T_lint + 24 h or later, in EDT; the notifier instance `mul-add-remeasure` wakes this session then |
| G5 | Phase 6 | the first live rewrite | a hana `verify.sh lint` call that started after T_lint ran the rewrite step and its final clippy verdict passed; the notifier instance `mul-add-first-fire` wakes this session then |
| G6 | Phase 10 | Phase 9's checkpoint in the live configuration | `git -C ~/.claude merge-base --is-ancestor <Phase 9 checkpoint> HEAD` succeeds; T_rounds is the committer time of the first `~/.claude` commit that contains it |
| G7 | Phase 10 | 24 hours after T_rounds | the clock reads T_rounds + 24 h or later, in EDT; the notifier instance `mul-add-rounds-remeasure` wakes this session then |

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

### Phase 3 — Live check  · status: done

#### As-built

The wider detector is confirmed live on natedev for both agents. T_reach, the time it reached the live hooks, is 2026-10-12 11:13:42 PDT (2026-10-12T18:13:42Z): the committer time of the merge that brought the every-likely-multiply-add checkpoint into `~/.claude`.

- In a scratch workspace that denies `nursery`, a Codex `apply_patch` and a Claude `Edit` that each add `let centre_row = top + metrics.button.height * 0.5;` receive one block reading `<file>:3: if top + metrics.button.height * 0.5 is f32 or f64 arithmetic, write metrics.button.height.mul_add(0.5, top) (clippy::suboptimal_flops). Leave integer and vector arithmetic as it is. The edit was applied.`
- A second edit that changes only the `let top` line receives no block, for both agents.
- `python3 scripts/hooks/codex_hooks.py check` exits 0 and reports both Codex handlers trusted.
- The live `~/.local/state/mul-add-hook/blocks.jsonl` holds one record per block: `agent` `claude`, `tool` `Edit`, at 2026-10-12T18:18:04Z; `agent` `codex`, `tool` `apply_patch`, at 2026-10-12T18:21:28Z; both with `line` 3 and `expression` `top + metrics.button.height * 0.5`.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — the "Detector reach, live check" paragraph, first under "Measured outcome"

**Binds later work:** the re-measure's window runs from T_reach to 2026-10-16 11:13:42 PDT. The two records above sit inside it and come from scratch crates (`file` under `live_check_claude/` and `live_check_codex/`), so they are no part of any block count. The re-measure's table and paragraph follow the live-check paragraph.

**Gotchas:** the hook reads text and never compiles, so the scratch function need not build. The block log writes one record per reported expression, each with its own `at`; the records of one hook response share no id.

### Phase 4 — Re-measure  · status: done

#### As-built

The 96-hour window after T_reach, 2026-10-12 14:13:42 EDT (2026-10-12T18:13:42Z) ≤ start < 2026-10-16 14:13:42 EDT (2026-10-16T18:13:42Z), is measured, and both limits fail:

- 850 hana clippy steps, 295 failed; 70 carry `clippy::suboptimal_flops`: 8.235 per 100 against the 1.979 limit (**FAIL**).
- 16 sole-cause failures cost 1.104 seat-hours a day against the 0.100 limit (**FAIL**).
- Of 227 warnings, 1 matches an earlier block for the same file and expression. 208 are unknown: the detector reports their stub, yet the hook left no record. These occur in 66 of the 70 affected steps and 15 of the 16 sole-cause failures, the largest cause for both FAILs. 18 are skipped by a rule on the stub (9 `VECTOR`, 5 `.powi(2)`, 2 `.sqrt()` receiver, 1 macro, 1 truncated excerpt).
- 39 block records, all Codex; the two live-check controls are excluded. All 39 are checked against the next commit touching their file: at least 17 matching rewrites, 2 kept, 8 unverified, 12 with no next commit in the window.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — the "Detector reach, re-measure" table and paragraphs under "Measured outcome", with the complete SQL; T_reach and the earlier window restated in EDT with UTC
- `~/.local/state/mul-add-hook/reach-2026-10-16/` — `measure.py`, `commits.py`, `audit.py` and their JSON outputs, outside the repository; each reads and writes beside itself

**Gotchas:** the buildlog index names the seat column `worker`. Clippy's multiply-add header comes in two wordings ("may be calculated" and "can be calculated"); a parser that knows one misses diagnostics.

**Ruled out:** sampling 30 blocks — all 39 were checked.

### Phase 5 — Rewrite multiply-adds in the lint step  · status: done

#### As-built

- `verify.sh lint` judges first. When the workspace clippy judgment (`--workspace --all-targets --all-features -- -D warnings`) fails and its captured output contains `#suboptimal_flops`, `invoke_clippy_mul_add_json` (`scripts/lint/invoke.sh`) replays the same call with `--message-format=json` through `run`. A replay exit of 0 or 101 is accepted; any other keeps the first verdict.
- `scripts/lint/apply_mul_add.py --workspace-root <root> <json>` applies only `clippy::suboptimal_flops` `MachineApplicable` suggestions from `message.spans` and `children[].spans`, inside the workspace root (symlinks resolved), duplicates once, overlaps skipped and counted, bytes edited last span first, every file checked before any write. It requires Cargo's `build-finished` record. Exit 0 rewrote, 3 (`NOTHING_APPLIED`) nothing to rewrite, 1 incomplete stream or I/O failure.
- It prints `verify.sh: rewrote <n> multiply-add(s) in <m> file(s)`; lint then formats `FMT_SCOPE` again, recomputes `VERDICT_KEY` and judges once more, and that verdict stands. Nothing to rewrite prints `verify.sh: clippy's multiply-add rewrite changed nothing`; a failure prints `…rewrite failed…`; both keep the first verdict. Clippy off runs nothing new and prints one `SKIPPED` line.
- `run_json_stdout` keeps cargo's JSON alone on stdout and sends the banner and stderr to stderr and the step log through a temporary FIFO and an ordinary background `tee`, joined before return (bash 3.2 constructs). Every step returns its command's own status, also when `tee` fails.
- The count line of each successful rewrite is saved as `<call_id>.log` under `MUL_ADD_REWRITE_LOG_DIR` (default `~/.local/state/mul-add-hook/lint-rewrite/`); a failed save prints a notice and never fails lint. The lint cache key covers the applier.

**Files:**
- `scripts/delegate/verify.sh` — lint rewrite branch; `WORKSPACE_ROOT` feeds Taplo and the applier
- `scripts/lint/invoke.sh` — `invoke_clippy_mul_add_json`, `run_json_stdout`
- `scripts/lint/apply_mul_add.py`, `scripts/lint/test_apply_mul_add.py` — the applier and its tests
- `scripts/delegate/test_verify_lint_scope.py` — lint-step tests with fake Cargo
- `~/.local/state/mul-add-hook/lint-rewrite/watch.py` — `first-fire <checkpoint>` and `window <checkpoint> <hours>`; `probe-2026-10-16/` holds the scratch-crate probe

**Binds later work:** the JSON replay step is marked by `--message-format=json` in its argv and shares the lint call's `call_id`; judging steps have no marker. Rewrite counts live in the saved `<call_id>.log` count lines.

**Gotchas:** under `-D warnings` the JSON replay normally exits 101. Named descriptor redirections (`{fd}>`) break the Mac's bash 3.2.

**Ruled out:** `cargo clippy --fix` limited to one lint — with `-A warnings` plus `-W`, `-D`, or `-D` and `--cap-lints=deny`, it rewrote nothing.

### Phase 6 — First live rewrite  · status: done

#### As-built

- The first passing live rewrite was hana lint call `v20261017T014232-1432240-10080`, started 2026-10-16 21:42:32 EDT (2026-10-17T01:42:32Z): judging clippy failed with `#suboptimal_flops` (101), the JSON replay (101) supplied one suggestion, the count line read `rewrote 1 multiply-add(s) in 1 file(s)`, fmt passed and the final judging clippy passed (0). The showrunner heard it at the wake.
- The fix was `crates/hana_diegetic/src/layout/element.rs:2511`; it sat uncommitted in `~/rust/widget-enhancements`, whose branch still pointed at the call's starting commit. The paragraph is in the as-built's "Lint-step rewrite" section.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — the first-fire paragraph

**Binds later work:** a rewrite lands as an uncommitted edit in the calling worktree, so no hana commit marks it. Three earlier count logs in the window (2026-10-16 20:43Z, 21:12Z, 21:42Z) rewrote without a passing verdict in their call.

### Phase 7 — Complete step logs under load  · status: done

#### As-built

- Cause: `run_json_stdout`'s stdout and stderr each had their own `tee -a` on one log; their chunks are not line-aligned, so under load a JSON record landed inside a stderr line, which the completeness test read as a missing line. No bytes were lost.
- Fix: stdout streams to the caller at once through `tee` into a temporary file, appended to the log after the stderr `tee` is joined. The log holds stderr, then stdout. Reproduction (2,048 nested calls, 64 at a time, 512 CPU workers): 28 damaged logs at load 100–270 before, 0 at load 208–382 after.
- `test_concurrent_json_and_stderr_keep_complete_lines_in_both_logs` sends 1,000 lines on each stream through the nested call and checks both logs on return; it catches the old runner often but not on every run.

**Files:**
- `scripts/lint/invoke.sh` — `run_json_stdout`
- `scripts/delegate/test_verify_lint_scope.py` — the regression test

**Ruled out:** unit-board's merge as the cause — the race is in this runner; that merge only changed timing.

### Phase 8 — Re-measure the lint-step rewrite  · status: done

#### As-built

- Window 2026-10-16 16:09:48 EDT (2026-10-16T20:09:48Z) ≤ start < 2026-10-17 16:09:48 EDT, per lint call: the verdict is the last judging clippy step of each `call_id`. Both limits **FAIL**: 5 of 124 final verdicts failed with `suboptimal_flops` (4.032 per 100, limit 1.979); one sole-cause call cost 0.448 seat-hours a day (limit 0.100). 14 JSON replays recorded 28 fixes and added 1,275.1 s of clippy time. `audit.py` PASS.
- Causes, ranked by failed verdicts: overlapping suggestions in a chained sum, whose inner term the applier skips (2, and the whole seat-time failure); macro-body suggestions, `MaybeIncorrect` (2); crates downstream of the first failing crate, never seen by the replay (1). The section is "Lint-step rewrite, re-measure" in the as-built.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — "Lint-step rewrite, re-measure"
- `~/.local/state/mul-add-hook/lint-rewrite/` — `measure.py`, `commits.py`, `audit.py` and their JSON

**Binds later work:** warning counts include repeated diagnostics (one macro expression appeared nine times in one verdict), so causes are ranked by failed verdicts.

**Ruled out:** retiring the edit hook, since both limits fail.

### Phase 9 — Rewrite until nothing is left  · status: todo

**Depends on:** Phase 8

#### Work Order

**Goal:** one lint call clears every multiply-add clippy can rewrite automatically, including the inner term of a chained sum and the crates downstream of the first crate that failed.

**Spec:**

The showrunner, 2026-10-17, after Phase 8 measured both limits failing: "Add Phase 9 (current): (1) for chained sums, the rewrite applies clippy's mul_add suggestions again until none is left (to a fixed point), so the inner a*x + b*y is rewritten too; (2) the first lint does not stop at the first failing crate, so the rewrite sees the downstream crates. Leave macro bodies alone (MaybeIncorrect)."

Phase 8's evidence: of five failed final verdicts, two kept the inner term of a chained sum the applier had skipped as overlapping (count lines "1 skipped: overlapping", "3 skipped: overlapping"), and one failed in `hana` after the first and replay runs stopped at `hana_conduit`, so the replay never reported `hana`'s warnings.

In `verify.sh lint`'s rewrite branch (`scripts/delegate/verify.sh`, after the judging clippy fails with `#suboptimal_flops`):
1. Replace the single replay and apply with rounds: each round replays clippy as JSON (`invoke_clippy_mul_add_json`, same arguments) and runs `apply_mul_add.py` on it. Stop when the applier exits 3 (`NOTHING_APPLIED`), when the replay exits other than 0 or 101, when the applier exits 1, or after **5** rounds; then format `FMT_SCOPE` once, recompute `VERDICT_KEY`, and judge once more, as today. A later round sees the file the earlier round wrote, so a skipped inner term and a crate the earlier run never reached both come up again. Edits a round already wrote stay when a later round fails; the final verdict judges them.
2. When no round applied anything, keep today's "changed nothing" line and the first verdict. When the first round applied and a later one failed, print the failure line and still format and judge.
3. Print the count line of each round that rewrote, unchanged in form; save every round's count line, one per line in order, in `<call_id>.log`. When the cap stops the rounds, print `verify.sh: stopped the multiply-add rewrite after 5 rounds`.
4. Every lint clippy run in this branch (the judging run, the replays and the final judgment) passes Cargo's `--keep-going`, so a failing crate no longer hides crates that do not depend on it.
Macro-body suggestions stay untouched: the applier's `MachineApplicable` rule already excludes them.

**Files:**
- `scripts/delegate/verify.sh` — the lint rewrite branch
- `scripts/lint/invoke.sh` — only when `--keep-going` belongs in `invoke_clippy` rather than at the call sites
- `scripts/delegate/test_verify_lint_scope.py` — lint-step tests with fake Cargo

**Workers:** 1 writer; the tests live beside the change.
- `impl` — rounds, `--keep-going`, tests

**Constraints from prior phases:**
- Phase 5: the replay step is marked by `--message-format=json` in its argv and shares the call's `call_id`; under `-D warnings` a useful replay exits 101. `apply_mul_add.py` exits 0 rewrote, 3 nothing, 1 incomplete stream or I/O failure, and requires Cargo's `build-finished` record. The count line is `verify.sh: rewrote <n> multiply-add(s) in <m> file(s)` with an optional `(<k> skipped: overlapping)`.
- Phase 7: `run_json_stdout` returns only once its log holds every line; keep bash 3.2 constructs (no `{fd}>`) and every step returning its command's own status.
- `~/.local/state/mul-add-hook/lint-rewrite/watch.py` reads count lines with a multi-line pattern; Phase 10 sums every line of a log.
- A rewrite lands uncommitted in the calling worktree.

**Acceptance gate:** fake-Cargo tests show: a chained sum whose inner term comes up in round 2 ends with a passing final verdict and two saved count lines; a downstream crate's suggestion that first appears in round 2 is rewritten; the cap stops at 5 rounds with its line; a later round's failure still formats and judges; `--keep-going` is on every clippy argv in the branch. `merge_gate.py --tests-only` passes.

### Phase 10 — Re-measure the rewrite rounds  · status: todo

**Depends on:** Phase 9
**Blocked by:** G6, G7

#### Work Order

**Goal:** the 24 hours after T_rounds are measured on Phase 4's two limits, and the as-built records PASS or FAIL for each.

**Spec:**

The showrunner, 2026-10-17: "Then Phase 10: re-measure over 24 hours against the same two limits."

The wake: at Phase 9's checkpoint, the unit director creates
`zsh ~/.claude/scripts/message/notifier.sh new mul-add-rounds-remeasure --every 15 --to session:<this session's id> --from mul-add-rounds-remeasure --command "mul-add rounds re-measure due: run phase 10" --check "python3 $HOME/.local/state/mul-add-hook/lint-rewrite/watch.py window <phase-9-checkpoint> 24"`
and removes it on the wake.

Method: copy Phase 8's `measure.py`, `commits.py` and `audit.py` from `~/.local/state/mul-add-hook/lint-rewrite/` into `~/.local/state/mul-add-hook/lint-rounds/` and run the copies with the window T_rounds ≤ start < T_rounds + 24 h, by Phase 8's per-call verdict rule. Report what Phase 8 reported, plus rounds per call (each saved count line is one round; sum the fixes of every line) and how many calls the 5-round cap stopped. Rank what remains by failed verdicts, as Phase 8 did. `audit.py` re-derives the counts independently and must PASS.

If both limits PASS, the as-built proposes retiring the edit hook; the proposal goes to the showrunner, and nothing is removed in this phase. If either FAILs, the as-built names the largest remaining cause by failed verdicts.

**Files:**
- `docs/as-built/build-followups-mul-add.md` — "Rewrite rounds, re-measure" under "Measured outcome", with the bounded SQL
- `~/.local/state/mul-add-hook/lint-rounds/` — outside the repository: the copied scripts and their JSON

**Workers:** 1 writer + 1 adversary review; nothing to test.
- `impl` — the scripts' copies, the run, the as-built section

**Constraints from prior phases:**
- Phase 4's limits: 1.979 per 100 and 0.100 seat-hours a day. Phase 8's result (4.032 and 0.448, per call) is the baseline row.
- Phase 8: the verdict is the last judging clippy step per `call_id`; JSON replay steps (`--message-format=json` in argv) are never verdicts. Warning counts include repeated diagnostics, so causes are ranked by failed verdicts.
- Count logs stay in `~/.local/state/mul-add-hook/lint-rewrite/<call_id>.log` (Phase 9 writes one line per round there).
- The build log index is UTC; state times in EDT with UTC beside them. Never write `~/.local/state/buildlog`, the block log or the hana tree.

**Acceptance gate:** `audit.py` PASS; both verdicts stated with their arithmetic; `git diff --check -- docs/as-built/build-followups-mul-add.md` passes.

## Out of scope

- Cross-file type inference.
