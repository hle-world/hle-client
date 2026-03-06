#!/usr/bin/env bash
# Release script for hle-client
#
# Usage:
#   ./scripts/release.sh 1.3.0
#   ./scripts/release.sh 1.3.0 --dry-run
#   ./scripts/release.sh 1.3.0 --tag   (manual fallback if auto-release didn't trigger)
#
# What it does:
#   1. Validates the version format
#   2. Updates version in pyproject.toml, __init__.py, README.md, install.sh
#   3. Adds a CHANGELOG.md entry (you fill in the details)
#   4. Creates a chore/release-X.Y.Z branch, commits, pushes, opens a PR
#   5. When the PR is merged, auto-release.yml creates the GitHub release
#      which triggers PyPI publish + Homebrew update automatically
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
VERSION="${1:-}"
DRY_RUN=false
TAG_ONLY=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --tag)     TAG_ONLY=true ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version> [--dry-run] [--tag]"
  echo ""
  echo "Examples:"
  echo "  $0 1.3.0           # Bump version, commit, push, open PR"
  echo "  $0 1.3.0 --dry-run # Show what would change without modifying files"
  echo "  $0 1.3.0 --tag     # Manual fallback: create GitHub release"
  exit 1
fi

# Validate semver format
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: Version must be semver (e.g. 1.3.0), got: $VERSION"
  exit 1
fi

# hle-client must always use patch version 0 — patch numbers are reserved
# for peripheral repos (ha-addon, hle-docker) to use for their own fixes.
PATCH="${VERSION##*.}"
if [[ "$PATCH" != "0" ]]; then
  echo "Error: hle-client versions must end in .0 (e.g. 1.14.0), got: $VERSION"
  echo "Patch versions are reserved for ha-addon and hle-docker."
  exit 1
fi

CURRENT_VERSION=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')
echo "Current version: $CURRENT_VERSION"
echo "New version:     $VERSION"
echo ""

# ---------------------------------------------------------------------------
# --tag mode: manual fallback to create GitHub release
# ---------------------------------------------------------------------------
if $TAG_ONLY; then
  echo "Creating GitHub release v$VERSION..."

  if $DRY_RUN; then
    echo "[dry-run] Would run: gh release create v$VERSION ..."
    exit 0
  fi

  # Extract the changelog entry for this version
  NOTES=$(sed -n "/^## v$VERSION/,/^## v/{ /^## v$VERSION/d; /^## v/d; p; }" CHANGELOG.md | sed '/^$/{ N; /^\n$/d; }')

  if [[ -z "$NOTES" ]]; then
    echo "Warning: No CHANGELOG.md entry found for v$VERSION"
    NOTES="Release v$VERSION"
  fi

  gh release create "v$VERSION" \
    --title "v$VERSION" \
    --notes "$NOTES"

  echo ""
  echo "Release created: https://github.com/hle-world/hle-client/releases/tag/v$VERSION"
  echo "PyPI publish and Homebrew update will be triggered automatically."
  exit 0
fi

# ---------------------------------------------------------------------------
# Version bump mode
# ---------------------------------------------------------------------------

# Files to update
FILES_CHANGED=()

# 1. pyproject.toml — version = "X.Y.Z"
echo "Updating pyproject.toml..."
if $DRY_RUN; then
  grep "^version" pyproject.toml
else
  sed -i '' "s/^version = \"$CURRENT_VERSION\"/version = \"$VERSION\"/" pyproject.toml
  FILES_CHANGED+=(pyproject.toml)
fi

# 2. src/hle_client/__init__.py — __version__ = "X.Y.Z"
echo "Updating src/hle_client/__init__.py..."
if $DRY_RUN; then
  grep "__version__" src/hle_client/__init__.py
else
  sed -i '' "s/__version__ = \"$CURRENT_VERSION\"/__version__ = \"$VERSION\"/" src/hle_client/__init__.py
  FILES_CHANGED+=(src/hle_client/__init__.py)
fi

# 3. README.md — --version X.Y.Z in curl example
echo "Updating README.md..."
if $DRY_RUN; then
  grep -n "\-\-version" README.md || true
else
  sed -i '' "s/--version [0-9]*\.[0-9]*\.[0-9]*/--version $VERSION/g" README.md
  FILES_CHANGED+=(README.md)
fi

# 4. install.sh — --version X.Y.Z in comment
echo "Updating install.sh..."
if $DRY_RUN; then
  grep -n "\-\-version" install.sh || true
else
  sed -i '' "s/--version [0-9]*\.[0-9]*\.[0-9]*/--version $VERSION/g" install.sh
  FILES_CHANGED+=(install.sh)
fi

# 5. CHANGELOG.md — add new entry at top (after the # Changelog header)
echo "Updating CHANGELOG.md..."
DATE=$(date +%Y-%m-%d)
CHANGELOG_ENTRY="## v$VERSION — $DATE

<!-- TODO: Fill in release notes before merging -->

"

if $DRY_RUN; then
  echo "Would prepend to CHANGELOG.md:"
  echo "$CHANGELOG_ENTRY"
else
  # Insert after the first line (# Changelog\n)
  TEMP=$(mktemp)
  head -1 CHANGELOG.md > "$TEMP"
  echo "" >> "$TEMP"
  printf "%s" "$CHANGELOG_ENTRY" >> "$TEMP"
  tail -n +3 CHANGELOG.md >> "$TEMP"
  mv "$TEMP" CHANGELOG.md
  FILES_CHANGED+=(CHANGELOG.md)
fi

echo ""

if $DRY_RUN; then
  echo "[dry-run] No files were modified."
  exit 0
fi

# Show what changed
echo "Files updated:"
for f in "${FILES_CHANGED[@]}"; do
  echo "  $f"
done
echo ""

# Create branch, commit, push, open PR
BRANCH="chore/release-$VERSION"
echo "Creating branch $BRANCH..."
git checkout -b "$BRANCH"
git add "${FILES_CHANGED[@]}"
git commit -m "Bump version to $VERSION"
git push -u origin "$BRANCH"

echo ""
echo "Opening PR..."
gh pr create \
  --title "Release v$VERSION" \
  --body "Bump version to \`$VERSION\` and update all version references.

When this PR is merged, a GitHub release will be created automatically,
which triggers PyPI publish and Homebrew formula update."

echo ""
echo "Next steps:"
echo "  1. Edit CHANGELOG.md in the PR to fill in release notes"
echo "  2. Merge the PR — release is created automatically"
