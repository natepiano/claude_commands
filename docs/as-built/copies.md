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
The standing checkout retains its target folder and unchanged file times.
A damaged private checkout index gets one recovery attempt.
The caller holds the checkout lease until its command and result records finish.

`RemoteMirror` records the owner, repository, host, mirror path, and holder session.
The remote registry uses `.remote-mirrors` below the configured copies root.
Mac, Linux, and AWS copies use the same kind.
The Mac path uses the validated `MacMirrorRoot` setting.

The mirror lease precedes the floor claim lock.
The floor claim lock precedes the copy-record lock.
The receiver keeps `.copy-record.json`, `.copy-session`, and `.mac-test-mirror` during rsync deletion.
Remote commands inherit the mirror lease.
Process inspection also keeps mirrors used by surviving children.
Unreadable process state prevents removal.

The Mac floor sweep acquires idle mirror leases before the claim lock.
The Linux floor sweep holds mirror leases while it checks and removes idle output.
Admission takes available slot leases before it selects a claim.
It releases unused slot leases after claim selection.
It skips busy mirrors and preserves their records.
Cancellation closes its acquired descriptors.
The lock files retain their inodes.

`copies.py remove` takes the owner from the caller's staff row and includes that owner's remote mirrors within the repository scope.
Retirement and production cleanup use the same removal path before deleting source trees or production state.
Busy removal names the holder session and mirror path.
A kept remote mirror raises `RemoteMirrorKept`, and retire refuses before it removes the worktree, because no sweep removes a mirror. A kept local tree prints one line, the retire continues, and the sweep takes the tree later.
Another owner's records remain.
