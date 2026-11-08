# Codex dispatch wait

<CodexDispatchWait>
Codex only; no timer process:

1. Empty-poll `${DISPATCH_HANDLE}` with `write_stdin`. Set `yield_time_ms` to the
   configured interval from <ProgressContract/>, capped by
   `background_terminal_max_timeout`. Do not use shell `wait`, `sleep`, a status-file loop, or a second
   terminal.
2. A result with the same `session_id` and no `exit_code` means the interval
   elapsed and the launcher remains active. Apply <EarlyReviewArm/>, then poll
   the same session again. A poll timeout composes no report.
3. An initial launch or poll result with `exit_code` means the launcher finished.
   Clear `${DISPATCH_HANDLE}`, read the call site's status/result files, and
   route to review, synthesis, repair, smoke, or the next stage immediately in
   this turn. Do not end the turn between completion and routing.
4. A user message may interrupt the poll. Answer it, retain the session handle,
   and resume this contract unless the user cancels or redirects the run. An
   instruction that steers the run is an interactive point: act on it, then
   run <ReviewPendingAddOns/>.
5. An early-launched reviewer occupies its own managed terminal under
   `REVIEW_DISPATCH_HANDLE`. Keep polling the primary dispatch session; each
   timeout is also the <EarlyReviewArm/> evaluation point. After the primary
   completes and the ready sentinel is written, poll the reviewer session under
   this same contract.
</CodexDispatchWait>
