# UX review

<UXReview>
Runs when the phase changes what users see, in the app or in any example; a
phase that changes nothing on screen skips it and says so in one line. Guide:
the UX guide the production doc names, else the one the repository's
instructions name; with none, skip and say so in one line.

1. After smoke has a build, take a shot of each changed view and state on the
   run's port at normal size — the same shots the checkpoint notice sends
   (<ProductionUnit/> item 3). Never view them yourself. In Hana, shoot with
   `/hana_shot`: a stored view (`crates/hana/brp_views.toml`) or an ad hoc
   `--target`, every view in one call, never a camera worked out by hand. A changed view the file
   lacks gets `views add`; when the scene changed, run `views check` and commit
   the file with the phase.
2. Open an activity; the
   label is exact, because `review-trial` counts it:
   `progress_history.py start-activity --session-dir "${SESSION_DIR}" --label "UX review" --activity "<what the shots show>"`
3. Run one design check, so the shots never enter your context. Use this
   prompt for the Claude helper:

   > Read `~/.claude/commands/ux_eval.md` and follow it for these shots:
   > <paths>. Guide: <path>. Scale: <shot pixels per logical pixel>. Context:
   > <unit> phase <N> — <what changed, every state the shots must show, and
   > each shot's window size in logical pixels>. Also judge by the three gods
   > (`<ThreeGods/>` in `~/.claude/docs/decision_criteria.md`) and these
   > production rules: <the production doc's rules that concern looks>. You
   > only review: edit nothing. Return only the verdict and the table.

   Spawn a fresh Claude Agent helper in the background with that prompt.
   Read its verdict once and stop it with TaskStop. This helper is the one
   design check.

   Save each canonical
   candidate it lists, without viewing it, into a slot still empty:
   `mkdir -p <guide>/examples/<stem> && magick <shot> -crop <crop> +repage <guide>/examples/<stem>/<slot>.png`.
   Saving it approves it for use now. Add one line to
   `<guide>/examples/pending.md`, which holds it for the user's final approval:
   `- <stem>/<slot>.png — <why it is clear> — <unit>, <date>`. Close the activity with
   `finish-activity --session-dir "${SESSION_DIR}" --result <pass|N defects>`.
4. Open each defect row as a finding — blocker, because the showrunner's merge
   check holds a checkpoint on any visible defect. A `no rule` row is still a
   finding:
   `findings.py open --severity blocker --lens ux --caught-by worker --title <defect> --file <shot path> --detail "<rule>: <fix>"`
   Then obey `findings.py gate` exactly as <Synthesize/> step 5 does, the same
   repair round under <FixDispatch/>, except that `converged` continues to
   <PhaseEnd/> in loop or verbose, or <RunPhaseReview/> in `single`.
5. Re-entered after a `ux` repair that changed the screen, this step replaces
   1–4: re-shoot and re-judge only the rows raised, under its own `UX review`
   activity, given just those rows and their shots. Run step 3's design-check
   procedure with the fresh Claude helper;
   then close the activity and record
   each with `findings.py verdict`. A `still_open` row returns to step 4's
   gate. Then continue to <PhaseEnd/> in loop or verbose, or
   <RunPhaseReview/> in `single`.
6. **The notice's verdict.** In a production unit, the checkpoint notice's
   `design check:` line (<ProductionUnit/> item 3) uses step 3's selected
   verdict on exactly the shots the notice sends, from a build of the
   checkpoint's code. Step 5's partial re-judge does not count, nor does a
   verdict from before any later change to what users see (a repair, a merge
   of the merge branch). In those cases, before the notice, re-shoot the whole
   set and judge it once more as in steps 1–3. Send on a `pass`, or with each
   defect moved to a follow-up phase (item 7's landing rule); a defect left in
   the phase goes back to step 4. The showrunner merges on that line and runs
   no check of its own (user rule 2026-10-01).
</UXReview>

<RunApplicationSmokeTest>
Read the diff. If no repository binary reaches its changes, record
`not applicable — <reason>` and continue to <UXReview/>. Otherwise select the
target from Delegation Context Run/Smoke, Acceptance gate, repository
instructions, then manifest. A build, test binary, static example build, or
worker report is not a smoke test.

Launch the real product from `${WORKING_DIR}` with useful logging/backtraces,
exercise the changed runtime behavior, observe stability, close cleanly, and
record command/action/result. Never run a `check` or `test` pass first to prove
it builds. Startup alone suffices only when no changed
behavior can be invoked. A pass continues to <UXReview/>.

A launch that needs a built binary — a smoke or shot helper's, or a launcher
that takes a path — builds it with this one line, then starts the
`binary: <path>` it prints:

`bash ~/.claude/scripts/direct/verify.sh --session-dir "${SESSION_DIR}" build [<package>] [--bin <name> | --example <name>]`

It waits for a build hold, takes the cargo token and memory admission, and
records the build, and its allow rule needs no approval. You and every helper
never run raw cargo (`cargo build`, `cargo run`): a helper's raw `cargo build`
needed the user's approval by hand (2026-10-23). Copy the line into every smoke
or shot helper prompt.

A panic, fatal log, unexpected exit, or wrong behavior is a blocker: route it
through <Synthesize/>, then repeat review, synthesis, and smoke before later
gates. If this environment cannot perform the interaction or locate an
applicable executable, close the process, record `deferred — <exact human action
and limitation>`, and continue to <UXReview/> without waiting. Deferred smoke
allows the checkpoint but is batched at <FinalGate/> and reported by
<RunSummary/>.
</RunApplicationSmokeTest>
