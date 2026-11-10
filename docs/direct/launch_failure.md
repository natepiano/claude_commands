# Worker launch failure

<WorkerLaunchFailure>
Check the local launcher, model, configuration, and publication fault before treating a provider message as the cause.
A real `hit your usage limit` launch or turn failure turns on <NoWorkerMode/> in `commands/unit/direct.md`.
`assign.py` ends a failed launch's run when no pass completed, no other worker exists, and the director holds only that run.
Each recorder slot counts as one worker, even when its pass records no worker name.
For Claude, this slot must record a new `pass_started` event during the launch.
The launcher keeps the phase reservation and the launch exit code.
After this end, omit `--session-dir` to start a new run on retry.
An explicit `--session-dir` retains the existing retry behavior for that folder.
For herdr, this end applies only to exit 2. Exits 3 through 8 retain the run.

Inspect `worker.py list` and the named pane.
Keep incomplete, unpublished, busy, and unknown workers unchanged.
A failed fresh start prints `publication failed: ...` and retains its pane without a saved pass.
Rerun the original launcher command; it publishes once from that pane.
For a published worker, resume the same pass through its saved launcher command.
</WorkerLaunchFailure>
