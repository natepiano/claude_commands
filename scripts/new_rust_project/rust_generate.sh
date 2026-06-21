#!/usr/bin/env bash
# Generate a Rust project from the local rust-template checkout.
#
# Always generates from the local working tree at ~/rust/rust-template
# (cargo generate --path), so unpushed template edits take effect immediately.
#
# Two modes:
#   standalone (default)        -> ~/rust/<name>, own git repo, standalone CI
#   workspace member (--workspace-root) -> <root>/crates/<name>, thin manifest,
#                                          wired into the host workspace
#
# NOTE: `gh` commands require dangerouslyDisableSandbox: true in Claude Code.
set -euo pipefail

TEMPLATE_DIR="$HOME/rust/rust-template"

usage() {
  cat >&2 <<'EOF'
Usage: rust_generate.sh <name> [options]

Common:
  --lib                     Generate a library crate (default: bin)
  --no-bevy                 Skip Bevy support

Workspace member (omit for a standalone ~/rust/<name> project):
  --workspace-root <path>   Generate <path>/crates/<name> as a member of the
                            Cargo workspace rooted at <path>
  --published               Per-crate version (publishes to crates.io on its own
                            cadence). Default: inherit version.workspace.
  --description <text>      Crate description for the thin manifest
  --keywords <csv>          Comma-separated keywords (required; clippy denies empty)
  --categories <csv>        Comma-separated crates.io category slugs (required)
  --shared-dep              Register <name> = { path = "crates/<name>" } in the
                            workspace's [workspace.dependencies]
EOF
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

NAME="$1"
shift

CRATE_TYPE="bin"
BEVY="true"
WORKSPACE_ROOT=""
PUBLISHED="false"
DESCRIPTION=""
KEYWORDS=""
CATEGORIES=""
SHARED_DEP="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib)            CRATE_TYPE="lib" ;;
    --no-bevy)        BEVY="false" ;;
    --workspace-root) WORKSPACE_ROOT="${2:?--workspace-root needs a path}"; shift ;;
    --published)      PUBLISHED="true" ;;
    --description)    DESCRIPTION="${2:-}"; shift ;;
    --keywords)       KEYWORDS="${2:-}"; shift ;;
    --categories)     CATEGORIES="${2:-}"; shift ;;
    --shared-dep)     SHARED_DEP="true" ;;
    -h|--help)        usage ;;
    *)                echo "Unknown option: $1" >&2; usage ;;
  esac
  shift
done

# --- shared setup ---------------------------------------------------------

if ! command -v cargo-generate &>/dev/null; then
  echo "=== Installing cargo-generate ==="
  cargo install cargo-generate
fi

[[ -f "$TEMPLATE_DIR/cargo-generate.toml" ]] || {
  echo "Error: template not found at $TEMPLATE_DIR" >&2; exit 1
}

# =========================================================================
# Workspace member
# =========================================================================
if [[ -n "$WORKSPACE_ROOT" ]]; then
  ROOT="${WORKSPACE_ROOT%/}"
  [[ -f "$ROOT/Cargo.toml" ]] || { echo "Error: $ROOT/Cargo.toml not found" >&2; exit 1; }
  grep -q '^\[workspace\]' "$ROOT/Cargo.toml" || {
    echo "Error: $ROOT is not a Cargo workspace root (no [workspace] table)" >&2; exit 1
  }

  CRATES_DIR="$ROOT/crates"
  DEST="$CRATES_DIR/$NAME"
  [[ -d "$DEST" ]] && { echo "Error: $DEST already exists" >&2; exit 1; }
  mkdir -p "$CRATES_DIR"

  # clippy::cargo_common_metadata (denied via the workspace cargo lint group)
  # treats empty keywords/categories as missing on any publishable crate, so a
  # member must supply both.
  [[ -n "$KEYWORDS" ]]   || { echo "Error: member crates need --keywords (comma-separated, non-empty)" >&2; exit 1; }
  [[ -n "$CATEGORIES" ]] || { echo "Error: member crates need --categories (comma-separated, non-empty)" >&2; exit 1; }

  # Derive homepage from the workspace repository URL (the GitHub repo name,
  # which is not always the local workspace dir name).
  REPO_URL=$(grep -E '^\s*repository\s*=' "$ROOT/Cargo.toml" | head -1 | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')
  HOMEPAGE="${REPO_URL%/}/tree/main/crates/$NAME"

  echo "=== Generating workspace member from template ==="
  cargo generate --path "$TEMPLATE_DIR" \
    --silent \
    --vcs none \
    --name "$NAME" \
    --destination "$CRATES_DIR" \
    --define "bevy=$BEVY" \
    --define "workspace_member=true" \
    --define "published=$PUBLISHED" \
    --define "description=$DESCRIPTION" \
    --define "keywords=$KEYWORDS" \
    --define "categories=$CATEGORIES" \
    --define "homepage_url=$HOMEPAGE" \
    "--$CRATE_TYPE"

  if [[ "$BEVY" == "true" ]]; then
    echo "=== Adding bevy ==="
    # Inherits `bevy.workspace = true` when the root declares bevy in
    # [workspace.dependencies]; otherwise adds a versioned dep.
    ( cd "$DEST" && cargo add bevy )
  fi

  if [[ "$SHARED_DEP" == "true" ]]; then
    echo "=== Registering $NAME in [workspace.dependencies] ==="
    python3 - "$ROOT/Cargo.toml" "$NAME" <<'PY'
import re, sys
path, name = sys.argv[1], sys.argv[2]
src = open(path).read()
if re.search(rf'^\s*{re.escape(name)}\s*=', src, re.M):
    print("  already present, skipping")
else:
    line = f'{name} = {{ path = "crates/{name}" }}\n'
    new = re.sub(r'^\[workspace\.dependencies\]\n', lambda m: m.group(0) + line, src, count=1, flags=re.M)
    if new == src:
        sys.exit("  [workspace.dependencies] not found — add the path dep manually")
    open(path, 'w').write(new)
    print(f"  added {name} = {{ path = \"crates/{name}\" }}")
PY
  fi

  # Enroll in the nightly clean-fix flow (config lives outside the workspace).
  CONF="$HOME/.claude/scripts/clean-fix/clean-fix.conf"
  ROOTDIR=$(basename "$ROOT")
  if [[ -f "$CONF" ]]; then
    echo "=== Enrolling in clean-fix ==="
    python3 - "$CONF" "$ROOTDIR" "$NAME" <<'PY'
import sys
conf, rootdir, name = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(conf).read().splitlines()

def section_range(sec):
    start = next((i for i, l in enumerate(lines) if l.strip() == f'[{sec}]'), None)
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith('[')), len(lines))
    return start, end

def ensure(sec, entry):
    rng = section_range(sec)
    if rng is None:
        print(f"  [{sec}] section not found — skipping {entry}", file=sys.stderr)
        return
    s, e = rng
    if entry in (lines[k].strip() for k in range(s + 1, e)):
        print(f"  {entry} already in [{sec}]")
        return
    ins = e
    while ins - 1 > s and lines[ins - 1].strip() == '':
        ins -= 1
    lines.insert(ins, entry)
    print(f"  added {entry} to [{sec}]")

ensure('build', rootdir)
ensure('projects', f'{rootdir}/crates/{name}')
open(conf, 'w').write('\n'.join(lines) + '\n')
PY
  fi

  # clippy, not build: the workspace denies pedantic/cargo lint groups that a
  # plain `cargo build` never runs (e.g. cargo_common_metadata, doc_markdown).
  # clippy compiles as well, so this also serves as the build check.
  echo "=== Checking $NAME (clippy) ==="
  ( cd "$ROOT" && cargo clippy -p "$NAME" )

  echo "=== Formatting ==="
  ( cd "$ROOT" && cargo +nightly fmt -p "$NAME" )
  # Run taplo from the workspace root so it loads the workspace's taplo.toml and
  # its include/exclude patterns match the relative paths. Running it from an
  # unrelated cwd loads the wrong config and silently excludes both files.
  ( cd "$ROOT" && taplo fmt Cargo.toml "crates/$NAME/Cargo.toml" )

  echo "=== Committing to the workspace repo ==="
  ( cd "$ROOT" && git add "crates/$NAME" Cargo.toml && git commit -m "feat: scaffold $NAME workspace member" )

  echo "=== Done! ==="
  echo "Member: $DEST"
  echo "Built, formatted, clean-fix enrolled, and committed to $ROOTDIR. Push when ready."
  exit 0
fi

# =========================================================================
# Standalone (original behavior)
# =========================================================================
DEST="$HOME/rust/$NAME"

if [[ -d "$DEST" ]]; then
  echo "Error: $DEST already exists" >&2
  exit 1
fi

echo "=== Generating project from template ==="
cargo generate --path "$TEMPLATE_DIR" \
  --silent \
  --name "$NAME" \
  --destination "$HOME/rust" \
  --define "bevy=$BEVY" \
  --define "workspace_member=false" \
  "--$CRATE_TYPE"

cd "$DEST"

if [[ "$BEVY" == "true" ]]; then
  echo "=== Adding Bevy dependencies ==="
  cargo add bevy --no-default-features
  if [[ "$CRATE_TYPE" == "lib" ]]; then
    cargo add --dev bevy
  fi
fi

echo "=== Excluding settings.local.json from git ==="
echo "settings.local.json" >> .git/info/exclude

echo "=== Creating default settings.local.json ==="
mkdir -p .claude
cp "$HOME/.claude/templates/settings_local.json" .claude/settings.local.json

if ! command -v cargo-mend &>/dev/null; then
  echo "=== Installing cargo-mend ==="
  cargo install cargo-mend
fi

echo "=== Formatting Cargo.toml ==="
taplo fmt Cargo.toml

echo "=== Generating Cargo.lock ==="
cargo generate-lockfile

echo "=== Creating initial commit ==="
git add -A
git commit -m "Initial commit"

echo "=== Done! ==="
echo "Project: $DEST"
