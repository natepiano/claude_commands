# Standing gate checkouts

`scripts/copies/copies.py` is the single place that makes gate, review, merge,
and landing checkouts.
`standing` calls `keeps_build_cache(source, base)` to select cache storage.
The function runs `git cat-file -e <base>:Cargo.toml`.
Every gate uses a private temporary checkout under TMPDIR.
A root Cargo manifest selects a retained cache slot.
Other commits keep no gate cache, including commits with only nested Cargo manifests.

## Configuration, place, and names

`config/copies.conf` owns the settings. The existing
`scripts.lint.config_values.config_values` reader reads its key-value settings
(section headings are organizational); there is no separate parser. `COPIES_CONFIG` can select
another configuration file, including a private test fixture. Every setting
below is required; the implementation has no fallback values.

| Key | Shipped value | Meaning |
| --- | --- | --- |
| `root` | `~/worktrees/gates` | Absolute pool root after expanding `~`. |
| `wait_seconds` | `1` | Positive, finite interval in seconds for rechecking the second checkout while both are busy. |
| `git_timeout_seconds` | `600` | Positive, finite deadline in seconds for each Git subprocess. |
| `guard_timeout_seconds` | `10` | Positive, finite deadline in seconds for the combined repository-copy and scratch guard. |

These values come from configuration.
`owner.nightly=nightly`, `owner.style=style`, and `owner.verify=verify` supply owners when production context is unavailable.
Each owner and repository with a root Cargo manifest has two standing cache slots.

The same file holds `allow.<path>=<reason>` entries under `[allow-list]`, with
paths relative to `scripts/`. `copies.load_config().allow_list` supplies these
entries to the guard's scan. The guard does not read configuration separately.

Standing cache folders live below `<root>/<repo>/`.
`StaffOwner(row_id)` identifies a staff row. `RoleOwner(name)` identifies a fixed role without a staff row.
The records hold the row id or the role name.
A new folder uses the current staff name or the role name, followed by `-gate`.
Another owner's folder takes that name, so the new folder uses the next free numbered suffix.
The folder keeps its creation name through a staff rename.
Lookup selects the owner's records before it selects free readable names.
The caller writes the first record while it holds the claim.
A gate folder without a record is free when lookup reaches its readable name.
The caller takes that folder only after it acquires the claim.
Owner removal does not select an unrecorded folder.
A rename removes no copy and changes no copy record.
Lists show the current staff name from the row id.
`<repo>` is the repository identity from the last part of its origin URL, without `.git`.
Local origins, including relative paths and `file://` URLs, lead to the source repository's identity.
A cycle of local origins is an error.
A repository without an origin uses its main checkout's folder name.
Linked worktrees and local clones use the same identity.
`~/.claude` has no root Cargo manifest and uses fresh copies.
Its nested proof crate does not select a standing cache.
The cache root under `~/worktrees` belongs to the existing trusted direnv root.
The temporary checkout uses a separate record for direnv trust.

The CLI reads the calling session's local staff row.
A worker uses its parent unit's row id.
A caller without a staff row receives an error.
Text owners enter through one boundary as staff row ids or fixed role names.

## Fresh copies

`fresh_tree(owner)` creates one `TemporaryDirectory` under TMPDIR for every gate.
Its prefix contains the current staff name or role name and `-gate`.
It creates a `checkout` folder inside that temporary folder and yields a `GateLease`.
The temporary folder's busy lock protects preparation and caller use.
Command children inherit the lease descriptors.
The checkout does not register as an editable workspace or a scratch task.
Preparation writes `.gate-copy.json` beside `checkout/`, outside the tracked tree.
The record contains `task`, `source_path`, and the selected `source_commit`.
`copy_record.record_path(checkout)` finds this record in another process.
`nix_env.registered_copy()` requires a regular `.envrc` whose blob matches that commit in the trusted source.
A changed `.envrc` or an untrusted source receives no automatic approval.
The temporary folder removes the record when the gate ends.
The standing cache keeps its separate `.gate.json` record.
Cargo gates also hold a standing cache lease.
Other gates receive separate folders without waiting for a cache slot.

Preparation fetches the selected commit and applies the requested source and merge policies.
The context removes the temporary folder after success, a caller exception, or a preparation failure.
A failed removal raises `PostUseError` and retains the caller's exception in `caller_error`.
TMPDIR cleanup at reboot removes folders left by killed processes.
Non-Cargo gates skip the metadata and cache refresh after use.

## Reset and build output

Each gate makes a clone with its own `.git` directory.
The clone borrows objects from the last available store in the source's single-path alternates chain.
A missing store, repeated store, or alternates file with multiple lines ends the chain.
A source with no chain keeps its own objects as the borrowed store.
When the last store belongs to a registered scratch copy, the clone copies objects without an alternates file.
The existing refresh fetches the source's own objects.
Task clones and editable workspaces normally clone the source with `--shared`.
A copy made from an active temporary gate uses `--no-local` to keep its own objects after that gate ends.
They retain commits that exist only in the source, including detached commits.
The gate then works after intermediate clones or the registered scratch source are removed.
It never copies a linked worktree's `.git` pointer or changes the source's branches.
The clone stages only Git metadata inside a `TemporaryDirectory` within the locked checkout.
The code moves `.git` into the checkout before the staging context ends.
The context removes staging files after a clone failure.

The copy's `origin` URL equals the source repository's `origin` URL.
Each use refreshes `refs/remotes/origin/*` from the source's matching remote refs.
It does not map the source's local branches into those refs.
The refresh uses a local fetch and keeps shared objects.
Existing copies receive the URL and ref repair on their next use.
The copy keeps `origin/HEAD` only when the source records that ref.

Each copy also has a `source` remote with the local source path as its URL.
Its `refs/remotes/source/*` track the source's local branches.
The copy sweep uses these refs to detect commits saved in the source.
Preparation refreshes both remotes with local fetches.
Cargo gates and editable workspaces also refresh both remotes after use.
A source without an `origin` remote keeps the local source URL and local branch mapping.
These copies also keep `source`, so all copies use the same refresh rule.
Legacy adoption resolves each local origin URL from its own repository before comparison.

Each use fetches the selected commit and prepares a new detached checkout.
A Cargo checkout's `target` symlink points to its standing cache's `target` folder.
Callers continue to use `<checkout>/target`.
The standing folder retains only `target/`, `.gate.json`, and `.claude-busy` after deploy removes legacy checkout files.
The code does not remove legacy checkout files.

The cache's `.gate.json` stores each tracked path's blob identity and modification time in nanoseconds.
After preparation, one `git grep` finds tracked files that name `CARGO_MANIFEST_DIR`.
These files retain their new checkout times, including when their blob identities stay unchanged.
Cargo rebuilds their crates and the dependents instead of reusing compiled output that holds a removed checkout path.
Every other path with the same blob identity receives its saved time.
A changed blob retains its new checkout time.
Unchanged crates without these files can reuse their cached build output.
This rule does not cover a dependency macro that reads `CARGO_MANIFEST_DIR` at expansion in a caller without that text.
Cargo gates refresh source remotes and both gate records after use.

`scratch.mark_cargo_cache` marks and refreshes the standing cache's `target/`.
The floor sweep can reclaim idle build output; a later gate rebuilds it.
The cache's busy mark protects its target while a gate holds the lease.
Cache marking does not follow a caller's replacement target symlink.

## Acquisition and cleanup

A Cargo gate holds `scratch.busy_locks(cache, create=True)` throughout preparation and caller use. The mark is the existing `.claude-busy`
flock. There is no second lock implementation or central registry.

Standing acquisition tries both trees in order without waiting. If both trees are
busy, it prints one stderr line naming the trees and their recorded holder
pids, then blocks on the primary tree's flock while rechecking the second at
`wait_seconds` intervals (one second in the shipped configuration). Primary
release wakes acquisition immediately. A third simultaneous caller waits; it
never creates a third tree. Failed
preparation and failed commands release the lock.
Git runs automatic maintenance synchronously because its children inherit the busy lock.
This also releases the lock after a failed metadata refresh.

Each prepared checkout and standing cache has a `.gate.json` record:

```json
{
  "owner": "11111111-1111-4111-8111-111111111111",
  "repo": "claude_commands",
  "source": "/home/natepiano/worktrees/claude-build-followups-verify",
  "commit": "1111111111111111111111111111111111111111",
  "last_use": 1793448000.0,
  "holder_pid": 12345
}
```

The record contains the most recent commit, use time, and holder pid. An idle
tree retains its last holder pid; the lock determines whether it is busy.
Records are written atomically under the busy lock. Git's `info/exclude` ignores
`/.gate.json`, `/.claude-busy`, and `/target`, including the cache symlink.

## Commands and Python API

Run targeted tests with the source worktree's uncommitted changes and a merge, in that order:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run \
  --repo ~/worktrees/claude-build-followups-verify \
  --commit HEAD \
  --apply-uncommitted ~/worktrees/claude-build-followups-verify \
  --merge origin/builds -- \
  ~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --checkout '{tree}'
```

`run --repo R --commit C [--apply-uncommitted W] [--merge REF]
-- <cmd> ...` executes the command with the checkout as its working directory.
Every `{tree}` occurrence in an argument becomes the checkout's absolute path.
The command's exit status is returned, and its output passes through.

`--apply-uncommitted` applies staged and unstaged tracked changes plus untracked,
not ignored files. It commits on top of the selected commit with `--no-verify`
and subject `gate: uncommitted <worktree> at <short sha>`. It reads the source
without changing it. For `--merge origin/<branch>`, copies.py first fetches the
branch from the source's origin into the temporary checkout.
It then merges the fetched commit.
A stale source tracking ref cannot select an old merge. Other merge refs are fetched from the
source repository. Local origin paths resolve relative to the source repository;
remote URLs keep their original form. Source refs stay unchanged. The merge uses `--no-ff --no-edit`.
Failed application or a merge conflict exits 3,
names the affected files, and releases the lease.
A fresh copy is deleted.
The next gate prepares another temporary checkout.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py list
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py remove \
  --repo ~/worktrees/claude-build-followups-verify
```

`list` prints a tab-separated header, then numbered local trees followed by remote mirrors sorted by host and path. The header is `number owner repository host path session commit state holder_pid target`.
Local gate rows and `localhost` mirror rows display `socket.gethostname()`.
Stored mirror host names and registry keys retain `localhost`.
List and sweep reads remove registry rows whose local checkout is gone.
A lease with `create=False` does not create a parent folder.
An unreadable `.gate.json` keeps a numbered row with state `unreadable` and owner `unavailable`.
Its repository comes from the parent folder, unavailable fields use `-`, and stderr names the error. The owner column shows the current
staff name, a role name, or `unavailable`. A local row shows its commit, busy
or idle, the last holder pid and the target size. A remote row shows `held`,
`idle` or `unavailable`. A held row names the holder session only when the
lease pid equals the recorded `holder_pid`; otherwise it shows `not recorded`.
`ListStaffNames` reads `staff.local_snapshot()` at most once, when a row needs it.
Owner and session display use that same snapshot without changing folder identities.
`list_remote_leases` reads `/proc/locks` once and reads each lease file's device key.
A missing lease file means idle. Other lease-file errors or an unreadable lock table mean unavailable.
Without `/proc/locks`, macOS lists remote lease state as unavailable.
A lease holder without a staff match remains held, with its holder name unavailable.
PID equality supplies display evidence; it does not authorize cleanup.
`remove` removes the caller's trees
across all repositories. An optional `--repo R` limits removal to that repository.
The Python API accepts only a `RepositoryScope` value for `repo`.
`copies.remove_trees(owner)` uses `AllRepositories()` by default.
`copies.remove_trees(owner, SelectedRepository(name))` limits removal to one repository identity.
The API refuses raw strings and paths, including an empty string.
Removal includes standing caches, registered editable copies, and adopted legacy workers.
A workspace record saves `repository_identity` during registration.
Repository removal reads this field without using the source checkout.
Editable copies retain scratch's checks for unsaved work and active tasks.
Removal continues after a busy tree, a kept copy, or a removal failure.
Removal keeps a gate while a registered copy still borrows its Git objects.
It removes each tree that it can remove.
Every idle `.removing` tombstone in the repository scope is removable, regardless of its former owner.
A tombstone has no owner because removal has already condemned it.
After it releases the locks, it reports each failure on a separate line and exits 2.
A retry completes interrupted removal. Repeated removal succeeds when the trees are gone.

Unit retirement and run end remove the unit's idle standing caches and workspaces.
A busy local tree prints one line and does not stop retirement or run end.
A remote mirror failure refuses retirement and keeps the unit worktree for a retry.
A retirement retry repeats cleanup. The existing cleanup also removes idle trees.
`tidy_wrapped.py` removes the production's checkouts and its units' checkouts
before it deletes production state. A refusal keeps the state for a retry.

Success exits 0; invalid owners, invalid configuration, and other refused
operations exit 2. Apply and merge failures exit 3. `run` returns its command's
status. Each gate uses a new checkout and retains its selected Cargo cache.
The last-use record and cache-use stamp are refreshed.
A fresh use creates another checkout.

Script callers use the same implementation:

```python
from scripts.copies import copies

with copies.standing(owner, repo, copies.WorktreeSource(commit, worktree),
                     merge=copies.MergeRevision(merge_ref)) as tree:
    run_gate(tree)
```

The source policy is `CommittedSource(revision)`, `WorktreeSource(revision, worktree)`, or `StagedSource(tree)`.
`StagedSource` requires a tree identity.
The merge policy defaults to `NoMerge()`.
`standing` yields a `Path` while it holds the lease.
Callers must finish all checkout access before the context ends.
`CopyError` represents a refusal.
`PreparationError` represents an application or merge failure.

`hold --repo R` holds a staged checkout until its standard input closes.
It computes the staged tree through a private index before it prepares the checkout.
It accepts no staged-tree argument.
It prints the tree identity first and the checkout path second.
It reads the source owner, including the owner in a standing checkout's `.gate.json`.
`verify.sh doc` reads both lines and retains the lease through descriptor 8.
The source index and unchanged file modification times remain unchanged.
`run` requires `--commit` and accepts no staged-source flags.

## Guard and exceptions

`scripts/copies/copy_guard.py` owns the creation rule; `config/copies.conf` owns
the allow list. The
PreToolUse Bash dispatcher refuses direct repository cloning, worktree
creation, and recursive repository copying. Its refusal gives a `copies.py run` example and the `make_worktree.sh <path> <branch>` command for a lasting phase tree.
Run that command from the source repository. The repository scan applies the same allow list to script code.

The guard distinguishes a detected tree-making operation from an unresolved relevant input and an allowed operation.
Only a detected tree-making operation adds the `copies.py` instruction.
An unresolved input names the value that the guard could not read.
A known tree-making operation keeps its refusal when another input is unresolved.

`shell_syntax.py` distinguishes literal words from expanded words.
The guard maps these to `KnownArgument` and `UnknownArgument`, while retaining known text.
A lexer error resumes at the next command separator.
No `bash -n` process runs for each hook call.
Interpreter decoding distinguishes program text, stdin programs, script operands, modules, and informational input.
Descriptor duplication, moves, closure, and replacement determine the selected stdin.
Literal data remains data. Executable substitutions receive a recursive check.
Unknown relevant runtime input refuses; static analysis can defer unknown function input to concrete calls.
Python import bindings share one scope model.
Deferred functions use a queue of function and execution pairs, so call order cannot hide a maker.

Later assignments replace earlier values.
Branches and loops merge values into flattened, deduplicated `ArgumentChoices`.
`guard_choice_limit=32` bounds combinations for one command.
Above that limit, `UnresolvedChoices.choice_texts` retains dropped text through one linear walk.
`ProgramWithChoiceTexts` carries this text through `eval`, `bash -c`, and `python -c`.
A program with no tree-making word passes.
Known or retained tree-making text refuses with `cannot resolve interpreter program choices`.
An unknown executable in a tree-making position keeps its unresolved-executable refusal.
The required-key reader uses the repository configuration when an override omits `guard_choice_limit`.

Expanded Python that does not parse passes unless its known text contains a tree-making word.
That case refuses with `cannot parse Python program`.
Supported syntax covers the tested forms, rather than every shell dialect.
Aliases, sourced files, generated programs, and interprocedural analysis retain known limits.
Above the choice limit, a tree word split across a literal and a dropped value can pass.
An unset source or Git path inside interpreter text can also pass when its top-level command would refuse.
[Pending verify work](../plans/natedev-verify-next.md) records these limits.
`test_branch_arguments_require_linear_invocation_work` checks the branch-analysis work bound.

| Allowed creator | Reason |
| --- | --- |
| `scripts/copies/copies.py` | Creates temporary gate checkouts, standing caches, remote mirrors, and registered test clones. |
| `scripts/production/add_unit.py` | Creates original unit worktrees. |
| `scripts/agents/shutdown.py` | Restores original unit worktrees. |
| `scripts/style/add_style.py` | Creates a unit's original checkout, like `add_unit.py`. |
| `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` | Clones external upstream at a version, not a copy of our work. |
| `scripts/worktree/make_worktree.sh` | Makes an original work worktree, not a build copy. |

Cargo gates use the first available cache slot.
More than two Cargo gates for one owner and repository wait.
TMPDIR cleanup at reboot removes checkouts left by killed gate processes.

## Remote mirrors

`copies.py` creates and refreshes mirrors for Mac, Linux, and AWS targets.
`offload.py` requests a mirror through this API.
`mac_run.sh` creates and registers its test clone through the same implementation.
Neither caller has a copy-creation exception.

`remote_lease_path(mirror)` names the lease beside the mirror's registry record.
The claim service reads `/proc/locks` once and reports held mirrors by host, path, and session.
A busy lease reads only its own registry record.
The mirror lease covers the claim read and the copy-record lock.
`read_claims` returns a snapshot after each short admission lock closes.
`mirror_metadata_locks` holds only the copy-record lock inside the mirror lease.
The remote command starts after the record lock closes.
It retains the mirror lease until it ends.

`mark_mirror` writes the record, session, and mark.
`floor_claims.read_mirror_record` reads a record path, including removal authority.
`floor_claims` owns the metadata names and mirror lock paths.
An outside mirror lease survives task parent removal.
The existing sibling lease remains in use for compatibility with live old leases.
Content removal deletes metadata last, with the record after the session and mark.
`remote_rsync` protects these names through explicit exclusions.
Receive recovery keeps the existing owner and preserves a complete replacement record.
It publishes all metadata before it removes the removal authority.
Successful use transfers ownership to the selected owner.
Removal prefers the current record and uses removal authority only when the record is absent.
The sweep finds marks and removal authority in the two supported mirror layouts.

`register-remote` requires `--owner` and `--repository`.
It accepts no source repository path.
`unit_owner` preserves recorded names, including underscores, and appends one `-unit` suffix.

Mirror checks use `tmp_sweep.process_paths` on Linux and `tmp_sweep.lsof_paths` on the Mac.
A working directory, mapped file, or file open for writing below the mirror keeps it busy.
A file open only for reading does not keep the mirror busy.
Linux reads descriptor flags from `/proc`; lsof includes the `a` access field.
Named constants describe these uses.
Temporary sweeps still protect folders with any open file and keep their existing report text.
A path with only the same text prefix does not keep the mirror busy.
`mirror_lease_set` takes candidate leases before one shared process scan.
It classifies mirrors while it holds these leases.
It releases busy mirrors and reports them as protected.
`local_mirror_lease` scans after it acquires its own lease.
An unreadable process in the recorded process group keeps the mirror busy.
An unavailable scan also keeps the mirror busy.
The lsof scanner accepts output only with exit status 0 or 1.
Every other status makes the scan unavailable.
The Linux scanner reports unreadable working directories during its single process scan.

`target_config.slot_capacity` reads the slot capacity for claims and offload admission.
Both callers raise `ConfigError` for a bad configured value, including an empty value.
The required `missing` policy applies only when both capacity keys are absent.
Mac claims still use one slot.
Offload records use `build_hold.owning_session()` before the shell session fallback.
Busy refusals can therefore name the unit director's session.

After confirmed clone cleanup, `mac_run.sh` calls `copies.py unregister-remote` for both registry records.
It removes the Mac's `localhost` record before the natedev record.
`unregister-remote` accepts only `--host` and `--path`.
`mirror-remove` accepts `--host`; receive and use accept the command arguments.
A failed clone cleanup keeps both records.
A marked mirror probe starts no remote Python and skips `mirror_local_path`.
An unmarked probe keeps the existing path check and metadata creation.

## Original worktrees and landing

`make_worktree.sh <path> <branch>` calls `worktrees.py make` from the source repository.
`config/copies.conf` permits both original-worktree helpers.
The helper creates the Git worktree and records it under `~/.local/state/worktrees/made/<path hash>.json`.
The path hash uses SHA-256.
`WORKTREES_STATE_DIR` selects private state.
`WorktreeRecord` holds version, path, branch, repository, base, merge branch, owner, and creation time.
A failed record write removes the new tree and branch.

The caller's staff row supplies its owner and merge branch.
Without a staff row, the helper uses `origin/HEAD` and an empty owner.
Without `origin/HEAD`, it records an empty merge branch, so a production merge does not end that tree.

`landed(record)` returns `Landed` or `NotLanded(commits, paths)`.
It accepts patch-equivalent commits, merge commits, and commits that change only `docs/plans/`.
Each other changed path must match the merge branch or have a newer change on that merge branch.

After a successful merge-branch push, `merge_checkpoint.py` calls `end_landed(repo, merge_branch)`.
It reports `Ended` or `Kept` as a `worktrees` step.
It keeps a staff-held tree, a dirty tree, a different checked-out branch, and work that has not landed.
A kept tree does not fail the merge.
An eligible tree ends through worktree removal, local and origin branch deletion, then record deletion.
Missing paths or branches allow a retry to complete an interrupted end.
The main checkout is the first entry from `git worktree list`.

There is no `end` command, sweep command, timer, or session-end cleanup for these original trees.
The successful landing operation owns their end.
Tests that call the helper must set a private `WORKTREES_STATE_DIR`.
