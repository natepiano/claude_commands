#!/usr/bin/env bash
# Ensure Bevy repository is cloned and migration guides are available.
#
# For RC versions: guides ship in the repo. The containing directory was named
#   release-content/migration-guides/ (<= 0.18) and renamed to
#   _release-content/migration-guides/ (0.19+); both are detected.
# For final releases: guides are fetched from bevy-website and split into files
#
# Prints the resolved migration-guides directory as the final stdout line;
# all progress output goes to stderr.
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

# Register this Bevy clone in the clean-fix skip list (idempotent).
# The clone lives under ~/rust/, which the nightly clean-fix automation scans by
# directory name and would otherwise clean/build/style-eval. clean-fix.sh parses
# bare lines under the conf's [exclude] section, so insert the clone's directory
# name there if absent. All output goes to stderr; callers capture only stdout.
register_in_clean_fix_skiplist() {
    local conf="${HOME}/.claude/scripts/clean-fix/clean-fix.conf"
    local name="$1"
    [ -f "${conf}" ] || return 0
    if grep -qxF "${name}" "${conf}"; then
        echo "clean-fix skip list already excludes ${name}" >&2
        return 0
    fi
    if grep -qxF "[exclude]" "${conf}"; then
        :
    else
        echo "Warning: no [exclude] section in ${conf}; skip-list not updated" >&2
        return 0
    fi
    # Write the temp alongside the conf so it shares the filesystem (atomic mv)
    # and stays within a writable directory rather than a restricted TMPDIR.
    local tmp="${conf}.tmp.$$"
    if awk -v entry="${name}" '
        { print }
        $0 == "[exclude]" && !inserted { print entry; inserted = 1 }
    ' "${conf}" > "${tmp}"; then
        mv "${tmp}" "${conf}"
        echo "Added ${name} to clean-fix skip list" >&2
    else
        rm -f "${tmp}"
        echo "Warning: failed to update clean-fix skip list for ${name}" >&2
    fi
}

register_in_clean_fix_skiplist "$(basename "${BEVY_REPO_DIR}")" || true

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

# Resolve the migration-guides directory. The containing dir was renamed from
# release-content (<= 0.18) to _release-content (0.19+); detect whichever holds
# guides. RC versions ship guides in the repo; final releases do not.
GUIDES_DIR=""
for candidate in \
    "${BEVY_REPO_DIR}/release-content/migration-guides" \
    "${BEVY_REPO_DIR}/_release-content/migration-guides"; do
    if [ -d "${candidate}" ] \
       && [ "$(find "${candidate}" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')" -ne 0 ]; then
        GUIDES_DIR="${candidate}"
        break
    fi
done

if [ -n "${GUIDES_DIR}" ]; then
    GUIDE_COUNT=$(find "${GUIDES_DIR}" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    echo "Found ${GUIDE_COUNT} migration guides in ${GUIDES_DIR}" >&2
else
    # Final releases ship no guides in the repo; fetch and split from bevy-website.
    echo "No migration guides in repo, fetching from bevy-website..." >&2
    GUIDES_DIR="${BEVY_REPO_DIR}/release-content/migration-guides"
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
fi

# Final stdout line: the resolved migration-guides directory (callers capture this).
echo "${GUIDES_DIR}"
