#!/usr/bin/env bash
# Copy processed outputs to the course repo's data/health/ directory.
# Usage: ./sync_to_course.sh <course_repo_path>
set -euo pipefail
DEST="${1:?Usage: $0 <course_repo_path>}"
mkdir -p "$DEST/data/health"
cp data/processed/* "$DEST/data/health/"
echo "Synced data/processed/ -> $DEST/data/health/"
