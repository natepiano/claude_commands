# Repository copies

`scripts/copies/copies.py` owns repository copies for builds.
The guard permits only original worktrees, the external upstream clone, and this implementation.

`CommittedSource` selects a revision.
`WorktreeSource` applies staged, unstaged, and untracked changes to a revision.
`StagedSource` selects only index content.
`NoMerge` keeps that source.
`MergeRevision` adds a merge after source preparation.
The CLI converts its flags into these policies.

Staged copies use a private source index and a synthetic commit without changing source refs.
Every gate owns one temporary checkout under TMPDIR.
A Cargo gate retains its target cache in a separate standing folder.
The checkout links `target` to that cache.
The cache record restores file times when tracked blob identities match.
Rust release trials keep Cargo mend output under `target/mend-target` in the retained cache.
A damaged private checkout index gets one recovery attempt.
The caller holds the checkout lease until its command and result records finish.

`RemoteMirror` records the owner, repository, host, mirror path, and holder session.
The remote registry uses `.remote-mirrors` below the configured copies root.
Mac, Linux, and AWS copies use the same kind.
The Mac path uses the validated `MacMirrorRoot` setting.

The mirror lease covers the claim read and the copy-record lock.
The claim reader releases each admission lock before it returns its snapshot.
`mirror_metadata_locks` holds only the copy-record lock.
An outside mirror lease protects task parent removal.
After acquisition, it rejects a missing or replaced lease path as busy.
It deletes the outside lease file after parent removal, while both leases remain held.
The existing sibling lease still protects a live lease from the old code.
The receiver keeps `.copy-record.json`, `.copy-session`, and `.mac-test-mirror` during rsync deletion.
Remote commands inherit the mirror lease.
Process inspection also keeps mirrors with a working directory, a mapped file, or a file open for writing.
A file open only for reading does not make a mirror busy.
Unreadable process state prevents removal.

The Mac floor sweep acquires idle mirror leases before it reads claims.
It retains these leases through removal.
It reads claims again before each removal group outside the acquired leases.
The Linux floor sweep holds mirror leases while it checks and removes idle output.
Admission takes available slot leases before it selects a claim.
It releases unused slot leases after claim selection.
It skips busy mirrors and preserves their records.
Cancellation closes its acquired descriptors.
The lock files retain their inodes.

`copies.py remove` takes the owner from the caller's staff row and includes that owner's remote mirrors within the repository scope.
Retirement and production cleanup use the same removal path before deleting source trees or production state.
Busy removal names the holder session and mirror path.
A host connection failure marks the local mirror record for deferred removal, so retirement can finish.
Removal tries an unreachable host only once per call.
`register_remote` retries deferred removals after the next successful copy or remote clone on that host.
Registration replaces the current path's record first, so reuse clears its removal mark.
A failed retry keeps the mark and prints one anomaly without failing the copy.
A successful retry removes the record.
Removal requires a zero status marker from the remote command before it deletes the local record.
Authentication failures and remote removal refusals still raise `RemoteMirrorKept` and prevent retirement.
A kept local tree prints one line, and retirement continues.
The sweep removes the local tree later.
Another owner's records remain.

Local mirror rows display the machine name while their records retain `localhost`.
List and sweep reads unregister local mirrors whose checkout is gone.
A lease with `create=False` creates no parent folder.
Clone metadata staging uses a temporary context inside the checkout and disappears after a failure.
