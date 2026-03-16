#!/usr/bin/env bash
# Generate a Rust project or example from natepiano/rust-template.
# NOTE: `gh` commands require dangerouslyDisableSandbox: true in Claude Code.
set -euo pipefail

EXAMPLE_TEMPLATE="$HOME/.claude/templates/example.rs"

usage() {
  echo "Usage: $0 <name> [--lib] [--no-bevy] [--example] [--include-github-repo]" >&2
  echo "" >&2
  echo "  --example              Copy example template into \$PWD/examples/<name>.rs and add dev-dependencies" >&2
  echo "  --include-github-repo  Create a GitHub repo and push (project only, off by default)" >&2
  echo "  --lib                  Generate a library crate (project only)" >&2
  echo "  --no-bevy              Skip Bevy CI support (project only)" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

NAME="$1"
shift

CRATE_TYPE="bin"
BEVY="true"
EXAMPLE="false"
INCLUDE_GITHUB="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib)                  CRATE_TYPE="lib" ;;
    --no-bevy)              BEVY="false" ;;
    --example)              EXAMPLE="true" ;;
    --include-github-repo)  INCLUDE_GITHUB="true" ;;
    -h|--help)              usage ;;
    *)                      echo "Unknown option: $1" >&2; usage ;;
  esac
  shift
done

# === Example flow ===
if [[ "$EXAMPLE" == "true" ]]; then
  DEST="$PWD/examples/$NAME.rs"

  if [[ -f "$DEST" ]]; then
    echo "Error: $DEST already exists" >&2
    exit 1
  fi

  if [[ ! -f "$EXAMPLE_TEMPLATE" ]]; then
    echo "Error: example template not found at $EXAMPLE_TEMPLATE" >&2
    exit 1
  fi

  mkdir -p "$PWD/examples"

  echo "=== Copying example template ==="
  cp "$EXAMPLE_TEMPLATE" "$DEST"

  # Add [[example]] entry if not already present
  if ! grep -q "name = \"$NAME\"" "$PWD/Cargo.toml" 2>/dev/null; then
    echo "=== Adding [[example]] to Cargo.toml ==="
    cat >> "$PWD/Cargo.toml" <<EOF

[[example]]
name = "$NAME"
EOF
  else
    echo "=== [[example]] entry for '$NAME' already exists ==="
  fi

  echo "=== Adding dev-dependencies ==="
  cargo add --dev bevy bevy_brp_extras bevy_panorbit_camera bevy_window_manager
  cargo add --dev --git https://github.com/natepiano/bevy_panorbit_camera_ext bevy_panorbit_camera_ext

  echo "=== Done! ==="
  echo "Example: $DEST"
  exit 0
fi

# === Project flow ===
DEST="$HOME/rust/$NAME"

if [[ -d "$DEST" ]]; then
  echo "Error: $DEST already exists" >&2
  exit 1
fi

CARGO_GEN_CONFIG="${CARGO_HOME:-$HOME/.cargo}/cargo-generate.toml"
if ! grep -q 'favorites.rust-template' "$CARGO_GEN_CONFIG" 2>/dev/null; then
  echo "=== Adding rust-template to cargo-generate favorites ==="
  cat >> "$CARGO_GEN_CONFIG" <<'EOF'

[favorites.rust-template]
git = "https://github.com/natepiano/rust-template"
description = "natepiano's Rust project template"
EOF
fi

echo "=== Generating project from template ==="
cargo generate rust-template \
  --name "$NAME" \
  --destination "$HOME/rust" \
  --define "bevy=$BEVY" \
  "--$CRATE_TYPE"

cd "$DEST"

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

if [[ "$INCLUDE_GITHUB" == "true" ]]; then
  echo "=== Creating GitHub repo ==="
  if ! gh repo create "natepiano/$NAME" --public --source . --push; then
    echo "" >&2
    echo "GitHub repo creation failed. Local project is intact at: $DEST" >&2
    echo "To recover manually:" >&2
    echo "  cd $DEST" >&2
    echo "  gh repo create natepiano/$NAME --public --source . --push" >&2
    exit 1
  fi

  echo "=== Done! ==="
  echo "Project: $DEST"
  echo "GitHub:  https://github.com/natepiano/$NAME"
else
  echo "=== Done! ==="
  echo "Project: $DEST"
fi
