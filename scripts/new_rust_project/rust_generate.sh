#!/usr/bin/env bash
# Generate a Rust project from natepiano/rust-template.
# NOTE: `gh` commands require dangerouslyDisableSandbox: true in Claude Code.
set -euo pipefail

usage() {
  echo "Usage: $0 <name> [--lib] [--no-bevy]" >&2
  echo "" >&2
  echo "  --lib        Generate a library crate" >&2
  echo "  --no-bevy    Skip Bevy CI support" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

NAME="$1"
shift

CRATE_TYPE="bin"
BEVY="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib)      CRATE_TYPE="lib" ;;
    --no-bevy)  BEVY="false" ;;
    -h|--help)  usage ;;
    *)          echo "Unknown option: $1" >&2; usage ;;
  esac
  shift
done

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
