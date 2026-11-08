# Berth decisions

<BerthDecisions>
Overlap answers, incursion resolves, orphan retirement and releases never reach
the user. Under a production, send the showrunner the overlap — holder ids,
paths, answer choices — and record the answer it picks; that answer is a
decision, not a relayed user approval. With no showrunner, pick the answer
yourself. Either way, report what was decided in one line.

An answer is `cargo-berth claim <paths> --reservation <id> --<choice> <holder>
--overlap-why "<why>"`, `<choice>` one of `before`, `after`, `defer`,
`override`, and `<id>` the asking phase's recorded reservation; it records in
one step and exits 0. `before` and `after` widen only that phase's reservation,
with the ordering edge; `defer` records the answer and takes no paths, so the
phase holds its dispatch on them. An id the ledger does not know is refused.
A claim behind a holder you already sequenced against reuses that edge and
adds the paths; only `cargo-berth sequence` still refuses a duplicate edge
(exit 2). When several holders
conflict, name every one in the same claim, one flag each: `--before` and
`--after` repeat once per holder and mix, as in `cargo-berth claim <paths>
--reservation <id> --before <A> --after <B> --overlap-why "<why>"`. `--defer`
repeats once per holder but excludes `--before`, `--after` and `--override`:
a defer takes no paths, so it cannot share a claim that widens. To defer to
one holder and order behind another, claim with `--defer` first, then claim
the paths with `--before`/`--after` when the deferred holder ends.

Every cargo-berth call is one plain command run in `${WORKING_DIR}`: no pipe,
loop, `;`, `&&`, `$(...)`, redirect or env-var prefix. cargo-berth reads
`CLAUDE_CODE_SESSION_ID` itself. Read the exit status and output from the tool
result.
</BerthDecisions>
