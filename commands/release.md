# Unified Release Command

Perform a release for any Rust crate or workspace project.

## Usage
- `/release` or `/release help` - Show this usage information
- `/release patch` - Auto-detect latest published version and increment patch
- `/release patch dry-run` - Rehearse patch release without mutations
- `/release X.Y.Z` - Release as final version (e.g., `0.18.0`)
- `/release X.Y.Z-rc.N` - Release as RC version (e.g., `0.18.0-rc.1`)
- `/release X.Y.Z dry-run` - Rehearse release without mutations

**Hotfix mode**: Auto-detected when the current branch is not `main`. Skips steps that modify main (README updates, branch creation, unreleased restore, dev version bump) and adds a post-release cherry-pick cleanup step.

**If `$ARGUMENTS` is empty or `help`**: First read `.claude/config/release.toml` if it exists and display the progress checklist (with config-aware sub-items), then display the usage block above, then stop. Do not proceed with any release steps beyond displaying the checklist and usage.

## Dry-Run Mode

When `dry-run` is present in `$ARGUMENTS`, set `${DRY_RUN_FLAG}` to `--dry-run` (otherwise empty string).

**All mutating scripts accept `--dry-run`** as a parameter. In dry-run mode, pass `${DRY_RUN_FLAG}` to every script call. The scripts themselves handle reporting what they would do without making changes.

For agent judgment steps (README updates, changelog review, GitHub release notes), describe what would be done without making changes. For git commits, report what would be committed without running the command.

## Versioning Strategy

All projects use a **branch-first release model**:
- **main branch**: Always has `-dev` versions (e.g., `0.18.0-dev`)
- **release branches**: Created BEFORE publishing, contain actual release versions
- **Publishing**: Always happens from release branches, never from main
- **No merge back**: Release branches are fire-and-forget snapshots
- **Hotfix releases**: When releasing from a non-main branch (e.g., a branch based on a previous release tag), the command auto-detects hotfix mode. Changes are published from the current branch, then cherry-picked back to main with user verification before the hotfix branch is cleaned up.

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
- `changelogs_finalized` — after changelogs finalized on main
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
7. Display the full checklist

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
[ ] STEP 11: Bump to Next Dev Version
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
[ ] STEP 11: Bump to Next Dev Version
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
[—] STEP 11: Bump to Next Dev Version (skipped — hotfix)
[ ] STEP 12: Cherry-pick to main
═══════════════════════════════════════════════════════════════
```

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

    **If normal mode (on main):**

    **STEP 2:** Execute <PreReleaseChecks/>
    → Checkpoint: `quality_checks_complete`
    **STEP 3:** Execute <UpdateReadmesOnMain/>
    → Checkpoint: `readmes_updated`
    **STEP 4:** Execute <FinalizeOnMain/>
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
    **STEP 11:** Execute <BumpToNextDev/>

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

**If argument is `patch` (with or without `dry-run`):**
1. Detect the primary crate name (single crate: from root `Cargo.toml`; workspace: from first `[workspace.members]` entry or first publish phase crate if config exists)
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

**Read optional config** from `.claude/config/release.toml` if it exists. Store any `install_verify`, `publish_phases`, and `judgment_checks` settings.

**If config references any `post_script` files, verify they exist:**
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
- If branch is `main`: **normal mode**
- If branch starts with `release-`: **possible stale release branch** — warn the user: "You are on branch `${BRANCH}` which looks like an existing release branch. Are you sure this is a hotfix? Type **yes** to continue in hotfix mode or **no** to abort." Stop if user says no.
- If branch is anything else: **hotfix mode** — set `${HOTFIX_MODE}` to `true` and `${HOTFIX_BRANCH}` to the branch name

**Display discovered project info:**
```
Project: ${GITHUB_REPO}
Type: single crate | workspace
Crates: ${CRATE_LIST}
Config: found | using defaults
Dry-run: yes | no
Mode: normal | hotfix (from branch: ${HOTFIX_BRANCH})
```
</ProjectDiscovery>

<PreReleaseChecks>
## STEP 2: Pre-Release Validation (on main)

**Run pre-release checks** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/pre_release_checks.sh
```

→ Report the full script output to the user. Stop if any check fails.

**Note**: This script is read-only — runs the same in dry-run mode.
</PreReleaseChecks>

<UpdateReadmesOnMain>
## STEP 3: Update READMEs (on main)

**IMPORTANT**: This step happens on main BEFORE creating the release branch. This ensures README updates are on main and included in the release branch.

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

**If changes were made, commit on main** (skip commit in dry-run mode):
```bash
git add ${CHANGED_README_FILES}
git commit -m "docs: update compatibility tables for v${VERSION}"
```
</UpdateReadmesOnMain>

<FinalizeOnMain>
## STEP 4: Finalize Changelogs and Bump Versions (on main)

**This step creates the clean version commit on main before branching.**

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

### Commit

**Commit everything in a single clean commit with just the version as the message** (skip in dry-run mode):
```bash
git add ${ALL_CHANGELOG_FILES} ${ALL_VERSION_FILES} Cargo.lock
git commit -m "${VERSION}"
```

This produces a clean commit label visible in GitHub's file list for both CHANGELOG.md and Cargo.toml.
</FinalizeOnMain>

<CreateReleaseBranch>
## STEP 5: Create Release Branch

**Run branch creation:**
```bash
~/.claude/scripts/release/create_release_branch.sh ${VERSION} ${DRY_RUN_FLAG}
```

→ Report the script output to the user.

**Note**: All subsequent steps happen on this release branch. Main is done — it already has the finalized changelogs and bumped versions.
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
→ Members are published in `[workspace.members]` order. If this fails due to dependency ordering, the project needs a `[[publish_phases]]` config.

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
~/.claude/scripts/release/push_release.sh ${VERSION} ${DRY_RUN_FLAG}
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

For single-crate projects, use the entries directly.

**Write the combined release notes to a temp file**, then run the script with `dangerouslyDisableSandbox: true`:
```bash
~/.claude/scripts/release/create_github_release.sh ${VERSION} ${GITHUB_REPO} ${PROJECT_NAME} ${NOTES_FILE} ${DRY_RUN_FLAG}
```
→ Report the script output to the user.
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
```bash
cargo install ${INSTALL_CRATE_NAME} --version "${VERSION}"
```
→ Report success or failure. Stop if installation fails.

**Note**: `verify_published.sh` is read-only — runs the same in dry-run mode. `cargo install` is skipped in dry-run mode.
</PostReleaseVerification>

<RestoreUnreleasedSections>
## STEP 10: Restore [Unreleased] Sections

**This step always runs** — after STEP 4 finalized the changelogs on main, they need `[Unreleased]` sections added back.

**Run restore:**
```bash
~/.claude/scripts/release/restore_unreleased.sh ${VERSION} ${DRY_RUN_FLAG} ${ALL_CHANGELOG_FILES}
```

→ Report the script output to the user.
</RestoreUnreleasedSections>

<BumpToNextDev>
## STEP 11: Restore Dev Version on Main

**This step always runs.** Step 4 set versions to the release version on main before branching. Main must be restored to a dev version.

**Determine the dev version to restore:**
- **Patch release** (`/release patch`): Restore the dev version that was on main *before* Step 4 changed it. This is the version from the commit prior to the changelog/version bump commit. Check `git show HEAD~1:${FIRST_VERSION_FILE}` to find it. If it was already a dev version (e.g., `0.19.0-dev`), use that. If it wasn't (indicating a prior release also missed this step), determine the correct next dev version as below.
- **Minor/major release** (e.g., `0.18.0`): Next dev is `X.Y+1.0-dev`
- **RC release** (e.g., `0.18.0-rc.1`): Next dev is `X.Y.Z-dev`

→ **Ask the user**: The dev version will be set to `${NEXT_DEV_VERSION}`. Confirm or provide a different version.

**Run version bump:**
```bash
~/.claude/scripts/release/bump_versions.sh ${NEXT_DEV_VERSION} ${DRY_RUN_FLAG} ${ALL_VERSION_FILES}
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
git push origin main
```

→ Report the script output to the user.

**Release complete!** All crates published from release branch. Release branch is fire-and-forget. Main now at next dev version.
</BumpToNextDev>

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

### Commit

**Commit everything in a single clean commit with just the version as the message** (skip in dry-run mode):
```bash
git add ${ALL_CHANGELOG_FILES} ${ALL_VERSION_FILES} Cargo.lock
git commit -m "${VERSION}"
```

### Rename Branch

**Rename the hotfix branch to the standard release naming convention** (with `dangerouslyDisableSandbox: true`):
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

If already published to crates.io, you cannot unpublish. Release a new patch version instead.

## Common Issues

1. **"Version already exists"**: Already published on crates.io
2. **"Version gap"**: Skipped a version number — use the next sequential version
3. **"Uncommitted changes"**: Run `git status` and commit or stash changes
4. **"Not on main branch"**: Hotfix mode is auto-detected — if intentional, proceed. If not, switch to main with `git checkout main`
5. **Build failures**: Fix compilation errors before releasing
6. **Workspace dependency ordering**: If publish fails due to dependency ordering, add `[[publish_phases]]` to `.claude/config/release.toml`
7. **crates.io indexing delay**: If a later phase fails because a just-published crate isn't indexed yet, increase `wait_seconds` in the config
8. **Missing post_script**: The config references a script that doesn't exist — create it or fix the path
