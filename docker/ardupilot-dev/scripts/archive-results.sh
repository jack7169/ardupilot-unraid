#!/bin/bash
# Archive current buildlogs into history/ before a new test run
BUILDLOGS="${BUILDLOGS:-/tmp/buildlogs}"
HISTORY="${BUILDLOGS}/history"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Only archive if there are files to move
shopt -s nullglob
files=("${BUILDLOGS}"/*)
shopt -u nullglob

has_files=false
for f in "${files[@]}"; do
    [ "$(basename "$f")" = "history" ] && continue
    has_files=true
    break
done

if [ "$has_files" = true ]; then
    mkdir -p "${HISTORY}/${TIMESTAMP}"
    for f in "${files[@]}"; do
        [ "$(basename "$f")" = "history" ] && continue
        mv "$f" "${HISTORY}/${TIMESTAMP}/"
    done
    echo "Archived buildlogs to ${HISTORY}/${TIMESTAMP}/"
else
    echo "No buildlogs to archive"
fi
