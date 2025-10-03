#!/usr/bin/env bash
# Ensure Bevy repository is cloned and checked out to the specified version
#
# Usage: bevy_migration_ensure_repo.sh <version>
# Example: bevy_migration_ensure_repo.sh 0.17.1
#
# Exit codes: 0 = success, 1 = error

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>" >&2
    echo "Example: $0 0.17.1" >&2
    exit 1
fi

VERSION="$1"
BEVY_REPO_DIR="${HOME}/rust/bevy-${VERSION}"

# Check if repository already exists
if [ -d "${BEVY_REPO_DIR}/.git" ]; then
    echo "Repository already exists at ${BEVY_REPO_DIR}" >&2
    git -C "${BEVY_REPO_DIR}" checkout "v${VERSION}"
else
    echo "Cloning Bevy ${VERSION} to ${BEVY_REPO_DIR}" >&2
    rm -rf "${BEVY_REPO_DIR}"  # Clean any partial/corrupt directories
    mkdir -p "$(dirname "${BEVY_REPO_DIR}")"
    git clone https://github.com/bevyengine/bevy.git "${BEVY_REPO_DIR}"
    git -C "${BEVY_REPO_DIR}" checkout "v${VERSION}"
fi

echo "${BEVY_REPO_DIR}"
