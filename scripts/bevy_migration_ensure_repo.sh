#!/usr/bin/env bash
# Ensure Bevy repository is cloned and migration guides are available.
#
# For RC versions: guides are in release-content/migration-guides/
# For final releases: guides are fetched from bevy-website and split into files
#
# Usage: bevy_migration_ensure_repo.sh <version>
# Example: bevy_migration_ensure_repo.sh 0.18.0
#
# Exit codes: 0 = success, 1 = error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>" >&2
    echo "Example: $0 0.18.0" >&2
    exit 1
fi

VERSION="$1"
BEVY_REPO_DIR="${HOME}/rust/bevy-${VERSION}"
GUIDES_DIR="${BEVY_REPO_DIR}/release-content/migration-guides"

# Clone or update repository
if [ -d "${BEVY_REPO_DIR}/.git" ]; then
    echo "Repository already exists at ${BEVY_REPO_DIR}" >&2
    git -C "${BEVY_REPO_DIR}" checkout "v${VERSION}" 2>/dev/null || true
else
    echo "Cloning Bevy ${VERSION} to ${BEVY_REPO_DIR}" >&2
    rm -rf "${BEVY_REPO_DIR}"
    mkdir -p "$(dirname "${BEVY_REPO_DIR}")"
    git clone --depth 1 --branch "v${VERSION}" https://github.com/bevyengine/bevy.git "${BEVY_REPO_DIR}"
fi

# Check if migration guides exist in repo (RC versions have them, final releases don't)
GUIDE_COUNT=$(find "${GUIDES_DIR}" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')

if [ "${GUIDE_COUNT}" -eq 0 ]; then
    echo "No migration guides in repo, fetching from bevy-website..." >&2

    # Ensure the directory exists
    mkdir -p "${GUIDES_DIR}"

    # Use the split script to fetch and split the consolidated guide
    "${SCRIPT_DIR}/bevy_migration_split_guide.py" \
        --version "${VERSION}" \
        --output-dir "${GUIDES_DIR}" >&2

    # Verify guides were created
    GUIDE_COUNT=$(find "${GUIDES_DIR}" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    if [ "${GUIDE_COUNT}" -eq 0 ]; then
        echo "Error: Failed to fetch migration guides from bevy-website" >&2
        exit 1
    fi
    echo "Fetched ${GUIDE_COUNT} migration guides from bevy-website" >&2
else
    echo "Found ${GUIDE_COUNT} migration guides in repository" >&2
fi

echo "${BEVY_REPO_DIR}"
