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
  echo "push rejected (attempt ${attempt}); rebuilding the state commit on top of origin/${BRANCH}" >&2
  sleep $((attempt * 2))
  # Only this workflow writes the DB (concurrency group), so our copy is always the newest.
  cp "$DB" "$BACKUP"
  git fetch -q origin "$BRANCH"
  git reset -q --hard "origin/${BRANCH}"
  cp "$BACKUP" "$DB"
  commit || exit 0
done

echo "could not push state after 5 attempts" >&2
exit 1
