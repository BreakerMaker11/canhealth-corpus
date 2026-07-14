#!/usr/bin/env bash
# Copy processed outputs to the course repo's data/health/ directory.
# Usage: ./sync_to_course.sh <course_repo_path>
set -euo pipefail

DEST="${1:?Usage: $0 <course_repo_path>}"
mkdir -p "$DEST/data/health"

FILES=(
    data/processed/corpus.csv
    data/processed/train.csv
    data/processed/dev.csv
    data/processed/gold_test.csv
    data/processed/rag_chunks.jsonl
)

for f in "${FILES[@]}"; do
    if [[ -f "$f" ]]; then
        cp "$f" "$DEST/data/health/"
        echo "  copied $(basename "$f")"
    else
        echo "  MISSING $f — skipping"
    fi
done

echo "Done -> $DEST/data/health/"
