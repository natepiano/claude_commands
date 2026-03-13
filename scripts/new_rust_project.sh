#!/usr/bin/env bash
# Scaffold a new Rust project from natepiano/rust-template and push to GitHub.
# NOTE: `gh` commands require dangerouslyDisableSandbox: true in Claude Code.
set -euo pipefail

usage() {
  echo "Usage: $0 <project-name> [--lib] [--no-bevy]" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

PROJECT_NAME="$1"
shift

CRATE_TYPE="bin"
BEVY="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib)     CRATE_TYPE="lib" ;;
    --no-bevy) BEVY="false" ;;
    *)         echo "Unknown option: $1" >&2; usage ;;
  esac
  shift
done

DEST="$HOME/rust/$PROJECT_NAME"

if [[ -d "$DEST" ]]; then
  echo "Error: $DEST already exists" >&2
  exit 1
fi

echo "=== Generating project from template ==="
cargo generate natepiano/rust-template \
  --name "$PROJECT_NAME" \
  --destination "$HOME/rust" \
  --define "bevy=$BEVY" \
  "--$CRATE_TYPE"

cd "$DEST"

echo "=== Formatting Cargo.toml ==="
taplo fmt Cargo.toml

echo "=== Creating initial commit ==="
git add -A
git commit -m "Initial commit"

echo "=== Creating GitHub repo ==="
if ! gh repo create "natepiano/$PROJECT_NAME" --public --source . --push; then
  echo "" >&2
  echo "GitHub repo creation failed. Local project is intact at: $DEST" >&2
  echo "To recover manually:" >&2
  echo "  cd $DEST" >&2
  echo "  gh repo create natepiano/$PROJECT_NAME --public --source . --push" >&2
  exit 1
fi

echo "=== Done! ==="
echo "Project: $DEST"
echo "GitHub:  https://github.com/natepiano/$PROJECT_NAME"
