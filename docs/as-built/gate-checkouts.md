# Standing gate checkouts

`scripts/copies/copies.py` is the single place that makes gate, review, merge,
and landing checkouts.
`standing` calls `keeps_build_cache(source, base)` to select the copy lifetime.
The function runs `git cat-file -e <base>:Cargo.toml`.
A root manifest in the selected commit selects a standing copy.
Other commits select a fresh copy, including commits with only nested Cargo manifests.

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
Each owner and repository with a root Cargo manifest has two standing checkout slots.

The same file holds `allow.<path>=<reason>` entries under `[allow-list]`, with
paths relative to `scripts/`. `copies.load_config().allow_list` supplies these
entries to the guard's scan. The guard does not read configuration separately.

Standing trees live below `<root>/<repo>/`.
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
Its nested proof crate does not select a standing copy.
The root under `~/worktrees` belongs to the existing trusted direnv root.

The CLI reads the calling session's local staff row.
A worker uses its parent unit's row id.
A caller without a staff row receives an error.
Text owners enter through one boundary as staff row ids or fixed role names.

## Fresh copies

`fresh_tree(owner)` creates a scratch task folder with the default task and the word `<current staff name>-gate` or `<role>-gate`.
It creates a `checkout` folder inside the task folder and yields a `GateLease`.
The task folder's busy lock protects the copy throughout preparation and caller use.
Command children inherit the lease descriptors.
Fresh copies do not use the standing pool or register as editable workspaces.
Overlapping callers receive separate folders without waiting for another caller.

Preparation fetches the selected commit and applies the requested source and merge policies.
The context removes the task folder after success, a caller exception, or a preparation failure.
A failed removal raises `PostUseError` and retains the caller's exception in `caller_error`.
A killed process leaves the folder for the scratch sweep.
`fresh_tree` sets the scratch record's `throwaway` mark because code creates the folder and no person edits it.
The sweep skips the Git work check for a marked folder.
The busy lock, expiry, and other keep rules still apply.
An absent mark means false, so other folders keep their Git work protection.
No command-line option sets the mark.
Fresh copies skip the metadata and cache refresh after use.

## Reset and build output

The first standing use makes a clone with its own `.git` directory.
The clone borrows objects from the last available store in the source's single-path alternates chain.
A missing store, repeated store, or alternates file with multiple lines ends the chain.
A source with no chain keeps its own objects as the borrowed store.
When the last store belongs to a registered scratch copy, the clone copies objects without an alternates file.
The existing refresh fetches the source's own objects.
Task clones and editable workspaces clone the source with `--shared`.
They retain commits that exist only in the source, including detached commits.
The gate then works after intermediate clones or the registered scratch source are removed.
It never copies a linked worktree's `.git` pointer or changes the source's branches.
The clone's Git metadata starts inside the locked directory.
The code then installs this metadata without releasing the lock.

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
Standing copies also refresh both remotes after use.
A source without an `origin` remote keeps the local source URL and local branch mapping.
These copies also keep `source`, so all copies use the same refresh rule.
Legacy adoption resolves each local origin URL from its own repository before comparison.

Each use fetches the selected commit from the source, checks it out with
`checkout --force --detach`, and runs `clean -ffdq`. It keeps ignored files,
including `target/`. Git updates changed files while preserving unchanged file
modification times, allowing the next build to remain incremental. There is no
seed copy or shared build directory.

`scratch.mark_cargo_cache` marks and refreshes `target/`. The existing floor
sweep can reclaim idle build output; a later gate rebuilds it. The checkout's
busy mark also protects its target while a caller uses the tree.
Cache refresh never follows a `target` symlink planted by a caller. The next
use removes that link and restores a local cache without changing its target.

## Acquisition and cleanup

A standing use holds `scratch.busy_locks(tree, create=True)` throughout preparation and the entire command or Python context. The mark is the existing `.claude-busy`
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

Each prepared tree has one `.gate.json` record:

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
`/.gate.json`, `/.claude-busy`, and `/target/`.

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
branch from the source's origin into the standing checkout, then merges it, so a
stale source tracking ref cannot select an old merge. Other merge refs are fetched from the
source repository. Local origin paths resolve relative to the source repository;
remote URLs keep their original form. Source refs stay unchanged. The merge uses `--no-ff --no-edit`.
Failed application or a merge conflict exits 3,
names the affected files, and releases the lease.
A fresh copy is deleted.
The next standing use resets its tree.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py list
~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py remove \
  --repo ~/worktrees/claude-build-followups-verify
```

`list` prints one row per tree: path, owner, repo, commit, busy or idle, last
holder pid, and target size. `remove` removes the caller's trees
across all repositories. An optional `--repo R` limits removal to that repository.
The Python API accepts only a `RepositoryScope` value for `repo`.
`copies.remove_trees(owner)` uses `AllRepositories()` by default.
`copies.remove_trees(owner, SelectedRepository(name))` limits removal to one repository identity.
The API refuses raw strings and paths, including an empty string.
Removal includes standing checkouts, registered editable copies, and adopted
legacy workers.
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

Unit retirement and run end remove the unit's idle standing trees and workspaces.
A busy local tree prints one line and does not stop retirement or run end.
A remote mirror failure refuses retirement and keeps the unit worktree for a retry.
A retirement retry repeats cleanup. The existing cleanup also removes idle trees.
`tidy_wrapped.py` removes the production's checkouts and its units' checkouts
before it deletes production state. A refusal keeps the state for a retry.

Success exits 0; invalid owners, invalid configuration, and other refused
operations exit 2. Apply and merge failures exit 3. `run` returns its command's
status. Repeating a standing use resets the same checkout and retains its build files.
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

The guard distinguishes no forbidden operation, a specific detected repository-tree operation, and an unresolved relevant operation. One invocation decoder handles shell commands, tool argv and Python subprocess vectors. It distinguishes command text, stdin programs, script/module operands and informational invocations using interpreter-specific option semantics: CPython options and operands, Bash/Zsh invocation options, and portable POSIX sh meanings. Script stdin and informational invocation input are data, while executable outer expansions are inspected before exemptions. Ordered descriptor duplication through <& and >&, moves, closure and replacement determine selected stdin; an unknown selected input refuses with a specific unresolved reason. Nested parsing distinguishes program, array word-list, parameter and arithmetic context. Literal data stays data while executable substitutions are inspected recursively. Runtime unresolved relevant programs refuse; static abstract analysis may defer unknown interpreter input to concrete call sites. Known gaps remain interprocedural analysis, generated programs such as bash -c "$(printf ...)", aliases and sourced files. Supported syntax is bounded by the documented tested forms rather than a claim to implement every shell dialect.

| Allowed creator | Reason |
| --- | --- |
| `scripts/copies/copies.py` | Creates fresh checkouts, standing checkouts, remote mirrors, and registered test clones. |
| `scripts/production/add_unit.py` | Creates original unit worktrees. |
| `scripts/shutdown/shutdown.py` | Restores original unit worktrees. |
| `scripts/berth_fix/launch.sh` | Creates a standing original fixer worktree, not a build copy. |
| `scripts/style/add_style.py` | Creates a unit's original checkout, like `add_unit.py`. |
| `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` | Clones external upstream at a version, not a copy of our work. |
| `scripts/worktree/make_worktree.sh` | Makes an original work worktree, not a build copy. |

Standing comparisons run in sequence in one tree or use the second tree.
More than two standing builds for one owner and repository wait.
The existing floor sweep handles old copies under `/tmp`.

## Remote mirrors

`copies.py` creates and refreshes mirrors for Mac, Linux, and AWS targets.
`offload.py` requests a mirror through this API.
`mac_run.sh` creates and registers its test clone through the same implementation.
Neither caller has a copy-creation exception.

`remote_lease_path(mirror)` names the lease beside the mirror's registry record.
The claim service reads `/proc/locks` once and reports held mirrors by host, path, and session.
A busy lease reads only its own registry record.
The mirror lease precedes the floor claim lock, which precedes the copy-record lock.
`mirror_metadata_locks` holds the claim and record locks inside the mirror lease.
The remote command starts after these metadata locks close.
It retains the mirror lease until it ends.

`mark_mirror` writes the record, session, and mark.
`floor_claims.read_mirror_record` reads a record path, including removal authority.
`floor_claims` owns the metadata names and the sibling lock paths.
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
A working directory, open file, or mapped file below the mirror keeps it busy.
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
