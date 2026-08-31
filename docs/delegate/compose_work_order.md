# Compose the work order

Read at the point of use from `/plan:delegate`. Defines `<ComposeWorkOrder/>` in
full.

**Read when:** starting a phase, before any prompt is written. Read it every
time — a work order composed from memory is the most common source of a phase
that builds the wrong thing.

1. For a phased plan, validate the target Work Order before reading any of its
   fields:

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.work_order \
     --repository-root "${WORKING_DIR}" validate --document "${PLAN_DOC}" \
     --phase <target-phase>
   ```

   The tagged output is authoritative for complete Goal/Spec/Files structure
   and lexical paths. Work Orders do not declare reservations; the edit hook
   claims exact paths on first touch.

   Then scan the target Work Order for `**Pending decision:**`.
   Verify cited code still matches the block. **Re-test the block against
   <DecisionRouting/> before presenting it** — a block is a claim that a decision
   is the user's, not proof of it, and the pass that wrote it may have been wrong
   or may have been overtaken by later phases. Resolve a packaging-only block
   yourself under <DecisionEconomy/>: edit the resolution in, delete the block,
   state the call in one line, and do not stop. If it is genuinely the user's,
   present it, apply
   <ExplainOnDemand/> when needed, edit the resolution into Spec/Files/gate, and
   remove the block before continuing. A resolution introduces behavior nothing
   else audits — `/plan:phase_review`'s `<StateAndConsequenceAudit/>` inspects
   only what a phase already shipped — so run that audit against the resolution
   here and state its destination and owner alongside it. An in-repository
   destination is the Spec/Files/gate edit already being made. A destination in
   another repository goes to the next-items file derived in step 5, and only
   with the user's approval; never append to it automatically.
   After editing a resolution into Spec, Files, or the acceptance gate, rerun
   the shared validation above before continuing. A validation failure blocks
   dispatch.
2. Parse the complete bounded-auto phrase before a standalone phase selector.
   Reject `single` plus `verbose`, auto without `verbose`, non-positive N, or an
   invalid range. Set `MODE=single` for `single` or non-phased work,
   `MODE=verbose` for a phased verbose invocation, otherwise `MODE=loop`.
   Infer absent work from the conversation.
3. Run `progress_history.py start-run --session-dir "${SESSION_DIR}"
   --working-dir "${WORKING_DIR}" [--plan-doc <path>]`. It is idempotent; stop
   if exact main-agent identity cannot be detected. The recorder alone owns the
   project clock: for a supplied plan it validates and uses `Project started`,
   or derives and persists it from the plan's oldest Git commit or run start;
   for ad hoc work it reuses the latest plan-backed clock for the exact working
   directory and branch, or starts at this run when none exists. Never
   calculate, pass, edit, or correct a project timestamp in the agent.
4. A delegate-ready plan has `## Delegation Context` and a target
   `#### Work Order` per `~/.claude/docs/delegate_plan_format.md`. `verbose`
   requires one.
5. For a phased plan, derive `${NEXT_ITEMS_PATH}` beside `${PLAN_DOC}`. Lowercase
   its filename stem, replace each run of non-alphanumeric characters with one
   hyphen, trim leading/trailing hyphens, and append `-next.md`. Stop if the
   normalized stem is empty. The file need not exist.

**Delegate-ready fast path:** do not research the codebase. Assemble
`${SESSION_DIR}/implementation_prompt.md` under <WritePromptContract/>:

- Project Context: Delegation Context except **Style**, plus Constraints from
  prior phases.
- Work Specification: Goal, Spec, Files, Seats verbatim, plus command-line
  amendments. Seats is what <LaunchImplementation/> partitions and opens from;
  when a plan predates the field, the launch step decides and says so.
- Capture **Style** only as `${STYLE_GATE_CONFIG}` for
  <RunProjectStyleReview/>.
- Verification: translate Build/Test/Lint/Run/Smoke and Acceptance gate into
  <VerificationContract/> lines. Convert old raw Cargo/full-clippy entries to
  scoped `verify.sh`; the main agent retains live smoke ownership.

Do not open code to fill a plan gap. Name the gap and let review catch its
effect. Mark the dispatch as assembled from the Work Order without research.

**Fallback:** research only enough to write the same prompt structure. Quote an
applicable plan section verbatim, or compose a complete spec from the
conversation with files, behavior, APIs, edges, and constraints. Point to files
instead of copying their contents. Set `${STYLE_GATE_CONFIG}` to `rust` for Rust
work, otherwise `none`; do not load style. Derive scoped verification per
<VerificationContract/>.

If an initial verbose invocation contains a bounded-auto control, resolve
`AUTO_WINDOW` and run <AutoWindowBatchBriefing/> before
<CoordinateDelegatedPhaseReservation/>. Otherwise
follow <AuthorizationContract/>.