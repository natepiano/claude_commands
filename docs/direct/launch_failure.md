# Worker launch failure

<WorkerLaunchFailure>
`assign.py` ends a failed launch's run when no pass completed, no other worker exists, and the director holds only that run.
Each recorder slot counts as one worker, even when its pass records no worker name.
For Claude, this slot must record a new `pass_started` event during the launch.
The launcher keeps the phase reservation and the launch exit code.
After this end, omit `--session-dir` to start a new run on retry.
An explicit `--session-dir` retains the existing retry behavior for that folder.
For herdr, this end applies only to exit 2. Exits 3 through 8 retain the run.

For `herdr`, inspect `worker.py list` and the named pane.
Keep incomplete, unpublished, busy, and unknown workers unchanged.
A failed fresh start prints `publication failed: ...` and retains its pane without a saved pass.
Rerun the original launcher command; it publishes once from that pane.
For a published worker, resume the same pass through its saved launcher command.
The following recovery rules apply only when the existing run's pin is `mesh`.
A dispatch that dies in seconds never reached the provider, and a provider
message it prints is a message some earlier call received. Workers launched
together that fail within seconds of each other carrying **byte-identical text,
the same retry-at timestamp included, are a local fault**: separate API calls do
not produce identical text. Confirm it against
`${SESSION_DIR}/mesh_server.log` — a launch that got out of this machine wrote
to it, and one that added no line was answered by the session's own
`codex app-server` from what it had cached. Never report workers as
unavailable, or a usage limit as reached, on the error text alone.

`codex exec` cannot test this and must not be used to. It opens its own process
against the API, where a dispatch attaches a thread to the long-lived server
recorded in `${SESSION_DIR}/mesh_server.json` — a different path that can fail
while `codex exec` answers. That disagreement is the wedged server's signature,
not evidence workers are back. The only valid probe is the dispatch itself.

The launcher already tried. On a fast failure with no work done, or a stale Codex
sign-in (`access token could not be refreshed`) even after the thread exists, `codex_mesh.py`
abandons the inherited server, starts one of its own and runs the worker again,
printing `retrying on a new app-server` to the worker's log. So an error that
reaches you has usually already been tested against a clean server and is real.
Read the log for that line before doing anything by hand: present, the retry
happened and the failure survived it; absent, the launcher held back — the worker
had already done work, or the failure took too long to be local — and the
paragraphs above are yours to apply.

A new Codex sign-in moves the run by itself. A message from `codex-sign-in` names
the server it retired; the next dispatch starts on a new one, a worker mid-turn
finishes first, and nothing is owed in reply.

Recovering by hand, in that case only. Check that no peer run claims the
recorded pid or port before signalling anything —
`grep -l '<pid>\|<port>' /tmp/claude/unit/*/mesh_server.json /tmp/claude/delegate/*/mesh_server.json` should name
only this session's file, and any second file means leave the process alone.
Then kill that pid, move `mesh_server.json` aside, and relaunch verbatim; the
next launcher starts a fresh server. Leave `mesh_roster.json` alone — its other
entries may name threads still live on a server that is fine. If the log **is**
growing and the error arrives new on each launch, the limit is real and none of
this applies.
</WorkerLaunchFailure>
