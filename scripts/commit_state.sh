#!/usr/bin/env bash
# Commit state/state.db and push it, retrying when the remote moved in the meantime.
# Only runs on GitHub Actions (it may hard-reset the checkout to the remote branch).
# Usage: scripts/commit_state.sh "reason"
set -euo pipefail
cd "$(dirname "$0")/.."

DB=state/state.db
REASON="${1:-update}"

if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
  echo "commit_state.sh only runs on GitHub Actions" >&2
  exit 0
fi
[ -f "$DB" ] || exit 0

BRANCH="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"
BACKUP="${RUNNER_TEMP:-/tmp}/state.db.mine"
git config user.name "autopost-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit() {
  git add "$DB"
  if git diff --cached --quiet; then
    return 1
  fi
  git commit -q -m "chore(state): ${REASON} [skip ci]"
}

commit || exit 0

for attempt in 1 2 3 4 5; do
  if git push -q origin "HEAD:${BRANCH}"; then
    exit 0
  fi
  echo "push rejected (attempt ${attempt}); merging our state with origin/${BRANCH}" >&2
  sleep $((attempt * 2))
  # Never overwrite: another run may have recorded posts too. Merge both copies by natural keys.
  cp "$DB" "$BACKUP"
  git fetch -q origin "$BRANCH"
  THEIRS="${RUNNER_TEMP:-/tmp}/state.db.theirs"
  rm -f "$THEIRS"
  git show "origin/${BRANCH}:${DB}" > "$THEIRS" 2>/dev/null || rm -f "$THEIRS"
  git reset -q --hard "origin/${BRANCH}"
  if [ -s "$THEIRS" ]; then
    python -m autopost merge-state --theirs "$THEIRS" --ours "$BACKUP" --out "$DB"
  else
    cp "$BACKUP" "$DB"
  fi
  commit || exit 0
done

echo "could not push state after 5 attempts" >&2
exit 1
