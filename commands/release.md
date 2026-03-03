# Unified Release Command

Perform a release for any Rust crate or workspace project.

## Usage
- `/release` or `/release help` - Show this usage information
- `/release patch` - Auto-detect latest published version and increment patch
- `/release patch dry-run` - Rehearse patch release without mutations
- `/release X.Y.Z` - Release as final version (e.g., `0.18.0`)
- `/release X.Y.Z-rc.N` - Release as RC version (e.g., `0.18.0-rc.1`)
- `/release X.Y.Z dry-run` - Rehearse release without mutations

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

## Configuration

The command works with **zero config** by convention. It auto-detects:
- Single crate vs workspace from root `Cargo.toml`
- Crate names from each `Cargo.toml` `[package].name`
- Version files: `{crate_dir}/Cargo.toml`
- Changelogs: `{crate_dir}/CHANGELOG.md`
- READMEs: Root `README.md` plus `{crate_dir}/README.md` if they exist
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
      [ ] Phase 1: Publish macros → update_workspace_dep.sh
      [ ] Phase 2: Publish extras and mcp
[ ] STEP 7:  Push Release Branch and Tag
[ ] STEP 8:  Create GitHub Release
[ ] STEP 9:  Post-Release Verification + install verify (mcp)
[ ] STEP 10: Restore [Unreleased] Sections
[ ] STEP 11: Bump to Next Dev Version
═══════════════════════════════════════════════════════════════
```

**BEFORE EACH STEP**: Re-render the full checklist showing:
- `[x]` for completed steps (and their sub-items)
- `[>]` for the step about to start
- `[ ]` for pending steps
- `[—]` for skipped steps (e.g., STEP 10 on patch releases)

**AFTER FINAL STEP**: Re-render one last time with all steps showing `[x]` (or `[—]` if skipped).
</ProgressBehavior>

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <ArgumentValidation/>
    **STEP 1:** Execute <ProjectDiscovery/>

    Display <ProgressBehavior/> full list (requires project discovery to have completed first), then proceed:
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
    **STEP 11:** Execute <BumpToNextDev/> **(Skip if patch release)**

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
- README: `README.md`

**Workspace** (`[workspace]` section present):
- Read `[workspace.members]` to find crate directories
- Exclude any member that contains `test`, `example`, or `benchmark` in its path
- For each crate directory:
  - Read crate name from `{dir}/Cargo.toml` `[package].name`
  - Version file: `{dir}/Cargo.toml`
  - Changelog: `{dir}/CHANGELOG.md` (if exists)
  - README: `{dir}/README.md` (if exists)
- Root README: `README.md` (always included)

**Detect GitHub repo** (uses git remote to avoid sandbox TLS issues with `gh`):
```bash
git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||'
```

**Display discovered project info:**
```
Project: ${GITHUB_REPO}
Type: single crate | workspace
Crates: ${CRATE_LIST}
Config: found | using defaults
Dry-run: yes | no
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

**This is an agent judgment step.** Update compatibility/version information in all discovered README files:
- Update the version range for the current series (e.g., `0.17.0` → `0.17.0-0.17.1` for patch releases)
- Update example version numbers in installation instructions
- Any version-specific notes

→ **Manual verification**: Confirm all READMEs updated with new version info
  - Type **continue** to proceed
  - Type **skip** if no README changes needed

**If changes were made, commit on main** (skip commit in dry-run mode):
```bash
git add ${ALL_README_FILES}
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

### Dry-Run All Crates First

Run `publish_crate.sh --dry-run` for every crate to verify they all pass before publishing any of them (with `dangerouslyDisableSandbox: true`):

**If `[[publish_phases]]` config exists:**
For each phase in order, for each crate in the phase:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```

**If no config — single crate:**
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```

**If no config — workspace:**
For each non-excluded workspace member:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME} --dry-run
```

→ Report all dry-run results to the user. Stop if any dry-run fails.

**If this is a dry-run release (`${DRY_RUN_FLAG}` is `--dry-run`), skip the rest of STEP 6 — do not publish.**

### Hard Stop Before Publishing

→ **Manual confirmation required**: All dry-run checks passed. Type **publish** to publish to crates.io. This is irreversible.

### Publish

**If `[[publish_phases]]` config exists:**

Execute each phase in order:

**For each phase:**
1. Display: `Publishing phase: ${PHASE_NAME}`
2. For each crate in the phase, publish (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```
→ Report the script output to the user. Stop if any publish fails.

3. If `wait_seconds` is set:
```bash
echo "Waiting ${WAIT_SECONDS} seconds for crates.io indexing..."
sleep ${WAIT_SECONDS}
```

4. If `post_script` is set, execute the project-specific script (with `dangerouslyDisableSandbox: true`):
```bash
${POST_SCRIPT_PATH} ${VERSION}
```
→ Report the script output to the user. Stop if script fails.

**If no config — single crate** (with `dangerouslyDisableSandbox: true`):
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```

**If no config — workspace** (with `dangerouslyDisableSandbox: true`):
For each non-excluded workspace member:
```bash
~/.claude/scripts/release/publish_crate.sh ${PACKAGE_NAME}
```
→ Members are published in `[workspace.members]` order. If this fails due to dependency ordering, the project needs a `[[publish_phases]]` config.
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
## STEP 11: Bump to Next Dev Version

**Skip this step if this is a patch release** — main is already at the correct dev version.

**Determine next dev version:**
- If released `X.Y.Z-rc.N`, next dev is `X.Y.Z-dev`
- If released final `X.Y.Z`, next dev is `X.Y+1.0-dev`

→ **Ask the user**: What should the next dev version be? (with the above as the suggested default)

**Run version bump:**
```bash
~/.claude/scripts/release/bump_versions.sh ${NEXT_DEV_VERSION} ${DRY_RUN_FLAG} ${ALL_VERSION_FILES}
```

**Commit and push** (skip in dry-run mode; push with `dangerouslyDisableSandbox: true`):
```bash
git add ${ALL_VERSION_FILES} Cargo.lock
git commit -m "chore: bump to ${NEXT_DEV_VERSION}"
git push origin main
```

→ Report the script output to the user.

**Release complete!** All crates published from release branch. Release branch is fire-and-forget. Main now at next dev version.
</BumpToNextDev>

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
4. **"Not on main branch"**: Switch to main with `git checkout main`
5. **Build failures**: Fix compilation errors before releasing
6. **Workspace dependency ordering**: If publish fails due to dependency ordering, add `[[publish_phases]]` to `.claude/config/release.toml`
7. **crates.io indexing delay**: If a later phase fails because a just-published crate isn't indexed yet, increase `wait_seconds` in the config
8. **Missing post_script**: The config references a script that doesn't exist — create it or fix the path
