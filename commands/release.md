# Unified Release Command

Perform a release for any Rust crate or workspace project.

## Usage
- `/release` or `/release help` - Show this usage information
- `/release patch` - Auto-detect latest published version and increment patch
- `/release patch dry-run` - Rehearse patch release without mutations
- `/release X.Y.Z` - Release as final version (e.g., `0.18.0`)
- `/release X.Y.Z-rc.N` - Release as RC version (e.g., `0.18.0-rc.1`)
- `/release X.Y.Z dry-run` - Rehearse release without mutations

**Releasing a single package from a multi-crate workspace** (only when the workspace does NOT use coordinated `[[publish_phases]]` config):
- `/release <package> X.Y.Z` - Release only that crate (e.g., `/release bevy_lagrange 0.0.4`)
- `/release <package> patch` - Patch-bump and release only that crate
- `/release <package> X.Y.Z-rc.N` - Release only that crate as an RC
- `/release <package> X.Y.Z dry-run` - Rehearse a single-package release without mutations

The package token is optional and identified by name — any argument that is not `help`, `patch`, `dry-run`, or a version. Argument order does not matter. Without it, the whole workspace releases together (unchanged). In single-package mode the release branch is `release-<package>-<version>` and the tag is `<package>-v<version>`, so single-package releases never collide with whole-workspace ones.

**Releasing from a non-`main` branch**: When the current branch is not `main`, the branch alone can't say whether it's a hotfix or an isolated release, so STEP 1 asks which mode applies:
- **Hotfix mode** — a fix branched off an old release tag. Finalizes and publishes from the current branch, then cherry-picks the fix back to main (STEP 12). The branch is a fire-and-forget record.
- **Isolated branch release** — the current branch is the future mainline (e.g. a Bevy-update branch you'll merge to main yourself later). Runs the full normal-mode sequence rooted on the current branch in place of main: finalizes there, cuts a `release-X.Y.Z` snapshot off it, publishes, then restores `[Unreleased]` and bumps the branch to the next `-dev`. **Main is never touched and nothing is cherry-picked.**

**If `$ARGUMENTS` is empty or `help`**: First read `.claude/config/release.toml` if it exists and display the progress checklist (with config-aware sub-items), then display the usage block above, then stop. Do not proceed with any release steps beyond displaying the checklist and usage.

## Dry-Run Mode

When `dry-run` is present in `$ARGUMENTS`, set `${DRY_RUN_FLAG}` to `--dry-run` (otherwise empty string).

**All mutating scripts accept `--dry-run`** as a parameter. In dry-run mode, pass `${DRY_RUN_FLAG}` to every script call. The scripts themselves handle reporting what they would do without making changes.

For agent judgment steps (README updates, changelog review, GitHub release notes), describe what would be done without making changes. For git commits, report what would be committed without running the command.

## Single-Package Mode

When a package selector is present in `$ARGUMENTS` (validated in STEP 1), set `${PACKAGE}` to that crate name and `${PACKAGE_FLAG}` to `--package ${PACKAGE}`. Otherwise both are empty and every step behaves exactly as a whole-workspace release.

In single-package mode the entire pipeline is scoped to the one crate: `${ALL_CRATE_NAMES}`, `${ALL_VERSION_FILES}`, `${ALL_CHANGELOG_FILES}`, the published README, and `${PROJECT_NAME}` all narrow to that crate. The release branch becomes `release-${PACKAGE}-${VERSION}` and the tag `${PACKAGE}-v${VERSION}`. All three modes (normal, isolated, hotfix) run unchanged apart from this scoping. `pre_release_checks.sh` (STEP 2) still runs workspace-wide — the whole workspace must build, lint, and test before any single crate ships.

**Single-package mode is incompatible with `[[publish_phases]]` config.** A workspace that defines publish phases releases its crates together with ordered cross-dependency updates; STEP 1 stops if a package is named there.

## Versioning Strategy

All projects use a **branch-first release model**:
- **main branch**: Always has `-dev` versions (e.g., `0.18.0-dev`)
- **release branches**: Created BEFORE publishing, contain actual release versions
- **Publishing**: Always happens from release branches, never from main
- **No merge back**: Release branches are fire-and-forget snapshots
- **Hotfix releases**: When releasing from a non-main branch (e.g., a branch based on a previous release tag), STEP 1 offers hotfix mode. Changes are published from the current branch, then cherry-picked back to main with user verification before the hotfix branch is cleaned up.
- **Isolated branch releases**: When releasing from a branch that will later merge to main (not a hotfix), STEP 1 offers isolated mode. The whole model above applies with the current branch playing main's role: it keeps the `-dev` version and `[Unreleased]` section, and a fire-and-forget `release-X.Y.Z` snapshot is cut from it. Main is left untouched — you merge the branch when ready.

## Configuration

The command works with **zero config** by convention. It auto-detects:
- Single crate vs workspace from root `Cargo.toml`
- Crate names from each `Cargo.toml` `[package].name`
- Version files: `{crate_dir}/Cargo.toml`
- Changelogs: `{crate_dir}/CHANGELOG.md`
- Published READMEs: determined per-crate from `Cargo.toml` `readme` field or `{crate_dir}/README.md` auto-discovery
- GitHub repo via `gh repo view`

**Optional config** at `.claude/config/release.toml` for projects that need overrides:

```toml
# Post-release install verification (binary crates only)
install_verify = "crate_dir"
# Optional: custom install script (overrides default `cargo install`)
install_verify_script = ".claude/scripts/release/install_verify.sh"

# Publish all workspace members in one `cargo publish --workspace` call
# (cargo >= 1.90). Members excluded by discovery get --exclude flags;
# publish = false members are skipped by cargo automatically.
# Incompatible with [[publish_phases]]. Ignored in single-package mode.
workspace_publish = true

# Ordered publish phases (workspace dependency chains only)
[[publish_phases]]
name = "Display name"
crates = ["crate_dir"]
wait_seconds = 30
workspace_dep_updates = ["dep_name"]
post_script = ".claude/scripts/release/my_script.sh"

[[publish_phases]]
name = "Display name"
crates = ["crate_dir_a", "crate_dir_b"]

# Agent judgment checks at named checkpoints
[[judgment_checks]]
name = "Check name"
checkpoint = "pre_publish"
instructions = "What the agent should verify..."
```

## Scripts

All scriptable steps use shell scripts. The agent orchestrates script execution and reports results.

**Sandbox note**: Scripts that make network calls (marked with ⊘ below) must run with `dangerouslyDisableSandbox: true`. The sandbox proxy breaks git credential forwarding.

**Universal scripts** in `~/.claude/scripts/release/`:
- `validate_version.sh` — format, collision, and gap checking ⊘
- `pre_release_checks.sh` — git status, clippy, build, test, fmt ⊘
- `create_release_branch.sh` — branch creation
- `bump_versions.sh` — update [package] version fields
- `finalize_changelogs.sh` — replace [Unreleased] with version header
- `publish_crate.sh` — dry-run and publish a single crate ⊘
- `update_workspace_deps.sh` — update workspace dependency versions between publish phases ⊘
- `push_release.sh` — tag, push branch, push tag ⊘
- `verify_published.sh` — check crates.io versions ⊘
- `create_github_release.sh` — create GitHub release ⊘
- `restore_unreleased.sh` — add [Unreleased] sections back to changelogs

**Project-specific scripts** in each project's `.claude/scripts/release/` — referenced by config `post_script` fields.

## Checkpoints

Named checkpoints are defined at key points in the release sequence. The config `[[judgment_checks]]` reference these by name. If a judgment check's `checkpoint` matches, the agent runs the check at that point.

Available checkpoints:
- `quality_checks_complete` — after pre-release validation passes
- `readmes_updated` — after README changes committed
- `changelogs_finalized` — after changelogs finalized on `${BASE_BRANCH}`
- `branch_created` — after release branch exists
- `versions_bumped` — after version bump commit
- `pre_publish` — before any crate is published
- `post_publish` — after all crates are published
- `release_pushed` — after branch and tag pushed

## IMPORTANT: Version Handling in Commands

**Throughout this release process**, when you see `${VERSION}` in bash commands, you must substitute the actual version number directly (e.g., "0.17.0") instead of using shell variables. Shell variable assignments require user approval.

**Example:**
- Documentation shows: `~/.claude/scripts/release/create_release_branch.sh ${VERSION}`
- You should run: `~/.claude/scripts/release/create_release_branch.sh 0.17.0`

This applies to ALL bash commands and script invocations in this process.

<ProgressBehavior>
**IMPORTANT**: Before rendering the checklist for the first time, read `.claude/config/release.toml` if it exists. The checklist must incorporate project-specific config.

**AT START**: Dynamically generate the full progress list:

1. Scan this document for all `## STEP N:` headers
2. Extract step number and description from each header
3. If config has `[[publish_phases]]`, expand STEP 6 with sub-items for each phase
4. If config has `[[judgment_checks]]`, show them as sub-items under the step whose checkpoint they target
5. If config has `install_verify`, append it to STEP 9's description
6. Only show checkpoint sub-items when they have judgment checks or publish phases attached — bare checkpoints with nothing configured are invisible
7. If `${SINGLE_PACKAGE_MODE}`, add a `Package: ${PACKAGE}` line beneath the `RELEASE ${VERSION}` title
8. Display the full checklist

**Example with no config (e.g., bevy_window_manager):**
```
═══════════════════════════════════════════════════════════════
                 RELEASE ${VERSION} - PROGRESS
═══════════════════════════════════════════════════════════════
[ ] STEP 0:  Argument Validation
[ ] STEP 1:  Project Discovery
[ ] STEP 2:  Pre-Release Validation
[ ] STEP 3:  Update READMEs
[ ] STEP 4:  Finalize Changelogs and Bump Versions
[ ] STEP 5:  Create Release Branch
[ ] STEP 6:  Publish to crates.io
[ ] STEP 7:  Push Release Branch and Tag
[ ] STEP 8:  Create GitHub Release
[ ] STEP 9:  Post-Release Verification
[ ] STEP 10: Restore [Unreleased] Sections
[ ] STEP 11: Restore Dev Version on ${BASE_BRANCH}
═══════════════════════════════════════════════════════════════
```

**Example with config (e.g., bevy_brp):**
```
═══════════════════════════════════════════════════════════════
                 RELEASE ${VERSION} - PROGRESS
═══════════════════════════════════════════════════════════════
[ ] STEP 0:  Argument Validation
[ ] STEP 1:  Project Discovery
[ ] STEP 2:  Pre-Release Validation
[ ] STEP 3:  Update READMEs
[ ] STEP 4:  Finalize Changelogs and Bump Versions
[ ] STEP 5:  Create Release Branch
[ ] STEP 6:  Publish to crates.io
      ◇ pre_publish — API docs sync
      [ ] Phase 1: Publish macros → update workspace deps
      [ ] Phase 2: Publish extras and mcp
[ ] STEP 7:  Push Release Branch and Tag
[ ] STEP 8:  Create GitHub Release
[ ] STEP 9:  Post-Release Verification + install verify (mcp)
[ ] STEP 10: Restore [Unreleased] Sections
[ ] STEP 11: Restore Dev Version on ${BASE_BRANCH}
═══════════════════════════════════════════════════════════════
```

**Example hotfix mode:**
```
═══════════════════════════════════════════════════════════════
          HOTFIX RELEASE ${VERSION} - PROGRESS
              (from branch: ${HOTFIX_BRANCH})
═══════════════════════════════════════════════════════════════
[ ] STEP 0:  Argument Validation
[ ] STEP 1:  Project Discovery
[ ] STEP 2:  Pre-Release Validation
[—] STEP 3:  Update READMEs (skipped — hotfix)
[ ] STEP 4:  Finalize Changelogs and Bump Versions (on hotfix branch)
[—] STEP 5:  Create Release Branch (skipped — hotfix)
[ ] STEP 6:  Publish to crates.io
[ ] STEP 7:  Push Release Branch and Tag
[ ] STEP 8:  Create GitHub Release
[ ] STEP 9:  Post-Release Verification
[—] STEP 10: Restore [Unreleased] Sections (skipped — hotfix)
[—] STEP 11: Restore Dev Version on ${BASE_BRANCH} (skipped — hotfix)
[ ] STEP 12: Cherry-pick to main
═══════════════════════════════════════════════════════════════
```

**Example isolated branch release:**
```
═══════════════════════════════════════════════════════════════
        ISOLATED BRANCH RELEASE ${VERSION} - PROGRESS
              (base branch: ${BASE_BRANCH})
═══════════════════════════════════════════════════════════════
[ ] STEP 0:  Argument Validation
[ ] STEP 1:  Project Discovery
[ ] STEP 2:  Pre-Release Validation
[ ] STEP 3:  Update READMEs (on ${BASE_BRANCH})
[ ] STEP 4:  Finalize Changelogs and Bump Versions (on ${BASE_BRANCH})
[ ] STEP 5:  Create Release Branch
[ ] STEP 6:  Publish to crates.io
[ ] STEP 7:  Push Release Branch and Tag
[ ] STEP 8:  Create GitHub Release
[ ] STEP 9:  Post-Release Verification
[ ] STEP 10: Restore [Unreleased] Sections (on ${BASE_BRANCH})
[ ] STEP 11: Restore Dev Version on ${BASE_BRANCH}
═══════════════════════════════════════════════════════════════
```
Same sequence as normal mode, but every "on main" step runs on `${BASE_BRANCH}` and main is never modified. There is no STEP 12.

**BEFORE EACH STEP**: Re-render the full checklist showing:
- `[x]` for completed steps (and their sub-items)
- `[>]` for the step about to start
- `[ ]` for pending steps
- `[—]` for skipped steps (e.g., hotfix-skipped steps)

**AFTER FINAL STEP**: Re-render one last time with all steps showing `[x]` (or `[—]` if skipped).
</ProgressBehavior>

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <ArgumentValidation/>
    **STEP 1:** Execute <ProjectDiscovery/>

    Display <ProgressBehavior/> full list (requires project discovery to have completed first), then proceed:

    **If normal mode (on `main`) or isolated branch release (on your branch):**

    These two modes run the identical sequence; only the base branch differs. `${BASE_BRANCH}` is `main` in normal mode and your current branch in isolated mode. In isolated mode, main is never modified.

    **STEP 2:** Execute <PreReleaseChecks/>
    → Checkpoint: `quality_checks_complete`
    **STEP 3:** Execute <UpdateReadmesOnBase/>
    → Checkpoint: `readmes_updated`
    **STEP 4:** Execute <FinalizeOnBase/>
    → Checkpoint: `changelogs_finalized`
    **STEP 5:** Execute <CreateReleaseBranch/>
    → Checkpoint: `branch_created`
    → Checkpoint: `versions_bumped`
    **STEP 6:** Execute <PublishPhases/>
    → Checkpoint: `pre_publish` (before first publish)
    → Checkpoint: `post_publish` (after last publish)
    **STEP 7:** Execute <PushReleaseBranch/>
    → Checkpoint: `release_pushed`
    **STEP 8:** Execute <CreateGitHubRelease/>
    **STEP 9:** Execute <PostReleaseVerification/>
    **STEP 10:** Execute <RestoreUnreleasedSections/>
    **STEP 11:** Execute <RestoreDevVersion/>

    **If hotfix mode (not on main):**

    **STEP 2:** Execute <PreReleaseChecks/>
    → Checkpoint: `quality_checks_complete`
    **STEP 3:** [—] Skip (hotfix — READMEs already updated on hotfix branch)
    **STEP 4:** Execute <FinalizeOnHotfixBranch/>
    → Checkpoint: `changelogs_finalized`
    → Checkpoint: `versions_bumped`
    **STEP 5:** [—] Skip (hotfix — already on release branch)
    **STEP 6:** Execute <PublishPhases/>
    → Checkpoint: `pre_publish` (before first publish)
    → Checkpoint: `post_publish` (after last publish)
    **STEP 7:** Execute <PushReleaseBranch/>
    → Checkpoint: `release_pushed`
    **STEP 8:** Execute <CreateGitHubRelease/>
    **STEP 9:** Execute <PostReleaseVerification/>
    **STEP 10:** [—] Skip (hotfix — main manages its own unreleased sections)
    **STEP 11:** [—] Skip (hotfix — main manages its own dev version)
    **STEP 12:** Execute <HotfixCleanup/>

    At each checkpoint, check if the config defines any `[[judgment_checks]]` with a matching `checkpoint` value. If so, execute the agent judgment check and report findings before continuing. Stop and consult the user if the check reveals issues.
</ExecutionSteps>

<ArgumentValidation>
## STEP 0: Argument Validation

**Parse `$ARGUMENTS`** — it may contain, in any order: an optional package selector, exactly one of a version (`X.Y.Z` / `X.Y.Z-rc.N`) or the literal `patch`, and an optional `dry-run`.
- The token matching `^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$` is the version.
- `patch` selects patch mode; `dry-run` sets `${DRY_RUN_FLAG}`; `help` or empty shows usage.
- Any remaining token is the **package selector** — set `${PACKAGE}` to it. It is validated against workspace members in STEP 1 (and rejected there if the workspace uses `[[publish_phases]]`).

**If argument is `patch` (with or without `dry-run`):**
1. Detect the crate name: if `${PACKAGE}` is set, use it; otherwise the primary crate (single crate: from root `Cargo.toml`; workspace: from first `[workspace.members]` entry or first publish phase crate if config exists)
2. Query crates.io for the latest published version:
```bash
curl -s "https://crates.io/api/v1/crates/${CRATE_NAME}" | jq -r '.crate.max_version'
```
3. Increment the patch number (e.g., `0.18.0` → `0.18.1`)
4. Set `${VERSION}` to the incremented version
5. Mark this as a **patch release** (STEP 10 will be skipped)

**Otherwise, extract version from `$ARGUMENTS`** (strip `dry-run` if present).

**Run version validation** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/validate_version.sh ${VERSION} ${ALL_CRATE_NAMES}
```

→ Report the script output to the user. Stop if validation fails.
</ArgumentValidation>

<ProjectDiscovery>
## STEP 1: Project Discovery

**Read optional config** from `.claude/config/release.toml` if it exists. Store any `install_verify`, `install_verify_script`, `workspace_publish`, `publish_phases`, and `judgment_checks` settings.

**If config sets both `workspace_publish = true` and `[[publish_phases]]`**, STOP with a clear error: the two are mutually exclusive — `workspace_publish` is for workspaces whose members publish together with no between-phase scripts or dep rewrites; `[[publish_phases]]` is for ordered chains that need them.

**If config references any `post_script` or `install_verify_script` files, verify they exist:**
→ Stop with clear error if any referenced script is missing.

**Detect project structure** from root `Cargo.toml`:

**Single crate** (no `[workspace]` section):
- One crate at `.` (root directory)
- Version file: `Cargo.toml`
- Changelog: `CHANGELOG.md`
- Published README: Check `Cargo.toml` for explicit `readme` field → use that path. Otherwise `README.md` if it exists. This is what ships to crates.io.

**Workspace** (`[workspace]` section present):
- Read `[workspace.members]` to find crate directories
- Exclude any member that contains `test`, `example`, or `benchmark` in its path
- For each crate directory:
  - Read crate name from `{dir}/Cargo.toml` `[package].name`
  - Version file: `{dir}/Cargo.toml`
  - Changelog: `{dir}/CHANGELOG.md` (if exists)
  - Published README: Check `{dir}/Cargo.toml` for explicit `readme` field → resolve path relative to crate dir. Otherwise `{dir}/README.md` if it exists. Only these ship to crates.io.
- Root README (`README.md`): tracked separately as **project README** — not published to crates.io for workspace projects. Only included in STEP 3 if it contains hardcoded version references.

**Detect GitHub repo** (uses git remote to avoid sandbox TLS issues with `gh`):
```bash
git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||'
```

**Detect release mode** from current branch:
```bash
git branch --show-current
```
- If branch is `main`: **normal mode**. Set `${BASE_BRANCH}` to `main`.
- If branch starts with `release-`: **possible stale release branch** — warn the user: "You are on branch `${BRANCH}` which looks like an existing release branch. Are you sure this is a hotfix? Type **yes** to continue in hotfix mode or **no** to abort." Stop if user says no.
- If branch is anything else: **ask which mode applies** — the branch alone can't distinguish a hotfix from an isolated release, so present both with their behavior and let the user choose:

  > You're on branch `${BRANCH}`, not `main`. Which kind of release is this?
  >
  > **hotfix** — a fix branched off an old release tag. I finalize and publish from this branch, then cherry-pick the fix back onto main (with your verification). The branch stays as a fire-and-forget record. Pick this to patch an already-released version without pulling in main's newer work.
  >
  > **isolated** — this branch is the future mainline (e.g. a Bevy-update branch you'll merge to main yourself later). I run the full release here exactly as on main: finalize, cut a `release-${VERSION}` snapshot, publish, then restore `[Unreleased]` and bump this branch to the next `-dev`. **Main is never touched and nothing is cherry-picked.** Pick this when the release must not modify main at all.
  >
  > Type **hotfix** or **isolated**.

  - If **hotfix**: set `${HOTFIX_MODE}` to `true` and `${HOTFIX_BRANCH}` to the branch name.
  - If **isolated**: set `${ISOLATED_MODE}` to `true` and `${BASE_BRANCH}` to the current branch name. The release follows the normal-mode step sequence with `${BASE_BRANCH}` substituted for `main` everywhere.

**Single-package scoping** (only when `${PACKAGE}` is set from STEP 0):
1. **Guard:** if the config defines any `[[publish_phases]]`, STOP with: "This workspace uses coordinated publish_phases — its crates release together with ordered cross-dependency updates, so a single-package release isn't supported here. Run `/release ${VERSION}` to release the whole workspace." Do not proceed.
2. Verify `${PACKAGE}` matches exactly one discovered member's `[package].name`. If it matches none, STOP and list the available member crate names.
3. If the matched crate's `Cargo.toml` uses `version.workspace = true`, WARN: its version is shared with the whole workspace, so releasing it alone forces the shared version onto its siblings. Ask the user to confirm or pick an independently-versioned crate before continuing.
4. Narrow every "all crates" value to the matched crate only — `${ALL_CRATE_NAMES}`, `${ALL_VERSION_FILES}`, `${ALL_CHANGELOG_FILES}`, the published README list, and `${PROJECT_NAME}`. Set `${SINGLE_PACKAGE_MODE}` = true and `${PACKAGE_FLAG}` = `--package ${PACKAGE}`.

When `${PACKAGE}` is not set, `${SINGLE_PACKAGE_MODE}` is false and `${PACKAGE_FLAG}` is empty — discovery keeps every non-excluded member as before.

**Display discovered project info:**
```
Project: ${GITHUB_REPO}
Type: single crate | workspace
Crates: ${CRATE_LIST}
Package: ${PACKAGE} (single-package release) | (whole workspace)
Config: found | using defaults
Dry-run: yes | no
Mode: normal | hotfix (from branch: ${HOTFIX_BRANCH}) | isolated (base branch: ${BASE_BRANCH})
```
</ProjectDiscovery>

<PreReleaseChecks>
## STEP 2: Pre-Release Validation

**Run pre-release checks** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/pre_release_checks.sh
```

→ Report the full script output to the user. Stop if any check fails.

**Note**: This script is read-only — runs the same in dry-run mode.
</PreReleaseChecks>

<UpdateReadmesOnBase>
## STEP 3: Update READMEs (on ${BASE_BRANCH})

**IMPORTANT**: This step happens on `${BASE_BRANCH}` BEFORE creating the release branch. This ensures README updates are on `${BASE_BRANCH}` and included in the release branch.

**Only published READMEs matter here.** The root `README.md` in a workspace is NOT published to crates.io — only per-crate READMEs ship with `cargo publish`.

**This is an agent judgment step.** For each **published README** (discovered in STEP 1):
- Update compatibility/version information
- Update the version range for the current series (e.g., `0.17.0` → `0.17.0-0.17.1` for patch releases)
- Update example version numbers in installation instructions
- Any version-specific notes

**For the root README** (workspace projects only):
- Check if it contains hardcoded version numbers (e.g., in installation instructions, compatibility tables)
- If yes, update them. If no hardcoded versions (e.g., uses dynamic badges), skip it.

**If no published READMEs exist or none need changes**, report that and move on — do not prompt the user unnecessarily.

→ **Manual verification** (only if changes were made): Confirm all READMEs updated with new version info
  - Type **continue** to proceed
  - Type **skip** if no README changes needed

**If changes were made, commit on `${BASE_BRANCH}`** (skip commit in dry-run mode):
```bash
git add ${CHANGED_README_FILES}
git commit -m "docs: update compatibility tables for v${VERSION}"
```
</UpdateReadmesOnBase>

<FinalizeOnBase>
## STEP 4: Finalize Changelogs and Bump Versions (on ${BASE_BRANCH})

**This step creates the clean version commit on `${BASE_BRANCH}` before branching.**

### Verify Changelog Entries

**This is an agent judgment step.** For each discovered changelog file:

1. Display the content under `## [Unreleased]` to the user
2. Review the entries and verify:
   - Entries exist (not empty)
   - Entries accurately reflect the actual code changes since the last release
   - Categories are correct (Added, Changed, Fixed, Removed, etc.)
   - No significant changes are missing — cross-reference with `git log` since the last tag
   - Entries are clear and useful to someone reading the changelog
3. Report any concerns to the user

→ **Manual verification**: Verify all changelogs have accurate entries under `[Unreleased]`
  - Type **continue** to proceed
  - Type **stop** to add or fix entries

**Note**: For coordinated workspace releases where some crates have no feature changes, add:
```markdown
### Changed
- Version bump to X.Y.Z to maintain workspace version synchronization
```

### Finalize Changelogs

**Run changelog finalization:**
```bash
~/.claude/scripts/release/finalize_changelogs.sh ${VERSION} ${DRY_RUN_FLAG} ${ALL_CHANGELOG_FILES}
```

→ Report the script output to the user.

### Bump Versions

**Run version bumps** (only touches `[package] version` fields, not workspace dependency declarations):
```bash
~/.claude/scripts/release/bump_versions.sh ${VERSION} ${DRY_RUN_FLAG} ${ALL_VERSION_FILES}
```

→ Report the script output to the user.

### Update Cargo.lock

**Regenerate the lockfile** to reflect the bumped versions (skip in dry-run mode):
```bash
cargo update --workspace
```

This syncs `Cargo.lock` with the new `[package] version` fields. Without this step, `Cargo.lock` remains stale and the next cargo command (publish dry-run, etc.) would create uncommitted changes that break the process.

→ Report that the lockfile was updated.

### Commit

**Commit everything in a single clean commit with just the version as the message** (skip in dry-run mode):
```bash
git add ${ALL_CHANGELOG_FILES} ${ALL_VERSION_FILES} Cargo.lock
git commit -m "${VERSION}"
```

This produces a clean commit label visible in GitHub's file list for both CHANGELOG.md and Cargo.toml.
</FinalizeOnBase>

<CreateReleaseBranch>
## STEP 5: Create Release Branch

**Run branch creation:**
```bash
~/.claude/scripts/release/create_release_branch.sh ${VERSION} ${DRY_RUN_FLAG} ${PACKAGE_FLAG}
```

→ Report the script output to the user.

**Note**: All subsequent steps happen on this release branch (cut from `${BASE_BRANCH}`). `${BASE_BRANCH}` is done for now — it already has the finalized changelogs and bumped versions; STEP 10 and 11 return to it at the end.
</CreateReleaseBranch>

<PublishPhases>
## STEP 6: Publish to crates.io

**IMPORTANT**: When `[[publish_phases]]` config exists, dry-runs and publishing happen **per-phase**, not all-upfront. This is critical for workspace dependency chains where later phases depend on earlier phases being published first (e.g., Phase 2 crates may depend on Phase 1 crates via workspace dependencies that get updated between phases).

### If no config — single crate or simple workspace

**Dry-run** (with `dangerouslyDisableSandbox: true`):

Single crate:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```

Workspace (no config):
For each non-excluded workspace member:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```

→ Report all dry-run results to the user. Stop if any dry-run fails.

**If this is a dry-run release (`${DRY_RUN_FLAG}` is `--dry-run`), skip the rest of STEP 6 — do not publish.**

→ **Manual confirmation required**: All dry-run checks passed. Type **publish** to publish to crates.io. This is irreversible.

**Publish** (with `dangerouslyDisableSandbox: true`):

Single crate:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```

Workspace:
For each non-excluded workspace member:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```
→ Members are published in `[workspace.members]` order. If this fails due to dependency ordering, the project needs either `workspace_publish = true` (cargo >= 1.90, no between-phase scripts) or a `[[publish_phases]]` config.

### If config sets `workspace_publish = true`

Publish every member in a single cargo invocation. Cargo resolves intra-workspace dependency order itself and verifies later crates against a locally patched registry, so members that depend on not-yet-published siblings dry-run cleanly — no indexing waits or ordering config needed. This requires cargo >= 1.90 (check `cargo --version`; if older, fall back to the no-config per-crate flow and tell the user).

Build `${EXCLUDE_FLAGS}` as one `--exclude <name>` per member that STEP 1 discovery excluded (test/example/benchmark paths). `publish = false` members are skipped by cargo automatically. Single-package mode ignores `workspace_publish` entirely (it uses the single-crate flow).

**Dry-run** (with `dangerouslyDisableSandbox: true`):
```bash
cargo publish --workspace --dry-run ${EXCLUDE_FLAGS}
```

→ Report dry-run results to the user. Stop if the dry-run fails.

**If this is a dry-run release (`${DRY_RUN_FLAG}` is `--dry-run`), skip the rest of STEP 6 — do not publish.**

→ **Manual confirmation required**: Dry-run checks passed. Type **publish** to publish to crates.io. This is irreversible.

**Publish** (with `dangerouslyDisableSandbox: true`):
```bash
cargo publish --workspace ${EXCLUDE_FLAGS}
```

→ Cargo publishes members in dependency order and waits for each upload before the next. Report the output to the user. Stop if it fails.

### If `[[publish_phases]]` config exists

Execute each phase in order. **Each phase does its own dry-run before publishing.**

**For each phase:**

1. Display: `Publishing phase: ${PHASE_NAME}`

2. **Dry-run** this phase's crates (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```
→ Report dry-run results. Stop if any dry-run fails.

3. **If this is a dry-run release (`${DRY_RUN_FLAG}` is `--dry-run`)**: report what would be published for remaining phases, then skip the rest of STEP 6 — do not publish.

4. **Hard stop before first publish only** (skip for subsequent phases):
→ **Manual confirmation required**: Dry-run checks passed. Type **publish** to publish to crates.io. This is irreversible.

5. **Publish** this phase's crates (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```
→ Report the script output to the user. Stop if any publish fails.

6. If `wait_seconds` is set:
```bash
echo "Waiting ${WAIT_SECONDS} seconds for crates.io indexing..."
sleep ${WAIT_SECONDS}
```

7. If `workspace_dep_updates` is set, update the named workspace dependencies to the release version (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/update_workspace_deps.sh ${VERSION} ${DRY_RUN_FLAG} ${DEP_NAMES}
```
→ This updates `[workspace.dependencies]` entries in root `Cargo.toml`, waits for crates.io indexing with fibonacci backoff, verifies the build, and commits. Report the script output. Stop if it fails.

8. If `post_script` is set, execute the project-specific script (with `dangerouslyDisableSandbox: true`):
```bash
${POST_SCRIPT_PATH} ${VERSION}
```
→ Report the script output to the user. Stop if script fails.

→ **Then proceed to the next phase** (which will dry-run against the now-updated workspace state).
</PublishPhases>

<PushReleaseBranch>
## STEP 7: Push Release Branch and Tag

**Run push** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/push_release.sh ${VERSION} ${DRY_RUN_FLAG} ${PACKAGE_FLAG}
```

→ Report the script output to the user. Stop if push fails.
</PushReleaseBranch>

<CreateGitHubRelease>
## STEP 8: Create GitHub Release

**This is an agent judgment step.** Gather CHANGELOG entries from all changelog files for version `${VERSION}` and combine them into release notes.

For workspace projects with multiple changelogs, format as:
```markdown
## crate_name
<entries from that crate's changelog>

## other_crate_name
<entries from that crate's changelog>
```

For single-crate projects, use the entries directly. In single-package mode, use the selected crate's entries directly (treat it like a single-crate project).

**Write the release notes and run the script in a single unsandboxed command** (combining them avoids sandbox `$TMPDIR` mismatches):
```bash
NOTES_FILE="$TMPDIR/release-notes-${VERSION}.md"
cat > "$NOTES_FILE" << 'NOTES_EOF'
<release notes content here>
NOTES_EOF
~/.claude/scripts/release/create_github_release.sh ${VERSION} ${GITHUB_REPO} ${PROJECT_NAME} "$NOTES_FILE" ${DRY_RUN_FLAG} ${PACKAGE_FLAG}
```
→ This must use `dangerouslyDisableSandbox: true`. Report the script output to the user.
</CreateGitHubRelease>

<PostReleaseVerification>
## STEP 9: Post-Release Verification

**Run verification** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/verify_published.sh ${VERSION} ${ALL_CRATE_NAMES}
```

→ Report the script output to the user.
  - If any crate doesn't show the correct version, ask the user to **retry** or **continue**

**If `install_verify` is set in config** (skip in dry-run mode, with `dangerouslyDisableSandbox: true`):

If `install_verify_script` is also set, run the custom script instead of the default `cargo install`:
```bash
${INSTALL_VERIFY_SCRIPT} ${VERSION}
```

Otherwise, use the default:
```bash
cargo install ${INSTALL_CRATE_NAME} --version "${VERSION}"
```
→ Report success or failure. Stop if installation fails.

**Note**: `verify_published.sh` is read-only — runs the same in dry-run mode. `cargo install` is skipped in dry-run mode.
</PostReleaseVerification>

<RestoreUnreleasedSections>
## STEP 10: Restore [Unreleased] Sections

**This step always runs** (normal and isolated modes) — after STEP 4 finalized the changelogs on `${BASE_BRANCH}`, they need `[Unreleased]` sections added back. The script checks out `${BASE_BRANCH}`, restores the sections, commits, and pushes `${BASE_BRANCH}`.

**Run restore:**
```bash
~/.claude/scripts/release/restore_unreleased.sh ${VERSION} ${DRY_RUN_FLAG} --base-branch ${BASE_BRANCH} ${ALL_CHANGELOG_FILES}
```

→ Report the script output to the user.
</RestoreUnreleasedSections>

<RestoreDevVersion>
## STEP 11: Restore Dev Version on ${BASE_BRANCH}

**This step always runs** (normal and isolated modes). Step 4 set versions to the release version on `${BASE_BRANCH}` before branching. `${BASE_BRANCH}` must be restored to a dev version.

**Determine the dev version to restore:**
- **Patch release** (`/release patch`): Restore the dev version that was on `${BASE_BRANCH}` *before* Step 4 changed it. This is the version from the commit prior to the changelog/version bump commit. Check `git show HEAD~1:${FIRST_VERSION_FILE}` to find it. If it was already a dev version (e.g., `0.19.0-dev`), use that. If it wasn't (indicating a prior release also missed this step), determine the correct next dev version as below.
- **Minor/major release** (e.g., `0.18.0`): Next dev is `X.Y+1.0-dev`
- **RC release** (e.g., `0.18.0-rc.1`): Next dev is `X.Y.Z-dev`

→ **Ask the user**: The dev version will be set to `${NEXT_DEV_VERSION}`. Confirm or provide a different version.

**Run version bump:**
```bash
~/.claude/scripts/release/bump_versions.sh ${NEXT_DEV_VERSION} ${DRY_RUN_FLAG} ${ALL_VERSION_FILES}
```

**Regenerate the lockfile** to reflect the dev versions (skip in dry-run mode):
```bash
cargo update --workspace
```

**Update workspace dependencies** (if config has `workspace_dep_updates`):
Any `workspace_dep_updates` entries from `[[publish_phases]]` in the config represent internal cross-crate dependencies declared in root `Cargo.toml`. These must also be updated to the dev version, otherwise the workspace won't build.

For each unique dependency name across all publish phases' `workspace_dep_updates`:
- Update root `Cargo.toml` `[workspace.dependencies]` entry to `version = "${NEXT_DEV_VERSION}"`
- This is a simple text replacement — no crates.io waiting needed (unlike during publish)

```bash
# For each workspace dep, update the version in root Cargo.toml
sed -i'' "s/bevy_brp_mcp_macros = \".*\"/bevy_brp_mcp_macros = \"${NEXT_DEV_VERSION}\"/" Cargo.toml
```

→ Verify `cargo check` passes after updating (skip in dry-run mode).

**Commit and push** (skip in dry-run mode; push with `dangerouslyDisableSandbox: true`):
```bash
git add ${ALL_VERSION_FILES} Cargo.toml Cargo.lock
git commit -m "chore: bump to ${NEXT_DEV_VERSION}"
git push origin ${BASE_BRANCH}
```

→ Report the script output to the user.

**Release complete!** All crates published from release branch. Release branch is fire-and-forget. `${BASE_BRANCH}` now at next dev version. In isolated mode, main was never touched — merge `${BASE_BRANCH}` into main yourself when ready.
</RestoreDevVersion>

<FinalizeOnHotfixBranch>
## STEP 4 (Hotfix): Finalize Changelogs and Bump Versions (on hotfix branch)

**This step runs on the current hotfix branch instead of main.**

### Verify Changelog Entries

**This is an agent judgment step.** For each discovered changelog file:

1. Display the content under `## [Unreleased]` to the user
2. Review the entries and verify:
   - Entries exist (not empty)
   - Entries accurately reflect the actual code changes in the hotfix
   - Categories are correct (Added, Changed, Fixed, Removed, etc.)
   - Entries are clear and useful to someone reading the changelog
3. Report any concerns to the user

→ **Manual verification**: Verify all changelogs have accurate entries under `[Unreleased]`
  - Type **continue** to proceed
  - Type **stop** to add or fix entries

**Note**: For coordinated workspace releases where some crates have no feature changes, add:
```markdown
### Changed
- Version bump to X.Y.Z to maintain workspace version synchronization
```

### Finalize Changelogs

**Run changelog finalization:**
```bash
~/.claude/scripts/release/finalize_changelogs.sh ${VERSION} ${DRY_RUN_FLAG} ${ALL_CHANGELOG_FILES}
```

→ Report the script output to the user.

### Bump Versions

**Run version bumps** (only touches `[package] version` fields, not workspace dependency declarations):
```bash
~/.claude/scripts/release/bump_versions.sh ${VERSION} ${DRY_RUN_FLAG} ${ALL_VERSION_FILES}
```

→ Report the script output to the user.

### Update Cargo.lock

**Regenerate the lockfile** to reflect the bumped versions (skip in dry-run mode):
```bash
cargo update --workspace
```

This syncs `Cargo.lock` with the new `[package] version` fields. Without this step, `Cargo.lock` remains stale and the next cargo command (publish dry-run, etc.) would create uncommitted changes that break the process.

→ Report that the lockfile was updated.

### Commit

**Commit everything in a single clean commit with just the version as the message** (skip in dry-run mode):
```bash
git add ${ALL_CHANGELOG_FILES} ${ALL_VERSION_FILES} Cargo.lock
git commit -m "${VERSION}"
```

### Rename Branch

**Rename the hotfix branch to the standard release naming convention** (with `dangerouslyDisableSandbox: true`). In single-package mode the release branch is `release-${PACKAGE}-${VERSION}` — use that name in place of `release-${VERSION}` here and in every later step (push, cleanup):
```bash
git branch -m ${HOTFIX_BRANCH} release-${VERSION}
```

→ Report: "Renamed branch `${HOTFIX_BRANCH}` → `release-${VERSION}`"
→ Update `${HOTFIX_BRANCH}` to `release-${VERSION}` for subsequent steps (push, cleanup).
</FinalizeOnHotfixBranch>

<HotfixCleanup>
## STEP 12: Hotfix Cleanup (cherry-pick to main)

**This step only runs in hotfix mode.** It ensures the hotfix changes reach main.

### Identify commits to cherry-pick

Find all commits on the release branch that are not on the parent tag. The branch was created from a release tag, so identify the fix commits (excluding the version bump and workspace dep update commits which main doesn't need):

```bash
git log --oneline release-${VERSION} --not $(git describe --tags --abbrev=0 release-${VERSION}~1)
```

**In single-package mode**, the branch is `release-${PACKAGE}-${VERSION}` and tags are package-prefixed, so add `--match "${PACKAGE}-v*"` to `git describe` to find the crate's own previous tag:
```bash
git log --oneline release-${PACKAGE}-${VERSION} --not $(git describe --tags --abbrev=0 --match "${PACKAGE}-v*" release-${PACKAGE}-${VERSION}~1)
```

The fix commit(s) are everything before the version bump commit. The version bump commit itself and any workspace dependency update commits should NOT be cherry-picked — main has its own version management.

### Cherry-pick to main

Switch to main and cherry-pick the fix commit(s) using `--no-commit` to allow version restoration before committing (with `dangerouslyDisableSandbox: true`):
```bash
git checkout main
git pull origin main
git cherry-pick --no-commit ${FIX_COMMIT_HASHES}
```

→ If cherry-pick has conflicts, report them to the user and help resolve them interactively.
→ If the fix commit already exists on main (e.g., from a prior cherry-pick attempt), skip and note it.

### Restore main's version state

The cherry-pick may have overwritten main's dev versions (e.g., `0.19.0-dev`) with the release branch's versions (e.g., `0.18.8`). Restore all version files and Cargo.lock to their pre-cherry-pick state:

```bash
git checkout HEAD -- ${ALL_VERSION_FILES} Cargo.lock
```

Check if the staging area still has changes (the fix itself, minus version files):
```bash
git diff --cached --stat
```

→ If no changes remain after restoring versions, the fix commit only touched version files — skip committing and report this to the user.

**Commit the cherry-pick** with the original commit message:
```bash
git commit -m "fix: <original commit message from fix commit>"
```

### User verification

→ **Manual verification required**: The hotfix has been cherry-picked to main. Please verify:
  - The cherry-pick applied cleanly (or conflicts were resolved correctly)
  - Version files still have the correct dev versions (e.g., `0.19.0-dev`)
  - `cargo build` succeeds
  - The changes look correct on main

  Type **verified** to proceed.
  Type **abort** to stop (you can clean up manually later).

**Hotfix release complete!** Published from release branch, changes cherry-picked to main. The `release-${VERSION}` branch remains on local and remote as a permanent record, same as all release branches.
</HotfixCleanup>

## Rollback Instructions

If something goes wrong after pushing but before publishing:

```bash
# Delete local tag
git tag -d "v${VERSION}"

# Delete remote tag
git push origin ":refs/tags/v${VERSION}"

# Delete release branch
git branch -D release-${VERSION}
git push origin :release-${VERSION}

# Return to main
git checkout main
```

**Single-package releases** use the package-scoped names instead: tag `${PACKAGE}-v${VERSION}` and branch `release-${PACKAGE}-${VERSION}`. Substitute those in the commands above.

If already published to crates.io, you cannot unpublish. Release a new patch version instead.

## Common Issues

1. **"Version already exists"**: Already published on crates.io
2. **"Version gap"**: Skipped a version number — use the next sequential version
3. **"Uncommitted changes"**: Run `git status` and commit or stash changes
4. **"Not on main branch"**: Releasing from a non-main branch makes STEP 1 prompt for **hotfix** vs **isolated** mode — if intentional, pick the right mode. If not, switch to main with `git checkout main`
5. **Build failures**: Fix compilation errors before releasing
6. **Workspace dependency ordering**: If publish fails due to dependency ordering, set `workspace_publish = true` in `.claude/config/release.toml` (cargo >= 1.90, members publish together, no between-phase scripts) or add `[[publish_phases]]` (ordered chains needing scripts or dep rewrites between phases, e.g. bevy_brp)
7. **crates.io indexing delay**: If a later phase fails because a just-published crate isn't indexed yet, increase `wait_seconds` in the config
8. **Missing post_script**: The config references a script that doesn't exist — create it or fix the path
