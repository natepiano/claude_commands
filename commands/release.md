# Release Command - cargo-release Integration

Perform a coordinated release for bevy_brp workspace crates using cargo-release. This command ensures synchronized versions and safe publishing to crates.io.

## Usage
- `/release <version>` - Release both crates with synchronized version
- `/release <version> <crate_name>` - Release a single crate independently

Examples:
- `/release 0.3.0` - Release both bevy_brp_extras and bevy_brp_mcp as v0.3.0
- `/release 0.3.0 bevy_brp_extras` - Release only bevy_brp_extras

## Prerequisites Check

Before starting the release, verify:
1. You're on the `main` branch
2. Working directory is clean (no uncommitted changes)
3. You're up to date with remote
4. cargo-release is installed (`cargo install cargo-release`)

## Step 1: Pre-Release Validation

Run these checks in order:

```bash
# 1. Verify git state
git status
git fetch origin
git log --oneline -5

# 2. Build all workspace members
cargo build --all

# 3. Run all tests
cargo test --all

# 4. Format check
cargo +nightly fmt --all -- --check

# 5. Clippy check
cargo clippy --all-targets --all-features -- -D warnings
```

## Step 2: Verify CHANGELOG Entries

Check that both crates have unreleased changes documented:

```bash
# Check for [Unreleased] or version entries in CHANGELOGs
head -20 mcp/CHANGELOG.md
head -20 extras/CHANGELOG.md
```

Ensure:
- Both have entries under the target version (or [Unreleased])
- Changes are properly categorized (Added/Changed/Fixed/etc.)
- Dates are correct

## Step 3: Dry Run

**ALWAYS perform a dry run first:**

For synchronized release (both crates):
```bash
cargo release <version> --workspace --dry-run
```

For single crate:
```bash
cargo release <version> --package <crate_name> --dry-run
```

Review the dry run output carefully:
- Version bumps are correct
- CHANGELOG modifications look right
- Git operations are as expected
- Publishing order is correct (extras before mcp)

## Step 4: Execute Release

After confirming the dry run looks correct:

For synchronized release:
```bash
# This will:
# 1. Update versions in Cargo.toml files
# 2. Update CHANGELOG.md files
# 3. Create git commit
# 4. Create git tags (bevy_brp_extras-vX.Y.Z and bevy_brp_mcp-vX.Y.Z)
cargo release <version> --workspace --execute
```

For single crate:
```bash
cargo release <version> --package <crate_name> --execute
```

## Step 5: Push Changes

After cargo-release completes successfully:

```bash
# Push the commits
git push origin main

# Push the tags
git push origin --tags
```

## Step 6: Publish to crates.io

**IMPORTANT**: Publish in dependency order (extras first, then mcp)

```bash
# 1. Publish bevy_brp_extras first
cd extras
cargo publish

# Wait for it to be available on crates.io (usually ~1 minute)
# You can check: https://crates.io/crates/bevy_brp_extras

# 2. Then publish bevy_brp_mcp
cd ../mcp
cargo publish
```

## Step 7: Create GitHub Releases

For each crate that was released:

1. Go to https://github.com/natepiano/bevy_brp/releases/new
2. Choose the appropriate tag (e.g., `bevy_brp_extras-v0.3.0`)
3. Title: `bevy_brp_extras v0.3.0` (or appropriate crate/version)
4. Copy the CHANGELOG entries for this version
5. Publish release

## Step 8: Post-Release Verification

Verify the release was successful:

```bash
# Check crates.io
curl -s https://crates.io/api/v1/crates/bevy_brp_extras | jq '.crate.max_version'
curl -s https://crates.io/api/v1/crates/bevy_brp_mcp | jq '.crate.max_version'

# Verify in a test project
cd /tmp
cargo new test_brp_release
cd test_brp_release
cargo add bevy_brp_extras@<version>
cargo add bevy_brp_mcp@<version>
cargo build
```

## Rollback Instructions

If something goes wrong after pushing but before publishing:

```bash
# Delete local tags
git tag -d bevy_brp_extras-v<version>
git tag -d bevy_brp_mcp-v<version>

# Delete remote tags
git push origin :refs/tags/bevy_brp_extras-v<version>
git push origin :refs/tags/bevy_brp_mcp-v<version>

# Revert the version bump commit
git revert HEAD
git push origin main
```

## Configuration Notes

The workspace uses `release.toml` with:
- `shared-version = true` for synchronized releases
- Custom tag format: `{{crate_name}}-v{{version}}`
- Pre-release hook runs `cargo build --all`
- Manual push/publish for safety
- Test apps excluded via `[package.metadata.release] release = false`

To release crates independently, temporarily set `shared-version = false` in release.toml.

## Common Issues

1. **"Version already exists"**: The version is already published on crates.io
2. **"Uncommitted changes"**: Run `git status` and commit or stash changes
3. **"Not on main branch"**: Switch to main with `git checkout main`
4. **Build failures**: Fix any compilation errors before releasing
5. **Dependency order**: Always publish bevy_brp_extras before bevy_brp_mcp

## Conventional Commits

For better changelog generation, use these commit prefixes:
- `feat:` - New features (bumps MINOR)
- `fix:` - Bug fixes (bumps PATCH)
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Test additions/changes
- `chore:` - Maintenance tasks
- `BREAKING CHANGE:` - Breaking changes (bumps MAJOR)